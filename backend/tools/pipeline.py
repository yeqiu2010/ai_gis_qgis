"""GIS pipeline stage artifact tools."""

from __future__ import annotations

import json
import re
from typing import Any

from ...database.session_db import SessionDB
from ..json_recovery import (
    is_probably_truncated_json,
    recover_json_object,
    unwrap_raw_arguments,
)
from ..processing.algorithm_evidence import (
    clear_processing_evidence,
    compact_processing_evidence,
    processing_algorithm_ids,
    processing_parameter_names,
    read_processing_evidence,
)
from .code_execution import (
    find_expected_output_contract_issues,
    find_generated_code_issues,
)
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
        if stage_name == "data_overview":
            layer_bindings = _current_request_inspected_layers(
                session_db,
                session_id,
            )
            if layer_bindings:
                # Do not rely on the model to copy opaque QGIS layer IDs from
                # inspect results into its stage artifact.  These bindings are
                # small, authoritative runtime evidence needed by generated
                # code several pipeline stages later.
                artifact = {**artifact, "resolved_layers": layer_bindings}
        structured_query = _latest_stage_artifact(
            session_db,
            session_id,
            "structured_query",
        )
        processing_evidence = read_processing_evidence(session_db, session_id)
        solution_plan = _latest_stage_artifact(
            session_db,
            session_id,
            "solution_plan",
        )
        validation_error = _validate_stage_artifact(
            stage_name,
            artifact,
            structured_query=structured_query,
            solution_plan=solution_plan,
            processing_evidence=processing_evidence,
        )
        if validation_error:
            response = {
                "success": False,
                "error": validation_error,
                "expected_stage": expected_stage,
            }
            relevant_ids = processing_algorithm_ids(artifact) | processing_algorithm_ids(
                solution_plan
            )
            repair_evidence = compact_processing_evidence(
                processing_evidence,
                relevant_ids,
            )
            if repair_evidence:
                response["processing_algorithm_evidence"] = repair_evidence
            return response
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
    clear_processing_evidence(session_db, session_id)


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


