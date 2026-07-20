"""Deterministic land-cover thematic map tools."""

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
    / "generate-land-cover-map"
    / "scripts"
    / "generate_land_cover_map.py"
)
EXPECTED_OUTPUTS = [
    {"path": "land_cover_map.png", "name": "土地覆盖专题图 PNG", "type": "file"},
    {"path": "land_cover_map.pdf", "name": "土地覆盖专题图 PDF", "type": "file"},
]
CLIPPED_RASTER_OUTPUT = {
    "path": "land_cover_clipped.tif",
    "name": "土地覆盖裁剪结果",
    "type": "file",
}
CLIPPED_VECTOR_OUTPUT = {
    "path": "land_cover_clipped.gpkg",
    "name": "土地覆盖裁剪结果",
    "type": "file",
}
DEFAULT_CLASS_MAPPING = [
    {"value": 1, "label": "Cropland", "color": [250, 227, 156]},
    {"value": 2, "label": "Forest", "color": [68, 111, 51]},
    {"value": 3, "label": "Shrub", "color": [51, 160, 44]},
    {"value": 4, "label": "Grassland", "color": [171, 211, 123]},
    {"value": 5, "label": "Water", "color": [30, 105, 180]},
    {"value": 6, "label": "Snow/Ice", "color": [166, 206, 227]},
    {"value": 7, "label": "Barren", "color": [207, 189, 163]},
    {"value": 8, "label": "Impervious", "color": [226, 66, 144]},
    {"value": 9, "label": "Wetland", "color": [40, 155, 232]},
]
MAX_CATEGORY_VALUES = 500


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    method = getattr(value, "isNull", None)
    return bool(method()) if callable(method) else False


def _json_category_value(value: Any) -> str | int | float | bool:
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _infer_legend_title(mapping: list[dict[str, Any]]) -> str:
    labels = "".join(str(item.get("label") or "") for item in mapping)
    if any("\uac00" <= char <= "\ud7af" for char in labels):
        return "범례"
    if any(
        "\u3040" <= char <= "\u30ff" or "\u31f0" <= char <= "\u31ff"
        for char in labels
    ):
        return "凡例"
    if any("\u3400" <= char <= "\u9fff" for char in labels):
        return "图例"
    return "Legend"


def _collect_vector_domain(layer, field_name: str) -> dict[str, Any]:
    field_index = layer.fields().indexFromName(field_name)
    if field_index < 0:
        raise ValueError(f"分类字段不存在：{field_name}")
    counts: dict[tuple[str, str], dict[str, Any]] = {}
    null_count = 0
    for feature in layer.getFeatures():
        raw_value = feature[field_index]
        if _is_null(raw_value):
            null_count += 1
            continue
        value = _json_category_value(raw_value)
        key = (type(value).__name__, str(value))
        item = counts.setdefault(key, {"value": value, "count": 0})
        item["count"] += 1
        if len(counts) > MAX_CATEGORY_VALUES:
            raise ValueError(
                f"分类字段唯一非空值超过 {MAX_CATEGORY_VALUES} 个，"
                "不适合直接生成分类专题图。"
            )
    value_counts = sorted(
        counts.values(),
        key=lambda item: (-item["count"], str(item["value"])),
    )
    return {
        "available_values": [item["value"] for item in value_counts],
        "value_counts": value_counts,
        "distinct_count": len(value_counts),
        "null_count": null_count,
        "domain_complete": True,
    }


def _is_polygon_layer(layer) -> bool:
    from qgis.core import Qgis, QgsWkbTypes

    geometry_type = layer.geometryType()
    candidates = [getattr(QgsWkbTypes, "PolygonGeometry", None)]
    candidates.append(getattr(getattr(Qgis, "GeometryType", object), "Polygon", None))
    return any(candidate is not None and geometry_type == candidate for candidate in candidates)


