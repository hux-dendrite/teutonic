#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import math
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


log = logging.getLogger("teutonic.genesis-initializer")
MIB = 1024 * 1024
GIB = 1024 * MIB
BF16_BYTES = 2
INITIALIZER_VERSION = "teutonic-bf16-normal-v1"


@dataclass(frozen=True, slots=True)
class TensorSpec:
    name: str
    shape: tuple[int, ...]
    fill: str = "normal"

    @property
    def elements(self) -> int:
        return math.prod(self.shape)

    @property
    def size(self) -> int:
        return self.elements * BF16_BYTES


def _positive_int(config: dict[str, Any], name: str) -> int:
    value = config.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError(f"config.json {name} must be a positive integer")
    return value


def tensor_inventory(config: dict[str, Any]) -> tuple[TensorSpec, ...]:
    if config.get("model_type") != "mimo_v2":
        raise RuntimeError("initializer only supports model_type=mimo_v2")
    if config.get("dtype") not in {"bfloat16", "bf16"}:
        raise RuntimeError("genesis config dtype must be bfloat16")
    if config.get("attention_projection_layout") != "fused_qkv":
        raise RuntimeError("genesis initializer currently requires fused_qkv projections")

    hidden = _positive_int(config, "hidden_size")
    vocab = _positive_int(config, "vocab_size")
    layer_count = _positive_int(config, "num_hidden_layers")
    routed_experts = _positive_int(config, "n_routed_experts")
    moe_intermediate = _positive_int(config, "moe_intermediate_size")
    dense_intermediate = _positive_int(config, "intermediate_size")
    shared_experts = int(config.get("n_shared_experts") or 0)
    if shared_experts < 0:
        raise RuntimeError("config.json n_shared_experts cannot be negative")

    hybrid = config.get("hybrid_layer_pattern")
    moe_frequency = config.get("moe_layer_freq")
    if not isinstance(hybrid, list) or len(hybrid) != layer_count:
        raise RuntimeError("hybrid_layer_pattern must have one entry per layer")
    if not isinstance(moe_frequency, list) or len(moe_frequency) != layer_count:
        raise RuntimeError("moe_layer_freq must have one entry per layer")

    specs: list[TensorSpec] = [TensorSpec("model.embed_tokens.weight", (vocab, hidden))]
    for layer in range(layer_count):
        prefix = f"model.layers.{layer}"
        is_swa = hybrid[layer] == 1
        attention_heads = _positive_int(
            config, "swa_num_attention_heads" if is_swa else "num_attention_heads"
        )
        key_value_heads = _positive_int(
            config, "swa_num_key_value_heads" if is_swa else "num_key_value_heads"
        )
        head_dim = _positive_int(config, "swa_head_dim" if is_swa else "head_dim")
        value_head_dim = _positive_int(
            config, "swa_v_head_dim" if is_swa else "v_head_dim"
        )
        q_size = attention_heads * head_dim
        k_size = key_value_heads * head_dim
        v_size = key_value_heads * value_head_dim
        output_hidden = attention_heads * value_head_dim

        if (
            (is_swa and config.get("add_swa_attention_sink_bias"))
            or (not is_swa and config.get("add_full_attention_sink_bias"))
        ):
            specs.append(
                TensorSpec(
                    f"{prefix}.self_attn.attention_sink_bias",
                    (attention_heads,),
                    "zeros",
                )
            )
        specs.extend(
            (
                TensorSpec(
                    f"{prefix}.self_attn.qkv_proj.weight",
                    (q_size + k_size + v_size, hidden),
                ),
                TensorSpec(
                    f"{prefix}.self_attn.o_proj.weight",
                    (hidden, output_hidden),
                ),
            )
        )

        if moe_frequency[layer]:
            for expert in range(routed_experts):
                expert_prefix = f"{prefix}.mlp.experts.{expert}"
                specs.extend(_mlp_specs(expert_prefix, hidden, moe_intermediate))
            specs.extend(
                (
                    TensorSpec(f"{prefix}.mlp.gate.weight", (routed_experts, hidden)),
                    TensorSpec(
                        f"{prefix}.mlp.gate.e_score_correction_bias",
                        (routed_experts,),
                        "zeros",
                    ),
                )
            )
            if shared_experts:
                specs.extend(
                    _mlp_specs(
                        f"{prefix}.mlp.shared_experts",
                        hidden,
                        shared_experts * moe_intermediate,
                    )
                )
        else:
            specs.extend(_mlp_specs(f"{prefix}.mlp", hidden, dense_intermediate))

        specs.extend(
            (
                TensorSpec(f"{prefix}.input_layernorm.weight", (hidden,), "ones"),
                TensorSpec(
                    f"{prefix}.post_attention_layernorm.weight", (hidden,), "ones"
                ),
            )
        )
    specs.extend(
        (
            TensorSpec("model.norm.weight", (hidden,), "ones"),
            TensorSpec("lm_head.weight", (vocab, hidden)),
        )
    )
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise RuntimeError("initializer produced duplicate tensor names")
    return tuple(specs)


