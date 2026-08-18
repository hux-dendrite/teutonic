from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
DEFAULT_MAILBOX_BUCKET = "teutonic-mailbox"
DEFAULT_INGEST_BUCKET = "teutonic-ingest"
DEFAULT_PRIVATE_MODEL_BUCKET = "teutonic-private-models"
DEFAULT_PUBLIC_MODEL_BUCKET = "teutonic-models"
DEFAULT_DASHBOARD_BUCKET = "teutonic-dash"


@dataclass(frozen=True, slots=True)
class WorkflowPolicy:
    controller_lease: timedelta = timedelta(seconds=120)
    evaluation_lease: timedelta = timedelta(seconds=120)
    promotion_lease: timedelta = timedelta(seconds=120)
    weight_lease: timedelta = timedelta(seconds=60)
    heartbeat_interval: timedelta = timedelta(seconds=30)
    service_stale_after: timedelta = timedelta(seconds=90)
    max_evaluation_attempts: int = 3
    max_controller_attempts: int = 8
    max_promotion_attempts: int = 8
    max_weight_attempts: int = 12
    temporary_credential_ttl: timedelta = timedelta(days=7)
    credential_rotation_lead: timedelta = timedelta(days=1)
    revoke_upload_access_on_ready: bool = True
    one_submission_per_hotkey: bool = True

    def __post_init__(self) -> None:
        leases = (
            self.controller_lease,
            self.evaluation_lease,
            self.promotion_lease,
            self.weight_lease,
        )
        if any(lease <= self.heartbeat_interval for lease in leases):
            raise ValueError("every lease must exceed the heartbeat interval")
        if self.service_stale_after <= self.heartbeat_interval:
            raise ValueError("service stale threshold must exceed the heartbeat interval")
        if self.temporary_credential_ttl > timedelta(days=7):
            raise ValueError("R2 temporary credentials cannot exceed seven days")
        if not timedelta(0) < self.credential_rotation_lead < self.temporary_credential_ttl:
            raise ValueError("credential rotation lead must be positive and shorter than the TTL")
        if not self.revoke_upload_access_on_ready:
            raise ValueError("upload access must be revoked after a finalized ready signal")
        if not self.one_submission_per_hotkey:
            raise ValueError("the control plane permits only one submission per hotkey")


@dataclass(frozen=True, slots=True)
class BucketNames:
    mailbox: str = DEFAULT_MAILBOX_BUCKET
    ingest: str = DEFAULT_INGEST_BUCKET
    private_models: str = DEFAULT_PRIVATE_MODEL_BUCKET
    public_models: str = DEFAULT_PUBLIC_MODEL_BUCKET
    dashboard: str = DEFAULT_DASHBOARD_BUCKET

    def __post_init__(self) -> None:
        values = (
            self.mailbox,
            self.ingest,
            self.private_models,
            self.public_models,
            self.dashboard,
        )
        if len(set(values)) != len(values):
            raise ValueError("mailbox, ingest, private, public, and dashboard buckets must be distinct")
        invalid = [value for value in values if not _BUCKET_RE.fullmatch(value)]
        if invalid:
            raise ValueError(f"invalid R2 bucket name(s): {', '.join(invalid)}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BucketNames:
        source = os.environ if env is None else env
        return cls(
            mailbox=source.get("TEUTONIC_MAILBOX_BUCKET", DEFAULT_MAILBOX_BUCKET),
            ingest=source.get("TEUTONIC_INGEST_BUCKET", DEFAULT_INGEST_BUCKET),
            private_models=source.get(
                "TEUTONIC_PRIVATE_MODEL_BUCKET", DEFAULT_PRIVATE_MODEL_BUCKET
            ),
            public_models=source.get("TEUTONIC_PUBLIC_MODEL_BUCKET", DEFAULT_PUBLIC_MODEL_BUCKET),
            dashboard=source.get("TEUTONIC_DASHBOARD_BUCKET", DEFAULT_DASHBOARD_BUCKET),
        )