def build_inspect_land_cover_map_inputs_tool(*, qgis_executor=None) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        layer_id = str(arguments.get("input_layer_id") or "").strip()
        boundary_layer_id = str(arguments.get("boundary_layer_id") or "").strip()
        field_name = str(arguments.get("classification_field") or "").strip()
        try:
            raster_band = int(arguments.get("raster_band") or 1)
        except (TypeError, ValueError):
            return {"success": False, "error": "raster_band 必须是正整数。"}
        if not layer_id:
            return {"success": False, "error": "必须提供 input_layer_id。"}

        def operation() -> dict[str, Any]:
            from qgis.core import QgsCoordinateTransform, QgsProject

            layer = _find_layer(layer_id)
            if not layer.isValid():
                raise ValueError(f"输入图层无效：{layer_id}")
            source_type = _layer_type_name(layer)
            result = {
                "success": True,
                "input_layer": {"id": layer.id(), "name": layer.name()},
                "source_type": source_type,
                "crs": layer.crs().authid() if layer.crs().isValid() else "",
                "scope_mode": "full_layer",
                "boundary_layer": None,
                "inspection_complete": True,
            }
            if boundary_layer_id:
                boundary = _find_layer(boundary_layer_id)
                if not boundary.isValid() or _layer_type_name(boundary) != "vector":
                    raise ValueError("裁剪范围图层必须是有效的矢量图层。")
                if not _is_polygon_layer(boundary):
                    raise ValueError("裁剪范围图层必须是面图层。")
                if boundary.featureCount() == 0:
                    raise ValueError("裁剪范围图层没有要素。")
                if not layer.crs().isValid() or not boundary.crs().isValid():
                    raise ValueError("土地覆盖图层和裁剪范围图层必须具有有效 CRS。")
                try:
                    transform = QgsCoordinateTransform(
                        boundary.crs(),
                        layer.crs(),
                        QgsProject.instance(),
                    )
                    boundary_extent = transform.transformBoundingBox(boundary.extent())
                except Exception as exc:
                    raise ValueError("无法将裁剪范围转换到土地覆盖图层 CRS。") from exc
                if not layer.extent().intersects(boundary_extent):
                    raise ValueError("裁剪范围与土地覆盖图层空间范围不相交。")
                result["scope_mode"] = "boundary_layer"
                result["boundary_layer"] = {
                    "id": boundary.id(),
                    "name": boundary.name(),
                    "crs": boundary.crs().authid(),
                    "feature_count": boundary.featureCount(),
                }
            if source_type == "vector":
                if not field_name:
                    raise ValueError("矢量土地覆盖图层必须提供 classification_field。")
                result["classification_field"] = field_name
                result["classification_domain"] = _collect_vector_domain(layer, field_name)
                return result
            if source_type == "raster":
                band_count = int(layer.bandCount())
                if raster_band < 1 or raster_band > band_count:
                    raise ValueError(f"raster_band 超出有效范围 1..{band_count}。")
                result["raster_band"] = raster_band
                result["band_count"] = band_count
                result["classification_domain"] = None
                return result
            raise ValueError("土地覆盖输入必须是矢量图层或栅格图层。")

        try:
            return _run_qgis(qgis_executor, operation)
        except (RuntimeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    return ToolEntry(
        name="inspect_land_cover_map_inputs",
        description=(
            "只读检查土地覆盖专题图输入。矢量模式返回分类字段完整真实值域，"
            "栅格模式验证分类波段；可同时验证可选面范围图层及空间相交关系。"
            "生成专题图前必须调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "input_layer_id": {"type": "string", "description": "分类影像或矢量图层 ID。"},
                "boundary_layer_id": {
                    "type": "string",
                    "description": "可选裁剪范围面图层 ID；不传表示按土地覆盖图层全图制图。",
                },
                "classification_field": {
                    "type": "string",
                    "description": "矢量分类字段；栅格模式不传。",
                },
                "raster_band": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "栅格分类波段。",
                },
            },
            "required": ["input_layer_id"],
            "additionalProperties": False,
        },
        handler=handler,
        category="land_cover_map",
        requires_confirmation=False,
        writes_project=False,
    )


