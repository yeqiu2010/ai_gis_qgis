"""Event stream transport placeholder for the agent runtime."""

from __future__ import annotations

from typing import Any, Protocol


class EventSink(Protocol):
    def emit_event(self, event: dict[str, Any]):
        """Push an agent event to the frontend."""
