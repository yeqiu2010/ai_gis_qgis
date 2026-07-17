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
        query = str(arguments.get("query") or "").strip()
        include_builtin = bool(arguments.get("include_builtin", True))
        limit = max(1, min(int(arguments.get("limit") or 20), 50))
        catalog = skill_manager.routing_catalog(include_builtin=include_builtin)
        return {
            "query": query,
            "matching": "ai_semantic_selection_required",
            "instruction": (
                "程序未对 Skill 做关键词匹配或相关性排序。"
                "请由当前 AI 将用户原始请求与每个 description 进行语义比较，"
                "优先选择完整覆盖任务的专用业务 Skill；只有没有专用 Skill 匹配时，"
                "才选择 qgis-toolbox、gis-pipeline 或 fast-path 等通用回退 Skill。"
            ),
            "skills": catalog[:limit],
        }

    return ToolEntry(
        name="search_skills",
        description=(
            "列出已加载且可独立路由的内置和用户自定义 Skill 卡片。"
            "本工具不进行分词、关键词打分或语义选择；调用后必须由当前 AI 根据用户原始请求"
            "与 description 判断最匹配的专用 Skill。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "未经程序分词的用户原始请求。"},
                "include_builtin": {"type": "boolean", "description": "是否包含内置 Skills，默认 true。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
        category="skill",
    )
