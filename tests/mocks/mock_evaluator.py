#!/usr/bin/env python3
"""Test-only protocol-v2 evaluator used for local control-plane runs."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from teutonic.evaluation import (
    AttemptBusyError,
    AttemptConflictError,
    EvaluationAttemptRegistry,
    EvaluationRequestV2,
    ProtocolValidationError,
    result_provenance,
)


log = logging.getLogger("teutonic.mock-evaluator")
HOST = os.environ.get("TEUTONIC_MOCK_EVAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("TEUTONIC_MOCK_EVAL_PORT", "9000"))
STEPS = int(os.environ.get("TEUTONIC_MOCK_EVAL_STEPS", "8"))
STEP_SECONDS = float(os.environ.get("TEUTONIC_MOCK_EVAL_STEP_SECONDS", "0.5"))
EVALUATOR_VERSION = "pair-evaluator-v2"
SOFTWARE_VERSION = "mock-evaluator-v1"

_attempts = EvaluationAttemptRegistry()
_jobs: dict[str, "MockJob"] = {}
_jobs_lock = threading.Lock()
_eval_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MockJob:
    request: EvaluationRequestV2
    events: list[dict[str, Any]] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        event = {
            "protocol_version": "teutonic-evaluator-v2",
            "eval_id": self.request.eval_id,
            "evaluation_id": self.request.evaluation_id,
            "attempt_number": self.request.attempt_number,
            "type": event_type,
            "data": data,
        }
        with self.condition:
            self.events.append(event)
            self.condition.notify_all()


def _losses(request: EvaluationRequestV2, step: int) -> tuple[float, float]:
    seed = int(request.request_sha256[:16], 16)
    phase = (seed % 10_000) / 10_000
    progress = step / STEPS
    base = 2.45 + ((seed >> 16) % 200) / 1000
    # Running loss is noisy in practice: preserve visible bumps while making
    # the end-to-end trend unambiguously lower.
    cycle_noise = {0: 0.022, 1: 0.002, 2: -0.009}[step % 3]
    noise = cycle_noise + 0.003 * math.sin((step + phase) * 1.73)
    king = base - 0.18 * progress + noise
    delta = float(request.limits["delta_threshold"])
    improvement = delta + 0.0012 + 0.00035 * math.sin((step + phase) * 1.11)
    challenger = king - improvement + 0.0025 * math.sin((step + phase) * 2.31)
    return round(king, 6), round(challenger, 6)


def _run_job(job: MockJob) -> None:
    request = job.request
    record = _attempts.get(request.eval_id)
    if record is None:
        return
    started_at = utc_now()
    started = time.monotonic()
    requested = int(request.limits["n"])
    try:
        record.state = "running"
        for step in range(1, STEPS + 1):
            king_loss, challenger_loss = _losses(request, step)
            completed = min(requested, max(1, round(requested * step / STEPS)))
            progress = {
                "phase": "scoring",
                "completed_sequences": completed,
                "requested_sequences": requested,
                "avg_king_loss": king_loss,
                "avg_challenger_loss": challenger_loss,
                "progress": round(step / STEPS, 4),
            }
            record.progress = progress
            job.publish("progress", progress)
            if STEP_SECONDS:
                time.sleep(STEP_SECONDS)

        king_loss, challenger_loss = _losses(request, STEPS)
        mu_hat = round(king_loss - challenger_loss, 6)
        uncertainty = 0.00045 + random.Random(request.request_sha256).random() * 0.00025
        lcb = round(mu_hat - uncertainty, 6)
        threshold = float(request.limits["delta_threshold"])
        accepted = lcb > threshold
        result = result_provenance(
            request,
            started_at=started_at,
            completed_at=utc_now(),
            requested_sequences=requested,
            completed_sequences=requested,
            early_stopped=False,
            hardware={"worker": "local-mock", "accelerator": "simulated-gpu"},
        )
        result.update(
            {
                "accepted": accepted,
                "verdict": "challenger" if accepted else "king",
                "mu_hat": mu_hat,
                "lcb": lcb,
                "delta_threshold": threshold,
                "avg_king_loss": king_loss,
                "avg_challenger_loss": challenger_loss,
                "wall_time_s": round(time.monotonic() - started, 3),
            }
        )
        artifact = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        result["result_artifact_sha256"] = hashlib.sha256(artifact).hexdigest()
        record.state = "completed"
        record.verdict = result
        job.publish("verdict", result)
        log.info(
            "completed eval=%s accepted=%s king_loss=%.6f challenger_loss=%.6f",
            request.eval_id,
            accepted,
            king_loss,
            challenger_loss,
        )
    except Exception as exc:
        record.state = "failed"
        record.error = type(exc).__name__
        record.reason = str(exc)
        job.publish("error", {"code": record.error, "reason": record.reason})
        log.exception("mock evaluation failed eval=%s", request.eval_id)
    finally:
        _eval_lock.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TeutonicMockEvaluator/1"

    def log_message(self, format: str, *args: Any) -> None:
        log.info("http " + format, *args)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _record(self, eval_id: str):
        record = _attempts.get(eval_id)
        if record is None:
            self._json(HTTPStatus.NOT_FOUND, {"detail": "eval not found"})
        return record

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if path == "/health":
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "mock": True,
                    "protocol_version": "teutonic-evaluator-v2",
                    "versions": {
                        "evaluator": EVALUATOR_VERSION,
                        "code": SOFTWARE_VERSION,
                    },
                    "active_evals": _attempts.active_count(),
                },
            )
            return
        if not path.startswith("/eval/"):
            self._json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        tail = path.removeprefix("/eval/")
        streaming = tail.endswith("/stream")
        eval_id = tail.removesuffix("/stream") if streaming else tail
        record = self._record(eval_id)
        if record is None:
            return
        if streaming:
            self._stream(eval_id)
            return
        self._json(
            HTTPStatus.OK,
            {
                **record.response(),
                "progress": record.progress,
                "verdict": record.verdict,
                "error": record.error,
                "reason": record.reason,
            },
        )

    def _stream(self, eval_id: str) -> None:
        with _jobs_lock:
            job = _jobs.get(eval_id)
        if job is None:
            self._json(HTTPStatus.NOT_FOUND, {"detail": "eval not found"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        index = 0
        while True:
            with job.condition:
                while index >= len(job.events):
                    record = _attempts.get(eval_id)
                    if record is None or record.state in {"completed", "failed"}:
                        return
                    job.condition.wait(timeout=1)
                event = job.events[index]
                index += 1
            try:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if event["type"] in {"verdict", "error"}:
                return

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/eval":
            self._json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            request = EvaluationRequestV2.from_mapping(payload)
            record, duplicate = _attempts.start(
                request,
                created_at=time.time(),
                admit_new=lambda: _eval_lock.acquire(blocking=False),
            )
        except (json.JSONDecodeError, ProtocolValidationError, ValueError) as exc:
            self._json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"detail": {"code": "invalid_protocol_v2_request", "message": str(exc)}},
            )
            return
        except AttemptConflictError as exc:
            self._json(
                HTTPStatus.CONFLICT,
                {"detail": {"code": "attempt_conflict", "message": str(exc)}},
            )
            return
        except AttemptBusyError as exc:
            self._json(
                HTTPStatus.CONFLICT,
                {"detail": {"code": "evaluator_busy", "message": str(exc)}},
            )
            return

        if not duplicate:
            job = MockJob(request)
            with _jobs_lock:
                _jobs[request.eval_id] = job
            threading.Thread(
                target=_run_job,
                args=(job,),
                daemon=True,
                name=f"mock-eval-{request.eval_id}",
            ).start()
        self._json(HTTPStatus.OK, record.response(duplicate=duplicate))


def main() -> int:
    if STEPS < 2 or STEP_SECONDS < 0:
        raise ValueError("mock evaluator requires at least two steps and non-negative delay")
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    def stop(_signum, _frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    log.info("mock evaluator listening host=%s port=%d steps=%d", HOST, PORT, STEPS)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
