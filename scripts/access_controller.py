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
import httpx
import psycopg
from botocore.config import Config
from nacl.signing import SigningKey

from teutonic.access import (
    AccessControllerJobRunner,
    AccessControllerRepository,
    ControllerLockUnavailable,
    MailboxCipher,
    MailboxStore,
    R2UploadController,
)
from teutonic.access.cloudflare import CloudflareR2TokenGateway
from teutonic.access.crypto import SecretCipher
from teutonic.config import BucketNames


log = logging.getLogger("teutonic.access-controller")
stopping = False


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def secret_key(name: str) -> bytes:
    try:
        value = bytes.fromhex(required(name))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be hexadecimal") from exc
    if len(value) != 32:
        raise RuntimeError(f"{name} must contain exactly 32 bytes")
    return value


def stop(_signum, _frame) -> None:
    global stopping
    stopping = True


def r2_endpoint() -> str:
    configured = os.environ.get("TEUTONIC_R2_ENDPOINT", "").strip()
    if configured:
        return configured
    return f"https://{required('CLOUDFLARE_ACCOUNT_ID')}.r2.cloudflarestorage.com"


def r2_client():
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
            max_pool_connections=64,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run durable Cloudflare credential and private-upload controller jobs"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    poll_seconds = float(os.environ.get("TEUTONIC_ACCESS_CONTROLLER_POLL_SECONDS", "2"))
    if poll_seconds <= 0:
        raise ValueError("access-controller poll interval must be positive")
    instance = os.environ.get(
        "TEUTONIC_ACCESS_CONTROLLER_INSTANCE_ID",
        f"{socket.gethostname()}-{os.getpid()}",
    )
    buckets = BucketNames.from_env()
    endpoint = r2_endpoint()
    s3 = r2_client()

    with (
        psycopg.connect(required("TEUTONIC_DATABASE_URL"), autocommit=True) as connection,
        httpx.Client(timeout=30.0) as http,
    ):
        repository = AccessControllerRepository(
            connection,
            registration_nonce=required("TEUTONIC_REGISTRATION_NONCE"),
            finalized_start_block=int(
                os.environ.get("TEUTONIC_FINALIZED_START_BLOCK", "0")
            ),
        )
        runner = AccessControllerJobRunner(
            repository,
            token_gateway=CloudflareR2TokenGateway(
                http,
                account_id=required("CLOUDFLARE_ACCOUNT_ID"),
                management_token=required("CLOUDFLARE_API_TOKEN"),
                bucket=buckets.private_models,
            ),
            upload_controller=R2UploadController(
                s3,
                private_model_bucket=buckets.private_models,
                chunk_size=8 * 1024 * 1024,
            ),
            mailbox_store=MailboxStore(s3, bucket=buckets.dashboard),
            secret_cipher=SecretCipher(secret_key("TEUTONIC_CONTROLLER_SECRET_KEY")),
            mailbox_cipher=MailboxCipher(
                SigningKey(secret_key("TEUTONIC_MAILBOX_SIGNING_KEY"))
            ),
            account_id=required("CLOUDFLARE_ACCOUNT_ID"),
            r2_endpoint=endpoint,
            private_model_bucket=buckets.private_models,
            instance_id=instance,
            lease=timedelta(
                seconds=int(os.environ.get("TEUTONIC_ACCESS_CONTROLLER_LEASE_SECONDS", "120"))
            ),
            retry_delay=timedelta(
                seconds=int(os.environ.get("TEUTONIC_ACCESS_CONTROLLER_RETRY_SECONDS", "5"))
            ),
        )
        log.info("access controller active instance=%s", instance)
        while not stopping:
            acquired = False
            try:
                repository.acquire_lock()
                acquired = True
                recovered = repository.recover_expired_jobs(now=datetime.now(timezone.utc))
                processed = runner.run_until_idle(maximum_jobs=100)
                if recovered or processed:
                    log.info(
                        "controller jobs recovered=%d processed=%d", recovered, processed
                    )
            except ControllerLockUnavailable:
                log.info("controller lock busy; retrying")
            except Exception:
                log.exception("access-controller cycle failed")
                if args.once:
                    raise
            finally:
                if acquired:
                    repository.release_lock()
            if args.once:
                return 0
            time.sleep(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
