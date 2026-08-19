#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import boto3
import psycopg
from botocore.config import Config
from psycopg.rows import dict_row

import chain_config
from teutonic.bootstrap import (
    GenesisIdentity,
    HuggingFaceSeed,
    PublicSeedStore,
    bootstrap_genesis,
)
from teutonic.config import BucketNames
from teutonic.validator import BittensorFinalizedMetagraphReader


log = logging.getLogger("teutonic.seed-bootstrap")


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def r2_endpoint() -> str:
    configured = os.environ.get("TEUTONIC_R2_ENDPOINT", "").strip()
    if configured:
        return configured
    return f"https://{required('CLOUDFLARE_ACCOUNT_ID')}.r2.cloudflarestorage.com"


def r2_client(*, max_pool_connections: int):
    return boto3.client(
        "s3",
        endpoint_url=r2_endpoint(),
        aws_access_key_id=required("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=required("R2_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get("R2_SESSION_TOKEN") or None,
        region_name=os.environ.get("TEUTONIC_R2_REGION", "auto"),
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            max_pool_connections=max_pool_connections,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the pinned Hugging Face seed, publish it to public R2, "
            "and create genesis."
        )
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=Path(os.environ.get("TEUTONIC_SEED_LOCAL_DIR", ".cache/teutonic-seed")),
        help="stable resumable Hugging Face download directory",
    )
    parser.add_argument("--download-workers", type=int, default=16)
    parser.add_argument("--upload-files", type=int, default=16)
    parser.add_argument("--upload-parts", type=int, default=16)
    parser.add_argument("--upload-part-size-mib", type=int, default=64)
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    expected_chain_path = Path(__file__).resolve().parents[1] / "chain.toml"
    if chain_config.CONFIG_PATH != expected_chain_path:
        raise RuntimeError(f"genesis bootstrap must read {expected_chain_path}")
    log.info("reading genesis configuration from %s", chain_config.CONFIG_PATH)
    if chain_config.SEED_REPO_BACKEND != "hf":
        raise RuntimeError("genesis bootstrap only accepts the configured Hugging Face backend")

    log.info("verifying Hugging Face seed %s@%s", chain_config.SEED_REPO, chain_config.SEED_DIGEST)
    artifact = HuggingFaceSeed(
        token=os.environ.get("HF_TOKEN"), max_workers=args.download_workers
    ).materialize(
        repo_id=chain_config.SEED_REPO,
        seed_digest=chain_config.SEED_DIGEST,
        local_dir=args.local_dir.resolve(),
    )
    buckets = BucketNames.from_env()
    log.info(
        "publishing genesis digest=%s to public bucket=%s prefix=%s",
        artifact.model_digest,
        buckets.public_models,
        artifact.prefix,
    )
    PublicSeedStore(
        r2_client(max_pool_connections=args.upload_files * args.upload_parts),
        bucket=buckets.public_models,
        file_concurrency=args.upload_files,
        part_concurrency=args.upload_parts,
        part_size=args.upload_part_size_mib * 1024 * 1024,
    ).publish(artifact)

    hotkey = chain_config.SEED_HOTKEY
    chain = BittensorFinalizedMetagraphReader(
        network=required("TEUTONIC_NETWORK"), netuid=int(required("TEUTONIC_NETUID"))
    ).snapshot()
    uid = chain.uid_by_hotkey.get(hotkey)
    if uid is None:
        raise RuntimeError("genesis hotkey is not registered at the current finalized block")
    identity = GenesisIdentity(
        hotkey=hotkey,
        uid=uid,
        finalized_block=chain.block,
    )
    with psycopg.connect(required("TEUTONIC_DATABASE_URL"), row_factory=dict_row) as connection:
        record = bootstrap_genesis(
            connection,
            netuid=int(required("TEUTONIC_NETUID")),
            chain_generation=required("TEUTONIC_CHAIN_GENERATION"),
            competition=required("TEUTONIC_COMPETITION"),
            public_bucket=buckets.public_models,
            artifact=artifact,
            identity=identity,
        )
    action = "created" if record.created else "verified existing"
    log.info(
        "%s genesis competition=%s reign=%s finalized_block=%s uid=%s",
        action,
        record.competition_id,
        record.reign_id,
        chain.block,
        uid,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
