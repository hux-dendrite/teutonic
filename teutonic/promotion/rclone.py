from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any

from .contracts import ObservedObject


_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_SERVER_SIDE_COPY = re.compile(r"\bCopied \(server-side copy\)")
_STREAMED_COPY = re.compile(r"\bCopied \((?:new|replaced existing)\)")
_COPY_TRANSFERS = 100
_COPY_CHECKERS = 100
_COPY_RETRIES = 5
log = logging.getLogger("teutonic.promotion-worker.rclone")


class PromotionStorageError(RuntimeError):
    pass


class PromotionCollisionError(PromotionStorageError):
    pass


class RcloneExecutionError(PromotionStorageError):
    pass


class S3InventoryInspector:
    """Inspect object headers only; model bodies never enter the promotion worker."""

    def __init__(self, s3_client: Any) -> None:
        self.s3 = s3_client

    def inventory(self, bucket: str, prefix: str) -> dict[str, ObservedObject]:
        observed: dict[str, ObservedObject] = {}
        continuation: str | None = None
        while True:
            request: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation:
                request["ContinuationToken"] = continuation
            response = self.s3.list_objects_v2(**request)
            for row in response.get("Contents", []):
                key = str(row["Key"])
                if key.endswith("/"):
                    continue
                if not key.startswith(prefix):
                    raise PromotionStorageError("object listing escaped its requested prefix")
                path = key[len(prefix) :]
                if not path or path in observed:
                    raise PromotionStorageError("object listing is ambiguous")
                head = self.s3.head_object(Bucket=bucket, Key=key)
                metadata = {str(k).lower(): str(v) for k, v in head.get("Metadata", {}).items()}
                observed[path] = ObservedObject(
                    path=path,
                    size=int(head.get("ContentLength", row.get("Size", -1))),
                    sha256=metadata.get("sha256"),
                    etag=head.get("ETag") or row.get("ETag"),
                )
            if not response.get("IsTruncated"):
                return observed
            continuation = response.get("NextContinuationToken")
            if not continuation:
                raise PromotionStorageError("truncated listing omitted a continuation token")


