"""Optional Skill-oriented subagent delegation tool."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...database.session_db import SessionDB
from ..llm.base_provider import LLMProvider
from ..skills.skill_manager import SkillManager
from ..skills.skill_runner import SkillRunner
from .registry import ToolEntry, ToolRegistry


def build_invoke_skill_tool(
    *,
    session_db: SessionDB,
    session_id: str,
    llm_provider: LLMProvider,
    skill_manager: SkillManager,
    tool_registry: ToolRegistry,
    should_cancel: Callable[[], bool] | None = None,
) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        skill_name = str(arguments.get("skill_name") or "").strip()
        task = str(arguments.get("task") or "").strip()
        raw_inputs = arguments.get("inputs")
        inputs: dict[str, Any] = dict(raw_inputs) if isinstance(raw_inputs, dict) else {}
        if not skill_name or not task:
            return {"success": False, "status": "failed", "error": "skill_name 和 task 必填。"}
        document = skill_manager.get(skill_name)
        if document is None:
            return {"success": False, "status": "failed", "error": f"Skill 不存在: {skill_name}"}
        side_effects = document.execution_contract.get("side_effects") or {}
        if any(
            bool(side_effects.get(key))
            for key in ("modifies_qgis_project", "writes_project", "writes_files", "requires_confirmation")
        ):
            return {
                "success": False,
                "status": "failed",
                "error": "该 Skill 声明了写入或确认副作用，必须在主 Agent Loop 中执行。",
                "recommended_mode": "main_loop",
            }

        active_task = session_db.get_active_task(session_id)
        invocation_id = None
        if active_task is not None:
            invocation_id = session_db.start_skill_invocation(
                str(active_task["id"]),
                skill_name,
                step_id=str(arguments.get("step_id") or "").strip() or None,
                execution_mode="delegated",
                arguments={"task": task, "inputs": inputs},
            )
        runner = SkillRunner(
            llm_provider=llm_provider,
            skill_manager=skill_manager,
            tool_registry=tool_registry,
            max_iterations=int(arguments.get("max_iterations") or 8),
            should_cancel=should_cancel,
        )
        try:
            result = runner.run(skill_name=skill_name, task=task, inputs=inputs)
        except Exception as exc:
            result = {
                "success": False,
                "status": "failed",
                "summary": "隔离子任务执行失败。",
                "artifacts": [],
                "missing_inputs": [],
                "evidence": [],
                "error": str(exc),
            }
        if invocation_id:
            session_db.finish_skill_invocation(
                invocation_id,
                status=str(result.get("status") or "failed"),
                result=result,
            )
        if active_task is not None and result.get("status") == "waiting_for_user":
            session_db.update_task(str(active_task["id"]), status="waiting_for_user")
        return result

    return ToolEntry(
        name="invoke_skill",
        description=(
            "在隔离、受限的子 Agent Loop 中执行复杂且上下文独立的只读 Skill。"
            "涉及 QGIS 工程修改、文件写入或用户确认的 Skill 必须留在主循环。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "skill_name": {"type": "string"},
                "task": {"type": "string"},
                "inputs": {"type": "object"},
                "step_id": {"type": "string"},
                "max_iterations": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["skill_name", "task", "inputs"],
            "additionalProperties": False,
        },
        handler=handler,
        category="delegation",
    )
