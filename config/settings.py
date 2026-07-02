"""Settings helpers with QGIS and non-QGIS fallbacks."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .defaults import DEFAULT_CONFIG

SETTINGS_KEY = "qgis_hermes_agent/config"
FALLBACK_SETTINGS_PATH = Path.home() / ".qgis_hermes_agent" / "settings.json"
BUNDLED_SETTINGS_PATH = Path(__file__).with_name("settings.json")


class SettingsManager:
    def __init__(self):
        self._qsettings = self._create_qsettings()

    def load(self) -> dict[str, Any]:
        config = copy.deepcopy(DEFAULT_CONFIG)
        for stored in self._read_raw_values():
            if stored:
                try:
                    self._deep_update(config, json.loads(stored))
                except json.JSONDecodeError:
                    pass
        return config

    def save(self, config: dict[str, Any]) -> dict[str, Any]:
        merged = copy.deepcopy(DEFAULT_CONFIG)
        self._deep_update(merged, config)
        raw = json.dumps(merged, ensure_ascii=False, indent=2)
        if self._qsettings is not None:
            self._qsettings.setValue(SETTINGS_KEY, raw)
        FALLBACK_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        FALLBACK_SETTINGS_PATH.write_text(raw, encoding="utf-8")
        return merged

    def _read_raw_values(self) -> list[str]:
        values = []
        qsettings_raw = self._read_qsettings_raw()
        if qsettings_raw:
            values.append(qsettings_raw)
        bundled_raw = self._read_bundled_raw()
        if bundled_raw:
            values.append(bundled_raw)
        file_raw = self._read_file_raw()
        if file_raw:
            values.append(file_raw)
        return values

    def _read_bundled_raw(self) -> str | None:
        if BUNDLED_SETTINGS_PATH.exists():
            return BUNDLED_SETTINGS_PATH.read_text(encoding="utf-8")
        return None

    def _read_file_raw(self) -> str | None:
        if FALLBACK_SETTINGS_PATH.exists():
            return FALLBACK_SETTINGS_PATH.read_text(encoding="utf-8")
        return None

    def _read_qsettings_raw(self) -> str | None:
        if self._qsettings is not None:
            value = self._qsettings.value(SETTINGS_KEY, "")
            return str(value) if value else None
        return None

    def _create_qsettings(self):
        try:
            from qgis.PyQt.QtCore import QSettings
        except Exception:
            return None
        return QSettings()

    def _deep_update(self, target: dict[str, Any], updates: dict[str, Any]) -> None:
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_update(target[key], value)
            else:
                target[key] = value
