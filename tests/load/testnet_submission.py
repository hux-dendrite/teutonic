#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import boto3
import bittensor as bt
import httpx
import psycopg
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from nacl.signing import SigningKey

from teutonic.access import (
    AccessControllerJobRunner,
    AccessControllerRepository,
    ControllerLockUnavailable,
    MailboxCipher,
    MailboxStore,
    Manifest,
    ManifestFile,
    MetagraphSnapshot,
    R2UploadController,
    ReadySignal,
    UidAssignment,
    ready_signal_payload,
)
from teutonic.access.cloudflare import CloudflareR2TokenGateway
from teutonic.access.crypto import SecretCipher, encode_signature
from teutonic.credentials import ActivationResponse, mailbox_object_key
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
REGISTRATION_NONCE = "qwen-15-submission-flow-v1"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def now() -> datetime:
    return datetime.now(timezone.utc)


def acquire_controller_lock(repository: AccessControllerRepository) -> None:
    while True:
        try:
            repository.acquire_lock()
            return
        except ControllerLockUnavailable:
            time.sleep(0.5)


def endpoint() -> str:
    return os.environ.get("TEUTONIC_R2_ENDPOINT", "").strip() or (
        f"https://{required('CLOUDFLARE_ACCOUNT_ID')}.r2.cloudflarestorage.com"
    )


