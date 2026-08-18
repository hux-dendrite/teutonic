from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Sequence


class WeightPlanError(ValueError):
    pass


def weight_payload_digest(target_uids: Sequence[int], normalized_weights: Sequence[float]) -> str:
    payload = {
        "target_uids": list(target_uids),
        "normalized_weights": list(normalized_weights),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class WeightPlan:
    publication_id: str
    competition_id: str
    source_reign_id: str
    current_reign_id: str
    reign_number: int
    policy_version: str
    target_uids: tuple[int, ...]
    normalized_weights: tuple[float, ...]
    payload_sha256: str
    idempotency_key: str
    previous_state: str
    attempt_count: int
    attempt_id: str | None = None
    attempt_sequence: int | None = None
    scheduled_block: int | None = None
    submission_started_block: int | None = None
    submission_expires_block: int | None = None
    observed_last_update: int | None = None
    extrinsic_id: str | None = None

    def validate(self, *, uid_count: int | None = None) -> None:
        if not self.target_uids or len(self.target_uids) != len(self.normalized_weights):
            raise WeightPlanError("weight plan cardinality is invalid")
        if len(set(self.target_uids)) != len(self.target_uids):
            raise WeightPlanError("weight plan contains duplicate target UIDs")
        if any(
            isinstance(uid, bool) or not isinstance(uid, int) or uid < 0
            for uid in self.target_uids
        ):
            raise WeightPlanError("weight plan contains an invalid target UID")
        if uid_count is not None and any(uid >= uid_count for uid in self.target_uids):
            raise WeightPlanError("weight plan targets a UID outside the finalized metagraph")
        if any(not math.isfinite(weight) or weight < 0.0 for weight in self.normalized_weights):
            raise WeightPlanError("weight plan contains a non-finite or negative weight")
        if not math.isclose(sum(self.normalized_weights), 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise WeightPlanError("normalized weights must sum to one")
        expected = weight_payload_digest(self.target_uids, self.normalized_weights)
        if self.payload_sha256 != expected:
            raise WeightPlanError("frozen weight payload digest does not match its values")
        expected_key = f"publish-weights:{self.source_reign_id}"
        if self.idempotency_key != expected_key:
            raise WeightPlanError("weight publication idempotency key is not canonical")

    @property
    def is_current(self) -> bool:
        return self.source_reign_id == self.current_reign_id

    @property
    def is_recovery(self) -> bool:
        return self.submission_started_block is not None


@dataclass(frozen=True, slots=True)
class ChainObservation:
    current_block: int
    finalized_block: int
    uid_count: int
    validator_uid: int
    last_update: int
    weights_match: bool


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    success: bool
    extrinsic_id: str | None = None
    included_block: int | None = None
    included_block_hash: str | None = None
    finalized_block: int | None = None
    finalized_block_hash: str | None = None
    error_code: str | None = None
