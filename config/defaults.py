"""Default plugin settings."""

PLUGIN_NAME = "AI GIS Agent"
FRONTEND_DIST_DIR = "resources/frontend_dist"

DEFAULT_CONFIG = {
    "llm": {
        "provider": "openai_compatible",
        "model": "",
        "base_url": "",
        "api_key": "",
        "temperature": 0.1,
        "max_tokens": 4096,
    },
    "database": {
        "path": "~/.qgis_hermes_agent/state.db",
        "max_sessions": 1000,
    },
    "executor": {
        "execution_mode": "current_qgis",
        "timeout_seconds": 300,
        "max_memory_mb": 2048,
        "workspace_dir": "~/.qgis_hermes_agent/workspaces",
    },
    "ui": {
        "panel_width": 420,
        "theme": "auto",
        "language": "zh-CN",
    },
}
