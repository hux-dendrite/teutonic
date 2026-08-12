#!/usr/bin/env python3
"""Compare HF and Gigatoken packed samples on disjoint FineWeb-Edu files."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download
from transformers import AutoTokenizer

_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from eval.tokenization import encode_batch, prepare_tokenizer  # noqa: I001


DEFAULT_MODEL = "dendriteholdings/mimo-v2.5-pro-104b-64e-w1024-top4"
DEFAULT_DATASET = "HuggingFaceFW/fineweb-edu"
DEFAULT_DATASET_TEMPLATE = "sample/10BT/{index:03d}_00000.parquet"


@dataclass
class SequencePacker:
    seq_len: int
    target: int
    remainder: list[int] = field(default_factory=list)
    count: int = 0
    digest: object = field(default_factory=hashlib.sha256)

    def add(self, token_ids: list[int], eos_id: int | None) -> None:
        if self.count >= self.target:
            return
        self.remainder.extend(token_ids)
        if eos_id is not None:
            self.remainder.append(eos_id)
        while len(self.remainder) >= self.seq_len and self.count < self.target:
            sequence = self.remainder[: self.seq_len]
            del self.remainder[: self.seq_len]
            self.digest.update(np.asarray(sequence, dtype="<u4").tobytes())
            self.count += 1

    @property
    def hexdigest(self) -> str:
        return self.digest.hexdigest()


def resolve_parquet_files(args: argparse.Namespace) -> list[str]:
    if args.parquet:
        files = [str(Path(value).expanduser().resolve()) for value in args.parquet]
    else:
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        files = [
            hf_hub_download(
                repo_id=args.dataset,
                repo_type="dataset",
                filename=DEFAULT_DATASET_TEMPLATE.format(index=index),
                token=token,
            )
            for index in range(args.repeats)
        ]
    if len(files) < args.repeats:
        raise ValueError(f"need at least {args.repeats} parquet files, got {len(files)}")
    missing = [path for path in files[: args.repeats] if not Path(path).is_file()]
    if missing:
        raise FileNotFoundError(f"missing parquet files: {missing}")
    return files[: args.repeats]


def compare_file(
    path: str,
    hf_tokenizer,
    gigatoken_tokenizer,
    *,
    seq_len: int,
    samples: int,
    text_column: str,
    min_chars: int,
    batch_size: int,
) -> dict:
    parquet = pq.ParquetFile(path)
    if text_column not in parquet.schema_arrow.names:
        raise KeyError(f"{path} does not contain {text_column!r}")

    eos_id = hf_tokenizer.eos_token_id
    if eos_id is None:
        eos_id = hf_tokenizer.sep_token_id
    hf_packer = SequencePacker(seq_len, samples)
    giga_packer = SequencePacker(seq_len, samples)
    docs_seen = 0
    hf_seconds = 0.0
    giga_seconds = 0.0

    for batch in parquet.iter_batches(batch_size=batch_size, columns=[text_column]):
        texts = [
            text
            for text in batch.column(text_column).to_pylist()
            if isinstance(text, str) and len(text) >= min_chars
        ]
        if not texts:
            continue
        docs_seen += len(texts)

        started = time.perf_counter()
        hf_rows = encode_batch(hf_tokenizer, texts)
        hf_seconds += time.perf_counter() - started

        started = time.perf_counter()
        giga_rows = encode_batch(gigatoken_tokenizer, texts)
        giga_seconds += time.perf_counter() - started

        if len(hf_rows) != len(giga_rows):
            raise AssertionError(
                f"batch row count mismatch: hf={len(hf_rows)} gigatoken={len(giga_rows)}"
            )
        for doc_offset, (hf_ids, giga_ids) in enumerate(zip(hf_rows, giga_rows)):
            if hf_ids != giga_ids:
                mismatch = next(
                    (idx for idx, pair in enumerate(zip(hf_ids, giga_ids)) if pair[0] != pair[1]),
                    min(len(hf_ids), len(giga_ids)),
                )
                raise AssertionError(
                    f"token mismatch at document {docs_seen - len(texts) + doc_offset}, "
                    f"token {mismatch}: hf_len={len(hf_ids)} giga_len={len(giga_ids)}"
                )
            hf_packer.add(hf_ids, eos_id)
            giga_packer.add(giga_ids, eos_id)
        if hf_packer.count >= samples and giga_packer.count >= samples:
            break

    if hf_packer.count != samples or giga_packer.count != samples:
        raise RuntimeError(
            f"{path} produced hf={hf_packer.count} gigatoken={giga_packer.count} "
            f"samples, expected {samples}"
        )
    if hf_packer.hexdigest != giga_packer.hexdigest:
        raise AssertionError(
            f"packed sample digest mismatch: hf={hf_packer.hexdigest} "
            f"gigatoken={giga_packer.hexdigest}"
        )
    return {
        "parquet": path,
        "docs_seen": docs_seen,
        "samples": samples,
        "seq_len": seq_len,
        "hf_digest": hf_packer.hexdigest,
        "gigatoken_digest": giga_packer.hexdigest,
        "match": True,
        "hf_tokenization_s": round(hf_seconds, 3),
        "gigatoken_tokenization_s": round(giga_seconds, 3),
        "speedup": round(hf_seconds / giga_seconds, 2) if giga_seconds else None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--parquet", action="append", default=[])
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--samples", type=int, default=25_000)
    parser.add_argument("--seq-len", type=int, default=8192)
    parser.add_argument("--text-column", default="text")
    parser.add_argument("--min-chars", type=int, default=128)
    parser.add_argument("--batch-size", type=int, default=1024)
    parser.add_argument("--report", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repeats <= 0 or args.samples <= 0 or args.seq_len <= 1:
        raise ValueError("repeats and samples must be positive; seq-len must be at least 2")

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    hf_tokenizer = AutoTokenizer.from_pretrained(
        args.model,
        token=token,
        trust_remote_code=True,
        use_fast=True,
    )
    gigatoken_tokenizer, _ = prepare_tokenizer(hf_tokenizer, "gigatoken")
    files = resolve_parquet_files(args)

    started = time.time()
    repetitions = []
    for repeat, path in enumerate(files):
        result = compare_file(
            path,
            hf_tokenizer,
            gigatoken_tokenizer,
            seq_len=args.seq_len,
            samples=args.samples,
            text_column=args.text_column,
            min_chars=args.min_chars,
            batch_size=args.batch_size,
        )
        result["repeat"] = repeat
        repetitions.append(result)
        print(json.dumps(result, sort_keys=True), flush=True)

    report = {
        "model": args.model,
        "dataset": args.dataset,
        "repeats": args.repeats,
        "samples_per_repeat": args.samples,
        "seq_len": args.seq_len,
        "all_match": all(item["match"] for item in repetitions),
        "wall_time_s": round(time.time() - started, 3),
        "results": repetitions,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        Path(args.report).write_text(rendered + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
