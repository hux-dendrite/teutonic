from __future__ import annotations

import hashlib
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np

from teutonic.evaluator import engine as base


log = logging.getLogger("teutonic.evaluator.sources")
URL_CACHE_DIR = Path(
    os.environ.get(
        "TEUTONIC_PRETOKENIZED_CACHE_DIR",
        str(base.SHARD_CACHE_DIR / "pretokenized"),
    )
)


@dataclass(frozen=True, slots=True)
class ShardRef:
    source: str
    url: str
    sha256: str
    size_bytes: int
    n_tokens: int


def _cache_path(shard: ShardRef) -> Path:
    filename = Path(shard.url.split("?", 1)[0]).name or "shard.npy"
    return URL_CACHE_DIR / f"{shard.sha256[:24]}-{filename}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verified(path: Path, shard: ShardRef) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == shard.size_bytes
        and _sha256_file(path) == shard.sha256
    )


def materialize_shard(shard: ShardRef, on_phase=None) -> Path:
    target = _cache_path(shard)
    if _verified(target, shard):
        return target
    target.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    partial.unlink(missing_ok=True)
    if on_phase:
        on_phase({"phase": "pretokenized_shard_download_start", "url": shard.url})
    request = Request(shard.url, headers={"User-Agent": "teutonic-eval/1.0"})
    try:
        with urlopen(request, timeout=600) as response, partial.open("xb") as output:
            while chunk := response.read(8 * 1024 * 1024):
                output.write(chunk)
        if not _verified(partial, shard):
            raise RuntimeError("downloaded shard differs from validator descriptor")
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    if on_phase:
        on_phase(
            {
                "phase": "pretokenized_shard_download_done",
                "url": shard.url,
                "sha256": shard.sha256,
                "size_bytes": shard.size_bytes,
            }
        )
    return target


def _load_with_retry(
    shard: ShardRef,
    req: base.EvalRequest,
    rng: np.random.Generator,
    limit: int,
    on_phase=None,
) -> tuple[Path, list[list[int]]]:
    path = materialize_shard(shard, on_phase=on_phase)
    try:
        return path, base.load_sequences_from_npy_shard(str(path), req, rng, limit)
    except Exception as exc:
        if not base.is_truncated_npy_error(exc):
            raise
        path.unlink(missing_ok=True)
        path = materialize_shard(shard, on_phase=on_phase)
        return path, base.load_sequences_from_npy_shard(str(path), req, rng, limit)


def _source_seed(dataset_seed: int, source: str) -> int:
    digest = hashlib.blake2b(f"{dataset_seed}:{source}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "little")


def sample_pretokenized_sequences(
    req: base.EvalRequest, on_phase=None
) -> tuple[list[list[int]], dict]:
    dataset_seed = base.dataset_seed(req)
    sources = req.dataset_sources
    if not sources:
        raise ValueError("validator request contains no pre-tokenized dataset sources")
    if sum(int(source["target_sequences"]) for source in sources) != req.n:
        raise ValueError("validator source targets do not sum to evaluation sample count")

    sequences: list[list[int]] = []
    source_labels: list[str] = []
    source_meta: list[dict] = []
    for source in sources:
        name = str(source["name"])
        target = int(source["target_sequences"])
        rng = np.random.default_rng(_source_seed(dataset_seed, name))
        selected: list[list[int]] = []
        used_shards: list[dict] = []
        for raw in source["shards"]:
            if len(selected) >= target:
                break
            shard = ShardRef(
                source=name,
                url=str(raw["url"]),
                sha256=str(raw["sha256"]),
                size_bytes=int(raw["size_bytes"]),
                n_tokens=int(raw["n_tokens"]),
            )
            remaining = target - len(selected)
            load_limit = int(remaining * 1.5) + 8 if req.vocab_size > 0 else remaining
            _local_path, loaded = _load_with_retry(
                shard, req, rng, load_limit, on_phase=on_phase
            )
            if req.vocab_size > 0:
                loaded = [sequence for sequence in loaded if max(sequence) < req.vocab_size]
            selected.extend(loaded)
            used_shards.append(
                {
                    "url": shard.url,
                    "sha256": shard.sha256,
                }
            )
        if len(selected) < target:
            raise RuntimeError(
                f"source {name!r} produced {len(selected)}/{target} requested sequences"
            )
        taken = selected[:target]
        sequences.extend(taken)
        source_labels.extend([name] * target)
        source_meta.append(
            {
                "name": name,
                "proportion": float(source["proportion"]),
                "target_sequences": target,
                "n_sequences": len(taken),
                "used_shards": used_shards,
                "used_refs": [item["url"] for item in used_shards],
            }
        )
        if on_phase:
            on_phase(
                {
                    "phase": "pretokenized_source_sampled",
                    "source": name,
                    "target_sequences": target,
                    "n_sequences": len(taken),
                    "used_shards": len(used_shards),
                }
            )

    tagged = list(zip(sequences, source_labels, strict=True))
    random.Random(dataset_seed).shuffle(tagged)
    sequences = [sequence for sequence, _source in tagged]
    source_labels = [source for _sequence, source in tagged]
    digest = hashlib.sha256(np.asarray(sequences, dtype=np.int64).tobytes()).hexdigest()
    return sequences, {
        "n": len(sequences),
        "seq_len": req.seq_len,
        "dataset_seed": dataset_seed,
        "seed_material": base.dataset_seed_material(req),
        "block_hash": req.block_hash,
        "hotkey": req.hotkey,
        "digest": digest,
        "source": "pretokenized_npy",
        "sources": source_meta,
        "_source_labels": source_labels,
    }


def sample_eval_sequences(req: base.EvalRequest, on_phase=None):
    if req.dataset_source != "pretokenized_npy":
        raise ValueError("GPU evaluator accepts only validator-provided pre-tokenized NPY shards")
    return sample_pretokenized_sequences(req, on_phase=on_phase)


base.sample_eval_sequences = sample_eval_sequences
