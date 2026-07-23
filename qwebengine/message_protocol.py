"""JSON-RPC and agent event protocol contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

RPC_METHODS = {
    "createSession",
    "listSessions",
    "getMessages",
    "getTaskState",
    "listLoadedSkills",
    "chat",
    "confirmToolCall",
    "cancelRun",
    "getSettings",
    "saveSettings",
}

AgentEventType = Literal[
    "run_start",
    "run_metrics",
    "thinking",
    "message_delta",
    "message",
    "tool_start",
    "tool_end",
    "stage_start",
    "stage_end",
    "code_generated",
    "confirm_request",
    "confirm_resolved",
    "error",
    "complete",
]


class RPCError(TypedDict, total=False):
    code: str
    message: str
    details: Any


@dataclass(frozen=True)
class RPCRequest:
    id: int
    method: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RPCRequest:
        request_id = payload.get("id")
        method = payload.get("method")
        if not isinstance(request_id, int):
            raise ValueError("RPC request id must be an integer")
        if method not in RPC_METHODS:
            raise ValueError(f"Unsupported RPC method: {method!r}")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("RPC request params must be an object")
        return cls(id=request_id, method=method, params=params)


def rpc_result(request_id: int, result: Any = None) -> dict[str, Any]:
    return {"id": request_id, "result": result}


def rpc_error(
    request_id: int | None,
    code: str,
    message: str,
    details: Any = None,
) -> dict[str, Any]:
    error: RPCError = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"id": request_id, "error": error}


def agent_event(
    event_type: AgentEventType,
    payload: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"type": event_type, "payload": payload or {}}
    if session_id is not None:
        event["session_id"] = session_id
    if run_id is not None:
        event["run_id"] = run_id
    return event
