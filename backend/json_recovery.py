"""Small, deterministic recovery helpers for model-generated JSON."""

from __future__ import annotations

import json
from typing import Any


def recover_json_object(raw_value: str) -> dict[str, Any] | None:
    """Recover one JSON object from common compatible-model wrappers.

    Some OpenAI-compatible servers return a valid object followed by tool-call
    markup, while others truncate only the final outer braces.  Recovery is
    deliberately conservative: it never edits JSON content and only extracts
    the first complete object or closes a few unmatched outer object levels.
    """
    raw = (raw_value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    if start < 0:
        return None
    candidate = raw[start:]
    try:
        parsed, _ = json.JSONDecoder(strict=False).raw_decode(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    candidate = candidate.split("</invoke", 1)[0].rstrip()
    for closing_count in range(1, 4):
        try:
            parsed = json.loads(candidate + "}" * closing_count, strict=False)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def unwrap_raw_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Unwrap compatible-provider ``_raw_arguments`` object wrappers.

    Some servers return tool arguments as an object whose only meaningful value
    is another serialized arguments object.  Preserve any sibling metadata and
    unwrap a small number of nested wrappers without guessing at invalid JSON.
    """
    current = arguments
    for _ in range(3):
        raw_arguments = current.get("_raw_arguments")
        if not isinstance(raw_arguments, str):
            break
        recovered = recover_json_object(raw_arguments)
        if recovered is None:
            break
        siblings = {
            key: value
            for key, value in current.items()
            if key != "_raw_arguments"
        }
        current = {**recovered, **siblings}
    return current


def is_probably_truncated_json(raw_value: str) -> bool:
    """Return whether JSON ends with an open string/object/array."""
    raw = (raw_value or "").strip()
    start = raw.find("{")
    if start < 0:
        return False
    depth = 0
    in_string = False
    escaped = False
    for char in raw[start:]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return in_string or depth > 0
