"""Deterministic QGIS script for land-use building metrics."""

import csv
import json
import math
from pathlib import Path

import processing
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

parameters = json.loads(LAND_USE_BUILDING_PARAMETERS_JSON)  # noqa: F821
workspace = Path(QGIS_AGENT_WORKSPACE)  # noqa: F821
vector_output = workspace / "land_use_building_metrics.gpkg"
table_output = workspace / "land_use_building_metrics.csv"


def require_layer(layer_id, role):
    layer = QgsProject.instance().mapLayer(layer_id)
    if layer is None or not layer.isValid():
        raise ValueError(f"找不到有效的{role}图层：{layer_id}")
    return layer


def require_field(layer, field_name, role):
    index = layer.fields().indexFromName(field_name)
    if index < 0:
        raise ValueError(f"{role}字段不存在：{field_name}")
    return index


def is_null(value):
    if value is None:
        return True
    method = getattr(value, "isNull", None)
    return bool(method()) if callable(method) else False


def numeric_value(value, *, positive=False, non_negative=False):
    if is_null(value) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if positive and number <= 0:
        return None
    if non_negative and number < 0:
        return None
    return number


def valid_geometry(feature):
    if not feature.hasGeometry():
        return None
    geometry = feature.geometry()
    if geometry.isNull() or geometry.isEmpty() or not geometry.isGeosValid():
        return None
    return QgsGeometry(geometry)


def reproject(layer, target_crs, context, feedback):
    return processing.run(
        "native:reprojectlayer",
        {"INPUT": layer, "TARGET_CRS": target_crs, "OUTPUT": "TEMPORARY_OUTPUT"},
        context=context,
        feedback=feedback,
    )["OUTPUT"]


def new_stats():
    return {
        "parcel_count": 0,
        "valid_land_area_count": 0,
        "invalid_land_area_count": 0,
        "land_area_m2": 0.0,
        "building_count": 0,
        "height_valid_count": 0,
        "height_invalid_count": 0,
        "height_sum": 0.0,
        "height_min": None,
        "height_max": None,
        "footprint_valid_count": 0,
        "footprint_invalid_count": 0,
        "footprint_m2": 0.0,
        "floor_area_valid_count": 0,
        "floor_area_invalid_count": 0,
        "floor_area_m2": 0.0,
    }


land_layer = require_layer(parameters["land_layer_id"], "用地")
building_layer = require_layer(parameters["building_layer_id"], "建筑单体")
if land_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
    raise ValueError("用地图层必须是面图层。")
if building_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
    raise ValueError("建筑单体图层必须是面图层。")

land_type_field = parameters["land_type_field"]
land_area_field = parameters["land_area_field"]
height_field = parameters["building_height_field"]
footprint_field = parameters["building_footprint_field"]
floor_area_field = parameters["building_floor_area_field"]
require_field(land_layer, land_type_field, "用地类型")
require_field(land_layer, land_area_field, "地块面积")
require_field(building_layer, height_field, "建筑高度")
require_field(building_layer, footprint_field, "建筑占地面积")
require_field(building_layer, floor_area_field, "建筑面积")

target_crs = QgsCoordinateReferenceSystem(parameters["target_crs"])
if not target_crs.isValid() or target_crs.isGeographic():
    raise ValueError("target_crs 必须是有效的投影 CRS。")
meter_unit = getattr(getattr(Qgis, "DistanceUnit", object), "Meters", None)
if meter_unit is not None and target_crs.mapUnits() != meter_unit:
    raise ValueError("target_crs 的线性单位必须是米。")

context = QgsProcessingContext()
context.setInvalidGeometryCheck(Qgis.InvalidGeometryCheck.GeometrySkipInvalid)
feedback = QgsProcessingFeedback()
land_projected = reproject(land_layer, target_crs, context, feedback)
building_projected = reproject(building_layer, target_crs, context, feedback)

boundary_geometry = None
boundary_name = None
if parameters["scope_mode"] == "boundary_layer":
    boundary_layer = require_layer(parameters["boundary_layer_id"], "计算边界")
    if boundary_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
        raise ValueError("计算边界图层必须是面图层。")
    boundary_projected = reproject(boundary_layer, target_crs, context, feedback)
    boundary_geometries = [
        geometry
        for feature in boundary_projected.getFeatures()
        if (geometry := valid_geometry(feature)) is not None
    ]
    if not boundary_geometries:
        raise ValueError("计算边界图层没有有效面几何。")
    boundary_geometry = QgsGeometry.unaryUnion(boundary_geometries)
    if (
        boundary_geometry.isNull()
        or boundary_geometry.isEmpty()
        or not boundary_geometry.isGeosValid()
    ):
        raise ValueError("计算边界合并失败。")
    boundary_name = boundary_layer.name()

unit_factors = {
    "square_meter": 1.0,
    "hectare": 10000.0,
    "mu": 2000.0 / 3.0,
    "square_kilometer": 1000000.0,
}
land_area_factor = unit_factors[parameters["land_area_unit"]]
building_area_factor = unit_factors[parameters["building_area_unit"]]

