"""Deterministic QGIS script for school service coverage analysis.

The tool wrapper injects ``SCHOOL_SERVICE_PARAMETERS_JSON``. This script is
executed inside the current QGIS Python environment by the existing executor.
"""

import csv
import json
from pathlib import Path

import processing
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsExpression,
    QgsField,
    QgsGeometry,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

parameters = json.loads(SCHOOL_SERVICE_PARAMETERS_JSON)  # noqa: F821 - tool-injected
workspace = Path(QGIS_AGENT_WORKSPACE)  # noqa: F821 - executor-injected
vector_output = workspace / "school_service_coverage.gpkg"
table_output = workspace / "school_service_coverage_by_group.csv"


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


school_layer = require_layer(parameters["school_layer_id"], "学校")
residential_layer = require_layer(parameters["residential_layer_id"], "居住区")
school_type_field = parameters["school_type_field"]
area_field = parameters["residential_area_field"]
group_field = parameters["group_field"]
school_type_index = require_field(school_layer, school_type_field, "学校类型")
require_field(residential_layer, area_field, "居住区面积")
require_field(residential_layer, group_field, "分组")

if school_layer.geometryType() not in (
    QgsWkbTypes.PointGeometry,
    QgsWkbTypes.PolygonGeometry,
):
    raise ValueError("学校图层必须是点或面图层。")
if residential_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
    raise ValueError("居住区图层必须是面图层。")

available_school_type_values = {
    str(feature[school_type_index])
    for feature in school_layer.getFeatures()
    if not is_null(feature[school_type_index])
    and str(feature[school_type_index]).strip() != ""
}
missing_school_type_values = [
    value
    for value in parameters["school_type_values"]
    if value not in available_school_type_values
]
if missing_school_type_values:
    available_preview = sorted(available_school_type_values)[:50]
    raise ValueError(
        "school_type_values 包含学校类型字段中不存在的值："
        + "、".join(missing_school_type_values)
        + "；实际可用值："
        + "、".join(available_preview)
    )

target_crs = QgsCoordinateReferenceSystem(parameters["target_crs"])
if not target_crs.isValid() or target_crs.isGeographic():
    raise ValueError("target_crs 必须是有效的投影 CRS。")
meter_unit = getattr(getattr(Qgis, "DistanceUnit", object), "Meters", None)
if meter_unit is not None and target_crs.mapUnits() != meter_unit:
    raise ValueError("target_crs 的线性单位必须是米。")

context = QgsProcessingContext()
context.setInvalidGeometryCheck(Qgis.InvalidGeometryCheck.GeometrySkipInvalid)
feedback = QgsProcessingFeedback()

school_projected = processing.run(
    "native:reprojectlayer",
    {
        "INPUT": school_layer,
        "TARGET_CRS": target_crs,
        "OUTPUT": "TEMPORARY_OUTPUT",
    },
    context=context,
    feedback=feedback,
)["OUTPUT"]
residential_projected = processing.run(
    "native:reprojectlayer",
    {
        "INPUT": residential_layer,
        "TARGET_CRS": target_crs,
        "OUTPUT": "TEMPORARY_OUTPUT",
    },
    context=context,
    feedback=feedback,
)["OUTPUT"]

quoted_field = QgsExpression.quotedColumnRef(school_type_field)
quoted_values = ", ".join(
    QgsExpression.quotedString(str(value)) for value in parameters["school_type_values"]
)
filter_expression = f"to_string({quoted_field}) IN ({quoted_values})"
selected_schools = processing.run(
    "native:extractbyexpression",
    {
        "INPUT": school_projected,
        "EXPRESSION": filter_expression,
        "OUTPUT": "TEMPORARY_OUTPUT",
    },
    context=context,
    feedback=feedback,
)["OUTPUT"]
school_count = selected_schools.featureCount()
if school_count <= 0:
    raise ValueError("学校类型筛选结果为空，请检查字段和值。")

service_area = processing.run(
    "native:buffer",
    {
        "INPUT": selected_schools,
        "DISTANCE": parameters["service_distance_m"],
        "SEGMENTS": 16,
        "END_CAP_STYLE": 0,
        "JOIN_STYLE": 0,
        "MITER_LIMIT": 2.0,
        "DISSOLVE": True,
        "OUTPUT": "TEMPORARY_OUTPUT",
    },
    context=context,
    feedback=feedback,
)["OUTPUT"]
service_geometries = [
    QgsGeometry(feature.geometry())
    for feature in service_area.getFeatures()
    if feature.hasGeometry() and not feature.geometry().isEmpty()
]
if not service_geometries:
    raise ValueError("学校服务范围为空。")
service_geometry = QgsGeometry.unaryUnion(service_geometries)
if service_geometry.isNull() or service_geometry.isEmpty():
    raise ValueError("学校服务范围合并失败。")
if not service_geometry.isGeosValid():
    raise ValueError("学校服务范围几何无效。")

