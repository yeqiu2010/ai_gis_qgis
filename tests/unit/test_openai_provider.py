from __future__ import annotations

import json

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
