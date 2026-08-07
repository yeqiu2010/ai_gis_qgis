"""GIS pipeline stage artifact tools."""

from __future__ import annotations

from typing import Any

from ...database.session_db import SessionDB
from ..json_recovery import (
    is_probably_truncated_json,
    recover_json_object,
    unwrap_raw_arguments,
)
from .code_execution import find_generated_code_issues, find_unwritten_expected_outputs
from .registry import ToolEntry

PIPELINE_STAGES = [
    "data_overview",
    "structured_query",
    "solution_plan",
    "generated_code",
    "execution_result",
]
PIPELINE_CYCLE_STATE_SUFFIX = "pipeline_cycle_start"


def build_record_pipeline_stage_tool(session_db: SessionDB, session_id: str) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        raw_arguments = arguments.get("_raw_arguments")
        had_raw_arguments = isinstance(raw_arguments, str)
        arguments = unwrap_raw_arguments(arguments)
        if had_raw_arguments and "_raw_arguments" in arguments:
            if is_probably_truncated_json(raw_arguments):
                return {
                    "success": False,
                    "error": (
                        "record_pipeline_stage 的参数 JSON 不完整或被截断；"
                        "通常是模型 max_tokens 太小。请精简代码中的注释和日志，"
                        "完整重新生成 generated_code，不要续写或执行当前片段。"
                    ),
                    "error_code": "truncated_tool_arguments",
                    "retryable": True,
                    "received_stage": "generated_code"
                    if '"generated_code"' in raw_arguments
                    else "",
                }
            return {
                "success": False,
                "error": "record_pipeline_stage 的 _raw_arguments 不是可恢复的 JSON object。",
                "error_code": "invalid_tool_arguments",
                "retryable": False,
                "received_stage": "",
            }
        artifact = arguments.get("artifact") or {}
        if isinstance(artifact, str):
            recovered_artifact = recover_json_object(artifact)
            if recovered_artifact is None:
                return {
                    "success": False,
                    "error": "artifact 不是可恢复的 JSON object。",
                    "received_stage": _normalize_stage_name(
                        str(arguments.get("stage_name") or "").strip()
                    ),
                }
            artifact = recovered_artifact
        if not isinstance(artifact, dict):
            return {
                "success": False,
                "error": "artifact 必须是 JSON object。",
                "received_stage": _normalize_stage_name(
                    str(arguments.get("stage_name") or "").strip()
                ),
            }
        stage_name = _normalize_stage_name(str(arguments.get("stage_name") or "").strip())
        if not stage_name:
            stage_name = _infer_stage_name(session_db, session_id, artifact)
        if stage_name not in PIPELINE_STAGES:
            return {
                "success": False,
                "error": f"stage_name 必须是以下之一：{', '.join(PIPELINE_STAGES)}",
                "received_stage": stage_name,
            }
        summary = str(arguments.get("summary") or artifact.get("summary") or "").strip()
        if not summary:
            summary = f"{_stage_display_name(stage_name)}已完成。"
            artifact = {**artifact, "summary": summary}
        expected_stage = next_pipeline_stage(session_db, session_id)
        if stage_name != expected_stage:
            completed_stages = current_pipeline_cycle(session_db, session_id)
            if stage_name in completed_stages:
                return {
                    "success": True,
                    "stage_name": stage_name,
                    "summary": summary,
                    "artifact": artifact,
                    "already_recorded": True,
                    "expected_stage": expected_stage,
                }
            return {
                "success": False,
                "error": (
                    f"Pipeline 阶段顺序错误：当前必须记录 {expected_stage}，"
                    f"不能记录 {stage_name}。不要重复已经完成的阶段。"
                ),
                "expected_stage": expected_stage,
                "received_stage": stage_name,
                "completed_stages": completed_stages,
            }
        validation_error = _validate_stage_artifact(stage_name, artifact)
        if validation_error:
            return {
                "success": False,
                "error": validation_error,
                "expected_stage": expected_stage,
            }
        message_id = session_db.log_stage_artifact(
            session_id,
            stage_name=stage_name,
            artifact=artifact,
            summary=summary or None,
        )
        _sync_pipeline_task_state(
            session_db,
            session_id,
            stage_name=stage_name,
            artifact=artifact,
        )
        response = {
            "success": True,
            "stage_name": stage_name,
            "summary": summary,
            "artifact": artifact,
            "message_id": message_id,
        }
        if stage_name == "generated_code":
            execution_arguments = {
                "code": str(artifact.get("code") or ""),
                "expected_outputs": artifact.get("expected_outputs") or [],
            }
            for optional_name in ("delivery_outputs", "timeout_seconds"):
                if artifact.get(optional_name):
                    execution_arguments[optional_name] = artifact[optional_name]
            response["requested_tool_call"] = {
                "name": "execute_gis_code",
                "arguments": execution_arguments,
                "reason": "generated_code 阶段已通过审查，等待用户确认执行。",
            }
        return response

    return ToolEntry(
        name="record_pipeline_stage",
        description=(
            "记录 GIS Pipeline 阶段产物并推送阶段事件。"
            "必须按 data_overview、structured_query、solution_plan、generated_code、execution_result 的顺序使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "stage_name": {
                    "type": "string",
                    "enum": PIPELINE_STAGES,
                    "description": "Pipeline 阶段名。缺失时会按当前会话已记录进度自动推断下一个阶段。",
                },
                "summary": {"type": "string", "description": "阶段结果的简短中文摘要。"},
                "artifact": {
                    "type": "object",
                    "description": (
                        "该阶段的结构化产物，必须直接作为 JSON object 传入，不得再次序列化成字符串，"
                        "也不得放入 _raw_arguments。generated_code 阶段包含 code、expected_outputs、"
                        "dependencies、assumptions、summary、review；代码应完整且精简。"
                    ),
                },
            },
            "required": ["artifact"],
            "additionalProperties": False,
        },
        handler=handler,
        category="pipeline",
    )


