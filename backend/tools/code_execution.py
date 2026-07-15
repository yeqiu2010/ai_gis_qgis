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
        preflight_error = validate_execute_gis_code_arguments(
            {"code": code, "expected_outputs": expected_outputs}
        )
        if preflight_error:
            return preflight_error
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
            "每个用户任务只用于生成最终文件，不得用于字段唯一值探查或仅打印诊断信息。"
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


def find_unwritten_expected_outputs(
    code: str,
    expected_outputs: list[dict[str, Any]],
) -> list[str]:
    """Return declared outputs that are not connected to an obvious write sink."""
    if not code.strip() or not expected_outputs:
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # Compilation reports the more useful syntax error later.

    assignments: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            values = _path_names_from_expression(node.value, assignments)
            for target in node.targets:
                if isinstance(target, ast.Name) and values:
                    assignments[target.id] = values

    written: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = _call_name(node.func)
        if func_name == "open" and node.args:
            mode = _literal_string(node.args[1]) if len(node.args) > 1 else "r"
            if any(flag in (mode or "") for flag in "wax+"):
                written.update(_path_names_from_expression(node.args[0], assignments))
        elif func_name.endswith((".write_text", ".write_bytes")) and isinstance(
            node.func, ast.Attribute
        ):
            written.update(_path_names_from_expression(node.func.value, assignments))
        elif func_name.endswith("processing.run") or func_name == "processing.run":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Dict):
                for key, value in zip(node.args[1].keys, node.args[1].values, strict=True):
                    if _literal_string(key) in {"OUTPUT", "OUTPUT_LAYER", "OUTPUT_TABLE"}:
                        written.update(_path_names_from_expression(value, assignments))
        elif func_name.endswith(("writeAsVectorFormatV3", "writeAsVectorFormatV2")):
            if len(node.args) >= 2:
                written.update(_path_names_from_expression(node.args[1], assignments))

    missing = []
    for output in expected_outputs:
        filename = _output_filename(str(output.get("path") or ""))
        if filename and filename not in written:
            missing.append(filename)
    return missing


