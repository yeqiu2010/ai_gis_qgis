from __future__ import annotations

import json

from ai_gis_qgis.config.settings import SettingsManager
from ai_gis_qgis.qwebengine.rpc_controller import RPCController


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


def test_settings_save_roundtrip_survives_manager_recreation(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr("ai_gis_qgis.config.settings.FALLBACK_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(
        "ai_gis_qgis.config.settings.BUNDLED_SETTINGS_PATH",
        tmp_path / "missing-bundled-settings.json",
    )
    manager = SettingsManager()
    manager._qsettings = None

    saved = manager.save(
        {
            "llm": {
                "provider": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen-test",
                "max_tokens": 24576,
            },
            "sam3": {"base_url": "http://127.0.0.1:9000"},
        }
    )

    reopened = SettingsManager()
    reopened._qsettings = None
    loaded = reopened.load()
    assert saved["settings_revision"] > 0
    assert loaded["llm"]["provider"] == "ollama"
    assert loaded["llm"]["model"] == "qwen-test"
    assert loaded["llm"]["max_tokens"] == 24576
    assert loaded["sam3"]["base_url"] == "http://127.0.0.1:9000"


def test_newest_settings_revision_wins_over_stale_later_source(monkeypatch):
    manager = SettingsManager()
    monkeypatch.setattr(
        manager,
        "_read_raw_values",
        lambda: [
            json.dumps(
                {
                    "config_version": 3,
                    "settings_revision": 200,
                    "llm": {"model": "new-model"},
                }
            ),
            json.dumps(
                {
                    "config_version": 3,
                    "settings_revision": 100,
                    "llm": {"model": "stale-model"},
                }
            ),
        ],
    )

    assert manager.load()["llm"]["model"] == "new-model"


def test_partial_save_preserves_unrelated_plugin_settings(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr("ai_gis_qgis.config.settings.FALLBACK_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(
        "ai_gis_qgis.config.settings.BUNDLED_SETTINGS_PATH",
        tmp_path / "missing-bundled-settings.json",
    )
    manager = SettingsManager()
    manager._qsettings = None
    manager.save({"plugins": {"enabled": ["sample-plugin"]}})

    saved = manager.save({"llm": {"model": "updated-model"}})

    assert saved["plugins"]["enabled"] == ["sample-plugin"]
    assert saved["llm"]["model"] == "updated-model"


def test_rpc_save_then_get_returns_persisted_values(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr("ai_gis_qgis.config.settings.FALLBACK_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(
        "ai_gis_qgis.config.settings.BUNDLED_SETTINGS_PATH",
        tmp_path / "missing-bundled-settings.json",
    )
    manager = SettingsManager()
    manager._qsettings = None
    controller = object.__new__(RPCController)
    controller.settings = manager
    controller.config = manager.load()
    controller.agent_core = object()
    controller._create_agent_core = lambda *args, **kwargs: object()

    submitted = controller.get_settings({})
    submitted["llm"]["model"] = "persisted-through-rpc"
    submitted["llm"]["max_tokens"] = 32768
    controller.save_settings(submitted)

    reopened = controller.get_settings({})
    assert reopened["llm"]["model"] == "persisted-through-rpc"
    assert reopened["llm"]["max_tokens"] == 32768
    disk_manager = SettingsManager()
    disk_manager._qsettings = None
    assert disk_manager.load()["llm"]["model"] == "persisted-through-rpc"