def _mlp_specs(prefix: str, hidden: int, intermediate: int) -> tuple[TensorSpec, ...]:
    return (
        TensorSpec(f"{prefix}.gate_proj.weight", (intermediate, hidden)),
        TensorSpec(f"{prefix}.up_proj.weight", (intermediate, hidden)),
        TensorSpec(f"{prefix}.down_proj.weight", (hidden, intermediate)),
    )


def _shards(
    specs: Iterable[TensorSpec], *, max_shard_size: int
) -> tuple[tuple[TensorSpec, ...], ...]:
    groups: list[list[TensorSpec]] = []
    current: list[TensorSpec] = []
    current_size = 0
    for spec in specs:
        if spec.size > max_shard_size:
            raise RuntimeError(
                f"tensor {spec.name} is {spec.size:,} bytes, larger than the shard limit"
            )
        if current and current_size + spec.size > max_shard_size:
            groups.append(current)
            current = []
            current_size = 0
        current.append(spec)
        current_size += spec.size
    if current:
        groups.append(current)
    return tuple(tuple(group) for group in groups)


def _header(specs: Iterable[TensorSpec], *, seed: int) -> bytes:
    offset = 0
    value: dict[str, Any] = {
        "__metadata__": {
            "format": "pt",
            "initializer": INITIALIZER_VERSION,
            "seed": str(seed),
        }
    }
    for spec in specs:
        value[spec.name] = {
            "dtype": "BF16",
            "shape": list(spec.shape),
            "data_offsets": [offset, offset + spec.size],
        }
        offset += spec.size
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return encoded + b" " * ((8 - len(encoded) % 8) % 8)


def _tensor_seed(seed: int, name: str) -> int:
    material = f"{INITIALIZER_VERSION}|{seed}|{name}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "little")


def _bf16_normal_bytes(rng: np.random.Generator, count: int, std: float) -> bytes:
    values = rng.standard_normal(count, dtype=np.float32)
    values *= np.float32(std)
    bits = values.view(np.uint32)
    rounded = bits + np.uint32(0x7FFF) + ((bits >> np.uint32(16)) & np.uint32(1))
    return (rounded >> np.uint32(16)).astype("<u2", copy=False).tobytes()


