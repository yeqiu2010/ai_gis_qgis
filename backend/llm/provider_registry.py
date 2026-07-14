"""Factory for configured LLM providers."""

from __future__ import annotations

from typing import Any

from .base_provider import EchoProvider, LLMProvider
from .ollama_provider import OllamaProvider
from .openai_provider import OpenAICompatibleProvider


def create_provider(config: dict[str, Any] | None = None) -> LLMProvider:
    llm_config = (config or {}).get("llm", config or {})
    provider_name = llm_config.get("provider", "echo")

    if provider_name == "ollama":
        return OllamaProvider(
            model=llm_config.get("model", "llama3.1"),
            base_url=llm_config.get("base_url", "http://127.0.0.1:11434"),
        )
    if provider_name in {"openai", "openai_compatible"}:
        return OpenAICompatibleProvider(
            model=llm_config.get("model", "gpt-4o"),
            base_url=llm_config.get("base_url") or "https://api.openai.com/v1",
            api_key=llm_config.get("api_key") or None,
            temperature=float(llm_config.get("temperature", 0.1)),
            max_tokens=int(llm_config.get("max_tokens", 4096)),
            max_context_tokens=int(llm_config.get("max_context_tokens", 32768)),
            timeout_seconds=int(llm_config.get("request_timeout_seconds", 300)),
        )
    return EchoProvider()
