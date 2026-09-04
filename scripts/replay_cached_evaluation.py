#!/usr/bin/env python3
"""Replay the exact evaluated prefix from a cached production evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path

import numpy as np

from teutonic.evaluation import EvaluationRequestV2
from teutonic.evaluator import engine, sources


def snapshot_path(cache_dir: Path, digest: str) -> Path:
    path = cache_dir / digest
    if not (path / "config.json").is_file() or not any(path.glob("*.safetensors")):
        raise FileNotFoundError(f"cached model snapshot is incomplete: {path}")
    return path


def parse_gpu_ids(value: str) -> list[int]:
    gpu_ids = [int(item) for item in value.split(",")]
    if len(gpu_ids) != 8 or len(set(gpu_ids)) != 8:
        raise argparse.ArgumentTypeError("exactly eight distinct GPU IDs are required")
    return gpu_ids


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--model-cache", required=True, type=Path)
    parser.add_argument("--batch-size", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--gpu-ids", type=parse_gpu_ids, default=list(range(8)))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--attn-implementation",
        choices=("eager", "flash_attention_4"),
        default="flash_attention_4",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    cached_record = json.loads(args.record.read_text())
    protocol_request = EvaluationRequestV2.from_mapping(cached_record["request"])
    reference = cached_record["verdict"]
    evaluated_count = int(reference.get("n_sequences_evaluated", reference["n_sequences"]))

    king_snapshot = snapshot_path(args.model_cache, protocol_request.king.expected_digest)
    challenger_snapshot = snapshot_path(
        args.model_cache,
        protocol_request.challenger.expected_digest,
    )
    base_request = engine.internal_request_from_v2(
        protocol_request,
        str(king_snapshot),
        str(challenger_snapshot),
    )
    request = engine.EvalRequest(
        **{
            **base_request.model_dump(),
            "attn_implementation": args.attn_implementation,
            "batch_size": args.batch_size,
            "early_stop_enabled": False,
            "king_digest": protocol_request.king.expected_digest,
            "challenger_digest": protocol_request.challenger.expected_digest,
        }
    )

    def progress(event: dict) -> None:
        phase = event.get("phase")
        if phase in {"model_worker_ready", "model_workers_loading", "heartbeat"} or (
            phase == "eval_progress" and int(event.get("done", 0)) % 100 == 0
        ):
            print(json.dumps(event, sort_keys=True), flush=True)

    all_sequences, dataset_meta = sources.sample_eval_sequences(request, on_phase=progress)
    if evaluated_count > len(all_sequences):
        raise RuntimeError(
            f"reference evaluated {evaluated_count} of only {len(all_sequences)} reconstructed sequences"
        )
    sequences = all_sequences[:evaluated_count]
    labels = dataset_meta["_source_labels"][:evaluated_count]
    sequence_digest = hashlib.sha256(np.asarray(sequences, dtype=np.int64).tobytes()).hexdigest()

    if args.prepare_only:
        report = {
            "status": "prepared",
            "reference_evaluation_id": protocol_request.evaluation_id,
            "reference_request_sha256": protocol_request.request_sha256,
            "requested_sequences": len(all_sequences),
            "evaluated_prefix_sequences": evaluated_count,
            "full_sequence_digest": dataset_meta["digest"],
            "evaluated_prefix_digest": sequence_digest,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2, sort_keys=True), flush=True)
        return

    started = time.perf_counter()
    try:
        king_losses, challenger_losses, worker_meta = engine.score_with_model_workers(
            sequences,
            request,
            str(king_snapshot),
            str(challenger_snapshot),
            args.gpu_ids,
            progress,
        )
    finally:
        if engine._model_worker_pool is not None:
            engine._model_worker_pool.close(force=True)
            engine._model_worker_pool = None
    elapsed = time.perf_counter() - started

    verdict = engine.bootstrap_verdict(king_losses, challenger_losses, request)
    verdict["source_scores"] = engine._compute_source_scores(
        king_losses,
        challenger_losses,
        labels,
    )
    comparison = {
        key: float(verdict[key]) - float(reference[key])
        for key in ("avg_king_loss", "avg_challenger_loss", "mu_hat", "lcb")
    }
    report = {
        "status": "complete",
        "reference_record": str(args.record),
        "reference_evaluation_id": protocol_request.evaluation_id,
        "reference_request_sha256": protocol_request.request_sha256,
        "king_digest": protocol_request.king.expected_digest,
        "challenger_digest": protocol_request.challenger.expected_digest,
        "attention_implementation": args.attn_implementation,
        "batch_size": args.batch_size,
        "n_sequences": evaluated_count,
        "sequence_digest": sequence_digest,
        "elapsed_seconds": elapsed,
        "sequences_per_second": evaluated_count / elapsed,
        "reference": {
            key: reference[key] for key in ("avg_king_loss", "avg_challenger_loss", "mu_hat", "lcb")
        },
        "replay": verdict,
        "replay_minus_reference": comparison,
        "worker_metadata": worker_meta,
        "king_losses": king_losses,
        "challenger_losses": challenger_losses,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "n_sequences": evaluated_count,
                "sequence_digest": sequence_digest,
                "sequences_per_second": report["sequences_per_second"],
                "replay_minus_reference": comparison,
            },
            indent=2,
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
