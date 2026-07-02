"""Run PyQGIS work on the Qt main thread from background workers."""

from __future__ import annotations

import threading
from typing import Any

try:
    from qgis.PyQt.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
except Exception:  # pragma: no cover - QGIS supplies these modules at runtime.
    QObject = None
    QThread = None

    class _Signal:
        def connect(self, callback):
            self._callback = callback

        def emit(self, value):
            self._callback(value)

    def pyqtSignal(*args, **kwargs):
        return _Signal()

    def pyqtSlot(*args, **kwargs):
        def decorator(func):
            return func

        return decorator


class MainThreadExecutor(QObject if QObject is not None else object):
    """Synchronously execute callables on the object's Qt thread."""

    taskRequested = pyqtSignal(object)

    def __init__(self, parent=None):
        if QObject is not None:
            super().__init__(parent)
        else:
            super().__init__()
        self.taskRequested.connect(self._run_task)

    def run(self, func):
        if self._is_current_thread():
            return func()

        done = threading.Event()
        task: dict[str, Any] = {"func": func, "done": done}
        self.taskRequested.emit(task)
        done.wait()
        if "error" in task:
            raise task["error"]
        return task.get("result")

    @pyqtSlot(object)
    def _run_task(self, task: dict[str, Any]) -> None:
        try:
            task["result"] = task["func"]()
        except Exception as exc:
            task["error"] = exc
        finally:
            task["done"].set()

    def _is_current_thread(self) -> bool:
        if QObject is None or QThread is None:
            return True
        return QThread.currentThread() == self.thread()
