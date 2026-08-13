from __future__ import annotations

import json

from ai_gis_qgis.config.settings import SettingsManager


def test_load_migrates_legacy_model_output_budget(monkeypatch):
    manager = SettingsManager()
    monkeypatch.setattr(
        manager,
        "_read_raw_values",
        lambda: [json.dumps({"llm": {"max_tokens": 8192}})],
    )

    config = manager.load()

    assert config["config_version"] == 3
    assert config["llm"]["max_tokens"] == 16384


def test_load_preserves_explicit_budget_from_current_config(monkeypatch):
    manager = SettingsManager()
    monkeypatch.setattr(
        manager,
        "_read_raw_values",
        lambda: [
            json.dumps(
                {
                    "config_version": 3,
                    "llm": {"max_tokens": 8192},
                }
            )
        ],
    )

    config = manager.load()

    assert config["llm"]["max_tokens"] == 8192


def test_sam3_defaults_and_url_normalization(monkeypatch):
    manager = SettingsManager()
    monkeypatch.setattr(
        manager,
        "_read_raw_values",
        lambda: [json.dumps({"sam3": {"base_url": "http://127.0.0.1:8000/"}})],
    )

    config = manager.load()

    assert config["sam3"]["base_url"] == "http://127.0.0.1:8000"
    assert config["sam3"]["request_timeout_seconds"] == 1200
    assert config["sam3"]["default_rgb_bands"] == [1, 2, 3]


def test_sam3_invalid_url_is_rejected(tmp_path, monkeypatch):
    manager = SettingsManager()
    monkeypatch.setattr("ai_gis_qgis.config.settings.FALLBACK_SETTINGS_PATH", tmp_path / "settings.json")

    try:
        manager.save({"sam3": {"base_url": "file:///tmp/model"}})
    except ValueError as exc:
        assert "http/https" in str(exc)
    else:
        raise AssertionError("invalid SAM3 URL was accepted")
