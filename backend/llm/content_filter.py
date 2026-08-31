"""Filters for provider text that must never be rendered to end users."""

from __future__ import annotations

import re


_HIDDEN_REASONING_BLOCK = re.compile(
    r"<\s*(?P<tag>think|thinking|reasoning|analysis)\b[^>]*>"
    r".*?"
    r"<\s*/\s*(?P=tag)\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_HIDDEN_REASONING_FENCE = re.compile(
    r"```(?:think|thinking|reasoning|analysis)\s*\r?\n.*?```",
    flags=re.IGNORECASE | re.DOTALL,
)
_UNCLOSED_REASONING_BLOCK = re.compile(
    r"<\s*(?:think|thinking|reasoning|analysis)\b[^>]*>.*$",
    flags=re.IGNORECASE | re.DOTALL,
)
_REASONING_TAG = re.compile(
    r"<\s*/?\s*(?:think|thinking|reasoning|analysis)\b[^>]*>",
    flags=re.IGNORECASE,
)
_EXCESS_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def strip_hidden_reasoning(content: str) -> str:
    """Remove inline reasoning containers while preserving the visible answer."""
    visible = _HIDDEN_REASONING_BLOCK.sub("", str(content or ""))
    visible = _HIDDEN_REASONING_FENCE.sub("", visible)
    visible = _UNCLOSED_REASONING_BLOCK.sub("", visible)
    visible = _REASONING_TAG.sub("", visible)
    visible = _EXCESS_BLANK_LINES.sub("\n\n", visible)
    return visible.strip()
