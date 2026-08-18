from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class EvaluationPolicyConfig:
    policy_version: str
    code_version: str
    dataset_version: str
    tokenizer_version: str
    evaluator_version: str
    sampling_seed: int
    bootstrap_seed: int
    n: int
    seq_len: int
    n_bootstrap: int
    alpha: float
    delta_threshold: float
    dataset_source: str
    dataset_label: str
    tokenizer_backend: str
    tokenizer_label: str
    lease: timedelta = timedelta(minutes=2)
    retry_base_delay: timedelta = timedelta(seconds=30)
    max_attempts: int = 3
    publish_non_winning_models: bool = False

    def __post_init__(self) -> None:
        if not all(
            (
                self.policy_version,
                self.code_version,
                self.dataset_version,
                self.tokenizer_version,
                self.evaluator_version,
            )
        ):
            raise ValueError("all evaluation version identities are required")
        if self.lease <= timedelta(0) or self.retry_base_delay < timedelta(0):
            raise ValueError("lease must be positive and retry delay cannot be negative")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.tokenizer_backend not in {"huggingface", "gigatoken"}:
            raise ValueError("unsupported tokenizer backend")

    @property
    def thresholds(self) -> dict[str, int | float]:
        return {
            "n": self.n,
            "seq_len": self.seq_len,
            "n_bootstrap": self.n_bootstrap,
            "alpha": self.alpha,
            "delta_threshold": self.delta_threshold,
            "batch_size": 1,
        }

    def retry_delay(self, attempt_number: int) -> timedelta:
        exponent = max(0, min(attempt_number - 1, 8))
        return self.retry_base_delay * (2**exponent)


@dataclass(frozen=True, slots=True)
class ClaimedEvaluation:
    evaluation_id: str
    upload_id: str
    attempt_number: int
    competition_id: str
    claimed_king_reign_id: str
    request: Mapping[str, Any]

    @property
    def eval_id(self) -> str:
        return f"{self.evaluation_id}:{self.attempt_number}"


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    evaluation_id: str
    upload_id: str
    attempt_number: int
    evaluator_job_id: str
    owner_instance_id: str | None
    competition_id: str
    claimed_king_reign_id: str
    request: Mapping[str, Any]