def validate_execute_gis_code_arguments(arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the code/output contract before asking for execution approval."""
    code = str(arguments.get("code") or "")
    expected_outputs = arguments.get("expected_outputs") or []
    if not isinstance(expected_outputs, list):
        return None
    unwritten_outputs = find_unwritten_expected_outputs(code, expected_outputs)
    issues = find_generated_code_issues(code)
    if unwritten_outputs:
        issues.insert(
            0,
            "以下文件已声明但代码没有写入：" + "、".join(unwritten_outputs),
        )
    if not issues:
        return None
    return {
        "success": False,
        "error": (
            "生成代码未通过执行前检查："
            + "；".join(issues)
            + "。请重新生成面向最终结果的代码，不要用 execute_gis_code 做诊断探查。"
        ),
        "expected_outputs": expected_outputs,
        "outputs": [],
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        "duration_ms": 0,
        "preflight_failed": True,
    }


def find_generated_code_issues(code: str) -> list[str]:
    """Detect recurrent PyQGIS mistakes observed in execution failure logs."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        location = f"第 {exc.lineno} 行" if exc.lineno else "未知行"
        return [f"生成代码存在 SyntaxError（{location}）：{exc.msg}"]

    issues: list[str] = []
    imported_roots: set[str] = set()
    file_processing_result_names: set[str] = set()
    output_path_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
            if any(alias.name in {"PyQt5", "PyQt6"} or alias.name.startswith(("PyQt5.", "PyQt6.")) for alias in node.names):
                _append_issue(issues, "禁止直接导入 PyQt5/PyQt6，必须使用 qgis.PyQt")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "os":
                imported_roots.add("os")
            if module in {"PyQt5", "PyQt6"} or module.startswith(("PyQt5.", "PyQt6.")):
                _append_issue(issues, "禁止直接导入 PyQt5/PyQt6，必须使用 qgis.PyQt")
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    for node in assignments:
        if not _processing_call_writes_file(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                file_processing_result_names.add(target.id)

    # Propagate paths read from file-backed Processing results through simple
    # aliases.  In-memory/TEMPORARY_OUTPUT results are QgsMapLayer objects and
    # must not be classified as paths.
    changed = True
    while changed:
        changed = False
        for node in assignments:
            value_is_output_path = _is_processing_output_path_expression(
                node.value,
                output_path_names,
                file_processing_result_names,
            )
            if not value_is_output_path:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in output_path_names:
                    output_path_names.add(target.id)
                    changed = True

    uses_os = any(
        isinstance(node, ast.Name) and node.id == "os" and isinstance(getattr(node, "ctx", None), ast.Load)
        for node in ast.walk(tree)
    )
    if uses_os and "os" not in imported_roots:
        _append_issue(issues, "使用了 os 但没有 import os；输出路径应优先使用 Path(QGIS_AGENT_WORKSPACE)")

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "QgsProcessingContext"
                and node.attr == "InvalidGeometryCheck"
            ):
                _append_issue(
                    issues,
                    "InvalidGeometryCheck 枚举属于 Qgis，不属于 QgsProcessingContext；"
                    "应使用 Qgis.InvalidGeometryCheck.GeometrySkipInvalid",
                )
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "processing"
                and node.attr == "QgsProcessingFeedback"
            ):
                _append_issue(
                    issues,
                    "QgsProcessingFeedback 属于 qgis.core，不是 processing 模块属性",
                )
            if node.attr == "addVectorLayer":
                _append_issue(
                    issues,
                    "QgsProject 没有 addVectorLayer；输出图层由执行器自动加载",
                )
            if node.attr == "isGeosEmpty":
                _append_issue(
                    issues,
                    "QgsGeometry 没有 isGeosEmpty；空几何应使用 isEmpty 判断，无效几何由 Processing GeometrySkipInvalid 排除",
                )
            if node.attr in {
                "create",
                "writeAsVectorFormat",
                "writeAsVectorFormatV2",
                "writeAsVectorFormatV3",
            } and _attribute_chain_contains(node.value, "QgsVectorFileWriter"):
                _append_issue(
                    issues,
                    "禁止猜测 QgsVectorFileWriter 重载签名；矢量结果优先使用已检索的 Processing OUTPUT",
                )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                node.func.attr == "featureCount"
                and _is_processing_output_path_expression(
                    node.func.value,
                    output_path_names,
                    file_processing_result_names,
                )
            ):
                _append_issue(
                    issues,
                    "processing.run 的文件 OUTPUT 是路径字符串，不能直接调用 featureCount；需加载 QgsVectorLayer 或省略计数",
                )
            if _call_name(node.func) == "processing.run":
                _check_processing_call(node, issues)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Call):
            if (
                isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "mapLayersByName"
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == 0
            ):
                _append_issue(
                    issues,
                    "mapLayersByName(...)[0] 缺少空列表检查；必须先检查 matches",
                )
        if isinstance(node, ast.Subscript):
            field_key = _subscript_string_key(node)
            if field_key and "??" in field_key:
                _append_issue(
                    issues,
                    "字段名中包含 ?? 乱码占位符；必须使用 inspect_layer 返回的真实字段名，中间输出优先使用 ASCII 别名",
                )
    return issues


def _is_processing_output_path_expression(
    node: ast.AST,
    output_path_names: set[str],
    file_processing_result_names: set[str],
) -> bool:
    """Return whether an expression is a Processing OUTPUT path value."""
    if isinstance(node, ast.Name):
        return node.id in output_path_names
    return (
        isinstance(node, ast.Subscript)
        and _subscript_string_key(node) == "OUTPUT"
        and isinstance(node.value, ast.Name)
        and node.value.id in file_processing_result_names
    )


def _processing_call_writes_file(node: ast.AST) -> bool:
    """Return whether an assigned processing.run call has a file-backed OUTPUT."""
    if not isinstance(node, ast.Call) or _call_name(node.func) != "processing.run":
        return False
    if len(node.args) < 2:
        return False
    output = _literal_dict_items(node.args[1]).get("OUTPUT")
    if output is None:
        return False
    literal = (_literal_string(output) or "").strip()
    if literal.lower().startswith("memory:") or literal.upper() == "TEMPORARY_OUTPUT":
        return False
    if isinstance(output, ast.Attribute) and _call_name(output) == "QgsProcessing.TEMPORARY_OUTPUT":
        return False
    # Generated final outputs are commonly variables such as output_path.  Any
    # non-temporary OUTPUT is conservatively treated as file-backed.
    return True