def build_land_cover_map_code(arguments: dict[str, Any]) -> str:
    parameters = _normalize_parameters(arguments)
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    payload = json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
    return f"LAND_COVER_MAP_PARAMETERS_JSON = {payload!r}\n{script}"


def _expected_outputs(parameters: dict[str, Any]) -> list[dict[str, str]]:
    outputs = [dict(output) for output in EXPECTED_OUTPUTS]
    if parameters.get("boundary_layer_id"):
        clipped = (
            CLIPPED_RASTER_OUTPUT
            if parameters["source_type"] == "raster"
            else CLIPPED_VECTOR_OUTPUT
        )
        outputs.append(dict(clipped))
    return outputs


def build_generate_land_cover_map_tool(
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
            code = build_land_cover_map_code(parameters)
        except (OSError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        result = code_executor.handler(
            {
                "code": code,
                "expected_outputs": _expected_outputs(parameters),
                "timeout_seconds": int(
                    arguments.get("timeout_seconds")
                    or (1200 if parameters["boundary_layer_id"] else 300)
                ),
            }
        )
        return {
            **result,
            "map_parameters": parameters,
            "fixed_script": SCRIPT_PATH.name,
        }

    class_item = {
        "type": "object",
        "properties": {
            "value": {"oneOf": [{"type": "string"}, {"type": "number"}]},
            "label": {"type": "string"},
            "color": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0, "maximum": 255},
                "minItems": 3,
                "maxItems": 3,
            },
        },
        "required": ["value", "label", "color"],
        "additionalProperties": False,
    }
    return ToolEntry(
        name="generate_land_cover_map",
        description=(
            "使用固定 QGIS 制图脚本为分类栅格或矢量生成土地覆盖专题图；"
            "可先按行政区、研究区等面范围图层裁剪，再直接对裁剪结果制图。"
            "应用分类配色、创建含标题/图例/指南针/比例尺的打印布局、导出 PNG/PDF，"
            "并在 QGIS 中显示样式图层和布局。AI 不得传入代码。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "input_layer_id": {"type": "string"},
                "boundary_layer_id": {
                    "type": "string",
                    "description": "可选裁剪范围面图层 ID；省略时使用土地覆盖图层全范围。",
                },
                "source_type": {"type": "string", "enum": ["vector", "raster"]},
                "classification_field": {"type": "string"},
                "raster_band": {"type": "integer", "minimum": 1, "default": 1},
                "class_mapping": {
                    "type": "array",
                    "items": class_item,
                    "minItems": 1,
                    "maxItems": 100,
                    "description": "类别值、图例标签和 RGB 颜色；不传时使用内置 1–9 类。",
                },
                "allow_unmapped_values": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否允许矢量分类字段存在未配置类别；默认禁止。",
                },
                "title": {"type": "string", "default": "土地覆盖专题图"},
                "subtitle": {"type": "string", "default": ""},
                "legend_title": {
                    "type": "string",
                    "description": "可选；不传时根据分类标签语言自动使用图例、Legend、凡例或 범례。",
                },
                "orientation": {
                    "type": "string",
                    "enum": ["landscape", "portrait"],
                    "default": "landscape",
                },
                "legend_position": {
                    "type": "string",
                    "enum": ["right", "bottom"],
                    "default": "right",
                },
                "show_legend": {"type": "boolean", "default": True},
                "show_north_arrow": {"type": "boolean", "default": True},
                "show_scale_bar": {"type": "boolean", "default": True},
                "title_font_size": {
                    "type": "number",
                    "minimum": 8,
                    "maximum": 48,
                    "default": 20,
                },
                "map_margin_percent": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 50,
                    "default": 5,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3600,
                    "default": 1200,
                },
            },
            "required": ["input_layer_id", "source_type", "title"],
            "additionalProperties": False,
        },
        handler=handler,
        category="land_cover_map",
        requires_confirmation=True,
        writes_project=True,
    )


