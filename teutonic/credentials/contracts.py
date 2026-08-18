from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

PROTOCOL_VERSION = 1
ED25519_SCHEME = "ed25519"
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{40,64}$")


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_hash(value: str, field: str) -> str:
    if not _HEX_SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _require_ss58(value: str, field: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{field} is not a canonical SS58-like identifier")
    return value


def registration_id(
    *, netuid: int, uid: int, hotkey: str, first_seen_finalized_block: int, validator_nonce: str
) -> str:
    """Derive an unambiguous registration identity for one finalized UID occupancy."""
    if min(netuid, uid, first_seen_finalized_block) < 0:
        raise ValueError("netuid, uid, and first_seen_finalized_block must be non-negative")
    _require_ss58(hotkey, "hotkey")
    if not validator_nonce or "|" in validator_nonce or len(validator_nonce) > 128:
        raise ValueError("validator_nonce must be a non-empty delimiter-safe value")
    body = json.dumps(
        {
            "first_seen_finalized_block": first_seen_finalized_block,
            "hotkey": hotkey,
            "netuid": netuid,
            "uid": uid,
            "validator_nonce": validator_nonce,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(b"teutonic-registration-v1\0" + body).hexdigest()


def activation_message(
    *,
    netuid: int,
    uid: int,
    hotkey: str,
    registration_id: str,
    validator_nonce: str,
    expires_at: datetime,
) -> str:
    """Canonical bytes-as-text that a miner signs with its Ed25519 hotkey."""
    _require_ss58(hotkey, "hotkey")
    _require_hash(registration_id, "registration_id")
    if min(netuid, uid) < 0:
        raise ValueError("netuid and uid must be non-negative")
    if not validator_nonce or "|" in validator_nonce or len(validator_nonce) > 128:
        raise ValueError("validator_nonce must be a non-empty delimiter-safe value")
    fields = (
        "activate",
        "v1",
        str(netuid),
        str(uid),
        hotkey,
        registration_id,
        validator_nonce,
        _utc_text(expires_at),
    )
    return "|".join(fields)


@dataclass(frozen=True, slots=True)
class ActivationChallenge:
    registration_id: str
    netuid: int
    uid: int
    hotkey: str
    validator_nonce: str
    expires_at: datetime
    protocol_version: int = PROTOCOL_VERSION
    signature_scheme: str = ED25519_SCHEME

    @property
    def message(self) -> str:
        if self.protocol_version != PROTOCOL_VERSION or self.signature_scheme != ED25519_SCHEME:
            raise ValueError("activation v1 requires Ed25519")
        return activation_message(
            netuid=self.netuid,
            uid=self.uid,
            hotkey=self.hotkey,
            registration_id=self.registration_id,
            validator_nonce=self.validator_nonce,
            expires_at=self.expires_at,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "signature_scheme": self.signature_scheme,
            "registration_id": self.registration_id,
            "netuid": self.netuid,
            "uid": self.uid,
            "hotkey": self.hotkey,
            "validator_nonce": self.validator_nonce,
            "expires_at": _utc_text(self.expires_at),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ActivationResponse:
    registration_id: str
    hotkey: str
    validator_nonce: str
    signature: str
    protocol_version: int = PROTOCOL_VERSION
    signature_scheme: str = ED25519_SCHEME

    def as_dict(self) -> dict[str, object]:
        _require_hash(self.registration_id, "registration_id")
        _require_ss58(self.hotkey, "hotkey")
        if self.protocol_version != PROTOCOL_VERSION or self.signature_scheme != ED25519_SCHEME:
            raise ValueError("activation v1 requires Ed25519")
        if not self.validator_nonce or not self.signature:
            raise ValueError("validator_nonce and signature are required")
        return {
            "protocol_version": self.protocol_version,
            "signature_scheme": self.signature_scheme,
            "registration_id": self.registration_id,
            "hotkey": self.hotkey,
            "validator_nonce": self.validator_nonce,
            "signature": self.signature,
        }


def mailbox_object_key(registration_id: str, generation: int) -> str:
    """Return the immutable key for one credential generation.

    Generations start at one, increment by exactly one, and are write-once. A
    miner polls the next numeric key; replacing or deleting an earlier key is
    never part of credential rotation.
    """
    _require_hash(registration_id, "registration_id")
    if generation < 1:
        raise ValueError("credential generation must start at one")
    return f"mailbox/v1/{registration_id}/generations/{generation:020d}.bin"
