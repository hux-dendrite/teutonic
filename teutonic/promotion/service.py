from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import PromotionClaim, inventory_digest
from .rclone import (
    PromotionCollisionError,
    PromotionStorageError,
    RclonePromotionExecutor,
    S3InventoryInspector,
    verify_inventory,
)
from .repository import PromotionRepository


class PromotionWorker:
    def __init__(
        self,
        repository: PromotionRepository,
        executor: RclonePromotionExecutor,
        inspector: S3InventoryInspector,
        *,
        lease: timedelta = timedelta(minutes=2),
        retry_base_delay: timedelta = timedelta(seconds=30),
        max_attempts: int = 8,
        clock: Callable[[], datetime] | None = None,
        on_winner_promoted: Callable[[str], Any] | None = None,
        on_heartbeat: Callable[[], None] | None = None,
        after_stage: Callable[[str, PromotionClaim], None] | None = None,
    ) -> None:
        if lease <= timedelta(0) or retry_base_delay < timedelta(0) or max_attempts < 1:
            raise ValueError("promotion retry policy is invalid")
        self.repository = repository
        self.executor = executor
        self.inspector = inspector
        self.lease = lease
        self.retry_base_delay = retry_base_delay
        self.max_attempts = max_attempts
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.on_winner_promoted = on_winner_promoted
        self.on_heartbeat = on_heartbeat
        self.after_stage = after_stage

    def _stage(self, name: str, claim: PromotionClaim) -> None:
        if self.after_stage is not None:
            self.after_stage(name, claim)

    def run_one(self, *, propagate: bool = False) -> bool:
        claim = self.repository.claim_next(now=self.clock(), lease=self.lease)
        if claim is None:
            return self.reconcile_one_crown(propagate=propagate)
        try:
            self._run_claim(claim)
        except PromotionCollisionError as exc:
            self.repository.fail_or_retry(
                claim,
                now=self.clock(),
                error_code=type(exc).__name__,
                retry_delay=timedelta(0),
                max_attempts=self.max_attempts,
                terminal=True,
            )
            if propagate:
                raise
            return True
        except Exception as exc:
            exponent = max(0, min(claim.attempt_count - 1, 8))
            self.repository.fail_or_retry(
                claim,
                now=self.clock(),
                error_code=type(exc).__name__,
                retry_delay=self.retry_base_delay * (2**exponent),
                max_attempts=self.max_attempts,
                terminal=False,
            )
            if propagate:
                raise
            return True
        if claim.disposition == "winner" and self.on_winner_promoted is not None:
            try:
                self.on_winner_promoted(claim.promotion_id)
            except Exception:
                if propagate:
                    raise
        return True

    def reconcile_one_crown(self, *, propagate: bool = False) -> bool:
        if self.on_winner_promoted is None:
            return False
        pending = self.repository.pending_winner_crowns()
        if not pending:
            return False
        try:
            self.on_winner_promoted(pending[0])
        except Exception:
            if propagate:
                raise
        return True

    def _run_claim(self, claim: PromotionClaim) -> None:
        def heartbeat() -> None:
            self.repository.heartbeat(claim, now=self.clock(), lease=self.lease)
            if self.on_heartbeat is not None:
                self.on_heartbeat()

        destination = self.inspector.inventory(claim.public_bucket, claim.public_prefix)
        verify_inventory(claim.expected, destination, complete=False)

        if claim.state != "deleting_private_source" and set(destination) != set(claim.expected):
            source = self.inspector.inventory(claim.private_bucket, claim.private_prefix)
            verify_inventory(claim.expected, source, complete=True)
            self.executor.copy(
                source_bucket=claim.private_bucket,
                source_prefix=claim.private_prefix,
                destination_bucket=claim.public_bucket,
                destination_prefix=claim.public_prefix,
                heartbeat=heartbeat,
            )
            self._stage("copy_completed", claim)

        if claim.state != "deleting_private_source":
            self.repository.transition(
                claim,
                expected_states=("copying_to_public", "public_copy_verifying"),
                target_state="public_copy_verifying",
                now=self.clock(),
                lease=self.lease,
            )
            self._stage("public_verification_started", claim)
            destination = self.inspector.inventory(claim.public_bucket, claim.public_prefix)
            verify_inventory(claim.expected, destination, complete=True)
            digest = inventory_digest(claim.expected)
            self.repository.transition(
                claim,
                expected_states=("public_copy_verifying",),
                target_state="public_copy_verified",
                now=self.clock(),
                lease=self.lease,
                observed_inventory_sha256=digest,
            )
            self._stage("public_copy_verified", claim)
            self.repository.transition(
                claim,
                expected_states=("public_copy_verified",),
                target_state="deleting_private_source",
                now=self.clock(),
                lease=self.lease,
            )
            self._stage("private_deletion_started", claim)

        source = self.inspector.inventory(claim.private_bucket, claim.private_prefix)
        verify_inventory(claim.expected, source, complete=False)
        if source:
            self.executor.delete_source(
                bucket=claim.private_bucket,
                prefix=claim.private_prefix,
                heartbeat=heartbeat,
            )
            self._stage("private_source_deleted", claim)

        destination = self.inspector.inventory(claim.public_bucket, claim.public_prefix)
        verify_inventory(claim.expected, destination, complete=True)
        remaining_source = self.inspector.inventory(claim.private_bucket, claim.private_prefix)
        if remaining_source:
            raise PromotionStorageError("private source is not empty after deletion")
        model_objects = [item for path, item in destination.items() if path != "manifest.json"]
        self.repository.mark_promoted(
            claim,
            now=self.clock(),
            observed_object_count=len(model_objects),
            observed_size_bytes=sum(item.size for item in model_objects),
            observed_inventory_sha256=inventory_digest(claim.expected),
        )
        self._stage("promoted", claim)
