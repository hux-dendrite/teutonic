from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import httpx


class MarketDataError(ValueError):
    pass


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise MarketDataError("market timestamp is missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketDataError("market timestamp is invalid") from exc
    if result.tzinfo is None:
        raise MarketDataError("market timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def _number(payload: Mapping[str, Any], name: str, low: float, high: float) -> float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarketDataError(f"market field {name} is not numeric")
    value = float(value)
    if not math.isfinite(value) or value < low or value > high:
        raise MarketDataError(f"market field {name} is outside its public range")
    return value


def validate_market(
    payload: Mapping[str, Any],
    *,
    expected_source: str,
    now: datetime,
    maximum_source_age: timedelta = timedelta(minutes=10),
) -> dict[str, Any]:
    allowed = {
        "source",
        "fetched_at",
        "stale",
        "tao_price_usd",
        "tao_change_24h",
        "sn3_alpha_price_tao",
        "sn3_reg_burn_tao",
    }
    if set(payload) - allowed:
        raise MarketDataError("market response has unapproved fields")
    if payload.get("source") != expected_source:
        raise MarketDataError("market response source identity does not match")
    fetched_at = _timestamp(payload.get("fetched_at"))
    current = now.astimezone(timezone.utc)
    if fetched_at > current + timedelta(minutes=1):
        raise MarketDataError("market timestamp is in the future")
    if current - fetched_at > maximum_source_age:
        raise MarketDataError("market response is already stale")
    return {
        "source": expected_source,
        "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
        "stale": False,
        "tao_price_usd": _number(payload, "tao_price_usd", 0.000001, 1_000_000),
        "tao_change_24h": _number(payload, "tao_change_24h", -100, 100_000),
        "sn3_alpha_price_tao": _number(payload, "sn3_alpha_price_tao", 0, 1_000_000),
        "sn3_reg_burn_tao": _number(payload, "sn3_reg_burn_tao", 0, 1_000_000_000),
    }


class MarketClient:
    def __init__(
        self,
        url: str,
        *,
        source: str,
        connect_timeout: float = 2.0,
        response_timeout: float = 4.0,
        transport=None,
    ) -> None:
        if not url.startswith("https://"):
            raise MarketDataError("market endpoint must use HTTPS")
        self.url = url
        self.source = source
        self.timeout = httpx.Timeout(response_timeout, connect=connect_timeout)
        self.transport = transport

    def fetch(self, *, now: datetime) -> dict[str, Any]:
        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            response = client.get(self.url, headers={"Accept": "application/json"})
            response.raise_for_status()
            if len(response.content) > 64 * 1024:
                raise MarketDataError("market response is too large")
            payload = response.json()
        if not isinstance(payload, Mapping):
            raise MarketDataError("market response must be an object")
        return validate_market(payload, expected_source=self.source, now=now)


def select_market(
    fetched: Mapping[str, Any] | None,
    previous: Mapping[str, Any] | None,
    *,
    now: datetime,
    maximum_stale: timedelta = timedelta(hours=1),
) -> dict[str, Any] | None:
    if fetched is not None:
        return dict(fetched)
    if previous is None:
        return None
    try:
        source = previous.get("source")
        if not isinstance(source, str) or not source:
            return None
        selected = validate_market(
            previous,
            expected_source=source,
            now=now,
            maximum_source_age=maximum_stale,
        )
        fetched_at = _timestamp(selected["fetched_at"])
    except MarketDataError:
        return None
    if now.astimezone(timezone.utc) - fetched_at > maximum_stale:
        return None
    selected["stale"] = True
    return selected
