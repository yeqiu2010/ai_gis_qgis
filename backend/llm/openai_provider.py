"""OpenAI-compatible chat provider using the standard library."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .base_provider import ChatMessage, ChatResponse, ToolCall


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        *,
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        timeout_seconds: int = 60,
    ):
        self.model = model
        self.base_url = self._normalize_base_url(base_url)
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}]
            + [{"role": message.role, "content": message.content} for message in messages],
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI-compatible 请求失败：HTTP {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI-compatible 请求失败：{exc}") from exc

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError:
                    arguments = {"_raw_arguments": arguments}
            tool_calls.append(
                ToolCall(
                    id=str(call.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                )
            )
        return ChatResponse(
            content=message.get("content") or "",
            model=data.get("model", self.model),
            finish_reason=choice.get("finish_reason") or "stop",
            tool_calls=tool_calls,
        )

    def _normalize_base_url(self, base_url: str) -> str:
        value = (base_url or "").strip().rstrip("/")
        if not value:
            return "https://api.openai.com/v1"
        if "://" not in value:
            value = f"http://{value}"
        return value
