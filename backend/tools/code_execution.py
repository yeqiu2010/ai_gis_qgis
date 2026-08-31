"""Generated GIS code execution tool."""

from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse

from ...database.session_db import SessionDB
from ..executor.qgis_executor import QGISCodeExecutor
from ..failure_analysis import classify_failure
from ..processing.algorithm_evidence import processing_parameter_names
from ..processing.toolbox_catalog import default_catalog
from .layer_ops import _infer_layer_type, _layer_summary, _qgis_classes, _run_qgis, _snapshot
from .registry import ToolEntry

VECTOR_OUTPUT_EXTENSIONS = {".shp", ".gpkg", ".geojson", ".kml"}
RASTER_OUTPUT_EXTENSIONS = {".tif", ".tiff"}
TABLE_OUTPUT_EXTENSIONS = {".csv", ".xlsx", ".dbf"}
FILE_OUTPUT_EXTENSIONS = {
    ".txt",
    ".json",
    ".html",
    ".md",
    ".pdf",
    ".png",
    ".qml",
}
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
QGIS_LAYER_ID_PATTERN = re.compile(
    r"(?:^|[_-])[0-9a-f]{8}(?:[_-][0-9a-f]{4}){3}[_-][0-9a-f]{12}$",
    re.IGNORECASE,
)


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
        preexisting_layer_ids: set[str] | None = None
        if use_current_qgis:
            assert qgis_executor is not None
            preexisting_layer_ids = _project_layer_ids(qgis_executor)
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
                    preexisting_layer_ids=preexisting_layer_ids,
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

        classification: dict[str, object] = {}
        if not success:
            classification = classify_failure(
                str(error or result.get("stderr") or ""),
                preflight_failed=bool(result.get("preflight_failed")),
            )
        final_result = {
            **result,
            "success": success,
            "error": error,
            "error_code": result.get("error_code") or classification.get("error_code"),
            "cause": classification.get("cause"),
            "loaded_layers": loaded_layers,
            "load_errors": load_errors,
            "delivered_outputs": delivered_outputs,
            "delivery_errors": delivery_errors,
            "expected_outputs_inferred": False,
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
            "用户要求生成或导出文件时，代码必须创建对应 expected_outputs；"
            "仅需统计结论或调整当前图层样式时可以不提供 expected_outputs，"
            "分别通过 stdout 返回最终结论或直接更新 QGIS 图层渲染状态。"
            "不得用于字段唯一值探查或为后续代码生成而执行诊断脚本。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "要执行的 Python 代码。需要创建文件时必须写入 QGIS_AGENT_WORKSPACE；"
                        "无文件任务必须通过 stdout 给出最终结论或完成用户要求的当前图层样式调整。"
                        "不得访问网络，不得调用 subprocess/os.system/eval/exec，"
                        "不得创建 QgsApplication/QApplication 或初始化新的 QGIS。"
                    ),
                },
                "expected_outputs": {
                    "type": "array",
                    "description": (
                        "可选的预期输出文件。path 必须是相对路径或工作目录内路径。"
                        "用户只要求统计结论或调整当前图层样式时可省略或传空数组；"
                        "用户要求生成/导出文件时必须完整列出。"
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
                            "required": {
                                "type": "boolean",
                                "description": "是否为任务成功所必需，默认 true。",
                            },
                        },
                        "required": ["path", "name", "type"],
                        "additionalProperties": False,
                    },
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
            "required": ["code"],
            "additionalProperties": False,
        },
        handler=handler,
        category="execution",
        requires_confirmation=True,
        writes_project=True,
        idempotency_key_fields=("code", "expected_outputs", "delivery_outputs"),
        resume_policy="return_result",
        execution_affinity="main_thread",
        timeout_seconds=int((executor_config or {}).get("timeout_seconds") or 300),
    )


