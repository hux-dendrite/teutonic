from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

try:
    import psycopg
    from psycopg import errors
except ImportError:  # Local unit-only runs do not require the PostgreSQL extra.
    psycopg = None
    errors = None


DATABASE_URL = os.environ.get("TEUTONIC_TEST_DATABASE_URL")
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


@unittest.skipUnless(DATABASE_URL and psycopg, "TEUTONIC_TEST_DATABASE_URL and psycopg required")
class PostgresFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.connection = psycopg.connect(DATABASE_URL, autocommit=True)
        database_name = cls.connection.execute("SELECT current_database()").fetchone()[0]
        if database_name != "teutonic_test":
            raise RuntimeError(f"refusing integration tests against non-test database {database_name!r}")
        cls.connection.execute(
            """
            TRUNCATE TABLE
                control_plane.service_instances,
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
                control_plane.registrations,
                control_plane.metagraph_uid_assignments,
                control_plane.metagraph_snapshots,
                control_plane.chain_cursors
            RESTART IDENTITY CASCADE
            """
        )
        cls.seed = cls._seed_control_plane()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    @classmethod
    def _seed_control_plane(cls) -> dict[str, object]:
        connection = cls.connection
        registration_id = "a" * 64
        snapshot_id = connection.execute(
            """
            INSERT INTO control_plane.metagraph_snapshots (
                netuid, chain_generation, finalized_block, finalized_block_hash,
                snapshot_checksum, uid_count, is_complete, observed_at
            ) VALUES (3, 'test-generation', 100, '0x100', %s, 1, true, %s)
            RETURNING snapshot_id
            """,
            ("1" * 64, NOW),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO control_plane.metagraph_uid_assignments
                (snapshot_id, uid, hotkey, coldkey, registration_block)
            VALUES (%s, 42, %s, %s, 100)
            """,
            (snapshot_id, "5" + "A" * 47, "5" + "C" * 47),
        )
        connection.execute(
            """
            INSERT INTO control_plane.registrations (
                registration_id, netuid, chain_generation, uid, hotkey,
                first_seen_finalized_block, last_seen_finalized_block, model_prefix, state
            ) VALUES (%s, 3, 'test-generation', 42, %s, 100, 100, %s, 'active')
            """,
            (registration_id, "5" + "A" * 47, f"models/registrations/{registration_id}/"),
        )
        connection.execute(
            """
            INSERT INTO control_plane.r2_parent_tokens (
                registration_id, cloudflare_token_id, access_key_id, state, activated_at
            ) VALUES (%s, 'cf-parent-test', 'r2-parent-test', 'active', %s)
            """,
            (registration_id, NOW),
        )
        connection.execute(
            """
            INSERT INTO control_plane.credential_generations (
                registration_id, generation, issued_at, expires_at, bucket_name,
                allowed_prefix, allowed_actions, mailbox_object_key,
                ciphertext_sha256, state, published_at
            ) VALUES (
                %s, 1, %s, %s + interval '7 days', 'private-models', %s, NULL,
                %s, %s, 'published', %s
            )
            """,
            (
                registration_id,
                NOW,
                NOW,
                f"models/registrations/{registration_id}/",
                f"mailbox/v1/{registration_id}/generations/{1:020d}.bin",
                "9" * 64,
                NOW,
            ),
        )
        upload_id = connection.execute(
            """
            INSERT INTO control_plane.uploads (
                registration_id, chain_generation, signalling_hotkey, ready_payload,
                ready_finalized_block, ready_extrinsic_index, ready_event_index,
                manifest_sha256, manifest_signature_verified, model_digest, model_name,
                object_count, total_size_bytes, state, ready_at
            ) VALUES (
                %s, 'test-generation', %s, %s, 101, 2, 3,
                %s, true, %s, 'test/model', 2, 1024, 'ready_for_evaluation', %s
            ) RETURNING upload_id
            """,
            (
                registration_id,
                "5" + "A" * 47,
                f"r2ready:v1|{registration_id}|{'2' * 64}",
                "2" * 64,
                "3" * 64,
                NOW,
            ),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO control_plane.verified_uploads (
                upload_id, immutable_bucket, immutable_prefix, model_digest,
                manifest_sha256, object_count, total_size_bytes, verified_at
            ) VALUES (%s, 'private-models', %s, %s, %s, 2, 1024, %s)
            """,
            (
                upload_id,
                f"models/registrations/{registration_id}/",
                "3" * 64,
                "2" * 64,
                NOW,
            ),
        )
        competition_id = connection.execute(
            """
            INSERT INTO control_plane.competitions (netuid, chain_generation, name)
            VALUES (3, 'test-generation', 'quasar')
            RETURNING competition_id
            """
        ).fetchone()[0]
        reign_id = connection.execute(
            """
            INSERT INTO control_plane.king_reigns (
                competition_id, reign_number, model_digest, public_bucket, public_prefix,
                hotkey, uid, crowned_at, crowned_finalized_block, operator_provenance
            ) VALUES (%s, 0, %s, 'public-models', %s, %s, 0, %s, 100, 'integration-test')
            RETURNING reign_id
            """,
            (
                competition_id,
                "4" * 64,
                f"models/sha256/{'4' * 64}/",
                "5" + "G" * 47,
                NOW,
            ),
        ).fetchone()[0]
        connection.execute(
            "UPDATE control_plane.competitions SET current_reign_id = %s WHERE competition_id = %s",
            (reign_id, competition_id),
        )
        return {
            "registration_id": registration_id,
            "upload_id": upload_id,
            "competition_id": competition_id,
            "reign_id": reign_id,
        }

    def evaluation_values(self, upload_id) -> tuple[object, ...]:
        return (
            upload_id,
            self.seed["competition_id"],
            self.seed["reign_id"],
            '{"threshold": 0.01}',
        )

    def insert_evaluation(self, upload_id, attempt: int = 1) -> None:
        self.connection.execute(
            """
            INSERT INTO control_plane.evaluations (
                upload_id, competition_id, attempt_number, claimed_king_reign_id,
                state, owner_instance_id, lease_expires_at, heartbeat_at,
                policy_version, code_version, dataset_version, tokenizer_version,
                sampling_seed, bootstrap_seed, thresholds
            ) VALUES (%s, %s, %s, %s, 'claimed', 'validator-test', %s + interval '2 minutes',
                      %s, 'policy-v1', 'code-v1', 'dataset-v1', 'tokenizer-v1', 7, 8, %s::jsonb)
            """,
            (
                upload_id,
                self.seed["competition_id"],
                attempt,
                self.seed["reign_id"],
                NOW,
                NOW,
                '{"threshold": 0.01}',
            ),
        )

    def test_fresh_start_schema_has_no_migration_ledger(self) -> None:
        self.assertIsNone(
            self.connection.execute(
                "SELECT to_regclass('public.schema_migrations')"
            ).fetchone()[0]
        )

    def test_phase_one_schema_inventory_is_complete(self) -> None:
        tables = self.connection.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'control_plane'
               AND table_type = 'BASE TABLE'
             ORDER BY table_name
            """
        ).fetchall()
        self.assertEqual(
            [row[0] for row in tables],
            [
                "chain_cursors",
                "competitions",
                "controller_jobs",
                "credential_generations",
                "evaluations",
                "king_reigns",
                "metagraph_snapshots",
                "metagraph_uid_assignments",
                "model_promotions",
                "notification_outbox",
                "public_state_revision",
                "r2_parent_tokens",
                "registrations",
                "service_instances",
                "upload_files",
                "uploads",
                "verified_uploads",
                "weight_publications",
                "weight_submission_attempts",
            ],
        )

    def test_public_state_revision_is_monotonic(self) -> None:
        def revision() -> int:
            return self.connection.execute(
                "SELECT revision FROM control_plane.public_state_revision WHERE singleton"
            ).fetchone()[0]

        before = revision()
        self.connection.execute(
            """
            INSERT INTO control_plane.service_instances (
                service_name, instance_id, software_version, state, phase,
                started_at, heartbeat_at
            ) VALUES ('revision-test', 'instance-1', 'test', 'active', 'testing', %s, %s)
            """,
            (NOW, NOW),
        )
        after_insert = revision()
        self.connection.execute(
            """
            UPDATE control_plane.service_instances SET phase = 'updated'
             WHERE service_name = 'revision-test' AND instance_id = 'instance-1'
            """
        )
        after_update = revision()
        self.connection.execute(
            """
            DELETE FROM control_plane.service_instances
             WHERE service_name = 'revision-test' AND instance_id = 'instance-1'
            """
        )
        after_delete = revision()
        self.assertLess(before, after_insert)
        self.assertLess(after_insert, after_update)
        self.assertLess(after_update, after_delete)

    def test_submission_atomically_revokes_its_one_shot_upload_authority(self) -> None:
        token = self.connection.execute(
            """
            SELECT state, revocation_reason, revocation_requested_at
              FROM control_plane.r2_parent_tokens
             WHERE registration_id = %s
            """,
            (self.seed["registration_id"],),
        ).fetchone()
        self.assertEqual(token[0], "pending_revoke")
        self.assertEqual(token[1], "model_submitted")
        self.assertIsNotNone(token[2])

        generation_state = self.connection.execute(
            """
            SELECT state FROM control_plane.credential_generations
             WHERE registration_id = %s AND generation = 1
            """,
            (self.seed["registration_id"],),
        ).fetchone()[0]
        self.assertEqual(generation_state, "superseded")

        jobs = self.connection.execute(
            """
            SELECT operation, state, upload_id
             FROM control_plane.controller_jobs
             WHERE registration_id = %s
               AND starts_with(idempotency_key, 'revoke-parent-after-submit:')
            """,
            (self.seed["registration_id"],),
        ).fetchall()
        self.assertEqual(jobs, [("revoke_parent_token", "pending", self.seed["upload_id"])])

    def test_submission_revocation_job_is_idempotent(self) -> None:
        self.connection.execute(
            """
            UPDATE control_plane.uploads SET ready_at = ready_at, updated_at = %s
             WHERE upload_id = %s
            """,
            (NOW, self.seed["upload_id"]),
        )
        count = self.connection.execute(
            """
            SELECT count(*) FROM control_plane.controller_jobs
             WHERE registration_id = %s
               AND starts_with(idempotency_key, 'revoke-parent-after-submit:')
            """,
            (self.seed["registration_id"],),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_submitted_hotkey_cannot_receive_authority_or_submit_again(self) -> None:
        registration_id = "c" * 64
        hotkey = "5" + "A" * 47
        self.connection.execute(
            """
            INSERT INTO control_plane.registrations (
                registration_id, netuid, chain_generation, uid, hotkey,
                first_seen_finalized_block, last_seen_finalized_block, model_prefix, state
            ) VALUES (%s, 3, 'later-generation', 42, %s, 200, 200, %s, 'active')
            """,
            (registration_id, hotkey, f"models/registrations/{registration_id}/"),
        )

        with self.assertRaises(errors.CheckViolation):
            self.connection.execute(
                """
                INSERT INTO control_plane.r2_parent_tokens (
                    registration_id, cloudflare_token_id, access_key_id, state, activated_at
                ) VALUES (%s, 'cf-parent-reissue', 'r2-parent-reissue', 'active', %s)
                """,
                (registration_id, NOW),
            )

        with self.assertRaises(errors.UniqueViolation):
            self.connection.execute(
                """
                INSERT INTO control_plane.uploads (
                    registration_id, chain_generation, signalling_hotkey, ready_payload,
                    ready_finalized_block, ready_extrinsic_index, ready_event_index,
                    manifest_sha256, state, ready_at
                ) VALUES (%s, 'later-generation', %s, 'second-submission', 201, 0, 0,
                          %s, 'ready_signaled', %s)
                """,
                (registration_id, hotkey, "7" * 64, NOW),
            )

    def test_upload_authority_cannot_be_reissued_after_submission(self) -> None:
        registration_id = self.seed["registration_id"]
        with self.assertRaises(errors.CheckViolation):
            self.connection.execute(
                """
                INSERT INTO control_plane.credential_generations (
                    registration_id, generation, issued_at, expires_at, bucket_name,
                    allowed_prefix, allowed_actions, mailbox_object_key,
                    ciphertext_sha256, state, published_at
                ) VALUES (
                    %s, 2, %s, %s + interval '7 days', 'private-models', %s, NULL,
                    %s, %s, 'published', %s
                )
                """,
                (
                    registration_id,
                    NOW,
                    NOW,
                    f"models/registrations/{registration_id}/",
                    f"mailbox/v1/{registration_id}/generations/{2:020d}.bin",
                    "8" * 64,
                    NOW,
                ),
            )
        with self.assertRaises(errors.CheckViolation):
            self.connection.execute(
                """
                UPDATE control_plane.r2_parent_tokens
                   SET state = 'active'
                 WHERE registration_id = %s
                """,
                (registration_id,),
            )

    def test_only_one_active_registration_can_occupy_a_uid(self) -> None:
        duplicate = "b" * 64
        with self.assertRaises(errors.UniqueViolation):
            self.connection.execute(
                """
                INSERT INTO control_plane.registrations (
                    registration_id, netuid, chain_generation, uid, hotkey,
                    first_seen_finalized_block, last_seen_finalized_block, model_prefix, state
                ) VALUES (%s, 3, 'test-generation', 42, %s, 100, 100, %s, 'active')
                """,
                (duplicate, "5" + "B" * 47, f"models/registrations/{duplicate}/"),
            )

    def test_ready_chain_position_is_unique(self) -> None:
        with self.assertRaises(errors.UniqueViolation):
            self.connection.execute(
                """
                INSERT INTO control_plane.uploads (
                    registration_id, chain_generation, signalling_hotkey, ready_payload,
                    ready_finalized_block, ready_extrinsic_index, ready_event_index,
                    manifest_sha256, state, ready_at
                ) VALUES (%s, 'test-generation', %s, 'duplicate', 101, 2, 3, %s,
                          'ready_signaled', %s)
                """,
                (self.seed["registration_id"], "5" + "A" * 47, "5" * 64, NOW),
            )

    def test_credential_generation_is_unique_per_registration(self) -> None:
        registration_id = self.seed["registration_id"]
        with self.assertRaises(errors.UniqueViolation):
            self.connection.execute(
                """
                INSERT INTO control_plane.credential_generations (
                    registration_id, generation, issued_at, expires_at, bucket_name,
                    allowed_prefix, allowed_actions, mailbox_object_key,
                    ciphertext_sha256, state
                ) VALUES (
                    %s, 1, %s, %s + interval '1 hour', 'private-models', %s, NULL,
                    %s, %s, 'superseded'
                )
                """,
                (
                    registration_id,
                    NOW,
                    NOW,
                    f"models/registrations/{registration_id}/",
                    f"mailbox/v1/{registration_id}/generations/{1:020d}.bin",
                    "6" * 64,
                ),
            )

    def test_manifest_commitment_is_consumed_once_per_registration(self) -> None:
        with self.assertRaises(errors.UniqueViolation):
            self.connection.execute(
                """
                INSERT INTO control_plane.uploads (
                    registration_id, chain_generation, manifest_sha256, state
                ) VALUES (%s, 'test-generation', %s, 'uploading')
                """,
                (self.seed["registration_id"], "2" * 64),
            )

    def test_immutable_model_digest_destination_is_unique(self) -> None:
        upload_id = self.connection.execute(
            """
            INSERT INTO control_plane.uploads (registration_id, chain_generation, state)
            VALUES (%s, 'test-generation', 'uploading') RETURNING upload_id
            """,
            (self.seed["registration_id"],),
        ).fetchone()[0]
        with self.assertRaises(errors.UniqueViolation):
            self.connection.execute(
                """
                INSERT INTO control_plane.verified_uploads (
                    upload_id, immutable_bucket, immutable_prefix, model_digest,
                    manifest_sha256, object_count, total_size_bytes, verified_at
                ) VALUES (%s, 'private-models', %s, %s, %s, 1, 1, %s)
                """,
                (
                    upload_id,
                    f"models/registrations/{self.seed['registration_id']}/",
                    "3" * 64,
                    "6" * 64,
                    NOW,
                ),
            )

    def test_reign_number_and_current_reign_are_unique_per_competition(self) -> None:
        with self.assertRaises(errors.UniqueViolation):
            self.connection.execute(
                """
                INSERT INTO control_plane.king_reigns (
                    competition_id, reign_number, model_digest, public_bucket,
                    public_prefix, hotkey, uid, crowned_at, crowned_finalized_block,
                    operator_provenance
                ) VALUES (%s, 0, %s, 'public-models', %s, %s, 1, %s, 101, 'duplicate')
                """,
                (
                    self.seed["competition_id"],
                    "5" * 64,
                    f"models/sha256/{'5' * 64}/",
                    "5" + "H" * 47,
                    NOW,
                ),
            )
        with self.assertRaises(errors.UniqueViolation):
            self.connection.execute(
                """
                INSERT INTO control_plane.king_reigns (
                    competition_id, reign_number, model_digest, public_bucket,
                    public_prefix, hotkey, uid, crowned_at, crowned_finalized_block
                ) VALUES (%s, 1, %s, 'public-models', %s, %s, 1, %s, 101)
                """,
                (
                    self.seed["competition_id"],
                    "5" * 64,
                    f"models/sha256/{'5' * 64}/",
                    "5" + "H" * 47,
                    NOW,
                ),
            )

    def test_every_external_job_table_has_a_unique_idempotency_key(self) -> None:
        rows = self.connection.execute(
            """
            SELECT table_name
              FROM information_schema.constraint_column_usage
             WHERE table_schema = 'control_plane'
               AND column_name = 'idempotency_key'
               AND constraint_name IN (
                   SELECT constraint_name
                     FROM information_schema.table_constraints
                    WHERE constraint_schema = 'control_plane'
                      AND constraint_type = 'UNIQUE'
               )
             ORDER BY table_name
            """
        ).fetchall()
        self.assertEqual(
            [row[0] for row in rows],
            [
                "controller_jobs",
                "model_promotions",
                "notification_outbox",
                "weight_publications",
                "weight_submission_attempts",
            ],
        )

    def test_unverified_upload_cannot_receive_an_evaluation(self) -> None:
        unverified_upload = self.connection.execute(
            """
            INSERT INTO control_plane.uploads (registration_id, chain_generation, state)
            VALUES (%s, 'test-generation', 'uploading') RETURNING upload_id
            """,
            (self.seed["registration_id"],),
        ).fetchone()[0]
        with self.assertRaises(errors.ForeignKeyViolation):
            self.insert_evaluation(unverified_upload, attempt=99)

    def test_one_successful_evaluation_per_upload_policy_and_king(self) -> None:
        self.insert_evaluation(self.seed["upload_id"], attempt=1)
        self.connection.execute(
            """
            UPDATE control_plane.evaluations
               SET state = 'completed', lease_expires_at = NULL, verdict = 'rejected',
                   verdict_summary = '{}'::jsonb, completed_at = %s
             WHERE upload_id = %s AND attempt_number = 1
            """,
            (NOW, self.seed["upload_id"]),
        )
        self.insert_evaluation(self.seed["upload_id"], attempt=2)
        with self.assertRaises(errors.UniqueViolation):
            self.connection.execute(
                """
                UPDATE control_plane.evaluations
                   SET state = 'completed', lease_expires_at = NULL, verdict = 'rejected',
                       verdict_summary = '{}'::jsonb, completed_at = %s
                 WHERE upload_id = %s AND attempt_number = 2
                """,
                (NOW, self.seed["upload_id"]),
            )

    def role_connection(self, role: str):
        connection = psycopg.connect(DATABASE_URL, autocommit=True)
        connection.execute(f"SET ROLE {role}")
        return connection

    def test_dashboard_role_is_view_only(self) -> None:
        with self.role_connection("teutonic_dashboard_view") as connection:
            connection.execute("SELECT * FROM control_plane.dashboard_stats").fetchall()
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM control_plane.registrations").fetchall()
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM control_plane.r2_parent_tokens").fetchall()

    def test_validator_cannot_read_parent_tokens(self) -> None:
        with self.role_connection("teutonic_validator") as connection:
            connection.execute("SELECT * FROM control_plane.verified_uploads").fetchall()
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM control_plane.r2_parent_tokens").fetchall()

    def test_weight_publisher_cannot_read_models_or_credentials(self) -> None:
        with self.role_connection("teutonic_weight_publisher") as connection:
            connection.execute("SELECT * FROM control_plane.weight_publications").fetchall()
            connection.execute("SELECT * FROM control_plane.weight_submission_attempts").fetchall()
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM control_plane.verified_uploads").fetchall()
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM control_plane.r2_parent_tokens").fetchall()

    def test_access_controller_cannot_write_evaluations(self) -> None:
        with self.role_connection("teutonic_access_controller") as connection:
            connection.execute("SELECT * FROM control_plane.registrations").fetchall()
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM control_plane.evaluations").fetchall()

    def test_auditor_can_read_every_table_but_cannot_mutate(self) -> None:
        with self.role_connection("teutonic_auditor") as connection:
            connection.execute("SELECT * FROM control_plane.r2_parent_tokens").fetchall()
            connection.execute("SELECT * FROM control_plane.evaluations").fetchall()
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute(
                    "UPDATE control_plane.registrations SET updated_at = %s WHERE false",
                    (NOW,),
                )

    def test_schema_owner_owns_schema_and_can_read_secret_tables(self) -> None:
        owner = self.connection.execute(
            """
            SELECT pg_get_userbyid(nspowner)
              FROM pg_namespace
             WHERE nspname = 'control_plane'
            """
        ).fetchone()[0]
        self.assertEqual(owner, "teutonic_schema_owner")
        with self.role_connection("teutonic_schema_owner") as connection:
            connection.execute("SELECT * FROM control_plane.r2_parent_tokens").fetchall()

    def test_public_cannot_access_control_plane_objects(self) -> None:
        public_functions = self.connection.execute(
            """
            SELECT p.proname
              FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'control_plane'
               AND has_function_privilege('public', p.oid, 'EXECUTE')
            """
        ).fetchall()
        self.assertEqual(public_functions, [])
        self.assertFalse(
            self.connection.execute(
                "SELECT has_schema_privilege('public', 'control_plane', 'USAGE')"
            ).fetchone()[0]
        )

    def test_validator_can_write_evaluations_but_not_upload_evidence(self) -> None:
        with self.role_connection("teutonic_validator") as connection:
            evaluation_id = connection.execute(
                """
                INSERT INTO control_plane.evaluations (
                    upload_id, competition_id, attempt_number, claimed_king_reign_id,
                    state, policy_version, code_version, dataset_version, tokenizer_version,
                    sampling_seed, bootstrap_seed, thresholds
                ) VALUES (%s, %s, 500, %s, 'terminal_failure', 'role-policy', 'role-code',
                          'role-data', 'role-tokenizer', 1, 2, '{}'::jsonb)
                RETURNING evaluation_id
                """,
                (
                    self.seed["upload_id"],
                    self.seed["competition_id"],
                    self.seed["reign_id"],
                ),
            ).fetchone()[0]
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute(
                    "UPDATE control_plane.uploads SET manifest_sha256 = %s WHERE upload_id = %s",
                    ("0" * 64, self.seed["upload_id"]),
                )
        self.connection.execute(
            "DELETE FROM control_plane.evaluations WHERE evaluation_id = %s",
            (evaluation_id,),
        )

    def test_weight_publisher_can_advance_only_weight_jobs(self) -> None:
        weight_id = self.connection.execute(
            """
            INSERT INTO control_plane.weight_publications (
                competition_id, source_reign_id, policy_version, policy_hotkeys,
                target_hotkeys, target_uids, normalized_weights, payload_sha256,
                mapping_finalized_block, idempotency_key, state
            ) VALUES (%s, %s, 'role-policy', ARRAY['seed-hotkey'],
                      ARRAY['seed-hotkey'], ARRAY[0],
                      ARRAY[1.0], %s, 0, 'weight-role-test', 'requested')
            RETURNING weight_publication_id
            """,
            (self.seed["competition_id"], self.seed["reign_id"], "7" * 64),
        ).fetchone()[0]
        with self.role_connection("teutonic_weight_publisher") as connection:
            connection.execute(
                """
                UPDATE control_plane.weight_publications
                   SET state = 'failed', last_error_code = 'role-test'
                 WHERE weight_publication_id = %s
                """,
                (weight_id,),
            )
            with self.assertRaises(errors.InsufficientPrivilege):
                connection.execute(
                    "UPDATE control_plane.registrations SET updated_at = %s WHERE false",
                    (NOW,),
                )
        self.connection.execute(
            "DELETE FROM control_plane.weight_publications WHERE weight_publication_id = %s",
            (weight_id,),
        )


if __name__ == "__main__":
    unittest.main()
