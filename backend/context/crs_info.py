"""Helpers for serializing QGIS coordinate reference systems."""

from __future__ import annotations

from typing import Any


def _crs_text(crs: Any, method_name: str) -> str:
    """Call a CRS text accessor without letting provider quirks break inspection."""
    try:
        value = getattr(crs, method_name)()
    except Exception:
        return ""
    return str(value or "").strip()


def describe_crs(crs: Any) -> dict[str, str]:
    """Return both a concise CRS value and its portable custom definition.

    Custom QGIS CRSs commonly have an empty auth id or a machine-local ``USER:*``
    id.  In those cases the PROJ/WKT definition is the only value an agent can
    reliably use to understand the CRS in another processing step.
    """
    try:
        valid = bool(crs is not None and crs.isValid())
    except Exception:
        valid = False
    if not valid:
        return {
            "crs": "",
            "crs_authid": "",
            "crs_name": "",
            "crs_definition": "",
            "crs_definition_format": "",
        }

    authid = _crs_text(crs, "authid")
    name = _crs_text(crs, "description")
    is_local_custom_crs = not authid or authid.upper().startswith("USER:")
    proj = _crs_text(crs, "toProj") if is_local_custom_crs else ""
    wkt = _crs_text(crs, "toWkt") if is_local_custom_crs and not proj else ""
    definition = proj or wkt
    definition_format = "proj" if proj else ("wkt" if wkt else "")

    return {
        "crs": definition or authid,
        "crs_authid": authid,
        "crs_name": name,
        "crs_definition": definition,
        "crs_definition_format": definition_format,
    }