def _write_shard(
    destination: Path,
    specs: tuple[TensorSpec, ...],
    *,
    seed: int,
    std: float,
    chunk_elements: int,
) -> None:
    header = _header(specs, seed=seed)
    expected_size = 8 + len(header) + sum(spec.size for spec in specs)
    if destination.is_file() and destination.stat().st_size == expected_size:
        with destination.open("rb") as handle:
            observed_header_size = struct.unpack("<Q", handle.read(8))[0]
            observed_header = handle.read(observed_header_size)
        if observed_header == header:
            log.info("reusing completed shard %s", destination.name)
            return
    if destination.exists():
        raise RuntimeError(f"existing shard does not match this initialization: {destination}")

    partial = destination.with_suffix(destination.suffix + ".partial")
    if partial.exists():
        partial.unlink()
    started = time.monotonic()
    with partial.open("xb") as handle:
        handle.write(struct.pack("<Q", len(header)))
        handle.write(header)
        for spec in specs:
            remaining = spec.elements
            rng = np.random.Generator(np.random.PCG64(_tensor_seed(seed, spec.name)))
            while remaining:
                count = min(remaining, chunk_elements)
                if spec.fill == "normal":
                    payload = _bf16_normal_bytes(rng, count, std)
                elif spec.fill == "ones":
                    payload = np.full(count, 0x3F80, dtype="<u2").tobytes()
                elif spec.fill == "zeros":
                    payload = bytes(count * BF16_BYTES)
                else:
                    raise RuntimeError(f"unknown initializer fill {spec.fill!r}")
                handle.write(payload)
                remaining -= count
        handle.flush()
        os.fsync(handle.fileno())
    if partial.stat().st_size != expected_size:
        raise RuntimeError(f"generated shard has an unexpected size: {partial}")
    os.replace(partial, destination)
    elapsed = time.monotonic() - started
    log.info(
        "wrote %s bytes=%s seconds=%.1f MBps=%.1f",
        destination.name,
        f"{expected_size:,}",
        elapsed,
        expected_size / max(elapsed, 0.001) / 1_000_000,
    )


def initialize(
    model_dir: Path,
    *,
    seed: int,
    max_shard_size: int,
    chunk_elements: int,
    workers: int = 1,
) -> tuple[int, int, int]:
    config_path = model_dir / "config.json"
    if not config_path.is_file():
        raise RuntimeError(f"missing model config: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    specs = tensor_inventory(config)
    groups = _shards(specs, max_shard_size=max_shard_size)
    total_shards = len(groups)
    filenames = [
        f"model-{number:05d}-of-{total_shards:05d}.safetensors"
        for number in range(1, total_shards + 1)
    ]
    expected_files = set(filenames)
    unexpected = sorted(
        path.name
        for path in model_dir.glob("*.safetensors")
        if path.name not in expected_files
    )
    if unexpected:
        raise RuntimeError(f"model directory contains unexpected weight shards: {unexpected[:8]}")

    total_size = sum(spec.size for spec in specs)
    log.info(
        "initializing tensors=%s parameters=%s bf16_bytes=%s shards=%s seed=%s",
        len(specs),
        f"{total_size // BF16_BYTES:,}",
        f"{total_size:,}",
        total_shards,
        seed,
    )
    if workers < 1:
        raise RuntimeError("initializer worker count must be positive")
    std = float(config.get("initializer_range", 0.02))

    def write(item: tuple[str, tuple[TensorSpec, ...]]) -> None:
        filename, group = item
        _write_shard(
            model_dir / filename,
            group,
            seed=seed,
            std=std,
            chunk_elements=chunk_elements,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(write, zip(filenames, groups, strict=True)))

    weight_map = {
        spec.name: filename
        for filename, group in zip(filenames, groups, strict=True)
        for spec in group
    }
    index = {
        "metadata": {
            "total_size": total_size,
            "initializer": INITIALIZER_VERSION,
            "seed": seed,
        },
        "weight_map": weight_map,
    }
    index_path = model_dir / "model.safetensors.index.json"
    partial_index = index_path.with_suffix(index_path.suffix + ".partial")
    partial_index.write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(partial_index, index_path)
    return len(specs), total_size // BF16_BYTES, total_shards


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a deterministic BF16 MiMo genesis checkpoint without loading it into RAM."
    )
    parser.add_argument("--model-dir", type=Path, default=Path("model"))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-shard-size-gib", type=int, default=4)
    parser.add_argument("--chunk-mib", type=int, default=32)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if args.seed < 0:
        raise RuntimeError("--seed must be non-negative")
    if args.max_shard_size_gib < 1 or args.chunk_mib < 1 or args.workers < 1:
        raise RuntimeError("shard, chunk, and worker values must be positive")
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    tensors, parameters, shards = initialize(
        args.model_dir.resolve(),
        seed=args.seed,
        max_shard_size=args.max_shard_size_gib * GIB,
        chunk_elements=args.chunk_mib * MIB // np.dtype(np.float32).itemsize,
        workers=args.workers,
    )
    log.info(
        "genesis initialization complete tensors=%s parameters=%s shards=%s",
        tensors,
        f"{parameters:,}",
        shards,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
