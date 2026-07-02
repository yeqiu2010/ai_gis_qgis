"""QWebEngine view that loads the built frontend."""

from __future__ import annotations

import site
import sys
import sysconfig
from pathlib import Path

try:
    from qgis.core import Qgis, QgsMessageLog
    from qgis.PyQt.QtCore import QTimer, QUrl, pyqtSignal, pyqtSlot
    from qgis.PyQt.QtGui import QTextCursor
    from qgis.PyQt.QtWidgets import (
        QApplication,
        QHBoxLayout,
        QLabel,
        QMessageBox,
        QPushButton,
        QTextBrowser,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except Exception:  # pragma: no cover - QGIS supplies these modules at runtime.
    QTimer = None
    QTextCursor = None
    QUrl = None
    pyqtSignal = None
    pyqtSlot = None
    QHBoxLayout = None
    QLabel = None
    QMessageBox = None
    QApplication = None
    QPushButton = None
    QTextBrowser = None
    QTextEdit = None
    QVBoxLayout = None
    QWidget = None
    Qgis = None
    QgsMessageLog = None

from .rpc_controller import RPCController
from .webchannel_bridge import QWebChannelBridge

LOG_TAG = "AI GIS Agent"


def _extend_python_paths() -> list[str]:
    """Add common pip install locations that QGIS may omit from sys.path."""
    candidates = []
    for getter in (site.getusersitepackages, site.getsitepackages):
        try:
            paths = getter()
        except Exception:  # pragma: no cover - runtime/environment dependent.
            continue
        if isinstance(paths, str):
            candidates.append(paths)
        else:
            candidates.extend(paths)

    for path_name in ("purelib", "platlib"):
        path = sysconfig.get_paths().get(path_name)
        if path:
            candidates.append(path)

    added = []
    for candidate in candidates:
        if candidate and candidate not in sys.path and Path(candidate).exists():
            sys.path.append(candidate)
            added.append(candidate)
    return added


PYTHON_PATHS_ADDED = _extend_python_paths()


def _import_first(candidates: tuple[tuple[str, str], ...]):
    """Import the first available symbol from QGIS/PyQt bindings."""
    errors = []
    for module_name, symbol_name in candidates:
        try:
            module = __import__(module_name, fromlist=[symbol_name])
            return getattr(module, symbol_name), module_name
        except Exception as exc:  # pragma: no cover - depends on the QGIS runtime.
            errors.append(f"{module_name}.{symbol_name}: {exc}")
    return None, "; ".join(errors)


def _shorten_diagnostics(value: str, *, limit: int = 450) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def log_message(message: str, level=None) -> None:
    if QgsMessageLog is not None:
        default_level = Qgis.Info if Qgis is not None else None
        QgsMessageLog.logMessage(message, LOG_TAG, level or default_level)


QWebEngineView, QWEBENGINE_VIEW_SOURCE = _import_first(
    (
        ("qgis.PyQt.QtWebEngineWidgets", "QWebEngineView"),
        ("PyQt6.QtWebEngineWidgets", "QWebEngineView"),
    )
)

QWebChannel, QWEBCHANNEL_SOURCE = _import_first(
    (
        ("qgis.PyQt.QtWebChannel", "QWebChannel"),
        ("PyQt6.QtWebChannel", "QWebChannel"),
    )
)

QWebEnginePage, QWEBENGINE_PAGE_SOURCE = _import_first(
    (
        ("qgis.PyQt.QtWebEngineCore", "QWebEnginePage"),
        ("qgis.PyQt.QtWebEngineWidgets", "QWebEnginePage"),
        ("PyQt6.QtWebEngineCore", "QWebEnginePage"),
    )
)

QWebEngineSettings, QWEBENGINE_SETTINGS_SOURCE = _import_first(
    (
        ("qgis.PyQt.QtWebEngineCore", "QWebEngineSettings"),
        ("qgis.PyQt.QtWebEngineWidgets", "QWebEngineSettings"),
        ("PyQt6.QtWebEngineCore", "QWebEngineSettings"),
    )
)


if QWebEnginePage is not None:

    class HermesWebEnginePage(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line_number, source_id):
            log_message(f"Frontend console: {message} ({source_id}:{line_number})")


if QWebEngineView is not None:

    class HermesWebEngineView(QWebEngineView):
        """Loads ``resources/frontend_dist/index.html`` and registers the bridge."""

        def __init__(self, plugin_dir: Path, parent=None, iface=None):
            super().__init__(parent)
            self.plugin_dir = Path(plugin_dir)
            self.controller = RPCController(iface=iface, plugin_dir=self.plugin_dir)
            self.bridge = QWebChannelBridge(handler=self.controller.handle, parent=self)
            self.controller.bind_bridge(self.bridge)

            if QWebEnginePage is not None:
                self.setPage(HermesWebEnginePage(self))
            self._configure_settings()
            self.loadFinished.connect(self._on_load_finished)

            if QWebChannel is not None:
                self.channel = QWebChannel(self.page())
                self.channel.registerObject("bridge", self.bridge)
                self.page().setWebChannel(self.channel)
            else:
                log_message("QWebChannel is not available; frontend bridge will not connect.")

            log_message(
                "Using WebEngine bindings: "
                f"view={QWEBENGINE_VIEW_SOURCE}, page={QWEBENGINE_PAGE_SOURCE}, "
                f"settings={QWEBENGINE_SETTINGS_SOURCE}, channel={QWEBCHANNEL_SOURCE}"
            )
            self.load_frontend()

        def load_frontend(self):
            index_file = self.plugin_dir / "resources" / "frontend_dist" / "index.html"
            if QUrl is not None and index_file.exists():
                html = index_file.read_text(encoding="utf-8")
                base_url = QUrl.fromLocalFile(str(index_file.parent) + "/")
                self.setHtml(html, base_url)
            else:
                self.setHtml(
                    "<h1>AI GIS Agent frontend has not been built.</h1>"
                    "<p>Missing resources/frontend_dist/index.html.</p>"
                )

        def _configure_settings(self):
            if QWebEngineSettings is None:
                return
            settings = self.settings()
            for attribute_name in (
                "LocalContentCanAccessFileUrls",
                "LocalContentCanAccessRemoteUrls",
                "JavascriptEnabled",
            ):
                attribute = self._web_attribute(attribute_name)
                if attribute is not None:
                    settings.setAttribute(attribute, True)

        def _web_attribute(self, name: str):
            if hasattr(QWebEngineSettings, name):
                return getattr(QWebEngineSettings, name)
            web_attribute = getattr(QWebEngineSettings, "WebAttribute", None)
            if web_attribute is not None and hasattr(web_attribute, name):
                return getattr(web_attribute, name)
            return None

        def _on_load_finished(self, ok: bool):
            if ok:
                log_message("Frontend loaded successfully.")
                return
            level = Qgis.Critical if Qgis is not None else None
            log_message("Frontend failed to load. Check resources/frontend_dist.", level)

else:

    class HermesWebEngineView(QWidget if QWidget is not None else object):
        """Native Qt chat panel for QGIS builds without QtWebEngine."""

        if pyqtSignal is not None:
            agentEventReceived = pyqtSignal(object)

        def __init__(self, plugin_dir: Path, parent=None, iface=None):
            if QWidget is not None:
                super().__init__(parent)
                log_message(
                    "QtWebEngine is not available; falling back to native Qt panel. "
                    f"Import errors: {QWEBENGINE_VIEW_SOURCE}. "
                    f"Additional Python paths: {PYTHON_PATHS_ADDED or 'none'}"
                )
                self.plugin_dir = Path(plugin_dir)
                self.controller = RPCController(iface=iface, plugin_dir=self.plugin_dir, bridge=self)
                self.session_id: str | None = None
                self._history_entries: list[dict[str, str]] = []
                self._streaming_run_id: str | None = None
                self._streaming_index: int | None = None
                self._process_run_id: str | None = None
                self._process_index: int | None = None
                if pyqtSignal is not None:
                    self.agentEventReceived.connect(self._handle_agent_event)
                self._setup_ui()
                self._bootstrap_session()
            else:
                super().__init__()

        def _setup_ui(self):
            layout = QVBoxLayout(self)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.setSpacing(8)

            self.title = QLabel("AI GIS Agent")
            self.title.setStyleSheet("font-weight: 700; font-size: 15px;")
            self.status = QLabel(
                "原生 Qt 面板模式：WebEngine 导入失败，"
                f"{_shorten_diagnostics(QWEBENGINE_VIEW_SOURCE)}"
            )
            self.status.setWordWrap(True)

            self.history = QTextBrowser(self)
            self.history.setOpenExternalLinks(True)

            self.input = QTextEdit(self)
            self.input.setPlaceholderText("输入 GIS 任务...")
            self.input.setFixedHeight(72)

            button_row = QHBoxLayout()
            self.new_button = QPushButton("新建会话", self)
            self.send_button = QPushButton("发送", self)
            button_row.addWidget(self.new_button)
            button_row.addStretch(1)
            button_row.addWidget(self.send_button)

            layout.addWidget(self.title)
            layout.addWidget(self.status)
            layout.addWidget(self.history, 1)
            layout.addWidget(self.input)
            layout.addLayout(button_row)

            self.new_button.clicked.connect(self._create_session)
            self.send_button.clicked.connect(self._send_message)

        def _bootstrap_session(self):
            try:
                sessions = self.controller.list_sessions({"limit": 20})
                if sessions:
                    self.session_id = sessions[0]["id"]
                    self._load_messages()
                else:
                    self._create_session()
            except Exception as exc:
                self._append_system(f"初始化会话失败：{exc}")

        def _create_session(self):
            try:
                session = self.controller.create_session({"title": "QGIS 对话"})
                self.session_id = session["id"]
                self.history.clear()
                self._history_entries = []
                self._streaming_run_id = None
                self._streaming_index = None
                self._process_run_id = None
                self._process_index = None
                self._append_system("新会话已创建。")
            except Exception as exc:
                self._append_system(f"创建会话失败：{exc}")

        def _load_messages(self):
            if not self.session_id:
                return
            self._history_entries = []
            self._streaming_run_id = None
            self._streaming_index = None
            self._process_run_id = None
            self._process_index = None
            for message in self.controller.get_messages({"session_id": self.session_id, "limit": 100}):
                entry = self._normalize_history_entry(message)
                if entry["role"] == "system" and entry["event_type"] == "process":
                    self._append_system(entry["content"])
                else:
                    self._append_message(entry["role"], entry["content"], entry["event_type"])

        def _send_message(self):
            text = self.input.toPlainText().strip()
            if not text:
                return
            if not self.session_id:
                self._create_session()
            if not self.session_id:
                return

            self.input.setPlainText("")
            self.send_button.setEnabled(False)
            self._process_run_id = None
            self._process_index = None
            self._append_message("user", text, "user")
            self._flush_ui_events()
            try:
                self.controller.chat({"session_id": self.session_id, "message": text})
            except Exception as exc:
                self._append_system(f"发送失败：{exc}")
                self.send_button.setEnabled(True)

        def emit_event(self, event: dict):
            if pyqtSignal is not None:
                self.agentEventReceived.emit(event)
                return
            self._handle_agent_event(event)

        if pyqtSlot is not None:

            @pyqtSlot(object)
            def _handle_agent_event(self, event: dict):
                self._dispatch_agent_event(event)

        else:

            def _handle_agent_event(self, event: dict):
                self._dispatch_agent_event(event)

        def _dispatch_agent_event(self, event: dict):
            event_type = event.get("type")
            payload = event.get("payload") or {}
            run_id = str(event.get("run_id") or "")
            if event_type == "run_start":
                self._process_run_id = run_id
                self._process_index = None
                self.status.setText(f"原生 Qt 面板模式 · {event_type}")
            elif event_type == "message":
                content = payload.get("content")
                if content:
                    self._finalize_stream(event.get("run_id"), str(content))
            elif event_type == "message_delta":
                delta = payload.get("delta")
                if isinstance(delta, str):
                    self._append_delta(event.get("run_id"), delta)
            elif event_type == "thinking":
                self._append_system(str(payload.get("message") or "正在处理请求..."), run_id)
            elif event_type == "tool_start":
                self._append_system(f"调用工具：{payload.get('name') or ''}", run_id)
            elif event_type == "tool_end":
                result = payload.get("result") or {}
                self._append_system(
                    self._format_tool_end_message(str(payload.get("name") or ""), result),
                    run_id,
                )
            elif event_type == "confirm_request":
                self._handle_confirm_request(payload, run_id)
            elif event_type == "confirm_resolved":
                self._append_system("已确认工具操作。" if payload.get("approved") else "已取消工具操作。", run_id)
            elif event_type == "stage_start":
                self._append_system(f"Pipeline 阶段开始：{payload.get('stage_name') or ''}", run_id)
            elif event_type == "stage_end":
                summary = payload.get("summary") or (payload.get("artifact") or {}).get("summary") or ""
                state = "失败" if payload.get("success") is False else "完成"
                self._append_system(f"Pipeline 阶段{state}：{payload.get('stage_name') or ''}\n{summary}", run_id)
            elif event_type == "code_generated":
                self._append_system("已生成待执行代码，等待确认或继续执行。", run_id)
            elif event_type == "error":
                self._append_system(str(payload.get("message") or "运行失败"), run_id)
                self.send_button.setEnabled(True)
            elif event_type == "complete":
                self.status.setText(f"原生 Qt 面板模式 · {event_type}")
                self.send_button.setEnabled(True)

        def _handle_confirm_request(self, payload: dict, run_id: str = ""):
            confirmation_id = str(payload.get("confirmation_id") or "")
            tool_name = str(payload.get("tool_name") or "")
            arguments = payload.get("arguments") or {}
            self._append_system(f"工具 {tool_name} 需要确认：{arguments}", run_id)
            if QMessageBox is None or not self.session_id or not confirmation_id:
                return
            answer = QMessageBox.question(
                self,
                "确认工具操作",
                f"是否执行工具 {tool_name}？\n\n参数：{arguments}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            approved = answer == QMessageBox.StandardButton.Yes
            try:
                self.controller.confirm_tool_call(
                    {
                        "session_id": self.session_id,
                        "confirmation_id": confirmation_id,
                        "approved": approved,
                    }
                )
            except Exception as exc:
                self._append_system(f"确认处理失败：{exc}", run_id)

        def _append_message(self, role: str, content: str, event_type: str = "message"):
            self._history_entries.append({"role": role, "content": content, "event_type": event_type})
            if role != "system" and event_type != "process":
                self._process_index = None
            self._render_history()

        def _normalize_history_entry(self, message: dict) -> dict[str, str]:
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            event_type = str(message.get("event_type") or "")
            if role == "user":
                return {"role": "user", "content": content, "event_type": "user"}
            if role == "system" or event_type in {"process", "stage_artifact"}:
                return {"role": "system", "content": content, "event_type": "process"}
            if role == "assistant":
                return {
                    "role": "assistant",
                    "content": content,
                    "event_type": "error" if event_type == "error" else "summary",
                }
            return {"role": role, "content": content, "event_type": event_type or "message"}

        def _format_tool_end_message(self, name: str, result: dict):
            prefix = "工具失败" if isinstance(result, dict) and result.get("success") is False else "工具完成"
            lines = [f"{prefix}：{name}"]
            if name == "execute_gis_code" and isinstance(result, dict):
                if result.get("workspace_dir"):
                    lines.append(f"工作目录：{result['workspace_dir']}")
                if result.get("error"):
                    lines.append(f"error：{result['error']}")
                if result.get("stderr"):
                    lines.append(f"stderr：{str(result['stderr'])[-1000:]}")
                if result.get("stdout"):
                    lines.append(f"stdout：{str(result['stdout'])[-500:]}")
            return "\n".join(lines)

        def _append_system(self, content: str, run_id: str = ""):
            normalized_run_id = run_id or self._process_run_id or ""
            if (
                self._process_run_id == normalized_run_id
                and self._process_index is not None
                and self._process_index == len(self._history_entries) - 1
                and self._process_index < len(self._history_entries)
                and self._history_entries[self._process_index].get("role") == "system"
                and self._history_entries[self._process_index].get("event_type") == "process"
            ):
                entry = self._history_entries[self._process_index]
                entry["content"] = f"{entry.get('content', '')}\n{content}"
            else:
                self._history_entries.append({"role": "system", "content": content, "event_type": "process"})
                self._process_run_id = normalized_run_id
                self._process_index = len(self._history_entries) - 1
            self._render_history()

        def _append_delta(self, run_id, delta: str):
            if self._streaming_run_id != run_id or self._streaming_index is None:
                self._streaming_run_id = str(run_id or "")
                self._history_entries.append(
                    {"role": "assistant", "content": "", "event_type": "streaming"}
                )
                self._streaming_index = len(self._history_entries) - 1
            entry = self._history_entries[self._streaming_index]
            entry["content"] = f"{entry.get('content', '')}{delta}"
            self._render_history()

        def _finalize_stream(self, run_id, content: str):
            if self._streaming_run_id == str(run_id or "") and self._streaming_index is not None:
                self._history_entries[self._streaming_index] = {
                    "role": "assistant",
                    "content": content,
                    "event_type": "summary",
                }
            else:
                self._history_entries.append(
                    {"role": "assistant", "content": content, "event_type": "summary"}
                )
            self._streaming_run_id = None
            self._streaming_index = None
            self._render_history()

        def _render_history(self):
            rows = []
            for entry in self._history_entries:
                role = entry.get("role", "")
                event_type = entry.get("event_type", "")
                content = self._escape(entry.get("content", "")).replace(chr(10), "<br>")
                if role == "user":
                    rows.append(
                        "<p style='background:#e6f3f2;border:1px solid #b8dcda;"
                        "padding:8px;border-radius:6px;'><b style='color:#0b6e69'>你</b><br>"
                        f"{content}</p>"
                    )
                elif role == "system" or event_type == "process":
                    rows.append(
                        "<p style='background:#eef2f4;color:#5e6b74;border:1px solid #d7dee2;"
                        "padding:7px;border-radius:6px;font-size:12px;'><b>过程</b><br>"
                        f"{content}</p>"
                    )
                elif event_type == "streaming":
                    rows.append(
                        "<p style='background:#f7fbfc;border:1px solid #b9d5df;"
                        "padding:8px;border-radius:6px;'><b style='color:#2e6f89'>Agent（生成中）</b><br>"
                        f"{content}</p>"
                    )
                else:
                    rows.append(
                        "<p style='border-left:4px solid #0b6e69;background:#ffffff;"
                        "padding:8px;border-radius:4px;'><b style='color:#0b6e69'>Agent</b><br>"
                        f"<span style='color:#172026'>{content}</span></p>"
                    )
            self.history.setHtml("".join(rows))
            self._scroll_history_to_bottom()
            if QTimer is not None:
                QTimer.singleShot(0, self._scroll_history_to_bottom)
                QTimer.singleShot(50, self._scroll_history_to_bottom)
                QTimer.singleShot(150, self._scroll_history_to_bottom)

        def _scroll_history_to_bottom(self):
            if QTextCursor is not None:
                move_operation = getattr(getattr(QTextCursor, "MoveOperation", None), "End", None)
                if move_operation is None:
                    move_operation = getattr(QTextCursor, "End", None)
                if move_operation is not None:
                    self.history.moveCursor(move_operation)
                    self.history.ensureCursorVisible()
            scrollbar = self.history.verticalScrollBar()
            if scrollbar is not None:
                scrollbar.setValue(scrollbar.maximum())

        def _escape(self, value: str) -> str:
            return (
                value.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
            )

        def _flush_ui_events(self):
            if QApplication is not None:
                QApplication.processEvents()
