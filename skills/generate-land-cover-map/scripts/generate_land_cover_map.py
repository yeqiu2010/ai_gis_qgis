"""Deterministic QGIS script for a land-cover thematic map."""

import json
import math
from pathlib import Path

import processing
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsLayoutExporter,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemPicture,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsLegendRenderer,
    QgsLegendStyle,
    QgsMapLayerLegendUtils,
    QgsPalettedRasterRenderer,
    QgsPrintLayout,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsRendererCategory,
    QgsSymbol,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFont

parameters = json.loads(LAND_COVER_MAP_PARAMETERS_JSON)  # noqa: F821
workspace = Path(QGIS_AGENT_WORKSPACE)  # noqa: F821
png_output = workspace / "land_cover_map.png"
pdf_output = workspace / "land_cover_map.pdf"
clipped_raster_output = workspace / "land_cover_clipped.tif"
clipped_vector_output = workspace / "land_cover_clipped.gpkg"
project = QgsProject.instance()
source_layer = project.mapLayer(parameters["input_layer_id"])
if source_layer is None or not source_layer.isValid():
    raise ValueError(f"找不到有效的土地覆盖图层：{parameters['input_layer_id']}")
layer = source_layer
boundary_layer = None
clip_output = None


def color_from_item(item):
    return QColor(item["color"][0], item["color"][1], item["color"][2])


def layout_font(point_size, *, bold=False):
    font = QFont("Sans Serif", int(point_size))
    font.setBold(bool(bold))
    return font


def horizontal_center_alignment():
    for enum_name in ("AlignmentFlag", "Alignment"):
        enum_type = getattr(Qt, enum_name, None)
        alignment = getattr(enum_type, "AlignHCenter", None)
        if alignment is not None:
            return alignment
    alignment = getattr(Qt, "AlignHCenter", None)
    if alignment is None:
        raise ValueError("当前 Qt 环境未提供水平居中对齐枚举。")
    return alignment


def nice_scale_distance(value):
    if not math.isfinite(value) or value <= 0:
        raise ValueError("无法根据当前地图范围计算有效比例尺分段。")
    exponent = math.floor(math.log10(value))
    power = 10.0**exponent
    fraction = value / power
    if fraction < 1.5:
        nice_fraction = 1.0
    elif fraction < 3.5:
        nice_fraction = 2.0
    elif fraction < 7.5:
        nice_fraction = 5.0
    else:
        nice_fraction = 10.0
    return nice_fraction * power


def legend_component(name):
    modern_enum = getattr(Qgis, "LegendComponent", None)
    value = getattr(modern_enum, name, None)
    if value is not None:
        return value
    legacy_enum = getattr(QgsLegendStyle, "Style", None)
    value = getattr(legacy_enum, name, None)
    if value is not None:
        return value
    value = getattr(QgsLegendStyle, name, None)
    if value is None:
        raise ValueError(f"当前 QGIS 环境未提供图例组件枚举：{name}")
    return value


def distance_unit(name):
    modern_enum = getattr(Qgis, "DistanceUnit", None)
    value = getattr(modern_enum, name, None)
    if value is not None:
        return value
    value = getattr(QgsUnitTypes, f"Distance{name}", None)
    if value is None:
        raise ValueError(f"当前 QGIS 环境未提供距离单位：{name}")
    return value


def is_null(value):
    if value is None:
        return True
    method = getattr(value, "isNull", None)
    return bool(method()) if callable(method) else False


def vector_category_value(field, value):
    type_name = str(field.typeName() or "").strip().casefold()
    if type_name in {
        "int",
        "int2",
        "int4",
        "int8",
        "integer",
        "integer64",
        "long",
        "longlong",
        "short",
        "uint",
        "uint64",
    }:
        return int(value)
    if type_name in {"decimal", "double", "float", "numeric", "real"}:
        return float(value)
    return str(value)


