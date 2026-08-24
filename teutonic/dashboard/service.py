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

    def publish_once(self, *, now: datetime | None = None):
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        payload = self.repository.project(now=current)
        dataset_manifest = self.repository.project_dataset_manifest(now=current)
        previous = self.store.previous_payload()
        payload["market"] = self._market(previous, now=current)
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
        return self.store.publish(
            canonical_dashboard_json(previous),
            source_watermark=previous["source_watermark"],
        )
