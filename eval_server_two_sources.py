#!/usr/bin/env python3
""":"
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then exec .venv/bin/python "$0" "$@"; fi
exec uv run python "$0" "$@"
":"""
from __future__ import annotations
"""Protocol-v2 Quasar evaluator with multiple live .npy data sources.

Importing :mod:`npy_sources` installs the multi-source sampling hooks on the
protocol-v2 evaluator.  The HTTP application itself is exported unchanged so
caller-owned evaluation IDs, immutable R2 artifacts, and v2 validation remain
the only production API.

Example:
    uvicorn eval_server_two_sources:app --host 127.0.0.1 --port 9011
"""

import os

import eval_server_quasar_pair as base
import npy_sources  # noqa: F401 — registers monkey-patches on import

app = base.app


if __name__ == "__main__":
    import uvicorn

    base.setup_logging()
    host = os.environ.get("EVAL_HOST", "127.0.0.1")
    port = int(os.environ.get("EVAL_PORT", "9000"))
    uvicorn.run(app, host=host, port=port, log_level="info")
