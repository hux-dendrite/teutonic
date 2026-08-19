from __future__ import annotations

import concurrent.futures
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from teutonic.evaluation.protocol_v2 import R2Artifact


class ArtifactIntegrityError(RuntimeError):
    pass


EVALUATOR_MODEL_BUCKETS = frozenset(
    {
        "teutonic-models",
        "teutonic-private-models",
    }
)


@dataclass(frozen=True)
class ArtifactDownloadProfile:
    backend: str = "rclone"
    file_concurrency: int = 5
    streams_per_file: int = 16
    cutoff_mib: int = 32
    chunk_mib: int = 64
    rclone_binary: str = "rclone"


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
        allowed_buckets: Collection[str] | None = None,
        download_profile: ArtifactDownloadProfile | None = None,
        command_runner: Callable[[list[str], Mapping[str, str]], None] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.allowed_buckets = frozenset(
            bucket.strip()
            for bucket in (
                EVALUATOR_MODEL_BUCKETS if allowed_buckets is None else allowed_buckets
            )
            if bucket.strip()
        )
        if not self.allowed_buckets:
            raise ValueError("evaluator model bucket allowlist cannot be empty")
        self._s3_client = s3_client
        self.download_profile = download_profile or ArtifactDownloadProfile()
        self._command_runner = command_runner or self._run_command

    def _client(self):
        if self._s3_client is None:
            import boto3

            endpoint = os.environ.get("TEUTONIC_EVALUATOR_R2_ENDPOINT", "")
            access_key = os.environ.get("TEUTONIC_EVALUATOR_R2_ACCESS_KEY_ID", "")
            secret_key = os.environ.get("TEUTONIC_EVALUATOR_R2_SECRET_ACCESS_KEY", "")
            if not endpoint or not access_key or not secret_key:
                raise RuntimeError("evaluator model R2 credentials are not configured")
            from botocore.config import Config

            self._s3_client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                aws_session_token=os.environ.get("TEUTONIC_EVALUATOR_R2_SESSION_TOKEN") or None,
                region_name=os.environ.get("TEUTONIC_EVALUATOR_R2_REGION", "auto"),
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 5, "mode": "standard"},
                    max_pool_connections=(
                        self.download_profile.file_concurrency
                        * self.download_profile.streams_per_file
                    ),
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        return self._s3_client

    @staticmethod
    def _run_command(command: list[str], environ: Mapping[str, str]) -> None:
        subprocess.run(command, env=dict(environ), check=True)

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

    def _verify_downloaded_inventory(
        self,
        root: Path,
        artifact: R2Artifact,
        objects: list[dict[str, Any]],
    ) -> None:
        expected = {
            self._relative_key(artifact.prefix, item["Key"]): int(item["Size"])
            for item in objects
        }
        downloaded = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
        if downloaded != set(expected):
            missing = sorted(path.as_posix() for path in set(expected) - downloaded)
            extra = sorted(path.as_posix() for path in downloaded - set(expected))
            raise ArtifactIntegrityError(
                f"downloaded inventory mismatch; missing={missing[:5]} extra={extra[:5]}"
            )
        for relative, expected_size in expected.items():
            destination = root / relative
            if destination.is_symlink():
                raise ArtifactIntegrityError("immutable artifacts may not contain symlinks")
            if destination.stat().st_size != expected_size:
                raise ArtifactIntegrityError(
                    f"downloaded size mismatch for {relative.as_posix()}"
                )

    def _download_boto3(
        self,
        artifact: R2Artifact,
        objects: list[dict[str, Any]],
        root: Path,
    ) -> None:
        from boto3.s3.transfer import TransferConfig

        profile = self.download_profile
        transfer_config = TransferConfig(
            multipart_threshold=profile.cutoff_mib * 1024 * 1024,
            multipart_chunksize=profile.chunk_mib * 1024 * 1024,
            max_concurrency=profile.streams_per_file,
            num_download_attempts=10,
            use_threads=True,
        )

        def download(item: dict[str, Any]) -> None:
            relative = self._relative_key(artifact.prefix, item["Key"])
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._client().download_file(
                artifact.bucket,
                item["Key"],
                str(destination),
                Config=transfer_config,
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=profile.file_concurrency
        ) as executor:
            list(executor.map(download, objects))

    def _rclone_environment(self) -> dict[str, str]:
        endpoint = os.environ.get("TEUTONIC_EVALUATOR_R2_ENDPOINT", "").strip()
        access_key = os.environ.get("TEUTONIC_EVALUATOR_R2_ACCESS_KEY_ID", "").strip()
        secret_key = os.environ.get("TEUTONIC_EVALUATOR_R2_SECRET_ACCESS_KEY", "").strip()
        if not endpoint or not access_key or not secret_key:
            raise RuntimeError("evaluator model R2 credentials are not configured")
        environ = os.environ.copy()
        environ.update(
            {
                "RCLONE_CONFIG_TEUTONICMODEL_TYPE": "s3",
                "RCLONE_CONFIG_TEUTONICMODEL_PROVIDER": "Cloudflare",
                "RCLONE_CONFIG_TEUTONICMODEL_ACCESS_KEY_ID": access_key,
                "RCLONE_CONFIG_TEUTONICMODEL_SECRET_ACCESS_KEY": secret_key,
                "RCLONE_CONFIG_TEUTONICMODEL_ENDPOINT": endpoint,
                "RCLONE_CONFIG_TEUTONICMODEL_REGION": os.environ.get(
                    "TEUTONIC_EVALUATOR_R2_REGION", "auto"
                ),
                "RCLONE_CONFIG_TEUTONICMODEL_NO_CHECK_BUCKET": "true",
            }
        )
        session_token = os.environ.get("TEUTONIC_EVALUATOR_R2_SESSION_TOKEN", "").strip()
        if session_token:
            environ["RCLONE_CONFIG_TEUTONICMODEL_SESSION_TOKEN"] = session_token
        return environ

    def _download_rclone(self, artifact: R2Artifact, root: Path) -> None:
        profile = self.download_profile
        command = [
            profile.rclone_binary,
            "copy",
            f"teutonicmodel:{artifact.bucket}/{artifact.prefix}",
            str(root),
            "--transfers",
            str(profile.file_concurrency),
            "--multi-thread-streams",
            str(profile.streams_per_file),
            "--multi-thread-cutoff",
            f"{profile.cutoff_mib}M",
            "--s3-chunk-size",
            f"{profile.chunk_mib}M",
            "--stats",
            "10s",
            "--stats-one-line",
        ]
        self._command_runner(command, self._rclone_environment())

    def resolve(self, artifact: R2Artifact) -> str:
        if artifact.bucket not in self.allowed_buckets:
            raise ArtifactIntegrityError("artifact bucket is outside the evaluator allowlist")
        target = self.cache_dir / artifact.expected_digest
        if target.exists():
            verify_snapshot(target, artifact.expected_digest)
            return str(target)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="artifact-", dir=self.cache_dir))
        try:
            objects = self._list_objects(artifact)
            if self.download_profile.backend == "rclone":
                self._download_rclone(artifact, temporary)
            else:
                self._download_boto3(artifact, objects, temporary)
            self._verify_downloaded_inventory(temporary, artifact, objects)
            verify_snapshot(temporary, artifact.expected_digest)
            try:
                temporary.rename(target)
            except FileExistsError:
                verify_snapshot(target, artifact.expected_digest)
            return str(target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
