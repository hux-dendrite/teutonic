from __future__ import annotations

import json
import base64
import unittest
from datetime import datetime, timezone

from teutonic.chain import (
    FINALIZATION_EVENT_EXTRINSIC_INDEX,
    INITIALIZATION_EVENT_EXTRINSIC_INDEX,
    FinalizedPosition,
)
from teutonic.config import BucketNames, WorkflowPolicy
from teutonic.schemas import SCHEMA_DIR
from teutonic.credentials import (
    ActivationChallenge,
    activation_message,
    mailbox_object_key,
    registration_id,
)
from teutonic.storage import MINER_PREFIX_SCOPE, create_local_temporary_credentials


HOTKEY = "5" + "A" * 47
NONCE = "AQIDBAUGBwgJCgsMDQ4PEA"


class CredentialContractTests(unittest.TestCase):
    def test_registration_id_is_stable_and_occupancy_specific(self) -> None:
        one = registration_id(
            netuid=3,
            uid=42,
            hotkey=HOTKEY,
            first_seen_finalized_block=100,
            validator_nonce=NONCE,
        )
        same = registration_id(
            netuid=3,
            uid=42,
            hotkey=HOTKEY,
            first_seen_finalized_block=100,
            validator_nonce=NONCE,
        )
        later = registration_id(
            netuid=3,
            uid=42,
            hotkey=HOTKEY,
            first_seen_finalized_block=101,
            validator_nonce=NONCE,
        )
        self.assertEqual(one, same)
        self.assertNotEqual(one, later)
        self.assertEqual(len(one), 64)

    def test_activation_message_binds_every_authority_field(self) -> None:
        registration = "a" * 64
        expiry = datetime(2026, 8, 17, 12, 30, tzinfo=timezone.utc)
        expected = (
            f"activate|v1|3|42|{HOTKEY}|{registration}|{NONCE}|2026-08-17T12:30:00Z"
        )
        self.assertEqual(
            activation_message(
                netuid=3,
                uid=42,
                hotkey=HOTKEY,
                registration_id=registration,
                validator_nonce=NONCE,
                expires_at=expiry,
            ),
            expected,
        )
        challenge = ActivationChallenge(
            registration_id=registration,
            netuid=3,
            uid=42,
            hotkey=HOTKEY,
            validator_nonce=NONCE,
            expires_at=expiry,
        )
        self.assertEqual(challenge.as_dict()["message"], expected)

    def test_mailbox_generations_are_immutable_lexically_ordered_keys(self) -> None:
        registration = "b" * 64
        first = mailbox_object_key(registration, 1)
        second = mailbox_object_key(registration, 2)
        self.assertLess(first, second)
        self.assertTrue(first.endswith("/00000000000000000001.bin"))
        with self.assertRaises(ValueError):
            mailbox_object_key(registration, 0)


