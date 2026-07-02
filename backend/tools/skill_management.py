"""Skill management tools."""

from __future__ import annotations

from collections.abc import Callable

from .registry import ToolEntry

StateSetter = Callable[[str, str], None]


def build_set_active_skill_tool(set_state: StateSetter, session_id: str) -> ToolEntry:
    def handler(arguments: dict) -> dict:
        skill_name = str(arguments.get("skill_name") or "").strip()
        if not skill_name:
            return {"success": False, "error": "skill_name is required"}
        set_state(f"{session_id}:active_skill", skill_name)
        return {"success": True, "active_skill": skill_name}

    return ToolEntry(
        name="set_active_skill",
        description="Switch the active skill for the current session.",
        parameters={
            "type": "object",
            "properties": {"skill_name": {"type": "string"}},
            "required": ["skill_name"],
        },
        handler=handler,
        category="skill",
    )
