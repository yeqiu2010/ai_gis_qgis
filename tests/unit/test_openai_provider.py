from __future__ import annotations

import json
import urllib.error

import pytest
from ai_gis_qgis.backend.llm.base_provider import ChatMessage
from ai_gis_qgis.backend.llm.errors import ContextWindowExceeded, LLMRequestRejected
from ai_gis_qgis.backend.llm.openai_provider import OpenAICompatibleProvider
from ai_gis_qgis.backend.llm.provider_registry import create_provider


class FakeHTTPResponse:
    def __init__(self, payload=None):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        payload = self.payload or {
            "model": "gemma-4-31B-it-Q4:latest",
            "choices": [
                {
                    "message": {"content": "this is a test"},
                    "finish_reason": "stop",
                }
            ],
        }
        return json.dumps(payload).encode("utf-8")


def test_openai_compatible_allows_local_ollama_without_api_key(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeHTTPResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(
        model="gemma-4-31B-it-Q4:latest",
        base_url="10.0.19.214:11430/v1",
        api_key="",
    )

    response = provider.chat(
        system="",
        messages=[ChatMessage(role="user", content="Say this is a test")],
    )

    assert captured["url"] == "http://10.0.19.214:11430/v1/chat/completions"
    assert "Authorization" not in captured["headers"]
    assert captured["payload"]["model"] == "gemma-4-31B-it-Q4:latest"
    assert response.content == "this is a test"


def test_openai_compatible_reads_usage_tokens(monkeypatch):
    def fake_urlopen(request, timeout):
        return FakeHTTPResponse(
            {
                "model": "usage-model",
                "choices": [
                    {
                        "message": {"content": "完成"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 18,
                    "total_tokens": 138,
                },
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(model="usage-model")

    response = provider.chat(
        system="system",
        messages=[ChatMessage(role="user", content="统计 token")],
    )

    assert response.input_tokens == 120
    assert response.output_tokens == 18
    assert response.total_tokens == 138
    assert response.usage_estimated is False


def test_openai_compatible_folds_system_messages_into_leading_prompt(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(model="deepseek")

    provider.chat(
        system="基础系统指令",
        messages=[
            ChatMessage(role="user", content="执行任务"),
            ChatMessage(role="system", content="确认后继续执行"),
            ChatMessage(role="assistant", content="继续"),
        ],
    )

    api_messages = captured["payload"]["messages"]
    assert [message["role"] for message in api_messages] == [
        "system",
        "user",
        "assistant",
    ]
    assert "基础系统指令" in api_messages[0]["content"]
    assert "确认后继续执行" in api_messages[0]["content"]


def test_provider_registry_passes_configured_request_timeout():
    provider = create_provider(
        {
            "llm": {
                "provider": "openai_compatible",
                "model": "qwen",
                "base_url": "http://127.0.0.1:8000/v1",
                "request_timeout_seconds": 480,
            }
        }
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.timeout_seconds == 480
    assert provider.max_tokens == 16384


def test_timeout_error_reports_client_limit_and_request_size(monkeypatch):
    def fake_urlopen(request, timeout):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(
        model="qwen",
        base_url="http://127.0.0.1:8000/v1",
        timeout_seconds=300,
    )

    try:
        provider.chat(system="system", messages=[ChatMessage(role="user", content="测试")])
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected timeout")

    assert "配置上限 300 秒" in message
    assert "估算输入" in message
    assert "最大输出" in message
    assert "不代表服务端存在并发占用" in message


class FakeHTTPErrorBody:
    def __init__(self, body: str):
        self.body = body.encode("utf-8")

    def read(self):
        return self.body

    def close(self):
        pass


def test_openai_compatible_marks_structural_http_400_as_non_retryable(monkeypatch):
    error_body = json.dumps(
        {"error": {"message": "System message must be at the beginning."}}
    )

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            hdrs=None,
            fp=FakeHTTPErrorBody(error_body),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(model="deepseek")

    with pytest.raises(LLMRequestRejected, match="System message must be at the beginning"):
        provider.chat(
            system="系统指令",
            messages=[ChatMessage(role="user", content="执行任务")],
        )


def test_openai_compatible_retries_with_bounded_max_tokens_after_context_error(monkeypatch):
    payloads = []
    error_body = json.dumps(
        {
            "error": {
                "message": (
                    "This model's maximum context length is 32768 tokens. "
                    "However, you requested 24000 output tokens and your prompt "
                    "contains at least 8769 input tokens, for a total of at least "
                    "32769 tokens. Please reduce the length of the input prompt or "
                    "the number of requested output tokens. "
                    "(parameter=input_tokens,value=8769)"
                ),
                "type": "BadRequestError",
                "param": "input_tokens",
                "code": 400,
            }
        }
    )

    def fake_urlopen(request, timeout):
        payloads.append(json.loads(request.data.decode("utf-8")))
        if len(payloads) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "Bad Request",
                hdrs=None,
                fp=FakeHTTPErrorBody(error_body),
            )
        return FakeHTTPResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(
        model="qwen",
        base_url="http://127.0.0.1:8000/v1",
        api_key="",
        max_tokens=24000,
        max_context_tokens=32768,
    )

    response = provider.chat(
        system="",
        messages=[ChatMessage(role="user", content="分析当前工程")],
    )

    assert response.content == "this is a test"
    assert [payload["max_tokens"] for payload in payloads] == [24000, 23672]
    assert provider._observed_context_limit == 32768
    assert provider._prompt_token_bias > 0


def test_large_context_budget_keeps_margin_for_tokenizer_estimation_error(monkeypatch):
    provider = OpenAICompatibleProvider(
        model="deepseek-v4-flash",
        max_tokens=65536,
        max_context_tokens=65536,
    )
    monkeypatch.setattr(provider, "_estimate_prompt_tokens", lambda messages, tools: 17628)

    bounded = provider._bounded_max_tokens([], None)

    assert bounded == 47253
    assert 17693 + bounded < 65536


def test_repeated_context_errors_shrink_strictly_and_stop_agent_retries(monkeypatch):
    payloads = []
    error_body = json.dumps(
        {
            "error": {
                "message": (
                    "This model's maximum context length is 65536 tokens. "
                    "However, you requested 47844 output tokens and your prompt "
                    "contains at least 17693 input tokens, for a total of at least "
                    "65537 tokens. (parameter=input_tokens, value=17693)"
                ),
                "type": "BadRequestError",
                "param": "input_tokens",
                "code": 400,
            }
        }
    )

    def fake_urlopen(request, timeout):
        payloads.append(json.loads(request.data.decode("utf-8")))
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            hdrs=None,
            fp=FakeHTTPErrorBody(error_body),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(
        model="deepseek-v4-flash",
        max_tokens=65536,
        max_context_tokens=65536,
    )

    with pytest.raises(ContextWindowExceeded, match="精确输入 17693"):
        provider._post_chat_completion(
            {
                "model": provider.model,
                "max_tokens": 47844,
                "messages": [{"role": "user", "content": "分析"}],
            }
        )

    requested = [payload["max_tokens"] for payload in payloads]
    assert requested == [47844, 47188, 46533]
    assert requested == sorted(requested, reverse=True)


def test_tool_arguments_parser_recovers_json_before_invoke_markup():
    provider = OpenAICompatibleProvider()
    raw = (
        '{"stage_name":"generated_code","artifact":{"summary":"完成"}}'
        "\n</invoke>}"
    )

    assert provider._parse_tool_arguments(raw) == {
        "stage_name": "generated_code",
        "artifact": {"summary": "完成"},
    }


def test_tool_arguments_parser_closes_outer_object_before_invoke_markup():
    provider = OpenAICompatibleProvider()
    raw = '{"stage_name":"data_overview","artifact":{"summary":"完成"}\n</invoke>}'

    assert provider._parse_tool_arguments(raw) == {
        "stage_name": "data_overview",
        "artifact": {"summary": "完成"},
    }


def test_chat_unwraps_raw_arguments_object_from_compatible_server(monkeypatch):
    def fake_urlopen(request, timeout):
        return FakeHTTPResponse(
            {
                "model": "test-model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "record_pipeline_stage",
                                        "arguments": {
                                            "_raw_arguments": json.dumps(
                                                {
                                                    "stage_name": "generated_code",
                                                    "artifact": {"code": "print('ok')"},
                                                },
                                                ensure_ascii=False,
                                            )
                                        },
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider()

    response = provider.chat(
        system="",
        messages=[ChatMessage(role="user", content="生成代码")],
        tools=[{"type": "function", "function": {"name": "record_pipeline_stage"}}],
    )

    assert response.tool_calls[0].arguments == {
        "stage_name": "generated_code",
        "artifact": {"code": "print('ok')"},
    }


def test_openai_compatible_preserves_reasoning_content_for_tool_follow_up(monkeypatch):
    captured_payloads = []

    def fake_urlopen(request, timeout):
        captured_payloads.append(json.loads(request.data.decode("utf-8")))
        if len(captured_payloads) == 1:
            return FakeHTTPResponse(
                {
                    "model": "deepseek-reasoner",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": "",
                                "reasoning_content": "需要先读取图层。",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "list_layers",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            )
        return FakeHTTPResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(model="deepseek-reasoner")

    response = provider.chat(
        system="system",
        messages=[ChatMessage(role="user", content="列出图层")],
        tools=[{"type": "function", "function": {"name": "list_layers"}}],
    )
    provider.chat(
        system="system",
        messages=[
            ChatMessage(role="user", content="列出图层"),
            ChatMessage(
                role="assistant",
                content=response.content,
                reasoning_content=response.reasoning_content,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "list_layers", "arguments": "{}"},
                    }
                ],
            ),
            ChatMessage(role="tool", content="[]", tool_call_id="call-1"),
        ],
    )

    assert response.reasoning_content == "需要先读取图层。"
    assistant_message = captured_payloads[1]["messages"][-2]
    assert assistant_message["reasoning_content"] == "需要先读取图层。"
