"""JSON-RPC method controller for the QWebChannel bridge."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from ..backend.agent_core import AgentCore
from ..backend.context.qgis_context import QGISContext
from ..backend.llm.provider_registry import create_provider
from ..config.settings import SettingsManager
from ..database.session_db import SessionDB
from .main_thread_executor import MainThreadExecutor
from .message_protocol import RPCRequest, agent_event


class RPCController:
    def __init__(self, *, iface=None, plugin_dir: Path | None = None, bridge=None):
        self.iface = iface
        self.plugin_dir = plugin_dir
        self.bridge = bridge
        self.settings = SettingsManager()
        self.config = self.settings.load()
        self.session_db = SessionDB(self.config["database"]["path"])
        self.main_thread_executor = MainThreadExecutor()
        self._run_lock = threading.Lock()
        self._active_runs: dict[str, str] = {}
        self._cancelled_runs: set[str] = set()
        self.agent_core = self._create_agent_core()
        self._workers: list[threading.Thread] = []

    def bind_bridge(self, bridge) -> None:
        self.bridge = bridge

    def handle(self, request: RPCRequest) -> dict[str, Any] | list[dict[str, Any]]:
        handlers = {
            "createSession": self.create_session,
            "listSessions": self.list_sessions,
            "getMessages": self.get_messages,
            "chat": self.chat,
            "confirmToolCall": self.confirm_tool_call,
            "cancelRun": self.cancel_run,
            "getSettings": self.get_settings,
            "saveSettings": self.save_settings,
        }
        return handlers[request.method](request.params)

    def create_session(self, params: dict[str, Any]) -> dict[str, Any]:
        context = QGISContext.collect(self.iface)
        session = self.session_db.create_session(
            title=params.get("title") or "新会话",
            model=self.agent_core.llm_provider.model,
            qgis_project_path=context.project_path,
            layer_count=context.layer_count,
        )
        return {
            "id": session.id,
            "title": session.title,
            "started_at": session.started_at,
            "message_count": session.message_count,
            "model": session.model,
        }

    def list_sessions(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self.session_db.list_sessions(limit=int(params.get("limit", 50)))

    def get_messages(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        session_id = self._require_session_id(params)
        return self.session_db.get_messages(session_id, limit=int(params.get("limit", 100)))

    def chat(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = self._require_session_id(params)
        message = str(params.get("message") or "").strip()
        if not message:
            raise ValueError("message 不能为空")

        self._start_worker(
            "hermes-chat",
            self._run_chat,
            session_id,
            message,
            QGISContext.collect(self.iface),
        )
        return {"accepted": True, "session_id": session_id, "background": True}

    def confirm_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = self._require_session_id(params)
        confirmation_id = str(params.get("confirmation_id") or "").strip()
        if not confirmation_id:
            raise ValueError("confirmation_id 不能为空")
        approved = bool(params.get("approved", False))
        self._start_worker(
            "hermes-confirm",
            self._run_confirm_tool_call,
            session_id,
            confirmation_id,
            approved,
        )
        return {
            "accepted": True,
            "session_id": session_id,
            "confirmation_id": confirmation_id,
            "approved": approved,
            "background": True,
        }

    def cancel_run(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = self._require_session_id(params)
        with self._run_lock:
            active_run_id = self._active_runs.get(session_id)
            active = active_run_id is not None
            if active_run_id is not None:
                self._cancelled_runs.add(active_run_id)
        self._emit(agent_event("complete", {"cancelled": True}, session_id=session_id))
        return {"accepted": True, "session_id": session_id, "active": active, "cancelled": True}

    def get_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._redact_settings(self.config)

    def save_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        llm_params = params.get("llm")
        if isinstance(llm_params, dict) and llm_params.get("api_key") in {"", "***"}:
            llm_params["api_key"] = self.config.get("llm", {}).get("api_key", "")
        self.config = self.settings.save(params)
        self.agent_core = self._create_agent_core()
        return self._redact_settings(self.config)

    def _start_worker(self, name: str, target, *args) -> None:
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        worker = threading.Thread(target=target, name=name, args=args, daemon=True)
        self._workers.append(worker)
        worker.start()

    def _run_chat(self, session_id: str, message: str, qgis_context: QGISContext) -> None:
        controller_run_id = str(uuid.uuid4())
        with self._run_lock:
            previous_run_id = self._active_runs.get(session_id)
            if previous_run_id is not None:
                self._cancelled_runs.add(previous_run_id)
            self._active_runs[session_id] = controller_run_id
            self._cancelled_runs.discard(controller_run_id)

        def emit_current_run(event: dict[str, Any]) -> None:
            self._emit_for_run(event, session_id, controller_run_id)

        try:
            self._create_agent_core(controller_run_id).run(
                session_id=session_id,
                user_message=message,
                emit=emit_current_run,
                qgis_context=qgis_context,
            )
        except Exception as exc:
            self._emit_for_run(
                agent_event(
                    "error",
                    {"message": f"后台对话执行失败：{exc}"},
                    session_id=session_id,
                ),
                session_id,
                controller_run_id,
            )
            self._emit_for_run(
                agent_event("complete", {}, session_id=session_id),
                session_id,
                controller_run_id,
            )
        finally:
            with self._run_lock:
                if self._active_runs.get(session_id) == controller_run_id:
                    self._active_runs.pop(session_id, None)
                self._cancelled_runs.discard(controller_run_id)

    def _run_confirm_tool_call(
        self,
        session_id: str,
        confirmation_id: str,
        approved: bool,
    ) -> None:
        try:
            self._create_agent_core(session_id).confirm_tool_call(
                session_id=session_id,
                confirmation_id=confirmation_id,
                approved=approved,
                emit=self._emit,
            )
        except Exception as exc:
            self._emit(
                agent_event(
                    "error",
                    {"message": f"后台确认执行失败：{exc}"},
                    session_id=session_id,
                )
            )
            self._emit(agent_event("complete", {}, session_id=session_id))

    def _emit(self, event: dict[str, Any]) -> None:
        if self.bridge is not None:
            self.bridge.emit_event(event)

    def _emit_for_run(self, event: dict[str, Any], session_id: str, run_id: str) -> None:
        if not self._should_emit_for_run(session_id, run_id):
            return
        self._emit(event)

    def _should_emit_for_run(self, session_id: str, run_id: str) -> bool:
        with self._run_lock:
            return (
                self._active_runs.get(session_id) == run_id
                and run_id not in self._cancelled_runs
            )

    def _is_cancelled_run(self, run_id: str | None) -> bool:
        if not run_id:
            return False
        with self._run_lock:
            return run_id in self._cancelled_runs

    def _create_agent_core(self, cancellable_run_id: str | None = None) -> AgentCore:
        return AgentCore(
            session_db=self.session_db,
            llm_provider=create_provider(self.config),
            iface=self.iface,
            qgis_executor=self.main_thread_executor.run,
            executor_config=self.config.get("executor") or {},
            should_cancel=lambda: self._is_cancelled_run(cancellable_run_id),
        )

    def _require_session_id(self, params: dict[str, Any]) -> str:
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        if self.session_db.get_session(session_id) is None:
            raise ValueError(f"会话不存在：{session_id}")
        return session_id

    def _redact_settings(self, config: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(config)
        redacted["llm"] = dict(redacted.get("llm") or {})
        if redacted["llm"].get("api_key"):
            redacted["llm"]["api_key"] = "***"
        return redacted
