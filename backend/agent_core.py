"""Minimal AgentCore for Phase 1 conversations."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..database.session_db import SessionDB
from ..qwebengine.message_protocol import agent_event
from .context.context_engine import (
    ContextEngine,
    QGISContextEngine,
)
from .context.prompt_builder import PromptBuilder
from .context.qgis_context import QGISContext
from .iteration_budget import IterationBudget
from .llm.base_provider import ChatMessage, ChatResponse, LLMProvider
from .llm.errors import ContextWindowExceeded
from .plugin_system import PluginManager
from .processing.algorithm_evidence import (
    compact_processing_evidence,
    processing_algorithm_ids,
    read_processing_evidence,
)
from .tools.code_execution import (
    build_execute_gis_code_tool,
    validate_execute_gis_code_arguments,
)
from .tools.cultivated_land_loss import (
    build_cultivated_land_loss_analysis_tool,
    build_inspect_cultivated_land_loss_inputs_tool,
)
from .tools.custom_tools import load_custom_tool_entries
from .tools.delegation import build_invoke_skill_tool
from .tools.gis_analysis import build_get_task_context_tool
from .tools.land_cover_map import (
    build_generate_land_cover_map_tool,
    build_inspect_land_cover_map_inputs_tool,
)
from .tools.land_use_building_metrics import (
    build_inspect_land_use_building_metrics_inputs_tool,
    build_land_use_building_metrics_tool,
)
from .tools.layer_ops import build_layer_tools
from .tools.learning import build_learning_tools
from .tools.pipeline import (
    PIPELINE_STAGES,
    build_record_pipeline_stage_tool,
    current_pipeline_cycle,
    next_pipeline_stage,
    start_pipeline_cycle,
)
from .tools.qgis_toolbox import build_qgis_toolbox_tools
from .tools.registry import ToolRegistry
from .tools.school_service_coverage import (
    build_inspect_school_service_coverage_inputs_tool,
    build_school_service_coverage_tool,
)
from .tools.search_tools import build_search_messages_tool
from .tools.skill_management import (
    build_inspect_skill_tool,
    build_list_loaded_skills_tool,
    build_load_skill_reference_tool,
    build_load_skill_tool,
    build_search_skills_tool,
    build_set_active_skill_tool,
    build_unload_skill_tool,
    read_loaded_skills,
    write_loaded_skills,
)
from .tools.task_planning import build_task_planning_tools, verify_task_completion

EventCallback = Callable[[dict[str, Any]], None]
CancelChecker = Callable[[], bool]

# Deprecated test/integration seam retained for one migration cycle. The
# kernel does not import SAM3; bundled Plugin code may consume an explicitly
# supplied factory when older integrations monkeypatch this symbol.
build_sam3_tools: Callable[..., list[Any]] | None = None


class _RunMetricsTracker:
    def __init__(self, session_db: SessionDB, session_id: str, run_id: str):
        self.session_db = session_db
        self.session_id = session_id
        self.run_id = run_id
        self.started_at = time.time()
        self._started_monotonic = time.monotonic()
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.llm_calls = 0
        self.usage_estimated = False
        self.status = "running"
        self.finished = False
        self.session_db.start_run_metrics(
            run_id,
            session_id,
            started_at=self.started_at,
        )

    def _duration_ms(self) -> int:
        return max(0, int((time.monotonic() - self._started_monotonic) * 1000))

    def record_response(self, response: ChatResponse) -> dict[str, Any]:
        self.input_tokens += max(0, int(response.input_tokens))
        self.output_tokens += max(0, int(response.output_tokens))
        self.total_tokens += max(
            0,
            int(response.total_tokens or response.input_tokens + response.output_tokens),
        )
        self.llm_calls += 1
        self.usage_estimated = self.usage_estimated or response.usage_estimated
        payload = self.payload()
        self.session_db.update_run_metrics(
            self.run_id,
            duration_ms=payload["duration_ms"],
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            llm_calls=self.llm_calls,
            usage_estimated=self.usage_estimated,
        )
        return payload

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "duration_ms": self._duration_ms(),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "llm_calls": self.llm_calls,
            "usage_estimated": self.usage_estimated,
            "status": self.status,
            "running": not self.finished,
        }

    def finish(self, status: str) -> dict[str, Any]:
        if self.finished:
            return self.payload()
        self.finished = True
        self.status = status
        payload = self.payload()
        self.session_db.finish_run_metrics(
            self.run_id,
            ended_at=time.time(),
            duration_ms=payload["duration_ms"],
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            llm_calls=self.llm_calls,
            usage_estimated=self.usage_estimated,
            status=status,
        )
        return payload


class AgentCore:
    def __init__(
        self,
        *,
        session_db: SessionDB,
        llm_provider: LLMProvider,
        prompt_builder: PromptBuilder | None = None,
        iface=None,
        qgis_executor=None,
        executor_config: dict[str, Any] | None = None,
        sam3_config: dict[str, Any] | None = None,
        plugins_config: dict[str, Any] | None = None,
        context_config: dict[str, Any] | None = None,
        custom_tools_dir: str | None = None,
        should_cancel: CancelChecker | None = None,
        context_engine: ContextEngine | None = None,
        llm_retry_attempts: int = 3,
        llm_retry_delay_seconds: float = 0.8,
    ):
        self.session_db = session_db
        self.llm_provider = llm_provider
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.iface = iface
        self.qgis_executor = qgis_executor
        self.executor_config = executor_config or {}
        self.sam3_config = sam3_config or {}
        self.plugins_config = plugins_config or {}
        self.context_config = context_config or {}
        self.plugin_manager: PluginManager | None = None
        self.custom_tools_dir = custom_tools_dir
        self.should_cancel = should_cancel or (lambda: False)
        self.context_engine = context_engine or QGISContextEngine(
            context_window_tokens=(
                int(getattr(llm_provider, "max_context_tokens", 0) or 0)
                if self.context_config.get("compression_enabled", True)
                else 0
            ),
            max_output_tokens=int(getattr(llm_provider, "max_tokens", 4096) or 4096),
            minimum_output_tokens=int(
                self.context_config.get("minimum_output_tokens", 2048)
            ),
            safety_tokens=int(self.context_config.get("safety_tokens", 256)),
            soft_threshold_ratio=float(
                self.context_config.get("soft_threshold_ratio", 0.55)
            ),
            max_inline_tool_result_chars=int(
                self.context_config.get("max_inline_tool_result_chars", 2400)
            ),
        )
        self.llm_retry_attempts = max(1, llm_retry_attempts)
        self.llm_retry_delay_seconds = max(0.0, llm_retry_delay_seconds)

    def run(
        self,
        *,
        session_id: str,
        user_message: str,
        emit: EventCallback | None = None,
        qgis_context: QGISContext | None = None,
    ) -> list[dict[str, Any]]:
        run_id = str(uuid.uuid4())
        events: list[dict[str, Any]] = []
        metrics = _RunMetricsTracker(self.session_db, session_id, run_id)
        terminal_status = "completed"

        def emit_event(event: dict[str, Any]) -> None:
            events.append(event)
            if emit is not None:
                emit(event)

        def publish(event: dict[str, Any]) -> None:
            nonlocal terminal_status
            if event.get("type") == "error":
                terminal_status = "failed"
            elif event.get("type") == "confirm_request":
                terminal_status = "waiting_confirmation"
            if event.get("type") == "complete" and not metrics.finished:
                if bool((event.get("payload") or {}).get("cancelled")):
                    terminal_status = "cancelled"
                metrics_payload = metrics.finish(terminal_status)
                emit_event(
                    agent_event(
                        "run_metrics",
                        metrics_payload,
                        session_id=session_id,
                        run_id=run_id,
                    )
                )
                event.setdefault("payload", {})["metrics"] = metrics_payload
            emit_event(event)

        def record_response(response: ChatResponse) -> None:
            publish(
                agent_event(
                    "run_metrics",
                    metrics.record_response(response),
                    session_id=session_id,
                    run_id=run_id,
                )
            )

        def check_cancelled() -> bool:
            if not self.should_cancel():
                return False
            content = "任务已停止。"
            self.session_db.save_message(
                session_id,
                "assistant",
                content,
                event_type="summary",
                run_id=run_id,
            )
            publish(agent_event("message", {"role": "assistant", "content": content, "model": self.llm_provider.model}, session_id=session_id, run_id=run_id))
            publish(agent_event("complete", {"cancelled": True}, session_id=session_id, run_id=run_id))
            return True

        publish(
            agent_event(
                "run_start",
                {"provider": self.llm_provider.name, **metrics.payload()},
                session_id=session_id,
                run_id=run_id,
            )
        )
        if check_cancelled():
            return events
        self.session_db.save_message(
            session_id,
            "user",
            user_message,
            event_type="user",
            run_id=run_id,
        )
        self.session_db.set_state(f"{session_id}:request_scope", run_id)

        try:
            if qgis_context is None:
                qgis_context = self._collect_qgis_context()
            active_skill = (
                self.session_db.get_state(f"{session_id}:active_skill")
                or "main-orchestrator"
            )
            loaded_skills = read_loaded_skills(
                self.session_db.get_state,
                session_id,
                self.prompt_builder.skill_manager,
            )
            if active_skill not in loaded_skills:
                active_skill = next(
                    (name for name in reversed(loaded_skills) if name != "main-orchestrator"),
                    "main-orchestrator",
                )
            # Build the registry first so Hermes availability metadata can be
            # evaluated against the tools/toolsets that are actually active.
            tool_registry = self._build_tool_registry(session_id)
            system_prompt = self._build_system_prompt(
                session_id,
                active_skill,
                qgis_context,
                loaded_skills=loaded_skills,
            )
            thinking_message = "正在组织上下文"
            self._save_process_message(session_id, thinking_message)
            publish(
                agent_event(
                    "thinking",
                    {"message": thinking_message},
                    session_id=session_id,
                    run_id=run_id,
                )
            )
            messages = self._build_conversation_messages(session_id)
        except Exception as exc:
            content = self._format_llm_error(exc)
            self._publish_final_error(content, publish, session_id, run_id)
            publish(agent_event("complete", {}, session_id=session_id, run_id=run_id))
            return events

        try:
            budget = IterationBudget()
            response = None
            while not budget.exhausted:
                if check_cancelled():
                    return events
                response = self._chat_with_retries(
                    system=system_prompt,
                    messages=messages,
                    tools=tool_registry.definitions_for_skills(loaded_skills),
                    publish=publish,
                    session_id=session_id,
                    run_id=run_id,
                    on_response=record_response,
                )
                if check_cancelled():
                    return events
                budget.record_iteration()
                task_must_continue = self._managed_task_must_continue(session_id)
                leaked_internal_result = self._looks_like_internal_tool_result(
                    response.content
                )
                if not response.tool_calls and (task_must_continue or leaked_internal_result):
                    expected = (
                        "模型刚才生成了看似工具结果的内部文本，但它不是实际工具调用，不能展示给用户。"
                        "必须使用已注册工具产生真实结果。"
                        if leaked_internal_result
                        else (
                            "当前通用计划尚未完成。请读取 get_task_state，继续未完成步骤；"
                            "需要用户补充时先把对应步骤标记为 waiting_for_user，"
                            "所有步骤完成后调用 finalize_task。"
                        )
                    )
                    messages.append(
                        ChatMessage(
                            role="assistant",
                            content=(
                                "当前任务尚未满足完成条件，不能把上一段不完整文本作为最终回复。"
                                f"{expected}"
                            ),
                        )
                    )
                    continue
                if not response.tool_calls:
                    break

                # Keep the provider-native tool-call chain. Tool results must
                # be returned with role=tool and the original call id; wrapping
                # them in an assistant prompt lets internal control text leak
                # into the user-visible answer and breaks confirmation resume.
                messages.append(self._assistant_tool_message(response))
                tool_results = []
                for call in response.tool_calls:
                    if check_cancelled():
                        return events
                    entry = tool_registry.get(call.name)
                    call_arguments = call.arguments
                    if call.name == "execute_gis_code":
                        preflight_result = validate_execute_gis_code_arguments(call.arguments)
                        if preflight_result is not None:
                            publish(
                                agent_event(
                                    "tool_start",
                                    {"name": call.name, "arguments": call.arguments},
                                    session_id=session_id,
                                    run_id=run_id,
                                )
                            )
                            self._save_process_message(
                                session_id, "执行前检查生成代码与预期输出。"
                            )
                            self.session_db.log_tool_call(
                                session_id,
                                call.name,
                                call.arguments,
                                preflight_result,
                                duration_ms=0,
                            )
                            tool_results.append(
                                {
                                    "call_id": call.id,
                                    "name": call.name,
                                    "arguments": call.arguments,
                                    "result": preflight_result,
                                }
                            )
                            publish(
                                agent_event(
                                    "tool_end",
                                    {"name": call.name, "result": preflight_result, "duration_ms": 0},
                                    session_id=session_id,
                                    run_id=run_id,
                                )
                            )
                            self._save_process_message(
                                session_id,
                                "生成代码未通过执行前检查，正在重新生成最终结果代码。\n"
                                f"error：{preflight_result['error']}",
                            )
                            budget.record_tool_call()
                            continue
                    if entry.requires_confirmation and entry.preflight is not None:
                        publish(
                            agent_event(
                                "tool_start",
                                {"name": call.name, "arguments": call_arguments},
                                session_id=session_id,
                                run_id=run_id,
                            )
                        )
                        self._save_process_message(session_id, f"执行前检查工具参数：{call.name}")
                        started = time.monotonic()
                        try:
                            preflight_result = entry.preflight(call_arguments)
                        except Exception as exc:
                            preflight_result = {
                                "success": False,
                                "error": str(exc),
                                "preflight_failed": True,
                            }
                        duration_ms = int((time.monotonic() - started) * 1000)
                        self.session_db.log_tool_call(
                            session_id,
                            call.name,
                            call_arguments,
                            preflight_result,
                            duration_ms=duration_ms,
                        )
                        publish(
                            agent_event(
                                "tool_end",
                                {
                                    "name": call.name,
                                    "result": preflight_result,
                                    "duration_ms": duration_ms,
                                },
                                session_id=session_id,
                                run_id=run_id,
                            )
                        )
                        if not preflight_result.get("success"):
                            tool_results.append(
                                {
                                    "call_id": call.id,
                                    "name": call.name,
                                    "arguments": call_arguments,
                                    "result": preflight_result,
                                }
                            )
                            self._save_process_message(
                                session_id,
                                f"工具参数预检失败：{call.name}\nerror：{preflight_result.get('error')}",
                            )
                            budget.record_tool_call()
                            continue
                        normalized_arguments = preflight_result.get("arguments")
                        if isinstance(normalized_arguments, dict):
                            call_arguments = normalized_arguments
                    if entry.requires_confirmation:
                        replay_result = self._confirmed_tool_replay(
                            session_id,
                            call.name,
                            call_arguments,
                            tool_registry,
                        )
                        if replay_result is not None:
                            self.session_db.log_tool_call(
                                session_id,
                                call.name,
                                call_arguments,
                                replay_result,
                                duration_ms=0,
                            )
                            tool_results.append(
                                {
                                    "call_id": call.id,
                                    "name": call.name,
                                    "arguments": call_arguments,
                                    "result": replay_result,
                                }
                            )
                            publish(
                                agent_event(
                                    "tool_end",
                                    {
                                        "name": call.name,
                                        "result": replay_result,
                                        "duration_ms": 0,
                                    },
                                    session_id=session_id,
                                    run_id=run_id,
                                )
                            )
                            self._save_process_message(
                                session_id,
                                self._duplicate_tool_process_message(call.name),
                            )
                            budget.record_tool_call()
                            continue
                        self._persist_tool_exchange(
                            session_id,
                            response,
                            tool_results,
                            run_id=run_id,
                        )
                        confirmation_id = str(uuid.uuid4())
                        pending = {
                            "confirmation_id": confirmation_id,
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "arguments": call_arguments,
                            "run_id": run_id,
                            "request_scope": self.session_db.get_state(
                                f"{session_id}:request_scope"
                            ) or run_id,
                        }
                        self.session_db.set_state(
                            self._confirmation_key(session_id, confirmation_id),
                            json.dumps(pending, ensure_ascii=False),
                        )
                        self.session_db.save_message(
                            session_id,
                            "assistant",
                            response.content,
                            event_type="tool_call",
                            finish_reason=response.finish_reason,
                            run_id=run_id,
                            tool_calls=self._tool_calls_payload([call]),
                        )
                        active_task = self.session_db.get_active_task(session_id)
                        if active_task is not None:
                            self.session_db.update_task(
                                str(active_task["id"]), status="waiting_confirmation"
                            )
                        publish(
                            agent_event(
                                "confirm_request",
                                {
                                    "confirmation_id": confirmation_id,
                                    "tool_name": call.name,
                                    "arguments": call_arguments,
                                    "destructive": entry.destructive,
                                    "writes_project": entry.writes_project,
                                    "description": entry.description,
                                },
                                session_id=session_id,
                                run_id=run_id,
                            )
                        )
                        content = (
                            f"工具 `{call.name}` 需要确认后才能执行。"
                            "请确认或取消该操作。"
                        )
                        self.session_db.save_message(
                            session_id,
                            "assistant",
                            content,
                            event_type="confirm_request",
                            run_id=run_id,
                        )
                        publish(
                            agent_event(
                                "message",
                                {"role": "assistant", "content": content, "model": response.model},
                                session_id=session_id,
                                run_id=run_id,
                            )
                        )
                        publish(agent_event("complete", {}, session_id=session_id, run_id=run_id))
                        return events

                    publish(
                        agent_event(
                            "tool_start",
                            {"name": call.name, "arguments": call_arguments},
                            session_id=session_id,
                            run_id=run_id,
                        )
                    )
                    self._save_process_message(session_id, f"调用工具：{call.name}")
                    self._publish_stage_start_if_needed(call.name, call.arguments, publish, session_id, run_id)
                    activation_failure = self._skill_activation_precondition(
                        session_id,
                        call.name,
                        call_arguments,
                    )
                    if activation_failure is not None:
                        result, duration_ms = activation_failure, 0
                    else:
                        result, duration_ms = tool_registry.execute(call.name, call_arguments)
                    if check_cancelled():
                        return events
                    if call.name == "set_active_skill" and result.get("success"):
                        next_skill = str(result.get("active_skill") or active_skill)
                        loaded_skills = write_loaded_skills(
                            self.session_db.set_state,
                            session_id,
                            self.prompt_builder.skill_manager,
                            [*loaded_skills, next_skill],
                        )
                        if next_skill == "gis-pipeline" and active_skill != "gis-pipeline":
                            start_pipeline_cycle(self.session_db, session_id)
                        active_skill = next_skill
                        system_prompt = self._build_system_prompt(
                            session_id,
                            active_skill,
                            qgis_context,
                            loaded_skills=loaded_skills,
                        )
                    elif call.name == "load_skill" and result.get("success"):
                        previous_skill = active_skill
                        loaded_skills = [str(name) for name in result.get("loaded_skills") or loaded_skills]
                        active_skill = str(result.get("skill_name") or active_skill)
                        self.session_db.set_state(
                            f"{session_id}:active_skill",
                            active_skill,
                        )
                        if active_skill == "gis-pipeline" and previous_skill != "gis-pipeline":
                            start_pipeline_cycle(self.session_db, session_id)
                        system_prompt = self._build_system_prompt(
                            session_id,
                            active_skill,
                            qgis_context,
                            loaded_skills=loaded_skills,
                        )
                        self._record_main_loop_skill_invocation(
                            session_id,
                            active_skill,
                            call.arguments,
                            result,
                        )
                    elif call.name == "unload_skill" and result.get("success"):
                        loaded_skills = [str(name) for name in result.get("loaded_skills") or loaded_skills]
                        active_skill = (
                            self.session_db.get_state(f"{session_id}:active_skill")
                            or "main-orchestrator"
                        )
                        system_prompt = self._build_system_prompt(
                            session_id,
                            active_skill,
                            qgis_context,
                            loaded_skills=loaded_skills,
                        )
                    self.session_db.log_tool_call(
                        session_id,
                        call.name,
                        call_arguments,
                        result,
                        duration_ms=duration_ms,
                    )
                    tool_results.append(
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call_arguments,
                            "result": result,
                        }
                    )
                    publish(
                        agent_event(
                            "tool_end",
                            {"name": call.name, "result": result, "duration_ms": duration_ms},
                            session_id=session_id,
                            run_id=run_id,
                        )
                    )
                    self._save_process_message(session_id, self._format_tool_process(call.name, result))
                    self._publish_stage_end_if_needed(call.name, result, publish, session_id, run_id)
                    requested_tool_call = self._requested_tool_call_from_result(result)
                    if requested_tool_call is not None:
                        self._request_tool_confirmation(
                            session_id=session_id,
                            run_id=run_id,
                            tool_name=requested_tool_call["name"],
                            arguments=requested_tool_call["arguments"],
                            tool_registry=tool_registry,
                            publish=publish,
                            model=response.model,
                        )
                        return events
                    budget.record_tool_call()
                    if check_cancelled():
                        return events
                if active_skill == "gis-pipeline" and self._pipeline_context_should_compact(
                    tool_results
                ):
                    messages = self._build_pipeline_stage_messages(session_id)
                    messages.append(self._assistant_tool_message(response))
                    result_context = self._compact_pipeline_tool_results(tool_results)
                else:
                    result_context = tool_results
                self._persist_tool_exchange(
                    session_id,
                    response,
                    tool_results,
                    run_id=run_id,
                )
                for item in result_context:
                    messages.append(self._tool_result_message(item))

            if response is None:
                response = self._chat_with_retries(
                    system=system_prompt,
                    messages=messages,
                    publish=publish,
                    session_id=session_id,
                    run_id=run_id,
                    on_response=record_response,
                )
                if check_cancelled():
                    return events
            elif budget.exhausted and (
                response.tool_calls
                or self._managed_task_must_continue(session_id)
            ):
                response = type(response)(
                    content="任务未完成：已达到本轮工具调用预算。请缩小任务范围或补充更明确的图层、字段和输出要求后重试。",
                    model=response.model,
                    finish_reason="tool_budget_exhausted",
                    tool_calls=[],
                )

            if check_cancelled():
                return events
            self.session_db.save_message(
                session_id,
                "assistant",
                response.content,
                event_type="summary",
                finish_reason=response.finish_reason,
                run_id=run_id,
            )
            self._publish_message_deltas(
                response.content,
                publish=publish,
                session_id=session_id,
                run_id=run_id,
                model=response.model,
            )
            publish(
                agent_event(
                    "message",
                    {
                        "role": "assistant",
                        "content": response.content,
                        "model": response.model,
                    },
                    session_id=session_id,
                    run_id=run_id,
                )
            )
            self._reset_task_scoped_skills(session_id)
        except Exception as exc:
            content = self._format_llm_error(exc)
            self._publish_final_error(content, publish, session_id, run_id)

        publish(agent_event("complete", {}, session_id=session_id, run_id=run_id))
        return events

    def confirm_tool_call(
        self,
        *,
        session_id: str,
        confirmation_id: str,
        approved: bool,
        emit: EventCallback | None = None,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        run_id = str(uuid.uuid4())
        metrics: _RunMetricsTracker
        terminal_status = "completed"

        def emit_event(event: dict[str, Any]) -> None:
            events.append(event)
            if emit is not None:
                emit(event)

        def publish(event: dict[str, Any]) -> None:
            nonlocal terminal_status
            if event.get("type") == "error":
                terminal_status = "failed"
            if event.get("type") == "complete" and not metrics.finished:
                metrics_payload = metrics.finish(terminal_status)
                emit_event(
                    agent_event(
                        "run_metrics",
                        metrics_payload,
                        session_id=session_id,
                        run_id=run_id,
                    )
                )
                event.setdefault("payload", {})["metrics"] = metrics_payload
            emit_event(event)

        def record_response(response: ChatResponse) -> None:
            publish(
                agent_event(
                    "run_metrics",
                    metrics.record_response(response),
                    session_id=session_id,
                    run_id=run_id,
                )
            )

        key = self._confirmation_key(session_id, confirmation_id)
        raw = self.session_db.get_state(key)
        if not raw:
            raise ValueError(f"确认请求不存在或已处理：{confirmation_id}")
        pending = json.loads(raw)
        self.session_db.delete_state(key)
        self.session_db.set_state(
            f"{session_id}:request_scope",
            str(pending.get("request_scope") or pending.get("run_id") or run_id),
        )
        metrics = _RunMetricsTracker(self.session_db, session_id, run_id)

        publish(
            agent_event(
                "run_start",
                {"provider": self.llm_provider.name, **metrics.payload()},
                session_id=session_id,
                run_id=run_id,
            )
        )

        tool_name = str(pending.get("tool_name") or "")
        arguments = pending.get("arguments") or {}
        publish(
            agent_event(
                "confirm_resolved",
                {
                    "confirmation_id": confirmation_id,
                    "tool_name": tool_name,
                    "approved": approved,
                },
                session_id=session_id,
                run_id=run_id,
            )
        )
        self._save_process_message(session_id, "已确认工具操作。" if approved else "已取消工具操作。")

        active_task = self.session_db.get_active_task(session_id)
        if active_task is not None:
            self.session_db.update_task(
                str(active_task["id"]),
                status="running" if approved else "waiting_for_user",
            )

        if not approved:
            self._reset_one_shot_skill(session_id)
            terminal_status = "cancelled"
            content = f"已取消工具 `{tool_name}`。"
            self.session_db.save_message(
                session_id,
                "assistant",
                content,
                event_type="summary",
                run_id=run_id,
            )
            publish(
                agent_event(
                    "message",
                    {"role": "assistant", "content": content, "model": self.llm_provider.model},
                    session_id=session_id,
                    run_id=run_id,
                )
            )
            publish(agent_event("complete", {}, session_id=session_id, run_id=run_id))
            return events

        tool_registry = self._build_tool_registry(session_id)
        publish(
            agent_event(
                "tool_start",
                {"name": tool_name, "arguments": arguments},
                session_id=session_id,
                run_id=run_id,
            )
        )
        self._save_process_message(session_id, f"调用工具：{tool_name}")
        self._publish_stage_start_if_needed(tool_name, arguments, publish, session_id, run_id)
        result, duration_ms = tool_registry.execute(tool_name, arguments)
        self.session_db.save_message(
            session_id,
            "tool",
            json.dumps(result, ensure_ascii=False),
            event_type="tool_result",
            run_id=run_id,
            tool_call_id=str(pending.get("tool_call_id") or confirmation_id),
            tool_name=tool_name,
        )
        self.session_db.log_tool_call(
            session_id,
            tool_name,
            arguments,
            result,
            duration_ms=duration_ms,
        )
        publish(
            agent_event(
                "tool_end",
                {"name": tool_name, "result": result, "duration_ms": duration_ms},
                session_id=session_id,
                run_id=run_id,
            )
        )
        self._save_process_message(session_id, self._format_tool_process(tool_name, result))
        self._publish_stage_end_if_needed(tool_name, result, publish, session_id, run_id)

        if tool_name == "execute_gis_code" and not result.get("success", True):
            try:
                retry_result = self._retry_failed_code_execution(
                    session_id=session_id,
                    failed_arguments=arguments,
                    failed_result=result,
                    tool_registry=tool_registry,
                    publish=publish,
                    run_id=run_id,
                    on_response=record_response,
                )
            except Exception as exc:
                content = self._format_llm_error(exc)
                self._publish_final_error(content, publish, session_id, run_id)
                publish(agent_event("complete", {}, session_id=session_id, run_id=run_id))
                return events
            if retry_result is not None:
                result = retry_result

        if result.get("success", True):
            self._remember_confirmed_tool_call(
                session_id,
                tool_name,
                arguments,
                result,
                tool_registry,
            )
        self._capture_confirmed_tool_artifacts(session_id, tool_name, result)
        if tool_name == "execute_gis_code":
            self._record_pipeline_execution_result(
                session_id=session_id,
                result=result,
                tool_registry=tool_registry,
                publish=publish,
                run_id=run_id,
            )
        self._reset_one_shot_skill(session_id)

        provisional_content = (
            self._format_tool_success(tool_name, result)
            if result.get("success", True)
            else self._format_tool_failure(tool_name, result)
        )
        task_completed = result.get("success", True) and self._finalize_ready_task(
            session_id,
            provisional_content,
        )
        active_task = self.session_db.get_active_task(session_id)
        should_resume_plan = result.get("success", True) and not task_completed and (
            self._managed_task_must_continue(session_id)
            or (
                active_task is None
                and tool_registry.get(tool_name).resume_policy == "continue_plan"
            )
            or (
                active_task is not None
                and active_task.get("status") not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "waiting_for_user",
                    "waiting_confirmation",
                }
            )
        )
        if should_resume_plan:
            try:
                content, paused_for_confirmation = self._continue_after_confirmed_tool(
                    session_id=session_id,
                    confirmed_tool_name=tool_name,
                    confirmed_result=result,
                    tool_registry=tool_registry,
                    publish=publish,
                    run_id=run_id,
                    on_response=record_response,
                )
            except Exception as exc:
                content = self._format_llm_error(exc)
                self._publish_final_error(content, publish, session_id, run_id)
                publish(agent_event("complete", {}, session_id=session_id, run_id=run_id))
                return events
            if paused_for_confirmation:
                return events
        elif result.get("success", True):
            content = provisional_content
        else:
            content = provisional_content
        self._finalize_ready_task(session_id, content)
        self.session_db.save_message(
            session_id,
            "assistant",
            content,
            event_type="summary",
            run_id=run_id,
        )
        self._publish_message_deltas(
            content,
            publish=publish,
            session_id=session_id,
            run_id=run_id,
            model=self.llm_provider.model,
        )
        publish(
            agent_event(
                "message",
                {"role": "assistant", "content": content, "model": self.llm_provider.model},
                session_id=session_id,
                run_id=run_id,
            )
        )
        self._reset_task_scoped_skills(session_id)
        publish(agent_event("complete", {}, session_id=session_id, run_id=run_id))
        return events

    def _continue_after_confirmed_tool(
        self,
        *,
        session_id: str,
        confirmed_tool_name: str,
        confirmed_result: dict[str, Any],
        tool_registry: ToolRegistry,
        publish: EventCallback,
        run_id: str,
        on_response: Callable[[ChatResponse], None] | None = None,
    ) -> tuple[str, bool]:
        """Resume the same agent turn after a confirmed tool has completed.

        Confirmation used to terminate the turn after executing one tool. In a
        managed multi-step task, confirmed outputs are intermediate artifacts
        and must be returned to the agent loop so the next Skill can run.
        """
        qgis_context = self._collect_qgis_context()
        loaded_skills = read_loaded_skills(
            self.session_db.get_state,
            session_id,
            self.prompt_builder.skill_manager,
        )
        active_skill = self.session_db.get_state(f"{session_id}:active_skill") or ""
        if active_skill not in loaded_skills:
            active_skill = next(
                (name for name in reversed(loaded_skills) if name != "main-orchestrator"),
                "main-orchestrator",
            )
        system_prompt = self._build_system_prompt(
            session_id,
            active_skill,
            qgis_context,
            loaded_skills=loaded_skills,
        )
        messages = self._build_conversation_messages(session_id)
        continuation_instruction = (
            "这是当前计划步骤的执行产物。若存在活动计划，必须调用 get_task_state 读取下一未完成步骤，"
            "并使用 artifacts、outputs 或 loaded_layers 中的真实路径和 layer_id 继续；"
            "只有全部步骤和交付物完成后才能最终回复。"
        )
        # The confirmed result is already persisted as a role=tool message.
        # Keep only a short policy reminder; never serialize the result again
        # as assistant-authored text.
        messages.append(
            ChatMessage(
                role="system",
                content=(
                    "已确认工具的结果是当前请求的中间产物，不是任务完成信号。"
                    + continuation_instruction
                ),
            )
        )
        budget = IterationBudget()
        last_response: ChatResponse | None = None
        failure_counts: dict[str, int] = {}

        def repeated_failure(
            name: str,
            arguments: dict[str, Any],
            result: dict[str, Any],
        ) -> bool:
            if result.get("success", True):
                return False
            signature = json.dumps(
                {
                    "name": name,
                    "arguments": arguments,
                    "error": result.get("error"),
                    "error_code": result.get("error_code"),
                    "completion_errors": result.get("completion_errors") or [],
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            failure_counts[signature] = failure_counts.get(signature, 0) + 1
            return failure_counts[signature] >= 2

        while not budget.exhausted:
            response = self._chat_with_retries(
                system=system_prompt,
                messages=messages,
                tools=tool_registry.definitions_for_skills(loaded_skills),
                publish=publish,
                session_id=session_id,
                run_id=run_id,
                on_response=on_response,
            )
            last_response = response
            budget.record_iteration()
            if not response.tool_calls:
                task_must_continue = self._managed_task_must_continue(session_id)
                leaked_internal_result = self._looks_like_internal_tool_result(
                    response.content
                )
                if task_must_continue or leaked_internal_result:
                    continuation = (
                        "模型刚才生成了看似工具结果的内部文本；必须改为真实工具调用。"
                        if leaked_internal_result
                        else (
                            "当前计划尚未完成。读取 get_task_state 并继续未完成步骤；"
                            "完成全部步骤和必需产物后再调用 finalize_task。"
                        )
                    )
                    messages.append(
                        ChatMessage(
                            role="assistant",
                            content=continuation,
                        )
                    )
                    continue
                return response.content, False

            messages.append(self._assistant_tool_message(response))
            tool_results: list[dict[str, Any]] = []
            terminal_failure: tuple[str, dict[str, Any]] | None = None
            finalized_summary: str | None = None
            for call in response.tool_calls:
                entry = tool_registry.get(call.name)
                call_arguments = call.arguments
                preflight_result: dict[str, Any] | None = None
                if call.name == "execute_gis_code":
                    preflight_result = validate_execute_gis_code_arguments(call_arguments)
                elif entry.requires_confirmation and entry.preflight is not None:
                    publish(
                        agent_event(
                            "tool_start",
                            {"name": call.name, "arguments": call_arguments},
                            session_id=session_id,
                            run_id=run_id,
                        )
                    )
                    started = time.monotonic()
                    try:
                        preflight_result = entry.preflight(call_arguments)
                    except Exception as exc:
                        preflight_result = {
                            "success": False,
                            "error": str(exc),
                            "preflight_failed": True,
                        }
                    duration_ms = int((time.monotonic() - started) * 1000)
                    self.session_db.log_tool_call(
                        session_id,
                        call.name,
                        call_arguments,
                        preflight_result,
                        duration_ms=duration_ms,
                    )
                    publish(
                        agent_event(
                            "tool_end",
                            {
                                "name": call.name,
                                "result": preflight_result,
                                "duration_ms": duration_ms,
                            },
                            session_id=session_id,
                            run_id=run_id,
                        )
                    )
                    if preflight_result.get("success"):
                        normalized = preflight_result.get("arguments")
                        if isinstance(normalized, dict):
                            call_arguments = normalized

                if preflight_result is not None and not preflight_result.get("success"):
                    if call.name == "execute_gis_code":
                        publish(
                            agent_event(
                                "tool_start",
                                {"name": call.name, "arguments": call_arguments},
                                session_id=session_id,
                                run_id=run_id,
                            )
                        )
                        self.session_db.log_tool_call(
                            session_id,
                            call.name,
                            call_arguments,
                            preflight_result,
                            duration_ms=0,
                        )
                        publish(
                            agent_event(
                                "tool_end",
                                {"name": call.name, "result": preflight_result, "duration_ms": 0},
                                session_id=session_id,
                                run_id=run_id,
                            )
                        )
                    tool_results.append(
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call_arguments,
                            "result": preflight_result,
                        }
                    )
                    budget.record_tool_call()
                    if repeated_failure(call.name, call_arguments, preflight_result):
                        terminal_failure = (
                            call.name,
                            {
                                **preflight_result,
                                "error": (
                                    f"{preflight_result.get('error') or '工具调用失败'} "
                                    "相同参数已连续失败两次，已停止自动重试。"
                                ),
                                "duplicate_failure_prevented": True,
                            },
                        )
                        break
                    continue

                if entry.requires_confirmation:
                    replay_result = self._confirmed_tool_replay(
                        session_id,
                        call.name,
                        call_arguments,
                        tool_registry,
                    )
                    if replay_result is not None:
                        self.session_db.log_tool_call(
                            session_id,
                            call.name,
                            call_arguments,
                            replay_result,
                            duration_ms=0,
                        )
                        publish(
                            agent_event(
                                "tool_end",
                                {
                                    "name": call.name,
                                    "result": replay_result,
                                    "duration_ms": 0,
                                },
                                session_id=session_id,
                                run_id=run_id,
                            )
                        )
                        self._save_process_message(
                            session_id,
                            self._duplicate_tool_process_message(call.name),
                        )
                        tool_results.append(
                            {
                                "call_id": call.id,
                                "name": call.name,
                                "arguments": call_arguments,
                                "result": replay_result,
                            }
                        )
                        budget.record_tool_call()
                        continue
                    self._persist_tool_exchange(
                        session_id,
                        response,
                        tool_results,
                        run_id=run_id,
                    )
                    self._request_tool_confirmation(
                        session_id=session_id,
                        run_id=run_id,
                        tool_name=call.name,
                        arguments=call_arguments,
                        tool_registry=tool_registry,
                        publish=publish,
                        model=response.model,
                        tool_call_id=call.id,
                        assistant_content=response.content,
                    )
                    return "", True

                publish(
                    agent_event(
                        "tool_start",
                        {"name": call.name, "arguments": call_arguments},
                        session_id=session_id,
                        run_id=run_id,
                    )
                )
                self._save_process_message(session_id, f"调用工具：{call.name}")
                self._publish_stage_start_if_needed(
                    call.name, call_arguments, publish, session_id, run_id
                )
                previous_skill = active_skill
                activation_failure = self._skill_activation_precondition(
                    session_id,
                    call.name,
                    call_arguments,
                )
                if activation_failure is not None:
                    result, duration_ms = activation_failure, 0
                else:
                    result, duration_ms = tool_registry.execute(call.name, call_arguments)
                if call.name in {"load_skill", "set_active_skill"} and result.get("success"):
                    active_skill = str(
                        result.get("skill_name")
                        or result.get("active_skill")
                        or active_skill
                    )
                    self.session_db.set_state(f"{session_id}:active_skill", active_skill)
                    loaded_skills = read_loaded_skills(
                        self.session_db.get_state,
                        session_id,
                        self.prompt_builder.skill_manager,
                    )
                    if active_skill == "gis-pipeline" and previous_skill != "gis-pipeline":
                        start_pipeline_cycle(self.session_db, session_id)
                    if call.name == "load_skill":
                        self._record_main_loop_skill_invocation(
                            session_id,
                            active_skill,
                            call_arguments,
                            result,
                        )
                elif call.name == "unload_skill" and result.get("success"):
                    loaded_skills = read_loaded_skills(
                        self.session_db.get_state,
                        session_id,
                        self.prompt_builder.skill_manager,
                    )
                    active_skill = self.session_db.get_state(
                        f"{session_id}:active_skill"
                    ) or "main-orchestrator"
                self.session_db.log_tool_call(
                    session_id,
                    call.name,
                    call_arguments,
                    result,
                    duration_ms=duration_ms,
                )
                publish(
                    agent_event(
                        "tool_end",
                        {"name": call.name, "result": result, "duration_ms": duration_ms},
                        session_id=session_id,
                        run_id=run_id,
                    )
                )
                self._save_process_message(
                    session_id, self._format_tool_process(call.name, result)
                )
                self._publish_stage_end_if_needed(
                    call.name, result, publish, session_id, run_id
                )
                requested_tool_call = self._requested_tool_call_from_result(result)
                if requested_tool_call is not None:
                    self._request_tool_confirmation(
                        session_id=session_id,
                        run_id=run_id,
                        tool_name=requested_tool_call["name"],
                        arguments=requested_tool_call["arguments"],
                        tool_registry=tool_registry,
                        publish=publish,
                        model=response.model,
                    )
                    return "", True
                tool_results.append(
                    {
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call_arguments,
                        "result": result,
                    }
                )
                budget.record_tool_call()
                if call.name == "finalize_task" and result.get("success"):
                    finalized_summary = str(
                        result.get("summary")
                        or call_arguments.get("summary")
                        or "任务已完成。"
                    )
                    break
                if repeated_failure(call.name, call_arguments, result):
                    terminal_failure = (
                        call.name,
                        {
                            **result,
                            "error": (
                                f"{result.get('error') or '工具调用失败'} "
                                "相同参数已连续失败两次，已停止自动重试。"
                            ),
                            "duplicate_failure_prevented": True,
                        },
                    )
                    break

            qgis_context = self._collect_qgis_context()
            system_prompt = self._build_system_prompt(
                session_id,
                active_skill,
                qgis_context,
                loaded_skills=loaded_skills,
            )
            self._persist_tool_exchange(
                session_id,
                response,
                tool_results,
                run_id=run_id,
            )
            for item in tool_results:
                messages.append(self._tool_result_message(item))
            if finalized_summary is not None:
                return finalized_summary, False
            if terminal_failure is not None:
                failed_tool, failed_result = terminal_failure
                return self._format_tool_failure(failed_tool, failed_result), False

        if last_response is not None and last_response.content:
            return last_response.content, False
        return "任务未完成：确认后的后续处理达到迭代或工具预算。", False

    def _build_tool_registry(self, session_id: str) -> ToolRegistry:
        registry = ToolRegistry()
        registry.set_skill_tools(self.prompt_builder.skill_manager.tool_allowlist())
        registry.register(
            build_set_active_skill_tool(
                self.session_db.set_state,
                session_id,
                self.prompt_builder.skill_manager,
            )
        )
        registry.register(build_search_skills_tool(self.prompt_builder.skill_manager))
        registry.register(
            build_load_skill_tool(
                self.prompt_builder.skill_manager,
                self.session_db.get_state,
                self.session_db.set_state,
                session_id,
                self.session_db.record_skill_usage,
            )
        )
        registry.register(
            build_unload_skill_tool(
                self.prompt_builder.skill_manager,
                self.session_db.get_state,
                self.session_db.set_state,
                session_id,
            )
        )
        registry.register(
            build_list_loaded_skills_tool(
                self.prompt_builder.skill_manager,
                self.session_db.get_state,
                session_id,
            )
        )
        registry.register(
            build_inspect_skill_tool(
                self.prompt_builder.skill_manager,
                self.session_db.record_skill_usage,
            )
        )
        registry.register(
            build_load_skill_reference_tool(
                self.prompt_builder.skill_manager,
                self.session_db.record_skill_usage,
            )
        )
        for entry in build_task_planning_tools(
            self.session_db,
            session_id,
            self.prompt_builder.skill_manager,
        ):
            registry.register(entry)
        for entry in build_learning_tools(self.session_db, session_id):
            registry.register(entry)
        registry.register(build_get_task_context_tool(self.iface, qgis_executor=self.qgis_executor))
        registry.register(build_search_messages_tool(self.session_db, session_id))
        registry.register(build_record_pipeline_stage_tool(self.session_db, session_id))
        registry.register(
            build_execute_gis_code_tool(
                session_db=self.session_db,
                session_id=session_id,
                iface=self.iface,
                qgis_executor=self.qgis_executor,
                executor_config=self.executor_config,
            )
        )
        registry.register(
            build_inspect_school_service_coverage_inputs_tool(
                qgis_executor=self.qgis_executor,
            )
        )
        registry.register(
            build_inspect_cultivated_land_loss_inputs_tool(
                qgis_executor=self.qgis_executor,
            )
        )
        registry.register(
            build_inspect_land_use_building_metrics_inputs_tool(
                qgis_executor=self.qgis_executor,
            )
        )
        registry.register(
            build_inspect_land_cover_map_inputs_tool(
                qgis_executor=self.qgis_executor,
            )
        )
        registry.register(
            build_school_service_coverage_tool(
                session_db=self.session_db,
                session_id=session_id,
                iface=self.iface,
                qgis_executor=self.qgis_executor,
                executor_config=self.executor_config,
            )
        )
        registry.register(
            build_cultivated_land_loss_analysis_tool(
                session_db=self.session_db,
                session_id=session_id,
                iface=self.iface,
                qgis_executor=self.qgis_executor,
                executor_config=self.executor_config,
            )
        )
        registry.register(
            build_land_use_building_metrics_tool(
                session_db=self.session_db,
                session_id=session_id,
                iface=self.iface,
                qgis_executor=self.qgis_executor,
                executor_config=self.executor_config,
            )
        )
        registry.register(
            build_generate_land_cover_map_tool(
                session_db=self.session_db,
                session_id=session_id,
                iface=self.iface,
                qgis_executor=self.qgis_executor,
                executor_config=self.executor_config,
            )
        )
        for entry in build_layer_tools(
            session_db=self.session_db,
            session_id=session_id,
            iface=self.iface,
            qgis_executor=self.qgis_executor,
        ):
            registry.register(entry)
        for entry in build_qgis_toolbox_tools(
            session_db=self.session_db,
            session_id=session_id,
            iface=self.iface,
            qgis_executor=self.qgis_executor,
            executor_config=self.executor_config,
        ):
            registry.register(entry)
        self.plugin_manager = PluginManager(
            skill_manager=self.prompt_builder.skill_manager,
            runtime={
                "session_db": self.session_db,
                "session_id": session_id,
                "iface": self.iface,
                "qgis_executor": self.qgis_executor,
                "executor_config": self.executor_config,
                "should_cancel": self.should_cancel,
                "plugin_config": {"sam3": self.sam3_config},
                "sam3_tool_factory": build_sam3_tools,
            },
            user_dir=self.plugins_config.get("user_dir"),
            project_dir=(
                self.plugins_config.get("project_dir")
                if self.plugins_config.get("enable_project_plugins")
                else None
            ),
            enabled=self.plugins_config.get("enabled") or (),
            disabled=self.plugins_config.get("disabled") or (),
        )
        self.plugin_manager.register_all(registry)
        registry.register(self.plugin_manager.build_diagnostics_tool())
        self.plugin_manager.attach_hooks(registry)
        for entry in load_custom_tool_entries(self.custom_tools_dir):
            registry.register(entry)
        registry.register(
            build_invoke_skill_tool(
                session_db=self.session_db,
                session_id=session_id,
                llm_provider=self.llm_provider,
                skill_manager=self.prompt_builder.skill_manager,
                tool_registry=registry,
                should_cancel=self.should_cancel,
            )
        )
        self.prompt_builder.skill_manager.set_runtime_capabilities(
            available_tools=registry.tool_names(),
            active_toolsets=registry.toolsets(),
        )
        registry.set_skill_tools(self.prompt_builder.skill_manager.tool_allowlist())
        self.context_engine.set_tool_reducers(registry.context_reducers())
        return registry

    def _managed_task_must_continue(self, session_id: str) -> bool:
        task = self.session_db.get_active_task(session_id)
        if task is None or task.get("status") in {
            "completed",
            "failed",
            "cancelled",
            "waiting_for_user",
            "waiting_confirmation",
        }:
            return False
        steps = self.session_db.get_plan_steps(str(task["id"]))
        return any(
            step.get("status") not in {"completed", "skipped"}
            for step in steps
        )

    @staticmethod
    def _looks_like_internal_tool_result(content: str) -> bool:
        text = str(content or "").strip()
        if not text:
            return False
        internal_prefixes = (
            "以下是刚刚执行的 QGIS 工具结果",
            "以下是继续执行用户原始请求得到的工具结果",
        )
        if any(text.startswith(prefix) for prefix in internal_prefixes):
            return True
        return '"name"' in text and '"result"' in text and '"success"' in text

    def _skill_activation_precondition(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Keep generic Pipeline activation behind the persisted task plan.

        Semantic routing remains the model's responsibility.  Once it chooses a
        multi-step Pipeline, however, the runtime must not let that generic path
        bypass planning or an earlier specialised-Skill step.
        """
        if tool_name not in {"load_skill", "set_active_skill"}:
            return None
        target_skill = str(arguments.get("skill_name") or "").strip()
        if target_skill != "gis-pipeline":
            return None

        task = self.session_db.get_active_task(session_id)
        if task is None or task.get("status") in {"completed", "failed", "cancelled"}:
            return {
                "success": False,
                "error_code": "plan_required_before_pipeline",
                "error": (
                    "gis-pipeline 只能在 create_plan 建立多步骤计划后加载。"
                    "请重新对照专用 Skill 目录；若任务包含影像地物提取与后续统计，"
                    "计划必须先执行对应的影像分割 Skill，再把 gis-pipeline 设为依赖步骤。"
                ),
            }

        steps = self.session_db.get_plan_steps(str(task["id"]))
        if not steps:
            return {
                "success": False,
                "error_code": "plan_required_before_pipeline",
                "error": "当前任务尚无计划步骤，请先调用 create_plan，再加载 gis-pipeline。",
            }

        steps_by_id = {str(step["id"]): step for step in steps}
        ready_steps: list[dict[str, Any]] = []
        for step in steps:
            if step.get("status") in {"completed", "skipped"}:
                continue
            dependencies = [
                steps_by_id.get(str(dependency))
                for dependency in step.get("dependencies") or []
            ]
            if all(
                dependency is not None
                and dependency.get("status") in {"completed", "skipped"}
                for dependency in dependencies
            ):
                ready_steps.append(step)

        required_step = next(
            (
                step
                for step in ready_steps
                if str(step.get("skill_name") or "").strip()
            ),
            None,
        )
        if required_step is None:
            return None
        required_skill = str(required_step.get("skill_name") or "").strip()
        if required_skill == target_skill:
            return None
        return {
            "success": False,
            "error_code": "skill_order_violation",
            "error": (
                f"计划中的当前前置步骤 {required_step['id']} 必须先使用 "
                f"{required_skill} 完成；在该步骤及其真实产物完成前不能加载 "
                "gis-pipeline。"
            ),
            "required_step_id": str(required_step["id"]),
            "required_skill": required_skill,
        }

    def _record_main_loop_skill_invocation(
        self,
        session_id: str,
        skill_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        task = self.session_db.get_active_task(session_id)
        if task is None:
            return
        steps = self.session_db.get_plan_steps(str(task["id"]))
        step = next(
            (
                item
                for item in steps
                if self.prompt_builder.skill_manager.canonical_name(
                    str(item.get("skill_name") or "")
                ) == self.prompt_builder.skill_manager.canonical_name(skill_name)
                and item.get("status") in {"pending", "in_progress", "waiting_for_user"}
            ),
            None,
        )
        invocation_id = self.session_db.start_skill_invocation(
            str(task["id"]),
            skill_name,
            step_id=str(step["id"]) if step else None,
            execution_mode="main_loop",
            arguments=arguments,
        )
        self.session_db.finish_skill_invocation(
            invocation_id,
            status="loaded",
            result={
                "loaded_skills": result.get("loaded_skills") or [],
                "already_loaded": bool(result.get("already_loaded")),
            },
        )

    def _capture_confirmed_tool_artifacts(
        self,
        session_id: str,
        tool_name: str,
        result: dict[str, Any],
    ) -> None:
        task = self.session_db.get_active_task(session_id)
        if task is None:
            return
        task_id = str(task["id"])
        steps = self.session_db.get_plan_steps(task_id)
        active_skill = self.session_db.get_state(f"{session_id}:active_skill") or ""
        step = next(
            (
                item
                for item in steps
                if item.get("status") == "in_progress"
                and (
                    not item.get("skill_name")
                    or self.prompt_builder.skill_manager.canonical_name(
                        str(item.get("skill_name"))
                    )
                    == self.prompt_builder.skill_manager.canonical_name(active_skill)
                )
            ),
            None,
        ) or next(
            (
                item
                for item in steps
                if item.get("status") == "pending"
                and (
                    not item.get("skill_name")
                    or self.prompt_builder.skill_manager.canonical_name(
                        str(item.get("skill_name"))
                    )
                    == self.prompt_builder.skill_manager.canonical_name(active_skill)
                )
            ),
            None,
        )
        if step is None:
            return
        step_id = str(step["id"])
        if not result.get("success", True):
            self.session_db.update_plan_step(
                task_id,
                step_id,
                status="in_progress",
                error=str(result.get("error") or "工具执行失败"),
            )
            self.session_db.update_task(task_id, status="running")
            return

        artifacts = self._artifacts_from_tool_result(tool_name, result)
        artifact_ids = []
        for artifact in artifacts:
            artifact_ids.append(
                self.session_db.register_artifact(
                    task_id,
                    str(artifact["artifact_type"]),
                    step_id=step_id,
                    name=str(artifact.get("name") or "") or None,
                    uri=str(artifact.get("uri") or "") or None,
                    payload=artifact.get("payload") or {},
                    producer=active_skill or tool_name,
                    verified=bool(artifact.get("verified", True)),
                )
            )
        outputs = {
            "tool_name": tool_name,
            "artifacts": artifact_ids,
            "outputs": result.get("outputs") or [],
            "loaded_layers": result.get("loaded_layers") or [],
            "parameters": result.get("parameters") or {},
        }
        self.session_db.update_plan_step(
            task_id,
            step_id,
            status="completed",
            outputs=outputs,
            error="",
        )
        self.session_db.update_task(task_id, status="running")

    @staticmethod
    def _artifacts_from_tool_result(
        tool_name: str,
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        declared = result.get("artifacts")
        if isinstance(declared, list):
            return [dict(item) for item in declared if isinstance(item, dict)]
        type_map = {
            "execute_school_service_coverage": ["coverage_layer", "group_statistics"],
            "execute_land_use_building_metrics": ["metrics_layer", "metrics_table"],
            "execute_cultivated_land_loss_analysis": [
                "report_table",
                "detail_layer",
                "metrics_table",
                "quality_report",
            ],
            "generate_land_cover_map": ["map_png", "map_pdf"],
        }
        mapped_types = type_map.get(tool_name, [])
        artifacts: list[dict[str, Any]] = []
        for index, output in enumerate(result.get("outputs") or []):
            if not isinstance(output, dict):
                continue
            artifact_type = (
                mapped_types[index]
                if index < len(mapped_types)
                else str(output.get("type") or "file")
            )
            artifacts.append(
                {
                    "artifact_type": artifact_type,
                    "name": output.get("name") or output.get("path"),
                    "uri": output.get("path") or output.get("absolute_path"),
                    "payload": output,
                    "verified": (
                        bool(output.get("verified"))
                        if tool_name == "execute_gis_code"
                        else bool(output.get("verified", True))
                    ),
                }
            )
        if tool_name == "generate_land_cover_map":
            artifacts.append(
                {
                    "artifact_type": "layout",
                    "name": str((result.get("map_parameters") or {}).get("title") or "土地覆盖专题图"),
                    "payload": {"loaded_layers": result.get("loaded_layers") or []},
                    "verified": True,
                }
            )
        if tool_name == "execute_gis_code" and artifacts:
            execution_outputs = [
                output
                for output in result.get("outputs") or []
                if isinstance(output, dict)
            ]
            artifacts.append(
                {
                    "artifact_type": "execution_outputs",
                    "name": "GIS execution outputs",
                    "payload": {"outputs": result.get("outputs") or []},
                    "verified": bool(execution_outputs)
                    and all(output.get("verified") for output in execution_outputs),
                }
            )
        return artifacts

    def _finalize_ready_task(self, session_id: str, summary: str) -> bool:
        task = self.session_db.get_active_task(session_id)
        if task is None:
            return False
        if task.get("status") == "completed":
            return True
        state = self.session_db.get_task_state(str(task["id"]))
        if state is None:
            return False
        if verify_task_completion(state, self.prompt_builder.skill_manager):
            return False
        self.session_db.update_task(
            str(task["id"]),
            status="completed",
            summary=summary,
            finalization={
                "summary": summary,
                "automatic_after_confirmation": True,
                "artifacts": [artifact["id"] for artifact in state.get("artifacts") or []],
            },
        )
        self.session_db.record_task_outcome(
            str(task["id"]),
            status="completed",
            completion_score=1.0,
            metrics={
                "step_count": len(state.get("steps") or []),
                "artifact_count": len(state.get("artifacts") or []),
                "automatic_after_confirmation": True,
            },
        )
        return True

    def _pipeline_must_continue(self, session_id: str, active_skill: str) -> bool:
        if active_skill != "gis-pipeline":
            return False
        cycle = current_pipeline_cycle(self.session_db, session_id)
        if not cycle or cycle == PIPELINE_STAGES:
            return False
        recent = self.session_db.get_recent_stage_artifacts(session_id, limit=1)
        if recent and recent[-1].get("stage_name") == "structured_query":
            artifact = recent[-1].get("stage_artifact") or {}
            if isinstance(artifact, dict) and artifact.get("questions"):
                return False
        return True

    @staticmethod
    def _requested_tool_call_from_result(
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not result.get("success") or result.get("already_recorded"):
            return None
        request = result.get("requested_tool_call")
        if not isinstance(request, dict):
            return None
        name = str(request.get("name") or "").strip()
        arguments = request.get("arguments")
        if not name or not isinstance(arguments, dict):
            return None
        return {"name": name, "arguments": arguments}

    def _request_tool_confirmation(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        tool_registry: ToolRegistry,
        publish: EventCallback,
        model: str,
        tool_call_id: str | None = None,
        assistant_content: str = "",
    ) -> None:
        entry = tool_registry.get(tool_name)
        confirmation_id = str(uuid.uuid4())
        pending = {
            "confirmation_id": confirmation_id,
            "tool_call_id": tool_call_id or confirmation_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "run_id": run_id,
            "request_scope": self.session_db.get_state(
                f"{session_id}:request_scope"
            ) or run_id,
        }
        self.session_db.set_state(
            self._confirmation_key(session_id, confirmation_id),
            json.dumps(pending, ensure_ascii=False),
        )
        self.session_db.save_message(
            session_id,
            "assistant",
            assistant_content,
            event_type="tool_call",
            finish_reason="tool_calls",
            run_id=run_id,
            tool_calls=[
                {
                    "id": tool_call_id or confirmation_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ],
        )
        active_task = self.session_db.get_active_task(session_id)
        if active_task is not None:
            self.session_db.update_task(
                str(active_task["id"]), status="waiting_confirmation"
            )
        publish(
            agent_event(
                "confirm_request",
                {
                    "confirmation_id": confirmation_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "destructive": entry.destructive,
                    "writes_project": entry.writes_project,
                    "description": entry.description,
                },
                session_id=session_id,
                run_id=run_id,
            )
        )
        content = f"工具 `{tool_name}` 需要确认后才能执行。请确认或取消该操作。"
        self.session_db.save_message(
            session_id,
            "assistant",
            content,
            event_type="confirm_request",
            run_id=run_id,
        )
        publish(
            agent_event(
                "message",
                {"role": "assistant", "content": content, "model": model},
                session_id=session_id,
                run_id=run_id,
            )
        )
        publish(agent_event("complete", {}, session_id=session_id, run_id=run_id))

    def _record_pipeline_execution_result(
        self,
        *,
        session_id: str,
        result: dict[str, Any],
        tool_registry: ToolRegistry,
        publish: EventCallback,
        run_id: str,
    ) -> None:
        if self.session_db.get_state(f"{session_id}:active_skill") != "gis-pipeline":
            return
        if next_pipeline_stage(self.session_db, session_id) != "execution_result":
            return
        summary = "GIS 脚本执行成功。" if result.get("success") else "GIS 脚本执行失败。"
        arguments = {
            "stage_name": "execution_result",
            "summary": summary,
            "artifact": {
                "summary": summary,
                "success": bool(result.get("success")),
                "error": result.get("error"),
                "stdout": result.get("stdout") or "",
                "stderr": result.get("stderr") or "",
                "outputs": result.get("outputs") or [],
                "loaded_layers": result.get("loaded_layers") or [],
            },
        }
        self._publish_stage_start_if_needed(
            "record_pipeline_stage",
            arguments,
            publish,
            session_id,
            run_id,
        )
        stage_result, _ = tool_registry.execute("record_pipeline_stage", arguments)
        self._publish_stage_end_if_needed(
            "record_pipeline_stage",
            stage_result,
            publish,
            session_id,
            run_id,
        )
        if stage_result.get("success"):
            self.session_db.set_state(
                f"{session_id}:active_skill",
                "main-orchestrator",
            )

    def _reset_one_shot_skill(self, session_id: str) -> None:
        active_skill = self.session_db.get_state(f"{session_id}:active_skill") or ""
        document = self.prompt_builder.skill_manager.get(active_skill)
        if document is None or document.lifecycle != "one-shot":
            return
        loaded = read_loaded_skills(
            self.session_db.get_state,
            session_id,
            self.prompt_builder.skill_manager,
        )
        write_loaded_skills(
            self.session_db.set_state,
            session_id,
            self.prompt_builder.skill_manager,
            [name for name in loaded if name != active_skill],
        )

    def _reset_task_scoped_skills(self, session_id: str) -> None:
        """Unload operational instructions after the current task is terminal."""
        task = self.session_db.get_active_task(session_id)
        if task is not None and task.get("status") not in {
            "completed",
            "failed",
            "cancelled",
        }:
            return
        loaded = read_loaded_skills(
            self.session_db.get_state,
            session_id,
            self.prompt_builder.skill_manager,
        )
        retained = []
        for name in loaded:
            document = self.prompt_builder.skill_manager.get(name)
            if document is not None and document.lifecycle == "task":
                continue
            retained.append(name)
        if retained != loaded:
            write_loaded_skills(
                self.session_db.set_state,
                session_id,
                self.prompt_builder.skill_manager,
                retained,
            )

    def _confirmed_tool_replay(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        tool_registry: ToolRegistry | None = None,
    ) -> dict[str, Any] | None:
        if tool_registry is not None:
            fingerprint = tool_registry.idempotency_key(tool_name, arguments)
            if fingerprint:
                scope = self.session_db.get_state(f"{session_id}:request_scope") or ""
                raw_generic = self.session_db.get_state(
                    f"{session_id}:confirmed_tool_calls"
                )
                try:
                    generic_history = json.loads(raw_generic) if raw_generic else {}
                except (json.JSONDecodeError, TypeError):
                    generic_history = {}
                completed = generic_history.get(f"{scope}:{fingerprint}")
                if isinstance(completed, dict):
                    return {
                        **completed,
                        "success": True,
                        "already_completed": True,
                        "duplicate_prevented": True,
                        "message": "相同参数的已确认工具调用已在本次请求中完成，已复用现有结果。",
                    }
        return None

    def _remember_confirmed_tool_call(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        tool_registry: ToolRegistry,
    ) -> None:
        fingerprint = tool_registry.idempotency_key(tool_name, arguments)
        if not fingerprint:
            return
        state_key = f"{session_id}:confirmed_tool_calls"
        raw = self.session_db.get_state(state_key)
        try:
            history = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            history = {}
        if not isinstance(history, dict):
            history = {}
        scope = self.session_db.get_state(f"{session_id}:request_scope") or ""
        history[f"{scope}:{fingerprint}"] = result
        self.session_db.set_state(state_key, json.dumps(history, ensure_ascii=False))

    @staticmethod
    def _duplicate_tool_process_message(tool_name: str) -> str:
        return f"已阻止重复执行工具 {tool_name}，复用现有输出并继续完成剩余计划。"

    def _confirmation_key(self, session_id: str, confirmation_id: str) -> str:
        return f"{session_id}:pending_confirmation:{confirmation_id}"

    def _collect_qgis_context(self) -> QGISContext:
        if self.qgis_executor is not None:
            return self.qgis_executor(lambda: QGISContext.collect(self.iface))
        return QGISContext.collect(self.iface)

    def _build_system_prompt(
        self,
        session_id: str,
        active_skill: str,
        qgis_context: QGISContext,
        *,
        loaded_skills: list[str],
    ) -> str:
        include_skills: list[str] | None = None
        if active_skill == "gis-pipeline":
            include_skills = {
                "data_overview": ["data-overview"],
                "structured_query": ["query-tuner"],
                "solution_plan": ["solution-planner"],
                "generated_code": ["code-generator", "code-reviewer"],
                "execution_result": ["executor"],
            }.get(next_pipeline_stage(self.session_db, session_id), [])
        return self.prompt_builder.build(
            active_skill,
            qgis_context,
            loaded_skills=loaded_skills,
            include_skills=include_skills,
        )

    def _publish_message_deltas(
        self,
        content: str,
        *,
        publish: EventCallback,
        session_id: str,
        run_id: str,
        model: str,
    ) -> None:
        if not content:
            return
        chunk_size = 24
        for index in range(0, len(content), chunk_size):
            delta = content[index : index + chunk_size]
            publish(
                agent_event(
                    "message_delta",
                    {"delta": delta, "model": model},
                    session_id=session_id,
                    run_id=run_id,
                )
            )

    def _chat_with_retries(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        publish: EventCallback | None = None,
        session_id: str = "",
        run_id: str = "",
        on_response: Callable[[ChatResponse], None] | None = None,
    ) -> ChatResponse:
        prepared = self.context_engine.prepare(
            system=system,
            messages=messages,
            tools=tools,
        )
        if session_id:
            self.session_db.set_state(
                f"{session_id}:context_stats",
                json.dumps(prepared.as_dict(), ensure_ascii=False),
            )
        if prepared.compacted:
            messages[:] = prepared.messages
            notice = (
                "已压缩历史上下文并保留当前 GIS 任务状态与工具调用链："
                f"估算输入 {prepared.estimated_input_tokens} tokens，"
                f"预留输出 {prepared.reserved_output_tokens} tokens。"
            )
            if publish is not None and session_id:
                self._save_process_message(session_id, notice)
                publish(
                    agent_event(
                        "thinking",
                        {"message": notice, "context": prepared.as_dict()},
                        session_id=session_id,
                        run_id=run_id,
                    )
                )
        effective_messages = prepared.messages
        last_exc: Exception | None = None
        for attempt in range(1, self.llm_retry_attempts + 1):
            if self.should_cancel():
                raise RuntimeError("任务已停止。")
            try:
                response = self.llm_provider.chat(
                    system=system,
                    messages=effective_messages,
                    tools=tools,
                )
            except Exception as exc:
                if isinstance(exc, ContextWindowExceeded):
                    raise
                last_exc = exc
                if session_id:
                    try:
                        self.session_db.log_failure(
                            session_id,
                            source_type="llm_call",
                            source_name=self.llm_provider.name,
                            error_message=self._short_error(exc),
                            attempt=attempt,
                            context={
                                "model": self.llm_provider.model,
                                "run_id": run_id,
                                "max_attempts": self.llm_retry_attempts,
                            },
                        )
                    except Exception:
                        # Failure telemetry must never replace the original error.
                        pass
                if attempt >= self.llm_retry_attempts:
                    break
                message = (
                    "LLM 提供商调用失败，正在重试"
                    f"（第 {attempt + 1}/{self.llm_retry_attempts} 次）：{self._short_error(exc)}"
                )
                if publish is not None:
                    self._save_process_message(session_id, message)
                    publish(agent_event("thinking", {"message": message}, session_id=session_id, run_id=run_id))
                if self.llm_retry_delay_seconds > 0:
                    time.sleep(self.llm_retry_delay_seconds * attempt)
            else:
                response = self._ensure_response_usage(
                    response,
                    system=system,
                    messages=effective_messages,
                    tools=tools,
                )
                if on_response is not None:
                    on_response(response)
                return response
        reason = self._short_error(last_exc) if last_exc is not None else "未知错误"
        raise RuntimeError(f"LLM 调用失败，已重试 {self.llm_retry_attempts} 次：{reason}") from last_exc

    def _ensure_response_usage(
        self,
        response: ChatResponse,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
    ) -> ChatResponse:
        input_tokens = max(0, int(response.input_tokens))
        output_tokens = max(0, int(response.output_tokens))
        usage_estimated = bool(response.usage_estimated)
        if input_tokens == 0:
            input_payload: dict[str, Any] = {
                "system": system,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
            }
            if tools:
                input_payload["tools"] = tools
            input_tokens = self._estimate_text_tokens(
                json.dumps(input_payload, ensure_ascii=False, separators=(",", ":"))
            )
            usage_estimated = True
        if output_tokens == 0:
            output_payload = {
                "content": response.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in response.tool_calls
                ],
            }
            output_tokens = self._estimate_text_tokens(
                json.dumps(output_payload, ensure_ascii=False, separators=(",", ":"))
            )
            usage_estimated = True
        total_tokens = max(
            0,
            int(response.total_tokens or input_tokens + output_tokens),
        )
        return replace(
            response,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            usage_estimated=usage_estimated,
        )

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        non_ascii = sum(1 for character in text if ord(character) > 127)
        ascii_chars = len(text) - non_ascii
        return max(1, int(ascii_chars / 4 + non_ascii * 1.1))

    def _format_llm_error(self, exc: Exception) -> str:
        if isinstance(exc, ContextWindowExceeded):
            return (
                "当前会话上下文过大，系统无法在保留 GIS 任务状态的同时为完整回答预留足够空间。\n"
                f"错误原因：{self._short_error(exc)}\n"
                "请检查模型 Context Window 配置；完整历史仍保存在会话数据库中。"
            )
        return (
            "Agent 暂时无法从 LLM 提供商获得响应。\n"
            f"错误原因：{self._short_error(exc)}\n"
            "已自动重试多次仍失败，请检查模型服务地址、模型名、网络连接和超时时间后再试。"
        )

    def _publish_final_error(
        self,
        content: str,
        publish: EventCallback,
        session_id: str,
        run_id: str,
    ) -> None:
        self.session_db.save_message(
            session_id,
            "assistant",
            content,
            event_type="error",
            run_id=run_id,
        )
        self._publish_message_deltas(
            content,
            publish=publish,
            session_id=session_id,
            run_id=run_id,
            model=self.llm_provider.model,
        )
        publish(
            agent_event(
                "message",
                {"role": "assistant", "content": content, "model": self.llm_provider.model},
                session_id=session_id,
                run_id=run_id,
            )
        )
        publish(agent_event("error", {"message": content}, session_id=session_id, run_id=run_id))

    def _short_error(self, exc: Exception | None) -> str:
        if exc is None:
            return "未知错误"
        text = str(exc).strip() or exc.__class__.__name__
        return text[-1000:]

    def _format_tool_failure(self, tool_name: str, result: dict[str, Any]) -> str:
        lines = [f"工具 `{tool_name}` 执行失败：{result.get('error') or '未知错误'}"]
        if tool_name in {
            "execute_gis_code",
            "generate_land_cover_map",
            "execute_land_use_building_metrics",
            "execute_school_service_coverage",
        }:
            if result.get("workspace_dir"):
                lines.append(f"工作目录：{result['workspace_dir']}")
            if result.get("stderr"):
                lines.append("stderr：")
                lines.append(str(result["stderr"])[-2000:])
            if result.get("stdout"):
                lines.append("stdout：")
                lines.append(str(result["stdout"])[-1000:])
            lines.append("建议：检查生成代码、输入路径、输出文件名和 expected_outputs 是否一致后重试。")
        return "\n".join(lines)

    def _format_tool_success(self, tool_name: str, result: dict[str, Any]) -> str:
        if tool_name == "execute_cultivated_land_loss_analysis":
            summary = result.get("analysis_summary") or {}
            metrics = summary.get("metrics") or {}
            data_quality = result.get("data_quality") or summary.get("data_quality") or {}
            unit = str(summary.get("unit_label") or summary.get("area_unit") or "")

            def metric(name: str) -> str:
                value = metrics.get(name)
                if isinstance(value, (int, float)):
                    return f"{float(value):,.2f} {unit}".rstrip()
                return "未返回"

            area_name = str(summary.get("management_area_name") or "")
            year = summary.get("analysis_year")
            title = f"{year}年度国土变更调查耕地流失分析已完成。" if year else "国土变更调查耕地流失分析已完成。"
            lines = [title]
            if area_name:
                lines.append(f"管理区：{area_name}")
            if summary.get("increment_count") is not None:
                lines.append(f"增量包原始要素：{summary['increment_count']} 个")
            lines.extend(
                [
                    "",
                    "指标汇总：",
                    f"- 增量包面积：{metric('increment_area')}",
                    f"- 耕地不合理流出：{metric('unreasonable_outflow_area')}，其中占永农 {metric('unreasonable_outflow_permanent_area')}",
                    f"- 非农化：{metric('non_agricultural_area')}，其中占永农 {metric('non_agricultural_permanent_area')}",
                    f"- 非粮化：{metric('non_grain_area')}，其中占永农 {metric('non_grain_permanent_area')}",
                    f"- 流向林地、园地：{metric('forest_garden_area')}，其中占永农 {metric('forest_garden_permanent_area')}",
                    f"- 流向其他农用地：{metric('other_agricultural_area')}，其中占永农 {metric('other_agricultural_permanent_area')}",
                    f"- 新增耕地：{metric('added_cultivated_area')}",
                    f"- 上年度耕地：{metric('previous_cultivated_area')}",
                    f"- 耕地变化：{metric('cultivated_change_area')}",
                    f"- 本年度耕地：{metric('current_cultivated_area')}",
                ]
            )
            warning_count = int(data_quality.get("warning_count") or 0)
            if warning_count:
                lines.extend(["", f"数据质量警告：{warning_count} 条"])
                for message in (data_quality.get("messages") or [])[:5]:
                    lines.append(f"- {message}")
                if data_quality.get("messages_truncated") or warning_count > 5:
                    lines.append("- 其余警告请查看质量报告。")
            outputs = result.get("outputs") or []
            output_lines = []
            for output in outputs:
                if not isinstance(output, dict):
                    continue
                path = output.get("path") or output.get("absolute_path")
                if path:
                    output_lines.append(
                        f"- {output.get('name') or path}：{path}"
                    )
            if output_lines:
                lines.extend(["", "结果文件：", *output_lines])
            return "\n".join(lines)
        if tool_name == "segment_remote_sensing_image":
            parameters = result.get("parameters") or {}
            prompt = parameters.get("prompt") or result.get("prompt")
            object_count = result.get("object_count")
            loaded_layers = result.get("loaded_layers") or []
            outputs = result.get("outputs") or []
            lines = ["SAM3 分割已完成。"]
            if prompt:
                lines.append(f"目标类别：{prompt}")
            if object_count is not None:
                lines.append(f"提取对象数：{object_count}")
            if loaded_layers:
                names = ", ".join(
                    str(layer.get("name") or layer.get("id") or "")
                    for layer in loaded_layers
                    if isinstance(layer, dict)
                )
                if names:
                    lines.append(f"已加载结果图层：{names}")
            if outputs:
                paths = ", ".join(
                    str(output.get("path") or output.get("absolute_path") or "")
                    for output in outputs
                    if isinstance(output, dict)
                    and (output.get("path") or output.get("absolute_path"))
                )
                if paths:
                    lines.append(f"输出文件：{paths}")
            return "\n".join(lines)
        if tool_name not in {
            "execute_gis_code",
            "generate_land_cover_map",
            "execute_land_use_building_metrics",
            "execute_school_service_coverage",
        }:
            return f"已确认并执行工具 `{tool_name}`。"
        stdout = str(result.get("stdout") or "").strip()
        lines = [stdout] if stdout else ["代码执行成功。"]
        loaded_layers = result.get("loaded_layers") or []
        outputs = result.get("outputs") or []
        if loaded_layers:
            names = ", ".join(str(layer.get("name") or "") for layer in loaded_layers)
            lines.append(f"已加载结果图层：{names}")
        elif outputs:
            names = ", ".join(str(output.get("name") or output.get("path") or "") for output in outputs)
            lines.append(f"已生成输出：{names}")
        delivered_outputs = result.get("delivered_outputs") or []
        if delivered_outputs:
            targets = ", ".join(str(output.get("target_path") or "") for output in delivered_outputs)
            lines.append(f"已导出到：{targets}")
        if result.get("retry_count"):
            lines.append(f"已根据上一次错误自动修复并重试 {result['retry_count']} 次。")
        return "\n".join(lines)

    def _retry_failed_code_execution(
        self,
        *,
        session_id: str,
        failed_arguments: dict[str, Any],
        failed_result: dict[str, Any],
        tool_registry: ToolRegistry,
        publish: EventCallback,
        run_id: str,
        on_response: Callable[[ChatResponse], None] | None = None,
    ) -> dict[str, Any] | None:
        messages = self._retry_messages_for_code_failure(session_id, failed_arguments, failed_result)
        active_skill = (
            self.session_db.get_state(f"{session_id}:active_skill")
            or "main-orchestrator"
        )
        loaded_skills = read_loaded_skills(
            self.session_db.get_state,
            session_id,
            self.prompt_builder.skill_manager,
        )
        system_prompt = self._build_system_prompt(
            session_id,
            active_skill,
            self._collect_qgis_context(),
            loaded_skills=loaded_skills,
        )
        last_result: dict[str, Any] | None = failed_result
        seen_execution_signatures = {
            self._code_execution_signature(failed_arguments)
        }
        previous_failure_signature = self._code_failure_signature(failed_result)
        for retry_index in range(1, 3):
            publish(
                agent_event(
                    "thinking",
                    {"message": f"代码执行失败，正在反馈错误并重新生成代码（第 {retry_index} 次）"},
                    session_id=session_id,
                    run_id=run_id,
                )
            )
            self._save_process_message(
                session_id,
                f"代码执行失败，正在反馈错误并重新生成代码（第 {retry_index} 次）",
            )
            response = self._chat_with_retries(
                system=system_prompt,
                messages=messages,
                tools=tool_registry.definitions_for_skills(loaded_skills),
                publish=publish,
                session_id=session_id,
                run_id=run_id,
                on_response=on_response,
            )
            if not response.tool_calls:
                if response.content:
                    messages.append(ChatMessage(role="assistant", content=response.content))
                break
            for call in response.tool_calls:
                if call.name != "execute_gis_code":
                    continue
                signature = self._code_execution_signature(call.arguments)
                if signature in seen_execution_signatures:
                    duplicate_result = {
                        **(last_result or failed_result),
                        "success": False,
                        "error": (
                            f"{(last_result or failed_result).get('error') or '代码执行失败'} "
                            "模型再次提交了相同代码和输出契约，已停止重复执行。"
                        ),
                        "duplicate_failure_prevented": True,
                    }
                    self._save_process_message(
                        session_id,
                        "检测到完全相同的失败代码，已停止自动重试。",
                    )
                    return duplicate_result
                seen_execution_signatures.add(signature)
                publish(
                    agent_event(
                        "tool_start",
                        {"name": call.name, "arguments": call.arguments, "retry": retry_index},
                        session_id=session_id,
                        run_id=run_id,
                    )
                )
                self._save_process_message(session_id, f"调用工具：{call.name}")
                result, duration_ms = tool_registry.execute(call.name, call.arguments)
                result["retry_count"] = retry_index
                self.session_db.log_tool_call(
                    session_id,
                    call.name,
                    call.arguments,
                    result,
                    duration_ms=duration_ms,
                )
                publish(
                    agent_event(
                        "tool_end",
                        {"name": call.name, "result": result, "duration_ms": duration_ms, "retry": retry_index},
                        session_id=session_id,
                        run_id=run_id,
                    )
                )
                self._save_process_message(session_id, self._format_tool_process(call.name, result))
                last_result = result
                if result.get("success", True):
                    return result
                failure_signature = self._code_failure_signature(result)
                if failure_signature == previous_failure_signature:
                    result["error"] = (
                        f"{result.get('error') or '代码执行失败'} "
                        "相同的运行时失败已连续出现两次，已停止自动重试。"
                    )
                    result["duplicate_failure_prevented"] = True
                    return result
                previous_failure_signature = failure_signature
                messages = self._retry_messages_for_code_failure(
                    session_id,
                    call.arguments,
                    result,
                    original_failed_arguments=failed_arguments,
                )
        return last_result

    @staticmethod
    def _code_execution_signature(arguments: dict[str, Any]) -> str:
        return json.dumps(
            {
                key: arguments.get(key)
                for key in (
                    "code",
                    "expected_outputs",
                    "delivery_outputs",
                    "timeout_seconds",
                )
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    @staticmethod
    def _code_failure_signature(result: dict[str, Any]) -> str:
        outputs = []
        for output in result.get("outputs") or []:
            if not isinstance(output, dict) or output.get("verified"):
                continue
            outputs.append(
                {
                    "name": output.get("name"),
                    "type": output.get("type"),
                    "exists": output.get("exists"),
                    "errors": output.get("validation_errors") or [],
                }
            )
        return json.dumps(
            {
                "error_code": result.get("error_code"),
                "error": None if outputs else result.get("error"),
                "outputs": outputs,
                "stderr_tail": str(result.get("stderr") or "").strip().splitlines()[-1:],
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _retry_messages_for_code_failure(
        self,
        session_id: str,
        failed_arguments: dict[str, Any],
        failed_result: dict[str, Any],
        *,
        original_failed_arguments: dict[str, Any] | None = None,
    ) -> list[ChatMessage]:
        history = self.session_db.get_conversation_messages(session_id, limit=20)
        messages = [
            ChatMessage(role=row["role"], content=row["content"] or "")
            for row in history
            if row["role"] in {"user", "assistant"}
        ]
        memory_message = self._build_memory_message(session_id)
        if memory_message is not None:
            messages.insert(0, memory_message)
        original_arguments = original_failed_arguments or failed_arguments
        original_payload = {
            key: original_arguments.get(key)
            for key in (
                "code",
                "expected_outputs",
                "delivery_outputs",
                "timeout_seconds",
            )
            if original_arguments.get(key) is not None
        }
        latest_payload = {
            key: failed_arguments.get(key)
            for key in (
                "code",
                "expected_outputs",
                "delivery_outputs",
                "timeout_seconds",
            )
            if failed_arguments.get(key) is not None
        }
        failure_evidence = {
            "success": failed_result.get("success"),
            "error": failed_result.get("error"),
            "error_code": failed_result.get("error_code"),
            "cause": failed_result.get("cause"),
            "outputs": failed_result.get("outputs") or [],
            "stderr": str(failed_result.get("stderr") or "")[-4000:],
            "stdout": str(failed_result.get("stdout") or "")[-1000:],
            "workspace_dir": failed_result.get("workspace_dir"),
            "preflight_failed": failed_result.get("preflight_failed"),
        }
        messages.append(
            ChatMessage(
                role="assistant",
                content=(
                    "execute_gis_code 执行失败。必须重新生成一份从当前 QGIS 原始图层开始、"
                    "包含原脚本全部步骤的完整自包含代码，再次调用 execute_gis_code。"
                    "每次 execute_gis_code 都会创建全新的空工作目录；上一次失败执行产生的"
                    "临时或中间文件不会被复制到重试目录。严禁写‘中间结果已存在’、只重跑失败步骤、"
                    "读取上次 workspace_dir，或假设任意 QGIS_AGENT_WORKSPACE 文件已存在。"
                    "必须保留原始脚本中失败步骤之前的图层获取、检查、处理和中间结果生成步骤，"
                    "只修正导致失败的代码。不要重复相同错误。"
                    "常见修复：如果使用 QgsProject/QgsVectorLayer 等 PyQGIS 类，"
                    "可以直接使用当前 QGIS 环境中已有符号，或显式 `from qgis.core import ...`；"
                    "输出仍必须写入 QGIS_AGENT_WORKSPACE。"
                    "inspect_layer 返回的 QGIS layer_id 不能直接作为字符串传给 Processing 的 INPUT、"
                    "OVERLAY、JOIN 或 MASK；必须先用 QgsProject.instance().mapLayer(layer_id) "
                    "解析为 QgsMapLayer，检查返回值不是 None，再传入算法。"
                    "如果错误来自空几何或无效几何，应在 Processing context 中使用 "
                    "`Qgis.InvalidGeometryCheck.GeometrySkipInvalid` 排除这些要素；"
                    "InvalidGeometryCheck 枚举不属于 QgsProcessingContext。"
                    "除非用户明确要求修复源数据，禁止调用 "
                    "native:fixgeometries，也不得生成完整图层的修复副本。"
                    "用户要求生成或导出文件时，必须在下一次 execute_gis_code 调用中补上最终输出文件，"
                    "例如 {\"path\":\"500m.shp\",\"name\":\"500m\",\"type\":\"vector\"}；"
                    "若 stdout 本身就是最终统计结论或任务只调整当前图层样式，则允许 expected_outputs 为空。\n"
                    "QgsSingleBandPseudoColorRenderer 的第三个构造参数和 setShader() 都要求 "
                    "QgsRasterShader，不能传 QgsColorRampShader。必须先调用 "
                    "raster_shader.setRasterShaderFunction(color_ramp_shader)，再把 raster_shader 传给 renderer。\n"
                    "native:fieldcalculator 中 FIELD_TYPE=2 是 Text/String，不是 Double；"
                    "密度、比例或除法结果应使用算法证据中的 Decimal/Double 类型，不能仅增大 FIELD_LENGTH。\n"
                    f"原始完整失败调用：{json.dumps(original_payload, ensure_ascii=False)}\n"
                    f"最近失败调用：{json.dumps(latest_payload, ensure_ascii=False)}\n"
                    f"失败证据：{json.dumps(failure_evidence, ensure_ascii=False)}"
                ),
            )
        )
        return messages

    def _build_conversation_messages(self, session_id: str) -> list[ChatMessage]:
        history = self.session_db.get_context_messages(
            session_id,
            historical_limit=int(
                self.context_config.get("historical_message_limit", 8)
            ),
        )
        messages = [self._chat_message_from_row(row) for row in history]
        memory_message = self._build_memory_message(session_id)
        if memory_message is not None:
            messages.insert(0, memory_message)
        task_state_message = self._build_task_state_message(session_id)
        if task_state_message is not None:
            messages.insert(0, task_state_message)
        return messages

    def _build_task_state_message(self, session_id: str) -> ChatMessage | None:
        task = self.session_db.get_active_task(session_id)
        if task is None:
            return None
        task_state = self.session_db.get_task_state(str(task["id"]))
        if task_state is None:
            return None
        snapshot = {
            "task_id": task_state.get("id"),
            "objective": task_state.get("objective"),
            "status": task_state.get("status"),
            "plan_version": task_state.get("plan_version"),
            "steps": [
                {
                    "id": step.get("id"),
                    "skill_name": step.get("skill_name"),
                    "instruction": step.get("instruction"),
                    "dependencies": step.get("dependencies") or [],
                    "status": step.get("status"),
                    "inputs": step.get("inputs") or {},
                    "outputs": step.get("outputs") or {},
                    "error": step.get("error"),
                }
                for step in task_state.get("steps") or []
            ],
            "artifacts": [
                {
                    "id": artifact.get("id"),
                    "type": artifact.get("artifact_type"),
                    "name": artifact.get("name"),
                    "uri": artifact.get("uri"),
                    "verified": artifact.get("verified"),
                    "payload": artifact.get("payload") or {},
                }
                for artifact in task_state.get("artifacts") or []
            ],
        }
        content = (
            "[GIS TASK STATE — AUTHORITATIVE]\n"
            "以下 JSON 是当前任务的无损状态；历史摘要只能作为参考，不能覆盖其中的图层、字段、参数、步骤或产物。\n"
            + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        )
        return ChatMessage(role="assistant", content=content)

    @staticmethod
    def _tool_calls_payload(calls: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": str(call.id),
                "type": "function",
                "function": {
                    "name": str(call.name),
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in calls
        ]

    @classmethod
    def _assistant_tool_message(cls, response: ChatResponse) -> ChatMessage:
        return ChatMessage(
            role="assistant",
            content=response.content or "",
            tool_calls=cls._tool_calls_payload(response.tool_calls),
        )

    @staticmethod
    def _tool_result_message(item: dict[str, Any]) -> ChatMessage:
        return ChatMessage(
            role="tool",
            content=json.dumps(item.get("result") or {}, ensure_ascii=False),
            tool_call_id=str(item.get("call_id") or ""),
            name=str(item.get("name") or "") or None,
        )

    def _persist_tool_exchange(
        self,
        session_id: str,
        response: ChatResponse,
        tool_results: list[dict[str, Any]],
        *,
        run_id: str,
    ) -> None:
        """Persist a complete provider-native tool exchange for recovery."""
        if not tool_results:
            return
        completed_ids = {str(item.get("call_id") or "") for item in tool_results}
        completed_calls = [
            call for call in response.tool_calls if str(call.id) in completed_ids
        ]
        if not completed_calls:
            return
        self.session_db.save_message(
            session_id,
            "assistant",
            response.content or "",
            event_type="tool_call",
            finish_reason=response.finish_reason,
            run_id=run_id,
            tool_calls=self._tool_calls_payload(completed_calls),
        )
        for item in tool_results:
            call_id = str(item.get("call_id") or "")
            if call_id not in completed_ids:
                continue
            self.session_db.save_message(
                session_id,
                "tool",
                json.dumps(item.get("result") or {}, ensure_ascii=False),
                event_type="tool_result",
                run_id=run_id,
                tool_call_id=call_id,
                tool_name=str(item.get("name") or "") or None,
            )

    @staticmethod
    def _chat_message_from_row(row: dict[str, Any]) -> ChatMessage:
        return ChatMessage(
            role=str(row.get("role") or "assistant"),
            content=str(row.get("content") or ""),
            tool_calls=list(row.get("tool_calls") or []),
            tool_call_id=str(row.get("tool_call_id") or "") or None,
            name=str(row.get("tool_name") or "") or None,
        )

    def _pipeline_context_should_compact(self, tool_results: list[dict[str, Any]]) -> bool:
        for call in tool_results:
            if call.get("name") != "record_pipeline_stage":
                continue
            raw_result = call.get("result")
            result: dict[str, Any] = dict(raw_result) if isinstance(raw_result, dict) else {}
            if result.get("success") or result.get("error_code") == "truncated_tool_arguments":
                return True
        return False

    def _compact_pipeline_tool_results(
        self,
        tool_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        compact = []
        for call in tool_results:
            raw_result = call.get("result")
            result: dict[str, Any] = dict(raw_result) if isinstance(raw_result, dict) else {}
            if call.get("name") == "record_pipeline_stage":
                compact.append(
                    {
                        "call_id": call.get("call_id"),
                        "name": "record_pipeline_stage",
                        "result": {
                            key: result.get(key)
                            for key in (
                                "success",
                                "stage_name",
                                "summary",
                                "expected_stage",
                                "received_stage",
                                "error_code",
                                "retryable",
                                "error",
                            )
                            if result.get(key) is not None
                        },
                    }
                )
            else:
                compact.append(call)
        return compact

    def _build_pipeline_stage_messages(self, session_id: str) -> list[ChatMessage]:
        """Pass only the minimum complete JSON state to the next Pipeline stage."""
        cycle = current_pipeline_cycle(self.session_db, session_id)
        recent = self.session_db.get_recent_stage_artifacts(session_id, limit=5)
        latest_by_stage = {
            str(item.get("stage_name") or ""): item
            for item in recent
            if str(item.get("stage_name") or "") in cycle
        }
        next_stage = next_pipeline_stage(self.session_db, session_id)
        envelope: dict[str, Any] = {
            "instruction": (
                "只根据本 JSON 执行 next_pipeline_stage。不要引用更早的对话、工具结果或阶段；"
                "输出必须作为完整 JSON object 传给 record_pipeline_stage。"
            ),
            "next_pipeline_stage": next_stage,
        }

        structured_item = latest_by_stage.get("structured_query")
        if structured_item is not None and next_stage in {
            "solution_plan",
            "generated_code",
            "execution_result",
        }:
            envelope["structured_query"] = structured_item.get("stage_artifact") or {}

        next_index = PIPELINE_STAGES.index(next_stage)
        previous_stage = PIPELINE_STAGES[next_index - 1] if next_index > 0 else ""
        previous_item = latest_by_stage.get(previous_stage)
        if previous_item is not None and previous_stage != "structured_query":
            envelope["previous_stage"] = {
                "stage_name": previous_stage,
                "artifact": previous_item.get("stage_artifact") or {},
            }

        if next_stage == "generated_code":
            solution_artifact = (
                latest_by_stage.get("solution_plan") or {}
            ).get("stage_artifact") or {}
            selected_ids = processing_algorithm_ids(solution_artifact)
            evidence = compact_processing_evidence(
                read_processing_evidence(self.session_db, session_id),
                selected_ids,
            )
            if evidence:
                envelope["processing_algorithm_evidence"] = evidence
                envelope["processing_evidence_instruction"] = (
                    "这是 get_qgis_processing_tool 在本轮实际返回并由服务端持久化的权威证据。"
                    "每个 processing.run 只能使用这里列出的算法 ID 和参数名；"
                    "代码形状以 code_example 为准，不得凭记忆改名或增加辅助算法。"
                )

        if next_stage in {"data_overview", "structured_query"}:
            history = self.session_db.get_conversation_messages(session_id, limit=12)
            latest_user_request = next(
                (
                    str(row.get("content") or "")
                    for row in reversed(history)
                    if row.get("role") == "user"
                ),
                "",
            )
            envelope["original_user_request"] = latest_user_request

        return [
            ChatMessage(
                role="user",
                content=json.dumps(
                    envelope,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        ]

    def _build_memory_message(self, session_id: str) -> ChatMessage | None:
        tool_calls = self.session_db.get_recent_tool_calls(session_id, limit=8)
        stage_artifacts = self.session_db.get_recent_stage_artifacts(session_id, limit=5)
        lines = []
        if tool_calls:
            lines.append("最近工具记忆：")
            for call in tool_calls:
                lines.extend(self._summarize_tool_memory(call))
        if stage_artifacts:
            lines.append("最近 Pipeline 记忆：")
            for artifact in stage_artifacts:
                summary = str(artifact.get("content") or "").strip()
                stage_name = str(artifact.get("stage_name") or "")
                if summary:
                    lines.append(f"- {stage_name}: {summary}")
        if not lines:
            return None
        content = (
            "会话记忆：以下内容来自本会话历史工具结果和阶段产物。"
            "当用户使用省略说法、代词或短句继续任务时，必须优先用这些记忆补全上下文；"
            "不要重复询问已经由工具确认过的图层、字段或筛选依据。\n"
            + "\n".join(lines)
        )
        return ChatMessage(role="assistant", content=content[:3600])

    def _summarize_tool_memory(self, call: dict[str, Any]) -> list[str]:
        name = str(call.get("tool_name") or "")
        raw_result = call.get("result")
        result: dict[str, Any] = dict(raw_result) if isinstance(raw_result, dict) else {}
        raw_arguments = call.get("arguments")
        arguments: dict[str, Any] = (
            dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
        )
        if name in {"inspect_layer", "inspect_layers"}:
            raw_layers = result.get("layers")
            layers: list[Any] = list(raw_layers) if isinstance(raw_layers, list) else [result]
            lines = []
            for item in layers[:6]:
                if not isinstance(item, dict):
                    continue
                raw_layer = item.get("layer")
                layer: dict[str, Any] = dict(raw_layer) if isinstance(raw_layer, dict) else {}
                raw_fields = item.get("fields")
                fields: list[Any] = list(raw_fields) if isinstance(raw_fields, list) else []
                field_names = [str(field.get("name")) for field in fields[:30] if isinstance(field, dict)]
                raw_samples = item.get("sample_features")
                samples: list[Any] = list(raw_samples) if isinstance(raw_samples, list) else []
                sample_hint = ""
                if samples:
                    sample_hint = f"，样例={json.dumps(samples[:2], ensure_ascii=False)[:500]}"
                lines.append(
                    f"- inspect: 图层={layer.get('name') or arguments.get('layer_name')}, "
                    f"ID={layer.get('id') or arguments.get('layer_id') or ''}, "
                    f"类型={layer.get('type')}, CRS={layer.get('crs')}, 字段={field_names}{sample_hint}"
                )
            return lines
        if name == "inspect_school_service_coverage_inputs":
            raw_school_layer = result.get("school_layer")
            school_layer: dict[str, Any] = (
                dict(raw_school_layer) if isinstance(raw_school_layer, dict) else {}
            )
            raw_available_values = result.get("available_values")
            available_values: list[Any] = (
                list(raw_available_values) if isinstance(raw_available_values, list) else []
            )
            return [
                "- inspect_school_service_coverage_inputs: "
                f"学校图层={school_layer.get('name') or ''}, "
                f"school_layer_id={school_layer.get('id') or arguments.get('school_layer_id') or ''}, "
                f"school_type_field={result.get('school_type_field') or arguments.get('school_type_field') or ''}, "
                f"available_values={json.dumps(available_values, ensure_ascii=False)}, "
                f"domain_complete={result.get('domain_complete')}"
            ]
        if name == "execute_school_service_coverage":
            status = "成功" if call.get("success") else f"失败：{call.get('error_message') or ''}"
            return [
                "- execute_school_service_coverage: "
                f"{status}；已绑定参数={json.dumps(arguments, ensure_ascii=False)}"
            ]
        if name in {"list_layers", "load_layer", "export_layer", "execute_gis_code"}:
            compact = json.dumps(result, ensure_ascii=False)
            return [f"- {name}: {compact[:800]}"]
        if name:
            status = "成功" if call.get("success") else f"失败：{call.get('error_message') or ''}"
            return [f"- {name}: {status}"]
        return []

    def _publish_stage_start_if_needed(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        publish: EventCallback,
        session_id: str,
        run_id: str,
    ) -> None:
        if tool_name != "record_pipeline_stage":
            return
        stage_name = str(arguments.get("stage_name") or "").strip()
        if not stage_name:
            stage_name = next_pipeline_stage(self.session_db, session_id)
        self._save_process_message(session_id, f"Pipeline 阶段开始：{stage_name}")
        publish(
            agent_event(
                "stage_start",
                {"stage_name": stage_name},
                session_id=session_id,
                run_id=run_id,
            )
        )

    def _publish_stage_end_if_needed(
        self,
        tool_name: str,
        result: dict[str, Any],
        publish: EventCallback,
        session_id: str,
        run_id: str,
    ) -> None:
        if tool_name != "record_pipeline_stage":
            return
        payload = {
            "stage_name": (
                result.get("stage_name")
                or result.get("received_stage")
                or result.get("expected_stage")
            ),
            "summary": result.get("summary"),
            "artifact": result.get("artifact") or {},
            "success": result.get("success", True),
            "error": result.get("error"),
        }
        publish(agent_event("stage_end", payload, session_id=session_id, run_id=run_id))
        state = "失败" if result.get("success") is False else "完成"
        summary = str(result.get("summary") or "")
        stage_name = (
            result.get("stage_name")
            or result.get("received_stage")
            or result.get("expected_stage")
            or ""
        )
        process_message = f"Pipeline 阶段{state}：{stage_name}"
        if summary:
            process_message = f"{process_message}\n{summary}"
        if result.get("error"):
            process_message = f"{process_message}\nerror：{result['error']}"
        self._save_process_message(session_id, process_message)
        if (
            result.get("stage_name") == "generated_code"
            and not result.get("already_recorded")
            and isinstance(result.get("artifact"), dict)
        ):
            artifact = result["artifact"]
            if artifact.get("code"):
                publish(
                    agent_event(
                        "code_generated",
                        {
                            "code": artifact.get("code"),
                            "expected_outputs": artifact.get("expected_outputs") or [],
                            "review": artifact.get("review") or artifact.get("safety_review"),
                        },
                        session_id=session_id,
                        run_id=run_id,
                    )
                )
                self._save_process_message(session_id, "已生成待执行代码，等待确认或继续执行。")

    def _save_process_message(self, session_id: str, content: str) -> None:
        if not content:
            return
        self.session_db.save_message(session_id, "system", content, event_type="process")

    def _format_tool_process(self, tool_name: str, result: dict[str, Any]) -> str:
        prefix = "工具失败" if result.get("success") is False else "工具完成"
        lines = [f"{prefix}：{tool_name}"]
        if tool_name == "execute_gis_code":
            if result.get("workspace_dir"):
                lines.append(f"工作目录：{result['workspace_dir']}")
            if result.get("error"):
                lines.append(f"error：{result['error']}")
            if result.get("stderr"):
                lines.append("stderr：")
                lines.append(str(result["stderr"])[-1000:])
            if result.get("stdout"):
                lines.append("stdout：")
                lines.append(str(result["stdout"])[-500:])
        return "\n".join(lines)
