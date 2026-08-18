from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

try:
    import psycopg
except ImportError:
    psycopg = None

from teutonic.promotion import (
    ObservedObject,
    PromotionInvariantError,
    PromotionRepository,
    PromotionWorker,
)


DATABASE_URL = os.environ.get("TEUTONIC_TEST_DATABASE_URL")
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


class SimulatedCrash(BaseException):
    pass


class MemoryInspector:
    def __init__(self):
        self.objects: dict[tuple[str, str], ObservedObject] = {}
        self.body_reads = 0

    def inventory(self, bucket, prefix):
        return {
            key[len(prefix) :]: value
            for (stored_bucket, key), value in self.objects.items()
            if stored_bucket == bucket and key.startswith(prefix)
        }


class MemoryRclone:
    def __init__(self, inspector):
        self.inspector = inspector
        self.copy_calls = 0
        self.delete_calls = 0

    def copy(
        self,
        *,
        source_bucket,
        source_prefix,
        destination_bucket,
        destination_prefix,
        heartbeat=None,
    ):
        self.copy_calls += 1
        if heartbeat is not None:
            heartbeat()
        source = list(self.inspector.inventory(source_bucket, source_prefix).values())
        for item in source:
            self.inspector.objects[(destination_bucket, destination_prefix + item.path)] = item

    def delete_source(self, *, bucket, prefix, heartbeat=None):
        self.delete_calls += 1
        if heartbeat is not None:
            heartbeat()
        for stored_bucket, key in list(self.inspector.objects):
            if stored_bucket == bucket and key.startswith(prefix):
                del self.inspector.objects[(stored_bucket, key)]