result_field_names = ["svc_cov_m2", "svc_rate", "svc_pct"]
existing_fields = {field.name() for field in residential_projected.fields()}
conflicts = [name for name in result_field_names if name in existing_fields]
if conflicts:
    raise ValueError("居住区图层已存在结果字段：" + "、".join(conflicts))

provider = residential_projected.dataProvider()
if not provider.addAttributes(
    [
        QgsField("svc_cov_m2", QVariant.Double, len=20, prec=3),
        QgsField("svc_rate", QVariant.Double, len=20, prec=10),
        QgsField("svc_pct", QVariant.Double, len=20, prec=6),
    ]
):
    raise ValueError("无法创建覆盖率结果字段。")
residential_projected.updateFields()

covered_index = residential_projected.fields().indexFromName("svc_cov_m2")
rate_index = residential_projected.fields().indexFromName("svc_rate")
percent_index = residential_projected.fields().indexFromName("svc_pct")
unit_factors = {
    "square_meter": 1.0,
    "hectare": 10000.0,
    "mu": 2000.0 / 3.0,
    "square_kilometer": 1000000.0,
}
area_factor = unit_factors[parameters["area_unit"]]

group_stats = {}
attribute_changes = {}
invalid_area_count = 0
invalid_geometry_count = 0
anomalous_rate_count = 0
total_covered_m2 = 0.0
total_area_m2 = 0.0
valid_count = 0

for feature in residential_projected.getFeatures():
    group_value = feature[group_field]
    group_name = "未分组" if is_null(group_value) or str(group_value).strip() == "" else str(group_value)
    stats = group_stats.setdefault(
        group_name,
        {
            "feature_count": 0,
            "valid_count": 0,
            "invalid_count": 0,
            "total_area_m2": 0.0,
            "covered_area_m2": 0.0,
        },
    )
    stats["feature_count"] += 1

    raw_area = feature[area_field]
    try:
        area_m2 = float(raw_area) * area_factor
    except (TypeError, ValueError):
        area_m2 = 0.0
    geometry = feature.geometry()
    geometry_is_valid = (
        feature.hasGeometry()
        and not geometry.isNull()
        and not geometry.isEmpty()
        and geometry.isGeosValid()
    )
    if area_m2 <= 0:
        invalid_area_count += 1
        stats["invalid_count"] += 1
        attribute_changes[feature.id()] = {
            covered_index: None,
            rate_index: None,
            percent_index: None,
        }
        continue
    if not geometry_is_valid:
        invalid_geometry_count += 1
        stats["invalid_count"] += 1
        attribute_changes[feature.id()] = {
            covered_index: None,
            rate_index: None,
            percent_index: None,
        }
        continue

    covered_m2 = 0.0
    if geometry.intersects(service_geometry):
        covered_m2 = geometry.intersection(service_geometry).area()
    rate = covered_m2 / area_m2
    if rate > 1.0 + 1e-9:
        anomalous_rate_count += 1
    attribute_changes[feature.id()] = {
        covered_index: covered_m2,
        rate_index: rate,
        percent_index: rate * 100.0,
    }
    stats["valid_count"] += 1
    stats["total_area_m2"] += area_m2
    stats["covered_area_m2"] += covered_m2
    valid_count += 1
    total_area_m2 += area_m2
    total_covered_m2 += covered_m2

if not provider.changeAttributeValues(attribute_changes):
    raise ValueError("写入覆盖率结果字段失败。")

processing.run(
    "native:savefeatures",
    {
        "INPUT": residential_projected,
        "OUTPUT": str(vector_output),
    },
    context=context,
    feedback=feedback,
)

with open(table_output, "w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(
        [
            "group_name",
            "feature_count",
            "valid_count",
            "invalid_count",
            "total_area_m2",
            "covered_area_m2",
            "coverage",
            "cover_pct",
        ]
    )
    for group_name in sorted(group_stats):
        stats = group_stats[group_name]
        denominator = stats["total_area_m2"]
        coverage = stats["covered_area_m2"] / denominator if denominator > 0 else None
        writer.writerow(
            [
                group_name,
                stats["feature_count"],
                stats["valid_count"],
                stats["invalid_count"],
                stats["total_area_m2"],
                stats["covered_area_m2"],
                coverage,
                coverage * 100.0 if coverage is not None else None,
            ]
        )

summary = {
    "school_count": school_count,
    "service_distance_m": parameters["service_distance_m"],
    "residential_count": residential_projected.featureCount(),
    "valid_count": valid_count,
    "invalid_area_count": invalid_area_count,
    "invalid_geometry_count": invalid_geometry_count,
    "anomalous_rate_count": anomalous_rate_count,
    "total_area_m2": total_area_m2,
    "covered_area_m2": total_covered_m2,
    "coverage": total_covered_m2 / total_area_m2 if total_area_m2 > 0 else None,
    "group_count": len(group_stats),
}
print(json.dumps(summary, ensure_ascii=False))
