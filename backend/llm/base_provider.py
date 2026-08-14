"""LLM provider abstractions used by AgentCore."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def as_api_message(self) -> dict[str, Any]:
        """Return an OpenAI-compatible message without lossy text wrapping."""
        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            message["tool_call_id"] = self.tool_call_id
        if self.name:
            message["name"] = self.name
        return message


@dataclass(frozen=True)
class ChatResponse:
    content: str
    model: str
    finish_reason: str = "stop"
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    usage_estimated: bool = False


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


class LLMProvider(Protocol):
    name: str
    model: str

    def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        ...


class EchoProvider:
    """Offline provider that makes the plugin usable immediately after install."""

    name = "echo"
    model = "echo-local"

    def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        user_messages = [message.content for message in messages if message.role == "user"]
        latest = user_messages[-1] if user_messages else ""
        if not latest:
            content = "你好，我是 Agent。当前正在使用离线 echo provider。"
        else:
            content = (
                "当前正在使用离线 echo provider，未调用真实 AI 或 QGIS 工具。收到的任务："
                f"{latest}\n\n"
                "请检查插件配置是否加载到 openai_compatible provider。"
            )
        return ChatResponse(content=content, model=self.model)
