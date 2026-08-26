from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import httpx


class MarketDataError(ValueError):
    pass


KEYLESS_MARKET_SOURCE = "coingecko-keyless+bittensor-chain-v1"
BITTENSOR_BLOCK_TIME_SECONDS = 12.0
COINGECKO_PRICE_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bittensor&vs_currencies=usd"
    "&include_24hr_change=true&include_last_updated_at=true"
)


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


class KeylessMarketClient:
    """Combine public TAO market data with live subnet chain state."""

    source = KEYLESS_MARKET_SOURCE

    def __init__(
        self,
        *,
        netuid: int,
        network: str = "finney",
        refresh_interval: timedelta = timedelta(minutes=1),
        connect_timeout: float = 2.0,
        response_timeout: float = 4.0,
        block_time_seconds: float = BITTENSOR_BLOCK_TIME_SECONDS,
        transport=None,
        subtensor=None,
    ) -> None:
        if netuid < 0:
            raise MarketDataError("market netuid cannot be negative")
        if refresh_interval < timedelta(seconds=20):
            raise MarketDataError("keyless market refresh interval cannot be below 20 seconds")
        if not math.isfinite(block_time_seconds) or block_time_seconds <= 0:
            raise MarketDataError("Bittensor block time must be positive")
        self.netuid = netuid
        self.network = network
        self.refresh_interval = refresh_interval
        self.timeout = httpx.Timeout(response_timeout, connect=connect_timeout)
        self.block_time_seconds = float(block_time_seconds)
        self.transport = transport
        self._subtensor = subtensor
        self._owns_subtensor = subtensor is None
        self._cached: dict[str, Any] | None = None
        self._cached_at: datetime | None = None
        self._payouts_cached: dict[str, float] | None = None
        self._payouts_cached_at: datetime | None = None

    def _chain(self):
        if self._subtensor is None:
            import bittensor as bt

            self._subtensor = bt.Subtensor(network=self.network)
        return self._subtensor

    @staticmethod
    def _tao(value: Any, name: str) -> float:
        if value is None:
            raise MarketDataError(f"chain market field {name} is unavailable")
        raw = getattr(value, "tao", value)
        try:
            result = float(raw)
        except (TypeError, ValueError) as exc:
            raise MarketDataError(f"chain market field {name} is invalid") from exc
        if not math.isfinite(result) or result < 0:
            raise MarketDataError(f"chain market field {name} is invalid")
        return result

    def fetch(self, *, now: datetime) -> dict[str, Any]:
        current = now.astimezone(timezone.utc)
        if (
            self._cached is not None
            and self._cached_at is not None
            and current - self._cached_at < self.refresh_interval
        ):
            return dict(self._cached)

        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            response = client.get(COINGECKO_PRICE_URL, headers={"Accept": "application/json"})
            response.raise_for_status()
            if len(response.content) > 64 * 1024:
                raise MarketDataError("CoinGecko response is too large")
            response_payload = response.json()
        if not isinstance(response_payload, Mapping):
            raise MarketDataError("CoinGecko response must be an object")
        tao = response_payload.get("bittensor")
        if not isinstance(tao, Mapping):
            raise MarketDataError("CoinGecko response has no Bittensor quote")
        updated_at = tao.get("last_updated_at")
        if isinstance(updated_at, bool) or not isinstance(updated_at, (int, float)):
            raise MarketDataError("CoinGecko response has no valid update time")
        fetched_at = datetime.fromtimestamp(float(updated_at), tz=timezone.utc)

        try:
            subtensor = self._chain()
            alpha_price = self._tao(
                subtensor.get_subnet_price(self.netuid), "sn3_alpha_price_tao"
            )
            registration = self._tao(subtensor.recycle(self.netuid), "sn3_reg_burn_tao")
        except Exception:
            if self._owns_subtensor:
                self._subtensor = None
            raise

        payload = validate_market(
            {
                "source": self.source,
                "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
                "stale": False,
                "tao_price_usd": tao.get("usd"),
                "tao_change_24h": tao.get("usd_24h_change"),
                "sn3_alpha_price_tao": alpha_price,
                "sn3_reg_burn_tao": registration,
            },
            expected_source=self.source,
            now=current,
        )
        self._cached = payload
        self._cached_at = current
        return dict(payload)

    def fetch_payouts(self, hotkeys: list[str], *, now: datetime) -> dict[str, float]:
        """Estimate hourly alpha earnings from each hotkey's last tempo emission."""
        current = now.astimezone(timezone.utc)
        if (
            self._payouts_cached is not None
            and self._payouts_cached_at is not None
            and current - self._payouts_cached_at < self.refresh_interval
        ):
            return {
                hotkey: self._payouts_cached[hotkey]
                for hotkey in hotkeys
                if hotkey in self._payouts_cached
            }

        try:
            info = self._chain().get_metagraph_info(self.netuid)
            tempo = int(getattr(info, "tempo", 0))
            chain_hotkeys = list(getattr(info, "hotkeys", ()))
            emissions = list(getattr(info, "emission", ()))
        except Exception:
            if self._owns_subtensor:
                self._subtensor = None
            raise
        if tempo <= 0:
            raise MarketDataError("SN3 tempo is unavailable")
        if len(chain_hotkeys) != len(emissions):
            raise MarketDataError("SN3 hotkey and emission counts do not match")

        hours_per_tempo = tempo * self.block_time_seconds / 3600.0
        payouts = {
            str(hotkey): self._tao(emission, "uid_alpha_emission") / hours_per_tempo
            for hotkey, emission in zip(chain_hotkeys, emissions, strict=True)
        }
        self._payouts_cached = payouts
        self._payouts_cached_at = current
        return {hotkey: payouts[hotkey] for hotkey in hotkeys if hotkey in payouts}


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