def is_polygon_layer(candidate):
    geometry_type = candidate.geometryType()
    polygon_type = getattr(QgsWkbTypes, "PolygonGeometry", None)
    if polygon_type is not None:
        return geometry_type == polygon_type
    modern_type = getattr(
        getattr(Qgis, "GeometryType", object),
        "Polygon",
        None,
    )
    return modern_type is not None and geometry_type == modern_type


if parameters["boundary_layer_id"]:
    boundary_layer = project.mapLayer(parameters["boundary_layer_id"])
    if boundary_layer is None or not boundary_layer.isValid():
        raise ValueError(f"找不到有效的裁剪范围图层：{parameters['boundary_layer_id']}")
    if not is_polygon_layer(boundary_layer):
        raise ValueError("裁剪范围图层必须是面图层。")
    clipped_name = f"{source_layer.name()} - {boundary_layer.name()}裁剪"
    if parameters["source_type"] == "raster":
        processing.run(
            "gdal:cliprasterbymasklayer",
            {
                "INPUT": source_layer,
                "MASK": boundary_layer,
                "SOURCE_CRS": source_layer.crs(),
                "TARGET_CRS": source_layer.crs(),
                "TARGET_EXTENT": None,
                "NODATA": None,
                "ALPHA_BAND": True,
                "CROP_TO_CUTLINE": True,
                "KEEP_RESOLUTION": True,
                "SET_RESOLUTION": False,
                "X_RESOLUTION": None,
                "Y_RESOLUTION": None,
                "MULTITHREADING": True,
                "OPTIONS": "",
                "DATA_TYPE": 0,
                "EXTRA": "",
                "OUTPUT": str(clipped_raster_output),
            },
        )
        layer = QgsRasterLayer(str(clipped_raster_output), clipped_name)
        clip_output = clipped_raster_output
    else:
        processing.run(
            "native:clip",
            {
                "INPUT": source_layer,
                "OVERLAY": boundary_layer,
                "OUTPUT": str(clipped_vector_output),
            },
        )
        layer = QgsVectorLayer(str(clipped_vector_output), clipped_name, "ogr")
        clip_output = clipped_vector_output
    if not layer.isValid():
        raise ValueError("土地覆盖裁剪结果无效，无法继续制图。")


if parameters["source_type"] == "vector":
    field_name = parameters["classification_field"]
    field_index = layer.fields().indexFromName(field_name)
    if field_index < 0:
        raise ValueError(f"分类字段不存在：{field_name}")
    field = layer.fields().field(field_index)
    categories = []
    mapped_values = set()
    for item in parameters["class_mapping"]:
        category_value = vector_category_value(field, item["value"])
        mapped_values.add(category_value)
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        if symbol is None:
            raise ValueError("无法为输入矢量几何创建分类符号。")
        symbol.setColor(color_from_item(item))
        categories.append(
            QgsRendererCategory(
                category_value,
                symbol,
                item["label"],
            )
        )
    if not parameters["allow_unmapped_values"]:
        actual_values = {
            vector_category_value(field, feature[field_index])
            for feature in layer.getFeatures()
            if not is_null(feature[field_index])
        }
        missing_values = sorted(
            (value for value in actual_values if value not in mapped_values),
            key=str,
        )
        if missing_values:
            raise ValueError(
                "class_mapping 未覆盖矢量分类字段值："
                + "、".join(str(value) for value in missing_values[:50])
            )
    renderer = QgsCategorizedSymbolRenderer(field_name, categories)
    layer.setRenderer(renderer)
elif parameters["source_type"] == "raster":
    band = parameters["raster_band"]
    if band < 1 or band > layer.bandCount():
        raise ValueError(f"栅格分类波段超出范围：{band}")
    classes = []
    for item in parameters["class_mapping"]:
        try:
            value = float(item["value"])
        except (TypeError, ValueError) as exc:
            raise ValueError("栅格 class_mapping 的 value 必须是数值。") from exc
        classes.append(
            QgsPalettedRasterRenderer.Class(value, color_from_item(item), item["label"])
        )
    layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), band, classes))
