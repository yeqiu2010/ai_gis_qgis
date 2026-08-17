"""Persist authoritative QGIS Processing details across Pipeline compaction."""

from __future__ import annotations

import json
import re
from typing import Any

PROCESSING_EVIDENCE_STATE_SUFFIX = "qgis_processing_algorithm_evidence"
PROCESSING_ALGORITHM_ID_PATTERN = re.compile(
    r"\b[a-zA-Z][a-zA-Z0-9_-]*:[a-zA-Z0-9_.-]+\b"
)
NON_PROCESSING_ID_PREFIXES = {"crs", "epsg", "file", "http", "https", "memory", "urn"}
KNOWN_PROCESSING_PROVIDER_PREFIXES = {
    "3d",
    "gdal",
    "grass",
    "grass7",
    "mesh",
    "native",
    "otb",
    "pdal",
    "qgis",
    "saga",
    "sagang",
}


def processing_evidence_state_key(session_id: str) -> str:
    return f"{session_id}:{PROCESSING_EVIDENCE_STATE_SUFFIX}"


def read_processing_evidence(session_db: Any, session_id: str) -> dict[str, dict[str, Any]]:
    if session_db is None or not session_id:
        return {}
    raw = session_db.get_state(processing_evidence_state_key(session_id))
    try:
        value = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(tool_id).lower(): _normalize_processing_detail(str(tool_id), detail)
        for tool_id, detail in value.items()
        if isinstance(detail, dict)
    }


def _normalize_processing_detail(
    tool_id: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    """Apply catalog corrections to evidence saved by older plugin builds."""
    normalized = dict(detail)
    if tool_id.lower() == "gdal:cliprasterbyextent":
        normalized["parameters"] = re.sub(
            r"(?m)^EXTENT(?=\s*:)",
            "PROJWIN",
            str(normalized.get("parameters") or ""),
            count=1,
        )
        normalized["code_example"] = re.sub(
            r"(?P<quote>['\"])EXTENT(?P=quote)(?=\s*:)",
            lambda match: f"{match.group('quote')}PROJWIN{match.group('quote')}",
            str(normalized.get("code_example") or ""),
            count=1,
        )
    return normalized


def record_processing_evidence(
    session_db: Any,
    session_id: str,
    details: list[dict[str, Any]],
) -> None:
    if session_db is None or not session_id:
        return
    evidence = read_processing_evidence(session_db, session_id)
    for detail in details:
        tool_id = str(detail.get("tool_id") or "").strip().lower()
        if not tool_id:
            continue
        evidence[tool_id] = {
            "tool_id": detail.get("tool_id") or tool_id,
            "name": detail.get("name") or "",
            "provider": detail.get("provider") or "",
            "description": detail.get("description") or "",
            "parameters": detail.get("parameters") or "",
            "code_example": detail.get("code_example") or "",
        }
    # A Pipeline normally needs only a handful of algorithms. Keep a generous
    # bound so a long-lived session cannot grow this state without limit.
    if len(evidence) > 32:
        evidence = dict(list(evidence.items())[-32:])
    session_db.set_state(
        processing_evidence_state_key(session_id),
        json.dumps(evidence, ensure_ascii=False),
    )


def clear_processing_evidence(session_db: Any, session_id: str) -> None:
    if session_db is not None and session_id:
        session_db.delete_state(processing_evidence_state_key(session_id))


def processing_algorithm_ids(value: Any) -> set[str]:
    """Extract every explicit Processing ID cited by a plan or evidence object."""
    found: set[str] = set()

    def visit(item: Any, *, algorithm_context: bool = False) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized_key = str(key).strip().lower()
                visit(
                    child,
                    algorithm_context=normalized_key
                    in {
                        "algorithm",
                        "algorithms",
                        "algorithm_id",
                        "tool_id",
                        "algorithm_evidence",
                        "verified_algorithm_evidence",
                    },
                )
            return
        if isinstance(item, list):
            for child in item:
                visit(child, algorithm_context=algorithm_context)
            return
        if isinstance(item, str):
            for match in PROCESSING_ALGORITHM_ID_PATTERN.findall(item):
                normalized = match.lower()
                prefix = normalized.split(":", 1)[0]
                if prefix in NON_PROCESSING_ID_PREFIXES:
                    continue
                if algorithm_context or prefix in KNOWN_PROCESSING_PROVIDER_PREFIXES:
                    found.add(normalized)

    visit(value)
    return found


def processing_parameter_names(detail: dict[str, Any]) -> list[str]:
    return sorted(
        set(
            re.findall(
                r"(?m)^\s*([A-Z][A-Z0-9_]*)\s*:",
                str(detail.get("parameters") or ""),
            )
        )
    )


def compact_processing_evidence(
    evidence: dict[str, dict[str, Any]],
    tool_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    selected = tool_ids if tool_ids is not None else set(evidence)
    compact: dict[str, dict[str, Any]] = {}
    for tool_id in sorted(selected):
        detail = evidence.get(tool_id.lower())
        if not detail:
            continue
        compact[tool_id.lower()] = {
            "tool_id": detail.get("tool_id") or tool_id,
            "parameter_names": processing_parameter_names(detail),
            "parameters": str(detail.get("parameters") or "")[:12000],
            "code_example": str(detail.get("code_example") or "")[:8000],
        }
    return compact
