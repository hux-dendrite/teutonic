from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

import bittensor as bt
from nacl.signing import SigningKey

from teutonic.credentials import registration_id


REGISTRATION_FILE = "registration.json"
AUTH_FILE = "upload-auth.json"
MANIFEST_FILE = "manifest.json"


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def env_or_none(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def add_wallet_arguments(
    parser: argparse.ArgumentParser, *, include_state_dir: bool = True
) -> None:
    parser.add_argument("--wallet-name", default=env_or_none("BT_WALLET_NAME"))
    parser.add_argument("--hotkey-name", default=env_or_none("BT_WALLET_HOTKEY"))
    parser.add_argument(
        "--wallet-path",
        type=Path,
        default=Path(os.environ.get("BT_WALLET_PATH", "~/.bittensor/wallets")).expanduser(),
    )
    if include_state_dir:
        parser.add_argument(
            "--state-dir",
            type=Path,
            help="local miner state directory (default: .teutonic-miner/<hotkey-address>)",
        )


def wallet_from_args(args: argparse.Namespace) -> bt.Wallet:
    if not args.wallet_name or not args.hotkey_name:
        raise RuntimeError("--wallet-name and --hotkey-name are required")
    wallet = bt.Wallet(
        name=args.wallet_name,
        hotkey=args.hotkey_name,
        path=str(args.wallet_path),
    )
    if int(wallet.hotkey.crypto_type) != 0:
        raise RuntimeError("Teutonic mailbox encryption requires an Ed25519 hotkey")
    return wallet


def state_dir_from_args(args: argparse.Namespace, wallet: bt.Wallet) -> Path:
    state_dir = args.state_dir or (Path.cwd() / ".teutonic-miner" / wallet.hotkey.ss58_address)
    state_dir = state_dir.expanduser().resolve()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        state_dir.chmod(0o700)
    except OSError:
        pass
    return state_dir


def write_json(path: Path, value: Mapping[str, Any], *, secret: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600 if secret else 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if secret:
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing miner state file: {path}") from exc
    except Exception as exc:
        raise RuntimeError(f"invalid miner state JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"miner state file must contain an object: {path}")
    return value


@dataclass(frozen=True, slots=True)
class RegistrationState:
    network: str
    netuid: int
    chain_generation: str
    wallet_name: str
    hotkey_name: str
    hotkey: str
    uid: int
    registration_block: int
    registration_id: str
    observed_finalized_block: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RegistrationState:
        expected = {
            "network",
            "netuid",
            "chain_generation",
            "wallet_name",
            "hotkey_name",
            "hotkey",
            "uid",
            "registration_block",
            "registration_id",
            "observed_finalized_block",
        }
        if set(value) != expected:
            raise RuntimeError("registration state fields do not match the miner protocol")
        state = cls(
            network=str(value["network"]),
            netuid=int(value["netuid"]),
            chain_generation=str(value["chain_generation"]),
            wallet_name=str(value["wallet_name"]),
            hotkey_name=str(value["hotkey_name"]),
            hotkey=str(value["hotkey"]),
            uid=int(value["uid"]),
            registration_block=int(value["registration_block"]),
            registration_id=str(value["registration_id"]),
            observed_finalized_block=int(value["observed_finalized_block"]),
        )
        expected_id = registration_id(
            netuid=state.netuid,
            uid=state.uid,
            hotkey=state.hotkey,
            registration_block=state.registration_block,
            chain_generation=state.chain_generation,
        )
        if state.registration_id != expected_id:
            raise RuntimeError("registration state contains a non-canonical registration ID")
        positions = (
            state.netuid,
            state.uid,
            state.registration_block,
            state.observed_finalized_block,
        )
        if min(positions) < 0:
            raise RuntimeError("registration state contains a negative chain position")
        return state

    def save(self, path: Path) -> None:
        write_json(path, asdict(self))


def load_registration(state_dir: Path, wallet: bt.Wallet) -> RegistrationState:
    state = RegistrationState.from_mapping(read_json(state_dir / REGISTRATION_FILE))
    if state.wallet_name != wallet.name or state.hotkey_name != wallet.hotkey_str:
        raise RuntimeError("registration state belongs to a different local wallet")
    if state.hotkey != wallet.hotkey.ss58_address:
        raise RuntimeError("registration state belongs to a different hotkey address")
    return state


def signing_key(wallet: bt.Wallet) -> SigningKey:
    try:
        payload = json.loads(bytes(wallet.hotkey_file.data))
        seed = bytes.fromhex(str(payload["secretSeed"]).removeprefix("0x"))
    except Exception as exc:
        raise RuntimeError(
            "could not unlock the Ed25519 hotkey seed for mailbox decryption"
        ) from exc
    if len(seed) != 32:
        raise RuntimeError("Ed25519 hotkey seed must contain exactly 32 bytes")
    return SigningKey(seed)


@contextmanager
def subtensor_connection(network: str) -> Iterator[bt.Subtensor]:
    subtensor = bt.Subtensor(network=network)
    try:
        yield subtensor
    finally:
        subtensor.close()


def finalized_registration(
    subtensor: bt.Subtensor,
    *,
    network: str,
    netuid: int,
    chain_generation: str,
    wallet: bt.Wallet,
) -> RegistrationState | None:
    block_hash = subtensor.substrate.get_chain_finalised_head()
    finalized_block = int(subtensor.substrate.get_block_number(block_hash))
    metagraph = subtensor.metagraph(netuid, block=finalized_block, lite=True)
    uids = metagraph.uids.tolist() if hasattr(metagraph.uids, "tolist") else metagraph.uids
    registration_blocks = (
        metagraph.block_at_registration.tolist()
        if hasattr(metagraph.block_at_registration, "tolist")
        else metagraph.block_at_registration
    )
    hotkey = wallet.hotkey.ss58_address
    for uid, observed_hotkey, registration_block in zip(
        uids, metagraph.hotkeys, registration_blocks
    ):
        if str(observed_hotkey) != hotkey:
            continue
        uid_value = int(uid)
        block_value = int(registration_block)
        return RegistrationState(
            network=network,
            netuid=netuid,
            chain_generation=chain_generation,
            wallet_name=wallet.name,
            hotkey_name=wallet.hotkey_str,
            hotkey=hotkey,
            uid=uid_value,
            registration_block=block_value,
            registration_id=registration_id(
                netuid=netuid,
                uid=uid_value,
                hotkey=hotkey,
                registration_block=block_value,
                chain_generation=chain_generation,
            ),
            observed_finalized_block=finalized_block,
        )
    return None


def require_current_registration(
    subtensor: bt.Subtensor,
    *,
    saved: RegistrationState,
    wallet: bt.Wallet,
) -> RegistrationState:
    current = finalized_registration(
        subtensor,
        network=saved.network,
        netuid=saved.netuid,
        chain_generation=saved.chain_generation,
        wallet=wallet,
    )
    if current is None:
        raise RuntimeError("hotkey is no longer registered at the finalized head")
    if current.registration_id != saved.registration_id:
        raise RuntimeError(
            "hotkey was re-registered under a new UID or registration block; rerun register.py"
        )
    return current
