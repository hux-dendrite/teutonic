from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from nacl.signing import SigningKey

from teutonic.access import FinalizedChainScanner, encode_ss58_public_key, ready_signal_payload
from teutonic.credentials import activation_signal_payload


NETUID = 306
REGISTRATION = "a" * 64
MANIFEST = "b" * 64


def commitment(hotkey: str, payload: str) -> dict:
    raw = payload.encode().hex()
    return {
        "address": hotkey,
        "call": {
            "call_module": "Commitments",
            "call_function": "set_commitment",
            "call_args": [
                {"name": "netuid", "value": NETUID},
                {"name": "info", "value": {"fields": [{f"Raw{len(payload)}": f"0x{raw}"}]}},
            ],
        },
    }


def event(hotkey: str, extrinsic_index: int) -> dict:
    return {
        "module_id": "Commitments",
        "event_id": "Commitment",
        "extrinsic_idx": extrinsic_index,
        "attributes": {"netuid": NETUID, "who": hotkey},
    }


class FakeSubstrate:
    def __init__(self, blocks: dict[int, tuple[dict, list[dict]]]) -> None:
        self.blocks = blocks

    def get_chain_finalised_head(self) -> int:
        return max(self.blocks)

    def get_block_number(self, block_hash: int) -> int:
        return int(block_hash)

    def get_block_hash(self, block_number: int) -> int:
        return block_number

    def get_block(self, *, block_hash: int) -> dict:
        return self.blocks[block_hash][0]

    def get_events(self, block_hash: int) -> list[dict]:
        return self.blocks[block_hash][1]


class FakeSubtensor:
    def __init__(self, hotkey: str, blocks: dict[int, tuple[dict, list[dict]]]) -> None:
        self.substrate = FakeSubstrate(blocks)
        self.hotkey = hotkey

    def metagraph(self, netuid: int, *, block: int, lite: bool):
        assert netuid == NETUID and lite
        return SimpleNamespace(
            uids=[42],
            hotkeys=[self.hotkey],
            coldkeys=[self.hotkey],
            block_at_registration=[90],
        )


class FakeRepository:
    def __init__(self) -> None:
        self.snapshots: list[int] = []
        self.activations = []
        self.ready = []

    def last_finalized_block(self, *, netuid: int, chain_generation: str) -> int:
        assert netuid == NETUID and chain_generation == "test-chain"
        return 100

    def apply_finalized_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot.finalized_block)

    def accept_activation_signal(self, signal, *, now) -> None:
        self.activations.append(signal)

    def accept_ready_signal(self, signal, *, now) -> None:
        self.ready.append(signal)


class FinalizedChainScannerTests(unittest.TestCase):
    def test_miner_load_client_has_no_control_plane_or_permanent_storage_access(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (root / "tests/load/testnet_submission.py").read_text()
        for forbidden in (
            "psycopg",
            "TEUTONIC_DATABASE_URL",
            "AccessControllerRepository",
            "CLOUDFLARE_API_TOKEN",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
        ):
            self.assertNotIn(forbidden, source)

    def test_scans_activation_and_ready_before_advancing_head(self) -> None:
        hotkey = encode_ss58_public_key(bytes(SigningKey.generate().verify_key))
        activation = activation_signal_payload(b"s" * 64)
        ready = ready_signal_payload(REGISTRATION, MANIFEST)
        blocks = {
            101: ({"extrinsics": [commitment(hotkey, activation)]}, [event(hotkey, 0)]),
            102: ({"extrinsics": [commitment(hotkey, ready)]}, [event(hotkey, 0)]),
            103: ({"extrinsics": []}, []),
        }
        repository = FakeRepository()
        scanner = FinalizedChainScanner(
            FakeSubtensor(hotkey, blocks),
            netuid=NETUID,
            chain_generation="test-chain",
        )

        scanned, accepted = scanner.scan(repository)

        self.assertEqual((scanned, accepted), (3, 2))
        self.assertEqual(repository.snapshots, [101, 102, 103])
        self.assertEqual(repository.activations[0].signalling_hotkey, hotkey)
        self.assertEqual(repository.ready[0].registration_id, REGISTRATION)
        self.assertEqual(repository.ready[0].manifest_sha256, MANIFEST)


if __name__ == "__main__":
    unittest.main()
