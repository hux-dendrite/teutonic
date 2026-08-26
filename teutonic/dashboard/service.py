from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .contracts import canonical_dashboard_json, canonical_dataset_manifest_json
from .market import select_market

log = logging.getLogger(__name__)


class DashboardViewService:
    def __init__(
        self,
        repository,
        store,
        *,
        market_client=None,
        maximum_market_stale: timedelta = timedelta(hours=1),
    ) -> None:
        self.repository = repository
        self.store = store
        self.market_client = market_client
        self.maximum_market_stale = maximum_market_stale

    def _market(self, previous, *, now: datetime):
        prior_market = previous.get("market") if previous else None
        market = None
        if self.market_client is not None:
            try:
                market = self.market_client.fetch(now=now)
            except Exception as exc:
                log.warning("market fetch unavailable: %s", type(exc).__name__)
        return select_market(
            market,
            prior_market,
            now=now,
            maximum_stale=self.maximum_market_stale,
        )

    def _apply_payouts(self, payload, *, now: datetime) -> None:
        fetch_payouts = getattr(self.market_client, "fetch_payouts", None)
        if not callable(fetch_payouts):
            return
        rows = payload.get("king_chain") or []
        king = payload.get("king") or {}
        hotkeys = list(
            dict.fromkeys(
                row.get("hotkey")
                for row in [*rows, king]
                if isinstance(row.get("hotkey"), str) and row["hotkey"]
            )
        )
        try:
            payouts = fetch_payouts(hotkeys, now=now)
        except Exception as exc:
            log.warning("payout fetch unavailable: %s", type(exc).__name__)
            return

        market = payload.get("market") or {}
        alpha_price_tao = market.get("sn3_alpha_price_tao")
        tao_price_usd = market.get("tao_price_usd")

        def values(hotkey):
            alpha = payouts.get(hotkey)
            usd = (
                alpha * alpha_price_tao * tao_price_usd
                if alpha is not None
                and isinstance(alpha_price_tao, (int, float))
                and isinstance(tao_price_usd, (int, float))
                else None
            )
            return alpha, usd

        for row in rows:
            row["alpha_per_hour"], row["usd_per_hour"] = values(row.get("hotkey"))
        if king:
            alpha, usd = values(king.get("hotkey"))
            payload["king_payout"]["alpha_per_hour"] = alpha
            payload["king_payout"]["usd_per_hour"] = usd

    def publish_once(self, *, now: datetime | None = None):
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        payload = self.repository.project(now=current)
        dataset_manifest = self.repository.project_dataset_manifest(now=current)
        previous = self.store.previous_payload()
        payload["market"] = self._market(previous, now=current)
        self._apply_payouts(payload, now=current)
        dataset_result = self.store.publish_dataset_manifest(
            canonical_dataset_manifest_json(dataset_manifest),
            config_version=dataset_manifest["config_version"],
        )
        body = canonical_dashboard_json(payload)
        dashboard_result = self.store.publish(
            body, source_watermark=payload["source_watermark"]
        )
        return dashboard_result, dataset_result

    def publish_market_only(self, *, now: datetime | None = None):
        """Refresh public market fields while the projection database is unavailable."""
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        previous = self.store.previous_payload()
        if previous is None:
            raise RuntimeError("cannot publish market data without an existing dashboard")
        previous["market"] = self._market(previous, now=current)
        self._apply_payouts(previous, now=current)
        return self.store.publish(
            canonical_dashboard_json(previous),
            source_watermark=previous["source_watermark"],
        )
