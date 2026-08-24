#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path

import bittensor as bt


ROOT = Path(__file__).resolve().parents[2]
NETWORK = "test"
NETUID = 306
COLDKEY = "testXXc"
DEFAULT_START = 30
DEFAULT_COUNT = 100
DEFAULT_CONCURRENCY = 10


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def hotkey_names(start: int, count: int) -> list[str]:
    if start < 1:
        raise ValueError("--start must be at least 1")
    if count < 1:
        raise ValueError("--count must be at least 1")
    return [f"{COLDKEY}{index}" for index in range(start, start + count)]


def wallet(name: str) -> bt.Wallet:
    return bt.Wallet(name=COLDKEY, hotkey=name, path=required("BT_WALLET_PATH"))


def ensure_hotkey(name: str) -> str:
    key_path = Path(required("BT_WALLET_PATH")) / COLDKEY / "hotkeys" / name
    if not key_path.exists():
        print(f"Creating ed25519 hotkey {name}", flush=True)
        result = subprocess.run(
            [
                str(ROOT / ".venv/bin/btcli"),
                "wallet",
                "new-hotkey",
                "--wallet-name",
                COLDKEY,
                "--wallet-path",
                required("BT_WALLET_PATH"),
                "--hotkey",
                name,
                "--n-words",
                "12",
                "--no-use-password",
                "--no-overwrite",
                "--crypto-type",
                "ed25519",
                "--quiet",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"btcli failed to create hotkey {name}")

    value = wallet(name)
    if int(value.hotkey.crypto_type) != 0:
        raise RuntimeError(f"existing hotkey {name} is not ed25519")
    return value.hotkey.ss58_address


def register_hotkey(name: str) -> None:
    print(f"Registering {name} on testnet subnet {NETUID}", flush=True)
    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/btcli"),
            "subnet",
            "register",
            "--wallet-name",
            COLDKEY,
            "--wallet-path",
            required("BT_WALLET_PATH"),
            "--hotkey",
            name,
            "--network",
            NETWORK,
            "--netuid",
            str(NETUID),
            "--safe-register",
            "--tolerance",
            "0.50",
            "--no-prompt",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"btcli failed to register {name}; restart the PM2 job to resume")


def run_submission(name: str) -> tuple[str, int]:
    print(f"Starting submission worker for {name}", flush=True)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/load/testnet_submission.py"),
            "--hotkey-name",
            name,
        ],
        cwd=ROOT,
        check=False,
    )
    if result.returncode == 0:
        print(f"Submission worker completed for {name}", flush=True)
    else:
        print(f"Submission worker failed for {name} (exit {result.returncode})", flush=True)
    return name, result.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create, register, and submit a resumable batch of testnet miners"
    )
    parser.add_argument("--start", type=int, default=DEFAULT_START)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    return parser.parse_args()


def validate_environment() -> None:
    if required("TEUTONIC_NETWORK") != NETWORK:
        raise RuntimeError("load runner is restricted to Bittensor testnet")
    if int(required("TEUTONIC_NETUID")) != NETUID:
        raise RuntimeError(f"load runner is restricted to subnet {NETUID}")
    if required("BT_WALLET_NAME") != COLDKEY:
        raise RuntimeError(f"load runner is restricted to coldkey {COLDKEY}")
    required("TEUTONIC_MAILBOX_PUBLIC_BASE_URL")


def main() -> int:
    args = parse_args()
    validate_environment()
    names = hotkey_names(args.start, args.count)
    if args.concurrency < 1:
        raise RuntimeError("--concurrency must be at least 1")

    subtensor = bt.Subtensor(network=NETWORK)
    try:
        registered = set(subtensor.metagraph(NETUID, lite=True).hotkeys)
    finally:
        subtensor.close()

    print(
        f"Preparing {len(names)} miners ({names[0]} through {names[-1]}); "
        f"submission concurrency={args.concurrency}",
        flush=True,
    )
    futures: list[concurrent.futures.Future[tuple[str, int]]] = []
    registration_error: Exception | None = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        for name in names:
            try:
                address = ensure_hotkey(name)
                if address not in registered:
                    register_hotkey(name)
                    registered.add(address)
                else:
                    print(f"Registration already exists for {name}", flush=True)
                futures.append(executor.submit(run_submission, name))
            except Exception as exc:
                registration_error = exc
                print(f"Stopping new registrations: {exc}", flush=True)
                break

        failures = [name for name, code in (future.result() for future in futures) if code != 0]

    if registration_error is not None:
        return 1
    if failures:
        print(
            f"{len(failures)} submission workers failed: {', '.join(failures)}; "
            "restart this PM2 job to resume",
            flush=True,
        )
        return 1
    print(f"All {len(names)} testnet miner submissions completed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
