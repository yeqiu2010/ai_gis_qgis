"""Deterministic land-use building metrics tools."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from ...database.session_db import SessionDB
from .code_execution import build_execute_gis_code_tool
from .layer_ops import _find_layer, _layer_type_name, _run_qgis
from .registry import ToolEntry

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "calculate-land-use-building-metrics"
    / "scripts"
    / "calculate_building_metrics.py"
)
EXPECTED_OUTPUTS = [
    {
        "path": "land_use_building_metrics.gpkg",
        "name": "各类用地建筑量",
        "type": "vector",
    },
    {
        "path": "land_use_building_metrics.csv",
        "name": "各类用地建筑量统计表",
        "type": "table",
    },
]
AREA_UNITS = {"square_meter", "hectare", "mu", "square_kilometer"}
SCOPE_MODES = {"full_layer", "boundary_layer"}
MAX_LAND_TYPE_VALUES = 500
INSPECTION_REQUIRED_FIELDS = (
    "land_layer_id",
    "building_layer_id",
    "land_type_field",
    "land_area_field",
    "building_height_field",
    "building_footprint_field",
    "building_floor_area_field",
)


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    method = getattr(value, "isNull", None)
    return bool(method()) if callable(method) else False


def _collect_value_domain(
    layer,
    field_name: str,
    *,
    max_values: int = MAX_LAND_TYPE_VALUES,
) -> dict[str, Any]:
    field_index = layer.fields().indexFromName(field_name)
    if field_index < 0:
        raise ValueError(f"用地类型字段不存在：{field_name}")

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
                "不像用地类型字段。请重新确认字段。"
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


def _numeric_field_profile(layer, field_name: str) -> dict[str, Any]:
    field_index = layer.fields().indexFromName(field_name)
    if field_index < 0:
        raise ValueError(f"字段不存在：{field_name}")

    valid_count = 0
    null_count = 0
    invalid_count = 0
    non_positive_count = 0
    minimum = None
    maximum = None
    for feature in layer.getFeatures():
        value = feature[field_index]
        if _is_null(value) or (isinstance(value, str) and not value.strip()):
            null_count += 1
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            invalid_count += 1
            continue
        if not math.isfinite(number):
            invalid_count += 1
            continue
        valid_count += 1
        if number <= 0:
            non_positive_count += 1
        minimum = number if minimum is None else min(minimum, number)
        maximum = number if maximum is None else max(maximum, number)

    return {
        "field": field_name,
        "valid_count": valid_count,
        "null_count": null_count,
        "invalid_count": invalid_count,
        "non_positive_count": non_positive_count,
        "minimum": minimum,
        "maximum": maximum,
    }


def build_inspect_land_use_building_metrics_inputs_tool(*, qgis_executor=None) -> ToolEntry:
    """Build the read-only input inspection tool for land-use building metrics."""

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        values = {
            name: str(arguments.get(name) or "").strip() for name in INSPECTION_REQUIRED_FIELDS
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            return {"success": False, "error": "缺少必要参数：" + "、".join(missing)}
        boundary_layer_id = str(arguments.get("boundary_layer_id") or "").strip()

        def operation() -> dict[str, Any]:
            from qgis.core import QgsWkbTypes

            land_layer = _find_layer(values["land_layer_id"])
            building_layer = _find_layer(values["building_layer_id"])
            for layer, role in ((land_layer, "用地"), (building_layer, "建筑")):
                if not layer.isValid() or _layer_type_name(layer) != "vector":
                    raise ValueError(f"{role}图层必须是有效的矢量图层。")
                if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                    raise ValueError(f"{role}图层必须是面图层。")

            boundary = None
            if boundary_layer_id:
                boundary = _find_layer(boundary_layer_id)
                if not boundary.isValid() or _layer_type_name(boundary) != "vector":
                    raise ValueError("边界图层必须是有效的矢量图层。")
                if boundary.geometryType() != QgsWkbTypes.PolygonGeometry:
                    raise ValueError("边界图层必须是面图层。")

            domain = _collect_value_domain(land_layer, values["land_type_field"])
            profiles = {
                "land_area": _numeric_field_profile(land_layer, values["land_area_field"]),
                "building_height": _numeric_field_profile(
                    building_layer, values["building_height_field"]
                ),
                "building_footprint": _numeric_field_profile(
                    building_layer, values["building_footprint_field"]
                ),
                "building_floor_area": _numeric_field_profile(
                    building_layer, values["building_floor_area_field"]
                ),
            }
            return {
                "success": True,
                "land_layer": {"id": land_layer.id(), "name": land_layer.name()},
                "building_layer": {
                    "id": building_layer.id(),
                    "name": building_layer.name(),
                },
                "boundary_layer": (
                    {"id": boundary.id(), "name": boundary.name()} if boundary else None
                ),
                "land_type_field": values["land_type_field"],
                "land_type_domain": domain,
                "numeric_profiles": profiles,
                "scope_mode": "boundary_layer" if boundary else "full_layer",
                "inspection_complete": True,
            }

        try:
            return _run_qgis(qgis_executor, operation)
        except (RuntimeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    return ToolEntry(
        name="inspect_land_use_building_metrics_inputs",
        description=(
            "只读检查各类用地建筑量统计所需的面图层、字段、真实用地类型值域和数值质量。"
            "在调用固定执行工具前必须调用；可同时验证可选的计算边界图层。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "land_layer_id": {"type": "string", "description": "用地面图层 ID。"},
                "building_layer_id": {"type": "string", "description": "建筑单体面图层 ID。"},
                "land_type_field": {"type": "string", "description": "用地类型字段。"},
                "land_area_field": {"type": "string", "description": "地块面积字段。"},
                "building_height_field": {"type": "string", "description": "建筑高度字段。"},
                "building_footprint_field": {
                    "type": "string",
                    "description": "建筑占地面积字段。",
                },
                "building_floor_area_field": {
                    "type": "string",
                    "description": "建筑面积字段。",
                },
                "boundary_layer_id": {
                    "type": "string",
                    "description": "可选的计算边界面图层 ID；不传表示检查全图层模式。",
                },
            },
            "required": list(INSPECTION_REQUIRED_FIELDS),
            "additionalProperties": False,
        },
        handler=handler,
        category="land_use_building_metrics",
        requires_confirmation=False,
        writes_project=False,
    )


def build_land_use_building_metrics_code(arguments: dict[str, Any]) -> str:
    parameters = _normalize_parameters(arguments)
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    payload = json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
    return f"LAND_USE_BUILDING_PARAMETERS_JSON = {payload!r}\n{script}"


def build_land_use_building_metrics_tool(
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
            parameters = _normalize_parameters(arguments)
            code = build_land_use_building_metrics_code(parameters)
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
            "analysis_parameters": parameters,
            "fixed_script": SCRIPT_PATH.name,
        }

    return ToolEntry(
        name="execute_land_use_building_metrics",
        description=(
            "使用内置固定脚本按用地类型统计建筑数量、高度、占地面积、建筑面积和建筑密度。"
            "支持默认全图层或可选边界图层；AI 只传入经检查确认的参数，不得传入代码。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "land_layer_id": {"type": "string"},
                "building_layer_id": {"type": "string"},
                "land_type_field": {"type": "string"},
                "land_area_field": {"type": "string"},
                "land_area_unit": {"type": "string", "enum": sorted(AREA_UNITS)},
                "building_height_field": {"type": "string"},
                "building_footprint_field": {"type": "string"},
                "building_floor_area_field": {"type": "string"},
                "building_area_unit": {"type": "string", "enum": sorted(AREA_UNITS)},
                "scope_mode": {
                    "type": "string",
                    "enum": sorted(SCOPE_MODES),
                    "default": "full_layer",
                    "description": "full_layer 为全图层；boundary_layer 为指定边界。",
                },
                "boundary_layer_id": {
                    "type": "string",
                    "description": "scope_mode=boundary_layer 时必须提供。",
                },
                "target_crs": {
                    "type": "string",
                    "description": "适合研究区且线性单位为米的投影 CRS。",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3600,
                    "default": 600,
                },
            },
            "required": [
                "land_layer_id",
                "building_layer_id",
                "land_type_field",
                "land_area_field",
                "land_area_unit",
                "building_height_field",
                "building_footprint_field",
                "building_floor_area_field",
                "building_area_unit",
                "scope_mode",
                "target_crs",
            ],
            "additionalProperties": False,
        },
        handler=handler,
        category="land_use_building_metrics",
        requires_confirmation=True,
        writes_project=True,
    )


def _normalize_parameters(arguments: dict[str, Any]) -> dict[str, Any]:
    required_strings = (
        "land_layer_id",
        "building_layer_id",
        "land_type_field",
        "land_area_field",
        "building_height_field",
        "building_footprint_field",
        "building_floor_area_field",
        "target_crs",
    )
    parameters: dict[str, Any] = {}
    for name in required_strings:
        value = str(arguments.get(name) or "").strip()
        if not value:
            raise ValueError(f"缺少必要参数：{name}")
        parameters[name] = value

    for name in ("land_area_unit", "building_area_unit"):
        unit = str(arguments.get(name) or "").strip()
        if unit not in AREA_UNITS:
            raise ValueError(f"{name} 必须是支持的面积单位。")
        parameters[name] = unit

    scope_mode = str(arguments.get("scope_mode") or "full_layer").strip()
    if scope_mode not in SCOPE_MODES:
        raise ValueError("scope_mode 必须是 full_layer 或 boundary_layer。")
    parameters["scope_mode"] = scope_mode
    boundary_layer_id = str(arguments.get("boundary_layer_id") or "").strip()
    if scope_mode == "boundary_layer" and not boundary_layer_id:
        raise ValueError("boundary_layer 模式必须提供 boundary_layer_id。")
    if scope_mode == "full_layer" and boundary_layer_id:
        raise ValueError("full_layer 模式不得提供 boundary_layer_id。")
    parameters["boundary_layer_id"] = boundary_layer_id or None
    return parameters
