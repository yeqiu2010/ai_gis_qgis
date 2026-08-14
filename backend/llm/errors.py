"""Typed LLM runtime errors shared by providers and context engines."""


class ContextWindowExceeded(RuntimeError):
    """The request cannot retain the configured minimum output budget."""