STAGE_ALIASES = {
    "data overview": "data_overview",
    "data-overview": "data_overview",
    "数据盘点": "data_overview",
    "structured query": "structured_query",
    "structured-query": "structured_query",
    "结构化需求": "structured_query",
    "solution plan": "solution_plan",
    "solution-plan": "solution_plan",
    "处理方案": "solution_plan",
    "generated code": "generated_code",
    "generated-code": "generated_code",
    "code_generation": "generated_code",
    "代码生成": "generated_code",
    "execution result": "execution_result",
    "execution-result": "execution_result",
    "执行结果": "execution_result",
}


def _normalize_stage_name(stage_name: str) -> str:
    normalized = stage_name.strip()
    if normalized in PIPELINE_STAGES:
        return normalized
    return STAGE_ALIASES.get(normalized.lower(), STAGE_ALIASES.get(normalized, normalized))


def _infer_stage_name(session_db: SessionDB, session_id: str, artifact: dict[str, Any]) -> str:
    return next_pipeline_stage(session_db, session_id)


def _stage_display_name(stage_name: str) -> str:
    return {
        "data_overview": "数据盘点",
        "structured_query": "结构化需求",
        "solution_plan": "处理方案",
        "generated_code": "代码生成",
        "execution_result": "执行结果",
    }.get(stage_name, stage_name)


def next_pipeline_stage(session_db: SessionDB, session_id: str) -> str:
    current_cycle = _current_pipeline_cycle(session_db, session_id)
    if current_cycle == PIPELINE_STAGES:
        return PIPELINE_STAGES[0]
    if not current_cycle:
        return PIPELINE_STAGES[0]
    return PIPELINE_STAGES[len(current_cycle)]


def current_pipeline_cycle(session_db: SessionDB, session_id: str) -> list[str]:
    return list(_current_pipeline_cycle(session_db, session_id))


def start_pipeline_cycle(session_db: SessionDB, session_id: str) -> None:
    stage_count = len(session_db.list_pipeline_stage_names(session_id))
    session_db.set_state(
        f"{session_id}:{PIPELINE_CYCLE_STATE_SUFFIX}",
        str(stage_count),
    )


def _current_pipeline_cycle(session_db: SessionDB, session_id: str) -> list[str]:
    current_cycle: list[str] = []
    recorded_stages = session_db.list_pipeline_stage_names(session_id)
    raw_start = session_db.get_state(f"{session_id}:{PIPELINE_CYCLE_STATE_SUFFIX}")
    try:
        start_index = max(0, min(int(raw_start or 0), len(recorded_stages)))
    except ValueError:
        start_index = 0
    for stage_name in recorded_stages[start_index:]:
        if not current_cycle and stage_name == PIPELINE_STAGES[0]:
            current_cycle = [stage_name]
        elif current_cycle == PIPELINE_STAGES and stage_name == PIPELINE_STAGES[0]:
            current_cycle = [stage_name]
        elif (
            current_cycle
            and len(current_cycle) < len(PIPELINE_STAGES)
            and stage_name == PIPELINE_STAGES[len(current_cycle)]
        ):
            current_cycle.append(stage_name)
    return current_cycle


