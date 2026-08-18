from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class PromotionObject:
    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if not self.path or self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError("promotion object path is unsafe")
        if self.size < 0:
            raise ValueError("promotion object size cannot be negative")
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256):
            raise ValueError("promotion object SHA-256 is invalid")


def inventory_digest(objects: Mapping[str, PromotionObject]) -> str:
    canonical = [
        {"path": item.path, "size": item.size, "sha256": item.sha256}
        for item in sorted(objects.values(), key=lambda item: item.path)
    ]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"teutonic-promotion-inventory-v1\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class PromotionClaim:
    promotion_id: str
    upload_id: str
    evaluation_id: str
    disposition: str
    model_digest: str
    private_bucket: str
    private_prefix: str
    public_bucket: str
    public_prefix: str
    state: str
    attempt_count: int
    expected: Mapping[str, PromotionObject]


@dataclass(frozen=True, slots=True)
class ObservedObject:
    path: str
    size: int
    sha256: str | None
    etag: str | None = None
