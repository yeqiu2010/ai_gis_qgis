"""Default plugin settings."""

PLUGIN_NAME = "AI GIS Agent"
FRONTEND_DIST_DIR = "resources/frontend_dist"
CONFIG_VERSION = 3

DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,
    "llm": {
        "provider": "openai_compatible",
        "model": "",
        "base_url": "",
        "api_key": "",
        "temperature": 0.1,
        "max_tokens": 16384,
        "max_context_tokens": 32768,
        "request_timeout_seconds": 300,
    },
    "database": {
        "path": "~/.qgis_hermes_agent/state.db",
        "max_sessions": 1000,
    },
    "context": {
        "compression_enabled": True,
        "soft_threshold_ratio": 0.55,
        "minimum_output_tokens": 2048,
        "safety_tokens": 256,
        "max_inline_tool_result_chars": 2400,
        "historical_message_limit": 8,
    },
    "executor": {
        "execution_mode": "current_qgis",
        "timeout_seconds": 300,
        "max_memory_mb": 2048,
        "workspace_dir": "~/.qgis_hermes_agent/workspaces",
    },
    "sam3": {
        "enabled": True,
        "base_url": "http://127.0.0.1:8000",
        "api_token": "",
        "connect_timeout_seconds": 10,
        "request_timeout_seconds": 1200,
        "max_upload_mb": 512,
        "max_pixels": 100000000,
        "max_boxes_per_request": 64,
        "health_cache_seconds": 30,
        "verify_tls": True,
        "default_output": "vector",
        "default_rgb_bands": [1, 2, 3],
    },
    "skills": {
        "custom_skills_dir": "~/.qgis_hermes_agent/custom_skills",
        "custom_tools_dir": "~/.qgis_hermes_agent/custom_tools",
    },
    "plugins": {
        "user_dir": "~/.qgis_hermes_agent/plugins",
        "project_dir": "",
        "enabled": [],
        "disabled": [],
        "enable_project_plugins": False,
    },
    "ui": {
        "panel_width": 420,
        "theme": "auto",
        "language": "zh-CN",
    },
}