stats_by_type = {}
geometries_by_type = {}
land_records = {}
land_spatial_index = QgsSpatialIndex()
invalid_land_geometry_count = 0

for feature in land_projected.getFeatures():
    geometry = valid_geometry(feature)
    if geometry is None:
        invalid_land_geometry_count += 1
        continue
    original_geometry_area = geometry.area()
    scoped_geometry = geometry
    scope_ratio = 1.0
    if boundary_geometry is not None:
        if not geometry.intersects(boundary_geometry):
            continue
        scoped_geometry = geometry.intersection(boundary_geometry)
        if (
            scoped_geometry.isNull()
            or scoped_geometry.isEmpty()
            or not scoped_geometry.isGeosValid()
        ):
            invalid_land_geometry_count += 1
            continue
        scope_ratio = (
            scoped_geometry.area() / original_geometry_area if original_geometry_area > 0 else 0.0
        )
        if scope_ratio <= 0:
            continue

    raw_type = feature[land_type_field]
    land_type = "未分类" if is_null(raw_type) or not str(raw_type).strip() else str(raw_type)
    stats = stats_by_type.setdefault(land_type, new_stats())
    stats["parcel_count"] += 1
    raw_land_area = numeric_value(feature[land_area_field], positive=True)
    if raw_land_area is None or scope_ratio <= 0:
        stats["invalid_land_area_count"] += 1
    else:
        stats["valid_land_area_count"] += 1
        stats["land_area_m2"] += raw_land_area * land_area_factor * scope_ratio

    geometries_by_type.setdefault(land_type, []).append(scoped_geometry)
    indexed_feature = QgsFeature(feature)
    indexed_feature.setGeometry(scoped_geometry)
    land_spatial_index.addFeature(indexed_feature)
    land_records[feature.id()] = {"land_type": land_type, "geometry": scoped_geometry}

if not land_records:
    raise ValueError("计算范围内没有有效用地面。")

unassigned_building_count = 0
outside_scope_building_count = 0
invalid_building_geometry_count = 0
overlap_assignment_count = 0

for feature in building_projected.getFeatures():
    geometry = valid_geometry(feature)
    if geometry is None:
        invalid_building_geometry_count += 1
        continue
    representative_point = geometry.pointOnSurface()
    if representative_point.isNull() or representative_point.isEmpty():
        invalid_building_geometry_count += 1
        continue
    if boundary_geometry is not None and not boundary_geometry.intersects(representative_point):
        outside_scope_building_count += 1
        continue

    matches = []
    for feature_id in land_spatial_index.intersects(representative_point.boundingBox()):
        record = land_records.get(feature_id)
        if record is None or not record["geometry"].intersects(representative_point):
            continue
        overlap_area = geometry.intersection(record["geometry"]).area()
        matches.append((overlap_area, feature_id, record["land_type"]))
    if not matches:
        unassigned_building_count += 1
        continue
    if len(matches) > 1:
        overlap_assignment_count += 1
    _, _, land_type = max(matches, key=lambda item: (item[0], -item[1]))
    stats = stats_by_type[land_type]
    stats["building_count"] += 1

    height = numeric_value(feature[height_field], non_negative=True)
    if height is None:
        stats["height_invalid_count"] += 1
    else:
        stats["height_valid_count"] += 1
        stats["height_sum"] += height
        stats["height_min"] = (
            height if stats["height_min"] is None else min(stats["height_min"], height)
        )
        stats["height_max"] = (
            height if stats["height_max"] is None else max(stats["height_max"], height)
        )

    footprint = numeric_value(feature[footprint_field], non_negative=True)
    if footprint is None:
        stats["footprint_invalid_count"] += 1
    else:
        stats["footprint_valid_count"] += 1
        stats["footprint_m2"] += footprint * building_area_factor

    floor_area = numeric_value(feature[floor_area_field], non_negative=True)
    if floor_area is None:
        stats["floor_area_invalid_count"] += 1
    else:
        stats["floor_area_valid_count"] += 1
        stats["floor_area_m2"] += floor_area * building_area_factor

result_layer = QgsVectorLayer(
    f"MultiPolygon?crs={target_crs.authid()}",
    "各类用地建筑量",
    "memory",
)
provider = result_layer.dataProvider()
if not provider.addAttributes(
    [
        QgsField("land_type", QVariant.String, len=120),
        QgsField("parcel_cnt", QVariant.Int),
        QgsField("land_m2", QVariant.Double, len=24, prec=3),
        QgsField("bldg_cnt", QVariant.Int),
        QgsField("h_valid", QVariant.Int),
        QgsField("h_sum", QVariant.Double, len=24, prec=3),
        QgsField("h_avg", QVariant.Double, len=24, prec=3),
        QgsField("h_min", QVariant.Double, len=24, prec=3),
        QgsField("h_max", QVariant.Double, len=24, prec=3),
        QgsField("foot_m2", QVariant.Double, len=24, prec=3),
        QgsField("floor_m2", QVariant.Double, len=24, prec=3),
        QgsField("density", QVariant.Double, len=24, prec=10),
        QgsField("dens_pct", QVariant.Double, len=24, prec=6),
    ]
):
    raise ValueError("无法创建各类用地建筑量结果字段。")
