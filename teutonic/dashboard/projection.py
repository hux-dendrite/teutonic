from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row

DASHBOARD_LOCK_ID = 6_082_759_349_011_801

PUBLIC_ERROR_MESSAGES = {
    "invalid_evaluation_input": "The submission did not satisfy the evaluation input policy.",
    "config_rejected": "The model configuration was rejected by the public evaluation policy.",
    "model_copy": "The submission matched a model that is not eligible to compete.",
    "evaluator_busy": "Evaluation capacity was temporarily unavailable.",
    "evaluator_job_lost": "The evaluation worker restarted before the result was durable.",
    "evaluation_failed": "The evaluation could not be completed.",
    "protocol_invalid": "The evaluator returned an invalid result contract.",
    "retry_exhausted": "The evaluation could not be completed after retries.",
}


class DashboardProjectionError(RuntimeError):
    pass


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise DashboardProjectionError("public timestamp was not a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes"}


def _scope_sql(view: str) -> str:
    return (
        f"SELECT * FROM control_plane.{view} "
        "WHERE netuid = %s AND chain_generation = %s AND competition = %s"
    )


class DashboardProjectionRepository:
    """Read a complete public projection using only the granted dashboard views."""

    def __init__(
        self,
        connection,
        *,
        netuid: int,
        chain_generation: str,
        competition: str,
        chain_name: str = "Teutonic",
    ) -> None:
        self.connection = connection
        self.netuid = netuid
        self.chain_generation = chain_generation
        self.competition = competition
        self.chain_name = chain_name
        self._scope = (netuid, chain_generation, competition)
        self._lock_held = False

    def acquire_lock(self) -> bool:
        self._lock_held = bool(
            self.connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (DASHBOARD_LOCK_ID,)
            ).fetchone()[0]
        )
        return self._lock_held

    def release_lock(self) -> None:
        if self._lock_held:
            self.connection.execute("SELECT pg_advisory_unlock(%s)", (DASHBOARD_LOCK_ID,))
            self._lock_held = False

    def project(self, *, now: datetime | None = None) -> dict[str, Any]:
        if not self._lock_held:
            raise DashboardProjectionError("dashboard publisher lock is not held")
        generated = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.connection.transaction(), self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            contract = cursor.execute(
                "SELECT schema_version FROM control_plane.dashboard_contract"
            ).fetchone()
            if contract is None or int(contract["schema_version"]) != 1:
                raise DashboardProjectionError("database dashboard contract is not version 1")

            chain = cursor.execute(_scope_sql("dashboard_chain"), self._scope).fetchone()
            stats = cursor.execute(_scope_sql("dashboard_stats"), self._scope).fetchone()
            king_rows = cursor.execute(_scope_sql("dashboard_current_king"), self._scope).fetchall()
            if len(king_rows) > 1:
                raise DashboardProjectionError("multiple current kings in dashboard snapshot")
            reigns = cursor.execute(
                _scope_sql("dashboard_king_reigns") + " ORDER BY reign_number ASC", self._scope
            ).fetchall()
            queue = cursor.execute(
                _scope_sql("dashboard_queue") + " ORDER BY queue_position ASC", self._scope
            ).fetchall()
            current_rows = cursor.execute(
                _scope_sql("dashboard_current_evaluation")
                + " ORDER BY started_at NULLS LAST, challenge_id",
                self._scope,
            ).fetchall()
            if len(current_rows) > 1:
                raise DashboardProjectionError("multiple active evaluations in dashboard snapshot")
            history = cursor.execute(
                _scope_sql("dashboard_evaluation_history")
                + " ORDER BY completed_at ASC NULLS LAST, challenge_id ASC",
                self._scope,
            ).fetchall()
            weights = cursor.execute(_scope_sql("dashboard_weight_status"), self._scope).fetchone()
            services = cursor.execute(
                "SELECT * FROM control_plane.dashboard_service_health ORDER BY service_name"
            ).fetchall()

        generated_at = _iso(generated)
        current = self._current(current_rows[0]) if current_rows else None
        weight_status = self._weight(weights)
        service_status = self._services(services, current, weight_status, generated)
        king = self._king(king_rows[0]) if king_rows else None
        current_weight = _float(king_rows[0].get("current_weight")) if king_rows else None
        payload = {
            "schema_version": 1,
            "publication_id": str(uuid.uuid4()),
            "generated_at": generated_at,
            "updated_at": generated_at,
            "source_watermark": int(stats["source_watermark"]) if stats else 0,
            "chain": {
                "name": self.chain_name,
                "netuid": self.netuid,
                "generation": self.chain_generation,
                "competition": self.competition,
                "finalized_start_block": _int(chain["finalized_start_block"]) if chain else None,
                "last_finalized_block": _int(chain["last_finalized_block"]) if chain else None,
                "observed_at": _iso(chain["observed_at"]) if chain else None,
            },
            "king": king,
            "king_payout": {
                "weight": current_weight,
                "alpha_per_hour": None,
                "usd_per_hour": None,
            },
            "king_chain": [self._reign(row) for row in reigns],
            "stats": {
                "active_registrations": int(stats["active_registrations"]) if stats else 0,
                "queue_depth": int(stats["queue_depth"]) if stats else 0,
                "completed_evaluations": int(stats["completed_evaluations"]) if stats else 0,
                "reign_count": int(stats["reign_count"]) if stats else 0,
            },
            "current_eval": current,
            "queue": [self._queue(row) for row in queue],
            "history": [self._history(row) for row in history],
            "weight_status": weight_status,
            "service_status": service_status,
            "market": None,
        }
        return payload

    def project_dataset_manifest(self, *, now: datetime | None = None) -> dict[str, Any]:
        if not self._lock_held:
            raise DashboardProjectionError("dashboard publisher lock is not held")
        del now
        with self.connection.transaction(), self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            rows = cursor.execute(
                _scope_sql("dashboard_dataset_manifests") + ' ORDER BY "position"',
                self._scope,
            ).fetchall()
        if not rows:
            raise DashboardProjectionError("competition has no active dataset manifest")
        first = rows[0]
        if any(row["config_version"] != first["config_version"] for row in rows):
            raise DashboardProjectionError("multiple evaluation configs in dataset snapshot")
        return {
            "schema_version": 1,
            "generated_at": _iso(first["config_created_at"]),
            "chain": {
                "name": self.chain_name,
                "netuid": self.netuid,
                "generation": self.chain_generation,
                "competition": self.competition,
            },
            "config_version": str(first["config_version"]),
            "dataset_label": str(first["dataset_label"]),
            "eval_n": int(first["eval_n"]),
            "delta_threshold": float(first["delta_threshold"]),
            "sampling": {
                "algorithm": "blake2b-64-block-hash-hotkey-v1",
                "inputs": ["block_hash", "hotkey"],
            },
            "sources": [
                {
                    "name": str(row["name"]),
                    "proportion": float(row["sample_proportion"]),
                    "manifest_url": str(row["manifest_url"]),
                    "manifest_sha256": str(row["manifest_sha256"]),
                    "manifest": row["manifest_json"],
                }
                for row in rows
            ],
        }

    @staticmethod
    def _identity(row) -> dict[str, Any]:
        return {"hotkey": row["hotkey"], "coldkey": row["coldkey"], "uid": int(row["uid"])}

    def _king(self, row) -> dict[str, Any]:
        digest = str(row["public_model_digest"])
        return {
            **self._identity(row),
            "model_repo": row["public_model_name"],
            "model_digest": digest,
            "model_reference": row["public_model_reference"],
            "king_digest": digest,
            "reign_number": int(row["reign_number"]),
            "crowned_at": _iso(row["crowned_at"]),
            "crowned_finalized_block": int(row["crowned_finalized_block"]),
            "mu_hat": _float(row["mu_hat"]),
            "lcb": _float(row["lcb"]),
            "delta": _float(row["delta"]),
            "avg_king_loss": _float(row["avg_king_loss"]),
            "avg_challenger_loss": _float(row["avg_challenger_loss"]),
            "wall_time_s": _float(row["wall_time_s"]),
        }

    def _reign(self, row) -> dict[str, Any]:
        return {
            **self._identity(row),
            "reign_number": int(row["reign_number"]),
            "model_repo": row["public_model_name"],
            "model_digest": str(row["public_model_digest"]),
            "model_reference": row["public_model_reference"],
            "crowned_at": _iso(row["crowned_at"]),
            "crowned_finalized_block": int(row["crowned_finalized_block"]),
            "ended_at": _iso(row["ended_at"]),
            "replacement_reason": row["replacement_reason"],
            "weight": _float(row["current_weight"]),
            "alpha_per_hour": None,
            "usd_per_hour": None,
        }

    def _queue(self, row) -> dict[str, Any]:
        return {
            **self._identity(row),
            "challenge_id": row["challenge_id"],
            "model_identity": "hidden_until_promotion",
            "block": int(row["ready_finalized_block"]),
            "queue_position": int(row["queue_position"]),
            "state": row["state"],
            "submitted_at": _iso(row["submitted_at"]),
        }

    def _current(self, row) -> dict[str, Any]:
        return {
            **self._identity(row),
            "challenge_id": row["challenge_id"],
            "model_identity": "hidden_until_promotion",
            "stage": row["progress_phase"] or row["stage"],
            "progress": _int(row["completed_sequences"]) or 0,
            "total": _int(row["requested_sequences"]) or 0,
            "percent": _float(row["percent"]),
            "elapsed_seconds": _float(row["elapsed_seconds"]),
            "early_stopped": _bool(row["early_stopped"]),
            "policy_version": row["policy_version"],
            "dataset_version": row["dataset_version"],
            "started_at": _iso(row["started_at"]),
            "last_progress_at": _iso(row["last_progress_at"]),
        }

    def _history(self, row) -> dict[str, Any]:
        public = row["public_model_digest"] is not None
        failed = row["verdict"] == "failed"
        error_code = row["public_error_code"] if failed else None
        return {
            **self._identity(row),
            "challenge_id": row["challenge_id"],
            "baseline_hotkey": row["baseline_hotkey"],
            "baseline_coldkey": row["baseline_coldkey"],
            "baseline_uid": int(row["baseline_uid"]),
            "verdict": "error" if failed else row["verdict"],
            "accepted": row["verdict"] == "accepted",
            "mu_hat": _float(row["mu_hat"]),
            "lcb": _float(row["lcb"]),
            "delta": _float(row["delta"]),
            "avg_king_loss": _float(row["avg_king_loss"]),
            "avg_challenger_loss": _float(row["avg_challenger_loss"]),
            "wall_time_s": _float(row["wall_time_s"]),
            "n_sequences_evaluated": _int(row["n_sequences_evaluated"]),
            "n_sequences": _int(row["n_sequences"]),
            "early_stopped": _bool(row["early_stopped"]),
            "error_code": error_code,
            "error_message": PUBLIC_ERROR_MESSAGES.get(error_code) if error_code else None,
            "policy_version": row["policy_version"],
            "dataset_version": row["dataset_version"],
            "timestamp": _iso(row["completed_at"]),
            "challenger_repo": row["public_model_name"] if public else None,
            "challenger_digest": str(row["public_model_digest"]) if public else None,
            "model_reference": row["public_model_reference"] if public else None,
            "publication_disposition": row["publication_disposition"] if public else None,
            "model_identity": "public" if public else "hidden_until_promotion",
        }

    @staticmethod
    def _weight(row) -> dict[str, Any]:
        if row is None:
            return {
                "state": None,
                "cadence_blocks": None,
                "last_attempted_block": None,
                "next_due_block": None,
                "latest_attempt_state": None,
                "latest_finalized_block": None,
                "error_code": None,
                "requested_at": None,
                "submitted_at": None,
                "finalized_at": None,
            }
        return {
            "state": row["state"],
            "cadence_blocks": _int(row["cadence_blocks"]),
            "last_attempted_block": _int(row["last_attempted_block"]),
            "next_due_block": _int(row["next_due_block"]),
            "latest_attempt_state": row["latest_attempt_state"],
            "latest_finalized_block": _int(row["latest_finalized_block"]),
            "error_code": row["public_error_code"],
            "requested_at": _iso(row["requested_at"]),
            "submitted_at": _iso(row["submitted_at"]),
            "finalized_at": _iso(row["finalized_at"]),
        }

    @staticmethod
    def _services(rows, current, weight_status, generated: datetime) -> dict[str, Any]:
        services = [
            {
                "name": row["service_name"],
                "health": row["health"],
                "heartbeat_age_seconds": max(0, int(row["heartbeat_age_seconds"])),
                "phase": row["phase"],
            }
            for row in rows
        ]
        priority = {"healthy": 0, "degraded": 1, "stale": 2, "offline": 3}
        overall = max((item["health"] for item in services), key=priority.get, default="offline")
        validator = next((item for item in services if item["name"] == "validator"), None)
        eval_age = None
        if current and current["started_at"]:
            started = datetime.fromisoformat(current["started_at"].replace("Z", "+00:00"))
            eval_age = max(0, int((generated - started).total_seconds()))
        weight_delayed = weight_status["state"] in {"retry_pending", "failed"}
        return {
            "overall": overall,
            "validator_phase": validator["phase"] if validator else None,
            "validator_heartbeat_age_seconds": (
                validator["heartbeat_age_seconds"] if validator else None
            ),
            "current_evaluation_age_seconds": eval_age,
            "weight_delayed": weight_delayed,
            "services": services,
        }
