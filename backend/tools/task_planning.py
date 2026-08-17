"""Generic persisted plan, artifact, and completion-verification tools."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...database.session_db import SessionDB
from ..executor.artifact_verifier import ArtifactVerifier
from ..skills.skill_manager import SkillManager
from .registry import ToolEntry

STEP_STATUSES = {
    "pending",
    "in_progress",
    "waiting_for_user",
    "completed",
    "failed",
    "skipped",
}


def build_task_planning_tools(
    session_db: SessionDB,
    session_id: str,
    skill_manager: SkillManager,
) -> list[ToolEntry]:
    return [
        _build_create_plan_tool(session_db, session_id, skill_manager),
        _build_revise_plan_tool(session_db, session_id, skill_manager),
        _build_get_task_state_tool(session_db, session_id),
        _build_update_plan_step_tool(session_db, session_id),
        _build_complete_plan_step_tool(session_db, session_id),
        _build_register_artifact_tool(session_db, session_id),
        _build_finalize_task_tool(session_db, session_id, skill_manager),
    ]


def _active_task_or_error(session_db: SessionDB, session_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    task = session_db.get_active_task(session_id)
    if task is None:
        return None, {"success": False, "error": "当前会话没有活动任务，请先调用 create_plan。"}
    return task, None


def _normalize_steps(
    raw_steps: Any,
    skill_manager: SkillManager,
) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(raw_steps, list) or not raw_steps:
        return [], "steps 必须是非空数组。"
    steps: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            return [], f"第 {index} 个步骤必须是 object。"
        step_id = str(raw_step.get("id") or f"step_{index}").strip()
        if not step_id or step_id in ids:
            return [], f"步骤 ID 为空或重复: {step_id}"
        instruction = str(raw_step.get("instruction") or raw_step.get("task") or "").strip()
        if not instruction:
            return [], f"步骤 {step_id} 缺少 instruction。"
        skill_name = str(raw_step.get("skill_name") or raw_step.get("skill") or "").strip()
        if skill_name and skill_manager.get(skill_name) is None:
            return [], f"步骤 {step_id} 引用了不存在的 Skill: {skill_name}"
        dependencies = raw_step.get("dependencies", raw_step.get("depends_on", [])) or []
        if not isinstance(dependencies, list):
            return [], f"步骤 {step_id} 的 dependencies 必须是数组。"
        steps.append(
            {
                "id": step_id,
                "position": index,
                "skill_name": skill_name or None,
                "instruction": instruction,
                "dependencies": [str(value).strip() for value in dependencies if str(value).strip()],
                "status": "pending",
                "inputs": raw_step.get("inputs") if isinstance(raw_step.get("inputs"), dict) else {},
                "outputs": {},
            }
        )
        ids.add(step_id)
    for step in steps:
        unknown = [value for value in step["dependencies"] if value not in ids]
        if unknown:
            return [], f"步骤 {step['id']} 包含未知依赖: {', '.join(unknown)}"
        if step["id"] in step["dependencies"]:
            return [], f"步骤 {step['id']} 不能依赖自身。"
    if _has_dependency_cycle(steps):
        return [], "计划步骤依赖存在环。"
    return steps, None


def _has_dependency_cycle(steps: list[dict[str, Any]]) -> bool:
    graph = {str(step["id"]): list(step["dependencies"]) for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(dependency) for dependency in graph[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def _create_or_revise_plan(
    session_db: SessionDB,
    session_id: str,
    skill_manager: SkillManager,
    arguments: dict[str, Any],
    *,
    revise: bool,
) -> dict[str, Any]:
    objective = str(arguments.get("objective") or "").strip()
    steps, error = _normalize_steps(arguments.get("steps"), skill_manager)
    if error:
        return {"success": False, "error": error}
    task = session_db.get_active_task(session_id)
    if revise:
        if task is None or task.get("status") in {"completed", "failed", "cancelled"}:
            return {"success": False, "error": "当前没有可修订的活动任务。"}
        task_id = str(task["id"])
        objective = objective or str(task["objective"])
        session_db.update_task(task_id, objective=objective)
    else:
        if not objective:
            return {"success": False, "error": "objective is required"}
        if task and task.get("status") in {
            "draft",
            "running",
            "waiting_for_user",
            "waiting_confirmation",
        } and session_db.get_plan_steps(str(task["id"])):
            return {
                "success": False,
                "error": "当前已有活动计划；如需调整请调用 revise_plan。",
                "task_id": task["id"],
            }
        task_id = str(task["id"]) if task and task.get("status") == "draft" else session_db.create_task(
            session_id, objective
        )
        session_db.update_task(task_id, objective=objective)
    session_db.replace_plan_steps(task_id, steps)
    return {"success": True, "task": session_db.get_task_state(task_id)}


def _build_create_plan_tool(
    session_db: SessionDB,
    session_id: str,
    skill_manager: SkillManager,
) -> ToolEntry:
    return ToolEntry(
        name="create_plan",
        description="为多步骤 GIS 任务创建持久化通用计划。普通问答和单步只读操作无需创建。",
        parameters=_plan_parameters(),
        handler=lambda arguments: _create_or_revise_plan(
            session_db, session_id, skill_manager, arguments, revise=False
        ),
        category="planning",
    )


def _build_revise_plan_tool(
    session_db: SessionDB,
    session_id: str,
    skill_manager: SkillManager,
) -> ToolEntry:
    return ToolEntry(
        name="revise_plan",
        description="根据工具结果、用户补充或失败恢复，替换当前活动任务的计划步骤。",
        parameters=_plan_parameters(objective_required=False),
        handler=lambda arguments: _create_or_revise_plan(
            session_db, session_id, skill_manager, arguments, revise=True
        ),
        category="planning",
    )


def _plan_parameters(*, objective_required: bool = True) -> dict[str, Any]:
    required = ["steps"]
    if objective_required:
        required.insert(0, "objective")
    return {
        "type": "object",
        "properties": {
            "objective": {"type": "string"},
            "steps": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "skill_name": {"type": "string"},
                        "instruction": {"type": "string"},
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                        "inputs": {"type": "object"},
                    },
                    "required": ["id", "instruction"],
                    "additionalProperties": False,
                },
            },
        },
        "required": required,
        "additionalProperties": False,
    }


def _build_get_task_state_tool(session_db: SessionDB, session_id: str) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = str(arguments.get("task_id") or "").strip()
        task = session_db.get_task_state(task_id) if task_id else None
        if task is None:
            active = session_db.get_active_task(session_id)
            task = session_db.get_task_state(str(active["id"])) if active else None
        return {"success": True, "task": task}

    return ToolEntry(
        name="get_task_state",
        description="读取当前任务的计划步骤、Skill 调用和产物。",
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=handler,
        category="planning",
    )


def _build_update_plan_step_tool(session_db: SessionDB, session_id: str) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        task, error = _active_task_or_error(session_db, session_id)
        if error:
            return error
        assert task is not None
        step_id = str(arguments.get("step_id") or "").strip()
        status = str(arguments.get("status") or "").strip()
        if status not in STEP_STATUSES:
            return {"success": False, "error": f"无效步骤状态: {status}"}
        step = session_db.get_plan_step(str(task["id"]), step_id)
        if step is None:
            return {"success": False, "error": f"计划步骤不存在: {step_id}"}
        if status in {"in_progress", "completed"}:
            dependencies = [
                session_db.get_plan_step(str(task["id"]), dependency)
                for dependency in step["dependencies"]
            ]
            blocked = []
            for dependency, item in zip(step["dependencies"], dependencies, strict=True):
                if item is None or item.get("status") not in {"completed", "skipped"}:
                    blocked.append(str(dependency))
            if blocked:
                return {
                    "success": False,
                    "error": "步骤依赖尚未完成: " + ", ".join(blocked),
                }
        session_db.update_plan_step(
            str(task["id"]),
            step_id,
            status=status,
            inputs=arguments.get("inputs") if isinstance(arguments.get("inputs"), dict) else None,
            outputs=arguments.get("outputs") if isinstance(arguments.get("outputs"), dict) else None,
            error=str(arguments.get("error")) if arguments.get("error") is not None else None,
        )
        task_status = "waiting_for_user" if status == "waiting_for_user" else "running"
        if status == "failed":
            task_status = "failed"
        session_db.update_task(str(task["id"]), status=task_status)
        return {"success": True, "task": session_db.get_task_state(str(task["id"]))}

    return ToolEntry(
        name="update_plan_step",
        description="更新一个计划步骤的状态、输入、输出或错误。",
        parameters={
            "type": "object",
            "properties": {
                "step_id": {"type": "string"},
                "status": {"type": "string", "enum": sorted(STEP_STATUSES)},
                "inputs": {"type": "object"},
                "outputs": {"type": "object"},
                "error": {"type": "string"},
            },
            "required": ["step_id", "status"],
            "additionalProperties": False,
        },
        handler=handler,
        category="planning",
    )


def _build_complete_plan_step_tool(session_db: SessionDB, session_id: str) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        task, error = _active_task_or_error(session_db, session_id)
        if error:
            return error
        assert task is not None
        step_id = str(arguments.get("step_id") or "").strip()
        step = session_db.get_plan_step(str(task["id"]), step_id)
        if step is None:
            return {"success": False, "error": f"计划步骤不存在: {step_id}"}
        blocked = []
        for dependency in step["dependencies"]:
            dependency_step = session_db.get_plan_step(str(task["id"]), dependency)
            if dependency_step is None or dependency_step.get("status") not in {"completed", "skipped"}:
                blocked.append(dependency)
        if blocked:
            return {"success": False, "error": "步骤依赖尚未完成: " + ", ".join(blocked)}
        outputs = arguments.get("outputs") if isinstance(arguments.get("outputs"), dict) else {}
        session_db.update_plan_step(
            str(task["id"]), step_id, status="completed", outputs=outputs, error=""
        )
        registered = _register_artifact_specs(
            session_db,
            str(task["id"]),
            step_id,
            arguments.get("artifacts"),
            producer=str(step.get("skill_name") or "") or None,
        )
        session_db.update_task(str(task["id"]), status="running")
        return {
            "success": True,
            "completed_step": step_id,
            "registered_artifacts": registered,
            "task": session_db.get_task_state(str(task["id"])),
        }

    return ToolEntry(
        name="complete_plan_step",
        description="在真实工具结果已返回后完成计划步骤，并登记结构化输出和产物。",
        parameters={
            "type": "object",
            "properties": {
                "step_id": {"type": "string"},
                "outputs": {"type": "object"},
                "artifacts": {"type": "array", "items": _artifact_schema()},
            },
            "required": ["step_id", "outputs"],
            "additionalProperties": False,
        },
        handler=handler,
        category="planning",
    )


def _artifact_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "artifact_type": {"type": "string"},
            "name": {"type": "string"},
            "uri": {"type": "string"},
            "payload": {"type": "object"},
            "verified": {"type": "boolean"},
        },
        "required": ["artifact_type"],
        "additionalProperties": False,
    }


def _register_artifact_specs(
    session_db: SessionDB,
    task_id: str,
    step_id: str | None,
    raw_artifacts: Any,
    *,
    producer: str | None,
) -> list[str]:
    if not isinstance(raw_artifacts, list):
        return []
    registered: list[str] = []
    for artifact in raw_artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_type = str(artifact.get("artifact_type") or "").strip()
        if not artifact_type:
            continue
        uri = str(artifact.get("uri") or "").strip() or None
        verified = bool(artifact.get("verified"))
        if uri and _is_local_uri(uri) and not _local_uri_exists(uri):
            verified = False
        elif uri and _local_uri_exists(uri):
            raw_payload = artifact.get("payload")
            payload: dict[str, Any] = (
                dict(raw_payload) if isinstance(raw_payload, dict) else {}
            )
            verification = ArtifactVerifier().verify(
                {
                    "path": uri.removeprefix("file://").split("|", 1)[0],
                    "name": artifact.get("name") or Path(uri).stem,
                    "type": _artifact_output_type(artifact_type, uri, payload),
                    "required": True,
                }
            )
            payload = {**payload, "verification": verification}
            artifact = {**artifact, "payload": payload}
            verified = bool(verification.get("verified"))
        registered.append(
            session_db.register_artifact(
                task_id,
                artifact_type,
                step_id=step_id,
                name=str(artifact.get("name") or "").strip() or None,
                uri=uri,
                payload=artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {},
                producer=producer,
                verified=verified,
            )
        )
    return registered


def _artifact_output_type(
    artifact_type: str,
    uri: str,
    payload: dict[str, Any],
) -> str:
    declared = str(payload.get("type") or "").lower()
    if declared in {"vector", "raster", "table", "file"}:
        return declared
    suffix = Path(uri.removeprefix("file://").split("|", 1)[0]).suffix.lower()
    if suffix in {".tif", ".tiff", ".vrt", ".img"}:
        return "raster"
    if suffix in {".shp", ".gpkg", ".geojson", ".kml"}:
        return "vector"
    if suffix in {".csv", ".xlsx", ".dbf"}:
        return "table"
    normalized_type = artifact_type.lower()
    if "raster" in normalized_type or "mask" in normalized_type:
        return "raster"
    if "vector" in normalized_type or "layer" in normalized_type:
        return "vector"
    return "file"


def _build_register_artifact_tool(session_db: SessionDB, session_id: str) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        task, error = _active_task_or_error(session_db, session_id)
        if error:
            return error
        assert task is not None
        registered = _register_artifact_specs(
            session_db,
            str(task["id"]),
            str(arguments.get("step_id") or "").strip() or None,
            [arguments],
            producer=str(arguments.get("producer") or "").strip() or None,
        )
        if not registered:
            return {"success": False, "error": "artifact_type is required"}
        return {"success": True, "artifact_id": registered[0]}

    schema = _artifact_schema()
    schema["properties"]["step_id"] = {"type": "string"}
    schema["properties"]["producer"] = {"type": "string"}
    return ToolEntry(
        name="register_artifact",
        description="登记当前计划产生的文件、QGIS 图层、统计表、布局或其他结构化产物。",
        parameters=schema,
        handler=handler,
        category="planning",
    )


def _build_finalize_task_tool(
    session_db: SessionDB,
    session_id: str,
    skill_manager: SkillManager,
) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        task, error = _active_task_or_error(session_db, session_id)
        if error:
            return error
        assert task is not None
        state = session_db.get_task_state(str(task["id"]))
        assert state is not None
        failures = verify_task_completion(state, skill_manager)
        if failures:
            return {
                "success": False,
                "error": "任务尚未满足完成条件。",
                "completion_errors": failures,
                "task": _completion_state_summary(state),
            }
        summary = str(arguments.get("summary") or "").strip()
        finalization = {
            "summary": summary,
            "evidence": arguments.get("evidence") or [],
            "artifacts": [artifact["id"] for artifact in state["artifacts"]],
        }
        session_db.update_task(
            str(task["id"]),
            status="completed",
            summary=summary,
            finalization=finalization,
        )
        session_db.record_task_outcome(
            str(task["id"]),
            status="completed",
            completion_score=1.0,
            metrics={
                "step_count": len(state["steps"]),
                "artifact_count": len(state["artifacts"]),
            },
        )
        if len(state["steps"]) >= 2:
            session_db.save_knowledge_candidate(
                candidate_type="recipe",
                target_name=str(task.get("objective") or "completed-workflow")[:120],
                payload={
                    "name": str(task.get("objective") or "completed-workflow")[:120],
                    "description": summary,
                    "intent": "task_plan_reuse",
                    "preconditions": [],
                    "steps": [
                        {
                            "skill_name": step.get("skill_name"),
                            "instruction": step.get("instruction"),
                            "dependencies": step.get("dependencies") or [],
                        }
                        for step in state["steps"]
                    ],
                    "artifact_contracts": [
                        artifact.get("artifact_type") for artifact in state["artifacts"]
                    ],
                    "success_criteria": ["所有计划步骤完成", "必需 Artifact 已验证"],
                    "fallbacks": [],
                },
                evidence={"success_count": 1, "task_id": task["id"]},
                evaluation={"passed": False, "reason": "等待重复样本和回归评测"},
                source_task_id=str(task["id"]),
            )
        return {
            "success": True,
            "task_id": task["id"],
            "status": "completed",
            "summary": summary,
            "artifacts": state["artifacts"],
        }

    return ToolEntry(
        name="finalize_task",
        description="验证所有计划步骤和必需产物后结束任务；验证失败时必须继续处理。",
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
        handler=handler,
        category="planning",
    )


def verify_task_completion(state: dict[str, Any], skill_manager: SkillManager) -> list[str]:
    failures: list[str] = []
    steps = state.get("steps") or []
    if not steps:
        failures.append("计划没有步骤")
        return failures
    for step in steps:
        if step.get("status") not in {"completed", "skipped"}:
            failures.append(f"步骤 {step.get('id')} 状态为 {step.get('status')}")
    artifacts = state.get("artifacts") or []
    verified_artifact_names = {
        str(value)
        for artifact in artifacts
        if artifact.get("verified")
        for value in (artifact.get("artifact_type"), artifact.get("name"))
        if value
    }
    for step in steps:
        if step.get("status") == "skipped":
            continue
        skill_name = str(step.get("skill_name") or "")
        document = skill_manager.get(skill_name)
        if document is None:
            continue
        completion = document.execution_contract.get("completion") or {}
        required = completion.get("required_artifacts") or []
        for artifact_name in required:
            if str(artifact_name) not in verified_artifact_names:
                failures.append(
                    f"步骤 {step.get('id')} 缺少已验证的必需产物 {artifact_name}"
                )
    for artifact in artifacts:
        uri = str(artifact.get("uri") or "")
        if not uri or not _is_local_uri(uri):
            continue
        payload = artifact.get("payload") or {}
        if isinstance(payload, dict) and payload.get("required", True) is False:
            continue
        if not _local_uri_exists(uri):
            failures.append(f"产物不存在: {uri}")
        elif not artifact.get("verified"):
            failures.append(f"产物未通过运行时验证: {uri}")
    return failures


def _completion_state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Return only the state needed to repair a failed finalization."""
    return {
        "id": state.get("id"),
        "objective": state.get("objective"),
        "status": state.get("status"),
        "steps": [
            {
                "id": step.get("id"),
                "skill_name": step.get("skill_name"),
                "status": step.get("status"),
                "outputs": step.get("outputs") or {},
                "error": step.get("error"),
            }
            for step in state.get("steps") or []
        ],
        "artifacts": [
            {
                "artifact_type": artifact.get("artifact_type"),
                "name": artifact.get("name"),
                "uri": artifact.get("uri"),
                "verified": artifact.get("verified"),
            }
            for artifact in state.get("artifacts") or []
        ],
    }


def _is_local_uri(uri: str) -> bool:
    value = str(uri or "").strip()
    if not value:
        return False
    if value.startswith("file://"):
        return True
    if "://" in value or value.startswith("memory:"):
        return False
    if value.startswith(("/", "\\\\")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return True
    # Relative files remain verifiable, while opaque QGIS layer IDs such as
    # ``building_ab12...`` are not misclassified as filesystem paths.
    return "/" in value or "\\" in value or bool(Path(value).suffix)


def _local_uri_exists(uri: str) -> bool:
    if not _is_local_uri(uri):
        return False
    path = uri.removeprefix("file://").split("|", 1)[0]
    return bool(path) and Path(path).expanduser().exists()
