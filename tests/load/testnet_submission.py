#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import concurrent.futures
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import boto3
import bittensor as bt
import httpx
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from nacl.signing import SigningKey

from teutonic.access import (
    MailboxCipher,
    Manifest,
    ManifestFile,
    ReadySignal,
    ready_signal_payload,
)
from teutonic.access.crypto import encode_signature
from teutonic.credentials import (
    activation_message,
    activation_signal_payload,
    mailbox_object_key,
    registration_id,
)
from teutonic.storage.artifacts import model_digest_from_inventory, sha256_file


ROOT = Path(__file__).resolve().parents[2]
NETUID = 306
MINER_WALLET = "testXXc"
MINER_HOTKEY_NAME = ""
MINER_HOTKEY = ""
SEED = (
    ROOT
    / ".cache/qwen2.5-0.5b-seed/Qwen--Qwen2.5-0.5B"
    / "060db6499f32faf8b98477b0a26969ef7d8b9987"
)


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def finalized_registration(subtensor: bt.Subtensor) -> tuple[int, int, str]:
    deadline = time.monotonic() + 600
    while True:
        block_hash = subtensor.substrate.get_chain_finalised_head()
        block = int(subtensor.substrate.get_block_number(block_hash))
        metagraph = subtensor.metagraph(NETUID, block=block, lite=True)
        uids = (
            metagraph.uids.tolist()
            if hasattr(metagraph.uids, "tolist")
            else metagraph.uids
        )
        registration_blocks = (
            metagraph.block_at_registration.tolist()
            if hasattr(metagraph.block_at_registration, "tolist")
            else metagraph.block_at_registration
        )
        for uid, hotkey, registration_block in zip(
            uids,
            metagraph.hotkeys,
            registration_blocks,
        ):
            if str(hotkey) == MINER_HOTKEY:
                identifier = registration_id(
                    netuid=NETUID,
                    uid=int(uid),
                    hotkey=MINER_HOTKEY,
                    registration_block=int(registration_block),
                    chain_generation=required("TEUTONIC_CHAIN_GENERATION"),
                )
                return int(uid), int(registration_block), identifier
        if time.monotonic() >= deadline:
            raise RuntimeError("hotkey did not appear in the finalized metagraph")
        time.sleep(6)


def public_mailbox(registration: str, generation: int = 1) -> bytes:
    base = required("TEUTONIC_MAILBOX_PUBLIC_BASE_URL").rstrip("/")
    key = mailbox_object_key(registration, generation)
    deadline = time.monotonic() + 600
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        attempt = 0
        while True:
            response = client.get(f"{base}/{key}", params={"poll": attempt})
            if response.status_code == 200:
                return response.content
            if response.status_code != 404:
                response.raise_for_status()
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for encrypted public mailbox credential")
            attempt += 1
            time.sleep(2)


def miner_wallet() -> bt.Wallet:
    return bt.Wallet(
        name=MINER_WALLET,
        hotkey=MINER_HOTKEY_NAME,
        path=required("BT_WALLET_PATH"),
    )


def miner_nacl_key() -> SigningKey:
    keyfile = (
        Path(required("BT_WALLET_PATH"))
        / MINER_WALLET
        / "hotkeys"
        / MINER_HOTKEY_NAME
    )
    payload = json.loads(keyfile.read_text())
    seed_hex = str(payload["secretSeed"])
    seed = bytes.fromhex(seed_hex.removeprefix("0x"))
    if len(seed) != 32:
        raise RuntimeError("test miner key file did not contain a 32-byte Ed25519 seed")
    return SigningKey(seed)


def make_challenger() -> Path:
    if not SEED.is_dir():
        raise RuntimeError(f"seed snapshot is missing: {SEED}")
    root = Path(
        tempfile.mkdtemp(prefix=f"qwen-{MINER_HOTKEY_NAME}-", dir=ROOT / ".cache")
    )
    for source in sorted(SEED.rglob("*")):
        relative = source.relative_to(SEED)
        if relative.parts and relative.parts[0] == ".cache":
            continue
        destination = root / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if source.is_symlink():
            raise RuntimeError(f"seed contains a symlink: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.as_posix() == "generation_config.json":
            value = json.loads(source.read_text())
            value["teutonic_test_variant"] = MINER_HOTKEY_NAME
            destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        else:
            os.link(source, destination)
    return root


def signed_manifest(root: Path, registration: str, wallet: bt.Wallet) -> Manifest:
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() != "manifest.json"
    )
    print(f"Hashing challenger inventory ({len(paths)} files, about 1 GB)...", flush=True)
    files = tuple(
        ManifestFile(
            path=path.relative_to(root).as_posix(),
            size=path.stat().st_size,
            sha256=sha256_file(path),
        )
        for path in paths
    )
    digest = model_digest_from_inventory(
        [(item.path, item.size, item.sha256) for item in files]
    )
    unsigned = Manifest(
        registration_id=registration,
        hotkey=MINER_HOTKEY,
        model_name=f"qwen2.5-0.5b-test-{MINER_HOTKEY_NAME}",
        files=files,
        model_digest=digest,
        signature="unsigned",
    )
    signature = wallet.hotkey.sign(unsigned.signing_payload())
    return replace(unsigned, signature=encode_signature(bytes(signature)))