def permanent_s3():
    return boto3.client(
        "s3",
        endpoint_url=endpoint(),
        aws_access_key_id=required("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=required("R2_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get("R2_SESSION_TOKEN") or None,
        region_name=os.environ.get("TEUTONIC_R2_REGION", "auto"),
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            max_pool_connections=256,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def finalized_snapshot(subtensor: bt.Subtensor, block: int | None = None) -> MetagraphSnapshot:
    if block is None:
        block_hash = subtensor.substrate.get_chain_finalised_head()
        block = int(subtensor.substrate.get_block_number(block_hash))
    else:
        block_hash = subtensor.substrate.get_block_hash(block)
    metagraph = subtensor.metagraph(NETUID, block=block, lite=True)
    assignments = tuple(
        UidAssignment(uid=int(uid), hotkey=str(hotkey), coldkey=str(coldkey))
        for uid, hotkey, coldkey in zip(
            metagraph.uids.tolist(), metagraph.hotkeys, metagraph.coldkeys
        )
    )
    return MetagraphSnapshot(
        netuid=NETUID,
        chain_generation=required("TEUTONIC_CHAIN_GENERATION"),
        finalized_block=block,
        finalized_block_hash=str(block_hash),
        assignments=assignments,
        observed_at=now(),
        complete=True,
    )


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


def receipt_position(response, subtensor, payload: str, uid: int) -> tuple[int, int, int]:
    receipt = response.extrinsic_receipt
    if receipt is None:
        raise RuntimeError("finalized commitment did not return an extrinsic receipt")
    block = getattr(receipt, "block_number", None)
    if block is None:
        block_hash = getattr(receipt, "block_hash", None)
        if block_hash is None:
            raise RuntimeError("commitment receipt omitted its block")
        block = int(subtensor.substrate.get_block_number(block_hash))
    index = getattr(receipt, "extrinsic_idx", None)
    if index is None:
        index = getattr(receipt, "extrinsic_index", None)
    if index is None:
        index = 0
    if block is None:
        finalized_hash = subtensor.substrate.get_chain_finalised_head()
        finalized = int(subtensor.substrate.get_block_number(finalized_hash))
        first = None
        for candidate in range(finalized, max(-1, finalized - 64), -1):
            if subtensor.get_commitment(NETUID, uid, block=candidate) == payload:
                first = candidate
            elif first is not None:
                break
        block = first
    if block is None:
        raise RuntimeError("could not resolve finalized commitment block number")

    block_hash = subtensor.substrate.get_block_hash(int(block))
    block_value = subtensor.substrate.get_block(block_hash=block_hash)
    index = None
    for position, extrinsic in enumerate(block_value.get("extrinsics", [])):
        value = extrinsic.value
        call = value.get("call") or {}
        if (
            value.get("address") == MINER_HOTKEY
            and call.get("call_module") == "Commitments"
            and call.get("call_function") == "set_commitment"
        ):
            index = position
            break
    if index is None:
        raise RuntimeError("could not locate the finalized commitment extrinsic")
    event_index = None
    for position, event in enumerate(subtensor.substrate.get_events(block_hash)):
        value = getattr(event, "value", event)
        attributes = value.get("attributes") or {}
        if (
            value.get("extrinsic_idx") is not None
            and int(value["extrinsic_idx"]) == index
            and value.get("module_id") == "Commitments"
            and value.get("event_id") == "Commitment"
            and attributes.get("who") == MINER_HOTKEY
        ):
            event_index = position
            break
    if event_index is None:
        raise RuntimeError("could not locate the finalized commitment event")
    return int(block), int(index), int(event_index)


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
    with psycopg.connect(required("TEUTONIC_DATABASE_URL"), autocommit=True) as connection:
        completed = connection.execute(
            "SELECT upload_id, state FROM control_plane.uploads "
            "WHERE signalling_hotkey = %s ORDER BY created_at DESC LIMIT 1",
            (MINER_HOTKEY,),
        ).fetchone()
    if completed is not None:
        print(
            f"SKIP {MINER_HOTKEY_NAME}: submission eligibility is already consumed "
            f"by upload={completed[0]} state={completed[1]}",
            flush=True,
        )
        return 0
    subtensor = bt.Subtensor(network="test")
    atexit.register(subtensor.close)
    s3 = permanent_s3()
    private_bucket = required("TEUTONIC_PRIVATE_MODEL_BUCKET")
    mailbox_bucket = required("TEUTONIC_DASHBOARD_BUCKET")
    challenger: Path | None = None

    controller_key = hashlib.sha256(
        b"teutonic-full-flow-controller-v1\0" + required("R2_SECRET_ACCESS_KEY").encode()
    ).digest()
    validator_key = SigningKey(
        hashlib.sha256(
            b"teutonic-full-flow-mailbox-v1\0" + required("R2_SECRET_ACCESS_KEY").encode()
        ).digest()
    )

    with psycopg.connect(required("TEUTONIC_DATABASE_URL"), autocommit=True) as connection:
        repository = AccessControllerRepository(
            connection,
            registration_nonce=REGISTRATION_NONCE,
            finalized_start_block=0,
        )
        acquire_controller_lock(repository)
        try:
            snapshot = finalized_snapshot(subtensor)
            result = repository.apply_finalized_snapshot(snapshot)
            uid = next(
                item.uid for item in snapshot.assignments if item.hotkey == MINER_HOTKEY
            )
            row = connection.execute(
                """
                SELECT registration_id, state
                  FROM control_plane.registrations
                 WHERE netuid = %s AND chain_generation = %s AND uid = %s AND hotkey = %s
                 ORDER BY first_seen_finalized_block DESC LIMIT 1
                """,
                (NETUID, required("TEUTONIC_CHAIN_GENERATION"), uid, MINER_HOTKEY),
            ).fetchone()
            if row is None:
                raise RuntimeError("access snapshot did not create the miner registration")
            registration, state = str(row[0]), str(row[1])
            print(
                f"Finalized registration: block={snapshot.finalized_block} uid={uid} "
                f"registration={registration[:12]}... state={state}",
                flush=True,
            )
            if state not in {"pending_activation", "activating", "active"}:
                raise RuntimeError(
                    f"test registration cannot resume (state={state})"
                )
            if state in {"pending_activation", "activating"}:
                challenge = repository.issue_activation_challenge(registration, now=now())
                activation_signature = wallet.hotkey.sign(challenge.message.encode())
                repository.verify_activation(
                    ActivationResponse(
                        registration_id=registration,
                        hotkey=MINER_HOTKEY,
                        validator_nonce=challenge.validator_nonce,
                        signature=encode_signature(bytes(activation_signature)),
                    ),
                    now=now(),
                )
                print("Miner activation signature verified", flush=True)
            else:
                print("Resuming the already-activated test registration", flush=True)

            with httpx.Client(timeout=30.0) as http:
                gateway = CloudflareR2TokenGateway(
                    http,
                    account_id=required("CLOUDFLARE_ACCOUNT_ID"),
                    management_token=required("CLOUDFLARE_API_TOKEN"),
                    bucket=private_bucket,
                )
                runner = AccessControllerJobRunner(
                    repository,
                    token_gateway=gateway,
                    upload_controller=R2UploadController(
                        s3, private_model_bucket=private_bucket, chunk_size=8 * 1024 * 1024
                    ),
                    mailbox_store=MailboxStore(s3, bucket=mailbox_bucket),
                    secret_cipher=SecretCipher(controller_key),
                    mailbox_cipher=MailboxCipher(validator_key),
                    account_id=required("CLOUDFLARE_ACCOUNT_ID"),
                    r2_endpoint=endpoint(),
                    private_model_bucket=private_bucket,
                    instance_id="testnet-load-controller",
                )
                jobs = runner.run_until_idle(propagate=True)
                print(f"Issued restricted credentials and mailbox ({jobs} jobs)", flush=True)

                key = mailbox_object_key(registration, 1)
                response = s3.get_object(Bucket=mailbox_bucket, Key=key)
                try:
                    ciphertext = response["Body"].read()
                finally:
                    response["Body"].close()
                envelope = MailboxCipher.decrypt_for_test(ciphertext, miner_nacl_key())
                if envelope["hotkey"] != MINER_HOTKEY:
                    raise RuntimeError("decrypted mailbox was not addressed to the test miner")
                if envelope["allowed_prefix"] != f"models/registrations/{registration}/":
                    raise RuntimeError("decrypted mailbox prefix differs from registration prefix")
                print("Miner decrypted and validated its credential mailbox", flush=True)
                repository.release_lock()

                challenger = make_challenger()
                manifest = signed_manifest(challenger, registration, wallet)
                upload_challenger(challenger, manifest, envelope)

                payload = ready_signal_payload(registration, manifest.manifest_sha256)
                # Hold the durable chain cursor while the ready commitment finalizes.
                # Otherwise a concurrent miner can persist a newer registration
                # snapshot first, making this ready block stale and unrecordable.
                acquire_controller_lock(repository)
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
                ready_block, extrinsic_index, event_index = receipt_position(
                    commitment, subtensor, payload, uid
                )
                repository.apply_finalized_snapshot(finalized_snapshot(subtensor, ready_block))
                signal = ReadySignal.parse(
                    payload,
                    signalling_hotkey=MINER_HOTKEY,
                    block_number=ready_block,
                    extrinsic_index=extrinsic_index,
                    event_index=event_index,
                )
                upload_id = repository.accept_ready_signal(signal, now=now())
                print(
                    f"Accepted finalized ready signal: block={ready_block} "
                    f"extrinsic={extrinsic_index} upload={upload_id}",
                    flush=True,
                )

                jobs = runner.run_until_idle(propagate=True)
                state = connection.execute(
                    "SELECT state FROM control_plane.uploads WHERE upload_id = %s",
                    (upload_id,),
                ).fetchone()[0]
                token_state = connection.execute(
                    "SELECT state FROM control_plane.r2_parent_tokens WHERE registration_id = %s",
                    (registration,),
                ).fetchone()[0]
                print(
                    f"Controller complete: jobs={jobs} upload_state={state} "
                    f"upload_access={token_state}",
                    flush=True,
                )
                if state != "ready_for_evaluation" or token_state != "revoked":
                    raise RuntimeError("controller did not finalize upload and revoke access")
                print(f"UPLOAD_ID={upload_id}", flush=True)
        finally:
            repository.release_lock()
            if challenger is not None:
                shutil.rmtree(challenger, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
