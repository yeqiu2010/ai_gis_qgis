"""QGIS layer management tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...database.session_db import SessionDB
from ..context.crs_info import describe_crs
from .registry import ToolEntry

VECTOR_EXTENSIONS = {
    ".geojson",
    ".gpkg",
    ".json",
    ".kml",
    ".shp",
    ".sqlite",
}
RASTER_EXTENSIONS = {
    ".asc",
    ".cog",
    ".img",
    ".tif",
    ".tiff",
}


def _run_qgis(qgis_executor, func):
    if qgis_executor is not None:
        return qgis_executor(func)
    return func()

def _qgis_classes() -> dict[str, Any]:
    try:
        from qgis.core import (
            QgsCoordinateTransformContext,
            QgsProject,
            QgsRasterFileWriter,
            QgsRasterLayer,
            QgsReadWriteContext,
            QgsVectorFileWriter,
            QgsVectorLayer,
        )
    except Exception as exc:
        raise RuntimeError("当前运行环境未提供 PyQGIS，图层操作只能在 QGIS 内执行。") from exc
    return {
        "QgsCoordinateTransformContext": QgsCoordinateTransformContext,
        "QgsProject": QgsProject,
        "QgsRasterFileWriter": QgsRasterFileWriter,
        "QgsRasterLayer": QgsRasterLayer,
        "QgsReadWriteContext": QgsReadWriteContext,
        "QgsVectorFileWriter": QgsVectorFileWriter,
        "QgsVectorLayer": QgsVectorLayer,
    }


def _project():
    return _qgis_classes()["QgsProject"].instance()


def _layer_type_name(layer) -> str:
    try:
        from qgis.core import Qgis

        if layer.type() == Qgis.LayerType.Vector:
            return "vector"
        if layer.type() == Qgis.LayerType.Raster:
            return "raster"
    except Exception:
        if layer.type() == 0:
            return "vector"
        if layer.type() == 1:
            return "raster"
    return str(layer.type())


def _safe_feature_count(layer) -> int | None:
    if _layer_type_name(layer) != "vector":
        return None
    try:
        count = layer.featureCount()
    except Exception:
        return None
    return int(count) if count is not None else None


def _extent_to_dict(layer) -> dict[str, float] | None:
    try:
        extent = layer.extent()
        if extent.isNull():
            return None
        return {
            "xmin": extent.xMinimum(),
            "ymin": extent.yMinimum(),
            "xmax": extent.xMaximum(),
            "ymax": extent.yMaximum(),
        }
    except Exception:
        return None


def _layer_summary(layer) -> dict[str, Any]:
    crs = layer.crs()
    return {
        "id": layer.id(),
        "name": layer.name(),
        "type": _layer_type_name(layer),
        "source": layer.source(),
        **describe_crs(crs),
        "feature_count": _safe_feature_count(layer),
        "extent": _extent_to_dict(layer),
        "is_valid": layer.isValid(),
    }


def _find_layer(identifier: str):
    identifier = str(identifier or "").strip()
    if not identifier:
        raise ValueError("必须提供 layer_id 或 layer_name。")

    project = _project()
    layer = project.mapLayer(identifier)
    if layer is not None:
        return layer

    matches = [
        candidate
        for candidate in project.mapLayers().values()
        if candidate.name() == identifier or candidate.name().lower() == identifier.lower()
    ]
    if not matches:
        aliases = _layer_name_aliases(identifier)
        matches = [
            candidate
            for candidate in project.mapLayers().values()
            if candidate.name().casefold() in aliases
        ]
    if not matches:
        raise ValueError(f"找不到图层：{identifier}")
    if len(matches) > 1:
        names = ", ".join(f"{layer.name()}({layer.id()})" for layer in matches)
        raise ValueError(f"存在多个同名图层，请使用 layer_id：{names}")
    return matches[0]


def _layer_name_aliases(identifier: str) -> set[str]:
    """Extract a leaf layer name from UI labels such as 'database — layer'."""
    aliases = set()
    for separator in (" — ", " – ", " - ", "::"):
        if separator in identifier:
            leaf = identifier.rsplit(separator, 1)[-1].strip()
            if leaf:
                aliases.add(leaf.casefold())
    return aliases


def _inspect_layer(layer, sample_limit: int = 5) -> dict[str, Any]:
    summary = _layer_summary(layer)
    fields = []
    samples = []
    if summary["type"] == "vector":
        field_names = [field.name() for field in layer.fields()]
        fields = [{"name": field.name(), "type": field.typeName()} for field in layer.fields()]
        bounded_sample_limit = max(0, min(int(sample_limit), 20))
        for index, feature in enumerate(layer.getFeatures()):
            if index >= bounded_sample_limit:
                break
            samples.append(dict(zip(field_names, feature.attributes(), strict=False)))
    return {"layer": summary, "fields": fields, "sample_features": samples}


def _infer_layer_type(source: str, requested: str | None) -> str:
    if requested in {"vector", "raster"}:
        return requested
    suffix = Path(source).suffix.lower()
    if suffix in RASTER_EXTENSIONS:
        return "raster"
    if suffix in VECTOR_EXTENSIONS:
        return "vector"
    return "vector"


def _normalize_source(value: Any) -> str:
    return str(value or "").strip().strip("\"'")


def _snapshot(session_db: SessionDB | None, session_id: str, layer, action: str) -> None:
    if session_db is None:
        return
    summary = _layer_summary(layer)
    session_db.log_layer_snapshot(
        session_id,
        layer_id=summary["id"],
        layer_name=summary["name"],
        source_path=summary["source"],
        crs=summary["crs"],
        feature_count=summary["feature_count"],
        action=action,
    )


def build_list_layers_tool(qgis_executor=None) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        def operation():
            layers = [_layer_summary(layer) for layer in _project().mapLayers().values()]
            return {"layers": layers, "count": len(layers)}

        return _run_qgis(qgis_executor, operation)

    return ToolEntry(
        name="list_layers",
        description="列出当前 QGIS 工程中的图层，包含名称、ID、类型、数据源、CRS、范围和要素数。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        category="data",
    )


def build_inspect_layer_tool(qgis_executor=None) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        def operation():
            layer = _find_layer(str(arguments.get("layer_id") or arguments.get("layer_name") or ""))
            return _inspect_layer(layer, int(arguments.get("sample_limit", 5)))

        return _run_qgis(qgis_executor, operation)

    return ToolEntry(
        name="inspect_layer",
        description="查看指定图层的字段、范围、CRS、要素数和少量样例属性。",
        parameters={
            "type": "object",
            "properties": {
                "layer_id": {"type": "string", "description": "QGIS 图层 ID。"},
                "layer_name": {"type": "string", "description": "QGIS 图层名称。"},
                "sample_limit": {
                    "type": "integer",
                    "description": "返回的样例要素数量，默认 5，最大 20。",
                    "minimum": 0,
                    "maximum": 20,
                },
            },
            "additionalProperties": False,
        },
        handler=handler,
        category="data",
    )


def build_inspect_layers_tool(qgis_executor=None) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        def operation():
            identifiers = arguments.get("layers") or []
            if not isinstance(identifiers, list) or not identifiers:
                raise ValueError("必须提供 layers，且至少包含一个图层名称或 ID。")
            sample_limit = int(arguments.get("sample_limit", 5))
            inspected = []
            errors = []
            for identifier in identifiers[:20]:
                try:
                    layer = _find_layer(str(identifier))
                    inspected.append(_inspect_layer(layer, sample_limit))
                except Exception as exc:
                    errors.append({"layer": str(identifier), "error": str(exc)})
            return {
                "layers": inspected,
                "errors": errors,
                "count": len(inspected),
                "requested_count": len(identifiers),
                "success": not errors,
            }

        return _run_qgis(qgis_executor, operation)

    return ToolEntry(
        name="inspect_layers",
        description="一次性查看多个图层的字段、范围、CRS、要素数和少量样例属性。复杂 Pipeline 应优先用它减少多次 inspect_layer 调用。",
        parameters={
            "type": "object",
            "properties": {
                "layers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "QGIS 图层名称或 ID 列表。",
                    "minItems": 1,
                    "maxItems": 20,
                },
                "sample_limit": {
                    "type": "integer",
                    "description": "每个矢量图层返回的样例要素数量，默认 5，最大 20。",
                    "minimum": 0,
                    "maximum": 20,
                },
            },
            "required": ["layers"],
            "additionalProperties": False,
        },
        handler=handler,
        category="data",
    )


def build_load_layer_tool(
    session_db: SessionDB | None,
    session_id: str,
    qgis_executor=None,
) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        def operation():
            classes = _qgis_classes()
            source = _normalize_source(arguments.get("source"))
            if not source:
                raise ValueError("必须提供 source。")
            name = str(arguments.get("name") or Path(source).stem or "layer")
            layer_type = _infer_layer_type(source, arguments.get("layer_type"))

            if layer_type == "raster":
                layer = classes["QgsRasterLayer"](source, name)
            else:
                provider = str(arguments.get("provider") or "ogr")
                layer = classes["QgsVectorLayer"](source, name, provider)

            if not layer.isValid():
                raise ValueError(f"图层无效或无法加载：{source}")

            classes["QgsProject"].instance().addMapLayer(layer)
            _snapshot(session_db, session_id, layer, "added")
            return {"layer": _layer_summary(layer)}

        return _run_qgis(qgis_executor, operation)

    return ToolEntry(
        name="load_layer",
        description="加载矢量或栅格数据到当前 QGIS 工程。支持常见本地文件和 OGR 数据源。",
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": (
                        "从用户自然语言中提取出的真实数据源路径或 QGIS 数据源 URI。"
                        "不要包含“数据”“图层”“加载”等说明性文字。"
                    ),
                },
                "name": {"type": "string", "description": "加载后的图层名称。"},
                "layer_type": {
                    "type": "string",
                    "enum": ["vector", "raster", "auto"],
                    "description": "图层类型，默认 auto。",
                },
                "provider": {"type": "string", "description": "矢量 provider，默认 ogr。"},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
        handler=handler,
        category="data",
        writes_project=True,
    )


def build_remove_layer_tool(
    session_db: SessionDB | None,
    session_id: str,
    qgis_executor=None,
) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        def operation():
            layer = _find_layer(str(arguments.get("layer_id") or arguments.get("layer_name") or ""))
            summary = _layer_summary(layer)
            _snapshot(session_db, session_id, layer, "removed")
            _project().removeMapLayer(layer.id())
            return {"removed_layer": summary}

        return _run_qgis(qgis_executor, operation)

    return ToolEntry(
        name="remove_layer",
        description="从当前 QGIS 工程移除图层，不删除源数据文件。",
        parameters={
            "type": "object",
            "properties": {
                "layer_id": {"type": "string", "description": "QGIS 图层 ID。"},
                "layer_name": {"type": "string", "description": "QGIS 图层名称。"},
            },
            "additionalProperties": False,
        },
        handler=handler,
        category="data",
        requires_confirmation=True,
        destructive=True,
        writes_project=True,
    )


def build_zoom_to_layer_tool(iface=None, qgis_executor=None) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        def operation():
            if iface is None:
                raise RuntimeError("当前没有 QGIS iface，无法控制地图画布。")
            layer = _find_layer(str(arguments.get("layer_id") or arguments.get("layer_name") or ""))
            canvas_extent = _zoom_canvas_to_layer(layer, iface)
            return {
                "layer": _layer_summary(layer),
                "canvas_extent": canvas_extent,
                "zoomed": True,
            }

        return _run_qgis(qgis_executor, operation)

    return ToolEntry(
        name="zoom_to_layer",
        description="将 QGIS 地图画布缩放到指定图层范围。",
        parameters={
            "type": "object",
            "properties": {
                "layer_id": {"type": "string", "description": "QGIS 图层 ID。"},
                "layer_name": {"type": "string", "description": "QGIS 图层名称。"},
            },
            "additionalProperties": False,
        },
        handler=handler,
        category="layer",
    )


def _zoom_canvas_to_layer(layer, iface) -> dict[str, float]:
    """Zoom in canvas CRS without changing the user's map rotation."""
    if iface is None:
        raise RuntimeError("当前没有 QGIS iface，无法控制地图画布。")
    update_extents = getattr(layer, "updateExtents", None)
    if callable(update_extents):
        update_extents()
    canvas = iface.mapCanvas()
    rotation_getter = getattr(canvas, "rotation", None)
    rotation_setter = getattr(canvas, "setRotation", None)
    original_rotation = (
        float(rotation_getter()) if callable(rotation_getter) else None
    )
    extent = layer.extent()
    layer_crs = layer.crs()
    canvas_crs = canvas.mapSettings().destinationCrs()
    if (
        layer_crs.isValid()
        and canvas_crs.isValid()
        and layer_crs != canvas_crs
    ):
        from qgis.core import QgsCoordinateTransform, QgsProject

        extent = QgsCoordinateTransform(
            layer_crs,
            canvas_crs,
            QgsProject.instance(),
        ).transformBoundingBox(extent)
    canvas.setExtent(extent)
    if original_rotation is not None and callable(rotation_setter):
        current_rotation = float(rotation_getter()) if callable(rotation_getter) else None
        if current_rotation != original_rotation:
            rotation_setter(original_rotation)
    canvas.refresh()
    return {
        "xmin": float(extent.xMinimum()),
        "ymin": float(extent.yMinimum()),
        "xmax": float(extent.xMaximum()),
        "ymax": float(extent.yMaximum()),
    }