else:
    raise ValueError("source_type 必须是 vector 或 raster。")

if layer is not source_layer:
    project.addMapLayer(layer)
    source_node = project.layerTreeRoot().findLayer(source_layer.id())
    if source_node is not None:
        source_node.setItemVisibilityChecked(False)

layer.triggerRepaint()
layer_node = project.layerTreeRoot().findLayer(layer.id())
if layer_node is not None:
    layer_node.setItemVisibilityChecked(True)

layout_name = f"土地覆盖专题图 - {layer.name()}"
layout_manager = project.layoutManager()
existing_layout = layout_manager.layoutByName(layout_name)
if existing_layout is not None:
    layout_manager.removeLayout(existing_layout)

layout = QgsPrintLayout(project)
layout.initializeDefaults()
layout.setName(layout_name)
page = layout.pageCollection().page(0)
if parameters["orientation"] == "portrait":
    page_width, page_height = 210.0, 297.0
else:
    page_width, page_height = 297.0, 210.0
page.setPageSize(QgsLayoutSize(page_width, page_height, QgsUnitTypes.LayoutMillimeters))

title = QgsLayoutItemLabel(layout)
title.setText(parameters["title"])
title.setFont(layout_font(parameters["title_font_size"], bold=True))
title.setHAlign(horizontal_center_alignment())
title.attemptMove(QgsLayoutPoint(10, 5, QgsUnitTypes.LayoutMillimeters))
title.attemptResize(
    QgsLayoutSize(page_width - 20, 12, QgsUnitTypes.LayoutMillimeters)
)
layout.addLayoutItem(title)

subtitle_height = 0.0
if parameters["subtitle"]:
    subtitle = QgsLayoutItemLabel(layout)
    subtitle.setText(parameters["subtitle"])
    subtitle.setFont(layout_font(10))
    subtitle.setHAlign(horizontal_center_alignment())
    subtitle.attemptMove(QgsLayoutPoint(10, 17, QgsUnitTypes.LayoutMillimeters))
    subtitle.attemptResize(
        QgsLayoutSize(page_width - 20, 7, QgsUnitTypes.LayoutMillimeters)
    )
    layout.addLayoutItem(subtitle)
    subtitle_height = 7.0

map_top = 24.0 + subtitle_height
if parameters["legend_position"] == "bottom" and parameters["show_legend"]:
    map_x, map_y = 12.0, map_top
    map_width, map_height = page_width - 24.0, page_height - map_top - 54.0
else:
    map_x, map_y = 12.0, map_top
    reserve_right = 58.0 if parameters["show_legend"] else 16.0
    map_width, map_height = page_width - map_x - reserve_right, page_height - map_top - 14.0

map_item = QgsLayoutItemMap(layout)
map_item.setLayers([layer])
map_item.setCrs(layer.crs())
extent = QgsRectangle(layer.extent())
if extent.isNull() or extent.width() <= 0 or extent.height() <= 0:
    raise ValueError("输入图层范围无效，无法生成专题图。")
grow_by = max(extent.width(), extent.height()) * parameters["map_margin_percent"] / 100.0
if grow_by > 0:
    extent.grow(grow_by)
map_item.setFrameEnabled(True)
map_item.setBackgroundEnabled(True)
map_item.setBackgroundColor(QColor(255, 255, 255))
map_item.attemptMove(QgsLayoutPoint(map_x, map_y, QgsUnitTypes.LayoutMillimeters))
map_item.attemptResize(
    QgsLayoutSize(map_width, map_height, QgsUnitTypes.LayoutMillimeters)
)
map_item.zoomToExtent(extent)
layout.addLayoutItem(map_item)
map_item.refresh()