def _check_processing_call(node: ast.Call, issues: list[str]) -> None:
    """Validate parameter shapes that Processing otherwise rejects at runtime."""
    if len(node.args) < 2:
        return
    algorithm_id = (_literal_string(node.args[0]) or "").lower()
    parameters = _literal_dict_items(node.args[1])
    if not parameters:
        return

    predicate = parameters.get("PREDICATE")
    if isinstance(predicate, (ast.List, ast.Tuple)) and any(
        isinstance(item, ast.Constant) and isinstance(item.value, str)
        for item in predicate.elts
    ):
        _append_issue(
            issues,
            "Processing PREDICATE 必须使用算法详情中的整数枚举列表，不能使用 intersects/within 等字符串",
        )

    join_fields = parameters.get("JOIN_FIELDS")
    if isinstance(join_fields, (ast.List, ast.Tuple)) and any(
        isinstance(item, ast.Constant)
        and isinstance(item.value, int)
        and not isinstance(item.value, bool)
        for item in join_fields.elts
    ):
        _append_issue(
            issues,
            "Processing JOIN_FIELDS 必须是字段名字符串列表，不能使用字段索引",
        )

    if algorithm_id == "native:aggregate":
        aggregates = parameters.get("AGGREGATES")
        if aggregates is not None and not isinstance(aggregates, (ast.List, ast.Tuple)):
            _append_issue(
                issues,
                "native:aggregate 的 AGGREGATES 必须是聚合定义 object 列表，不能传 dict 或 JSON 字符串",
            )
        group_field = _simple_field_expression(parameters.get("GROUP_BY"))
        if group_field and isinstance(aggregates, (ast.List, ast.Tuple)):
            materializes_group_key = False
            for aggregate_item in aggregates.elts:
                item = _literal_dict_items(aggregate_item)
                input_field = _simple_field_expression(item.get("input"))
                aggregate_name = (_literal_string(item.get("aggregate")) or "").lower()
                output_name = (_literal_string(item.get("name")) or "").strip()
                if (
                    input_field == group_field
                    and aggregate_name in {"first_value", "minimum", "maximum"}
                    and output_name
                ):
                    materializes_group_key = True
                    break
            if not materializes_group_key:
                _append_issue(
                    issues,
                    "native:aggregate 的 GROUP_BY 不会自动写入输出字段；"
                    f"必须在 AGGREGATES 中用 first_value 显式输出分组键 {group_field}（建议使用 ASCII 别名），"
                    "下游连接必须引用该输出别名",
                )

    if algorithm_id == "native:joinattributesbylocation" and "OVERLAY" in parameters:
        _append_issue(
            issues,
            "native:joinattributesbylocation 的连接图层参数名是 JOIN，不是 OVERLAY；调用前应以 inspect_processing_algorithm 返回的参数为准",
        )


def _literal_dict_items(node: ast.AST) -> dict[str, ast.AST]:
    if not isinstance(node, ast.Dict):
        return {}
    items: dict[str, ast.AST] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        literal_key = _literal_string(key)
        if literal_key is not None:
            items[literal_key] = value
    return items


def _simple_field_expression(node: ast.AST | None) -> str | None:
    """Return a field name from a simple Processing field expression."""
    if node is None:
        return None
    value = (_literal_string(node) or "").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].replace('""', '"')
    if not value or any(char in value for char in "()[]+-*/"):
        return None
    return value


def _subscript_string_key(node: ast.Subscript) -> str | None:
    return _literal_string(node.slice)


def _attribute_chain_contains(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.Name):
        return node.id == name
    if isinstance(node, ast.Attribute):
        return _attribute_chain_contains(node.value, name)
    return False


def _append_issue(issues: list[str], issue: str) -> None:
    if issue not in issues:
        issues.append(issue)


def _path_names_from_expression(
    node: ast.AST,
    assignments: dict[str, set[str]],
) -> set[str]:
    if isinstance(node, ast.Name):
        return assignments.get(node.id, set())
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.update(assignments.get(child.id, set()))
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            candidate = _candidate_output_path(child.value)
            if candidate:
                names.add(_output_filename(candidate))
    return names


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


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
