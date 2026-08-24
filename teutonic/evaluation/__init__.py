"""Behavior-preserving evaluation seams for the PostgreSQL control plane."""

from .client import (
    EvaluatorBusyError,
    EvaluatorConflictError,
    EvaluatorJobNotFoundError,
    HttpEvaluatorClient,
)
from .policy import (
    build_failure_history_entry,
    build_verdict_history_entry,
    classify_eval_error,
    decide_model_copy,
    normalize_verdict,
    paired_bootstrap_verdict,
    provisional_paired_bootstrap,
    validate_config_lock,
)
from .protocol_v2 import (
    PROTOCOL_VERSION,
    AttemptBusyError,
    AttemptConflictError,
    EvaluationAttemptRegistry,
    EvaluationRequestV2,
    ProtocolValidationError,
    R2Artifact,
    result_provenance,
    validate_result_v2,
)

__all__ = [
    "EvaluatorBusyError",
    "EvaluatorConflictError",
    "EvaluatorJobNotFoundError",
    "HttpEvaluatorClient",
    "build_failure_history_entry",
    "build_verdict_history_entry",
    "classify_eval_error",
    "decide_model_copy",
    "normalize_verdict",
    "paired_bootstrap_verdict",
    "provisional_paired_bootstrap",
    "validate_config_lock",
    "PROTOCOL_VERSION",
    "AttemptBusyError",
    "AttemptConflictError",
    "EvaluationAttemptRegistry",
    "EvaluationRequestV2",
    "ProtocolValidationError",
    "R2Artifact",
    "result_provenance",
    "validate_result_v2",
]
