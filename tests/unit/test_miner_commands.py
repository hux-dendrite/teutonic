from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from miner.cli import (
    eligibility_from_commitment,
    load_settings,
    save_settings,
    saved_miners,
    select_saved_miner,
)
from miner.common import RegistrationState, read_json, write_json
from miner.upload_model import model_paths, validate_auth
from teutonic.access.contracts import ready_signal_payload
from teutonic.credentials import registration_id


HOTKEY = "5DbsTAQ59aJBeL6fASvVi1xM5Ew2A6DmM5NHc1hzvWtfCNHo"


def registration_state() -> RegistrationState:
    identifier = registration_id(
        netuid=306,
        uid=32,
        hotkey=HOTKEY,
        registration_block=7817347,
        chain_generation="generation-1",
    )
    return RegistrationState(
        network="test",
        netuid=306,
        chain_generation="generation-1",
        wallet_name="cold",
        hotkey_name="hot",
        hotkey=HOTKEY,
        uid=32,
        registration_block=7817347,
        registration_id=identifier,
        observed_finalized_block=7818000,
    )


class MinerCommandTests(unittest.TestCase):
    def test_cli_classifies_finalized_ready_as_consumed(self) -> None:
        state = registration_state()
        self.assertEqual(eligibility_from_commitment(state, "r2activate:v1:anything"), "available")
        ready = ready_signal_payload(state.registration_id, "a" * 64)
        self.assertEqual(eligibility_from_commitment(state, ready), "consumed")

    def test_cli_discovers_and_selects_saved_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = registration_state()
            state_dir = root / state.hotkey
            state.save(state_dir / "registration.json")
            save_settings(root, {"version": 1, "active_hotkey": state.hotkey})

            self.assertEqual(saved_miners(root)[0].registration, state)
            self.assertEqual(select_saved_miner(root).registration, state)
            self.assertEqual(select_saved_miner(root, state.hotkey_name).registration, state)
            self.assertEqual(load_settings(root)["active_hotkey"], state.hotkey)
            self.assertEqual(os.stat(root / "settings.json").st_mode & 0o777, 0o600)

    def test_registration_state_revalidates_deterministic_identity(self) -> None:
        state = registration_state()
        self.assertEqual(RegistrationState.from_mapping(asdict(state)), state)
        corrupt = asdict(state)
        corrupt["uid"] = 33
        with self.assertRaisesRegex(RuntimeError, "non-canonical"):
            RegistrationState.from_mapping(corrupt)

    def test_secret_state_is_written_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "upload-auth.json"
            write_json(path, {"secret": "value"}, secret=True)
            self.assertEqual(read_json(path), {"secret": "value"})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_upload_auth_and_model_inventory_are_strict(self) -> None:
        state = registration_state()
        auth = {
            "registration_id": state.registration_id,
            "hotkey": state.hotkey,
            "netuid": state.netuid,
            "uid": state.uid,
            "chain_generation": state.chain_generation,
            "registration_block": state.registration_block,
            "allowed_prefix": f"models/registrations/{state.registration_id}/",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "r2_endpoint": "https://example.r2.cloudflarestorage.com",
            "private_model_bucket": "models",
            "access_key_id": "access",
            "secret_access_key": "secret",
            "session_token": "session",
        }
        validate_auth(auth, state)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.json").write_text("{}")
            self.assertEqual(model_paths(root), [root / "config.json"])
            (root / "manifest.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "reserved"):
                model_paths(root)


if __name__ == "__main__":
    unittest.main()