def validate_execute_gis_code_arguments(arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the code/output contract before asking for execution approval."""
    code = str(arguments.get("code") or "")
    expected_outputs = arguments.get("expected_outputs") or []
    if not isinstance(expected_outputs, list):
        return None
    issues = find_generated_code_issues(code)
    issues[:0] = find_expected_output_contract_issues(expected_outputs)
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


def find_expected_output_contract_issues(
    expected_outputs: list[Any],
) -> list[str]:
    """Validate declarations without guessing how generated code writes files."""
    issues: list[str] = []
    seen_paths: set[str] = set()
    for index, output in enumerate(expected_outputs, start=1):
        if not isinstance(output, dict):
            issues.append(f"第 {index} 个 expected_outputs 必须是 object")
            continue
        raw_path = str(output.get("path") or "").strip()
        if not raw_path:
            issues.append(f"第 {index} 个 expected_outputs 缺少 path")
        else:
            normalized_path = raw_path.replace("\\", "/").casefold()
            if normalized_path in seen_paths:
                issues.append(f"expected_outputs 包含重复路径：{raw_path}")
            seen_paths.add(normalized_path)
        if not str(output.get("name") or "").strip():
            issues.append(f"第 {index} 个 expected_outputs 缺少 name")
        output_type = str(output.get("type") or "").strip().lower()
        if output_type not in {"vector", "raster", "table", "file"}:
            issues.append(f"第 {index} 个 expected_outputs 的 type 无效：{output_type or '空'}")
        required = output.get("required", True)
        if not isinstance(required, bool):
            issues.append(f"第 {index} 个 expected_outputs 的 required 必须是 boolean")
    return issues


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
    literal_string_assignments = _assigned_literal_strings(assignments)
    map_layer_assignments = _assigned_map_layer_names(assignments)
    guarded_layer_names = _null_guarded_names(tree)
    for layer_name in sorted(map_layer_assignments.difference(guarded_layer_names)):
        _append_issue(
            issues,
            f"QgsProject.mapLayer() 返回值 {layer_name} 未检查是否为 None；"
            "必须在访问 extent、fields 或传入 Processing 前显式检查",
        )
    color_ramp_shader_names = _assigned_constructor_names(
        assignments,
        "QgsColorRampShader",
    )
    pseudo_color_renderer_names = _assigned_constructor_names(
        assignments,
        "QgsSingleBandPseudoColorRenderer",
    )
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
        if isinstance(node, ast.Call):
            _check_raster_shader_call(
                node,
                color_ramp_shader_names=color_ramp_shader_names,
                pseudo_color_renderer_names=pseudo_color_renderer_names,
                issues=issues,
            )
            _check_color_ramp_shader_item_call(
                node,
                color_ramp_shader_names=color_ramp_shader_names,
                issues=issues,
            )
            _check_color_ramp_shader_range_call(
                node,
                color_ramp_shader_names=color_ramp_shader_names,
                issues=issues,
            )
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
                _check_processing_call(
                    node,
                    issues,
                    literal_string_assignments=literal_string_assignments,
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


def _check_processing_call(
    node: ast.Call,
    issues: list[str],
    *,
    literal_string_assignments: dict[str, str] | None = None,
) -> None:
    """Validate parameter shapes that Processing otherwise rejects at runtime."""
    if len(node.args) < 2:
        return
    algorithm_id = (_literal_string(node.args[0]) or "").lower()
    parameters = _literal_dict_items(node.args[1])
    if not parameters:
        return

    literal_string_assignments = literal_string_assignments or {}
    tool_spec = default_catalog().get(algorithm_id) if algorithm_id else None
    if tool_spec is not None:
        catalog_parameters = set(processing_parameter_names(tool_spec.detail()))
        unknown_parameters = set(parameters).difference(catalog_parameters)
        if (
            algorithm_id == "qgis:heatmapkerneldensityestimation"
            and "OUTPUT_VALUES" in unknown_parameters
        ):
            _append_issue(
                issues,
                "qgis:heatmapkerneldensityestimation 的参数名是单数 OUTPUT_VALUE，"
                "不是 OUTPUT_VALUES；请将键改为 OUTPUT_VALUE",
            )
            unknown_parameters.remove("OUTPUT_VALUES")
        # Some legacy catalog entries are visibly partial (for example an
        # overlay algorithm without its overlay input). Do not turn incomplete
        # documentation into a false runtime blocker. Rich parameter records
        # remain suitable for deterministic validation.
        if len(catalog_parameters) >= 4 and unknown_parameters:
            allowed = ", ".join(sorted(catalog_parameters))
            example = " ".join(str(tool_spec.code_example or "").split())[:1600]
            _append_issue(
                issues,
                f"Processing 算法 {algorithm_id} 的参数不在 Catalog 证据中："
                + ", ".join(sorted(unknown_parameters))
                + f"；允许参数：{allowed}"
                + (f"；Catalog 代码示例：{example}" if example else "")
                + "；必须严格按该证据修正，不能继续猜测参数名",
            )
    if algorithm_id == "gdal:cliprasterbyextent" and "PROJWIN" not in parameters:
        _append_issue(
            issues,
            "Processing 算法 gdal:cliprasterbyextent 缺少必填参数 PROJWIN；"
            "PROJWIN 是该算法公开的 Processing 裁剪范围参数名，不是仅供 GDAL 内部使用的名称；"
            "可直接传范围图层、QgsRectangle，或 xmin,xmax,ymin,ymax 字符串",
        )
    for parameter_name, value in parameters.items():
        if not _is_processing_layer_parameter(parameter_name):
            continue
        for layer_value in _iter_layer_parameter_values(value):
            if isinstance(layer_value, ast.Call) and _call_name(
                layer_value.func
            ).endswith("mapLayer"):
                _append_issue(
                    issues,
                    f"Processing {parameter_name} 不能直接调用 mapLayer(...)；"
                    "必须先赋给变量、检查结果不是 None，再传入 QgsMapLayer 对象",
                )
                continue
            literal = _resolved_literal_string(
                layer_value,
                literal_string_assignments,
            )
            if literal and QGIS_LAYER_ID_PATTERN.search(literal):
                _append_issue(
                    issues,
                    f"Processing {parameter_name} 不能直接传 QGIS 图层 ID 字符串 {literal}；"
                    "必须使用 QgsProject.instance().mapLayer(layer_id) 解析为图层对象并检查不是 None",
                )

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

    if algorithm_id == "native:fieldcalculator":
        field_name = (_literal_string(parameters.get("FIELD_NAME")) or "").lower()
        formula = _literal_string(parameters.get("FORMULA")) or ""
        field_type_node = parameters.get("FIELD_TYPE")
        field_type = (
            field_type_node.value
            if isinstance(field_type_node, ast.Constant)
            and isinstance(field_type_node.value, int)
            and not isinstance(field_type_node.value, bool)
            else None
        )
        numeric_name = any(
            marker in field_name
            for marker in ("dense", "density", "ratio", "rate", "coverage", "密度", "比例", "率")
        )
        if field_type == 2 and (numeric_name or "/" in formula):
            _append_issue(
                issues,
                "native:fieldcalculator 的 FIELD_TYPE=2 是 Text/String，不是 Double；"
                "密度或除法结果必须使用算法详情中的 Decimal/Double 类型（当前算法通常为 FIELD_TYPE=0），"
                "不能靠增大 FIELD_LENGTH 修复",
            )
        if (
            "area" in field_name
            and re.fullmatch(
                r"\s*to_(?:real|float)\(\s*['\"]land_area['\"]\s*\)\s*",
                formula,
                flags=re.IGNORECASE,
            )
        ):
            _append_issue(
                issues,
                "地块几何面积不能直接由可能为空、单位不明的 String 字段 land_area 转换；"
                "用户要求按平方米筛选地块面积时，应先统一到米制投影，再用 $area 计算 Double 字段。"
                "只有用户明确指定 land_area 且 inspect_layer 已确认其非空数值和单位时才能转换该属性",
            )

    if algorithm_id == "native:joinattributesbylocation" and "OVERLAY" in parameters:
        _append_issue(
            issues,
            "native:joinattributesbylocation 的连接图层参数名是 JOIN，不是 OVERLAY；调用前应以 inspect_processing_algorithm 返回的参数为准",
        )


def _assigned_literal_strings(assignments: list[ast.Assign]) -> dict[str, str]:
    candidates: dict[str, list[ast.AST]] = {}
    for assignment in assignments:
        for target in assignment.targets:
            if isinstance(target, ast.Name):
                candidates.setdefault(target.id, []).append(assignment.value)

    values: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for name, candidate_values in candidates.items():
            # Reassigned variables are intentionally treated as dynamic. This
            # avoids both false certainty and oscillation between literals.
            if name in values or len(candidate_values) != 1:
                continue
            value = _resolved_literal_string(candidate_values[0], values)
            if value is None:
                continue
            values[name] = value
            changed = True
    return values


def _resolved_literal_string(
    node: ast.AST,
    assignments: dict[str, str],
) -> str | None:
    literal = _literal_string(node)
    if literal is not None:
        return literal
    if isinstance(node, ast.Name):
        return assignments.get(node.id)
    return None


def _assigned_map_layer_names(assignments: list[ast.Assign]) -> set[str]:
    names: set[str] = set()
    for assignment in assignments:
        if not isinstance(assignment.value, ast.Call):
            continue
        if not _call_name(assignment.value.func).endswith("mapLayer"):
            continue
        names.update(
            target.id for target in assignment.targets if isinstance(target, ast.Name)
        )
    return names


def _null_guarded_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.Assert)):
            names.update(_null_guard_names_from_test(node.test))
    return names


def _null_guard_names_from_test(node: ast.AST) -> set[str]:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return {node.operand.id} if isinstance(node.operand, ast.Name) else set()
    if isinstance(node, ast.BoolOp):
        return {
            name
            for value in node.values
            for name in _null_guard_names_from_test(value)
        }
    if not isinstance(node, ast.Compare):
        return set()
    operands = [node.left, *node.comparators]
    if not any(isinstance(value, ast.Constant) and value.value is None for value in operands):
        return set()
    return {value.id for value in operands if isinstance(value, ast.Name)}


def _is_processing_layer_parameter(name: str) -> bool:
    normalized = str(name or "").upper()
    return (
        normalized == "INPUT"
        or normalized.startswith("INPUT_")
        or normalized
        in {
            "INTERSECT",
            "JOIN",
            "LAYERS",
            "MASK",
            "OVERLAY",
            "REFERENCE_LAYER",
        }
    )


def _iter_layer_parameter_values(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return list(node.elts)
    return [node]


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


def _assigned_constructor_names(
    assignments: list[ast.Assign],
    constructor_name: str,
) -> set[str]:
    """Return simple variables which hold instances of a known constructor."""
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            if not _is_constructor_instance(
                assignment.value,
                constructor_name=constructor_name,
                assigned_names=names,
            ):
                continue
            for target in assignment.targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _is_constructor_instance(
    node: ast.AST,
    *,
    constructor_name: str,
    assigned_names: set[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in assigned_names
    if not isinstance(node, ast.Call):
        return False
    call_name = _call_name(node.func)
    return call_name == constructor_name or call_name.endswith(f".{constructor_name}")


def _check_raster_shader_call(
    node: ast.Call,
    *,
    color_ramp_shader_names: set[str],
    pseudo_color_renderer_names: set[str],
    issues: list[str],
) -> None:
    """Reject passing QgsColorRampShader where QgsRasterShader is required."""
    call_name = _call_name(node.func)
    shader_argument: ast.AST | None = None
    if call_name == "QgsSingleBandPseudoColorRenderer" or call_name.endswith(
        ".QgsSingleBandPseudoColorRenderer"
    ):
        shader_argument = _call_argument(
            node,
            position=2,
            keyword_names={"shader"},
        )
    elif (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "setShader"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in pseudo_color_renderer_names
    ):
        shader_argument = _call_argument(
            node,
            position=0,
            keyword_names={"shader"},
        )
    if shader_argument is None or not _is_constructor_instance(
        shader_argument,
        constructor_name="QgsColorRampShader",
        assigned_names=color_ramp_shader_names,
    ):
        return
    _append_issue(
        issues,
        "QgsSingleBandPseudoColorRenderer 需要 QgsRasterShader，不能直接传入 "
        "QgsColorRampShader；必须先创建 QgsRasterShader，调用 "
        "raster_shader.setRasterShaderFunction(color_ramp_shader)，再把 "
        "raster_shader 传给构造器或 setShader",
    )


def _check_color_ramp_shader_item_call(
    node: ast.Call,
    *,
    color_ramp_shader_names: set[str],
    issues: list[str],
) -> None:
    """Reject the nonexistent singular QgsColorRampShader item setter."""
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "setColorRampItem":
        return
    if not _is_constructor_instance(
        node.func.value,
        constructor_name="QgsColorRampShader",
        assigned_names=color_ramp_shader_names,
    ):
        return
    _append_issue(
        issues,
        "QgsColorRampShader 没有 setColorRampItem；必须先构造 "
        "QgsColorRampShader.ColorRampItem 列表，再调用 setColorRampItemList(items)",
    )


def _check_color_ramp_shader_range_call(
    node: ast.Call,
    *,
    color_ramp_shader_names: set[str],
    issues: list[str],
) -> None:
    """Reject guessed classification range setters absent from QGIS API."""
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in {
        "setClassificationMin",
        "setClassificationMax",
    }:
        return
    if not _is_constructor_instance(
        node.func.value,
        constructor_name="QgsColorRampShader",
        assigned_names=color_ramp_shader_names,
    ):
        return
    _append_issue(
        issues,
        f"QgsColorRampShader 没有 {node.func.attr}；范围应通过构造器的最小值/最大值参数，"
        "或 setMinimumValue()/setMaximumValue() 设置",
    )


def _append_issue(issues: list[str], issue: str) -> None:
    if issue not in issues:
        issues.append(issue)


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


def _call_argument(
    node: ast.Call,
    *,
    position: int,
    keyword_names: set[str],
) -> ast.AST | None:
    """Return a positional or named argument from a generated-code call."""
    if len(node.args) > position:
        return node.args[position]
    for keyword in node.keywords:
        if keyword.arg in keyword_names:
            return keyword.value
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


def _load_output_layers(
    outputs: list[dict[str, Any]],
    *,
    session_db: SessionDB | None,
    session_id: str,
    qgis_executor=None,
    preexisting_layer_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    loadable_outputs = [
        output
        for output in outputs
        if output.get("verified")
        and str(output.get("type") or "").lower() in {"vector", "raster"}
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
            existing_layer = _find_loaded_output_layer(
                project,
                path,
                output_name=name,
                output_type=output_type,
                preexisting_layer_ids=preexisting_layer_ids,
            )
            if existing_layer is not None and existing_layer.isValid():
                _snapshot(session_db, session_id, existing_layer, "generated_reused")
                loaded.append({**_layer_summary(existing_layer), "reused": True})
                continue
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


def _project_layer_ids(qgis_executor) -> set[str] | None:
    """Snapshot project layer IDs before generated code mutates the project."""
    try:
        return set(
            _run_qgis(
                qgis_executor,
                lambda: _qgis_classes()["QgsProject"].instance().mapLayers().keys(),
            )
        )
    except Exception:
        # Source URI matching still provides a safe fallback if a project
        # snapshot cannot be collected in a particular QGIS host.
        return None


def _find_loaded_output_layer(
    project,
    path: str,
    *,
    output_name: str = "",
    output_type: str = "",
    preexisting_layer_ids: set[str] | None = None,
):
    """Return a layer already loaded by generated code for this output."""
    target_paths = _local_source_paths(path)
    for layer in project.mapLayers().values():
        if any(
            _same_local_file(target, source)
            for target in target_paths
            for raw_source in _layer_source_values(layer)
            for source in _local_source_paths(raw_source)
        ):
            return layer

    # Some providers expose decorated or opaque data-source URIs. Only fall
    # back to name/type matching for layers created by this exact execution;
    # this prevents an older same-named project layer from being reused.
    if preexisting_layer_ids is None:
        return None
    candidates = []
    normalized_name = output_name.strip().casefold()
    for layer in project.mapLayers().values():
        try:
            if str(layer.id()) in preexisting_layer_ids:
                continue
            if str(layer.name() or "").strip().casefold() != normalized_name:
                continue
            if (
                output_type
                and _infer_layer_type(str(layer.source() or ""), None) != output_type
            ):
                layer_kind = (
                    "raster"
                    if layer.type() == 1
                    else "vector"
                    if layer.type() == 0
                    else ""
                )
                if layer_kind != output_type:
                    continue
        except (AttributeError, RuntimeError, ValueError):
            continue
        candidates.append(layer)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _layer_source_values(layer) -> list[str]:
    """Collect source strings exposed by a QGIS layer and its provider."""
    values: list[str] = []
    try:
        values.append(str(layer.source() or ""))
    except (AttributeError, RuntimeError):
        pass
    try:
        provider = layer.dataProvider()
        if provider is not None:
            values.append(str(provider.dataSourceUri() or ""))
    except (AttributeError, RuntimeError):
        pass
    return [value for value in dict.fromkeys(values) if value]


def _local_source_paths(value: str) -> list[Path]:
    """Extract local paths from common QGIS/GDAL source URI forms."""
    raw = unquote(str(value or "").strip().strip("\"'"))
    if not raw:
        return []
    raw = raw.split("|", 1)[0]
    quoted_path = re.match(r'^[A-Za-z0-9_]+:"([^"]+)"(?::.*)?$', raw)
    if quoted_path:
        raw = quoted_path.group(1)
    if raw.casefold().startswith("file:"):
        parsed = urlparse(raw)
        raw = unquote(parsed.path)
        if parsed.netloc:
            raw = f"//{parsed.netloc}{raw}"
        elif re.match(r"^/[A-Za-z]:/", raw):
            raw = raw[1:]
    try:
        return [Path(raw).expanduser().resolve()]
    except (OSError, RuntimeError, ValueError):
        return []


def _same_local_file(left: Path, right: Path) -> bool:
    try:
        if left.exists() and right.exists():
            return left.samefile(right)
    except (OSError, RuntimeError, ValueError):
        pass
    normalized_left = str(left).replace("\\", "/").casefold()
    normalized_right = str(right).replace("\\", "/").casefold()
    return normalized_left == normalized_right
