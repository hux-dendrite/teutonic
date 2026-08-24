from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from .chain import WeightChainGateway
from .contracts import SubmissionReceipt, WeightPlan, WeightPlanError
from .repository import WeightPublicationRepository


log = logging.getLogger("teutonic.weight-publisher.jobs")


class WeightPublisher:
    def __init__(
        self,
        repository: WeightPublicationRepository,
        chain: WeightChainGateway,
        *,
        publisher_mode: str,
        lease: timedelta = timedelta(seconds=60),
        retry_base_delay: timedelta = timedelta(seconds=30),
        max_attempts: int = 12,
        finality_safety_blocks: int = 12,
        clock: Callable[[], datetime] | None = None,
        after_stage: Callable[[str, WeightPlan, SubmissionReceipt | None], None] | None = None,
    ) -> None:
        if publisher_mode not in {"dry_run", "active"}:
            raise ValueError("weight publisher mode must be dry_run or active")
        if lease <= timedelta(0) or retry_base_delay < timedelta(0):
            raise ValueError("weight publisher lease/retry policy is invalid")
        if max_attempts < 1 or finality_safety_blocks < 1:
            raise ValueError("weight publisher attempt/finality policy is invalid")
        self.repository = repository
        self.chain = chain
        self.publisher_mode = publisher_mode
        self.lease = lease
        self.retry_base_delay = retry_base_delay
        self.max_attempts = max_attempts
        self.finality_safety_blocks = finality_safety_blocks
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.after_stage = after_stage

    def _stage(
        self, name: str, plan: WeightPlan, receipt: SubmissionReceipt | None = None
    ) -> None:
        if self.after_stage is not None:
            self.after_stage(name, plan, receipt)

    def run_one(self, *, propagate: bool = False) -> bool:
        plan = self.repository.claim_next(
            now=self.clock(), lease=self.lease, current_block=self.chain.current_block()
        )
        if plan is None:
            return False
        log.info(
            "weight plan claimed publication=%s reign=%d revision=%d attempt=%d scheduled_block=%s recovery=%s",
            plan.publication_id,
            plan.reign_number,
            plan.payload_revision,
            plan.attempt_count,
            plan.scheduled_block if plan.scheduled_block is not None else "-",
            plan.is_recovery,
        )
        try:
            plan.validate()
            observation = self.chain.observe(plan)
            plan.validate(uid_count=observation.uid_count)
            if plan.is_recovery:
                self._recover(plan, observation)
            else:
                self._submit(plan, observation)
        except WeightPlanError as exc:
            self.repository.retry_or_fail(
                plan,
                now=self.clock(),
                error_code=type(exc).__name__,
                retry_delay=timedelta(0),
                max_attempts=self.max_attempts,
                terminal=True,
            )
            log.error(
                "weight plan failed publication=%s error=%s retry=false",
                plan.publication_id,
                type(exc).__name__,
            )
            if propagate:
                raise
        except Exception as exc:
            exponent = max(0, min(plan.attempt_count - 1, 8))
            self.repository.retry_or_fail(
                plan,
                now=self.clock(),
                error_code=type(exc).__name__,
                retry_delay=self.retry_base_delay * (2**exponent),
                max_attempts=self.max_attempts,
            )
            log.warning(
                "weight plan retry scheduled publication=%s attempt=%d error=%s",
                plan.publication_id,
                plan.attempt_count,
                type(exc).__name__,
                exc_info=True,
            )
            if propagate:
                raise
        return True

    def _recover(self, plan: WeightPlan, observation) -> None:
        if not self.repository.is_current(plan):
            self.repository.supersede_claim(plan, now=self.clock())
            self._stage("superseded", plan)
            return
        if plan.extrinsic_id is not None:
            finalized = self.chain.find_finalized(
                plan.extrinsic_id,
                start_block=plan.submission_started_block or 0,
            )
            if finalized is not None:
                self.repository.mark_finalized(plan, receipt=finalized, now=self.clock())
                self._stage("reconciled_finalized", plan, finalized)
                return
        updated_after_submission = (
            plan.observed_last_update is not None
            and observation.last_update > plan.observed_last_update
        )
        if observation.weights_match and updated_after_submission:
            receipt = SubmissionReceipt(
                True,
                extrinsic_id=plan.extrinsic_id,
                included_block=observation.last_update,
                finalized_block=observation.finalized_block,
                finalized_block_hash=f"finalized-observation:{observation.finalized_block}",
            )
            self.repository.mark_finalized(plan, receipt=receipt, now=self.clock())
            self._stage("reconciled_finalized", plan, receipt)
            return
        deadline = (plan.submission_expires_block or observation.current_block) + (
            self.finality_safety_blocks
        )
        if observation.finalized_block <= deadline:
            self.repository.retry_or_fail(
                plan,
                now=self.clock(),
                error_code="submission_outcome_pending",
                retry_delay=self.retry_base_delay,
                max_attempts=self.max_attempts,
            )
            self._stage("reconciliation_deferred", plan)
            return
        self._submit(plan, observation)

    def _submit(self, plan: WeightPlan, observation) -> None:
        if not self.repository.is_current(plan):
            self.repository.supersede_claim(plan, now=self.clock())
            self._stage("superseded", plan)
            return
        expires = observation.current_block + self.chain.mortality_period
        self.repository.mark_submitting(
            plan,
            now=self.clock(),
            lease=self.lease,
            publisher_mode=self.publisher_mode,
            network=self.chain.network,
            signer_hotkey=self.chain.signer_hotkey,
            started_block=observation.current_block,
            expires_block=expires,
            observed_last_update=observation.last_update,
        )
        self._stage("submitting", plan)
        receipt = self.chain.submit(plan)
        # Fault injection here models process death after network acceptance and before
        # PostgreSQL acknowledgement. Recovery will observe finality or await mortality.
        self._stage("external_returned", plan, receipt)
        if not receipt.success:
            self.repository.retry_or_fail(
                plan,
                now=self.clock(),
                error_code=receipt.error_code or "chain_rejected",
                retry_delay=self.retry_base_delay,
                max_attempts=self.max_attempts,
            )
            return
        if receipt.extrinsic_id is None:
            self.repository.retry_or_fail(
                plan,
                now=self.clock(),
                error_code="submission_receipt_missing",
                retry_delay=self.retry_base_delay,
                max_attempts=self.max_attempts,
            )
            return
        self.repository.mark_submitted(plan, receipt=receipt, now=self.clock())
        self._stage("submitted", plan, receipt)
        if receipt.included_block is not None:
            self.repository.mark_included(plan, receipt=receipt, now=self.clock())
            self._stage("included", plan, receipt)
        if receipt.finalized_block is not None:
            self.repository.mark_finalized(plan, receipt=receipt, now=self.clock())
            self._stage("finalized", plan, receipt)
