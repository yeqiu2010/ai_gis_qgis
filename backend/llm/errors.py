"""Typed LLM runtime errors shared by providers and context engines."""


class ContextWindowExceeded(RuntimeError):
    """The request cannot retain the configured minimum output budget."""


class LLMRequestRejected(RuntimeError):
    """The provider rejected a request that will not succeed unchanged."""
