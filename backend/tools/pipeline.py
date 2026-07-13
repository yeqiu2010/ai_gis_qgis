"""GIS pipeline stage artifact tools."""

from __future__ import annotations

import json
from typing import Any

from ...database.session_db import SessionDB
from .code_execution import find_unwritten_expected_outputs
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
        artifact = arguments.get("artifact") or {}
        if isinstance(artifact, str):
            try:
                artifact = json.loads(artifact)
            except json.JSONDecodeError as exc:
                return {
                    "success": False,
                    "error": f"artifact 不是有效的 JSON object：{exc.msg}",
                    "received_stage": _normalize_stage_name(
                        str(arguments.get("stage_name") or "").strip()
                    ),
                }
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
        return {
            "success": True,
            "stage_name": stage_name,
            "summary": summary,
            "artifact": artifact,
            "message_id": message_id,
        }

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
                    "description": "该阶段的结构化产物，必须可 JSON 序列化。",
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
        if not isinstance(artifact.get("expected_outputs"), list) or not artifact["expected_outputs"]:
            return "generated_code 阶段缺少 expected_outputs。"
        review = artifact.get("review")
        if not isinstance(review, dict):
            return "generated_code 阶段缺少 review。"
        if review.get("passed") is not True:
            return "generated_code 的 review.passed 必须为 true；请修正代码后重新记录本阶段。"
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
