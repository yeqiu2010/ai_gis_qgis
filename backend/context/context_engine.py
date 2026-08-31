"""Token-aware context assembly and deterministic GIS-safe compaction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from ..llm.base_provider import ChatMessage
from ..llm.errors import ContextWindowExceeded

ToolContextReducer = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class PreparedContext:
    messages: list[ChatMessage]
    estimated_input_tokens: int
    system_tokens: int
    message_tokens: int
    tool_schema_tokens: int
    input_limit_tokens: int
    reserved_output_tokens: int
    compacted: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "estimated_input_tokens": self.estimated_input_tokens,
            "system_tokens": self.system_tokens,
            "message_tokens": self.message_tokens,
            "tool_schema_tokens": self.tool_schema_tokens,
            "input_limit_tokens": self.input_limit_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "compacted": self.compacted,
            "reason": self.reason,
        }


class ContextEngine(Protocol):
    def set_tool_reducers(self, reducers: dict[str, ToolContextReducer]) -> None: ...

    def prepare(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
    ) -> PreparedContext: ...


class QGISContextEngine:
    """Keep the active tool chain valid while bounding verbose GIS history."""

    _LARGE_ARGUMENT_FIELDS = {
        "code",
        "_raw_arguments",
        "binary_data",
        "data",
        "payload",
        "raw_payload",
    }
    _RESULT_KEYS = (
        "success",
        "status",
        "error",
        "error_code",
        "summary",
        "message",
        "job_id",
        "workspace_dir",
        "count",
        "feature_count",
        "object_count",
        "confidence_threshold",
        "parameters",
        "outputs",
        "output_files",
        "output_layers",
        "loaded_layers",
        "artifacts",
        "stdout",
        "stderr",
        "retry_count",
        "duplicate_prevented",
    )

    def __init__(
        self,
        *,
        context_window_tokens: int = 0,
        max_output_tokens: int = 4096,
        minimum_output_tokens: int = 2048,
        safety_tokens: int = 256,
        soft_threshold_ratio: float = 0.55,
        max_inline_tool_result_chars: int = 2400,
    ):
        self.context_window_tokens = max(0, int(context_window_tokens))
        self.max_output_tokens = max(1, int(max_output_tokens))
        self.minimum_output_tokens = max(1, int(minimum_output_tokens))
        proportional_safety = int(self.context_window_tokens * 0.01)
        self.safety_tokens = min(
            4096,
            max(256, int(safety_tokens), proportional_safety),
        )
        self.soft_threshold_ratio = min(0.85, max(0.25, float(soft_threshold_ratio)))
        self.max_inline_tool_result_chars = max(400, int(max_inline_tool_result_chars))
        self._tool_reducers: dict[str, ToolContextReducer] = {}

    def set_tool_reducers(self, reducers: dict[str, ToolContextReducer]) -> None:
        self._tool_reducers = dict(reducers)

    def prepare(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
    ) -> PreparedContext:
        tools = tools or []
        system_tokens = estimate_text_tokens(system)
        tool_tokens = estimate_json_tokens(tools)
        message_tokens = estimate_messages_tokens(messages)
        estimated = system_tokens + tool_tokens + message_tokens
        if self.context_window_tokens <= 0:
            return PreparedContext(
                messages=list(messages),
                estimated_input_tokens=estimated,
                system_tokens=system_tokens,
                message_tokens=message_tokens,
                tool_schema_tokens=tool_tokens,
                input_limit_tokens=0,
                reserved_output_tokens=self.minimum_output_tokens,
            )

        reserved_output = min(
            self.max_output_tokens,
            max(self.minimum_output_tokens, int(self.context_window_tokens * 0.10)),
        )
        input_limit = self.context_window_tokens - reserved_output - self.safety_tokens
        fixed_tokens = system_tokens + tool_tokens
        if fixed_tokens >= input_limit:
            raise ContextWindowExceeded(
                "系统提示词和工具定义已占满模型上下文，无法为消息和完整回答预留空间。"
                f"固定输入约 {fixed_tokens} tokens，上限 {input_limit} tokens。"
            )

        soft_limit = min(
            input_limit,
            max(1, int(self.context_window_tokens * self.soft_threshold_ratio)),
        )
        if estimated <= soft_limit:
            return PreparedContext(
                messages=list(messages),
                estimated_input_tokens=estimated,
                system_tokens=system_tokens,
                message_tokens=message_tokens,
                tool_schema_tokens=tool_tokens,
                input_limit_tokens=input_limit,
                reserved_output_tokens=reserved_output,
            )

        compacted = self._compact_completed_tool_exchanges(messages)
        compacted = self._bound_memory_message(compacted)
        compacted_tokens = estimate_messages_tokens(compacted)
        if fixed_tokens + compacted_tokens > input_limit:
            compacted = self._compact_middle_messages(
                compacted,
                message_budget=max(512, input_limit - fixed_tokens),
            )
            compacted_tokens = estimate_messages_tokens(compacted)

        final_estimate = fixed_tokens + compacted_tokens
        if final_estimate > input_limit:
            raise ContextWindowExceeded(
                "上下文压缩后仍无法为完整回答预留安全输出空间。"
                f"估算输入 {final_estimate} tokens，安全输入上限 {input_limit} tokens。"
            )
        return PreparedContext(
            messages=compacted,
            estimated_input_tokens=final_estimate,
            system_tokens=system_tokens,
            message_tokens=compacted_tokens,
            tool_schema_tokens=tool_tokens,
            input_limit_tokens=input_limit,
            reserved_output_tokens=reserved_output,
            compacted=compacted != messages,
            reason="context_pressure",
        )

    def _compact_completed_tool_exchanges(
        self,
        messages: list[ChatMessage],
    ) -> list[ChatMessage]:
        call_context: dict[str, tuple[str, dict[str, Any]]] = {}
        completed_ids = {
            str(message.tool_call_id)
            for message in messages
            if message.role == "tool" and message.tool_call_id
        }
        for message in messages:
            for call in message.tool_calls:
                call_id, name, arguments = self._parse_tool_call(call)
                if call_id:
                    call_context[call_id] = (name, arguments)

        compacted: list[ChatMessage] = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                compacted_calls = [
                    self._externalize_completed_arguments(call, completed_ids)
                    for call in message.tool_calls
                ]
                compacted.append(replace(message, tool_calls=compacted_calls))
                continue
            if message.role != "tool" or not message.tool_call_id:
                compacted.append(message)
                continue
            call_id = str(message.tool_call_id)
            name, arguments = call_context.get(call_id, (str(message.name or ""), {}))
            result = self._parse_json_object(message.content)
            reducer = self._tool_reducers.get(name)
            if reducer is not None:
                try:
                    reduced = reducer(arguments, result)
                except Exception:
                    reduced = self._generic_reduce_result(result)
            else:
                reduced = self._generic_reduce_result(result)
            content = json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))
            if len(content) > self.max_inline_tool_result_chars:
                bounded = {
                    key: self._compact_value(reduced[key], depth=1)
                    for key in (
                        "success",
                        "error",
                        "error_code",
                        "summary",
                        "outputs",
                        "output_files",
                        "output_layers",
                        "loaded_layers",
                        "artifacts",
                    )
                    if key in reduced
                }
                bounded.update(
                    {
                        "context_compacted": True,
                        "result_sha256": hashlib.sha256(
                            content.encode("utf-8")
                        ).hexdigest(),
                    }
                )
                content = json.dumps(
                    bounded,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            compacted.append(replace(message, content=content))
        return compacted

    def _externalize_completed_arguments(
        self,
        call: dict[str, Any],
        completed_ids: set[str],
    ) -> dict[str, Any]:
        call_id, _, arguments = self._parse_tool_call(call)
        if call_id not in completed_ids:
            return call
        changed = False
        compacted_arguments = dict(arguments)
        for key, value in list(compacted_arguments.items()):
            if key not in self._LARGE_ARGUMENT_FIELDS or not isinstance(value, str):
                continue
            if len(value) <= 1000:
                continue
            compacted_arguments[key] = {
                "externalized": True,
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "chars": len(value),
            }
            changed = True
        if not changed:
            return call
        compacted_call = dict(call)
        function = dict(compacted_call.get("function") or {})
        function["arguments"] = json.dumps(
            compacted_arguments,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        compacted_call["function"] = function
        return compacted_call

    def _generic_reduce_result(self, result: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(result, ensure_ascii=False, default=str)) <= self.max_inline_tool_result_chars:
            return result
        reduced = {
            key: self._compact_value(result[key], depth=0)
            for key in self._RESULT_KEYS
            if key in result
        }
        if not reduced:
            reduced = {
                "success": bool(result.get("success", True)),
                "summary": "大型工具结果已移出活动上下文，可从审计日志按需读取。",
            }
        reduced["context_compacted"] = True
        return reduced

    def _compact_value(self, value: Any, *, depth: int) -> Any:
        if depth >= 3:
            return "[nested value compacted]"
        if isinstance(value, str):
            limit = 1600 if depth == 0 else 600
            if len(value) <= limit:
                return value
            if value.lstrip().startswith(("Traceback", "Error", "ERROR")):
                return value[-limit:]
            return value[:limit] + "…"
        if isinstance(value, list):
            compacted = [self._compact_value(item, depth=depth + 1) for item in value[:20]]
            if len(value) > 20:
                compacted.append({"omitted_items": len(value) - 20})
            return compacted
        if isinstance(value, dict):
            return {
                str(key): self._compact_value(item, depth=depth + 1)
                for key, item in list(value.items())[:30]
            }
        return value

    @staticmethod
    def _bound_memory_message(messages: list[ChatMessage]) -> list[ChatMessage]:
        bounded = []
        for message in messages:
            if message.content.startswith("会话记忆：") and len(message.content) > 3200:
                bounded.append(replace(message, content=message.content[:3200] + "…"))
            else:
                bounded.append(message)
        return bounded

    def _compact_middle_messages(
        self,
        messages: list[ChatMessage],
        *,
        message_budget: int,
    ) -> list[ChatMessage]:
        groups = self._message_groups(messages)
        if len(groups) <= 2:
            return messages

        latest_user_group = max(
            (
                index
                for index, group in enumerate(groups)
                if any(message.role == "user" for message in group)
            ),
            default=len(groups) - 1,
        )
        protected = {latest_user_group, len(groups) - 1}
        for index, group in enumerate(groups):
            if any(
                message.role == "assistant"
                and message.tool_calls
                and not self._group_has_all_tool_results(group)
                for message in group
            ):
                protected.add(index)
            if any(
                message.content.startswith(("会话记忆：", "[GIS TASK STATE"))
                for message in group
            ):
                protected.add(index)

        tail_budget = max(800, int(message_budget * 0.45))
        used = 0
        for index in range(len(groups) - 1, -1, -1):
            group_tokens = estimate_messages_tokens(groups[index])
            if index in protected or used + group_tokens <= tail_budget:
                protected.add(index)
                used += group_tokens

        removed = [group for index, group in enumerate(groups) if index not in protected]
        if not removed:
            return messages
        summary = self._historical_summary(removed, max_chars=max(1200, message_budget * 2))
        summary_message = ChatMessage(role="assistant", content=summary)
        assembled: list[ChatMessage] = []
        inserted = False
        for index, group in enumerate(groups):
            if index not in protected:
                if not inserted:
                    assembled.append(summary_message)
                    inserted = True
                continue
            assembled.extend(group)
        return assembled

    @staticmethod
    def _message_groups(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
        groups: list[list[ChatMessage]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            group = [message]
            if message.role == "assistant" and message.tool_calls:
                expected = {
                    str(call.get("id") or "") for call in message.tool_calls if call.get("id")
                }
                next_index = index + 1
                while next_index < len(messages):
                    candidate = messages[next_index]
                    if candidate.role != "tool" or str(candidate.tool_call_id or "") not in expected:
                        break
                    group.append(candidate)
                    next_index += 1
                index = next_index
            else:
                index += 1
            groups.append(group)
        return groups

    @staticmethod
    def _group_has_all_tool_results(group: list[ChatMessage]) -> bool:
        calls = {
            str(call.get("id") or "")
            for message in group
            for call in message.tool_calls
            if call.get("id")
        }
        results = {
            str(message.tool_call_id or "")
            for message in group
            if message.role == "tool" and message.tool_call_id
        }
        return bool(calls) and calls.issubset(results)

    def _historical_summary(
        self,
        groups: list[list[ChatMessage]],
        *,
        max_chars: int,
    ) -> str:
        entries: list[dict[str, Any]] = []
        for group in groups:
            for message in group:
                if message.role == "user":
                    entries.append({"role": "user", "content": message.content[:800]})
                elif message.role == "assistant" and not message.tool_calls and message.content:
                    entries.append({"role": "assistant", "content": message.content[:800]})
                elif message.role == "tool":
                    entries.append(
                        {
                            "role": "tool",
                            "name": message.name,
                            "result": message.content[:800],
                        }
                    )
        payload = json.dumps(entries[-30:], ensure_ascii=False, separators=(",", ":"))
        prefix = (
            "[CONTEXT COMPACTION — REFERENCE ONLY]\n"
            "以下内容是已经发生的历史记录，只用于避免重复操作；最新用户消息是当前任务的唯一依据。\n"
        )
        return prefix + payload[:max_chars]

    @staticmethod
    def _parse_tool_call(call: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        call_id = str(call.get("id") or "")
        function = call.get("function") or {}
        name = str(function.get("name") or "") if isinstance(function, dict) else ""
        raw_arguments = function.get("arguments") if isinstance(function, dict) else {}
        if isinstance(raw_arguments, dict):
            arguments = dict(raw_arguments)
        elif isinstance(raw_arguments, str):
            arguments = QGISContextEngine._parse_json_object(raw_arguments)
        else:
            arguments = {}
        return call_id, name, arguments

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, Any]:
        try:
            value = json.loads(content or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"summary": str(content)[:1200]}
        return dict(value) if isinstance(value, dict) else {"value": value}


def estimate_text_tokens(text: str) -> int:
    non_ascii = sum(1 for character in text if ord(character) > 127)
    ascii_chars = len(text) - non_ascii
    return max(1, int(ascii_chars / 4 + non_ascii * 1.1))


def estimate_json_tokens(value: Any) -> int:
    return estimate_text_tokens(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def estimate_messages_tokens(messages: list[ChatMessage]) -> int:
    return estimate_json_tokens([message.as_api_message() for message in messages])
