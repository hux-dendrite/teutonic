#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import time
from datetime import datetime, timezone

import psycopg

from teutonic.weights import BittensorWeightGateway, DryRunWeightGateway
from teutonic.weights.repository import WeightPublicationRepository
from teutonic.weights.service import WeightPublisher


SOFTWARE_VERSION = "phase7-v1"
log = logging.getLogger("teutonic.weight-publisher")
stopping = False


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def stop(_signum, _frame) -> None:
    global stopping
    stopping = True


def build_gateway(mode: str, netuid: int):
    if mode == "dry_run":
        return DryRunWeightGateway(netuid=netuid)
    return BittensorWeightGateway(
        network=required("TEUTONIC_NETWORK"),
        netuid=netuid,
        wallet_path=os.path.expanduser(
            os.environ.get("BT_WALLET_PATH", "~/.bittensor/wallets")
        ),
        wallet_name=required("BT_WALLET_NAME"),
        wallet_hotkey=required("BT_WALLET_HOTKEY"),
        mortality_period=int(os.environ.get("TEUTONIC_WEIGHT_MORTALITY_BLOCKS", "128")),
        version_key=int(os.environ.get("TEUTONIC_WEIGHT_VERSION_KEY", "10005000")),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Durable PostgreSQL-backed weight publisher")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    database_url = required("TEUTONIC_DATABASE_URL")
    netuid = int(required("TEUTONIC_NETUID"))
    generation = required("TEUTONIC_CHAIN_GENERATION")
    competition = required("TEUTONIC_COMPETITION")
    mode = os.environ.get("TEUTONIC_WEIGHT_PUBLISHER_MODE", "dry_run").strip()
    instance = os.environ.get("TEUTONIC_INSTANCE_ID", f"{socket.gethostname()}-{os.getpid()}")
    poll_seconds = float(os.environ.get("TEUTONIC_WEIGHT_POLL_SECONDS", "12"))
    gateway = build_gateway(mode, netuid)

    with psycopg.connect(database_url, autocommit=True) as connection:
        repository = WeightPublicationRepository(
            connection,
            netuid=netuid,
            chain_generation=generation,
            competition=competition,
            instance_id=instance,
        )
        repository.acquire_lock()
        worker = WeightPublisher(repository, gateway, publisher_mode=mode)
        log.info(
            "weight publisher active mode=%s network=%s netuid=%d competition=%s signer=%s",
            mode,
            gateway.network,
            netuid,
            competition,
            gateway.signer_hotkey,
        )
        try:
            while not stopping:
                now = datetime.now(timezone.utc)
                repository.heartbeat_service(
                    now=now, phase="polling", software_version=SOFTWARE_VERSION
                )
                worked = worker.run_one()
                if args.once:
                    return 0 if worked else 3
                if not worked:
                    time.sleep(poll_seconds)
        finally:
            repository.release_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
