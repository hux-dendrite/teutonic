from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "dashboard-v1.schema.json"


class DashboardContractError(ValueError):
    pass


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _reject_non_finite(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise DashboardContractError(f"non-finite number at {path}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_non_finite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_finite(item, f"{path}[{index}]")


def validate_dashboard(payload: Mapping[str, Any]) -> None:
    _reject_non_finite(payload)
    errors = sorted(_validator().iter_errors(payload), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
        )
        raise DashboardContractError(f"dashboard-v1 invalid at {location}: {error.message}")


def canonical_dashboard_json(payload: Mapping[str, Any]) -> bytes:
    validate_dashboard(payload)
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return text.encode("utf-8", errors="strict")
    except (UnicodeError, ValueError) as exc:
        raise DashboardContractError("dashboard cannot be serialized as strict UTF-8 JSON") from exc
