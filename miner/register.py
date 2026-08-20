#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from teutonic.access import ReadySignal
from teutonic.access.crypto import verify_hotkey_signature
from teutonic.credentials import (
    ActivationSignal,
    activation_message,
    activation_signal_payload,
)

from miner.common import (
    REGISTRATION_FILE,
    add_wallet_arguments,
    env_or_none,
    finalized_registration,
    state_dir_from_args,
    subtensor_connection,
    wallet_from_args,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Register a hotkey if needed, resolve its finalized registration identity, "
            "and commit its signed mailbox-activation proof."
        )
    )
    add_wallet_arguments(parser)
    parser.add_argument("--network", default=env_or_none("TEUTONIC_NETWORK"))
    parser.add_argument(
        "--netuid",
        type=int,
        default=int(value) if (value := env_or_none("TEUTONIC_NETUID")) else None,
    )
    parser.add_argument(
        "--chain-generation", default=env_or_none("TEUTONIC_CHAIN_GENERATION")
    )
    parser.add_argument("--registration-timeout", type=int, default=600)
    parser.add_argument("--registration-tolerance", default="0.50")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="fail instead of spending TAO when the hotkey is not registered",
    )
    return parser.parse_args()


def btcli_path() -> str:
    discovered = shutil.which("btcli")
    if discovered:
        return discovered
    adjacent = Path(sys.executable).with_name("btcli")
    if adjacent.is_file():
        return str(adjacent)
    raise RuntimeError("btcli is required to register a hotkey")


def register_on_subnet(args: argparse.Namespace) -> None:
    command = [
        btcli_path(),
        "subnet",
        "register",
        "--wallet-name",
        args.wallet_name,
        "--wallet-path",
        str(args.wallet_path),
        "--hotkey",
        args.hotkey_name,
        "--network",
        args.network,
        "--netuid",
        str(args.netuid),
        "--safe-register",
        "--tolerance",
        args.registration_tolerance,
        "--no-prompt",
    ]
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise RuntimeError(f"btcli registration failed with exit status {result.returncode}")


def wait_for_registration(args: argparse.Namespace, wallet, subtensor):
    deadline = time.monotonic() + args.registration_timeout
    while True:
        state = finalized_registration(
            subtensor,
            network=args.network,
            netuid=args.netuid,
            chain_generation=args.chain_generation,
            wallet=wallet,
        )
        if state is not None:
            return state
        if time.monotonic() >= deadline:
            raise RuntimeError("hotkey did not appear in the finalized metagraph before timeout")
        time.sleep(6)


def activation_matches(payload: str, state) -> bool:
    try:
        signal = ActivationSignal.parse(
            payload,
            netuid=state.netuid,
            chain_generation=state.chain_generation,
            signalling_hotkey=state.hotkey,
            block_number=0,
            extrinsic_index=0,
            event_index=0,
        )
        message = activation_message(
            netuid=state.netuid,
            uid=state.uid,
            hotkey=state.hotkey,
            registration_id=state.registration_id,
            registration_block=state.registration_block,
            chain_generation=state.chain_generation,
        )
        verify_hotkey_signature(state.hotkey, message.encode(), signal.signature)
        return True
    except ValueError:
        return False


def main() -> int:
    args = parse_args()
    if not args.network or args.netuid is None or not args.chain_generation:
        raise RuntimeError("--network, --netuid, and --chain-generation are required")
    if args.netuid < 0 or args.registration_timeout < 1:
        raise RuntimeError("netuid must be non-negative and timeout must be positive")
    wallet = wallet_from_args(args)
    state_dir = state_dir_from_args(args, wallet)

    with subtensor_connection(args.network) as subtensor:
        state = finalized_registration(
            subtensor,
            network=args.network,
            netuid=args.netuid,
            chain_generation=args.chain_generation,
            wallet=wallet,
        )
        if state is None:
            if args.check_only:
                raise RuntimeError("hotkey is not registered at the finalized head")
            print(
                f"Registering {wallet.hotkey.ss58_address} "
                f"on {args.network} netuid {args.netuid}"
            )
            register_on_subnet(args)
            state = wait_for_registration(args, wallet, subtensor)

        state.save(state_dir / REGISTRATION_FILE)
        current = str(subtensor.get_commitment(args.netuid, state.uid) or "")
        if current.startswith("r2ready:v1"):
            ready = ReadySignal.parse(
                current,
                signalling_hotkey=state.hotkey,
                block_number=0,
                extrinsic_index=0,
                event_index=0,
            )
            if ready.registration_id == state.registration_id:
                print("registration already has a finalized-ready commitment; eligibility consumed")
                return 0
        if activation_matches(current, state):
            print("matching mailbox activation commitment already exists")
        else:
            message = activation_message(
                netuid=state.netuid,
                uid=state.uid,
                hotkey=state.hotkey,
                registration_id=state.registration_id,
                registration_block=state.registration_block,
                chain_generation=state.chain_generation,
            )
            payload = activation_signal_payload(bytes(wallet.hotkey.sign(message.encode())))
            print("Submitting signed mailbox activation commitment and waiting for finalization")
            result = subtensor.set_commitment(
                wallet=wallet,
                netuid=state.netuid,
                data=payload,
                raise_error=True,
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )
            if not result.success:
                raise RuntimeError(f"activation commitment failed: {result.message}")

    print(
        f"registered hotkey={state.hotkey} uid={state.uid} "
        f"registration_id={state.registration_id} state_dir={state_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
