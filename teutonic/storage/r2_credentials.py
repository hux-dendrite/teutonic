from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlparse

MAX_TEMPORARY_CREDENTIAL_TTL_SECONDS = 604_800
VALID_ACTIONS = frozenset(
    {
        "HeadObject",
        "GetObject",
        "GetBucketLocation",
        "ListObjectsV1",
        "ListObjectsV2",
        "ListMultipartUploads",
        "ListParts",
        "PutObject",
        "DeleteObject",
        "DeleteObjects",
        "CopyObject",
        "CreateMultipartUpload",
        "UploadPart",
        "UploadPartCopy",
        "AbortMultipartUpload",
        "CompleteMultipartUpload",
    }
)
MINER_UPLOAD_ACTIONS = (
    "PutObject",
    "CreateMultipartUpload",
    "UploadPart",
    "CompleteMultipartUpload",
    "AbortMultipartUpload",
    "ListParts",
)
MINER_PREFIX_SCOPE = "object-read-write"


@dataclass(frozen=True, slots=True)
class TemporaryCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expires_at_unix: int


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def create_local_temporary_credentials(
    *,
    endpoint: str,
    account_id: str,
    parent_access_key_id: str,
    parent_secret_access_key: str,
    bucket: str,
    prefix: str | None = None,
    object_path: str | None = None,
    actions: Sequence[str] | None = None,
    ttl_seconds: int = MAX_TEMPORARY_CREDENTIAL_TTL_SECONDS,
    issued_at_unix: int | None = None,
) -> TemporaryCredentials:
    """Create Cloudflare R2 prefix-scoped temporary credentials.

    Cloudflare currently rejects the otherwise documented fine-grained
    ``actions`` claim for this account. Production credentials therefore omit
    it and use the platform's prefix-scoped object-read-write capability. An
    explicit action list remains available only for capability probes.
    """
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError("R2 endpoint must be an HTTPS origin without a path")
    if not account_id or not parent_access_key_id or not parent_secret_access_key:
        raise ValueError("account and parent credential fields are required")
    if not bucket or (prefix is None) == (object_path is None):
        raise ValueError("bucket and exactly one object path or prefix are required")
    if prefix is not None and (not prefix or not prefix.endswith("/")):
        raise ValueError("temporary credential prefix must be slash-terminated")
    if object_path is not None and (
        not object_path or object_path.startswith("/") or object_path.endswith("/")
    ):
        raise ValueError("temporary credential object path must identify one object")
    if not 1 <= ttl_seconds <= MAX_TEMPORARY_CREDENTIAL_TTL_SECONDS:
        raise ValueError("temporary credential TTL must be between 1 and 604800 seconds")
    selected_actions = None if actions is None else tuple(dict.fromkeys(actions))
    unknown = set() if selected_actions is None else set(selected_actions) - VALID_ACTIONS
    if selected_actions is not None and (not selected_actions or unknown):
        raise ValueError(f"unknown or empty R2 action set: {sorted(unknown)}")

    issued_at = int(time.time()) if issued_at_unix is None else issued_at_unix
    expires_at = issued_at + ttl_seconds
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "aud": parsed.netloc,
        "bucket": bucket,
        "exp": expires_at,
        "iat": issued_at,
        "iss": parent_access_key_id,
        "paths": {
            "objectPaths": [object_path] if object_path is not None else [],
            "prefixPaths": [prefix] if prefix is not None else [],
        },
        "scope": MINER_PREFIX_SCOPE,
        "sub": account_id,
    }
    if selected_actions is not None:
        claims["actions"] = list(selected_actions)
    signing_input = (
        _b64url(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    )
    signature = _b64url(
        hmac.new(parent_secret_access_key.encode(), signing_input.encode(), hashlib.sha256).digest()
    )
    jwt = f"{signing_input}.{signature}"
    return TemporaryCredentials(
        access_key_id=parent_access_key_id,
        secret_access_key=hashlib.sha256(jwt.encode()).hexdigest(),
        session_token=base64.b64encode(f"jwt/{jwt}".encode()).decode(),
        expires_at_unix=expires_at,
    )
