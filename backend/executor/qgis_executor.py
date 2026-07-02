"""Generated code executors for subprocess and current-QGIS modes."""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from .sandbox_policy import SandboxPolicy


class QGISCodeExecutor:
    """Run generated code in an isolated working directory."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.timeout_seconds = int(config.get("timeout_seconds") or 300)
        self.execution_mode = str(config.get("execution_mode") or "subprocess")
        self.workspace_root = Path(
            config.get("workspace_dir") or "~/.qgis_hermes_agent/workspaces"
        ).expanduser()

    def execute(
        self,
        *,
        code: str,
        expected_outputs: list[dict[str, Any]],
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        workspace_dir = self._create_workspace()
        policy = SandboxPolicy(workspace_dir)
        try:
            policy.validate_code(code)
            normalized_outputs = policy.normalize_expected_outputs(expected_outputs)
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "workspace_dir": str(workspace_dir),
                "expected_outputs": expected_outputs,
                "outputs": [],
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "duration_ms": 0,
            }
        payload_outputs = [output.as_payload() for output in normalized_outputs]
        payload = {
            "code": code,
            "workspace_dir": str(workspace_dir),
            "expected_outputs": payload_outputs,
        }

        started = time.monotonic()
        worker_path = Path(__file__).with_name("worker_entry.py")
        try:
            completed = subprocess.run(
                [sys.executable, str(worker_path)],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=int(timeout_seconds or self.timeout_seconds),
                cwd=str(workspace_dir),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return {
                "success": False,
                "error": f"代码执行超时，超过 {int(timeout_seconds or self.timeout_seconds)} 秒。",
                "workspace_dir": str(workspace_dir),
                "expected_outputs": payload_outputs,
                "outputs": [],
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "exit_code": None,
                "duration_ms": duration_ms,
            }

        duration_ms = int((time.monotonic() - started) * 1000)
        result = self._parse_worker_result(completed.stdout)
        stderr = f"{result.get('stderr') or ''}{completed.stderr or ''}"
        outputs = result.get("outputs") or []
        missing = [output for output in outputs if not output.get("exists")]
        success = completed.returncode == 0 and result.get("success") is True and not missing
        error = result.get("error")
        if missing and not error:
            names = ", ".join(output.get("path", "") for output in missing)
            error = f"代码执行结束，但缺少预期输出文件：{names}"

        return {
            "success": success,
            "error": error,
            "workspace_dir": str(workspace_dir),
            "expected_outputs": payload_outputs,
            "outputs": outputs,
            "stdout": result.get("stdout") or "",
            "stderr": stderr,
            "exit_code": result.get("exit_code", completed.returncode),
            "duration_ms": duration_ms,
        }

    def _create_workspace(self) -> Path:
        workspace_dir = self.workspace_root / time.strftime("%Y%m%d") / str(uuid.uuid4())
        workspace_dir.mkdir(parents=True, exist_ok=False)
        return workspace_dir.resolve()

    def _parse_worker_result(self, stdout: str) -> dict[str, Any]:
        try:
            return json.loads(stdout or "{}")
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": "worker 未返回有效 JSON。",
                "stdout": stdout,
                "stderr": "",
                "outputs": [],
                "exit_code": 1,
            }

    def execute_current_qgis(
        self,
        *,
        code: str,
        expected_outputs: list[dict[str, Any]],
        iface=None,
    ) -> dict[str, Any]:
        """Execute code inside the already-running QGIS Python environment."""
        workspace_dir = self._create_workspace()
        policy = SandboxPolicy(workspace_dir)
        try:
            policy.validate_code(code)
            normalized_outputs = policy.normalize_expected_outputs(expected_outputs)
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "workspace_dir": str(workspace_dir),
                "expected_outputs": expected_outputs,
                "outputs": [],
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "duration_ms": 0,
                "execution_mode": "current_qgis",
            }

        payload_outputs = [output.as_payload() for output in normalized_outputs]
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        started = time.monotonic()
        previous_workspace = os.environ.get("QGIS_AGENT_WORKSPACE")
        previous_outputs = os.environ.get("QGIS_AGENT_EXPECTED_OUTPUTS")
        try:
            os.environ["QGIS_AGENT_WORKSPACE"] = str(workspace_dir)
            os.environ["QGIS_AGENT_EXPECTED_OUTPUTS"] = json.dumps(payload_outputs, ensure_ascii=False)
            namespace = self._current_qgis_namespace(workspace_dir, iface)
            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
                compiled = compile(code, "<ai_gis_agent_current_qgis_code>", "exec")
                exec(compiled, namespace, namespace)
            outputs = self._collect_outputs(payload_outputs)
            missing = [output for output in outputs if not output.get("exists")]
            error = None
            if missing:
                names = ", ".join(output.get("path", "") for output in missing)
                error = f"代码执行结束，但缺少预期输出文件：{names}"
            return {
                "success": not missing,
                "error": error,
                "workspace_dir": str(workspace_dir),
                "expected_outputs": payload_outputs,
                "outputs": outputs,
                "stdout": stdout_buffer.getvalue(),
                "stderr": stderr_buffer.getvalue(),
                "exit_code": 0 if not missing else 1,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "execution_mode": "current_qgis",
            }
        except Exception as exc:
            stderr_buffer.write(traceback.format_exc())
            return {
                "success": False,
                "error": str(exc),
                "workspace_dir": str(workspace_dir),
                "expected_outputs": payload_outputs,
                "outputs": self._collect_outputs(payload_outputs),
                "stdout": stdout_buffer.getvalue(),
                "stderr": stderr_buffer.getvalue(),
                "exit_code": 1,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "execution_mode": "current_qgis",
            }
        finally:
            if previous_workspace is None:
                os.environ.pop("QGIS_AGENT_WORKSPACE", None)
            else:
                os.environ["QGIS_AGENT_WORKSPACE"] = previous_workspace
            if previous_outputs is None:
                os.environ.pop("QGIS_AGENT_EXPECTED_OUTPUTS", None)
            else:
                os.environ["QGIS_AGENT_EXPECTED_OUTPUTS"] = previous_outputs

    def _current_qgis_namespace(self, workspace_dir: Path, iface=None) -> dict[str, Any]:
        safe_builtins = dict(vars(builtins))
        safe_builtins["open"] = self._guarded_open(workspace_dir)
        safe_builtins.pop("eval", None)
        safe_builtins.pop("exec", None)
        safe_builtins.pop("compile", None)
        namespace = {
            "__builtins__": safe_builtins,
            "__name__": "__main__",
            "iface": iface,
            "QGIS_AGENT_WORKSPACE": str(workspace_dir),
            "Path": Path,
        }
        namespace.update(self._qgis_symbols())
        return namespace

    def _qgis_symbols(self) -> dict[str, Any]:
        symbols: dict[str, Any] = {}
        try:
            from qgis.core import (  # type: ignore[import-not-found]
                QgsCoordinateReferenceSystem,
                QgsCoordinateTransform,
                QgsFeature,
                QgsFeatureRequest,
                QgsField,
                QgsFields,
                QgsGeometry,
                QgsProcessing,
                QgsProcessingContext,
                QgsProcessingFeedback,
                QgsProject,
                QgsRasterLayer,
                QgsRectangle,
                QgsVectorFileWriter,
                QgsVectorLayer,
            )

            symbols.update(
                {
                    "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem,
                    "QgsCoordinateTransform": QgsCoordinateTransform,
                    "QgsFeature": QgsFeature,
                    "QgsFeatureRequest": QgsFeatureRequest,
                    "QgsField": QgsField,
                    "QgsFields": QgsFields,
                    "QgsGeometry": QgsGeometry,
                    "QgsProject": QgsProject,
                    "QgsProcessing": QgsProcessing,
                    "QgsProcessingContext": QgsProcessingContext,
                    "QgsProcessingFeedback": QgsProcessingFeedback,
                    "QgsRasterLayer": QgsRasterLayer,
                    "QgsRectangle": QgsRectangle,
                    "QgsVectorFileWriter": QgsVectorFileWriter,
                    "QgsVectorLayer": QgsVectorLayer,
                }
            )
        except Exception:
            pass
        try:
            import processing  # type: ignore[import-not-found]

            symbols["processing"] = processing
        except Exception:
            pass
        return symbols

    def _guarded_open(self, workspace_dir: Path):
        original_open = builtins.open

        def open_guard(file, mode="r", *args, **kwargs):
            path = Path(file).expanduser()
            if not path.is_absolute():
                path = workspace_dir / path
            resolved = path.resolve()
            writes = any(flag in str(mode) for flag in ("w", "a", "x", "+"))
            if writes and not resolved.is_relative_to(workspace_dir):
                raise PermissionError(f"禁止写入工作目录外文件：{resolved}")
            return original_open(resolved, mode, *args, **kwargs)

        return open_guard

    def _collect_outputs(self, expected_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        outputs = []
        for expected in expected_outputs:
            path = Path(str(expected.get("path") or "")).resolve()
            exists = path.exists()
            outputs.append(
                {
                    "path": str(path),
                    "name": str(expected.get("name") or path.stem),
                    "type": str(expected.get("type") or "vector"),
                    "exists": exists,
                    "size_bytes": path.stat().st_size if exists and path.is_file() else None,
                }
            )
        return outputs