class RclonePromotionExecutor:
    """Invoke rclone with one remote so Cloudflare R2 uses server-side CopyObject."""

    def __init__(
        self,
        remote: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        heartbeat_interval: timedelta = timedelta(seconds=20),
    ) -> None:
        if not _REMOTE_RE.fullmatch(remote):
            raise ValueError("rclone remote must be one configured remote name")
        if heartbeat_interval <= timedelta(0):
            raise ValueError("rclone heartbeat interval must be positive")
        self.remote = remote
        self.runner = runner
        self.heartbeat_interval = heartbeat_interval

    def _path(self, bucket: str, prefix: str) -> str:
        if not _BUCKET_RE.fullmatch(bucket):
            raise ValueError("invalid R2 bucket name")
        normalized = prefix.strip("/")
        if not normalized or ".." in normalized.split("/"):
            raise ValueError("invalid R2 promotion prefix")
        return f"{self.remote}:{bucket}/{normalized}/"

    def _run(self, command: list[str], *, heartbeat: Callable[[], None] | None) -> str:
        environment = os.environ.copy()
        # boto3 needs the validator host's private CA bundle, but rclone's S3
        # backend cannot combine AWS_CA_BUNDLE with its wrapped HTTP transport.
        # Keep the override scoped to rclone instead of changing the worker.
        environment.pop("AWS_CA_BUNDLE", None)
        if self.runner is not None:
            result = self.runner(
                command,
                check=False,
                text=True,
                capture_output=True,
                env=environment,
            )
            if heartbeat is not None:
                heartbeat()
            return_code = result.returncode
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        else:
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as output_file:
                process = subprocess.Popen(
                    command,
                    stdout=output_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=environment,
                )
                while True:
                    try:
                        return_code = process.wait(
                            timeout=self.heartbeat_interval.total_seconds()
                        )
                        break
                    except subprocess.TimeoutExpired:
                        if heartbeat is not None:
                            try:
                                heartbeat()
                            except BaseException:
                                process.terminate()
                                try:
                                    process.wait(timeout=10)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=10)
                                raise
                output_file.seek(0)
                output = output_file.read()
        if return_code:
            tail = " | ".join(line.strip() for line in output.splitlines()[-8:] if line.strip())
            detail = f": {tail}" if tail else ""
            raise RcloneExecutionError(f"rclone exited with status {return_code}{detail}")
        return output

    def _object_path(self, bucket: str, prefix: str, path: str) -> str:
        base = self._path(bucket, prefix).rstrip("/")
        parsed = PurePosixPath(path)
        if not path or parsed.is_absolute() or ".." in parsed.parts or str(parsed) != path:
            raise ValueError("invalid R2 promotion object path")
        return f"{base}/{path}"

    @staticmethod
    def _copy_modes(output: str) -> tuple[int, int]:
        return len(_SERVER_SIDE_COPY.findall(output)), len(_STREAMED_COPY.findall(output))

    @staticmethod
    def _copy_flags(*, bulk: bool) -> list[str]:
        flags = [
            "--checksum",
            "--immutable",
            "--metadata",
            "--retries",
            str(_COPY_RETRIES),
            "--log-level",
            "INFO",
            "--stats",
            "0",
        ]
        if bulk:
            flags.extend(
                [
                    "--fast-list",
                    "--check-first",
                    "--transfers",
                    str(_COPY_TRANSFERS),
                    "--checkers",
                    str(_COPY_CHECKERS),
                ]
            )
        return flags

    def _require_server_side(self, output: str, *, expected: int, stage: str) -> None:
        server_side, streamed = self._copy_modes(output)
        if streamed:
            raise RcloneExecutionError(
                f"rclone {stage} attempted {streamed} host-streamed object copy/copies"
            )
        if server_side != expected:
            raise RcloneExecutionError(
                f"rclone {stage} confirmed {server_side} server-side copies; expected {expected}"
            )

    def copy(
        self,
        *,
        source_bucket: str,
        source_prefix: str,
        destination_bucket: str,
        destination_prefix: str,
        probe_path: str,
        expected_object_count: int,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        source = self._path(source_bucket, source_prefix)
        destination = self._path(destination_bucket, destination_prefix)
        if source_bucket == destination_bucket:
            raise ValueError("private and public promotion buckets must differ")
        if expected_object_count < 1:
            raise ValueError("promotion copy must expect at least one missing object")

        probe_output = self._run(
            [
                "rclone",
                "copyto",
                self._object_path(source_bucket, source_prefix, probe_path),
                self._object_path(destination_bucket, destination_prefix, probe_path),
                *self._copy_flags(bulk=False),
            ],
            heartbeat=heartbeat,
        )
        self._require_server_side(probe_output, expected=1, stage="probe")
        log.info("rclone server-side copy confirmed probe=%s", probe_path)

        remaining = expected_object_count - 1
        if remaining == 0:
            return
        bulk_output = self._run(
            ["rclone", "copy", source, destination, *self._copy_flags(bulk=True)],
            heartbeat=heartbeat,
        )
        self._require_server_side(bulk_output, expected=remaining, stage="bulk copy")
        log.info(
            "rclone server-side bulk copy confirmed objects=%d transfers=%d checkers=%d",
            remaining,
            _COPY_TRANSFERS,
            _COPY_CHECKERS,
        )

    def delete_source(
        self, *, bucket: str, prefix: str, heartbeat: Callable[[], None] | None = None
    ) -> None:
        self._run(
            [
                "rclone",
                "delete",
                self._path(bucket, prefix),
                "--rmdirs",
                "--fast-list",
                "--checkers",
                str(_COPY_CHECKERS),
                "--retries",
                str(_COPY_RETRIES),
            ],
            heartbeat=heartbeat,
        )


def verify_inventory(
    expected: Mapping[str, Any], observed: Mapping[str, ObservedObject], *, complete: bool
) -> None:
    unexpected = sorted(set(observed) - set(expected))
    if unexpected:
        raise PromotionCollisionError(f"promotion prefix contains unexpected paths: {unexpected}")
    for path, item in observed.items():
        wanted = expected[path]
        if item.size != wanted.size:
            raise PromotionCollisionError(f"promotion object size differs: {path}")
        if item.sha256 != wanted.sha256:
            raise PromotionCollisionError(f"promotion object SHA-256 differs: {path}")
    if complete and set(observed) != set(expected):
        missing = sorted(set(expected) - set(observed))
        raise PromotionStorageError(f"promotion inventory is incomplete: {missing}")
