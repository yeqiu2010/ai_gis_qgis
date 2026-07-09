"""Skill management tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..skills.skill_manager import SkillManager
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


def build_search_skills_tool(skill_manager: SkillManager) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip().lower()
        include_builtin = bool(arguments.get("include_builtin", True))
        limit = max(1, min(int(arguments.get("limit") or 8), 20))
        scored = []
        for document in skill_manager.all().values():
            is_custom = ".qgis_hermes_agent" in str(document.path)
            if not include_builtin and not is_custom:
                continue
            haystack = " ".join(
                [
                    document.name,
                    document.description,
                    " ".join(document.tags),
                    document.body[:1200],
                ]
            ).lower()
            score = 0
            for token in [part for part in query.split() if part]:
                if token in haystack:
                    score += 1
            if not query:
                score = 1
            if score:
                scored.append(
                    {
                        "name": document.name,
                        "description": document.description,
                        "tags": document.tags,
                        "tools": document.tools,
                        "custom": is_custom,
                        "score": score,
                    }
                )
        return {"skills": sorted(scored, key=lambda item: item["score"], reverse=True)[:limit]}

    return ToolEntry(
        name="search_skills",
        description="搜索已加载的内置和用户自定义 Skills，用于 Skill-first 路由。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户任务或关键词。"},
                "include_builtin": {"type": "boolean", "description": "是否包含内置 Skills，默认 true。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
        category="skill",
    )
