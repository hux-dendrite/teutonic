from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from teutonic.storage.artifacts import model_digest_from_inventory, sha256_file


_HF_DIGEST = re.compile(r"^hf:([0-9a-f]{40})$")


class SeedBootstrapError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SeedFile:
    path: str
    local_path: Path
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SeedArtifact:
    repo_id: str
    revision: str
    model_digest: str
    files: tuple[SeedFile, ...]
    manifest: bytes

    @property
    def prefix(self) -> str:
        return f"models/sha256/{self.model_digest}/"

    @property
    def manifest_sha256(self) -> str:
        import hashlib

        return hashlib.sha256(self.manifest).hexdigest()


@dataclass(frozen=True, slots=True)
class GenesisIdentity:
    hotkey: str
    uid: int
    finalized_block: int
    operator: str

    def __post_init__(self) -> None:
        if not self.hotkey or not self.operator:
            raise ValueError("genesis hotkey and operator are required")
        if self.uid < 0 or self.finalized_block < 0:
            raise ValueError("genesis UID and finalized block must be non-negative")


@dataclass(frozen=True, slots=True)
class GenesisRecord:
    competition_id: str
    reign_id: str
    created: bool


def _revision(seed_digest: str) -> str:
    match = _HF_DIGEST.fullmatch(seed_digest.strip().lower())
    if match is None:
        raise SeedBootstrapError("seed digest must be an immutable hf:<40-hex-commit> value")
    return match.group(1)


def _safe_file_inventory(snapshot: Path) -> tuple[SeedFile, ...]:
    if not snapshot.is_dir():
        raise SeedBootstrapError("Hugging Face seed snapshot is not a directory")
    files: list[SeedFile] = []
    for path in sorted(snapshot.rglob("*")):
        relative = path.relative_to(snapshot)
        if relative.parts and relative.parts[0] == ".cache":
            continue
        if path.is_symlink():
            raise SeedBootstrapError(f"seed snapshot contains a symlink: {relative.as_posix()}")
        if not path.is_file():
            continue
        name = relative.as_posix()
        parsed = PurePosixPath(name)
        if parsed.is_absolute() or ".." in parsed.parts or str(parsed) != name:
            raise SeedBootstrapError(f"seed snapshot contains an unsafe path: {name!r}")
        if name == "manifest.json":
            raise SeedBootstrapError("Hugging Face seed reserves manifest.json")
        files.append(
            SeedFile(
                path=name,
                local_path=path,
                size=path.stat().st_size,
                sha256=sha256_file(path),
            )
        )
    if not files:
        raise SeedBootstrapError("Hugging Face seed snapshot contains no model files")
    return tuple(files)


class HuggingFaceSeed:
    """Resolve one explicitly pinned Hugging Face commit into a local inventory."""

    def __init__(
        self,
        *,
        api: Any | None = None,
        downloader: Callable[..., str] | None = None,
        token: str | None = None,
    ) -> None:
        self._api = api
        self._downloader = downloader
        self.token = token or None

    def _clients(self) -> tuple[Any, Callable[..., str]]:
        if self._api is None or self._downloader is None:
            from huggingface_hub import HfApi, snapshot_download

            self._api = self._api or HfApi(token=self.token)
            self._downloader = self._downloader or snapshot_download
        return self._api, self._downloader

    def materialize(self, *, repo_id: str, seed_digest: str, local_dir: Path) -> SeedArtifact:
        revision = _revision(seed_digest)
        if not repo_id or repo_id.count("/") != 1:
            raise SeedBootstrapError("seed repository must be a Hugging Face namespace/name")
        api, downloader = self._clients()
        info = api.model_info(repo_id=repo_id, revision=revision)
        resolved = str(getattr(info, "sha", "")).lower()
        if resolved != revision:
            raise SeedBootstrapError(
                f"Hugging Face resolved {repo_id}@{revision} to unexpected commit {resolved!r}"
            )
        snapshot_dir = local_dir / repo_id.replace("/", "--") / revision
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        downloaded = Path(
            downloader(
                repo_id=repo_id,
                revision=revision,
                local_dir=str(snapshot_dir),
                token=self.token,
            )
        )
        files = _safe_file_inventory(downloaded)
        model_digest = model_digest_from_inventory(
            [(item.path, item.size, item.sha256) for item in files]
        )
        manifest_value = {
            "files": [
                {"path": item.path, "sha256": item.sha256, "size": item.size}
                for item in files
            ],
            "model_digest": model_digest,
            "protocol_version": 1,
            "source": {"backend": "hf", "repo_id": repo_id, "revision": revision},
            "type": "teutonic-genesis",
        }
        manifest = json.dumps(
            manifest_value, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return SeedArtifact(repo_id, revision, model_digest, files, manifest)


class PublicSeedStore:
    """Publish a genesis snapshot directly to its immutable public R2 prefix."""

    def __init__(self, s3_client: Any, *, bucket: str) -> None:
        if not bucket:
            raise ValueError("public seed bucket is required")
        self.s3 = s3_client
        self.bucket = bucket

    def _list(self, prefix: str) -> dict[str, Mapping[str, Any]]:
        found: dict[str, Mapping[str, Any]] = {}
        continuation: str | None = None
        while True:
            request: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation:
                request["ContinuationToken"] = continuation
            response = self.s3.list_objects_v2(**request)
            for item in response.get("Contents", []):
                key = str(item["Key"])
                if not key.endswith("/"):
                    if key in found:
                        raise SeedBootstrapError("public R2 listing repeated an object")
                    found[key] = item
            if not response.get("IsTruncated"):
                return found
            continuation = response.get("NextContinuationToken")
            if not continuation:
                raise SeedBootstrapError("truncated public R2 listing omitted its token")

    def _verify_object(self, *, key: str, size: int, sha256: str) -> None:
        head = self.s3.head_object(Bucket=self.bucket, Key=key)
        metadata = {str(k).lower(): str(v).lower() for k, v in head.get("Metadata", {}).items()}
        observed_size = int(head.get("ContentLength", -1))
        if observed_size != size or metadata.get("sha256") != sha256:
            raise SeedBootstrapError(f"public genesis object differs at {key}")

    def publish(self, artifact: SeedArtifact) -> None:
        expected: dict[str, tuple[int, str, Path | None]] = {
            f"{artifact.prefix}{item.path}": (item.size, item.sha256, item.local_path)
            for item in artifact.files
        }
        expected[f"{artifact.prefix}manifest.json"] = (
            len(artifact.manifest),
            artifact.manifest_sha256,
            None,
        )
        existing = self._list(artifact.prefix)
        unexpected = sorted(set(existing) - set(expected))
        if unexpected:
            raise SeedBootstrapError(
                f"public genesis prefix contains unexpected objects: {unexpected[:8]}"
            )
        for key in sorted(existing):
            size, sha256, _path = expected[key]
            self._verify_object(key=key, size=size, sha256=sha256)

        for key, (size, sha256, local_path) in expected.items():
            if key in existing:
                continue
            if local_path is None:
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=artifact.manifest,
                    ContentType="application/json",
                    Metadata={"sha256": sha256},
                )
            else:
                self.s3.upload_file(
                    str(local_path),
                    self.bucket,
                    key,
                    ExtraArgs={"Metadata": {"sha256": sha256}},
                )

        observed = self._list(artifact.prefix)
        if set(observed) != set(expected):
            raise SeedBootstrapError("public genesis prefix is incomplete after upload")
        for key, (size, sha256, _path) in expected.items():
            self._verify_object(key=key, size=size, sha256=sha256)