if parameters["show_legend"]:
    legend = QgsLayoutItemLegend(layout)
    legend.setTitle(parameters["legend_title"])
    legend.setLinkedMap(map_item)
    manual_mode = getattr(getattr(Qgis, "LegendSyncMode", None), "Manual", None)
    if manual_mode is not None and hasattr(legend, "setSyncMode"):
        legend.setSyncMode(manual_mode)
    else:
        legend.setAutoUpdateModel(False)
    legend_model = legend.model()
    legend_root = legend_model.rootGroup()
    legend_root.removeAllChildren()
    legend_layer_node = legend_root.addLayer(layer)
    legend_layer_node.setUseLayerName(False)
    legend_layer_node.setName("")
    legend_layer_node.setCustomProperty("legend/title-label", "")
    QgsLegendRenderer.setNodeLegendStyle(
        legend_layer_node,
        legend_component("Hidden"),
    )
    legend_model.refreshLayerLegend(legend_layer_node)
    legend_nodes = legend_model.layerLegendNodes(legend_layer_node)
    category_count = len(parameters["class_mapping"])
    if len(legend_nodes) < category_count:
        raise ValueError("图例分类节点数量少于 class_mapping，无法生成精简图例。")
    first_category_index = len(legend_nodes) - category_count
    legend_node_indices = list(range(first_category_index, len(legend_nodes)))
    QgsMapLayerLegendUtils.setLegendNodeOrder(
        legend_layer_node,
        legend_node_indices,
    )
    for original_index, item in zip(
        legend_node_indices,
        parameters["class_mapping"],
        strict=True,
    ):
        QgsMapLayerLegendUtils.setLegendNodeUserLabel(
            legend_layer_node,
            original_index,
            item["label"],
        )
    legend_model.refreshLayerLegend(legend_layer_node)
    legend.setLegendFilterByMapEnabled(True)
    if parameters["legend_position"] == "bottom":
        legend.setColumnCount(3)
        legend.setSplitLayer(True)
        legend.attemptMove(
            QgsLayoutPoint(12, page_height - 45, QgsUnitTypes.LayoutMillimeters)
        )
        legend.attemptResize(
            QgsLayoutSize(page_width - 24, 36, QgsUnitTypes.LayoutMillimeters)
        )
    else:
        legend.attemptMove(
            QgsLayoutPoint(page_width - 52, map_y, QgsUnitTypes.LayoutMillimeters)
        )
        legend.attemptResize(
            QgsLayoutSize(45, map_height, QgsUnitTypes.LayoutMillimeters)
        )
    layout.addLayoutItem(legend)

if parameters["show_north_arrow"]:
    arrow_candidates = [
        Path(QgsApplication.pkgDataPath()) / "svg" / "arrows" / "NorthArrow_04.svg"
    ]
    arrow_candidates.extend(
        Path(svg_path) / "arrows" / "NorthArrow_04.svg"
        for svg_path in QgsApplication.svgPaths()
    )
    arrow_path = next((path for path in arrow_candidates if path.exists()), None)
    if arrow_path is None:
        raise ValueError("QGIS SVG 目录中找不到 arrows/NorthArrow_04.svg。")
    arrow_x = map_x + map_width - 17
    arrow_y = map_y + 5
    north_arrow = QgsLayoutItemPicture(layout)
    north_arrow.setPicturePath(str(arrow_path))
    north_arrow.attemptMove(
        QgsLayoutPoint(arrow_x, arrow_y, QgsUnitTypes.LayoutMillimeters)
    )
    north_arrow.attemptResize(
        QgsLayoutSize(12, 18, QgsUnitTypes.LayoutMillimeters)
    )
    layout.addLayoutItem(north_arrow)