def upload_challenger(root: Path, manifest: Manifest, envelope: dict) -> None:
    client = boto3.client(
        "s3",
        endpoint_url=envelope["r2_endpoint"],
        aws_access_key_id=envelope["access_key_id"],
        aws_secret_access_key=envelope["secret_access_key"],
        aws_session_token=envelope["session_token"],
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 10, "mode": "standard"},
            max_pool_connections=256,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    transfer = TransferConfig(
        multipart_threshold=32 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=16,
        num_download_attempts=10,
        use_threads=True,
    )
    bucket = envelope["private_model_bucket"]
    prefix = envelope["allowed_prefix"]
    paths = sorted(path for path in root.rglob("*") if path.is_file())
    probe_key = prefix + ".credential-propagation-probe"
    for attempt in range(36):
        try:
            client.put_object(Bucket=bucket, Key=probe_key, Body=b"ready")
            client.delete_object(Bucket=bucket, Key=probe_key)
            break
        except Exception:
            if attempt == 35:
                raise
            if attempt == 0:
                print("Waiting for the new R2 parent token to propagate...", flush=True)
            time.sleep(5)
    print(f"Uploading challenger directly to private R2 ({len(paths)} files)...", flush=True)
    started = time.monotonic()

    def upload(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        key = prefix + relative
        digest = next(item.sha256 for item in manifest.files if item.path == relative)
        client.upload_file(
            str(path), bucket, key, ExtraArgs={"Metadata": {"sha256": digest}}, Config=transfer
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(upload, paths))
    client.put_object(
        Bucket=bucket,
        Key=prefix + "manifest.json",
        Body=manifest.as_bytes(),
        ContentType="application/json",
        Metadata={"sha256": manifest.manifest_sha256},
    )
    elapsed = time.monotonic() - started
    size = sum(path.stat().st_size for path in paths)
    print(f"Private upload complete: {size / 1e9:.3f} GB in {elapsed:.1f}s", flush=True)


def main() -> int:
    global MINER_HOTKEY_NAME, MINER_HOTKEY
    parser = argparse.ArgumentParser(description="Run one real testnet miner submission")
    parser.add_argument("--hotkey-name", required=True)
    args = parser.parse_args()
    if re.fullmatch(r"testXXc(?:[1-9][0-9]*)", args.hotkey_name) is None:
        raise RuntimeError("test harness accepts only numbered testXXc hotkeys")
    MINER_HOTKEY_NAME = args.hotkey_name
    if required("TEUTONIC_NETWORK") != "test":
        raise RuntimeError("this temporary harness is restricted to Bittensor testnet")
    if int(required("TEUTONIC_NETUID")) != NETUID:
        raise RuntimeError(f"this temporary harness is restricted to subnet {NETUID}")
    wallet = miner_wallet()
    if int(wallet.hotkey.crypto_type) != 0:
        raise RuntimeError(f"{MINER_HOTKEY_NAME} is not an Ed25519 hotkey")
    MINER_HOTKEY = wallet.hotkey.ss58_address
    subtensor = bt.Subtensor(network="test")
    atexit.register(subtensor.close)
    challenger: Path | None = None
    try:
        uid, registration_block, registration = finalized_registration(subtensor)
        current = subtensor.get_commitment(NETUID, uid)
        if current.startswith("r2ready:v1"):
            previous = ReadySignal.parse(
                current,
                signalling_hotkey=MINER_HOTKEY,
                block_number=0,
                extrinsic_index=0,
                event_index=0,
            )
            if previous.registration_id == registration:
                print(
                    f"SKIP {MINER_HOTKEY_NAME}: this registration is already ready",
                    flush=True,
                )
                return 0
        print(
            f"Finalized registration: block={registration_block} uid={uid} "
            f"registration={registration[:12]}...",
            flush=True,
        )

        message = activation_message(
            netuid=NETUID,
            uid=uid,
            hotkey=MINER_HOTKEY,
            registration_id=registration,
            registration_block=registration_block,
            chain_generation=required("TEUTONIC_CHAIN_GENERATION"),
        )
        activation_payload = activation_signal_payload(
            bytes(wallet.hotkey.sign(message.encode()))
        )
        print(
            f"Submitting finalized activation commitment from {MINER_HOTKEY_NAME}...",
            flush=True,
        )
        activation = subtensor.set_commitment(
            wallet=wallet,
            netuid=NETUID,
            data=activation_payload,
            raise_error=True,
            wait_for_inclusion=True,
            wait_for_finalization=True,
        )
        if not activation.success:
            raise RuntimeError(f"on-chain activation failed: {activation.message}")

        ciphertext = public_mailbox(registration)
        envelope = MailboxCipher.decrypt_for_test(ciphertext, miner_nacl_key())
        expected = {
            "hotkey": MINER_HOTKEY,
            "registration_id": registration,
            "chain_generation": required("TEUTONIC_CHAIN_GENERATION"),
            "registration_block": registration_block,
            "allowed_prefix": f"models/registrations/{registration}/",
        }
        for field, value in expected.items():
            if envelope.get(field) != value:
                raise RuntimeError(f"decrypted mailbox has an unexpected {field}")
        print("Miner decrypted public mailbox credential generation 1", flush=True)

        challenger = make_challenger()
        manifest = signed_manifest(challenger, registration, wallet)
        upload_challenger(challenger, manifest, envelope)

        payload = ready_signal_payload(registration, manifest.manifest_sha256)
        print(f"Submitting finalized ready commitment from {MINER_HOTKEY_NAME}...", flush=True)
        commitment = subtensor.set_commitment(
            wallet=wallet,
            netuid=NETUID,
            data=payload,
            raise_error=True,
            wait_for_inclusion=True,
            wait_for_finalization=True,
        )
        if not commitment.success:
            raise RuntimeError(f"on-chain ready commitment failed: {commitment.message}")
        print(
            "Finalized ready commitment submitted; validator processing is independent",
            flush=True,
        )
    finally:
        if challenger is not None:
            shutil.rmtree(challenger, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
