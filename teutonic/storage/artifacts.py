from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from teutonic.evaluation.protocol_v2 import R2Artifact


class ArtifactIntegrityError(RuntimeError):
    pass


def model_digest_from_inventory(files: list[tuple[str, int, str]]) -> str:
    if not files:
        raise ArtifactIntegrityError("immutable model inventory contains no model files")
    digest = hashlib.sha256()
    seen: set[str] = set()
    for relative, size, file_digest in sorted(files):
        if relative in seen:
            raise ArtifactIntegrityError(f"duplicate model inventory path: {relative}")
        seen.add(relative)
        normalized = file_digest.lower().removeprefix("sha256:")
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ArtifactIntegrityError(f"invalid SHA-256 for model inventory path: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(normalized))
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_digest(snapshot: str | Path) -> str:
    """Hash a model tree deterministically, excluding its signed manifest."""
    root = Path(snapshot)
    if not root.is_dir():
        raise ArtifactIntegrityError("immutable model snapshot is not a directory")
    symlinks = [path for path in root.rglob("*") if path.is_symlink()]
    if symlinks:
        raise ArtifactIntegrityError("immutable model snapshots may not contain symlinks")
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() != "manifest.json"
    )
    if not files:
        raise ArtifactIntegrityError("immutable model snapshot contains no model files")
    inventory: list[tuple[str, int, str]] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        file_digest = sha256_file(path)
        inventory.append((relative, path.stat().st_size, file_digest))
    return model_digest_from_inventory(inventory)


def verify_snapshot(snapshot: str | Path, expected_digest: str) -> str:
    observed = snapshot_digest(snapshot)
    if observed != expected_digest:
        raise ArtifactIntegrityError(
            f"immutable artifact digest mismatch: expected {expected_digest}, observed {observed}"
        )
    return observed


class R2ArtifactResolver:
    """Materialize immutable R2 prefixes using evaluator-owned read-only credentials."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        s3_client: Any | None = None,
        allowed_bucket: str | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.allowed_bucket = allowed_bucket or os.environ.get(
            "TEUTONIC_EVALUATOR_R2_BUCKET", ""
        )
        self._s3_client = s3_client

    def _client(self):
        if self._s3_client is None:
            import boto3

            endpoint = os.environ.get("TEUTONIC_EVALUATOR_R2_ENDPOINT", "")
            access_key = os.environ.get("TEUTONIC_EVALUATOR_R2_ACCESS_KEY_ID", "")
            secret_key = os.environ.get("TEUTONIC_EVALUATOR_R2_SECRET_ACCESS_KEY", "")
            if not endpoint or not access_key or not secret_key:
                raise RuntimeError("evaluator private-model R2 credentials are not configured")
            self._s3_client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                aws_session_token=os.environ.get("TEUTONIC_EVALUATOR_R2_SESSION_TOKEN") or None,
                region_name=os.environ.get("TEUTONIC_EVALUATOR_R2_REGION", "auto"),
            )
        return self._s3_client

    @staticmethod
    def _relative_key(prefix: str, key: str) -> Path:
        if not key.startswith(prefix):
            raise ArtifactIntegrityError("R2 returned an object outside the requested prefix")
        relative = key[len(prefix) :]
        posix = PurePosixPath(relative)
        if not relative or posix.is_absolute() or ".." in posix.parts:
            raise ArtifactIntegrityError(f"unsafe immutable artifact object key: {key!r}")
        return Path(*posix.parts)

    def _list_objects(self, artifact: R2Artifact) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        continuation: str | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": artifact.bucket,
                "Prefix": artifact.prefix,
            }
            if continuation:
                request["ContinuationToken"] = continuation
            response = self._client().list_objects_v2(**request)
            objects.extend(
                item for item in response.get("Contents", []) if not item["Key"].endswith("/")
            )
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
            if not continuation:
                raise ArtifactIntegrityError("truncated R2 listing omitted continuation token")
        if not objects:
            raise ArtifactIntegrityError("immutable R2 artifact prefix is empty")
        return sorted(objects, key=lambda item: item["Key"])

    def resolve(self, artifact: R2Artifact) -> str:
        if self.allowed_bucket and artifact.bucket != self.allowed_bucket:
            raise ArtifactIntegrityError("artifact bucket is outside the evaluator allowlist")
        target = self.cache_dir / artifact.expected_digest
        if target.exists():
            verify_snapshot(target, artifact.expected_digest)
            return str(target)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="artifact-", dir=self.cache_dir))
        try:
            for item in self._list_objects(artifact):
                relative = self._relative_key(artifact.prefix, item["Key"])
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._client().download_file(artifact.bucket, item["Key"], str(destination))
                if destination.is_symlink():
                    raise ArtifactIntegrityError("immutable artifacts may not contain symlinks")
                expected_size = item.get("Size")
                if expected_size is not None and destination.stat().st_size != expected_size:
                    raise ArtifactIntegrityError(
                        f"downloaded size mismatch for {relative.as_posix()}"
                    )
            verify_snapshot(temporary, artifact.expected_digest)
            try:
                temporary.rename(target)
            except FileExistsError:
                verify_snapshot(target, artifact.expected_digest)
            return str(target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
