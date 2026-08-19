#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export EVAL_HOST="${EVAL_HOST:-127.0.0.1}"
export EVAL_PORT="${EVAL_PORT:-9000}"

if ! command -v rclone >/dev/null 2>&1; then
  echo "rclone is required for evaluator model downloads" >&2
  exit 1
fi

if [[ -n "${TEUTONIC_PYTHON:-}" ]]; then
  if [[ "$TEUTONIC_PYTHON" = /* ]]; then
    PYTHON_BIN="$TEUTONIC_PYTHON"
  else
    PYTHON_BIN="$REPO_DIR/$TEUTONIC_PYTHON"
  fi
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "TEUTONIC_PYTHON is not executable: $PYTHON_BIN" >&2
    exit 1
  fi
  exec "$PYTHON_BIN" -m uvicorn teutonic.evaluator.app:app \
    --host "$EVAL_HOST" --port "$EVAL_PORT" --log-level "${LOG_LEVEL:-info}"
fi

if [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
  exec "$REPO_DIR/.venv/bin/python" -m uvicorn teutonic.evaluator.app:app \
    --host "$EVAL_HOST" --port "$EVAL_PORT" --log-level "${LOG_LEVEL:-info}"
fi

exec uv run python -m uvicorn teutonic.evaluator.app:app \
  --host "$EVAL_HOST" --port "$EVAL_PORT" --log-level "${LOG_LEVEL:-info}"