class ControlPlaneDecisionTests(unittest.TestCase):
    def test_chain_position_is_a_total_order(self) -> None:
        positions = [
            FinalizedPosition(100, 2, 0),
            FinalizedPosition(100, 1, 4),
            FinalizedPosition(99, 9, 9),
            FinalizedPosition(100, 1, 3),
        ]
        self.assertEqual(
            sorted(positions),
            [
                FinalizedPosition(99, 9, 9),
                FinalizedPosition(100, 1, 3),
                FinalizedPosition(100, 1, 4),
                FinalizedPosition(100, 2, 0),
            ],
        )

    def test_finalization_event_without_extrinsic_has_a_stable_order(self) -> None:
        position = FinalizedPosition.from_event(
            block_number=100,
            extrinsic_index=None,
            event_index=18,
            phase="Finalization",
        )
        self.assertEqual(position.extrinsic_index, FINALIZATION_EVENT_EXTRINSIC_INDEX)
        initialization = FinalizedPosition.from_event(
            block_number=100,
            extrinsic_index=None,
            event_index=1,
            phase="Initialization",
        )
        self.assertEqual(
            initialization.extrinsic_index, INITIALIZATION_EVENT_EXTRINSIC_INDEX
        )
        with self.assertRaises(ValueError):
            FinalizedPosition.from_event(
                block_number=100,
                extrinsic_index=None,
                event_index=18,
                phase="ApplyExtrinsic",
            )

    def test_phase_zero_policy_defaults_are_internally_safe(self) -> None:
        policy = WorkflowPolicy()
        self.assertEqual(policy.max_evaluation_attempts, 3)
        self.assertGreater(policy.evaluation_lease, policy.heartbeat_interval)
        self.assertEqual(policy.temporary_credential_ttl.days, 7)
        self.assertTrue(policy.revoke_upload_access_on_ready)
        self.assertTrue(policy.one_submission_per_hotkey)
        with self.assertRaises(ValueError):
            WorkflowPolicy(revoke_upload_access_on_ready=False)
        with self.assertRaises(ValueError):
            WorkflowPolicy(one_submission_per_hotkey=False)

    def test_three_bucket_boundaries_are_distinct(self) -> None:
        buckets = BucketNames()
        values = {
            buckets.private_models,
            buckets.public_models,
            buckets.dashboard,
        }
        self.assertEqual(len(values), 3)
        self.assertEqual(buckets.mailbox, buckets.dashboard)
        with self.assertRaises(ValueError):
            BucketNames(private_models="same-bucket", public_models="same-bucket")

    def test_all_contract_schema_files_are_valid_json(self) -> None:
        schema_dir = SCHEMA_DIR
        names = {
            "activation-challenge-v1.schema.json",
            "activation-response-v1.schema.json",
            "mailbox-envelope-v1.schema.json",
            "ready-signal-v1.schema.json",
            "dashboard-v1.schema.json",
        }
        for name in names:
            payload = json.loads((schema_dir / name).read_text())
            self.assertEqual(payload["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_r2_temporary_credentials_are_broad_only_inside_registration_prefix(self) -> None:
        credentials = create_local_temporary_credentials(
            endpoint="https://account.r2.cloudflarestorage.com",
            account_id="a" * 32,
            parent_access_key_id="parent-id",
            parent_secret_access_key="parent-secret",
            bucket="teutonic-private-models",
            prefix=f"models/registrations/{'c' * 64}/",
            ttl_seconds=900,
            issued_at_unix=1_700_000_000,
        )
        token = base64.b64decode(credentials.session_token).decode()
        self.assertTrue(token.startswith("jwt/"))
        claims_segment = token.removeprefix("jwt/").split(".")[1]
        claims_segment += "=" * (-len(claims_segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(claims_segment))
        self.assertEqual(claims["exp"] - claims["iat"], 900)
        self.assertEqual(claims["paths"]["prefixPaths"], [f"models/registrations/{'c' * 64}/"])
        self.assertNotIn("actions", claims)
        self.assertEqual(claims["scope"], MINER_PREFIX_SCOPE)

    def test_r2_local_signer_can_omit_action_claim_for_platform_comparison(self) -> None:
        credentials = create_local_temporary_credentials(
            endpoint="https://account.r2.cloudflarestorage.com",
            account_id="a" * 32,
            parent_access_key_id="parent-id",
            parent_secret_access_key="parent-secret",
            bucket="teutonic-private-models",
            prefix=f"models/registrations/{'d' * 64}/",
            ttl_seconds=900,
            issued_at_unix=1_700_000_000,
        )
        token = base64.b64decode(credentials.session_token).decode()
        claims_segment = token.removeprefix("jwt/").split(".")[1]
        claims_segment += "=" * (-len(claims_segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(claims_segment))
        self.assertNotIn("actions", claims)
        self.assertEqual(claims["scope"], "object-read-write")

    def test_dashboard_temporary_credential_is_one_exact_object(self) -> None:
        credentials = create_local_temporary_credentials(
            endpoint="https://account.r2.cloudflarestorage.com",
            account_id="a" * 32,
            parent_access_key_id="dashboard-parent",
            parent_secret_access_key="dashboard-secret",
            bucket="teutonic-dash",
            object_path="dashboard.json",
            ttl_seconds=900,
            issued_at_unix=1_700_000_000,
        )
        token = base64.b64decode(credentials.session_token).decode()
        claims_segment = token.removeprefix("jwt/").split(".")[1]
        claims_segment += "=" * (-len(claims_segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(claims_segment))
        self.assertEqual(claims["paths"]["objectPaths"], ["dashboard.json"])
        self.assertEqual(claims["paths"]["prefixPaths"], [])


if __name__ == "__main__":
    unittest.main()
