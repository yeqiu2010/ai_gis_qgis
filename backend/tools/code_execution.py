"""Generated GIS code execution tool."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path, PureWindowsPath
from typing import Any

from ...database.session_db import SessionDB
from ..executor.qgis_executor import QGISCodeExecutor
from .layer_ops import _infer_layer_type, _layer_summary, _qgis_classes, _run_qgis, _snapshot
from .registry import ToolEntry

VECTOR_OUTPUT_EXTENSIONS = {".shp", ".gpkg", ".geojson", ".kml"}
RASTER_OUTPUT_EXTENSIONS = {".tif", ".tiff"}
TABLE_OUTPUT_EXTENSIONS = {".csv", ".xlsx", ".dbf"}
FILE_OUTPUT_EXTENSIONS = {".txt", ".json", ".html", ".md"}
OUTPUT_HINTS = ("output", "result", "save", "export", "write", "输出", "结果")
SHAPEFILE_SIDECAR_EXTENSIONS = {
    ".shp",
    ".shx",
    ".dbf",
    ".prj",
    ".cpg",
    ".qpj",
    ".sbn",
    ".sbx",
    ".fix",
}


def build_execute_gis_code_tool(
    *,
    session_db: SessionDB | None,
    session_id: str,
    iface=None,
    qgis_executor=None,
    executor_config: dict[str, Any] | None = None,
) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        code = str(arguments.get("code") or "")
        expected_outputs = arguments.get("expected_outputs") or []
        delivery_outputs = arguments.get("delivery_outputs") or []
        expected_outputs_inferred = False
        if not expected_outputs:
            expected_outputs = infer_expected_outputs_from_code(code)
            expected_outputs_inferred = bool(expected_outputs)
        expected_outputs, delivery_outputs = _prepare_delivery_outputs(
            expected_outputs,
            delivery_outputs,
        )
        timeout_seconds = arguments.get("timeout_seconds")

        executor = QGISCodeExecutor(executor_config)
        use_current_qgis = qgis_executor is not None and executor.execution_mode == "current_qgis"
        if use_current_qgis:
            result = qgis_executor(
                lambda: executor.execute_current_qgis(
                    code=code,
                    expected_outputs=expected_outputs,
                    iface=iface,
                )
            )
        else:
            result = executor.execute(
                code=code,
                expected_outputs=expected_outputs,
                timeout_seconds=int(timeout_seconds) if timeout_seconds else None,
            )

        loaded_layers: list[dict[str, Any]] = []
        load_errors: list[str] = []
        if result.get("success"):
            try:
                loaded_layers = _load_output_layers(
                    result.get("outputs") or [],
                    session_db=session_db,
                    session_id=session_id,
                    qgis_executor=qgis_executor,
                )
            except Exception as exc:
                load_errors.append(str(exc))

        delivered_outputs: list[dict[str, Any]] = []
        delivery_errors: list[str] = []
        if result.get("success") and delivery_outputs:
            delivered_outputs, delivery_errors = _deliver_outputs(
                result.get("outputs") or [],
                delivery_outputs,
            )

        success = bool(result.get("success")) and not load_errors and not delivery_errors
        error = result.get("error")
        if load_errors and not error:
            error = "输出文件已生成，但加载到 QGIS 失败：" + "；".join(load_errors)
        if delivery_errors and not error:
            error = "输出文件已生成，但导出到指定目录失败：" + "；".join(delivery_errors)
        if not success and not error and result.get("stderr"):
            error = str(result["stderr"]).strip().splitlines()[-1]

        final_result = {
            **result,
            "success": success,
            "error": error,
            "loaded_layers": loaded_layers,
            "load_errors": load_errors,
            "delivered_outputs": delivered_outputs,
            "delivery_errors": delivery_errors,
            "expected_outputs_inferred": expected_outputs_inferred,
        }
        if session_db is not None:
            session_db.log_code_execution(
                session_id,
                code=code,
                workspace_dir=str(final_result.get("workspace_dir") or ""),
                expected_outputs=final_result.get("expected_outputs") or [],
                outputs=final_result.get("outputs") or [],
                stdout=str(final_result.get("stdout") or ""),
                stderr=str(final_result.get("stderr") or ""),
                exit_code=final_result.get("exit_code"),
                duration_ms=int(final_result.get("duration_ms") or 0),
                success=success,
                error_message=error,
            )
        return final_result

    return ToolEntry(
        name="execute_gis_code",
        description=(
            "在当前已打开的 QGIS Python 环境中执行生成的 GIS 代码。"
            "代码只能通过文件产出结果，工具会加载 vector/raster 输出到当前 QGIS 工程。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "要执行的 Python 代码。必须把输出写入 QGIS_AGENT_WORKSPACE，"
                        "不得访问网络，不得调用 subprocess/os.system/eval/exec，"
                        "不得创建 QgsApplication/QApplication 或初始化新的 QGIS。"
                    ),
                },
                "expected_outputs": {
                    "type": "array",
                    "description": (
                        "预期输出文件。path 必须是相对路径或工作目录内路径。"
                        "如果用户要求导出到外部目录，请代码仍输出到工作目录，并使用 delivery_outputs 指定外部目标。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "输出文件路径。建议使用相对路径。"},
                            "name": {"type": "string", "description": "加载到 QGIS 后的图层名称。"},
                            "type": {
                                "type": "string",
                                "enum": ["vector", "raster", "table", "file"],
                                "description": "输出类型。vector/raster 会自动加载到 QGIS。",
                            },
                        },
                        "required": ["path", "name", "type"],
                        "additionalProperties": False,
                    },
                    "minItems": 1,
                },
                "delivery_outputs": {
                    "type": "array",
                    "description": "可选。执行成功后复制到用户指定外部路径的交付目标。source_path 对应 expected_outputs.path。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_path": {"type": "string", "description": "工作目录内输出文件名，例如 qn_500_area_8000.geojson。"},
                            "target_path": {"type": "string", "description": "用户指定外部完整路径，例如 E:\\Desktop\\test\\qn_500_area_8000.geojson。"},
                        },
                        "required": ["source_path", "target_path"],
                        "additionalProperties": False,
                    },
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "执行超时秒数，默认使用插件设置。",
                    "minimum": 1,
                    "maximum": 3600,
                },
            },
            "required": ["code", "expected_outputs"],
            "additionalProperties": False,
        },
        handler=handler,
        category="execution",
        requires_confirmation=True,
        writes_project=True,
    )


def infer_expected_outputs_from_code(code: str) -> list[dict[str, str]]:
    """Infer expected outputs from obvious generated-code output path literals."""
    if not code.strip():
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    lines = code.splitlines()
    outputs: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        path = _candidate_output_path(node.value)
        if not path:
            continue
        line = lines[node.lineno - 1].lower() if getattr(node, "lineno", 0) else ""
        if not any(hint in line for hint in OUTPUT_HINTS):
            continue
        if path in seen:
            continue
        seen.add(path)
        output_path = Path(path)
        outputs.append(
            {
                "path": path,
                "name": output_path.stem,
                "type": _infer_expected_output_type(output_path.suffix.lower()),
            }
        )
    return outputs


def _prepare_delivery_outputs(
    expected_outputs: list[dict[str, Any]],
    delivery_outputs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prepared_outputs = []
    prepared_delivery = list(delivery_outputs) if isinstance(delivery_outputs, list) else []
    for output in expected_outputs:
        if not isinstance(output, dict):
            continue
        prepared = dict(output)
        raw_path = str(prepared.get("path") or "").strip()
        if _is_external_output_path(raw_path):
            filename = _output_filename(raw_path)
            prepared["path"] = filename
            prepared["name"] = str(prepared.get("name") or Path(filename).stem)
            prepared_delivery.append(
                {
                    "source_path": filename,
                    "target_path": raw_path,
                }
            )
        prepared_outputs.append(prepared)
    return prepared_outputs, prepared_delivery


def _is_external_output_path(raw_path: str) -> bool:
    if not raw_path:
        return False
    if PureWindowsPath(raw_path).drive:
        return True
    return Path(raw_path).expanduser().is_absolute()


def _output_filename(raw_path: str) -> str:
    windows_path = PureWindowsPath(raw_path)
    filename = windows_path.name if windows_path.drive else Path(raw_path).name
    return _fix_common_extension_typo(filename)


def _fix_common_extension_typo(filename: str) -> str:
    lower = filename.lower()
    for extension in (
        "geojson",
        "json",
        "gpkg",
        "shp",
        "tif",
        "tiff",
        "csv",
        "xlsx",
    ):
        suffix = f",{extension}"
        if lower.endswith(suffix):
            return f"{filename[:-len(suffix)]}.{extension}"
    return filename


def _deliver_outputs(
    outputs: list[dict[str, Any]],
    delivery_outputs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    output_by_name = {Path(str(output.get("path") or "")).name: output for output in outputs}
    delivered = []
    errors = []
    for delivery in delivery_outputs:
        source_name = _output_filename(str(delivery.get("source_path") or ""))
        target_path = Path(str(delivery.get("target_path") or "")).expanduser()
        output = output_by_name.get(source_name)
        if not output:
            errors.append(f"找不到待交付输出：{source_name}")
            continue
        source_path = Path(str(output.get("path") or ""))
        if not source_path.exists():
            errors.append(f"待交付输出不存在：{source_path}")
            continue
        try:
            copied = _copy_output_bundle(source_path, target_path)
        except Exception as exc:
            errors.append(f"{target_path}: {exc}")
            continue
        delivered.append(
            {
                "source_path": str(source_path),
                "target_path": str(target_path),
                "files": [str(path) for path in copied],
            }
        )
    return delivered, errors


def _copy_output_bundle(source_path: Path, target_path: Path) -> list[Path]:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    copied = []
    if source_path.suffix.lower() == ".shp":
        for sidecar in source_path.parent.glob(f"{source_path.stem}.*"):
            if sidecar.suffix.lower() not in SHAPEFILE_SIDECAR_EXTENSIONS:
                continue
            target = target_path.with_suffix(sidecar.suffix)
            shutil.copy2(sidecar, target)
            copied.append(target)
        return copied
    shutil.copy2(source_path, target_path)
    return [target_path]


def _candidate_output_path(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    path = Path(value)
    suffix = path.suffix.lower()
    allowed_suffixes = (
        VECTOR_OUTPUT_EXTENSIONS
        | RASTER_OUTPUT_EXTENSIONS
        | TABLE_OUTPUT_EXTENSIONS
        | FILE_OUTPUT_EXTENSIONS
    )
    if suffix not in allowed_suffixes:
        return None
    if path.is_absolute():
        return path.name
    return value.replace("\\", "/")


def _infer_expected_output_type(suffix: str) -> str:
    if suffix in VECTOR_OUTPUT_EXTENSIONS:
        return "vector"
    if suffix in RASTER_OUTPUT_EXTENSIONS:
        return "raster"
    if suffix in TABLE_OUTPUT_EXTENSIONS:
        return "table"
    return "file"


def _load_output_layers(
    outputs: list[dict[str, Any]],
    *,
    session_db: SessionDB | None,
    session_id: str,
    qgis_executor=None,
) -> list[dict[str, Any]]:
    loadable_outputs = [
        output for output in outputs if str(output.get("type") or "").lower() in {"vector", "raster"}
    ]
    if not loadable_outputs:
        return []

    def operation() -> list[dict[str, Any]]:
        classes = _qgis_classes()
        project = classes["QgsProject"].instance()
        loaded = []
        for output in loadable_outputs:
            output_type = str(output.get("type") or "").lower()
            path = str(output.get("path") or "")
            if not path or not Path(path).exists():
                raise ValueError(f"输出文件不存在，无法加载：{path}")
            name = str(output.get("name") or Path(path).stem)
            layer_type = _infer_layer_type(path, output_type)
            if layer_type == "raster":
                layer = classes["QgsRasterLayer"](path, name)
            else:
                layer = classes["QgsVectorLayer"](path, name, "ogr")
            if not layer.isValid():
                raise ValueError(f"输出图层无效，无法加载：{path}")
            project.addMapLayer(layer)
            _snapshot(session_db, session_id, layer, "generated")
            loaded.append(_layer_summary(layer))
        return loaded

    return _run_qgis(qgis_executor, operation)
