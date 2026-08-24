from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from botocore.exceptions import ClientError

from teutonic.storage.artifacts import ArtifactIntegrityError

from .contracts import Manifest
from .crypto import verify_hotkey_signature


class GenesisContractMismatch(ArtifactIntegrityError):
    """A submitted model differs from the immutable genesis contract files."""


class UploadQuotaExceeded(ArtifactIntegrityError):
    """A private registration prefix exceeds the model upload limit."""


MAX_MINER_UPLOAD_BYTES = 250_000_000_000
_REGISTRATION_ROOT = "models/registrations/"
_REGISTRATION_KEY = re.compile(
    r"^models/registrations/(?P<registration>[0-9a-f]{64})/.+"
)


@dataclass(frozen=True, slots=True)
class VerifiedManifest:
    manifest: Manifest
    manifest_sha256: str
    manifest_size: int
    source_etags: Mapping[str, str | None]


@dataclass(frozen=True, slots=True)
class ImmutableSnapshotResult:
    bucket: str
    prefix: str
    version: str
    manifest_size: int
    etags: Mapping[str, str | None]


class R2UploadController:
    """Verify direct miner uploads in the private model bucket."""

    def __init__(
        self,
        s3_client: Any,
        *,
        private_model_bucket: str,
        genesis_contract_files: Mapping[str, str],
        chunk_size: int = 1024 * 1024,
        max_upload_bytes: int = MAX_MINER_UPLOAD_BYTES,
    ) -> None:
        if not private_model_bucket:
            raise ValueError("private model bucket is required")
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        if max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be positive")
        if not genesis_contract_files:
            raise ValueError("genesis contract files are required")
        if any(
            not path
            or path == "manifest.json"
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or str(PurePosixPath(path)) != path
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for path, digest in genesis_contract_files.items()
        ):
            raise ValueError("genesis contract file lock is invalid")
        self.s3 = s3_client
        self.private_model_bucket = private_model_bucket
        self.genesis_contract_files = dict(genesis_contract_files)
        self.chunk_size = chunk_size
        self.max_upload_bytes = max_upload_bytes

    @staticmethod
    def _registration_from_key(key: str) -> str | None:
        match = _REGISTRATION_KEY.fullmatch(key)
        return None if match is None else match.group("registration")

    def _multipart_uploads(self, prefix: str) -> tuple[dict[str, Any], ...]:
        uploads: list[dict[str, Any]] = []
        key_marker: str | None = None
        upload_marker: str | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": self.private_model_bucket,
                "Prefix": prefix,
            }
            if key_marker:
                request["KeyMarker"] = key_marker
            if upload_marker:
                request["UploadIdMarker"] = upload_marker
            response = self.s3.list_multipart_uploads(**request)
            uploads.extend(dict(item) for item in response.get("Uploads", ()))
            if not response.get("IsTruncated"):
                return tuple(uploads)
            key_marker = response.get("NextKeyMarker")
            upload_marker = response.get("NextUploadIdMarker")
            if not key_marker:
                raise ArtifactIntegrityError(
                    "truncated multipart listing omitted its continuation marker"
                )

    def _multipart_size(self, *, key: str, upload_id: str) -> int:
        total = 0
        marker: int | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": self.private_model_bucket,
                "Key": key,
                "UploadId": upload_id,
            }
            if marker is not None:
                request["PartNumberMarker"] = marker
            try:
                response = self.s3.list_parts(**request)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "NoSuchUpload":
                    return 0
                raise
            for part in response.get("Parts", ()):
                size = int(part["Size"])
                if size < 0:
                    raise ArtifactIntegrityError("R2 multipart part reported a negative size")
                total += size
            if not response.get("IsTruncated"):
                return total
            next_marker = response.get("NextPartNumberMarker")
            if next_marker is None:
                raise ArtifactIntegrityError(
                    "truncated R2 part listing omitted its continuation marker"
                )
            marker = int(next_marker)

    def registration_upload_usage(
        self, registrations: Iterable[str]
    ) -> dict[str, int]:
        """Return completed plus in-progress bytes for active registration prefixes."""
        selected = set(registrations)
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in selected):
            raise ValueError("registration upload usage requires canonical identifiers")
        usage = {registration: 0 for registration in selected}
        if not selected:
            return usage

        for key, item in self._list_objects(
            self.private_model_bucket, _REGISTRATION_ROOT
        ).items():
            registration = self._registration_from_key(key)
            if registration not in selected:
                continue
            size = int(item["Size"])
            if size < 0:
                raise ArtifactIntegrityError("R2 object reported a negative size")
            usage[registration] += size

        for upload in self._multipart_uploads(_REGISTRATION_ROOT):
            key = str(upload["Key"])
            registration = self._registration_from_key(key)
            if registration not in selected:
                continue
            usage[registration] += self._multipart_size(
                key=key,
                upload_id=str(upload["UploadId"]),
            )
        return usage

    def _validate_genesis_contract(self, manifest: Manifest) -> None:
        submitted = {item.path: item.sha256 for item in manifest.files}
        missing = sorted(set(self.genesis_contract_files) - set(submitted))
        changed = sorted(
            path
            for path, expected in self.genesis_contract_files.items()
            if submitted.get(path) is not None and submitted[path] != expected
        )
        if missing or changed:
            raise GenesisContractMismatch(
                "submitted model does not byte-match genesis contract files; "
                f"missing={missing}, changed={changed}"
            )

    def _list_objects(self, bucket: str, prefix: str) -> dict[str, dict[str, Any]]:
        objects: dict[str, dict[str, Any]] = {}
        continuation: str | None = None
        while True:
            request: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation:
                request["ContinuationToken"] = continuation
            response = self.s3.list_objects_v2(**request)
            for item in response.get("Contents", []):
                key = item["Key"]
                if key in objects:
                    raise ArtifactIntegrityError(f"R2 listing repeated object key {key!r}")
                if not key.endswith("/"):
                    objects[key] = item
            if not response.get("IsTruncated"):
                return objects
            continuation = response.get("NextContinuationToken")
            if not continuation:
                raise ArtifactIntegrityError("truncated R2 listing omitted continuation token")

    def _assert_no_multipart_uploads(self, prefix: str) -> None:
        key_marker: str | None = None
        upload_marker: str | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": self.private_model_bucket,
                "Prefix": prefix,
            }
            if key_marker:
                request["KeyMarker"] = key_marker
            if upload_marker:
                request["UploadIdMarker"] = upload_marker
            response = self.s3.list_multipart_uploads(**request)
            if response.get("Uploads"):
                raise ArtifactIntegrityError(
                    "private model prefix contains unfinished multipart uploads"
                )
            if not response.get("IsTruncated"):
                return
            key_marker = response.get("NextKeyMarker")
            upload_marker = response.get("NextUploadIdMarker")
            if not key_marker:
                raise ArtifactIntegrityError(
                    "truncated multipart listing omitted its continuation marker"
                )

    def _read_and_hash(
        self, bucket: str, key: str, *, capture: bool = False
    ) -> tuple[bytes | None, int, str, str | None, str | None]:
        response = self.s3.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if capture else None
        size = 0
        try:
            while True:
                chunk = body.read(self.chunk_size)
                if not chunk:
                    break
                if chunks is not None:
                    chunks.append(chunk)
                size += len(chunk)
                digest.update(chunk)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        captured = b"".join(chunks) if chunks is not None else None
        metadata = {
            str(name).lower(): str(value).lower()
            for name, value in (response.get("Metadata") or {}).items()
        }
        return captured, size, digest.hexdigest(), response.get("ETag"), metadata.get("sha256")

    def _head_object(self, bucket: str, key: str) -> tuple[int, str | None, str | None]:
        response = self.s3.head_object(Bucket=bucket, Key=key)
        metadata = {
            str(name).lower(): str(value).lower()
            for name, value in (response.get("Metadata") or {}).items()
        }
        return int(response["ContentLength"]), response.get("ETag"), metadata.get("sha256")

    def verify_manifest(
        self,
        *,
        model_prefix: str,
        registration_id: str,
        hotkey: str,
        expected_manifest_sha256: str,
    ) -> VerifiedManifest:
        if model_prefix != f"models/registrations/{registration_id}/":
            raise ArtifactIntegrityError("registration model prefix is not canonical")
        self._assert_no_multipart_uploads(model_prefix)
        objects = self._list_objects(self.private_model_bucket, model_prefix)
        total_uploaded_bytes = sum(int(item["Size"]) for item in objects.values())
        if total_uploaded_bytes > self.max_upload_bytes:
            raise UploadQuotaExceeded(
                f"private model upload is {total_uploaded_bytes} bytes; "
                f"limit is {self.max_upload_bytes} bytes"
            )
        manifest_key = f"{model_prefix}manifest.json"
        if manifest_key not in objects:
            raise ArtifactIntegrityError("private model prefix does not contain manifest.json")
        raw, manifest_size, manifest_digest, _, manifest_metadata_digest = self._read_and_hash(
            self.private_model_bucket, manifest_key, capture=True
        )
        if manifest_size == 0 or manifest_digest != expected_manifest_sha256:
            raise ArtifactIntegrityError(
                "manifest object does not match the finalized ready signal"
            )
        if manifest_metadata_digest != manifest_digest:
            raise ArtifactIntegrityError(
                "manifest object SHA-256 metadata is missing or incorrect"
            )
        try:
            manifest = Manifest.from_bytes(raw or b"")
        except ValueError as exc:
            raise ArtifactIntegrityError(str(exc)) from exc
        if manifest.registration_id != registration_id or manifest.hotkey != hotkey:
            raise ArtifactIntegrityError("manifest identity does not own the model prefix")
        try:
            verify_hotkey_signature(hotkey, manifest.signing_payload(), manifest.signature)
        except ValueError as exc:
            raise ArtifactIntegrityError(str(exc)) from exc
        self._validate_genesis_contract(manifest)

        expected_keys = {manifest_key} | {
            f"{model_prefix}{item.path}" for item in manifest.files
        }
        if set(objects) != expected_keys:
            missing = sorted(expected_keys - set(objects))
            undeclared = sorted(set(objects) - expected_keys)
            raise ArtifactIntegrityError(
                f"model inventory differs from manifest; missing={missing}, undeclared={undeclared}"
            )

        etags: dict[str, str | None] = {}
        for item in manifest.files:
            key = f"{model_prefix}{item.path}"
            if item.path in self.genesis_contract_files:
                _, size, digest, etag, metadata_digest = self._read_and_hash(
                    self.private_model_bucket, key
                )
                if size != item.size or digest != item.sha256:
                    raise GenesisContractMismatch(
                        f"submitted genesis contract bytes differ: {item.path}"
                    )
            else:
                # The miner can declare custom SHA-256 metadata, so this is an
                # inventory/immutability check only. The evaluator verifies all
                # model bytes against the signed manifest during its one required
                # download, before loading any model onto a GPU.
                size, etag, metadata_digest = self._head_object(
                    self.private_model_bucket, key
                )
            if metadata_digest != item.sha256:
                raise ArtifactIntegrityError(
                    f"object SHA-256 metadata is missing or incorrect: {item.path}"
                )
            listed_size = objects[key].get("Size")
            if listed_size is not None and int(listed_size) != size:
                raise ArtifactIntegrityError(
                    f"R2 listing size changed during verification: {item.path}"
                )
            listed_etag = objects[key].get("ETag")
            if etag is not None and listed_etag is not None and etag != listed_etag:
                raise ArtifactIntegrityError(
                    f"R2 object changed during verification: {item.path}"
                )
            etags[item.path] = etag or listed_etag
        return VerifiedManifest(
            manifest=manifest,
            manifest_sha256=manifest_digest,
            manifest_size=manifest_size,
            source_etags=etags,
        )

    def create_immutable_snapshot(
        self,
        *,
        model_prefix: str,
        verified: VerifiedManifest,
    ) -> ImmutableSnapshotResult:
        manifest = verified.manifest
        paths = ["manifest.json", *(item.path for item in manifest.files)]
        inventory_hash = hashlib.sha256(b"teutonic-immutable-snapshot-v1\0")
        for path in paths:
            if path == "manifest.json":
                size = verified.manifest_size
                digest = verified.manifest_sha256
            else:
                item = next(item for item in manifest.files if item.path == path)
                size = item.size
                digest = item.sha256
            inventory_hash.update(path.encode())
            inventory_hash.update(b"\0")
            inventory_hash.update(str(size).encode())
            inventory_hash.update(b"\0")
            inventory_hash.update(bytes.fromhex(digest))
        return ImmutableSnapshotResult(
            bucket=self.private_model_bucket,
            prefix=model_prefix,
            version=inventory_hash.hexdigest(),
            manifest_size=verified.manifest_size,
            etags=verified.source_etags,
        )

    def abort_multipart_uploads(self, model_prefix: str) -> int:
        aborted = 0
        response = self.s3.list_multipart_uploads(
            Bucket=self.private_model_bucket, Prefix=model_prefix
        )
        while True:
            for upload in response.get("Uploads", []):
                self.s3.abort_multipart_upload(
                    Bucket=self.private_model_bucket,
                    Key=upload["Key"],
                    UploadId=upload["UploadId"],
                )
                aborted += 1
            if not response.get("IsTruncated"):
                return aborted
            response = self.s3.list_multipart_uploads(
                Bucket=self.private_model_bucket,
                Prefix=model_prefix,
                KeyMarker=response["NextKeyMarker"],
                UploadIdMarker=response.get("NextUploadIdMarker", ""),
            )

    def cleanup_model_prefix(self, model_prefix: str) -> int:
        objects = self._list_objects(self.private_model_bucket, model_prefix)
        if not objects:
            return 0
        keys = sorted(objects)
        for offset in range(0, len(keys), 1000):
            self.s3.delete_objects(
                Bucket=self.private_model_bucket,
                Delete={
                    "Objects": [{"Key": key} for key in keys[offset : offset + 1000]],
                    "Quiet": True,
                },
            )
        return len(keys)