result_layer.updateFields()

result_features = []
anomalous_density_count = 0
for land_type in sorted(stats_by_type):
    stats = stats_by_type[land_type]
    land_area_m2 = stats["land_area_m2"]
    density = stats["footprint_m2"] / land_area_m2 if land_area_m2 > 0 else None
    if density is not None and density > 1.0 + 1e-9:
        anomalous_density_count += 1
    height_average = (
        stats["height_sum"] / stats["height_valid_count"]
        if stats["height_valid_count"] > 0
        else None
    )
    result_feature = QgsFeature(result_layer.fields())
    result_feature.setGeometry(QgsGeometry.unaryUnion(geometries_by_type[land_type]))
    result_feature.setAttributes(
        [
            land_type,
            stats["parcel_count"],
            land_area_m2,
            stats["building_count"],
            stats["height_valid_count"],
            stats["height_sum"] if stats["height_valid_count"] else None,
            height_average,
            stats["height_min"],
            stats["height_max"],
            stats["footprint_m2"],
            stats["floor_area_m2"],
            density,
            density * 100.0 if density is not None else None,
        ]
    )
    result_features.append(result_feature)

add_features_result = provider.addFeatures(result_features)
add_features_success = (
    bool(add_features_result[0])
    if isinstance(add_features_result, tuple)
    else bool(add_features_result)
)
if not add_features_success:
    raise ValueError("无法创建各类用地建筑量结果。")
result_layer.updateExtents()
processing.run(
    "native:savefeatures",
    {"INPUT": result_layer, "OUTPUT": str(vector_output)},
    context=context,
    feedback=feedback,
)

with open(table_output, "w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(
        [
            "land_type",
            "parcel_count",
            "valid_land_area_count",
            "invalid_land_area_count",
            "land_area_m2",
            "building_count",
            "height_valid_count",
            "height_invalid_count",
            "height_sum",
            "height_average",
            "height_minimum",
            "height_maximum",
            "footprint_valid_count",
            "footprint_invalid_count",
            "footprint_area_m2",
            "floor_area_valid_count",
            "floor_area_invalid_count",
            "floor_area_m2",
            "building_density",
            "building_density_percent",
        ]
    )
    for land_type in sorted(stats_by_type):
        stats = stats_by_type[land_type]
        denominator = stats["land_area_m2"]
        density = stats["footprint_m2"] / denominator if denominator > 0 else None
        height_average = (
            stats["height_sum"] / stats["height_valid_count"]
            if stats["height_valid_count"] > 0
            else None
        )
        writer.writerow(
            [
                land_type,
                stats["parcel_count"],
                stats["valid_land_area_count"],
                stats["invalid_land_area_count"],
                stats["land_area_m2"],
                stats["building_count"],
                stats["height_valid_count"],
                stats["height_invalid_count"],
                stats["height_sum"] if stats["height_valid_count"] else None,
                height_average,
                stats["height_min"],
                stats["height_max"],
                stats["footprint_valid_count"],
                stats["footprint_invalid_count"],
                stats["footprint_m2"],
                stats["floor_area_valid_count"],
                stats["floor_area_invalid_count"],
                stats["floor_area_m2"],
                density,
                density * 100.0 if density is not None else None,
            ]
        )

total_land_area_m2 = sum(stats["land_area_m2"] for stats in stats_by_type.values())
total_footprint_m2 = sum(stats["footprint_m2"] for stats in stats_by_type.values())
summary = {
    "scope_mode": parameters["scope_mode"],
    "boundary_layer": boundary_name,
    "land_type_count": len(stats_by_type),
    "parcel_count": sum(stats["parcel_count"] for stats in stats_by_type.values()),
    "building_count": sum(stats["building_count"] for stats in stats_by_type.values()),
    "land_area_m2": total_land_area_m2,
    "footprint_area_m2": total_footprint_m2,
    "floor_area_m2": sum(stats["floor_area_m2"] for stats in stats_by_type.values()),
    "building_density": (
        total_footprint_m2 / total_land_area_m2 if total_land_area_m2 > 0 else None
    ),
    "unassigned_building_count": unassigned_building_count,
    "outside_scope_building_count": outside_scope_building_count,
    "invalid_land_geometry_count": invalid_land_geometry_count,
    "invalid_building_geometry_count": invalid_building_geometry_count,
    "overlap_assignment_count": overlap_assignment_count,
    "anomalous_density_count": anomalous_density_count,
}
print(json.dumps(summary, ensure_ascii=False))