def _validate_stage_artifact(stage_name: str, artifact: dict[str, Any]) -> str | None:
    if stage_name == "generated_code":
        if not str(artifact.get("code") or "").strip():
            return "generated_code 阶段缺少非空 code。"
        if not isinstance(artifact.get("expected_outputs"), list):
            return "generated_code 阶段缺少 expected_outputs 列表；无文件结果时应传空数组。"
        review = artifact.get("review")
        if not isinstance(review, dict):
            return "generated_code 阶段缺少 review。"
        if review.get("passed") is not True:
            return "generated_code 的 review.passed 必须为 true；请修正代码后重新记录本阶段。"
        code_issues = find_generated_code_issues(str(artifact.get("code") or ""))
        if code_issues:
            return (
                "generated_code 未通过服务端代码检查："
                + "；".join(code_issues)
                + "。请修正代码后重新记录本阶段。"
            )
        unwritten = find_unwritten_expected_outputs(
            str(artifact.get("code") or ""),
            artifact["expected_outputs"],
        )
        if unwritten:
            return (
                "generated_code 的代码没有写入以下 expected_outputs："
                + "、".join(unwritten)
                + "。仅打印到 stdout 不会创建输出文件。"
            )
    if stage_name == "execution_result" and not any(
        key in artifact for key in ("stdout", "stderr", "outputs", "error", "success")
    ):
        return "execution_result 必须包含真实执行结果：success、stdout、stderr、outputs 或 error。"
    return None


def _sync_pipeline_task_state(
    session_db: SessionDB,
    session_id: str,
    *,
    stage_name: str,
    artifact: dict[str, Any],
) -> None:
    """Represent the legacy five stages as ordinary persisted plan steps."""
    task = session_db.get_active_task(session_id)
    if task is None or task.get("status") in {"completed", "failed", "cancelled"}:
        history = session_db.get_conversation_messages(session_id, limit=12)
        objective = next(
            (
                str(item.get("content") or "").strip()
                for item in reversed(history)
                if item.get("role") == "user" and str(item.get("content") or "").strip()
            ),
            "完成 GIS Pipeline 分析",
        )
        task_id = session_db.create_task(session_id, objective)
        task = session_db.get_task(task_id)
    assert task is not None
    task_id = str(task["id"])
    steps = session_db.get_plan_steps(task_id)
    if not steps:
        session_db.replace_plan_steps(
            task_id,
            [
                {
                    "id": f"pipeline_{name}",
                    "position": index,
                    "skill_name": "gis-pipeline",
                    "instruction": f"完成 GIS Pipeline 阶段：{name}",
                    "dependencies": [f"pipeline_{PIPELINE_STAGES[index - 2]}"]
                    if index > 1
                    else [],
                    "status": "pending",
                    "inputs": {},
                    "outputs": {},
                }
                for index, name in enumerate(PIPELINE_STAGES, start=1)
            ],
        )
        steps = session_db.get_plan_steps(task_id)

    step_id = f"pipeline_{stage_name}"
    if not any(str(step.get("id")) == step_id for step in steps):
        # A Coordinator-created business plan may model the Pipeline as one
        # coarse step. It remains authoritative and is completed after the
        # confirmed execution result instead of being replaced here.
        return
    questions = artifact.get("questions") if stage_name == "structured_query" else []
    if isinstance(questions, list) and questions:
        session_db.update_plan_step(
            task_id,
            step_id,
            status="waiting_for_user",
            outputs={"artifact": artifact},
        )
        session_db.update_task(task_id, status="waiting_for_user")
        return
    session_db.update_plan_step(
        task_id,
        step_id,
        status="completed",
        outputs={"artifact": artifact},
        error="",
    )
    session_db.register_artifact(
        task_id,
        "pipeline_stage",
        step_id=step_id,
        name=stage_name,
        payload=artifact,
        producer="gis-pipeline",
        verified=True,
    )
    session_db.update_task(task_id, status="running")