def _provenance(artifact: SeedArtifact, identity: GenesisIdentity) -> str:
    return json.dumps(
        {
            "model_digest": artifact.model_digest,
            "operator": identity.operator,
            "source": {
                "backend": "hf",
                "repo_id": artifact.repo_id,
                "revision": artifact.revision,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def bootstrap_genesis(
    connection: Any,
    *,
    netuid: int,
    chain_generation: str,
    competition: str,
    public_bucket: str,
    artifact: SeedArtifact,
    identity: GenesisIdentity,
) -> GenesisRecord:
    if netuid < 0 or not chain_generation or not competition or not public_bucket:
        raise ValueError("genesis competition identity is invalid")
    provenance = _provenance(artifact, identity)
    with connection.transaction():
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"teutonic-genesis:{netuid}:{chain_generation}:{competition}",),
        )
        row = connection.execute(
            """
            SELECT c.competition_id, c.current_reign_id, c.next_reign_number,
                   genesis.reign_id, genesis.reign_number, genesis.model_digest,
                   genesis.public_bucket, genesis.public_prefix, genesis.hotkey,
                   genesis.uid, genesis.crowned_finalized_block,
                   genesis.operator_provenance
              FROM control_plane.competitions c
              LEFT JOIN control_plane.king_reigns genesis
                ON genesis.competition_id = c.competition_id AND genesis.reign_number = 0
             WHERE c.netuid = %s AND c.chain_generation = %s AND c.name = %s
             FOR UPDATE OF c
            """,
            (netuid, chain_generation, competition),
        ).fetchone()
        expected_prefix = artifact.prefix
        if row is not None and row["reign_id"] is not None:
            expected = (
                0,
                artifact.model_digest,
                public_bucket,
                expected_prefix,
                identity.hotkey,
                provenance,
            )
            observed = (
                int(row["reign_number"]),
                str(row["model_digest"]),
                str(row["public_bucket"]),
                str(row["public_prefix"]),
                str(row["hotkey"]),
                str(row["operator_provenance"]),
            )
            if observed != expected:
                raise SeedBootstrapError("competition already has a different genesis king")
            if row["current_reign_id"] is None:
                raise SeedBootstrapError("competition has genesis but no current king")
            return GenesisRecord(str(row["competition_id"]), str(row["reign_id"]), False)
        if row is None:
            competition_id = connection.execute(
                """
                INSERT INTO control_plane.competitions (netuid, chain_generation, name)
                VALUES (%s, %s, %s) RETURNING competition_id
                """,
                (netuid, chain_generation, competition),
            ).fetchone()["competition_id"]
        else:
            if int(row["next_reign_number"]) != 1:
                raise SeedBootstrapError("empty competition has an invalid next reign number")
            competition_id = row["competition_id"]
        reign_id = connection.execute(
            """
            INSERT INTO control_plane.king_reigns (
                competition_id, reign_number, model_digest, public_bucket, public_prefix,
                hotkey, uid, crowned_at, crowned_finalized_block, operator_provenance
            ) VALUES (%s, 0, %s, %s, %s, %s, %s, clock_timestamp(), %s, %s)
            RETURNING reign_id
            """,
            (
                competition_id,
                artifact.model_digest,
                public_bucket,
                expected_prefix,
                identity.hotkey,
                identity.uid,
                identity.finalized_block,
                provenance,
            ),
        ).fetchone()["reign_id"]
        connection.execute(
            """
            UPDATE control_plane.competitions
               SET current_reign_id = %s, updated_at = clock_timestamp()
             WHERE competition_id = %s
            """,
            (reign_id, competition_id),
        )
        return GenesisRecord(str(competition_id), str(reign_id), True)
