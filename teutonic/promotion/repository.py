from __future__ import annotations

from datetime import datetime, timedelta

from psycopg.rows import dict_row

from teutonic.validator.repository import SchedulerLockUnavailable, scheduler_lock_key

from .contracts import PromotionClaim, PromotionObject, inventory_digest


class PromotionInvariantError(RuntimeError):
    pass


class PromotionLeaseLostError(RuntimeError):
    pass


class PromotionRepository:
    def __init__(
        self,
        connection,
        *,
        netuid: int,
        chain_generation: str,
        competition: str,
        instance_id: str,
    ) -> None:
        self.connection = connection
        self.netuid = netuid
        self.chain_generation = chain_generation
        self.competition = competition
        self.instance_id = instance_id
        self._lock_key = scheduler_lock_key(netuid, chain_generation, competition)
        self._lock_held = False

    def acquire_lock(self) -> None:
        acquired = self.connection.execute(
            "SELECT pg_try_advisory_lock(%s)", (self._lock_key,)
        ).fetchone()[0]
        if not acquired:
            raise SchedulerLockUnavailable("another validator holds the competition lock")
        self._lock_held = True

    def release_lock(self) -> None:
        if self._lock_held:
            self.connection.execute("SELECT pg_advisory_unlock(%s)", (self._lock_key,))
            self._lock_held = False

    def _require_lock(self) -> None:
        if not self._lock_held:
            raise SchedulerLockUnavailable("competition scheduler lock is not held")

    def claim_next(self, *, now: datetime, lease: timedelta) -> PromotionClaim | None:
        self._require_lock()
        with self.connection.transaction(), self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT p.*, vu.manifest_sha256, vu.manifest_size_bytes,
                       e.state AS evaluation_state, e.verdict AS evaluation_verdict,
                       u.state AS upload_state, u.registration_id
                  FROM control_plane.model_promotions p
                  JOIN control_plane.evaluations e ON e.evaluation_id = p.evaluation_id
                  JOIN control_plane.competitions c ON c.competition_id = e.competition_id
                  JOIN control_plane.verified_uploads vu ON vu.upload_id = p.upload_id
                  JOIN control_plane.uploads u ON u.upload_id = p.upload_id
                 WHERE c.netuid = %s AND c.chain_generation = %s AND c.name = %s
                   AND (
                       (p.state IN ('promotion_pending', 'retry_pending')
                        AND COALESCE(p.next_retry_at, '-infinity') <= %s)
                       OR p.state = 'public_copy_verified'
                       OR (p.state IN (
                              'copying_to_public', 'public_copy_verifying',
                              'deleting_private_source'
                          ) AND (p.lease_expires_at <= %s OR p.owner_instance_id = %s))
                   )
                 ORDER BY p.created_at, p.promotion_id
                 FOR UPDATE OF p SKIP LOCKED
                 LIMIT 1
                """,
                (
                    self.netuid,
                    self.chain_generation,
                    self.competition,
                    now,
                    now,
                    self.instance_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            eligible = (
                row["evaluation_state"] == "completed"
                and (
                    (
                        row["disposition"] == "winner"
                        and row["evaluation_verdict"] == "accepted"
                        and row["upload_state"]
                        in {"accepted_pending_promotion", "promoted", "accepted"}
                    )
                    or (
                        row["disposition"] == "non_winner"
                        and row["evaluation_verdict"] == "rejected"
                        and row["upload_state"] == "rejected"
                    )
                )
            )
            if not eligible:
                raise PromotionInvariantError("promotion is not backed by an eligible verdict")
            canonical_private_prefix = f"models/registrations/{row['registration_id']}/"
            canonical_public_prefix = f"models/sha256/{row['model_digest']}/"
            if (
                row["private_prefix"] != canonical_private_prefix
                or row["public_prefix"] != canonical_public_prefix
                or row["private_bucket"] == row["public_bucket"]
            ):
                raise PromotionInvariantError("promotion storage references are not canonical")
            if row["manifest_size_bytes"] is None:
                raise PromotionInvariantError("verified upload omitted manifest size")
            files = cursor.execute(
                """
                SELECT object_path, size_bytes, sha256
                  FROM control_plane.upload_files
                 WHERE upload_id = %s
                 ORDER BY object_path
                """,
                (row["upload_id"],),
            ).fetchall()
            expected = {
                item["object_path"]: PromotionObject(
                    item["object_path"], int(item["size_bytes"]), str(item["sha256"])
                )
                for item in files
            }
            expected["manifest.json"] = PromotionObject(
                "manifest.json", int(row["manifest_size_bytes"]), str(row["manifest_sha256"])
            )
            if len(files) != row["expected_object_count"]:
                raise PromotionInvariantError("promotion count differs from immutable manifest")
            if sum(item.size for path, item in expected.items() if path != "manifest.json") != row[
                "expected_size_bytes"
            ]:
                raise PromotionInvariantError("promotion size differs from immutable manifest")
            expected_digest = inventory_digest(expected)
            if row["expected_inventory_sha256"] not in {None, expected_digest}:
                raise PromotionInvariantError(
                    "promotion expected inventory changed across attempts"
                )
            if row["observed_inventory_sha256"] not in {None, expected_digest}:
                raise PromotionInvariantError(
                    "promotion observed inventory conflicts with manifest"
                )
            target_state = (
                "deleting_private_source"
                if row["state"] == "public_copy_verified"
                else "copying_to_public"
                if row["state"] in {"promotion_pending", "retry_pending"}
                else row["state"]
            )
            cursor.execute(
                """
                UPDATE control_plane.model_promotions
                   SET state = %s, owner_instance_id = %s, lease_expires_at = %s,
                       attempt_count = attempt_count + 1, next_retry_at = NULL,
                       expected_inventory_sha256 = %s, updated_at = clock_timestamp()
                 WHERE promotion_id = %s
                """,
                (
                    target_state,
                    self.instance_id,
                    now + lease,
                    expected_digest,
                    row["promotion_id"],
                ),
            )
        return PromotionClaim(
            promotion_id=str(row["promotion_id"]),
            upload_id=str(row["upload_id"]),
            evaluation_id=str(row["evaluation_id"]),
            disposition=row["disposition"],
            model_digest=str(row["model_digest"]),
            private_bucket=row["private_bucket"],
            private_prefix=row["private_prefix"],
            public_bucket=row["public_bucket"],
            public_prefix=row["public_prefix"],
            state=target_state,
            attempt_count=int(row["attempt_count"]) + 1,
            expected=expected,
        )

    def transition(
        self,
        claim: PromotionClaim,
        *,
        expected_states: tuple[str, ...],
        target_state: str,
        now: datetime,
        lease: timedelta,
        observed_inventory_sha256: str | None = None,
    ) -> None:
        self._require_lock()
        active = target_state in {
            "copying_to_public",
            "public_copy_verifying",
            "deleting_private_source",
        }
        with self.connection.transaction():
            row = self.connection.execute(
                """
                UPDATE control_plane.model_promotions
                   SET state = %s, lease_expires_at = %s,
                       observed_inventory_sha256 = COALESCE(%s, observed_inventory_sha256),
                       public_verified_at = CASE
                           WHEN %s = 'public_copy_verified' THEN COALESCE(public_verified_at, %s)
                           ELSE public_verified_at
                       END,
                       updated_at = clock_timestamp()
                 WHERE promotion_id = %s AND owner_instance_id = %s AND state = ANY(%s)
                 RETURNING promotion_id
                """,
                (
                    target_state,
                    now + lease if active else None,
                    observed_inventory_sha256,
                    target_state,
                    now,
                    claim.promotion_id,
                    self.instance_id,
                    list(expected_states),
                ),
            ).fetchone()
            if row is None:
                raise PromotionLeaseLostError("promotion transition lost its lease")

    def heartbeat(self, claim: PromotionClaim, *, now: datetime, lease: timedelta) -> None:
        self._require_lock()
        with self.connection.transaction():
            row = self.connection.execute(
                """
                UPDATE control_plane.model_promotions
                   SET lease_expires_at = %s, updated_at = clock_timestamp()
                 WHERE promotion_id = %s AND owner_instance_id = %s
                   AND state IN (
                       'copying_to_public', 'public_copy_verifying',
                       'deleting_private_source'
                   )
                 RETURNING promotion_id
                """,
                (now + lease, claim.promotion_id, self.instance_id),
            ).fetchone()
            if row is None:
                raise PromotionLeaseLostError("promotion heartbeat lost its lease")

    def mark_promoted(
        self,
        claim: PromotionClaim,
        *,
        now: datetime,
        observed_object_count: int,
        observed_size_bytes: int,
        observed_inventory_sha256: str,
    ) -> None:
        self._require_lock()
        with self.connection.transaction():
            row = self.connection.execute(
                """
                UPDATE control_plane.model_promotions
                   SET state = 'promoted', lease_expires_at = NULL,
                       observed_object_count = %s, observed_size_bytes = %s,
                       observed_inventory_sha256 = %s,
                       public_verified_at = COALESCE(public_verified_at, %s),
                       private_deleted_at = COALESCE(private_deleted_at, %s),
                       promoted_at = COALESCE(promoted_at, %s),
                       updated_at = clock_timestamp()
                 WHERE promotion_id = %s AND owner_instance_id = %s
                   AND state = 'deleting_private_source'
                 RETURNING upload_id
                """,
                (
                    observed_object_count,
                    observed_size_bytes,
                    observed_inventory_sha256,
                    now,
                    now,
                    now,
                    claim.promotion_id,
                    self.instance_id,
                ),
            ).fetchone()
            if row is None:
                raise PromotionLeaseLostError("promotion completion lost its lease")
            if claim.disposition == "winner":
                self.connection.execute(
                    """
                    UPDATE control_plane.uploads
                       SET state = 'promoted', updated_at = clock_timestamp()
                     WHERE upload_id = %s AND state = 'accepted_pending_promotion'
                    """,
                    (row[0],),
                )

    def fail_or_retry(
        self,
        claim: PromotionClaim,
        *,
        now: datetime,
        error_code: str,
        retry_delay: timedelta,
        max_attempts: int,
        terminal: bool,
    ) -> str:
        self._require_lock()
        target = "failed" if terminal or claim.attempt_count >= max_attempts else "retry_pending"
        with self.connection.transaction():
            row = self.connection.execute(
                """
                UPDATE control_plane.model_promotions
                   SET state = %s, owner_instance_id = NULL, lease_expires_at = NULL,
                       next_retry_at = %s, last_error_code = %s,
                       updated_at = clock_timestamp()
                 WHERE promotion_id = %s AND owner_instance_id = %s
                   AND state IN (
                       'copying_to_public', 'public_copy_verifying',
                       'public_copy_verified', 'deleting_private_source'
                   )
                 RETURNING promotion_id
                """,
                (
                    target,
                    None if target == "failed" else now + retry_delay,
                    error_code,
                    claim.promotion_id,
                    self.instance_id,
                ),
            ).fetchone()
            if row is None:
                raise PromotionLeaseLostError("promotion failure lost its lease")
        return target

    def pending_winner_crowns(self) -> tuple[str, ...]:
        self._require_lock()
        rows = self.connection.execute(
            """
            SELECT p.promotion_id
              FROM control_plane.model_promotions p
              JOIN control_plane.evaluations e ON e.evaluation_id = p.evaluation_id
              JOIN control_plane.competitions c ON c.competition_id = e.competition_id
              JOIN control_plane.uploads u ON u.upload_id = p.upload_id
             WHERE c.netuid = %s AND c.chain_generation = %s AND c.name = %s
               AND p.state = 'promoted' AND p.disposition = 'winner'
               AND e.state = 'completed' AND e.verdict = 'accepted'
               AND u.state = 'promoted'
               AND NOT EXISTS (
                   SELECT 1 FROM control_plane.king_reigns r
                    WHERE r.causing_evaluation_id = e.evaluation_id
               )
             ORDER BY p.promoted_at, p.promotion_id
            """,
            (self.netuid, self.chain_generation, self.competition),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)