def _validate_stage_artifact(
    stage_name: str,
    artifact: dict[str, Any],
    *,
    structured_query: dict[str, Any] | None = None,
    solution_plan: dict[str, Any] | None = None,
    processing_evidence: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    if stage_name == "solution_plan" and processing_evidence is not None:
        selected_ids = processing_algorithm_ids(artifact)
        missing_ids = sorted(selected_ids.difference(processing_evidence))
        if missing_ids:
            return (
                "solution_plan 使用了尚未读取真实详情的 Processing 算法："
                + "、".join(missing_ids)
                + "。必须先调用 get_qgis_processing_tool，并在同一次调用的 tool_ids 中读取这些算法；"
                "不得凭搜索摘要或记忆填写参数。"
            )
        parameter_issue = _find_solution_parameter_issue(artifact, processing_evidence)
        if parameter_issue:
            return parameter_issue
    if stage_name == "solution_plan" and not artifact.get("substitution_approved"):
        selected_text = _selected_solution_text(artifact)
        if re.search(
            r"(?:使用|采用|改用).{0,80}(?:替代|代替|近似)|"
            r"(?:替代|代替).{0,80}(?:分析|算法|工具)|"
            r"\bsubstitut(?:e|ed|ion)\b|\bapproximate\b",
            selected_text,
            flags=re.IGNORECASE,
        ):
            return (
                "solution_plan 选择了替代或近似算法，但没有用户批准证据。"
                "必须保留用户要求的分析语义；若 Catalog 中没有等价工具，"
                "请停止流程并询问用户是否接受替代。只有用户明确同意后才能设置 "
                "substitution_approved=true。"
            )
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
        if processing_evidence is not None:
            code_ids = _processing_ids_from_code(str(artifact.get("code") or ""))
            planned_ids = processing_algorithm_ids(solution_plan or {})
            # Legacy stage artifacts may predate algorithm evidence entirely.
            # Enforce the cross-stage contract whenever this Pipeline cycle has
            # declared or retrieved Processing algorithms.
            if planned_ids or processing_evidence:
                unplanned_ids = sorted(code_ids.difference(planned_ids))
                if unplanned_ids:
                    return (
                        "generated_code 使用了 solution_plan 未选择的 Processing 算法："
                        + "、".join(unplanned_ids)
                        + "。不得在代码阶段临时猜测辅助算法；只能使用方案中已读取详情并明确选择的算法。"
                    )
                missing_ids = sorted(code_ids.difference(processing_evidence))
                if missing_ids:
                    return (
                        "generated_code 缺少以下 Processing 算法的权威参数证据："
                        + "、".join(missing_ids)
                        + "。请回到方案证据，先读取算法详情，不能继续猜参数。"
                    )
        declaration_issues = find_expected_output_contract_issues(
            artifact.get("expected_outputs") or []
        )
        if declaration_issues:
            return "generated_code 的 expected_outputs 契约无效：" + "；".join(
                declaration_issues
            )
        code_issues = find_generated_code_issues(str(artifact.get("code") or ""))
        if code_issues:
            return (
                "generated_code 未通过服务端代码检查："
                + "；".join(code_issues)
                + "。请修正代码后重新记录本阶段。"
            )
        output_contract_error = _validate_expected_output_contract(
            artifact.get("expected_outputs") or [],
            structured_query or {},
        )
        if output_contract_error:
            return output_contract_error
    if stage_name == "execution_result" and not any(
        key in artifact for key in ("stdout", "stderr", "outputs", "error", "success")
    ):
        return "execution_result 必须包含真实执行结果：success、stdout、stderr、outputs 或 error。"
    return None


def _processing_ids_from_code(code: str) -> set[str]:
    return {
        match.lower()
        for match in re.findall(
            r"processing\.run\(\s*['\"]([a-zA-Z][\w-]*:[a-zA-Z0-9_.-]+)['\"]",
            code,
        )
    }


def _find_solution_parameter_issue(
    artifact: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
) -> str | None:
    raw_steps = artifact.get("steps") or artifact.get("operations") or []
    if not isinstance(raw_steps, list):
        return None
    for step in raw_steps:
        if not isinstance(step, dict) or not isinstance(step.get("parameters"), dict):
            continue
        step_ids = processing_algorithm_ids({"algorithm": step.get("algorithm")})
        # Composite operations normally use nested parameter dictionaries. They
        # are covered by the evidence gate and checked again against actual code.
        if len(step_ids) != 1:
            continue
        tool_id = next(iter(step_ids))
        detail = evidence.get(tool_id)
        if not detail:
            continue
        allowed = processing_parameter_names(detail)
        if len(allowed) < 4:
            continue
        unknown = sorted(set(map(str, step["parameters"])).difference(allowed))
        if not unknown:
            continue
        example = " ".join(str(detail.get("code_example") or "").split())[:1200]
        return (
            f"solution_plan 为 {tool_id} 使用了 Catalog 中不存在的参数："
            + "、".join(unknown)
            + "。允许参数："
            + "、".join(allowed)
            + (f"。Catalog 示例：{example}" if example else "")
        )
    return None


def _latest_stage_artifact(
    session_db: SessionDB,
    session_id: str,
    stage_name: str,
) -> dict[str, Any]:
    for item in reversed(session_db.get_recent_stage_artifacts(session_id, limit=8)):
        if item.get("stage_name") == stage_name:
            artifact = item.get("stage_artifact")
            return dict(artifact) if isinstance(artifact, dict) else {}
    return {}


def _current_request_inspected_layers(
    session_db: SessionDB,
    session_id: str,
) -> list[dict[str, Any]]:
    """Return compact layer bindings confirmed during the current request."""
    history = session_db.get_context_messages(session_id, historical_limit=0)
    request_started_at = max(
        (
            float(item.get("timestamp") or 0)
            for item in history
            if item.get("role") == "user"
        ),
        default=0.0,
    )
    bindings_by_id: dict[str, dict[str, Any]] = {}
    for call in session_db.get_recent_tool_calls(session_id, limit=64):
        if not call.get("success") or float(call.get("timestamp") or 0) < request_started_at:
            continue
        tool_name = str(call.get("tool_name") or "")
        if tool_name not in {"inspect_layer", "inspect_layers"}:
            continue
        result = call.get("result")
        if not isinstance(result, dict):
            continue
        inspected = result.get("layers") if tool_name == "inspect_layers" else [result]
        if not isinstance(inspected, list):
            continue
        for item in inspected:
            if not isinstance(item, dict) or not isinstance(item.get("layer"), dict):
                continue
            layer = item["layer"]
            layer_id = str(layer.get("id") or "").strip()
            if not layer_id:
                continue
            bindings_by_id[layer_id] = {
                key: layer.get(key)
                for key in (
                    "id",
                    "name",
                    "type",
                    "source",
                    "crs",
                    "crs_authid",
                    "crs_name",
                    "crs_definition",
                    "crs_definition_format",
                )
                if layer.get(key) is not None
            }
    return list(bindings_by_id.values())


def _selected_solution_text(artifact: dict[str, Any]) -> str:
    selected = {
        key: artifact.get(key)
        for key in ("summary", "steps", "algorithms", "algorithm_evidence")
        if artifact.get(key) is not None
    }
    return json.dumps(selected, ensure_ascii=False, default=str)


def _validate_expected_output_contract(
    generated_outputs: list[Any],
    structured_query: dict[str, Any],
) -> str | None:
    required_outputs = _find_expected_output_specs(structured_query)
    if not required_outputs:
        return None
    required_names = _output_contract_names(required_outputs)
    generated_names = _output_contract_names(generated_outputs)
    missing = sorted(required_names.difference(generated_names))
    if not missing:
        return None
    return (
        "generated_code 改写或遗漏了结构化需求中的最终输出名称："
        + "、".join(missing)
        + "。代码文件名、图层名和 expected_outputs 必须逐字保留用户指定的名称及数字后缀。"
    )


def _find_expected_output_specs(value: Any) -> list[Any]:
    if isinstance(value, dict):
        direct = value.get("expected_outputs")
        if isinstance(direct, list) and direct:
            return direct
        for child in value.values():
            found = _find_expected_output_specs(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_expected_output_specs(child)
            if found:
                return found
    return []


def _output_contract_names(outputs: list[Any]) -> set[str]:
    names: set[str] = set()
    for output in outputs:
        if isinstance(output, dict):
            candidates = (output.get("name"), output.get("path"))
        else:
            candidates = (output,)
        for candidate in candidates:
            value = str(candidate or "").strip().replace("\\", "/")
            if not value:
                continue
            filename = value.rsplit("/", 1)[-1]
            names.add(filename.casefold())
            if "." in filename:
                names.add(filename.rsplit(".", 1)[0].casefold())
    return names


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
