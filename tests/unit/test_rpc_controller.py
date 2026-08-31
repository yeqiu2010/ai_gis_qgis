from __future__ import annotations

import threading
from pathlib import Path

from ai_gis_qgis.qwebengine.message_protocol import agent_event
from ai_gis_qgis.qwebengine.rpc_controller import RPCController
from ai_gis_qgis.database.session_db import SessionDB


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


def test_cancel_run_persists_active_task_cancellation(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="cancel task", model="test", source="test")
    task_id = session_db.create_task(session.id, "旧任务", status="running")

    controller = RPCController.__new__(RPCController)
    controller.session_db = session_db
    controller.bridge = RecordingBridge()
    controller._run_lock = threading.Lock()
    controller._active_runs = {session.id: "old-run"}
    controller._cancelled_runs = set()

    result = controller.cancel_run({"session_id": session.id})

    assert result["cancelled"] is True
    assert session_db.get_task(task_id)["status"] == "cancelled"
    assert session_db.get_active_task(session.id)["status"] == "cancelled"


def test_get_messages_hides_inline_thinking_from_existing_history(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="history", model="test", source="test")
    session_db.save_message(
        session.id,
        "assistant",
        "<think>不应显示的内部推理</think>\n请补充目标字段。",
        event_type="summary",
    )
    session_db.save_message(
        session.id,
        "assistant",
        "<analysis>只有内部分析，没有答复。</analysis>",
        event_type="summary",
    )

    controller = RPCController.__new__(RPCController)
    controller.session_db = session_db

    messages = controller.get_messages({"session_id": session.id, "limit": 100})

    assert [message["content"] for message in messages] == ["请补充目标字段。"]
