"""OpenAI-compatible chat provider using the standard library."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from ..json_recovery import recover_json_object, unwrap_raw_arguments
from .base_provider import ChatMessage, ChatResponse, ToolCall
from .errors import ContextWindowExceeded, LLMRequestRejected

DEFAULT_MAX_CONTEXT_TOKENS = 32768
MIN_CONTEXT_WINDOW_SAFETY_TOKENS = 256
MAX_CONTEXT_WINDOW_SAFETY_TOKENS = 4096
CONTEXT_WINDOW_SAFETY_RATIO = 0.01
CONTEXT_WINDOW_RETRY_ATTEMPTS = 2
MINIMUM_OUTPUT_TOKENS = 2048


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        *,
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 16384,
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
        self._observed_context_limit = 0
        self._prompt_token_bias = 0

    def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        requires_reasoning_replay = bool(tools) and self._requires_reasoning_replay()
        api_messages = self._build_api_messages(
            system,
            messages,
            requires_reasoning_replay=requires_reasoning_replay,
        )
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
            if isinstance(arguments, dict):
                arguments = unwrap_raw_arguments(arguments)
            tool_calls.append(
                ToolCall(
                    id=str(call.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                )
            )
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = self._usage_value(usage, "prompt_tokens", "input_tokens")
        output_tokens = self._usage_value(usage, "completion_tokens", "output_tokens")
        total_tokens = self._usage_value(usage, "total_tokens")
        content = str(message.get("content") or "")
        reasoning_content = (
            str(message["reasoning_content"])
            if message.get("reasoning_content") is not None
            else None
        )
        if reasoning_content is None and self._requires_reasoning_replay():
            # Some DeepSeek-compatible gateways place thinking text in
            # ``content`` but still require it under ``reasoning_content`` on
            # the next tool-enabled request. Preserve the only lossless value
            # available instead of breaking the following turn.
            reasoning_content = content
        return ChatResponse(
            content=content,
            model=data.get("model", self.model),
            reasoning_content=reasoning_content,
            finish_reason=choice.get("finish_reason") or "stop",
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens or input_tokens + output_tokens,
        )

    @staticmethod
    def _build_api_messages(
        system: str,
        messages: list[ChatMessage],
        *,
        requires_reasoning_replay: bool = False,
    ) -> list[dict[str, Any]]:
        """Keep one leading system message for strict compatible providers."""
        system_parts = [str(system or "").strip()]
        conversation: list[dict[str, Any]] = []
        for message in messages:
            api_message = message.as_api_message()
            if api_message.get("role") == "system":
                content = str(api_message.get("content") or "").strip()
                if content:
                    system_parts.append(content)
                continue
            if (
                requires_reasoning_replay
                and api_message.get("role") == "assistant"
                and api_message.get("reasoning_content") is None
            ):
                # Repair legacy history written before reasoning_content was
                # persisted. Tool-call content commonly contains the original
                # thinking text, so it is a better replay value than an empty
                # placeholder and satisfies DeepSeek's required field.
                api_message["reasoning_content"] = str(
                    api_message.get("content") or ""
                )
            conversation.append(api_message)
        combined_system = "\n\n".join(part for part in system_parts if part)
        return [{"role": "system", "content": combined_system}, *conversation]

    def _requires_reasoning_replay(self) -> bool:
        identity = f"{self.model} {self.base_url}".casefold()
        return "deepseek" in identity

    @staticmethod
    def _usage_value(usage: dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
        return 0

    def _parse_tool_arguments(self, raw_arguments: str) -> dict[str, Any]:
        """Recover valid JSON arguments from common model wrapper artifacts."""
        parsed = recover_json_object(raw_arguments)
        return parsed if parsed is not None else {"_raw_arguments": raw_arguments}

    def _post_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        started = time.monotonic()
        current_payload = payload
        reasoning_repair_used = False
        for retry_index in range(CONTEXT_WINDOW_RETRY_ATTEMPTS + 1):
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(current_payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except TimeoutError as exc:
                raise self._timeout_error(current_payload, started, exc) from exc
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if not reasoning_repair_used:
                    reasoning_payload = self._payload_for_reasoning_content_retry(
                        current_payload,
                        body,
                    )
                    if reasoning_payload is not None:
                        current_payload = reasoning_payload
                        reasoning_repair_used = True
                        continue
                retry_payload = self._payload_for_context_window_retry(
                    current_payload,
                    body,
                )
                if retry_payload is None:
                    if self._parse_context_window_error(body) is not None:
                        raise self._context_window_error(current_payload, body) from exc
                    error_type = (
                        LLMRequestRejected
                        if 400 <= exc.code < 500 and exc.code not in {408, 409, 429}
                        else RuntimeError
                    )
                    raise error_type(
                        f"OpenAI-compatible 请求失败：HTTP {exc.code} {body}"
                    ) from exc
                if retry_index >= CONTEXT_WINDOW_RETRY_ATTEMPTS:
                    raise self._context_window_error(current_payload, body) from exc
                current_payload = retry_payload
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    raise self._timeout_error(current_payload, started, exc.reason) from exc
                raise RuntimeError(f"OpenAI-compatible 请求失败：{exc}") from exc
        raise AssertionError("unreachable context-window retry state")

    @staticmethod
    def _payload_for_reasoning_content_retry(
        payload: dict[str, Any],
        error_body: str,
    ) -> dict[str, Any] | None:
        normalized_error = str(error_body or "").casefold()
        if not (
            "reasoning_content" in normalized_error
            and "must be passed back" in normalized_error
        ):
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        repaired_messages: list[Any] = []
        changed = False
        for raw_message in messages:
            if not isinstance(raw_message, dict):
                repaired_messages.append(raw_message)
                continue
            message = dict(raw_message)
            if (
                message.get("role") == "assistant"
                and message.get("reasoning_content") is None
            ):
                message["reasoning_content"] = str(message.get("content") or "")
                changed = True
            repaired_messages.append(message)
        if not changed:
            return None
        return {**payload, "messages": repaired_messages}

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
        prompt_tokens = self._estimate_prompt_tokens(messages, tools) + self._prompt_token_bias
        context_limit = self.max_context_tokens
        if self._observed_context_limit > 0:
            context_limit = min(context_limit, self._observed_context_limit)
        available = context_limit - prompt_tokens - self._context_window_safety_tokens(
            context_limit
        )
        minimum_output = min(self.max_tokens, MINIMUM_OUTPUT_TOKENS)
        if available < minimum_output:
            raise ContextWindowExceeded(
                "请求输入过大，无法保留最低输出预算："
                f"估算输入 {prompt_tokens} tokens，上下文 {context_limit} tokens，"
                f"至少需要输出 {minimum_output} tokens。"
            )
        return min(self.max_tokens, available)

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
        self._record_context_window_observation(payload, context_limit, input_tokens)
        safety_tokens = self._context_window_safety_tokens(context_limit)
        available = context_limit - input_tokens - safety_tokens
        current_max_tokens = int(payload.get("max_tokens") or self.max_tokens)
        if available < 1:
            return None
        if available >= current_max_tokens:
            # Some compatible gateways return a stale or rounded token count.
            # Still force strict progress instead of replaying the same payload.
            available = current_max_tokens - safety_tokens
        if available < 1 or available >= current_max_tokens:
            return None
        retry_payload = dict(payload)
        retry_payload["max_tokens"] = max(1, available)
        return retry_payload

    def _record_context_window_observation(
        self,
        payload: dict[str, Any],
        context_limit: int,
        input_tokens: int,
    ) -> None:
        if context_limit > 0:
            if self._observed_context_limit <= 0:
                self._observed_context_limit = context_limit
            else:
                self._observed_context_limit = min(
                    self._observed_context_limit,
                    context_limit,
                )
        estimated = self._estimate_prompt_tokens(
            payload.get("messages") or [],
            payload.get("tools"),
        )
        self._prompt_token_bias = max(
            self._prompt_token_bias,
            max(0, input_tokens - estimated),
        )

    @staticmethod
    def _context_window_safety_tokens(context_limit: int) -> int:
        proportional = int(max(0, context_limit) * CONTEXT_WINDOW_SAFETY_RATIO)
        return min(
            MAX_CONTEXT_WINDOW_SAFETY_TOKENS,
            max(MIN_CONTEXT_WINDOW_SAFETY_TOKENS, proportional),
        )

    def _context_window_error(
        self,
        payload: dict[str, Any],
        error_body: str,
    ) -> ContextWindowExceeded:
        limits = self._parse_context_window_error(error_body)
        if limits is None:
            return ContextWindowExceeded("模型服务拒绝了上下文预算。")
        context_limit, input_tokens = limits
        self._record_context_window_observation(payload, context_limit, input_tokens)
        return ContextWindowExceeded(
            "模型服务报告上下文超限，自动收缩输出预算后仍无法完成请求："
            f"精确输入 {input_tokens} tokens，上下文上限 {context_limit} tokens，"
            f"最后请求输出上限 {payload.get('max_tokens')} tokens。"
        )

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