@unittest.skipUnless(DATABASE_URL and psycopg, "TEUTONIC_TEST_DATABASE_URL and psycopg required")
class ModelPromotionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.connection = psycopg.connect(DATABASE_URL, autocommit=True)
        cls.second_connection = psycopg.connect(DATABASE_URL, autocommit=True)

    @classmethod
    def tearDownClass(cls):
        cls.second_connection.close()
        cls.connection.close()

    def setUp(self):
        self.repository = PromotionRepository(
            self.connection,
            netuid=306,
            chain_generation="test",
            competition="quasar",
            instance_id="promotion-worker-a",
        )
        self.repository.acquire_lock()
        self.inspector = MemoryInspector()
        self.executor = MemoryRclone(self.inspector)
        self._reset_and_seed()

    def tearDown(self):
        self.repository.release_lock()

    def _reset_and_seed(self, *, disposition="winner"):
        self.connection.execute(
            """
            TRUNCATE TABLE
                control_plane.notification_outbox,
                control_plane.weight_publications,
                control_plane.model_promotions,
                control_plane.evaluations,
                control_plane.king_reigns,
                control_plane.competitions,
                control_plane.controller_jobs,
                control_plane.verified_uploads,
                control_plane.upload_files,
                control_plane.uploads,
                control_plane.credential_generations,
                control_plane.r2_parent_tokens,
                control_plane.activation_challenges,
                control_plane.registrations,
                control_plane.metagraph_uid_assignments,
                control_plane.metagraph_snapshots,
                control_plane.chain_cursors
            RESTART IDENTITY CASCADE
            """
        )
        self.inspector.objects.clear()
        self.executor.copy_calls = 0
        self.executor.delete_calls = 0
        registration = "1" * 64
        model_digest = "2" * 64
        manifest_digest = "3" * 64
        prefix = f"models/sha256/{model_digest}/"
        self.connection.execute(
            """
            INSERT INTO control_plane.registrations (
                registration_id, netuid, chain_generation, uid, hotkey,
                first_seen_finalized_block, last_seen_finalized_block,
                ingest_prefix, state
            ) VALUES (%s, 306, 'test', 7, 'miner-hotkey', 100, 101, %s, 'active')
            """,
            (registration, f"ingest/{registration}/"),
        )
        self.connection.execute(
            """
            INSERT INTO control_plane.r2_parent_tokens (
                registration_id, cloudflare_token_id, access_key_id, state, activated_at
            ) VALUES (%s, 'promotion-token', 'promotion-access', 'active', %s)
            """,
            (registration, NOW),
        )
        upload = self.connection.execute(
            """
            INSERT INTO control_plane.uploads (
                registration_id, chain_generation, signalling_hotkey, ready_payload,
                ready_finalized_block, ready_extrinsic_index, ready_event_index,
                manifest_sha256, manifest_signature_verified, model_digest, model_name,
                object_count, total_size_bytes, state, ready_at
            ) VALUES (
                %s, 'test', 'miner-hotkey', 'r2ready:v1', 101, 0, 0,
                %s, true, %s, 'phase6/model', 2, 18, %s, %s
            ) RETURNING upload_id
            """,
            (
                registration,
                manifest_digest,
                model_digest,
                "accepted_pending_promotion" if disposition == "winner" else "rejected",
                NOW,
            ),
        ).fetchone()[0]
        files = (
            ("config.json", 2, "4" * 64),
            ("weights/model.bin", 16, "5" * 64),
        )
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO control_plane.upload_files (
                    upload_id, object_path, size_bytes, sha256, verified_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                [(upload, path, size, digest, NOW) for path, size, digest in files],
            )
        self.connection.execute(
            """
            INSERT INTO control_plane.verified_uploads (
                upload_id, immutable_bucket, immutable_prefix, immutable_version,
                model_digest, manifest_sha256, manifest_size_bytes,
                object_count, total_size_bytes, verified_at
            ) VALUES (%s, 'private-models', %s, 'snapshot-v1', %s, %s, 100, 2, 18, %s)
            """,
            (upload, prefix, model_digest, manifest_digest, NOW),
        )
        competition = self.connection.execute(
            """
            INSERT INTO control_plane.competitions (netuid, chain_generation, name)
            VALUES (306, 'test', 'quasar') RETURNING competition_id
            """
        ).fetchone()[0]
        king = self.connection.execute(
            """
            INSERT INTO control_plane.king_reigns (
                competition_id, reign_number, model_digest, public_bucket, public_prefix,
                hotkey, uid, crowned_at, crowned_finalized_block, operator_provenance
            ) VALUES (%s, 0, %s, 'public-models', %s, 'genesis', 0, %s, 100, 'fixture')
            RETURNING reign_id
            """,
            (competition, "a" * 64, f"models/sha256/{'a' * 64}/", NOW),
        ).fetchone()[0]
        self.connection.execute(
            "UPDATE control_plane.competitions SET current_reign_id = %s WHERE competition_id = %s",
            (king, competition),
        )
        evaluation = self.connection.execute(
            """
            INSERT INTO control_plane.evaluations (
                upload_id, competition_id, attempt_number, claimed_king_reign_id,
                state, policy_version, code_version, dataset_version, tokenizer_version,
                sampling_seed, bootstrap_seed, thresholds, verdict, verdict_summary,
                completed_at
            ) VALUES (
                %s, %s, 1, %s, 'completed', 'policy-v1', 'code-v1', 'data-v1',
                'tokenizer-v1', 1, 2, '{}'::jsonb, %s, '{}'::jsonb, %s
            ) RETURNING evaluation_id
            """,
            (
                upload,
                competition,
                king,
                "accepted" if disposition == "winner" else "rejected",
                NOW,
            ),
        ).fetchone()[0]
        promotion = self.connection.execute(
            """
            INSERT INTO control_plane.model_promotions (
                upload_id, evaluation_id, model_digest, disposition,
                private_bucket, private_prefix, public_bucket, public_prefix,
                state, idempotency_key, next_retry_at,
                expected_object_count, expected_size_bytes
            ) VALUES (
                %s, %s, %s, %s, 'private-models', %s, 'public-models', %s,
                'promotion_pending', %s, %s, 2, 18
            ) RETURNING promotion_id
            """,
            (
                upload,
                evaluation,
                model_digest,
                disposition,
                prefix,
                prefix,
                f"promote-model:{upload}",
                NOW,
            ),
        ).fetchone()[0]
        expected = {
            path: ObservedObject(path, size, digest) for path, size, digest in files
        }
        expected["manifest.json"] = ObservedObject("manifest.json", 100, manifest_digest)
        for path, item in expected.items():
            self.inspector.objects[("private-models", prefix + path)] = item
        self.upload_id = str(upload)
        self.promotion_id = str(promotion)
        self.king_id = str(king)
        self.prefix = prefix

    def _worker(self, *, after_stage=None, on_winner=None):
        return PromotionWorker(
            self.repository,
            self.executor,
            self.inspector,
            clock=lambda: NOW,
            retry_base_delay=timedelta(0),
            after_stage=after_stage,
            on_winner_promoted=on_winner,
        )

    def test_winner_promotes_only_after_verified_copy_and_private_deletion(self):
        before = self.connection.execute(
            """
            SELECT public_model_name, public_model_digest, public_model_reference
              FROM control_plane.dashboard_evaluation_history
             WHERE challenge_id IS NOT NULL
            """
        ).fetchone()
        self.assertEqual(before, (None, None, None))
        callbacks = []
        self.assertTrue(self._worker(on_winner=callbacks.append).run_one(propagate=True))

        promotion = self.connection.execute(
            """
            SELECT state, observed_object_count, observed_size_bytes,
                   public_verified_at IS NOT NULL, private_deleted_at IS NOT NULL
              FROM control_plane.model_promotions WHERE promotion_id = %s
            """,
            (self.promotion_id,),
        ).fetchone()
        self.assertEqual(promotion, ("promoted", 2, 18, True, True))
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(self.inspector.inventory("private-models", self.prefix), {})
        self.assertEqual(len(self.inspector.inventory("public-models", self.prefix)), 3)
        self.assertEqual(self.inspector.body_reads, 0)
        after = self.connection.execute(
            """
            SELECT public_model_name, public_model_digest, public_model_reference
              FROM control_plane.dashboard_evaluation_history
             WHERE challenge_id IS NOT NULL
            """
        ).fetchone()
        # A winning copy is still hidden until the separate crown callback commits
        # a matching king_reigns row. Phase 8 tests the post-crown disclosure.
        self.assertEqual(after, (None, None, None))

    def test_crown_callback_failure_retries_without_recopying_model(self):
        calls = []

        def crown(promotion_id):
            calls.append(promotion_id)
            if len(calls) == 1:
                raise RuntimeError("simulated crown transaction outage")
            self.connection.execute(
                "UPDATE control_plane.uploads SET state = 'accepted' WHERE upload_id = %s",
                (self.upload_id,),
            )

        worker = self._worker(on_winner=crown)
        self.assertTrue(worker.run_one())
        self.assertEqual(
            self.connection.execute("SELECT state FROM control_plane.uploads").fetchone()[0],
            "promoted",
        )
        self.assertTrue(worker.run_one(propagate=True))
        self.assertEqual(calls, [self.promotion_id, self.promotion_id])
        self.assertEqual(self.executor.copy_calls, 1)
        self.assertEqual(self.executor.delete_calls, 1)
        self.assertEqual(
            self.connection.execute("SELECT state FROM control_plane.uploads").fetchone()[0],
            "accepted",
        )

    def test_forced_crash_at_every_stage_reconciles_idempotently(self):
        stages = (
            "copy_completed",
            "public_verification_started",
            "public_copy_verified",
            "private_deletion_started",
            "private_source_deleted",
            "promoted",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                self._reset_and_seed()
                crashed = False

                def crash_here(observed_stage, _claim):
                    nonlocal crashed
                    if observed_stage == stage and not crashed:
                        crashed = True
                        raise SimulatedCrash(stage)

                with self.assertRaises(SimulatedCrash):
                    self._worker(after_stage=crash_here).run_one()
                self.assertTrue(crashed)
                self._worker().run_one(propagate=True)
                state = self.connection.execute(
                    "SELECT state FROM control_plane.model_promotions WHERE promotion_id = %s",
                    (self.promotion_id,),
                ).fetchone()[0]
                self.assertEqual(state, "promoted")
                self.assertEqual(
                    self.inspector.inventory("private-models", self.prefix), {}
                )
                self.assertEqual(
                    len(self.inspector.inventory("public-models", self.prefix)), 3
                )

    def test_digest_collision_fails_without_deleting_source_or_changing_king(self):
        self.inspector.objects[("public-models", self.prefix + "config.json")] = ObservedObject(
            "config.json", 2, "f" * 64
        )
        self._worker().run_one()
        state = self.connection.execute(
            "SELECT state, last_error_code FROM control_plane.model_promotions"
        ).fetchone()
        self.assertEqual(state, ("failed", "PromotionCollisionError"))
        self.assertEqual(len(self.inspector.inventory("private-models", self.prefix)), 3)
        self.assertEqual(
            str(
                self.connection.execute(
                    "SELECT current_reign_id FROM control_plane.competitions"
                ).fetchone()[0]
            ),
            self.king_id,
        )
        self.assertEqual(
            self.connection.execute("SELECT state FROM control_plane.uploads").fetchone()[0],
            "accepted_pending_promotion",
        )

    def test_expired_copy_lease_is_reclaimed_by_a_new_worker(self):
        crashed = False

        def crash_after_copy(stage, _claim):
            nonlocal crashed
            if stage == "copy_completed" and not crashed:
                crashed = True
                raise SimulatedCrash(stage)

        with self.assertRaises(SimulatedCrash):
            self._worker(after_stage=crash_after_copy).run_one()
        self.repository.release_lock()
        replacement = PromotionRepository(
            self.second_connection,
            netuid=306,
            chain_generation="test",
            competition="quasar",
            instance_id="promotion-worker-b",
        )
        replacement.acquire_lock()
        try:
            worker = PromotionWorker(
                replacement,
                self.executor,
                self.inspector,
                clock=lambda: NOW + timedelta(minutes=3),
            )
            self.assertTrue(worker.run_one(propagate=True))
            self.assertEqual(
                self.connection.execute(
                    "SELECT state FROM control_plane.model_promotions"
                ).fetchone()[0],
                "promoted",
            )
        finally:
            replacement.release_lock()
            self.repository.acquire_lock()

    def test_restricted_validator_role_can_complete_promotion(self):
        self.repository.release_lock()
        self.second_connection.execute("SET ROLE teutonic_validator")
        restricted = PromotionRepository(
            self.second_connection,
            netuid=306,
            chain_generation="test",
            competition="quasar",
            instance_id="promotion-worker-restricted",
        )
        restricted.acquire_lock()
        try:
            worker = PromotionWorker(restricted, self.executor, self.inspector, clock=lambda: NOW)
            self.assertTrue(worker.run_one(propagate=True))
            self.assertEqual(
                self.connection.execute(
                    "SELECT state FROM control_plane.model_promotions"
                ).fetchone()[0],
                "promoted",
            )
        finally:
            restricted.release_lock()
            self.second_connection.execute("RESET ROLE")
            self.repository.acquire_lock()

    def test_non_winner_promotion_never_changes_verdict_king_or_weights(self):
        self._reset_and_seed(disposition="non_winner")
        callbacks = []
        self._worker(on_winner=callbacks.append).run_one(propagate=True)
        self.assertEqual(callbacks, [])
        self.assertEqual(
            self.connection.execute("SELECT state FROM control_plane.uploads").fetchone()[0],
            "rejected",
        )
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM control_plane.king_reigns").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM control_plane.weight_publications"
            ).fetchone()[0],
            0,
        )

    def test_invalid_or_mismatched_verdict_is_ineligible_for_promotion(self):
        self.connection.execute(
            "UPDATE control_plane.evaluations SET verdict = 'rejected'"
        )
        with self.assertRaises(PromotionInvariantError):
            self._worker().run_one()
        self.assertEqual(self.executor.copy_calls, 0)
        self.assertEqual(self.executor.delete_calls, 0)
        self.assertEqual(
            len(self.inspector.inventory("private-models", self.prefix)), 3
        )
