"""Minimal Ollama chat provider."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base_provider import ChatMessage, ChatResponse


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        *,
        model: str = "llama3.1",
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: int = 60,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}]
            + [{"role": message.role, "content": message.content} for message in messages],
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama 请求失败：{exc}") from exc

        message = data.get("message") or {}
        return ChatResponse(
            content=message.get("content", ""),
            model=data.get("model", self.model),
            finish_reason=data.get("done_reason") or "stop",
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            total_tokens=int(data.get("prompt_eval_count") or 0)
            + int(data.get("eval_count") or 0),
        )