def _normalize_parameters(arguments: dict[str, Any]) -> dict[str, Any]:
    layer_id = str(arguments.get("input_layer_id") or "").strip()
    boundary_layer_id = str(arguments.get("boundary_layer_id") or "").strip()
    source_type = str(arguments.get("source_type") or "").strip()
    if not layer_id:
        raise ValueError("缺少必要参数：input_layer_id")
    if source_type not in {"vector", "raster"}:
        raise ValueError("source_type 必须是 vector 或 raster。")
    field_name = str(arguments.get("classification_field") or "").strip()
    if source_type == "vector" and not field_name:
        raise ValueError("矢量模式必须提供 classification_field。")
    if source_type == "raster" and field_name:
        raise ValueError("栅格模式不得提供 classification_field。")
    try:
        raster_band = int(arguments.get("raster_band") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("raster_band 必须是正整数。") from exc
    if raster_band < 1:
        raise ValueError("raster_band 必须是正整数。")

    raw_mapping = arguments.get("class_mapping") or DEFAULT_CLASS_MAPPING
    if not isinstance(raw_mapping, list) or not 1 <= len(raw_mapping) <= 100:
        raise ValueError("class_mapping 必须包含 1 到 100 个类别。")
    mapping = []
    seen_values = set()
    for index, item in enumerate(raw_mapping, start=1):
        if not isinstance(item, dict) or "value" not in item:
            raise ValueError(f"第 {index} 个类别缺少 value。")
        value = item["value"]
        if not isinstance(value, str | int | float) or isinstance(value, bool):
            raise ValueError(f"第 {index} 个类别 value 必须是字符串或数值。")
        key = (type(value).__name__, str(value))
        if key in seen_values:
            raise ValueError(f"class_mapping 包含重复类别值：{value}")
        seen_values.add(key)
        label = str(item.get("label") or "").strip()
        color = item.get("color")
        if not label:
            raise ValueError(f"第 {index} 个类别缺少 label。")
        if (
            not isinstance(color, list)
            or len(color) != 3
            or any(not isinstance(channel, int) or not 0 <= channel <= 255 for channel in color)
        ):
            raise ValueError(f"第 {index} 个类别 color 必须是三个 0–255 整数。")
        mapping.append({"value": value, "label": label, "color": list(color)})

    orientation = str(arguments.get("orientation") or "landscape").strip()
    legend_position = str(arguments.get("legend_position") or "right").strip()
    if orientation not in {"landscape", "portrait"}:
        raise ValueError("orientation 必须是 landscape 或 portrait。")
    if legend_position not in {"right", "bottom"}:
        raise ValueError("legend_position 必须是 right 或 bottom。")
    title = str(arguments.get("title") or "土地覆盖专题图").strip()
    if not title:
        raise ValueError("title 不能为空。")
    title_font_size = float(arguments.get("title_font_size") or 20)
    margin = float(arguments.get("map_margin_percent") or 5)
    if not 8 <= title_font_size <= 48:
        raise ValueError("title_font_size 必须在 8–48 之间。")
    if not 0 <= margin <= 50:
        raise ValueError("map_margin_percent 必须在 0–50 之间。")

    legend_title = str(arguments.get("legend_title") or "").strip()
    if not legend_title:
        legend_title = _infer_legend_title(mapping)

    return {
        "input_layer_id": layer_id,
        "boundary_layer_id": boundary_layer_id or None,
        "scope_mode": "boundary_layer" if boundary_layer_id else "full_layer",
        "source_type": source_type,
        "classification_field": field_name or None,
        "raster_band": raster_band,
        "class_mapping": mapping,
        "allow_unmapped_values": bool(arguments.get("allow_unmapped_values", False)),
        "title": title,
        "subtitle": str(arguments.get("subtitle") or "").strip(),
        "legend_title": legend_title,
        "orientation": orientation,
        "legend_position": legend_position,
        "show_legend": bool(arguments.get("show_legend", True)),
        "show_north_arrow": bool(arguments.get("show_north_arrow", True)),
        "show_scale_bar": bool(arguments.get("show_scale_bar", True)),
        "title_font_size": title_font_size,
        "map_margin_percent": margin,
    }