def build_set_style_tool(
    session_db: SessionDB | None,
    session_id: str,
    qgis_executor=None,
) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        def operation():
            layer = _find_layer(str(arguments.get("layer_id") or arguments.get("layer_name") or ""))
            qml_path = str(arguments.get("qml_path") or "").strip()
            if not qml_path:
                raise ValueError("当前 set_style 仅支持 qml_path。")
            if not Path(qml_path).exists():
                raise ValueError(f"QML 样式文件不存在：{qml_path}")
            style_result = layer.loadNamedStyle(qml_path)
            if isinstance(style_result, tuple) and len(style_result) == 2:
                first, second = style_result
                if isinstance(first, bool):
                    ok, message = first, second
                else:
                    message, ok = first, second
            else:
                ok, message = bool(style_result), ""
            if not ok:
                raise ValueError(message or f"加载 QML 样式失败：{qml_path}")
            layer.triggerRepaint()
            _snapshot(session_db, session_id, layer, "styled")
            return {"layer": _layer_summary(layer), "qml_path": qml_path}

        return _run_qgis(qgis_executor, operation)

    return ToolEntry(
        name="set_style",
        description="给指定图层加载 QML 样式文件。",
        parameters={
            "type": "object",
            "properties": {
                "layer_id": {"type": "string", "description": "QGIS 图层 ID。"},
                "layer_name": {"type": "string", "description": "QGIS 图层名称。"},
                "qml_path": {"type": "string", "description": "QML 样式文件路径。"},
            },
            "required": ["qml_path"],
            "additionalProperties": False,
        },
        handler=handler,
        category="layer",
        writes_project=True,
    )


