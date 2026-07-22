"""Minimal tool registry."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolEntry:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    category: str
    toolset: str = "default"
    requires_confirmation: bool = False
    destructive: bool = False
    writes_project: bool = False
    preflight: ToolHandler | None = None

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self._skill_tools: dict[str, set[str]] = {}

    def register(self, entry: ToolEntry) -> None:
        self._tools[entry.name] = entry

    def set_skill_tools(self, skill_tools: dict[str, list[str]]) -> None:
        self._skill_tools = {
            str(skill_name): {str(tool_name) for tool_name in tool_names}
            for skill_name, tool_names in skill_tools.items()
            if tool_names
        }

    def get(self, name: str) -> ToolEntry:
        if name not in self._tools:
            raise KeyError(f"Tool not registered: {name}")
        return self._tools[name]

    def definitions_for_skill(self, skill_name: str) -> list[dict[str, Any]]:
        allowed = self._skill_tools.get(skill_name)
        if allowed is None:
            return [entry.definition() for entry in self._tools.values()]
        allowed = set(allowed)
        allowed.add("set_active_skill")
        return [
            entry.definition()
            for entry in self._tools.values()
            if entry.name in allowed
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        entry = self.get(name)
        started = time.monotonic()
        try:
            result = entry.handler(arguments)
            if "success" not in result:
                result = {"success": True, **result}
            result = make_json_safe(result)
        except Exception as exc:
            result = {"success": False, "error": str(exc)}
        duration_ms = int((time.monotonic() - started) * 1000)
        return result, duration_ms


def make_json_safe(value: Any) -> Any:
    """Convert QGIS/PyQt values into JSON-serializable Python values."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(make_json_safe(key)): make_json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [make_json_safe(item) for item in value]

    converted, handled = _convert_qvariant_like(value)
    if handled:
        return make_json_safe(converted)

    return str(value)


def _convert_qvariant_like(value: Any) -> tuple[Any, bool]:
    type_name = type(value).__name__.lower()
    module_name = type(value).__module__.lower()
    looks_qt_value = "qvariant" in type_name or "pyqt" in module_name or "qgis" in module_name
    if not looks_qt_value:
        return value, False

    for null_method in ("isNull", "isValid"):
        method = getattr(value, null_method, None)
        if callable(method):
            try:
                state = bool(method())
            except Exception:
                continue
            if null_method == "isNull" and state:
                return None, True
            if null_method == "isValid" and not state:
                return None, True

    for method_name in ("toPyObject", "value", "toString"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            converted = method()
        except Exception:
            continue
        if converted is not value:
            return converted, True
    return str(value), True
