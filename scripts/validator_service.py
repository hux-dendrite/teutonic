#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
from datetime import datetime, timedelta, timezone

import boto3
import psycopg
from botocore.config import Config

from teutonic.config import BucketNames
from teutonic.evaluation import EvaluationRequestV2, HttpEvaluatorClient
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
    ValidatorScheduler,
    evaluation_policy_from_env,
)


SOFTWARE_VERSION = "postgres-validator-v2"
log = logging.getLogger("teutonic.validator")
stopping = False


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
    return boto3.session.Session().client(
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
    """Give rclone the same permanent R2 authority as the inventory client."""
    prefix = f"RCLONE_CONFIG_{remote.upper()}_"
    values = {
        "TYPE": "s3",
        "PROVIDER": "Cloudflare",
        "ACCESS_KEY_ID": required("R2_ACCESS_KEY_ID"),
        "SECRET_ACCESS_KEY": required("R2_SECRET_ACCESS_KEY"),
        "ENDPOINT": r2_endpoint(),
        "REGION": os.environ.get("TEUTONIC_R2_REGION", "auto"),
        "ENV_AUTH": "false",
    }
    token = os.environ.get("R2_SESSION_TOKEN", "").strip()
    if token:
        values["SESSION_TOKEN"] = token
    for suffix, value in values.items():
        os.environ.setdefault(prefix + suffix, value)


async def contract_preflight(request):
    EvaluationRequestV2.from_mapping(request)
    return None


async def heartbeat_loop(repository, phase: dict[str, str], interval: float) -> None:
    while not stopping:
        repository.heartbeat_service(
            now=datetime.now(timezone.utc),
            phase=phase["value"],
            software_version=SOFTWARE_VERSION,
        )
        await asyncio.sleep(interval)


def refresh_weight_plan(coordinator) -> bool:
    try:
        refreshed = coordinator.reconcile_current_weight_plan()
        if refreshed:
            log.info("refreshed current weight plan after finalized UID remap")
        return refreshed
    except Exception:
        log.exception("current weight plan refresh failed")
        return False


async def weight_plan_refresh_loop(coordinator, interval: float) -> None:
    while not stopping:
        refresh_weight_plan(coordinator)
        await asyncio.sleep(interval)


async def run(*, once: bool) -> int:
    database_url = required("TEUTONIC_DATABASE_URL")
    network = required("TEUTONIC_NETWORK")
    netuid = int(required("TEUTONIC_NETUID"))
    generation = required("TEUTONIC_CHAIN_GENERATION")
    competition = required("TEUTONIC_COMPETITION")
    evaluator_url = required("TEUTONIC_EVAL_SERVER")
    instance = os.environ.get(
        "TEUTONIC_VALIDATOR_INSTANCE_ID",
        os.environ.get("TEUTONIC_INSTANCE_ID", f"{socket.gethostname()}-{os.getpid()}"),
    )
    policy = evaluation_policy_from_env()
    buckets = BucketNames.from_env()
    poll_seconds = float(os.environ.get("TEUTONIC_VALIDATOR_POLL_SECONDS", "12"))
    heartbeat_seconds = float(os.environ.get("TEUTONIC_HEARTBEAT_SECONDS", "30"))
    weight_refresh_seconds = float(
        os.environ.get("TEUTONIC_WEIGHT_PLAN_REFRESH_SECONDS", "12")
    )
    if poll_seconds <= 0 or heartbeat_seconds <= 0 or weight_refresh_seconds <= 0:
        raise ValueError("validator poll, heartbeat, and weight refresh intervals must be positive")
    promotion_lease = timedelta(
        seconds=int(os.environ.get("TEUTONIC_PROMOTION_LEASE_SECONDS", "120"))
    )
    promotion_retry = timedelta(
        seconds=int(os.environ.get("TEUTONIC_PROMOTION_RETRY_SECONDS", "30"))
    )
    promotion_attempts = int(os.environ.get("TEUTONIC_PROMOTION_MAX_ATTEMPTS", "8"))
    remote = os.environ.get("TEUTONIC_RCLONE_REMOTE", "teutonicr2").strip()
    configure_rclone(remote)

    with psycopg.connect(database_url, autocommit=True) as connection:
        repository = ValidatorRepository(
            connection,
            netuid=netuid,
            chain_generation=generation,
            competition=competition,
            instance_id=instance,
            public_model_bucket=buckets.public_models,
        )
        promotions = PromotionRepository(
            connection,
            netuid=netuid,
            chain_generation=generation,
            competition=competition,
            instance_id=instance,
        )
        repository.acquire_lock()
        promotions.acquire_lock()
        chain = BittensorFinalizedMetagraphReader(network=network, netuid=netuid)
        coordinator = CrownCoordinator(
            repository,
            chain,
            burn_uid=int(os.environ.get("TEUTONIC_BURN_UID", "0")),
            king_chain_size=int(os.environ.get("TEUTONIC_KING_CHAIN_SIZE", "5")),
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
                log.exception("winner crown reconciliation failed promotion=%s", promotion_id)
                raise

        phase = {"value": "starting"}

        def promotion_heartbeat() -> None:
            repository.heartbeat_service(
                now=datetime.now(timezone.utc),
                phase=phase["value"],
                software_version=SOFTWARE_VERSION,
            )
            refresh_weight_plan(coordinator)

        async with HttpEvaluatorClient(evaluator_url) as evaluator:
            scheduler = ValidatorScheduler(
                repository,
                evaluator,
                policy=policy,
                preflight=contract_preflight,
            )
            promotion_worker = PromotionWorker(
                promotions,
                RclonePromotionExecutor(remote),
                S3InventoryInspector(build_r2_client()),
                lease=promotion_lease,
                retry_base_delay=promotion_retry,
                max_attempts=promotion_attempts,
                on_winner_promoted=crown,
                on_heartbeat=promotion_heartbeat,
            )
            heartbeat_task = asyncio.create_task(
                heartbeat_loop(repository, phase, heartbeat_seconds)
            )
            weight_refresh_task = asyncio.create_task(
                weight_plan_refresh_loop(coordinator, weight_refresh_seconds)
            )
            try:
                phase["value"] = "reconciling_evaluations"
                recovered = await scheduler.reconcile()
                log.info(
                    "validator active network=%s netuid=%d competition=%s recovered=%d",
                    network,
                    netuid,
                    competition,
                    recovered,
                )
                while not stopping:
                    phase["value"] = "evaluating"
                    evaluated = await scheduler.run_once()
                    phase["value"] = "promoting"
                    promoted = promotion_worker.run_one()
                    phase["value"] = "refreshing_weight_plan"
                    weights_refreshed = refresh_weight_plan(coordinator)
                    if once:
                        return 0 if recovered or evaluated or promoted or weights_refreshed else 3
                    if not evaluated and not promoted and not weights_refreshed:
                        phase["value"] = "idle"
                        await asyncio.sleep(poll_seconds)
            finally:
                phase["value"] = "stopping"
                repository.heartbeat_service(
                    now=datetime.now(timezone.utc),
                    phase="stopping",
                    state="stopping",
                    software_version=SOFTWARE_VERSION,
                )
                heartbeat_task.cancel()
                weight_refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                with contextlib.suppress(asyncio.CancelledError):
                    await weight_refresh_task
                promotions.release_lock()
                repository.release_lock()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PostgreSQL-backed evaluator scheduler and R2 promotion worker"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    return asyncio.run(run(once=args.once))


if __name__ == "__main__":
    raise SystemExit(main())
