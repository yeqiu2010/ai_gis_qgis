from __future__ import annotations

import json
import urllib.error

from ai_gis_qgis.backend.llm.base_provider import ChatMessage
from ai_gis_qgis.backend.llm.openai_provider import OpenAICompatibleProvider


class FakeHTTPResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(
            {
                "model": "gemma-4-31B-it-Q4:latest",
                "choices": [
                    {
                        "message": {"content": "this is a test"},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode("utf-8")


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


class FakeHTTPErrorBody:
    def __init__(self, body: str):
        self.body = body.encode("utf-8")

    def read(self):
        return self.body

    def close(self):
        pass


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
    assert [payload["max_tokens"] for payload in payloads] == [24000, 23935]