if parameters["show_scale_bar"]:
    scale_bar = QgsLayoutItemScaleBar(layout)
    scale_bar.setStyle("Single Box")
    scale_bar.setLinkedMap(map_item)
    map_scale = float(map_item.scale())
    if not math.isfinite(map_scale) or map_scale <= 0:
        raise ValueError("布局地图比例无效，无法创建比例尺。")
    visible_width_m = map_scale * map_width / 1000.0
    if not math.isfinite(visible_width_m) or visible_width_m <= 0:
        raise ValueError("无法根据裁剪后的地图范围计算比例尺单位。")
    if visible_width_m >= 10000.0:
        scale_bar_unit = "km"
        visible_width_units = visible_width_m / 1000.0
        scale_bar.setUnits(distance_unit("Kilometers"))
    else:
        scale_bar_unit = "m"
        visible_width_units = visible_width_m
        scale_bar.setUnits(distance_unit("Meters"))
    scale_bar_segments = 4 if map_width >= 160.0 else 3
    scale_bar.setNumberOfSegments(scale_bar_segments)
    scale_bar.setNumberOfSegmentsLeft(0)
    scale_bar.setUnitLabel(scale_bar_unit)
    scale_bar_max_width = min(55.0, max(30.0, map_width * 0.32))
    scale_bar_min_width = max(20.0, scale_bar_max_width * 0.6)
    fit_width_mode = getattr(
        getattr(Qgis, "ScaleBarSegmentSizeMode", None),
        "FitWidth",
        None,
    )
    if fit_width_mode is not None:
        scale_bar.setSegmentSizeMode(fit_width_mode)
        scale_bar.setMinimumBarWidth(scale_bar_min_width)
        scale_bar.setMaximumBarWidth(scale_bar_max_width)
    else:
        scale_bar.setUnitsPerSegment(
            nice_scale_distance(visible_width_units / (scale_bar_segments * 3.0))
        )
    scale_bar.applyDefaultSize()
    scale_bar.attemptMove(
        QgsLayoutPoint(map_x + 5, map_y + map_height - 12, QgsUnitTypes.LayoutMillimeters)
    )
    layout.addLayoutItem(scale_bar)
    scale_bar.update()

layout.refresh()

if not layout_manager.addLayout(layout):
    raise ValueError("无法将土地覆盖打印布局添加到 QGIS 工程。")

exporter = QgsLayoutExporter(layout)
image_settings = QgsLayoutExporter.ImageExportSettings()
image_settings.dpi = 300
pdf_settings = QgsLayoutExporter.PdfExportSettings()
if exporter.exportToImage(str(png_output), image_settings) != QgsLayoutExporter.Success:
    raise ValueError("土地覆盖专题图 PNG 导出失败。")
if exporter.exportToPdf(str(pdf_output), pdf_settings) != QgsLayoutExporter.Success:
    raise ValueError("土地覆盖专题图 PDF 导出失败。")

if iface is not None:  # noqa: F821 - executor-injected QGIS interface
    iface.mapCanvas().setExtent(extent)  # noqa: F821
    iface.mapCanvas().refresh()  # noqa: F821
    iface.openLayoutDesigner(layout)  # noqa: F821

print(
    json.dumps(
        {
            "source_layer": source_layer.name(),
            "layer": layer.name(),
            "source_type": parameters["source_type"],
            "scope_mode": parameters["scope_mode"],
            "boundary_layer": boundary_layer.name() if boundary_layer is not None else None,
            "clip_output": str(clip_output) if clip_output is not None else None,
            "category_count": len(parameters["class_mapping"]),
            "layout_name": layout_name,
            "title": parameters["title"],
            "orientation": parameters["orientation"],
            "legend_position": parameters["legend_position"],
            "legend_title": parameters["legend_title"],
            "legend_items_only": True,
            "scale_bar_unit": scale_bar_unit if parameters["show_scale_bar"] else None,
            "scale_bar_segments": (
                scale_bar_segments if parameters["show_scale_bar"] else None
            ),
            "qgis_layout_added": True,
            "qgis_layout_opened": iface is not None,  # noqa: F821
        },
        ensure_ascii=False,
    )
)
