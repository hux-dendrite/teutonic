from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from teutonic.storage.artifacts import ArtifactIntegrityError

from .contracts import Manifest
from .crypto import verify_hotkey_signature


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
        chunk_size: int = 1024 * 1024,
    ) -> None:
        if not private_model_bucket:
            raise ValueError("private model bucket is required")
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self.s3 = s3_client
        self.private_model_bucket = private_model_bucket
        self.chunk_size = chunk_size

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
    ) -> tuple[bytes | None, int, str, str | None]:
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
        return captured, size, digest.hexdigest(), response.get("ETag")

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
        manifest_key = f"{model_prefix}manifest.json"
        if manifest_key not in objects:
            raise ArtifactIntegrityError("private model prefix does not contain manifest.json")
        raw, manifest_size, manifest_digest, _ = self._read_and_hash(
            self.private_model_bucket, manifest_key, capture=True
        )
        if manifest_size == 0 or manifest_digest != expected_manifest_sha256:
            raise ArtifactIntegrityError("manifest object does not match the finalized ready signal")
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
            _, size, digest, etag = self._read_and_hash(self.private_model_bucket, key)
            if size != item.size or digest != item.sha256:
                raise ArtifactIntegrityError(f"object bytes do not match manifest: {item.path}")
            listed_size = objects[key].get("Size")
            if listed_size is not None and int(listed_size) != size:
                raise ArtifactIntegrityError(f"R2 listing size changed during verification: {item.path}")
            etags[item.path] = etag or objects[key].get("ETag")
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
        # Upload authority has already been revoked. Reverify the exact source
        # tree before admitting this registration prefix to evaluation.
        source = self.verify_manifest(
            model_prefix=model_prefix,
            registration_id=manifest.registration_id,
            hotkey=manifest.hotkey,
            expected_manifest_sha256=verified.manifest_sha256,
        )
        if source.manifest != manifest or source.manifest_size != verified.manifest_size:
            raise ArtifactIntegrityError("manifest changed after initial verification")

        paths = ["manifest.json", *(item.path for item in manifest.files)]
        inventory_hash = hashlib.sha256(b"teutonic-immutable-snapshot-v1\0")
        for path in paths:
            if path == "manifest.json":
                size = source.manifest_size
                digest = source.manifest_sha256
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
            manifest_size=source.manifest_size,
            etags=source.source_etags,
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
