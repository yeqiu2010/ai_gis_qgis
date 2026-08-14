"""Skill management tools."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..skills.skill_manager import SkillManager
from .registry import ToolEntry

StateSetter = Callable[[str, str], None]
StateGetter = Callable[[str], str | None]
StateDeleter = Callable[[str], None]

LOADED_SKILLS_STATE_SUFFIX = "loaded_skills"
MAX_LOADED_SKILLS = 5


def loaded_skills_key(session_id: str) -> str:
    return f"{session_id}:{LOADED_SKILLS_STATE_SUFFIX}"


def read_loaded_skills(
    get_state: StateGetter,
    session_id: str,
    skill_manager: SkillManager,
) -> list[str]:
    raw = get_state(loaded_skills_key(session_id))
    values: list[str] = []
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            values = [str(item) for item in parsed]
    if not values:
        legacy = get_state(f"{session_id}:active_skill")
        if legacy:
            values = [legacy]
    return skill_manager.normalize_loaded_skills(values, max_skills=MAX_LOADED_SKILLS)


def write_loaded_skills(
    set_state: StateSetter,
    session_id: str,
    skill_manager: SkillManager,
    names: list[str],
) -> list[str]:
    normalized = skill_manager.normalize_loaded_skills(names, max_skills=MAX_LOADED_SKILLS)
    set_state(loaded_skills_key(session_id), json.dumps(normalized, ensure_ascii=False))
    active = next((name for name in reversed(normalized) if name != "main-orchestrator"), None)
    set_state(f"{session_id}:active_skill", active or "main-orchestrator")
    return normalized


def build_set_active_skill_tool(
    set_state: StateSetter,
    session_id: str,
    skill_manager: SkillManager | None = None,
) -> ToolEntry:
    def handler(arguments: dict) -> dict:
        skill_name = str(arguments.get("skill_name") or "").strip()
        if not skill_name:
            return {"success": False, "error": "skill_name is required"}
        if skill_manager is not None:
            if skill_manager.get(skill_name) is None:
                return {"success": False, "error": f"Skill 不存在: {skill_name}"}
            available, reasons = skill_manager.availability(skill_name)
            if not available:
                return {
                    "success": False,
                    "error": f"Skill 当前不可用: {'；'.join(reasons)}",
                }
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


def build_load_skill_tool(
    skill_manager: SkillManager,
    get_state: StateGetter,
    set_state: StateSetter,
    session_id: str,
    record_usage=None,
) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        skill_name = str(arguments.get("skill_name") or "").strip()
        document = skill_manager.get(skill_name)
        if document is None:
            return {"success": False, "error": f"Skill 不存在: {skill_name}"}
        skill_name = document.name
        available, reasons = skill_manager.availability(skill_name)
        if not available:
            return {
                "success": False,
                "error": f"Skill 当前不可用: {'；'.join(reasons)}",
                "skill_name": skill_name,
            }
        loaded = read_loaded_skills(get_state, session_id, skill_manager)
        if skill_name in loaded:
            return {
                "success": True,
                "skill_name": skill_name,
                "loaded_skills": loaded,
                "already_loaded": True,
                "skill": skill_manager.skill_card(document),
            }
        if len(loaded) >= MAX_LOADED_SKILLS:
            return {
                "success": False,
                "error": f"最多同时加载 {MAX_LOADED_SKILLS} 个 Skill，请先卸载不再需要的 Skill。",
                "loaded_skills": loaded,
            }
        loaded = write_loaded_skills(
            set_state,
            session_id,
            skill_manager,
            [*loaded, skill_name],
        )
        if record_usage is not None:
            record_usage(skill_name, action="use")
        return {
            "success": True,
            "skill_name": skill_name,
            "loaded_skills": loaded,
            "skill": skill_manager.inspect(skill_name, include_body=True),
        }

    return ToolEntry(
        name="load_skill",
        description=(
            "按需加载一个 SKILL.md 到当前主 Agent Loop。加载不会创建子 Agent，"
            "已有计划、用户参数和 QGIS 上下文会继续保留。"
        ),
        parameters={
            "type": "object",
            "properties": {"skill_name": {"type": "string"}},
            "required": ["skill_name"],
            "additionalProperties": False,
        },
        handler=handler,
        category="skill",
    )


def build_unload_skill_tool(
    skill_manager: SkillManager,
    get_state: StateGetter,
    set_state: StateSetter,
    session_id: str,
) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        skill_name = str(arguments.get("skill_name") or "").strip()
        if skill_name == "main-orchestrator":
            return {"success": False, "error": "主 Coordinator Skill 不能卸载。"}
        loaded = read_loaded_skills(get_state, session_id, skill_manager)
        if skill_name not in loaded:
            return {
                "success": True,
                "skill_name": skill_name,
                "loaded_skills": loaded,
                "already_unloaded": True,
            }
        loaded = write_loaded_skills(
            set_state,
            session_id,
            skill_manager,
            [name for name in loaded if name != skill_name],
        )
        return {"success": True, "skill_name": skill_name, "loaded_skills": loaded}

    return ToolEntry(
        name="unload_skill",
        description="从当前主 Agent Loop 卸载不再需要的 Skill 指令。",
        parameters={
            "type": "object",
            "properties": {"skill_name": {"type": "string"}},
            "required": ["skill_name"],
            "additionalProperties": False,
        },
        handler=handler,
        category="skill",
    )


def build_list_loaded_skills_tool(
    skill_manager: SkillManager,
    get_state: StateGetter,
    session_id: str,
) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        loaded = read_loaded_skills(get_state, session_id, skill_manager)
        return {
            "success": True,
            "loaded_skills": [
                skill_manager.inspect(name, include_body=False)
                for name in loaded
                if skill_manager.get(name) is not None
            ],
        }

    return ToolEntry(
        name="list_loaded_skills",
        description="列出当前主 Agent Loop 已加载的 Skill 和运行时契约摘要。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        category="skill",
    )


def build_inspect_skill_tool(skill_manager: SkillManager, record_usage=None) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        skill_name = str(arguments.get("skill_name") or "").strip()
        inspected = skill_manager.inspect(skill_name, include_body=True)
        if inspected is None:
            return {"success": False, "error": f"Skill 不存在: {skill_name}"}
        if record_usage is not None:
            record_usage(str(inspected["name"]), action="view")
        return {"success": True, "skill": inspected}

    return ToolEntry(
        name="inspect_skill",
        description="查看一个 Skill 的完整 SKILL.md 指令、工具和执行契约。",
        parameters={
            "type": "object",
            "properties": {"skill_name": {"type": "string"}},
            "required": ["skill_name"],
            "additionalProperties": False,
        },
        handler=handler,
        category="skill",
    )


def build_load_skill_reference_tool(skill_manager: SkillManager, record_usage=None) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        skill_name = str(arguments.get("skill_name") or "").strip()
        path = str(arguments.get("path") or "").strip()
        try:
            content = skill_manager.load_reference(skill_name, path)
        except (KeyError, ValueError, OSError) as exc:
            return {"success": False, "error": str(exc)}
        if record_usage is not None:
            document = skill_manager.get(skill_name)
            record_usage(document.name if document else skill_name, action="view")
        return {"success": True, "skill_name": skill_name, "path": path, "content": content}

    return ToolEntry(
        name="load_skill_reference",
        description="按需读取已安装 Skill 目录中的 reference、template、script 或示例文件。",
        parameters={
            "type": "object",
            "properties": {
                "skill_name": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["skill_name", "path"],
            "additionalProperties": False,
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
            "列出可用的内置和用户自定义 Skill 摘要卡片。"
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
