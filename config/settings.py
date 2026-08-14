"""Settings helpers with QGIS and non-QGIS fallbacks."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .defaults import CONFIG_VERSION, DEFAULT_CONFIG

SETTINGS_KEY = "qgis_hermes_agent/config"
FALLBACK_SETTINGS_PATH = Path.home() / ".qgis_hermes_agent" / "settings.json"
BUNDLED_SETTINGS_PATH = Path(__file__).with_name("settings.json")


class SettingsManager:
    def __init__(self):
        self._qsettings = self._create_qsettings()

    def load(self) -> dict[str, Any]:
        config = copy.deepcopy(DEFAULT_CONFIG)
        loaded_config_version = 0
        candidates: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
        for priority, stored in enumerate(self._read_raw_values()):
            if stored:
                try:
                    parsed = json.loads(stored)
                    if not isinstance(parsed, dict):
                        continue
                    revision = int(parsed.get("settings_revision") or 0)
                    # A revisioned value always wins over legacy values. For
                    # equally old values, _read_raw_values source priority is
                    # used as the compatibility tiebreaker.
                    candidates.append(((1 if revision else 0, revision, priority), parsed))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
        for _, parsed in sorted(candidates, key=lambda item: item[0]):
            self._deep_update(config, parsed)
            loaded_config_version = int(parsed.get("config_version") or 0)
        if loaded_config_version < CONFIG_VERSION:
            legacy_max_tokens = int(config.get("llm", {}).get("max_tokens") or 0)
            if legacy_max_tokens in {4096, 8192}:
                config["llm"]["max_tokens"] = DEFAULT_CONFIG["llm"]["max_tokens"]
        config["config_version"] = CONFIG_VERSION
        self._normalize(config)
        return config

    def save(self, config: dict[str, Any]) -> dict[str, Any]:
        # Merge partial settings updates over the latest persisted state, not
        # only over defaults. This prevents one settings page from resetting
        # keys owned by another page or Plugin.
        merged = self.load()
        self._deep_update(merged, config)
        self._normalize(merged)
        merged["config_version"] = CONFIG_VERSION
        merged["settings_revision"] = time.time_ns()
        raw = json.dumps(merged, ensure_ascii=False, indent=2)
        if self._qsettings is not None:
            self._qsettings.setValue(SETTINGS_KEY, raw)
            sync = getattr(self._qsettings, "sync", None)
            if callable(sync):
                sync()
        FALLBACK_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(FALLBACK_SETTINGS_PATH, raw)
        # Read through the same precedence path used on the next dialog open
        # and application restart. Returning this verified copy also exposes a
        # persistence mismatch to the caller immediately.
        return self.load()

    def _normalize(self, config: dict[str, Any]) -> None:
        plugins = config.setdefault("plugins", {})
        for key in ("enabled", "disabled"):
            values = plugins.get(key) or []
            if not isinstance(values, list):
                raise ValueError(f"plugins.{key} 必须是字符串数组。")
            plugins[key] = [str(value).strip() for value in values if str(value).strip()]
        plugins["user_dir"] = str(
            plugins.get("user_dir") or "~/.qgis_hermes_agent/plugins"
        )
        plugins["project_dir"] = str(plugins.get("project_dir") or "")
        plugins["enable_project_plugins"] = bool(
            plugins.get("enable_project_plugins", False)
        )
        sam3 = config.setdefault("sam3", {})
        base_url = str(sam3.get("base_url") or "").strip().rstrip("/")
        if base_url:
            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("SAM3 Base URL 必须是有效的 http/https 地址。")
        sam3["base_url"] = base_url
        bands = sam3.get("default_rgb_bands") or [1, 2, 3]
        if not isinstance(bands, list) or len(bands) != 3:
            raise ValueError("SAM3 default_rgb_bands 必须包含 3 个波段编号。")
        sam3["default_rgb_bands"] = [int(value) for value in bands]
        for key, minimum in (
            ("connect_timeout_seconds", 1),
            ("request_timeout_seconds", 1),
            ("max_upload_mb", 1),
            ("max_pixels", 1),
            ("max_boxes_per_request", 1),
            ("health_cache_seconds", 0),
        ):
            value = int(sam3.get(key) or 0)
            if value < minimum:
                raise ValueError(f"SAM3 {key} 必须大于等于 {minimum}。")
            sam3[key] = value

    def _read_raw_values(self) -> list[str]:
        values = []
        # Legacy unversioned precedence: bundled < QSettings < user file.
        bundled_raw = self._read_bundled_raw()
        if bundled_raw:
            values.append(bundled_raw)
        qsettings_raw = self._read_qsettings_raw()
        if qsettings_raw:
            values.append(qsettings_raw)
        file_raw = self._read_file_raw()
        if file_raw:
            values.append(file_raw)
        return values

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                temporary_path = Path(temporary_name)
                if temporary_path.exists():
                    temporary_path.unlink()

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
