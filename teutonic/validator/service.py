from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from teutonic.evaluation import (
    EvaluatorBusyError,
    EvaluatorConflictError,
    EvaluatorJobNotFoundError,
    EvaluationRequestV2,
    ProtocolValidationError,
    classify_eval_error,
    validate_result_v2,
)

from .contracts import ClaimedEvaluation, EvaluationPolicyConfig
from .repository import ValidatorRepository


Preflight = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any] | None]]


class ValidatorScheduler:
    def __init__(
        self,
        repository: ValidatorRepository,
        evaluator: Any,
        *,
        policy: EvaluationPolicyConfig,
        preflight: Preflight,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.evaluator = evaluator
        self.policy = policy
        self.preflight = preflight
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run_once(self) -> bool:
        claim = self.repository.claim_next(now=self.clock(), policy=self.policy)
        if claim is None:
            return False
        try:
            failure = await self.preflight(claim.request)
            if failure is not None:
                self.repository.fail_attempt(
                    claim.evaluation_id,
                    now=self.clock(),
                    failure_class="deterministic_submission",
                    public_error_code=str(failure.get("error_code", "invalid_evaluation_input")),
                    retry=False,
                    private_diagnostic_reference=failure.get("private_diagnostic_reference"),
                )
                return True
            response = await self.evaluator.start_attempt(claim.request)
            eval_id = str(response["eval_id"])
            if eval_id != claim.eval_id:
                raise ProtocolValidationError("evaluator returned a different attempt identity")
            self.repository.start_evaluating(
                claim.evaluation_id,
                evaluator_job_id=eval_id,
                now=self.clock(),
                lease=self.policy.lease,
            )
            terminal = await self._consume(claim, eval_id)
            self._persist_terminal(claim, terminal)
        except Exception as exc:
            self._persist_error(claim, exc)
        return True

    async def _consume(self, claim: ClaimedEvaluation, eval_id: str) -> Mapping[str, Any]:
        async for event in self.evaluator.events(eval_id):
            if event.get("evaluation_id") != claim.evaluation_id or event.get(
                "attempt_number"
            ) != claim.attempt_number:
                raise ProtocolValidationError("evaluator event identity mismatch")
            event_type = event.get("type")
            data = event.get("data")
            if not isinstance(data, Mapping):
                raise ProtocolValidationError("evaluator event data must be an object")
            if event_type == "progress":
                self.repository.heartbeat(
                    claim.evaluation_id,
                    now=self.clock(),
                    lease=self.policy.lease,
                    progress=data,
                )
            elif event_type == "verdict":
                return data
            elif event_type == "error":
                raise RuntimeError(f"eval server error: {data.get('code', 'evaluation_failed')}")
        status = await self.evaluator.status(eval_id)
        if status.get("state") == "completed" and isinstance(status.get("verdict"), Mapping):
            return status["verdict"]
        if status.get("state") == "failed":
            raise RuntimeError(f"eval server error: {status.get('error', 'evaluation_failed')}")
        raise RuntimeError("evaluator stream closed before a terminal result")

    def _persist_terminal(self, claim: ClaimedEvaluation, result: Mapping[str, Any]) -> None:
        request = EvaluationRequestV2.from_mapping(claim.request)
        validate_result_v2(result, request)
        self.repository.complete_verdict(
            claim.evaluation_id,
            result=result,
            now=self.clock(),
            publish_non_winning=self.policy.publish_non_winning_models,
            result_artifact_reference=result.get("result_artifact_reference"),
        )

    def _persist_error(self, claim: ClaimedEvaluation, exc: Exception) -> None:
        transient, marker = classify_eval_error(exc)
        if isinstance(exc, EvaluatorBusyError):
            transient, marker = True, "evaluator_busy"
        elif isinstance(exc, EvaluatorJobNotFoundError):
            transient, marker = True, "evaluator_job_lost"
        elif isinstance(exc, (EvaluatorConflictError, ProtocolValidationError)):
            transient, marker = False, "evaluator_policy_mismatch"
        attempt_remaining = claim.attempt_number < self.policy.max_attempts
        retry = transient and attempt_remaining
        failure_class = (
            "transient_infrastructure"
            if transient
            else "policy"
            if isinstance(exc, (EvaluatorConflictError, ProtocolValidationError))
            else "unknown"
        )
        self.repository.fail_attempt(
            claim.evaluation_id,
            now=self.clock(),
            failure_class=failure_class,
            public_error_code=marker or type(exc).__name__,
            retry=retry,
            retry_delay=self.policy.retry_delay(claim.attempt_number),
            private_diagnostic_reference=f"diagnostic:{claim.evaluation_id}:{type(exc).__name__}",
        )

    async def reconcile(self) -> int:
        recovered = 0
        for candidate in self.repository.recovery_candidates(now=self.clock()):
            claim = ClaimedEvaluation(
                evaluation_id=candidate.evaluation_id,
                upload_id=candidate.upload_id,
                attempt_number=candidate.attempt_number,
                competition_id=candidate.competition_id,
                claimed_king_reign_id=candidate.claimed_king_reign_id,
                request=candidate.request,
            )
            try:
                status = await self.evaluator.status(candidate.evaluator_job_id)
            except EvaluatorJobNotFoundError as exc:
                self.repository.adopt(
                    candidate.evaluation_id, now=self.clock(), lease=self.policy.lease
                )
                self._persist_error(claim, exc)
                recovered += 1
                continue
            self.repository.adopt(
                candidate.evaluation_id, now=self.clock(), lease=self.policy.lease
            )
            if status.get("state") == "completed" and isinstance(status.get("verdict"), Mapping):
                self._persist_terminal(claim, status["verdict"])
            elif status.get("state") == "failed":
                self._persist_error(
                    claim, RuntimeError(f"eval server error: {status.get('error', 'failed')}")
                )
            else:
                try:
                    result = await self._consume(claim, candidate.evaluator_job_id)
                    self._persist_terminal(claim, result)
                except Exception as exc:
                    self._persist_error(claim, exc)
            recovered += 1
        return recovered