def build_export_layer_tool(
    session_db: SessionDB | None,
    session_id: str,
    qgis_executor=None,
) -> ToolEntry:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        def operation():
            classes = _qgis_classes()
            layer = _find_layer(str(arguments.get("layer_id") or arguments.get("layer_name") or ""))
            output_path = str(arguments.get("output_path") or "").strip()
            if not output_path:
                raise ValueError("必须提供 output_path。")
            Path(output_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

            if _layer_type_name(layer) == "vector":
                driver = str(arguments.get("driver") or _driver_for_output(output_path))
                options = classes["QgsVectorFileWriter"].SaveVectorOptions()
                options.driverName = driver
                options.fileEncoding = "UTF-8"
                result = classes["QgsVectorFileWriter"].writeAsVectorFormatV3(
                    layer,
                    output_path,
                    classes["QgsCoordinateTransformContext"](),
                    options,
                )
                error_code = result[0] if isinstance(result, tuple) else result
                if int(error_code) != 0:
                    raise RuntimeError(f"导出矢量图层失败：{result}")
            else:
                pipe = layer.pipe()
                writer = classes["QgsRasterFileWriter"](output_path)
                result = writer.writeRaster(
                    pipe,
                    layer.width(),
                    layer.height(),
                    layer.extent(),
                    layer.crs(),
                    classes["QgsCoordinateTransformContext"](),
                )
                if int(result) != 0:
                    raise RuntimeError(f"导出栅格图层失败，错误码：{result}")

            _snapshot(session_db, session_id, layer, "exported")
            return {"layer": _layer_summary(layer), "output_path": output_path}

        return _run_qgis(qgis_executor, operation)

    return ToolEntry(
        name="export_layer",
        description="导出指定图层到文件。矢量支持 GeoPackage、GeoJSON、Shapefile 等，栅格按 GDAL writer 导出。",
        parameters={
            "type": "object",
            "properties": {
                "layer_id": {"type": "string", "description": "QGIS 图层 ID。"},
                "layer_name": {"type": "string", "description": "QGIS 图层名称。"},
                "output_path": {"type": "string", "description": "导出目标文件路径。"},
                "driver": {"type": "string", "description": "可选 GDAL/OGR driver 名称。"},
            },
            "required": ["output_path"],
            "additionalProperties": False,
        },
        handler=handler,
        category="layer",
        requires_confirmation=True,
    )


def _driver_for_output(output_path: str) -> str:
    suffix = Path(output_path).suffix.lower()
    if suffix == ".gpkg":
        return "GPKG"
    if suffix in {".geojson", ".json"}:
        return "GeoJSON"
    if suffix == ".shp":
        return "ESRI Shapefile"
    if suffix == ".kml":
        return "KML"
    return "GPKG"


def build_layer_tools(
    *,
    session_db: SessionDB | None,
    session_id: str,
    iface=None,
    qgis_executor=None,
) -> list[ToolEntry]:
    return [
        build_list_layers_tool(qgis_executor),
        build_inspect_layer_tool(qgis_executor),
        build_inspect_layers_tool(qgis_executor),
        build_load_layer_tool(session_db, session_id, qgis_executor),
        build_remove_layer_tool(session_db, session_id, qgis_executor),
        build_zoom_to_layer_tool(iface, qgis_executor),
        build_set_style_tool(session_db, session_id, qgis_executor),
        build_export_layer_tool(session_db, session_id, qgis_executor),
    ]
