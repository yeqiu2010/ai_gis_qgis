"""Deterministic school service coverage execution tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...database.session_db import SessionDB
from .code_execution import build_execute_gis_code_tool
from .layer_ops import _find_layer, _layer_type_name, _run_qgis
from .registry import ToolEntry

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "calculate-school-service-coverage"
    / "scripts"
    / "calculate_coverage.py"
)
EXPECTED_OUTPUTS = [
    {
        "path": "school_service_coverage.gpkg",
        "name": "中小学服务覆盖率",
        "type": "vector",
    },
    {
        "path": "school_service_coverage_by_group.csv",
        "name": "分组中小学服务覆盖率",
        "type": "table",
    },
]
AREA_UNITS = {"square_meter", "hectare", "mu", "square_kilometer"}
MAX_SCHOOL_TYPE_VALUES = 200


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    method = getattr(value, "isNull", None)
    return bool(method()) if callable(method) else False


def _collect_school_type_domain(
    layer,
    field_name: str,
    *,
    max_values: int = MAX_SCHOOL_TYPE_VALUES,
) -> dict[str, Any]:
    """Return the complete, stringified category domain used by the fixed script."""
    field_index = layer.fields().indexFromName(field_name)
    if field_index < 0:
        raise ValueError(f"学校类型字段不存在：{field_name}")

    counts: dict[str, int] = {}
    null_count = 0
    empty_count = 0
    for feature in layer.getFeatures():
        raw_value = feature[field_index]
        if _is_null(raw_value):
            null_count += 1
            continue
        value = str(raw_value)
        if not value.strip():
            empty_count += 1
            continue
        counts[value] = counts.get(value, 0) + 1
        if len(counts) > max_values:
            raise ValueError(
                f"字段 {field_name} 的唯一非空值超过 {max_values} 个，"
                "不像类别字段。请重新确认学校类型字段。"
            )

    value_counts = [
        {"value": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "available_values": [item["value"] for item in value_counts],
        "value_counts": value_counts,
        "distinct_count": len(value_counts),
        "null_count": null_count,
        "empty_count": empty_count,
        "domain_complete": True,
    }


def build_inspect_school_service_coverage_inputs_tool(*, qgis_executor=None) -> ToolEntry:
    """Build the read-only category-domain inspection tool for this workflow."""

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        layer_id = str(arguments.get("school_layer_id") or "").strip()
        field_name = str(arguments.get("school_type_field") or "").strip()
        if not layer_id or not field_name:
            return {
                "success": False,
                "error": "必须提供 school_layer_id 和 school_type_field。",
            }

        def operation() -> dict[str, Any]:
            layer = _find_layer(layer_id)
            if not layer.isValid():
                raise ValueError(f"学校图层无效：{layer_id}")
            if _layer_type_name(layer) != "vector":
                raise ValueError("学校图层必须是矢量图层。")
            domain = _collect_school_type_domain(layer, field_name)
            return {
                "success": True,
                "school_layer": {"id": layer.id(), "name": layer.name()},
                "school_type_field": field_name,
                **domain,
                "selection_policy": (
                    "school_type_values 必须是 available_values 的子集；"
                    "用户目标与真实值完全一致时只选择该真实值；"
                    "禁止补充 available_values 中不存在的常识类别。"
                ),
            }

        try:
            return _run_qgis(qgis_executor, operation)
        except (RuntimeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    return ToolEntry(
        name="inspect_school_service_coverage_inputs",
        description=(
            "只读检查学校类型字段的完整实际唯一值及计数。"
            "在确定 school_type_values、调用学校服务覆盖率执行工具之前必须调用；"
            "AI 只能从返回的 available_values 中选择类别。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "school_layer_id": {"type": "string", "description": "已确认的学校图层 ID。"},
                "school_type_field": {"type": "string", "description": "待确认取值域的学校类型字段。"},
            },
            "required": ["school_layer_id", "school_type_field"],
            "additionalProperties": False,
        },
        handler=handler,
        category="school_service_coverage",
        requires_confirmation=False,
        writes_project=False,
    )


def build_school_service_coverage_code(arguments: dict[str, Any]) -> str:
    parameters = _normalize_parameters(arguments)
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    payload = json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
    return f"SCHOOL_SERVICE_PARAMETERS_JSON = {payload!r}\n{script}"


def build_school_service_coverage_tool(
    *,
    session_db: SessionDB | None,
    session_id: str,
    iface=None,
    qgis_executor=None,
    executor_config: dict[str, Any] | None = None,
) -> ToolEntry:
    code_executor = build_execute_gis_code_tool(
        session_db=session_db,
        session_id=session_id,
        iface=iface,
        qgis_executor=qgis_executor,
        executor_config=executor_config,
    )

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            code = build_school_service_coverage_code(arguments)
        except (OSError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        result = code_executor.handler(
            {
                "code": code,
                "expected_outputs": EXPECTED_OUTPUTS,
                "timeout_seconds": int(arguments.get("timeout_seconds") or 600),
            }
        )
        return {
            **result,
            "analysis_parameters": _normalize_parameters(arguments),
            "fixed_script": SCRIPT_PATH.name,
        }

    return ToolEntry(
        name="execute_school_service_coverage",
        description=(
            "使用内置固定脚本计算学校服务半径对居住区的覆盖率，并按街道、乡镇或行政区汇总。"
            "AI 只负责传入已经通过图层检查确认的参数，不得生成、修改或传入 Python 代码。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "school_layer_id": {"type": "string", "description": "已确认的学校图层 ID。"},
                "residential_layer_id": {
                    "type": "string",
                    "description": "已确认的居住区面图层 ID。",
                },
                "school_type_field": {"type": "string", "description": "学校类型字段。"},
                "school_type_values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 100,
                    "description": (
                        "代表目标学校类型的精确字段值；必须来自 "
                        "inspect_school_service_coverage_inputs 返回的 available_values。"
                    ),
                },
                "residential_area_field": {
                    "type": "string",
                    "description": "居住区总面积数值字段。",
                },
                "area_unit": {
                    "type": "string",
                    "enum": sorted(AREA_UNITS),
                    "description": "面积字段单位：平方米、公顷、亩或平方千米。",
                },
                "group_field": {
                    "type": "string",
                    "description": "街道、乡镇或行政区分组字段。",
                },
                "service_distance_m": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 100000,
                    "default": 500,
                    "description": "以米为单位的服务半径。",
                },
                "target_crs": {
                    "type": "string",
                    "description": "适合研究区、线性单位为米的投影 CRS，例如 EPSG:4547。",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3600,
                    "default": 600,
                },
            },
            "required": [
                "school_layer_id",
                "residential_layer_id",
                "school_type_field",
                "school_type_values",
                "residential_area_field",
                "area_unit",
                "group_field",
                "service_distance_m",
                "target_crs",
            ],
            "additionalProperties": False,
        },
        handler=handler,
        category="school_service_coverage",
        requires_confirmation=True,
        writes_project=True,
    )


def _normalize_parameters(arguments: dict[str, Any]) -> dict[str, Any]:
    required_strings = (
        "school_layer_id",
        "residential_layer_id",
        "school_type_field",
        "residential_area_field",
        "group_field",
        "target_crs",
    )
    parameters: dict[str, Any] = {}
    for name in required_strings:
        value = str(arguments.get(name) or "").strip()
        if not value:
            raise ValueError(f"缺少必要参数：{name}")
        parameters[name] = value

    values = arguments.get("school_type_values")
    if not isinstance(values, list) or not values or len(values) > 100:
        raise ValueError("school_type_values 必须是包含 1 到 100 个值的数组。")
    normalized_values = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("school_type_values 只能包含字符串；数值代码也应按原显示值传为字符串。")
        if not value.strip():
            raise ValueError("school_type_values 不能包含空值。")
        normalized_values.append(value)
    parameters["school_type_values"] = normalized_values

    area_unit = str(arguments.get("area_unit") or "").strip()
    if area_unit not in AREA_UNITS:
        raise ValueError("area_unit 必须是 square_meter、hectare、mu 或 square_kilometer。")
    parameters["area_unit"] = area_unit

    try:
        distance = float(arguments.get("service_distance_m"))
    except (TypeError, ValueError) as exc:
        raise ValueError("service_distance_m 必须是数值。") from exc
    if not 0 < distance <= 100000:
        raise ValueError("service_distance_m 必须大于 0 且不超过 100000 米。")
    parameters["service_distance_m"] = distance
    return parameters
