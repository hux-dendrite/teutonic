from __future__ import annotations

import hashlib
import math
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
    """Verify miner uploads and create content-addressed private R2 snapshots."""

    def __init__(
        self,
        s3_client: Any,
        *,
        ingest_bucket: str,
        private_bucket: str,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        if not ingest_bucket or not private_bucket or ingest_bucket == private_bucket:
            raise ValueError("ingest and private buckets must be distinct and non-empty")
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self.s3 = s3_client
        self.ingest_bucket = ingest_bucket
        self.private_bucket = private_bucket
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
                "Bucket": self.ingest_bucket,
                "Prefix": prefix,
            }
            if key_marker:
                request["KeyMarker"] = key_marker
            if upload_marker:
                request["UploadIdMarker"] = upload_marker
            response = self.s3.list_multipart_uploads(**request)
            if response.get("Uploads"):
                raise ArtifactIntegrityError(
                    "ingest prefix contains unfinished multipart uploads"
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
        ingest_prefix: str,
        registration_id: str,
        hotkey: str,
        expected_manifest_sha256: str,
    ) -> VerifiedManifest:
        if ingest_prefix != f"ingest/{registration_id}/":
            raise ArtifactIntegrityError("registration ingest prefix is not canonical")
        self._assert_no_multipart_uploads(ingest_prefix)
        objects = self._list_objects(self.ingest_bucket, ingest_prefix)
        manifest_key = f"{ingest_prefix}manifest.json"
        if manifest_key not in objects:
            raise ArtifactIntegrityError("ingest prefix does not contain manifest.json")
        raw, manifest_size, manifest_digest, _ = self._read_and_hash(
            self.ingest_bucket, manifest_key, capture=True
        )
        if manifest_size == 0 or manifest_digest != expected_manifest_sha256:
            raise ArtifactIntegrityError("manifest object does not match the finalized ready signal")
        try:
            manifest = Manifest.from_bytes(raw or b"")
        except ValueError as exc:
            raise ArtifactIntegrityError(str(exc)) from exc
        if manifest.registration_id != registration_id or manifest.hotkey != hotkey:
            raise ArtifactIntegrityError("manifest identity does not own the ingest prefix")
        try:
            verify_hotkey_signature(hotkey, manifest.signing_payload(), manifest.signature)
        except ValueError as exc:
            raise ArtifactIntegrityError(str(exc)) from exc

        expected_keys = {manifest_key} | {
            f"{ingest_prefix}{item.path}" for item in manifest.files
        }
        if set(objects) != expected_keys:
            missing = sorted(expected_keys - set(objects))
            undeclared = sorted(set(objects) - expected_keys)
            raise ArtifactIntegrityError(
                f"ingest inventory differs from manifest; missing={missing}, undeclared={undeclared}"
            )

        etags: dict[str, str | None] = {}
        for item in manifest.files:
            key = f"{ingest_prefix}{item.path}"
            _, size, digest, etag = self._read_and_hash(self.ingest_bucket, key)
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
        ingest_prefix: str,
        verified: VerifiedManifest,
    ) -> ImmutableSnapshotResult:
        manifest = verified.manifest
        # Reverify all mutable source bytes immediately before copying.
        source = self.verify_manifest(
            ingest_prefix=ingest_prefix,
            registration_id=manifest.registration_id,
            hotkey=manifest.hotkey,
            expected_manifest_sha256=verified.manifest_sha256,
        )
        if source.manifest != manifest or source.manifest_size != verified.manifest_size:
            raise ArtifactIntegrityError("manifest changed after initial verification")

        prefix = f"models/sha256/{manifest.model_digest}/"
        paths = ["manifest.json", *(item.path for item in manifest.files)]
        sizes = {"manifest.json": verified.manifest_size} | {
            item.path: item.size for item in manifest.files
        }
        for path in paths:
            expected_digest = (
                verified.manifest_sha256
                if path == "manifest.json"
                else next(item.sha256 for item in manifest.files if item.path == path)
            )
            self._copy_object(
                source_key=f"{ingest_prefix}{path}",
                destination_key=f"{prefix}{path}",
                size=sizes[path],
                sha256=expected_digest,
            )

        immutable_objects = self._list_objects(self.private_bucket, prefix)
        expected_keys = {f"{prefix}{path}" for path in paths}
        if set(immutable_objects) != expected_keys:
            raise ArtifactIntegrityError("immutable destination contains an unexpected inventory")

        etags: dict[str, str | None] = {}
        inventory_hash = hashlib.sha256(b"teutonic-immutable-snapshot-v1\0")
        for path in paths:
            _, size, digest, etag = self._read_and_hash(
                self.private_bucket, f"{prefix}{path}"
            )
            if path == "manifest.json":
                expected_size = verified.manifest_size
                expected_digest = verified.manifest_sha256
            else:
                item = next(item for item in manifest.files if item.path == path)
                expected_size = item.size
                expected_digest = item.sha256
                etags[path] = etag or immutable_objects[f"{prefix}{path}"].get("ETag")
            if size != expected_size or digest != expected_digest:
                raise ArtifactIntegrityError(f"immutable object verification failed: {path}")
            inventory_hash.update(path.encode())
            inventory_hash.update(b"\0")
            inventory_hash.update(str(size).encode())
            inventory_hash.update(b"\0")
            inventory_hash.update(bytes.fromhex(digest))
        return ImmutableSnapshotResult(
            bucket=self.private_bucket,
            prefix=prefix,
            version=inventory_hash.hexdigest(),
            manifest_size=verified.manifest_size,
            etags=etags,
        )

    def _copy_object(
        self, *, source_key: str, destination_key: str, size: int, sha256: str
    ) -> None:
        copy_source = {"Bucket": self.ingest_bucket, "Key": source_key}
        if size <= 5 * 1024**3:
            self.s3.copy_object(
                Bucket=self.private_bucket,
                Key=destination_key,
                CopySource=copy_source,
                MetadataDirective="REPLACE",
                Metadata={"sha256": sha256},
            )
            return

        # S3 CopyObject is capped at 5 GiB. Choose a part size that is both at
        # least 128 MiB and keeps even unusually large objects within 10k parts.
        part_size = max(128 * 1024**2, math.ceil(size / 10_000))
        multipart = self.s3.create_multipart_upload(
            Bucket=self.private_bucket,
            Key=destination_key,
            Metadata={"sha256": sha256},
        )
        upload_id = multipart["UploadId"]
        parts: list[dict[str, Any]] = []
        try:
            for part_number, start in enumerate(range(0, size, part_size), start=1):
                end = min(size, start + part_size) - 1
                response = self.s3.upload_part_copy(
                    Bucket=self.private_bucket,
                    Key=destination_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    CopySource=copy_source,
                    CopySourceRange=f"bytes={start}-{end}",
                )
                parts.append(
                    {
                        "PartNumber": part_number,
                        "ETag": response["CopyPartResult"]["ETag"],
                    }
                )
            self.s3.complete_multipart_upload(
                Bucket=self.private_bucket,
                Key=destination_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            self.s3.abort_multipart_upload(
                Bucket=self.private_bucket, Key=destination_key, UploadId=upload_id
            )
            raise

    def abort_multipart_uploads(self, ingest_prefix: str) -> int:
        aborted = 0
        response = self.s3.list_multipart_uploads(
            Bucket=self.ingest_bucket, Prefix=ingest_prefix
        )
        while True:
            for upload in response.get("Uploads", []):
                self.s3.abort_multipart_upload(
                    Bucket=self.ingest_bucket,
                    Key=upload["Key"],
                    UploadId=upload["UploadId"],
                )
                aborted += 1
            if not response.get("IsTruncated"):
                return aborted
            response = self.s3.list_multipart_uploads(
                Bucket=self.ingest_bucket,
                Prefix=ingest_prefix,
                KeyMarker=response["NextKeyMarker"],
                UploadIdMarker=response.get("NextUploadIdMarker", ""),
            )

    def cleanup_ingest_prefix(self, ingest_prefix: str) -> int:
        objects = self._list_objects(self.ingest_bucket, ingest_prefix)
        if not objects:
            return 0
        keys = sorted(objects)
        for offset in range(0, len(keys), 1000):
            self.s3.delete_objects(
                Bucket=self.ingest_bucket,
                Delete={
                    "Objects": [{"Key": key} for key in keys[offset : offset + 1000]],
                    "Quiet": True,
                },
            )
        return len(keys)
