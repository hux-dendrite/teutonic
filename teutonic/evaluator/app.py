"""Protocol-v2 evaluator ASGI application with multiple live NPY sources.

Importing :mod:`teutonic.evaluator.sources` installs the sampling hooks on the
protocol-v2 evaluator.  The HTTP application itself is exported unchanged so
caller-owned evaluation IDs, immutable R2 artifacts, and v2 validation remain
the only production API.

Example:
    uvicorn teutonic.evaluator.app:app --host 127.0.0.1 --port 9000
"""

from __future__ import annotations

import os

from teutonic.evaluator import engine as base
from teutonic.evaluator import sources  # noqa: F401 — registers sampling hooks

app = base.app


if __name__ == "__main__":
    import uvicorn

    base.setup_logging()
    host = os.environ.get("EVAL_HOST", "127.0.0.1")
    port = int(os.environ.get("EVAL_PORT", "9000"))
    uvicorn.run(app, host=host, port=port, log_level="info")
