from __future__ import annotations

import threading

from ai_gis_qgis.qwebengine.message_protocol import agent_event
from ai_gis_qgis.qwebengine.rpc_controller import RPCController


class RecordingBridge:
    def __init__(self):
        self.events = []

    def emit_event(self, event):
        self.events.append(event)


def test_rpc_controller_suppresses_cancelled_stale_run_events():
    controller = RPCController.__new__(RPCController)
    controller.bridge = RecordingBridge()
    controller._run_lock = threading.Lock()
    controller._active_runs = {}
    controller._cancelled_runs = set()

    session_id = "session-1"
    old_run_id = "old-run"
    new_run_id = "new-run"

    with controller._run_lock:
        controller._active_runs[session_id] = old_run_id
        controller._cancelled_runs.add(old_run_id)
        controller._active_runs[session_id] = new_run_id

    controller._emit_for_run(
        agent_event("thinking", {"message": "旧任务残余消息"}, session_id=session_id),
        session_id,
        old_run_id,
    )
    controller._emit_for_run(
        agent_event("thinking", {"message": "新任务消息"}, session_id=session_id),
        session_id,
        new_run_id,
    )

    assert [event["payload"]["message"] for event in controller.bridge.events] == ["新任务消息"]

