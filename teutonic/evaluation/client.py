from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx


HEALTH_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class EvaluatorBusyError(RuntimeError):
    pass


class EvaluatorConflictError(RuntimeError):
    pass


class EvaluatorJobNotFoundError(RuntimeError):
    pass


class HttpEvaluatorClient:
    """Small transport boundary around the legacy evaluator HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | float = httpx.Timeout(2700.0, connect=30.0),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout
        self._owns_client = client is None

    async def __aenter__(self) -> HttpEvaluatorClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpEvaluatorClient must be used as an async context manager")
        return self._client

    async def health(self) -> dict[str, Any]:
        response = await self._require_client().get(
            f"{self.base_url}/health", timeout=HEALTH_TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    async def start_attempt(self, request: Mapping[str, Any]) -> dict[str, Any]:
        response = await self._require_client().post(f"{self.base_url}/eval", json=dict(request))
        if response.status_code == 409:
            detail = response.json().get("detail", {})
            code = detail.get("code") if isinstance(detail, dict) else ""
            if code == "attempt_conflict":
                raise EvaluatorConflictError(
                    "evaluation attempt conflicts with its original request"
                )
            raise EvaluatorBusyError("evaluator is busy")
        response.raise_for_status()
        payload = response.json()
        eval_id = payload.get("eval_id")
        if not isinstance(eval_id, str) or not eval_id:
            raise RuntimeError("evaluator start response omitted eval_id")
        return payload

    async def start(self, request: Mapping[str, Any]) -> str:
        return (await self.start_attempt(request))["eval_id"]

    async def status(self, eval_id: str) -> dict[str, Any]:
        response = await self._require_client().get(f"{self.base_url}/eval/{eval_id}")
        if response.status_code == 404:
            raise EvaluatorJobNotFoundError(eval_id)
        response.raise_for_status()
        return response.json()

    def stream(self, eval_id: str, *, timeout: httpx.Timeout | float | None = None):
        return self._require_client().stream(
            "GET",
            f"{self.base_url}/eval/{eval_id}/stream",
            timeout=self._timeout if timeout is None else timeout,
        )

    async def events(self, eval_id: str) -> AsyncIterator[dict[str, Any]]:
        async with self.stream(eval_id) as response:
            if response.status_code == 404:
                raise EvaluatorJobNotFoundError(eval_id)
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])
