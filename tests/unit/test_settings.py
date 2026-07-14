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

    assert config["config_version"] == 2
    assert config["llm"]["max_tokens"] == 16384


def test_load_preserves_explicit_budget_from_current_config(monkeypatch):
    manager = SettingsManager()
    monkeypatch.setattr(
        manager,
        "_read_raw_values",
        lambda: [
            json.dumps(
                {
                    "config_version": 2,
                    "llm": {"max_tokens": 8192},
                }
            )
        ],
    )

    config = manager.load()

    assert config["llm"]["max_tokens"] == 8192
