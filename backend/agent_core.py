"""Minimal AgentCore for Phase 1 conversations."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from ..database.session_db import SessionDB
from ..qwebengine.message_protocol import agent_event
from .context.prompt_builder import PromptBuilder
from .context.qgis_context import QGISContext
from .iteration_budget import IterationBudget
from .llm.base_provider import ChatMessage, LLMProvider
from .tools.code_execution import build_execute_gis_code_tool
from .tools.gis_analysis import build_get_task_context_tool
from .tools.layer_ops import build_layer_tools
from .tools.pipeline import build_record_pipeline_stage_tool
from .tools.registry import ToolRegistry
from .tools.search_tools import build_search_messages_tool
from .tools.skill_management import build_set_active_skill_tool

EventCallback = Callable[[dict[str, Any]], None]
CancelChecker = Callable[[], bool]


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
        should_cancel: CancelChecker | None = None,
    ):
        self.session_db = session_db
        self.llm_provider = llm_provider
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.iface = iface
        self.qgis_executor = qgis_executor
        self.executor_config = executor_config or {}
        self.should_cancel = should_cancel or (lambda: False)

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

        def publish(event: dict[str, Any]) -> None:
            events.append(event)
            if emit is not None:
                emit(event)

        def check_cancelled() -> bool:
            if not self.should_cancel():
                return False
            content = "任务已停止。"
            self.session_db.save_message(session_id, "assistant", content, event_type="summary")
            publish(agent_event("message", {"role": "assistant", "content": content, "model": self.llm_provider.model}, session_id=session_id, run_id=run_id))
            publish(agent_event("complete", {"cancelled": True}, session_id=session_id, run_id=run_id))
            return True

        publish(agent_event("run_start", {"provider": self.llm_provider.name}, session_id=session_id, run_id=run_id))
        if check_cancelled():
            return events
        self.session_db.save_message(session_id, "user", user_message, event_type="user")

        if qgis_context is None:
            qgis_context = self._collect_qgis_context()
        active_skill = self.session_db.get_state(f"{session_id}:active_skill") or "main-orchestrator"
        system_prompt = self.prompt_builder.build(
            active_skill,
            qgis_context,
        )
        thinking_message = "正在组织上下文"
        self._save_process_message(session_id, thinking_message)
        publish(agent_event("thinking", {"message": thinking_message}, session_id=session_id, run_id=run_id))

        messages = self._build_conversation_messages(session_id)

        tool_registry = self._build_tool_registry(session_id)
        try:
            budget = IterationBudget(max_iterations=16, max_tool_calls=40)
            response = None
            while not budget.exhausted:
                if check_cancelled():
                    return events
                response = self.llm_provider.chat(
                    system=system_prompt,
                    messages=messages,
                    tools=tool_registry.definitions_for_skill(active_skill),
                )
                budget.record_iteration()
                if not response.tool_calls:
                    break

                tool_results = []
                for call in response.tool_calls:
                    if check_cancelled():
                        return events
                    entry = tool_registry.get(call.name)
                    if entry.requires_confirmation:
                        confirmation_id = str(uuid.uuid4())
                        pending = {
                            "confirmation_id": confirmation_id,
                            "tool_name": call.name,
                            "arguments": call.arguments,
                            "run_id": run_id,
                        }
                        self.session_db.set_state(
                            self._confirmation_key(session_id, confirmation_id),
                            json.dumps(pending, ensure_ascii=False),
                        )
                        publish(
                            agent_event(
                                "confirm_request",
                                {
                                    "confirmation_id": confirmation_id,
                                    "tool_name": call.name,
                                    "arguments": call.arguments,
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
                            {"name": call.name, "arguments": call.arguments},
                            session_id=session_id,
                            run_id=run_id,
                        )
                    )
                    self._save_process_message(session_id, f"调用工具：{call.name}")
                    self._publish_stage_start_if_needed(call.name, call.arguments, publish, session_id, run_id)
                    result, duration_ms = tool_registry.execute(call.name, call.arguments)
                    if call.name == "set_active_skill" and result.get("success"):
                        active_skill = str(result.get("active_skill") or active_skill)
                        system_prompt = self.prompt_builder.build(active_skill, qgis_context)
                    self.session_db.log_tool_call(
                        session_id,
                        call.name,
                        call.arguments,
                        result,
                        duration_ms=duration_ms,
                    )
                    tool_results.append({"name": call.name, "arguments": call.arguments, "result": result})
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
                    budget.record_tool_call()
                    if check_cancelled():
                        return events
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=(
                            "以下是刚刚执行的 QGIS 工具结果。"
                            "如果任务尚未完成，继续调用必要工具完成任务；"
                            "只有确认已经完成或需要用户补充信息时，才给用户最终答复：\n"
                            "注意：如果某个工具 result.success 为 false，必须说明该工具执行失败及 error，"
                            "不要把失败结果解释为查询结果为空或操作成功。\n"
                            f"{json.dumps(tool_results, ensure_ascii=False)}"
                        ),
                    )
                )

            if response is None:
                response = self.llm_provider.chat(system=system_prompt, messages=messages)
            elif response.tool_calls and budget.exhausted:
                response = type(response)(
                    content="任务未完成：已达到本轮工具调用预算。请缩小任务范围或补充更明确的图层、字段和输出要求后重试。",
                    model=response.model,
                    finish_reason="tool_budget_exhausted",
                    tool_calls=[],
                )

            self.session_db.save_message(
                session_id,
                "assistant",
                response.content,
                event_type="summary",
                finish_reason=response.finish_reason,
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
        except Exception as exc:
            content = f"对话生成失败：{exc}"
            self.session_db.save_message(session_id, "assistant", content, event_type="error")
            publish(agent_event("error", {"message": content}, session_id=session_id, run_id=run_id))

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

        def publish(event: dict[str, Any]) -> None:
            events.append(event)
            if emit is not None:
                emit(event)

        key = self._confirmation_key(session_id, confirmation_id)
        raw = self.session_db.get_state(key)
        if not raw:
            raise ValueError(f"确认请求不存在或已处理：{confirmation_id}")
        pending = json.loads(raw)
        self.session_db.delete_state(key)

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

        if not approved:
            content = f"已取消工具 `{tool_name}`。"
            self.session_db.save_message(session_id, "assistant", content, event_type="summary")
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
            retry_result = self._retry_failed_code_execution(
                session_id=session_id,
                failed_arguments=arguments,
                failed_result=result,
                tool_registry=tool_registry,
                publish=publish,
                run_id=run_id,
            )
            if retry_result is not None:
                result = retry_result

        if result.get("success", True):
            content = self._format_tool_success(tool_name, result)
        else:
            content = self._format_tool_failure(tool_name, result)
        self.session_db.save_message(session_id, "assistant", content, event_type="summary")
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
        publish(agent_event("complete", {}, session_id=session_id, run_id=run_id))
        return events

    def _build_tool_registry(self, session_id: str) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(build_set_active_skill_tool(self.session_db.set_state, session_id))
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
        for entry in build_layer_tools(
            session_db=self.session_db,
            session_id=session_id,
            iface=self.iface,
            qgis_executor=self.qgis_executor,
        ):
            registry.register(entry)
        return registry

    def _confirmation_key(self, session_id: str, confirmation_id: str) -> str:
        return f"{session_id}:pending_confirmation:{confirmation_id}"

    def _collect_qgis_context(self) -> QGISContext:
        if self.qgis_executor is not None:
            return self.qgis_executor(lambda: QGISContext.collect(self.iface))
        return QGISContext.collect(self.iface)

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

    def _format_tool_failure(self, tool_name: str, result: dict[str, Any]) -> str:
        lines = [f"工具 `{tool_name}` 执行失败：{result.get('error') or '未知错误'}"]
        if tool_name == "execute_gis_code":
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
        if tool_name != "execute_gis_code":
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
    ) -> dict[str, Any] | None:
        messages = self._retry_messages_for_code_failure(session_id, failed_arguments, failed_result)
        system_prompt = self.prompt_builder.build(
            self.session_db.get_state(f"{session_id}:active_skill") or "main-orchestrator",
            self._collect_qgis_context(),
        )
        last_result: dict[str, Any] | None = None
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
            response = self.llm_provider.chat(
                system=system_prompt,
                messages=messages,
                tools=tool_registry.definitions_for_skill("gis-pipeline"),
            )
            if not response.tool_calls:
                if response.content:
                    messages.append(ChatMessage(role="assistant", content=response.content))
                break
            for call in response.tool_calls:
                if call.name != "execute_gis_code":
                    continue
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
                messages = self._retry_messages_for_code_failure(session_id, call.arguments, result)
        return last_result

    def _retry_messages_for_code_failure(
        self,
        session_id: str,
        failed_arguments: dict[str, Any],
        failed_result: dict[str, Any],
    ) -> list[ChatMessage]:
        history = self.session_db.get_messages(session_id, limit=20)
        messages = [
            ChatMessage(role=row["role"], content=row["content"] or "")
            for row in history
            if row["role"] in {"user", "assistant"}
        ]
        memory_message = self._build_memory_message(session_id)
        if memory_message is not None:
            messages.insert(0, memory_message)
        messages.append(
            ChatMessage(
                role="assistant",
                content=(
                    "execute_gis_code 执行失败，请根据错误重新生成修复后的代码并再次调用 execute_gis_code。"
                    "不要重复相同错误。常见修复：如果使用 QgsProject/QgsVectorLayer 等 PyQGIS 类，"
                    "可以直接使用当前 QGIS 环境中已有符号，或显式 `from qgis.core import ...`；"
                    "输出仍必须写入 QGIS_AGENT_WORKSPACE。"
                    "如果错误提示必须提供 expected_outputs，必须在下一次 execute_gis_code 调用中"
                    "补上最终输出文件，例如 {\"path\":\"500m.shp\",\"name\":\"500m\",\"type\":\"vector\"}。\n"
                    f"失败参数：{json.dumps(failed_arguments, ensure_ascii=False)}\n"
                    f"失败结果：{json.dumps(failed_result, ensure_ascii=False)}"
                ),
            )
        )
        return messages

    def _build_conversation_messages(self, session_id: str) -> list[ChatMessage]:
        history = self.session_db.get_messages(session_id, limit=50)
        messages = [
            ChatMessage(role=row["role"], content=row["content"] or "")
            for row in history
            if row["role"] in {"user", "assistant"}
        ]
        memory_message = self._build_memory_message(session_id)
        if memory_message is not None:
            messages.insert(0, memory_message)
        return messages

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
        return ChatMessage(role="assistant", content=content[:6000])

    def _summarize_tool_memory(self, call: dict[str, Any]) -> list[str]:
        name = str(call.get("tool_name") or "")
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        if name in {"inspect_layer", "inspect_layers"}:
            layers = result.get("layers") if isinstance(result.get("layers"), list) else [result]
            lines = []
            for item in layers[:6]:
                if not isinstance(item, dict):
                    continue
                layer = item.get("layer") if isinstance(item.get("layer"), dict) else {}
                fields = item.get("fields") if isinstance(item.get("fields"), list) else []
                field_names = [str(field.get("name")) for field in fields[:30] if isinstance(field, dict)]
                samples = item.get("sample_features") if isinstance(item.get("sample_features"), list) else []
                sample_hint = ""
                if samples:
                    sample_hint = f"，样例={json.dumps(samples[:2], ensure_ascii=False)[:500]}"
                lines.append(
                    f"- inspect: 图层={layer.get('name') or arguments.get('layer_name')}, "
                    f"类型={layer.get('type')}, CRS={layer.get('crs')}, 字段={field_names}{sample_hint}"
                )
            return lines
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
        stage_name = str(arguments.get("stage_name") or "")
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
            "stage_name": result.get("stage_name"),
            "summary": result.get("summary"),
            "artifact": result.get("artifact") or {},
            "success": result.get("success", True),
            "error": result.get("error"),
        }
        publish(agent_event("stage_end", payload, session_id=session_id, run_id=run_id))
        state = "失败" if result.get("success") is False else "完成"
        summary = str(result.get("summary") or "")
        process_message = f"Pipeline 阶段{state}：{result.get('stage_name') or ''}"
        if summary:
            process_message = f"{process_message}\n{summary}"
        self._save_process_message(session_id, process_message)
        if result.get("stage_name") == "generated_code" and isinstance(result.get("artifact"), dict):
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
