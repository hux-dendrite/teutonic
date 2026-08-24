#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import time
from datetime import datetime, timedelta, timezone

import boto3
import psycopg
from botocore.config import Config

from teutonic.config import BucketNames
from teutonic.promotion import (
    PromotionRepository,
    PromotionWorker,
    RclonePromotionExecutor,
    S3InventoryInspector,
)
from teutonic.validator import (
    BittensorFinalizedMetagraphReader,
    CrownCoordinator,
    ValidatorRepository,
)


SOFTWARE_VERSION = "promotion-worker-v1"
log = logging.getLogger("teutonic.promotion-worker")
stopping = False
STATUS_LOG_SECONDS = 60.0


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def stop(_signum, _frame) -> None:
    global stopping
    stopping = True


def r2_endpoint() -> str:
    configured = os.environ.get("TEUTONIC_R2_ENDPOINT", "").strip()
    if configured:
        return configured
    return f"https://{required('CLOUDFLARE_ACCOUNT_ID')}.r2.cloudflarestorage.com"


def build_r2_client():
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
        ),
    )


def configure_rclone(remote: str) -> None:
    prefix = f"RCLONE_CONFIG_{remote.upper()}_"
    values = {
        "TYPE": "s3",
        "PROVIDER": "Cloudflare",
        "ACCESS_KEY_ID": required("R2_ACCESS_KEY_ID"),
        "SECRET_ACCESS_KEY": required("R2_SECRET_ACCESS_KEY"),
        "ENDPOINT": r2_endpoint(),
        "REGION": os.environ.get("TEUTONIC_R2_REGION", "auto"),
        "ENV_AUTH": "false",
        "NO_CHECK_BUCKET": "true",
    }
    session_token = os.environ.get("R2_SESSION_TOKEN", "").strip()
    if session_token:
        values["SESSION_TOKEN"] = session_token
    for suffix, value in values.items():
        os.environ.setdefault(prefix + suffix, value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Independent immutable-model promotion worker"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    network = required("TEUTONIC_NETWORK")
    netuid = int(required("TEUTONIC_NETUID"))
    generation = required("TEUTONIC_CHAIN_GENERATION")
    competition = required("TEUTONIC_COMPETITION")
    instance = os.environ.get(
        "TEUTONIC_PROMOTION_INSTANCE_ID", f"{socket.gethostname()}-{os.getpid()}"
    )
    poll_seconds = float(os.environ.get("TEUTONIC_PROMOTION_POLL_SECONDS", "12"))
    if poll_seconds <= 0:
        raise ValueError("promotion poll interval must be positive")
    lease = timedelta(
        seconds=int(os.environ.get("TEUTONIC_PROMOTION_LEASE_SECONDS", "120"))
    )
    retry = timedelta(
        seconds=int(os.environ.get("TEUTONIC_PROMOTION_RETRY_SECONDS", "30"))
    )
    attempts = int(os.environ.get("TEUTONIC_PROMOTION_MAX_ATTEMPTS", "8"))
    remote_base = os.environ.get("TEUTONIC_RCLONE_REMOTE", "teutonicr2").strip()
    source_remote = f"{remote_base}source"
    destination_remote = f"{remote_base}destination"
    configure_rclone(source_remote)
    configure_rclone(destination_remote)
    buckets = BucketNames.from_env()
    log.info(
        "promotion worker initializing instance=%s network=%s netuid=%d competition=%s",
        instance,
        network,
        netuid,
        competition,
    )

    with psycopg.connect(required("TEUTONIC_DATABASE_URL"), autocommit=True) as connection:
        promotions = PromotionRepository(
            connection,
            netuid=netuid,
            chain_generation=generation,
            competition=competition,
            instance_id=instance,
        )
        crown_repository = ValidatorRepository(
            connection,
            netuid=netuid,
            chain_generation=generation,
            competition=competition,
            instance_id=instance,
            public_model_bucket=buckets.public_models,
        )
        promotions.acquire_lock()
        coordinator = CrownCoordinator(
            crown_repository,
            BittensorFinalizedMetagraphReader(network=network, netuid=netuid),
            burn_uid=int(os.environ.get("TEUTONIC_BURN_UID", "0")),
            king_chain_size=int(os.environ.get("TEUTONIC_KING_CHAIN_SIZE", "5")),
        )
        phase = {"value": "starting"}

        def heartbeat() -> None:
            promotions.heartbeat_service(
                now=datetime.now(timezone.utc),
                phase=phase["value"],
                software_version=SOFTWARE_VERSION,
            )

        def crown(promotion_id: str):
            try:
                reign_id = coordinator(promotion_id)
                if reign_id is not None:
                    log.info(
                        "crowned promoted winner promotion=%s reign=%s",
                        promotion_id,
                        reign_id,
                    )
                return reign_id
            except Exception:
                log.exception(
                    "winner crown reconciliation failed promotion=%s",
                    promotion_id,
                )
                raise

        worker = PromotionWorker(
            promotions,
            RclonePromotionExecutor(source_remote, destination_remote),
            S3InventoryInspector(build_r2_client()),
            lease=lease,
            retry_base_delay=retry,
            max_attempts=attempts,
            on_winner_promoted=crown,
            on_heartbeat=heartbeat,
            after_stage=lambda stage, claim: log.info(
                "promotion stage=%s promotion=%s upload=%s",
                stage,
                claim.promotion_id,
                claim.upload_id,
            ),
        )
        log.info(
            "promotion worker active instance=%s network=%s netuid=%d competition=%s private_bucket=%s public_bucket=%s poll_seconds=%s",
            instance,
            network,
            netuid,
            competition,
            buckets.private_models,
            buckets.public_models,
            poll_seconds,
        )
        next_status_log = 0.0
        try:
            while not stopping:
                phase["value"] = "promoting"
                heartbeat()
                worked = worker.run_one()
                if args.once:
                    return 0 if worked else 3
                if not worked:
                    phase["value"] = "idle"
                    heartbeat()
                    if time.monotonic() >= next_status_log:
                        log.info(
                            "promotion worker heartbeat status=idle instance=%s",
                            instance,
                        )
                        next_status_log = time.monotonic() + STATUS_LOG_SECONDS
                    time.sleep(poll_seconds)
        finally:
            promotions.heartbeat_service(
                now=datetime.now(timezone.utc),
                phase="stopping",
                state="stopping",
                software_version=SOFTWARE_VERSION,
            )
            promotions.release_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
