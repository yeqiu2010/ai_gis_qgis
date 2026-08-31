"""Database constants."""

from __future__ import annotations

from pathlib import Path

APP_STATE_DIR = Path.home() / ".qgis_hermes_agent"
DEFAULT_DB_PATH = APP_STATE_DIR / "state.db"
SCHEMA_VERSION = 7
