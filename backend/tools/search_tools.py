"""Search tools backed by SessionDB."""

from __future__ import annotations

from ...database.session_db import SessionDB
from .registry import ToolEntry


def build_search_messages_tool(session_db: SessionDB, session_id: str) -> ToolEntry:
    def handler(arguments: dict) -> dict:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "query is required"}
        limit = int(arguments.get("limit", 10))
        return {
            "success": True,
            "matches": session_db.search_messages(query, session_id=session_id, limit=limit),
        }

    return ToolEntry(
        name="search_messages",
        description="Search previous messages in the current session.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
        handler=handler,
        category="search",
    )
