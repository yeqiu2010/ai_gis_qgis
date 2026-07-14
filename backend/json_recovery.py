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
