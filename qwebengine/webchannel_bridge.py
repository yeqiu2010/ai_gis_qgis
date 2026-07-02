"""QWebChannel bridge exposed to the Vue frontend."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .message_protocol import RPCRequest, rpc_error, rpc_result

try:
    from qgis.PyQt.QtCore import QObject, pyqtSignal, pyqtSlot
except Exception:  # pragma: no cover - QGIS supplies these modules at runtime.
    class QObject:
        def __init__(self, parent=None):
            self.parent = parent

    class _Signal:
        def emit(self, *args, **kwargs):
            return None

    def pyqtSignal(*args, **kwargs):
        return _Signal()

    def pyqtSlot(*args, **kwargs):
        def decorator(func):
            return func

        return decorator


Handler = Callable[[RPCRequest], Any]


class QWebChannelBridge(QObject):
    """JSON-RPC gateway for the plugin frontend.

    Synchronous responses only acknowledge the request. Long-running agent
    progress is pushed through ``eventEmitted``.
    """

    responseReady = pyqtSignal(str)
    eventEmitted = pyqtSignal(str)

    def __init__(self, handler: Handler | None = None, parent=None):
        super().__init__(parent)
        self._handler = handler or self._default_handler

    @pyqtSlot(str)
    def sendMessage(self, message: str):
        payload = None
        try:
            payload = json.loads(message)
            request = RPCRequest.from_dict(payload)
            result = self._handler(request)
            response = rpc_result(request.id, result)
        except Exception as exc:
            request_id = payload.get("id") if isinstance(payload, dict) else None
            response = rpc_error(request_id, "bad_request", str(exc))

        self.responseReady.emit(json.dumps(response, ensure_ascii=False))

    def emit_event(self, event: dict[str, Any]):
        self.eventEmitted.emit(json.dumps(event, ensure_ascii=False))

    def _default_handler(self, request: RPCRequest) -> dict[str, Any]:
        return {"accepted": False, "method": request.method, "message": "RPC controller is not configured"}
