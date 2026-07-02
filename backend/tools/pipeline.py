"""GIS pipeline stage artifact tools."""

from __future__ import annotations

from typing import Any

from ...database.session_db import SessionDB
from .registry import ToolEntry

PIPELINE_STAGES = [
    "data_overview",
    "structured_query",
    "solution_plan",
    "generated_code",
    "execution_result",
]


def build_record_pipeline_stage_tool(session_db: SessionDB, session_id: str) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        artifact = arguments.get("artifact") or {}
        if not isinstance(artifact, dict):
            return {"success": False, "error": "artifact 必须是 JSON object。"}
        stage_name = _normalize_stage_name(str(arguments.get("stage_name") or "").strip())
        if not stage_name:
            stage_name = _infer_stage_name(session_db, session_id, artifact)
        if stage_name not in PIPELINE_STAGES:
            return {
                "success": False,
                "error": f"stage_name 必须是以下之一：{', '.join(PIPELINE_STAGES)}",
            }
        summary = str(arguments.get("summary") or artifact.get("summary") or "").strip()
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
    if artifact.get("code") or artifact.get("expected_outputs") or artifact.get("review"):
        return "generated_code"
    if artifact.get("stdout") or artifact.get("stderr") or artifact.get("outputs"):
        return "execution_result"
    recorded = session_db.list_pipeline_stage_names(session_id)
    for stage_name in PIPELINE_STAGES:
        if stage_name not in recorded:
            return stage_name
    return PIPELINE_STAGES[-1]
