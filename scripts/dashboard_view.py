#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import time
from datetime import timedelta

import boto3
import psycopg
from botocore.config import Config

from teutonic.config import BucketNames
from teutonic.dashboard import (
    DashboardObjectStore,
    DashboardProjectionRepository,
    DashboardViewService,
    MarketClient,
)

log = logging.getLogger("teutonic.dashboard-view")
stopping = False


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def stop(_signum, _frame) -> None:
    global stopping
    stopping = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish dashboard-v1 from sanitized PostgreSQL views")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    endpoint = required("TEUTONIC_DASHBOARD_R2_ENDPOINT")
    session = boto3.session.Session()
    client = session.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=required("TEUTONIC_DASHBOARD_R2_ACCESS_KEY_ID"),
        aws_secret_access_key=required("TEUTONIC_DASHBOARD_R2_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get("TEUTONIC_DASHBOARD_R2_SESSION_TOKEN") or None,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
    )
    market_url = os.environ.get("TEUTONIC_MARKET_URL", "").strip()
    market = (
        MarketClient(market_url, source=required("TEUTONIC_MARKET_SOURCE"))
        if market_url
        else None
    )
    instance = os.environ.get("TEUTONIC_INSTANCE_ID", f"{socket.gethostname()}-{os.getpid()}")
    del instance  # Reserved for service heartbeat integration; never published.
    repository = None
    with psycopg.connect(required("TEUTONIC_DATABASE_URL"), autocommit=True) as connection:
        repository = DashboardProjectionRepository(
            connection,
            netuid=int(required("TEUTONIC_NETUID")),
            chain_generation=required("TEUTONIC_CHAIN_GENERATION"),
            competition=required("TEUTONIC_COMPETITION"),
            chain_name=os.environ.get("TEUTONIC_CHAIN_NAME", "Teutonic"),
        )
        if not repository.acquire_lock():
            raise RuntimeError("another dashboard-view publisher holds the advisory lock")
        service = DashboardViewService(
            repository,
            DashboardObjectStore(
                client,
                bucket=BucketNames.from_env().dashboard,
                maximum_bytes=int(os.environ.get("TEUTONIC_DASHBOARD_MAX_BYTES", 10 * 1024 * 1024)),
            ),
            market_client=market,
            maximum_market_stale=timedelta(
                seconds=int(os.environ.get("TEUTONIC_MARKET_MAX_STALE_SECONDS", "3600"))
            ),
        )
        failures = 0
        try:
            while not stopping:
                try:
                    result, dataset_result = service.publish_once()
                    failures = 0
                    log.info(
                        "dashboard %s bytes=%d sha256=%s",
                        result.state,
                        result.size_bytes,
                        result.sha256,
                    )
                    log.info(
                        "dataset manifest %s bytes=%d sha256=%s",
                        dataset_result.state,
                        dataset_result.size_bytes,
                        dataset_result.sha256,
                    )
                    if args.once:
                        return 0
                    active = repository.project()["current_eval"] is not None
                    time.sleep(5 if active else 20)
                except Exception as exc:
                    failures += 1
                    log.error("dashboard publication failed type=%s", type(exc).__name__)
                    if args.once:
                        raise
                    time.sleep(min(60, 2 ** min(failures, 5)))
        finally:
            repository.release_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
