#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from teutonic.access.crypto import decode_ss58_public_key

from miner.common import add_wallet_arguments, wallet_from_args


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify that a local Bittensor hotkey is canonical Ed25519."
    )
    add_wallet_arguments(parser, include_state_dir=False)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    wallet = wallet_from_args(args)
    address = wallet.hotkey.ss58_address
    public_key = decode_ss58_public_key(address)
    if public_key != bytes(wallet.hotkey.public_key):
        raise RuntimeError("hotkey SS58 address does not encode its Ed25519 public key")
    print(f"valid_ed25519_hotkey={address}")
    print(f"keyfile_encrypted={str(wallet.hotkey_file.is_encrypted()).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
