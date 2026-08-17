"""MVP code execution safety checks."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORBIDDEN_IMPORTS = {
    "ftplib",
    "http",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "telnetlib",
    "urllib",
    "webbrowser",
}

FORBIDDEN_CALLS = {
    "QApplication",
    "QCoreApplication",
    "QgsApplication",
    "compile",
    "eval",
    "exit",
    "exec",
    "execfile",
    "input",
    "quit",
    "__import__",
}

FORBIDDEN_ATTR_CALLS = {
    ("os", "popen"),
    ("os", "remove"),
    ("os", "removedirs"),
    ("os", "rename"),
    ("os", "replace"),
    ("os", "rmdir"),
    ("os", "system"),
    ("pathlib", "unlink"),
    ("pathlib", "rmdir"),
    ("QApplication", "exec"),
    ("QApplication", "exec_"),
    ("QCoreApplication", "exec"),
    ("QCoreApplication", "exec_"),
    ("QgsApplication", "exitQgis"),
    ("QgsApplication", "initQgis"),
    ("QgsApplication", "setPrefixPath"),
    ("sys", "exit"),
}


@dataclass(frozen=True)
class ExpectedOutput:
    path: Path
    name: str
    type: str
    required: bool = True

    def as_payload(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.name,
            "type": self.type,
            "required": self.required,
        }


class SandboxPolicy:
    """Validate generated code and constrain output paths to one workspace."""

    def __init__(self, workspace_dir: Path | str):
        self.workspace_dir = Path(workspace_dir).expanduser().resolve()

    def validate_code(self, code: str) -> None:
        if not code.strip():
            raise ValueError("代码不能为空。")
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(f"代码语法错误：{exc}") from exc

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._validate_import(alias.name)
            elif isinstance(node, ast.ImportFrom):
                self._validate_import(node.module or "")
            elif isinstance(node, ast.Call):
                self._validate_call(node)

    def normalize_expected_outputs(self, values: list[dict[str, Any]]) -> list[ExpectedOutput]:
        outputs = []
        seen_paths: set[Path] = set()
        for index, raw in enumerate(values, start=1):
            raw_path = str(raw.get("path") or "").strip()
            if not raw_path:
                raise ValueError(f"第 {index} 个 expected_outputs 缺少 path。")
            output_path = self._resolve_output_path(raw_path)
            if output_path in seen_paths:
                raise ValueError(f"expected_outputs 包含重复路径：{raw_path}")
            seen_paths.add(output_path)
            output_type = str(raw.get("type") or "vector").strip().lower()
            if output_type not in {"vector", "raster", "table", "file"}:
                raise ValueError(f"不支持的输出类型：{output_type}")
            name = str(raw.get("name") or output_path.stem).strip() or output_path.stem
            required = raw.get("required", True)
            if not isinstance(required, bool):
                raise ValueError(f"第 {index} 个 expected_outputs 的 required 必须是 boolean。")
            outputs.append(
                ExpectedOutput(
                    path=output_path,
                    name=name,
                    type=output_type,
                    required=required,
                )
            )
        return outputs

    def _resolve_output_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.workspace_dir / path
        path = path.resolve()
        if not path.is_relative_to(self.workspace_dir):
            raise ValueError(f"输出文件必须位于工作目录内：{raw_path}")
        return path

    def _validate_import(self, module_name: str) -> None:
        root = module_name.split(".", 1)[0]
        if root in FORBIDDEN_IMPORTS:
            raise ValueError(f"禁止导入模块：{module_name}")

    def _validate_call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            raise ValueError(f"禁止调用：{node.func.id}")

        if isinstance(node.func, ast.Attribute):
            owner = self._attribute_owner(node.func.value)
            if owner is not None and (owner, node.func.attr) in FORBIDDEN_ATTR_CALLS:
                raise ValueError(f"禁止调用：{owner}.{node.func.attr}")
            if node.func.attr in {"unlink", "rmdir"}:
                raise ValueError(f"禁止调用：{node.func.attr}")

    def _attribute_owner(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return self._attribute_owner(node.value)
        return None
