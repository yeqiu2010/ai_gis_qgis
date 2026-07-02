"""Worker process entrypoint for generated GIS code."""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any


def main() -> int:
    started = time.monotonic()
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    exit_code = 0
    error = None
    outputs: list[dict[str, Any]] = []

    try:
        payload = json.loads(sys.stdin.read() or "{}")
        workspace_dir = Path(payload["workspace_dir"]).resolve()
        expected_outputs = payload.get("expected_outputs") or []
        code = str(payload.get("code") or "")
        workspace_dir.mkdir(parents=True, exist_ok=True)
        os.environ["QGIS_AGENT_WORKSPACE"] = str(workspace_dir)
        os.environ["QGIS_AGENT_EXPECTED_OUTPUTS"] = json.dumps(expected_outputs, ensure_ascii=False)

        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            namespace = _execution_namespace(workspace_dir)
            compiled = compile(code, "<ai_gis_agent_generated_code>", "exec")
            exec(compiled, namespace, namespace)

        outputs = _collect_outputs(expected_outputs)
    except Exception as exc:  # pragma: no cover - exercised through subprocess tests.
        exit_code = 1
        error = str(exc)
        stderr_buffer.write(traceback.format_exc())

    result = {
        "success": exit_code == 0,
        "exit_code": exit_code,
        "error": error,
        "stdout": stdout_buffer.getvalue(),
        "stderr": stderr_buffer.getvalue(),
        "outputs": outputs,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


def _execution_namespace(workspace_dir: Path) -> dict[str, Any]:
    safe_builtins = dict(vars(builtins))
    safe_builtins["open"] = _guarded_open(workspace_dir)
    safe_builtins.pop("eval", None)
    safe_builtins.pop("exec", None)
    safe_builtins.pop("compile", None)
    return {
        "__builtins__": safe_builtins,
        "__name__": "__main__",
        "QGIS_AGENT_WORKSPACE": str(workspace_dir),
    }


def _guarded_open(workspace_dir: Path):
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


def _collect_outputs(expected_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


if __name__ == "__main__":  # pragma: no cover - subprocess entrypoint.
    raise SystemExit(main())
