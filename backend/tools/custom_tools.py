"""Load user custom ToolEntry objects from Python modules."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from .registry import ToolEntry


def load_custom_tool_entries(custom_tools_dir: Path | str | None = None) -> list[ToolEntry]:
    directory = Path(custom_tools_dir or Path.home() / ".qgis_hermes_agent" / "custom_tools")
    if not directory.exists():
        return []
    entries: list[ToolEntry] = []
    for path in sorted(directory.glob("*.py")):
        entries.extend(_load_entries_from_module(path))
    return entries


def _load_entries_from_module(path: Path) -> list[ToolEntry]:
    module_name = f"qgis_agent_custom_tool_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    entries: list[ToolEntry] = []
    for factory_name in ("build_tools", "build_tool"):
        factory = getattr(module, factory_name, None)
        if not callable(factory):
            continue
        value = factory()
        entries.extend(_coerce_entries(value))
    return entries


def _coerce_entries(value: Any) -> list[ToolEntry]:
    if isinstance(value, ToolEntry):
        return [value]
    if isinstance(value, list | tuple):
        return [entry for entry in value if isinstance(entry, ToolEntry)]
    return []

