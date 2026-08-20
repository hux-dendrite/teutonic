#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from teutonic.access.contracts import Manifest, ManifestFile
from teutonic.access.crypto import encode_signature
from teutonic.storage.artifacts import model_digest_from_inventory, sha256_file

from miner.common import (
    AUTH_FILE,
    MANIFEST_FILE,
    add_wallet_arguments,
    load_registration,
    read_json,
    state_dir_from_args,
    wallet_from_args,
    write_json,
)


FILE_WORKERS = 16
PART_STREAMS = 16
MULTIPART_THRESHOLD = 32 * 1024 * 1024
PART_SIZE = 64 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hash and sign a local model tree, upload it to the mailbox-provided R2 prefix, "
            "and publish manifest.json last."
        )
    )
    add_wallet_arguments(parser)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--model-name", required=True)
    return parser.parse_args()


def model_paths(root: Path) -> list[Path]:
    if not root.is_dir():
        raise RuntimeError(f"model directory does not exist: {root}")
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise RuntimeError(f"model directory contains a symlink: {relative}")
        if not path.is_file():
            continue
        if relative == "manifest.json":
            raise RuntimeError("model directory contains reserved file manifest.json")
        paths.append(path)
    if not paths:
        raise RuntimeError("model directory contains no files")
    return paths


def build_manifest(root: Path, paths: list[Path], state, wallet, model_name: str) -> Manifest:
    def inspect(path: Path) -> ManifestFile:
        return ManifestFile(
            path=path.relative_to(root).as_posix(),
            size=path.stat().st_size,
            sha256=sha256_file(path),
        )

    print(f"Hashing {len(paths)} model files", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=FILE_WORKERS) as executor:
        files = tuple(executor.map(inspect, paths))
    digest = model_digest_from_inventory(
        [(item.path, item.size, item.sha256) for item in files]
    )
    unsigned = Manifest(
        registration_id=state.registration_id,
        hotkey=state.hotkey,
        model_name=model_name.strip(),
        files=files,
        model_digest=digest,
        signature="unsigned",
    )
    signature = wallet.hotkey.sign(unsigned.signing_payload())
    return replace(unsigned, signature=encode_signature(bytes(signature)))


def validate_auth(auth: dict, state) -> None:
    expected = {
        "registration_id": state.registration_id,
        "hotkey": state.hotkey,
        "netuid": state.netuid,
        "uid": state.uid,
        "chain_generation": state.chain_generation,
        "registration_block": state.registration_block,
        "allowed_prefix": f"models/registrations/{state.registration_id}/",
    }
    for field, value in expected.items():
        if auth.get(field) != value:
            raise RuntimeError(f"upload credential contains an unexpected {field}")
    expires_at = datetime.fromisoformat(str(auth.get("expires_at", "")).replace("Z", "+00:00"))
    if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
        raise RuntimeError("upload credential is expired")
    for field in (
        "r2_endpoint",
        "private_model_bucket",
        "access_key_id",
        "secret_access_key",
        "session_token",
    ):
        if not isinstance(auth.get(field), str) or not auth[field]:
            raise RuntimeError(f"upload credential is missing {field}")


def upload(root: Path, paths: list[Path], manifest: Manifest, auth: dict) -> None:
    client = boto3.client(
        "s3",
        endpoint_url=auth["r2_endpoint"],
        aws_access_key_id=auth["access_key_id"],
        aws_secret_access_key=auth["secret_access_key"],
        aws_session_token=auth["session_token"],
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 10, "mode": "standard"},
            max_pool_connections=FILE_WORKERS * PART_STREAMS,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    transfer = TransferConfig(
        multipart_threshold=MULTIPART_THRESHOLD,
        multipart_chunksize=PART_SIZE,
        max_concurrency=PART_STREAMS,
        num_download_attempts=10,
        use_threads=True,
    )
    bucket = auth["private_model_bucket"]
    prefix = auth["allowed_prefix"]
    probe_key = prefix + f".credential-propagation-{uuid.uuid4().hex}"
    for attempt in range(36):
        try:
            client.put_object(Bucket=bucket, Key=probe_key, Body=b"ready")
            client.delete_object(Bucket=bucket, Key=probe_key)
            break
        except Exception:
            if attempt == 35:
                raise
            if attempt == 0:
                print("Waiting for the new R2 credential to propagate", flush=True)
            time.sleep(5)

    digest_by_path = {item.path: item.sha256 for item in manifest.files}

    def upload_one(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        client.upload_file(
            str(path),
            bucket,
            prefix + relative,
            ExtraArgs={"Metadata": {"sha256": digest_by_path[relative]}},
            Config=transfer,
        )

    size = sum(path.stat().st_size for path in paths)
    print(f"Uploading {len(paths)} files ({size / 1e9:.3f} GB) to {bucket}/{prefix}", flush=True)
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=FILE_WORKERS) as executor:
        list(executor.map(upload_one, paths))
    client.put_object(
        Bucket=bucket,
        Key=prefix + "manifest.json",
        Body=manifest.as_bytes(),
        ContentType="application/json",
        Metadata={"sha256": manifest.manifest_sha256},
    )
    print(f"Upload complete in {time.monotonic() - started:.1f}s", flush=True)


def main() -> int:
    args = parse_args()
    if not args.model_name.strip():
        raise RuntimeError("--model-name must not be empty")
    wallet = wallet_from_args(args)
    state_dir = state_dir_from_args(args, wallet)
    root = args.model_dir.expanduser().resolve()
    if state_dir == root or state_dir.is_relative_to(root):
        raise RuntimeError("miner state directory must not be inside the uploaded model tree")
    state = load_registration(state_dir, wallet)
    auth = read_json(state_dir / AUTH_FILE)
    validate_auth(auth, state)
    paths = model_paths(root)
    manifest = build_manifest(root, paths, state, wallet, args.model_name)
    upload(root, paths, manifest, auth)
    write_json(state_dir / MANIFEST_FILE, json.loads(manifest.as_bytes()))
    print(f"model_digest={manifest.model_digest}")
    print(f"manifest_sha256={manifest.manifest_sha256}")
    print(f"manifest_file={state_dir / MANIFEST_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
