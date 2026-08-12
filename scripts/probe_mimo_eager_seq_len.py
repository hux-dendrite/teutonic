#!/usr/bin/env python3
"""Empirically find the two-GPU MiMo eager-attention sequence ceiling."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import torch

_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

import eval_server_quasar_pair as pair  # noqa: I001


DEFAULT_REPO = "dendriteholdings/mimo-v2.5-pro-104b-64e-w1024-top4"
DEFAULT_REVISION = "56fc5b83784cb00a32c3f59e5ef92b9b58a7d7a9"


def parse_lengths(value: str) -> list[int]:
    lengths = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not lengths or any(length < 2 for length in lengths):
        raise argparse.ArgumentTypeError("lengths must be comma-separated integers >= 2")
    if lengths != sorted(set(lengths)):
        raise argparse.ArgumentTypeError("lengths must be unique and increasing")
    return lengths


def parse_gpus(value: str) -> list[int]:
    gpu_ids = [int(item.strip()) for item in value.split(",") if item.strip()]
    if len(gpu_ids) != 2 or len(set(gpu_ids)) != 2 or any(gpu_id < 0 for gpu_id in gpu_ids):
        raise argparse.ArgumentTypeError("gpus must contain exactly two distinct non-negative IDs")
    return gpu_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--gpus", type=parse_gpus, default=parse_gpus("0,1"))
    parser.add_argument(
        "--lengths",
        type=parse_lengths,
        default=parse_lengths("8192,10240,12288,14336,16384,20480,24576,32768"),
    )
    parser.add_argument("--report", default="/home/ubuntu/mimo-eager-seq-ceiling.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required")
    unavailable = [gpu_id for gpu_id in args.gpus if gpu_id >= torch.cuda.device_count()]
    if unavailable:
        raise ValueError(f"GPUs are unavailable: {unavailable}")
    digest = f"hf:{args.revision}"
    request = pair.EvalRequest(
        king_repo=args.repo,
        challenger_repo=args.repo,
        king_digest=digest,
        challenger_digest=digest,
        n=1,
        batch_size=1,
        parallel_batch_size=1,
        attn_implementation="eager",
    )
    snapshot = pair.materialize_model(args.repo, digest)
    config, artifacts = pair.load_model_config(snapshot, request, "probe")
    attention = pair.validate_and_report_attention_config(config, "probe")
    model = pair.load_eval_model(
        snapshot,
        config,
        "auto",
        "probe",
        request,
        gpu_ids=args.gpus,
    )

    results = []
    ceiling = None
    first_oom = None
    for seq_len in args.lengths:
        torch.cuda.empty_cache()
        for gpu_id in args.gpus:
            torch.cuda.reset_peak_memory_stats(gpu_id)
        token_ids = ((torch.arange(seq_len, dtype=torch.int64) * 7919) % config.vocab_size).tolist()
        started = time.time()
        try:
            loss = pair.compute_per_sequence_loss(model, [token_ids], request.lm_head_chunk)[0]
        except RuntimeError as exc:
            if "OOM scoring" not in str(exc):
                raise
            first_oom = seq_len
            results.append({
                "seq_len": seq_len,
                "status": "oom",
                "error": str(exc),
                "peak_allocated_gib": {
                    str(gpu_id): round(torch.cuda.max_memory_allocated(gpu_id) / (1024**3), 3)
                    for gpu_id in args.gpus
                },
                "peak_reserved_gib": {
                    str(gpu_id): round(torch.cuda.max_memory_reserved(gpu_id) / (1024**3), 3)
                    for gpu_id in args.gpus
                },
            })
            break
        ceiling = seq_len
        results.append({
            "seq_len": seq_len,
            "status": "ok",
            "loss": loss,
            "wall_time_s": round(time.time() - started, 3),
            "peak_allocated_gib": {
                str(gpu_id): round(torch.cuda.max_memory_allocated(gpu_id) / (1024**3), 3)
                for gpu_id in args.gpus
            },
            "peak_reserved_gib": {
                str(gpu_id): round(torch.cuda.max_memory_reserved(gpu_id) / (1024**3), 3)
                for gpu_id in args.gpus
            },
        })
        print(json.dumps(results[-1], sort_keys=True), flush=True)

    report = {
        "repo": args.repo,
        "revision": args.revision,
        "gpus": args.gpus,
        "gpu_names": {str(gpu_id): torch.cuda.get_device_name(gpu_id) for gpu_id in args.gpus},
        "dtype": "bfloat16",
        "attn_implementation": "eager",
        "use_cache": False,
        "tensor_parallel_size": 1,
        "model_parallel_strategy": "layer_sharding",
        "grouped_moe": True,
        "checkpoint_artifacts": artifacts,
        "attention": attention,
        "largest_tested_success": ceiling,
        "first_tested_oom": first_oom,
        "results": results,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    Path(args.report).write_text(rendered + "\n")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
