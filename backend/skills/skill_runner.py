"""Isolated, bounded Skill runner used by optional ``invoke_skill`` delegation."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..llm.base_provider import ChatMessage, LLMProvider
from ..tools.registry import ToolRegistry
from .skill_manager import SkillManager


class SkillRunner:
    def __init__(
        self,
        *,
        llm_provider: LLMProvider,
        skill_manager: SkillManager,
        tool_registry: ToolRegistry,
        max_iterations: int = 8,
        max_tool_calls: int = 12,
        should_cancel: Callable[[], bool] | None = None,
    ):
        self.llm_provider = llm_provider
        self.skill_manager = skill_manager
        self.tool_registry = tool_registry
        self.max_iterations = max(1, max_iterations)
        self.max_tool_calls = max(1, max_tool_calls)
        self.should_cancel = should_cancel or (lambda: False)

    def run(
        self,
        *,
        skill_name: str,
        task: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        document = self.skill_manager.get(skill_name)
        if document is None:
            return self._failure(f"Skill 不存在: {skill_name}")
        available, reasons = self.skill_manager.availability(skill_name)
        if not available:
            return self._failure("Skill 当前不可用: " + "；".join(reasons))
        missing_inputs = self._missing_required_inputs(document.execution_contract, inputs)
        if missing_inputs:
            return self._waiting_for_user(
                "子任务缺少 Skill 执行契约要求的输入。",
                missing_inputs,
            )

        definitions = []
        allowed_names: set[str] = set()
        for definition in self.tool_registry.definitions_for_skill(skill_name):
            name = str(definition.get("function", {}).get("name") or "")
            if not name:
                continue
            entry = self.tool_registry.get(name)
            if entry.category in {"skill", "planning", "delegation"}:
                continue
            if entry.requires_confirmation or entry.destructive or entry.writes_project:
                continue
            allowed_names.add(name)
            definitions.append(definition)

        contract = document.execution_contract
        system = (
            "你是隔离的 Skill Runner。你看不到父会话，只能使用下面提供的目标、输入和 Skill 指令。"
            "不得向用户直接提问，也不得声称执行未调用的工具。缺少输入时返回 JSON："
            '{"status":"waiting_for_user","summary":"...","missing_inputs":[...],'
            '"artifacts":[],"evidence":[],"error":null}。'
            "成功或失败时也必须返回同一结构的 JSON；status 只能是 completed、failed、"
            "waiting_for_user、cancelled。\n\n"
            f"执行契约：\n{json.dumps(contract, ensure_ascii=False)}\n\n"
            f"{self.skill_manager.compose_prompt(skill_name)}"
        )
        messages = [
            ChatMessage(
                role="user",
                content=json.dumps(
                    {"task": task, "inputs": inputs},
                    ensure_ascii=False,
                ),
            )
        ]
        tool_call_count = 0
        for _ in range(self.max_iterations):
            if self.should_cancel():
                return self._cancelled()
            response = self.llm_provider.chat(
                system=system,
                messages=messages,
                tools=definitions,
            )
            if not response.tool_calls:
                return self._parse_final(response.content)
            results: list[dict[str, Any]] = []
            for call in response.tool_calls:
                if self.should_cancel():
                    return self._cancelled()
                if tool_call_count >= self.max_tool_calls:
                    return self._failure("子任务已达到工具调用预算。")
                tool_call_count += 1
                if call.name not in allowed_names:
                    results.append(
                        {
                            "name": call.name,
                            "success": False,
                            "error": "该工具未被当前隔离 Skill 允许或具有副作用。",
                        }
                    )
                    continue
                result, duration_ms = self.tool_registry.execute(call.name, call.arguments)
                results.append(
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "result": result,
                        "duration_ms": duration_ms,
                    }
                )
            messages.append(
                ChatMessage(
                    role="assistant",
                    content="隔离子任务工具结果：\n" + json.dumps(results, ensure_ascii=False),
                )
            )
        return self._failure("子任务已达到迭代预算。")

    @staticmethod
    def _parse_final(content: str) -> dict[str, Any]:
        raw = str(content or "").strip()
        candidate = raw
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            candidate = "\n".join(lines).strip()
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return SkillRunner._failure("子任务没有返回约定的结构化 JSON 结果。")
        if not isinstance(parsed, dict):
            return SkillRunner._failure("子任务返回值不是 JSON object。")
        status = str(parsed.get("status") or "completed")
        if status not in {"completed", "failed", "waiting_for_user", "cancelled"}:
            status = "failed"
        return {
            "success": status == "completed",
            "status": status,
            "summary": str(parsed.get("summary") or raw),
            "artifacts": parsed.get("artifacts") if isinstance(parsed.get("artifacts"), list) else [],
            "missing_inputs": (
                parsed.get("missing_inputs")
                if isinstance(parsed.get("missing_inputs"), list)
                else []
            ),
            "evidence": parsed.get("evidence") if isinstance(parsed.get("evidence"), list) else [],
            "error": parsed.get("error"),
        }

    @staticmethod
    def _failure(error: str) -> dict[str, Any]:
        return {
            "success": False,
            "status": "failed",
            "summary": "子任务执行失败。",
            "artifacts": [],
            "missing_inputs": [],
            "evidence": [],
            "error": error,
        }

    @staticmethod
    def _waiting_for_user(summary: str, missing_inputs: list[str]) -> dict[str, Any]:
        return {
            "success": False,
            "status": "waiting_for_user",
            "summary": summary,
            "artifacts": [],
            "missing_inputs": missing_inputs,
            "evidence": [],
            "error": None,
        }

    @staticmethod
    def _cancelled() -> dict[str, Any]:
        return {
            "success": False,
            "status": "cancelled",
            "summary": "子任务已取消。",
            "artifacts": [],
            "missing_inputs": [],
            "evidence": [],
            "error": None,
        }

    @staticmethod
    def _missing_required_inputs(
        contract: dict[str, Any],
        inputs: dict[str, Any],
    ) -> list[str]:
        input_contract = contract.get("inputs")
        if not isinstance(input_contract, dict):
            return []
        required = input_contract.get("required")
        if isinstance(required, dict):
            names = required.keys()
        elif isinstance(required, list):
            names = required
        else:
            return []
        return [
            str(name)
            for name in names
            if str(name) not in inputs or inputs.get(str(name)) in (None, "")
        ]
