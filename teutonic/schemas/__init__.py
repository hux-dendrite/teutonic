"""Packaged SQL and JSON schema locations."""

from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent
CONTROL_PLANE_SCHEMA_PATH = SCHEMA_DIR / "control_plane.sql"

__all__ = ["CONTROL_PLANE_SCHEMA_PATH", "SCHEMA_DIR"]
