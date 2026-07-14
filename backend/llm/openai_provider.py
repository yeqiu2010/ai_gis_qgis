"""OpenAI-compatible chat provider using the standard library."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from ..json_recovery import recover_json_object
from .base_provider import ChatMessage, ChatResponse, ToolCall

DEFAULT_MAX_CONTEXT_TOKENS = 32768
CONTEXT_WINDOW_SAFETY_TOKENS = 64


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
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        timeout_seconds: int = 300,
    ):
        self.model = model
        self.base_url = self._normalize_base_url(base_url)
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.temperature = temperature
        self.max_tokens = max(1, int(max_tokens))
        self.max_context_tokens = max(0, int(max_context_tokens))
        self.timeout_seconds = max(1, int(timeout_seconds))

    def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        api_messages = [{"role": "system", "content": system}] + [
            {"role": message.role, "content": message.content} for message in messages
        ]
        max_tokens = self._bounded_max_tokens(api_messages, tools)
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "messages": api_messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = self._post_chat_completion(payload)

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                arguments = self._parse_tool_arguments(arguments)
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

    def _parse_tool_arguments(self, raw_arguments: str) -> dict[str, Any]:
        """Recover valid JSON arguments from common model wrapper artifacts."""
        parsed = recover_json_object(raw_arguments)
        return parsed if parsed is not None else {"_raw_arguments": raw_arguments}

    def _post_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise self._timeout_error(payload, started, exc) from exc
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retry_payload = self._payload_for_context_window_retry(payload, body)
            if retry_payload is not None:
                try:
                    retry_request = urllib.request.Request(
                        f"{self.base_url}/chat/completions",
                        data=json.dumps(retry_payload).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    with urllib.request.urlopen(retry_request, timeout=self.timeout_seconds) as response:
                        return json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as retry_exc:
                    retry_body = retry_exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"OpenAI-compatible 请求失败：HTTP {retry_exc.code} {retry_body}"
                    ) from retry_exc
                except TimeoutError as retry_exc:
                    raise self._timeout_error(retry_payload, started, retry_exc) from retry_exc
                except urllib.error.URLError as retry_exc:
                    if isinstance(retry_exc.reason, TimeoutError):
                        raise self._timeout_error(
                            retry_payload, started, retry_exc.reason
                        ) from retry_exc
                    raise RuntimeError(f"OpenAI-compatible 请求失败：{retry_exc}") from retry_exc
            raise RuntimeError(f"OpenAI-compatible 请求失败：HTTP {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise self._timeout_error(payload, started, exc.reason) from exc
            raise RuntimeError(f"OpenAI-compatible 请求失败：{exc}") from exc

    def _timeout_error(
        self,
        payload: dict[str, Any],
        started: float,
        exc: BaseException,
    ) -> RuntimeError:
        elapsed = time.monotonic() - started
        prompt_tokens = self._estimate_prompt_tokens(payload.get("messages") or [], payload.get("tools"))
        return RuntimeError(
            "OpenAI-compatible 客户端读取超时："
            f"等待 {elapsed:.1f} 秒（配置上限 {self.timeout_seconds} 秒），"
            f"估算输入 {prompt_tokens} tokens，最大输出 {payload.get('max_tokens')} tokens。"
            "这通常表示非流式生成尚未完成，不代表服务端存在并发占用。"
            f"底层错误：{exc}"
        )

    def _bounded_max_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> int:
        if self.max_context_tokens <= 0:
            return self.max_tokens
        prompt_tokens = self._estimate_prompt_tokens(messages, tools)
        available = self.max_context_tokens - prompt_tokens - CONTEXT_WINDOW_SAFETY_TOKENS
        return max(1, min(self.max_tokens, available))

    def _estimate_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> int:
        prompt = {"messages": messages}
        if tools:
            prompt["tools"] = tools
        text = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
        non_ascii = sum(1 for char in text if ord(char) > 127)
        ascii_chars = len(text) - non_ascii
        return max(1, int(ascii_chars / 4 + non_ascii * 1.1) + 32)

    def _payload_for_context_window_retry(
        self,
        payload: dict[str, Any],
        error_body: str,
    ) -> dict[str, Any] | None:
        limits = self._parse_context_window_error(error_body)
        if limits is None:
            return None
        context_limit, input_tokens = limits
        available = context_limit - input_tokens - CONTEXT_WINDOW_SAFETY_TOKENS
        current_max_tokens = int(payload.get("max_tokens") or self.max_tokens)
        if available < 1 or available >= current_max_tokens:
            return None
        retry_payload = dict(payload)
        retry_payload["max_tokens"] = max(1, available)
        return retry_payload

    def _parse_context_window_error(self, error_body: str) -> tuple[int, int] | None:
        message = error_body
        try:
            data = json.loads(error_body)
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or error_body)
        except json.JSONDecodeError:
            pass
        context_match = re.search(r"maximum context length is\s*(\d+)\s*tokens", message, re.IGNORECASE)
        input_match = re.search(r"prompt contains at least\s*(\d+)\s*input\s*tokens", message, re.IGNORECASE)
        if not context_match or not input_match:
            return None
        return int(context_match.group(1)), int(input_match.group(1))

    def _normalize_base_url(self, base_url: str) -> str:
        value = (base_url or "").strip().rstrip("/")
        if not value:
            return "https://api.openai.com/v1"
        if "://" not in value:
            value = f"http://{value}"
        return value
