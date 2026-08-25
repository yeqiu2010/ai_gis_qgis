"""Deterministic cultivated-land loss analysis for annual land-change surveys.

The tool wrapper injects ``CULTIVATED_LAND_LOSS_PARAMETERS_JSON``. The script
runs inside the current QGIS Python environment and writes only to the executor
workspace.
"""

import csv
import json
from datetime import datetime
from pathlib import Path

import processing
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

parameters = json.loads(CULTIVATED_LAND_LOSS_PARAMETERS_JSON)  # noqa: F821
workspace = Path(QGIS_AGENT_WORKSPACE)  # noqa: F821

report_output = workspace / "cultivated_land_loss_analysis.xlsx"
details_output = workspace / "cultivated_land_loss_details.gpkg"
metrics_output = workspace / "cultivated_land_loss_metrics.csv"
quality_output = workspace / "cultivated_land_loss_quality.json"

unit_factors = {
    "square_meter": 1.0,
    "hectare": 10000.0,
    "mu": 2000.0 / 3.0,
    "square_kilometer": 1000000.0,
}
unit_labels = {
    "square_meter": "平方米",
    "hectare": "公顷",
    "mu": "亩",
    "square_kilometer": "平方千米",
}


def require_layer(layer_id, role):
    layer = QgsProject.instance().mapLayer(layer_id)
    if layer is None or not layer.isValid():
        raise ValueError(f"找不到有效的{role}图层：{layer_id}")
    if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
        raise ValueError(f"{role}图层必须是面图层。")
    if not layer.crs().isValid():
        raise ValueError(f"{role}图层缺少有效 CRS。")
    return layer


def clone_without_subset(layer, role):
    subset = str(layer.subsetString() or "").strip()
    if not subset:
        return layer, ""
    cloned = layer.clone()
    if cloned is None or not cloned.isValid():
        raise ValueError(f"无法为{role}创建无筛选分析副本。")
    if not cloned.setSubsetString(""):
        raise ValueError(f"无法清除{role}分析副本的活动筛选：{subset}")
    return cloned, subset


def require_field(layer, field_name, role):
    index = layer.fields().indexFromName(field_name)
    if index < 0:
        raise ValueError(f"{role}地类编码字段不存在：{field_name}")
    return index


def is_null(value):
    if value is None:
        return True
    method = getattr(value, "isNull", None)
    return bool(method()) if callable(method) else False


def normalize_code(value):
    if is_null(value):
        return ""
    return str(value).strip().upper()


def repaired_projected_geometry(feature, layer, target_crs, role, quality):
    if not feature.hasGeometry():
        raise ValueError(f"{role}图层要素 {feature.id()} 缺少几何。")
    geometry = QgsGeometry(feature.geometry())
    if geometry.isNull() or geometry.isEmpty():
        raise ValueError(f"{role}图层要素 {feature.id()} 的几何为空。")
    if not geometry.isGeosValid():
        quality[role]["repaired_geometry_count"] += 1
        geometry = geometry.makeValid()
    if geometry.isNull() or geometry.isEmpty():
        raise ValueError(f"{role}图层要素 {feature.id()} 的几何无法修复。")
    geometry_type = QgsWkbTypes.geometryType(geometry.wkbType())
    if geometry_type != QgsWkbTypes.PolygonGeometry:
        raise ValueError(f"{role}图层要素 {feature.id()} 修复后不是面几何。")
    if layer.crs() != target_crs:
        transform = QgsCoordinateTransform(
            layer.crs(), target_crs, QgsProject.instance().transformContext()
        )
        if geometry.transform(transform) != 0:
            raise ValueError(f"{role}图层要素 {feature.id()} 投影转换失败。")
    if not geometry.isGeosValid():
        geometry = geometry.makeValid()
    if geometry.isNull() or geometry.isEmpty() or not geometry.isGeosValid():
        raise ValueError(f"{role}图层要素 {feature.id()} 投影后几何无效。")
    return geometry


def union_geometries(geometries, label):
    if not geometries:
        return None
    level = list(geometries)
    while len(level) > 1:
        next_level = []
        for offset in range(0, len(level), 500):
            merged = QgsGeometry.unaryUnion(level[offset : offset + 500])
            if merged.isNull() or merged.isEmpty():
                raise ValueError(f"{label}几何融合失败。")
            if not merged.isGeosValid():
                merged = merged.makeValid()
            if merged.isNull() or merged.isEmpty() or not merged.isGeosValid():
                raise ValueError(f"{label}融合后几何无效。")
            next_level.append(merged)
        level = next_level
    return level[0]


def intersection_area(geometry, mask):
    if geometry is None or mask is None or geometry.isEmpty() or mask.isEmpty():
        return 0.0
    if not geometry.intersects(mask):
        return 0.0
    result = geometry.intersection(mask)
    if result.isNull() or result.isEmpty():
        return 0.0
    return float(result.area())


def safe_intersection(geometry, mask, label):
    if geometry is None or mask is None or not geometry.intersects(mask):
        return None
    result = geometry.intersection(mask)
    if result.isNull() or result.isEmpty():
        return None
    if not result.isGeosValid():
        result = result.makeValid()
    if result.isNull() or result.isEmpty() or not result.isGeosValid():
        raise ValueError(f"{label}相交几何无效。")
    return result


def safe_difference(geometry, mask, label):
    if geometry is None:
        return None
    if mask is None or not geometry.intersects(mask):
        return QgsGeometry(geometry)
    result = geometry.difference(mask)
    if result.isNull() or result.isEmpty():
        return None
    if not result.isGeosValid():
        result = result.makeValid()
    if result.isNull() or result.isEmpty() or not result.isGeosValid():
        raise ValueError(f"{label}差集几何无效。")
    return result


previous_visible_layer = require_layer(
    parameters["previous_survey_layer_id"], "上年度调查"
)
increment_visible_layer = require_layer(
    parameters["increment_layer_id"], "年度增量包"
)
management_visible_layer = require_layer(
    parameters["land_management_layer_id"], "用地管理信息"
)
permanent_visible_layer = require_layer(
    parameters["permanent_farmland_layer_id"], "永久基本农田"
)
previous_layer, previous_subset = clone_without_subset(
    previous_visible_layer, "上年度调查"
)
increment_layer, increment_subset = clone_without_subset(
    increment_visible_layer, "年度增量包"
)
management_layer, management_subset = clone_without_subset(
    management_visible_layer, "用地管理信息"
)
permanent_layer, permanent_subset = clone_without_subset(
    permanent_visible_layer, "永久基本农田"
)
previous_code_index = require_field(
    previous_layer, parameters["previous_land_code_field"], "上年度调查"
)
increment_code_index = require_field(
    increment_layer, parameters["increment_land_code_field"], "年度增量包"
)

target_crs = QgsCoordinateReferenceSystem(parameters["target_crs"])
if not target_crs.isValid() or target_crs.isGeographic():
    raise ValueError("target_crs 必须是有效的投影 CRS。")
meter_unit = getattr(getattr(Qgis, "DistanceUnit", object), "Meters", None)
if meter_unit is not None and target_crs.mapUnits() != meter_unit:
    raise ValueError("target_crs 的线性单位必须是米。")

classification = parameters["classification"]
categories = classification["categories"]
code_categories = classification["code_categories"]
known_codes = set(classification["all_codes"])
cultivated_codes = set(categories["cultivated"])
forest_garden_codes = set(categories["forest_garden"])
other_agricultural_codes = set(categories["other_agricultural"])
construction_codes = set(categories["construction"])
unused_codes = set(categories["unused"])
non_grain_codes = forest_garden_codes | other_agricultural_codes

quality = {
    "rule_version": classification["version"],
    "target_crs": target_crs.authid() or target_crs.toWkt(),
    "area_unit": parameters["area_unit"],
    "layers": {},
    "unknown_codes": [],
    "warnings": [],
    "skipped_features": [],
    "checks": {},
}
for role, layer, visible_layer, subset in (
    ("previous", previous_layer, previous_visible_layer, previous_subset),
    ("increment", increment_layer, increment_visible_layer, increment_subset),
    ("management", management_layer, management_visible_layer, management_subset),
    ("permanent", permanent_layer, permanent_visible_layer, permanent_subset),
):
    quality[role] = {
        "repaired_geometry_count": 0,
        "skipped_feature_count": 0,
    }
    quality["layers"][role] = {
        "id": visible_layer.id(),
        "name": layer.name(),
        "feature_count": int(layer.featureCount()),
        "visible_feature_count": int(visible_layer.featureCount()),
        "source_crs": layer.crs().authid() or layer.crs().toWkt(),
        "active_subset": subset,
        "subset_ignored": bool(subset),
    }
    if subset:
        quality["warnings"].append(
            f"{role}图层的活动筛选已在分析副本中忽略：{subset}"
        )


def record_skipped_feature(role, feature, reason):
    feature_id = int(feature.id())
    message = f"{role}图层要素 {feature_id} 已跳过：{reason}"
    quality[role]["skipped_feature_count"] += 1
    quality["warnings"].append(message)
    quality["skipped_features"].append(
        {"role": role, "feature_id": feature_id, "reason": str(reason)}
    )


def optional_projected_geometry(feature, layer, target_crs, role, quality):
    try:
        return repaired_projected_geometry(feature, layer, target_crs, role, quality)
    except Exception as exc:
        record_skipped_feature(role, feature, exc)
        return None

previous_cultivated_geometries = []
previous_non_cultivated_geometries = []
previous_codes_seen = set()
previous_code_counts = {}
previous_non_cultivated_code_counts = {}
for feature in previous_layer.getFeatures():
    code = normalize_code(feature[previous_code_index])
    if not code:
        record_skipped_feature("previous", feature, "地类编码为空")
        continue
    previous_codes_seen.add(code)
    if code not in known_codes:
        record_skipped_feature("previous", feature, f"未配置地类编码 {code}")
        continue
    geometry = optional_projected_geometry(
        feature, previous_layer, target_crs, "previous", quality
    )
    if geometry is not None:
        if code in cultivated_codes:
            previous_cultivated_geometries.append(geometry)
            previous_code_counts[code] = previous_code_counts.get(code, 0) + 1
        else:
            previous_non_cultivated_geometries.append(geometry)
            previous_non_cultivated_code_counts[code] = (
                previous_non_cultivated_code_counts.get(code, 0) + 1
            )

increment_records = []
increment_cultivated_geometries = []
increment_codes_seen = set()
increment_code_counts = {}
increment_area_m2_total = 0.0
increment_area_feature_count = 0
for feature in increment_layer.getFeatures():
    geometry = optional_projected_geometry(
        feature, increment_layer, target_crs, "increment", quality
    )
    if geometry is None:
        continue
    increment_area_m2_total += float(geometry.area())
    increment_area_feature_count += 1

    code = normalize_code(feature[increment_code_index])
    if not code:
        record_skipped_feature(
            "increment",
            feature,
            "地类编码为空；仍计入增量包面积，但不参与分类指标",
        )
        continue
    increment_codes_seen.add(code)
    if code not in known_codes:
        record_skipped_feature(
            "increment",
            feature,
            f"未配置地类编码 {code}；仍计入增量包面积，但不参与分类指标",
        )
        continue
    increment_records.append((feature.id(), code, geometry))
    increment_code_counts[code] = increment_code_counts.get(code, 0) + 1
    if code in cultivated_codes:
        increment_cultivated_geometries.append(geometry)

unknown_codes = sorted((previous_codes_seen | increment_codes_seen) - known_codes)
quality["unknown_codes"] = unknown_codes
if not previous_cultivated_geometries:
    quality["warnings"].append(
        "上年度调查数据中没有可参与计算的 0101、0102、0103 耕地，"
        "上年度耕地面积及相关流出面积按 0 计算。"
    )


def collect_mask(layer, role):
    geometries = []
    for feature in layer.getFeatures():
        geometry = optional_projected_geometry(
            feature, layer, target_crs, role, quality
        )
        if geometry is not None:
            geometries.append(geometry)
    return union_geometries(geometries, role)


previous_cultivated = union_geometries(
    previous_cultivated_geometries, "上年度耕地"
)
previous_non_cultivated = union_geometries(
    previous_non_cultivated_geometries, "上年度非耕地"
)
increment_cultivated = union_geometries(
    increment_cultivated_geometries, "增量包耕地"
)
added_cultivated = safe_intersection(
    previous_non_cultivated,
    increment_cultivated,
    "新增耕地",
)
management_mask = collect_mask(management_layer, "management")
permanent_mask = collect_mask(permanent_layer, "permanent")

metrics_m2 = {
    "increment_area": increment_area_m2_total,
    "cultivated_outflow_area": 0.0,
    "reasonable_outflow_area": 0.0,
    "unreasonable_outflow_area": 0.0,
    "unreasonable_outflow_permanent_area": 0.0,
    "non_agricultural_area": 0.0,
    "non_agricultural_permanent_area": 0.0,
    "non_grain_area": 0.0,
    "non_grain_permanent_area": 0.0,
    "forest_garden_area": 0.0,
    "forest_garden_permanent_area": 0.0,
    "other_agricultural_area": 0.0,
    "other_agricultural_permanent_area": 0.0,
    "unused_outflow_area": 0.0,
    "unused_outflow_permanent_area": 0.0,
    "added_cultivated_area": (
        float(added_cultivated.area()) if added_cultivated is not None else 0.0
    ),
    "previous_cultivated_area": (
        float(previous_cultivated.area()) if previous_cultivated is not None else 0.0
    ),
}

detail_layer = QgsVectorLayer(
    f"MultiPolygon?crs={target_crs.authid()}",
    "耕地流失图斑明细",
    "memory",
)
detail_provider = detail_layer.dataProvider()
detail_fields = [
    QgsField("source_fid", QVariant.LongLong),
    QgsField("new_code", QVariant.String, len=16),
    QgsField("land_class", QVariant.String, len=32),
    QgsField("inc_m2", QVariant.Double, len=24, prec=6),
    QgsField("outflow_m2", QVariant.Double, len=24, prec=6),
    QgsField("reasonable_m2", QVariant.Double, len=24, prec=6),
    QgsField("unreasonable_m2", QVariant.Double, len=24, prec=6),
    QgsField("unreas_perm_m2", QVariant.Double, len=24, prec=6),
    QgsField("nonagri_m2", QVariant.Double, len=24, prec=6),
    QgsField("nonagri_perm_m2", QVariant.Double, len=24, prec=6),
    QgsField("nongrain_m2", QVariant.Double, len=24, prec=6),
    QgsField("nongrain_perm_m2", QVariant.Double, len=24, prec=6),
    QgsField("forest_garden_m2", QVariant.Double, len=24, prec=6),
    QgsField("forest_garden_perm_m2", QVariant.Double, len=24, prec=6),
    QgsField("other_agri_m2", QVariant.Double, len=24, prec=6),
    QgsField("other_agri_perm_m2", QVariant.Double, len=24, prec=6),
    QgsField("added_m2", QVariant.Double, len=24, prec=6),
]
if not detail_provider.addAttributes(detail_fields):
    raise ValueError("无法创建耕地流失明细字段。")
detail_layer.updateFields()

for source_fid, code, geometry in increment_records:
    increment_area_m2 = float(geometry.area())
    values = {
        "outflow_m2": 0.0,
        "reasonable_m2": 0.0,
        "unreasonable_m2": 0.0,
        "unreas_perm_m2": 0.0,
        "nonagri_m2": 0.0,
        "nonagri_perm_m2": 0.0,
        "nongrain_m2": 0.0,
        "nongrain_perm_m2": 0.0,
        "forest_garden_m2": 0.0,
        "forest_garden_perm_m2": 0.0,
        "other_agri_m2": 0.0,
        "other_agri_perm_m2": 0.0,
        "added_m2": 0.0,
    }

    if code in cultivated_codes:
        # This field preserves the source-polygon area for feature-level auditing.
        # The summary metric is calculated once from the dissolved intersection.
        values["added_m2"] = increment_area_m2
    else:
        outflow = safe_intersection(geometry, previous_cultivated, "耕地流出")
        if outflow is not None:
            values["outflow_m2"] = float(outflow.area())
            metrics_m2["cultivated_outflow_area"] += values["outflow_m2"]
            reasonable = safe_intersection(outflow, management_mask, "合理流出")
            unreasonable = safe_difference(outflow, management_mask, "不合理流出")
            values["reasonable_m2"] = float(reasonable.area()) if reasonable else 0.0
            values["unreasonable_m2"] = (
                float(unreasonable.area()) if unreasonable else 0.0
            )
            values["unreas_perm_m2"] = intersection_area(
                unreasonable, permanent_mask
            )
            metrics_m2["reasonable_outflow_area"] += values["reasonable_m2"]
            metrics_m2["unreasonable_outflow_area"] += values["unreasonable_m2"]
            metrics_m2["unreasonable_outflow_permanent_area"] += values[
                "unreas_perm_m2"
            ]

            if code in construction_codes:
                values["nonagri_m2"] = values["unreasonable_m2"]
                values["nonagri_perm_m2"] = intersection_area(
                    unreasonable, permanent_mask
                )
                metrics_m2["non_agricultural_area"] += values["nonagri_m2"]
                metrics_m2["non_agricultural_permanent_area"] += values[
                    "nonagri_perm_m2"
                ]
            elif code in non_grain_codes:
                values["nongrain_m2"] = values["unreasonable_m2"]
                values["nongrain_perm_m2"] = intersection_area(
                    unreasonable, permanent_mask
                )
                metrics_m2["non_grain_area"] += values["nongrain_m2"]
                metrics_m2["non_grain_permanent_area"] += values[
                    "nongrain_perm_m2"
                ]
                if code in forest_garden_codes:
                    values["forest_garden_m2"] = values["nongrain_m2"]
                    values["forest_garden_perm_m2"] = values[
                        "nongrain_perm_m2"
                    ]
                    metrics_m2["forest_garden_area"] += values[
                        "forest_garden_m2"
                    ]
                    metrics_m2["forest_garden_permanent_area"] += values[
                        "forest_garden_perm_m2"
                    ]
                else:
                    values["other_agri_m2"] = values["nongrain_m2"]
                    values["other_agri_perm_m2"] = values[
                        "nongrain_perm_m2"
                    ]
                    metrics_m2["other_agricultural_area"] += values[
                        "other_agri_m2"
                    ]
                    metrics_m2["other_agricultural_permanent_area"] += values[
                        "other_agri_perm_m2"
                    ]
            elif code in unused_codes:
                metrics_m2["unused_outflow_area"] += values["unreasonable_m2"]
                metrics_m2["unused_outflow_permanent_area"] += intersection_area(
                    unreasonable, permanent_mask
                )

    detail_feature = QgsFeature(detail_layer.fields())
    detail_geometry = QgsGeometry(geometry)
    if not QgsWkbTypes.isMultiType(detail_geometry.wkbType()):
        if not detail_geometry.convertToMultiType():
            raise ValueError(f"无法把增量图斑 {source_fid} 转换为 MultiPolygon。")
    detail_feature.setGeometry(detail_geometry)
    detail_feature.setAttributes(
        [
            int(source_fid),
            code,
            code_categories[code],
            increment_area_m2,
            values["outflow_m2"],
            values["reasonable_m2"],
            values["unreasonable_m2"],
            values["unreas_perm_m2"],
            values["nonagri_m2"],
            values["nonagri_perm_m2"],
            values["nongrain_m2"],
            values["nongrain_perm_m2"],
            values["forest_garden_m2"],
            values["forest_garden_perm_m2"],
            values["other_agri_m2"],
            values["other_agri_perm_m2"],
            values["added_m2"],
        ]
    )
    if not detail_provider.addFeature(detail_feature):
        raise ValueError(f"无法写入增量图斑 {source_fid} 的审计明细。")

detail_layer.updateExtents()
processing.run(
    "native:savefeatures",
    {"INPUT": detail_layer, "OUTPUT": str(details_output)},
)

metrics_m2["cultivated_change_area"] = (
    metrics_m2["unreasonable_outflow_area"]
    - metrics_m2["added_cultivated_area"]
)
metrics_m2["current_cultivated_area"] = (
    metrics_m2["previous_cultivated_area"]
    - metrics_m2["unreasonable_outflow_area"]
    + metrics_m2["added_cultivated_area"]
)

non_cultivated_increment_count = sum(
    count
    for code, count in increment_code_counts.items()
    if code not in cultivated_codes
)
if increment_records and non_cultivated_increment_count == 0:
    quality["warnings"].append(
        "增量包全部有效要素的地类编码均为耕地，耕地流出、非农化和非粮化按 0 计算；"
        "请确认传入的是完整增量包且地类字段代表变更后的地类。"
    )
elif (
    non_cultivated_increment_count > 0
    and metrics_m2["previous_cultivated_area"] > 0
    and metrics_m2["cultivated_outflow_area"] == 0
):
    quality["warnings"].append(
        f"增量包包含 {non_cultivated_increment_count} 个非耕地有效要素，"
        "但与上年度耕地没有面积相交；请检查两个图层的 CRS 定义和空间位置。"
    )

tolerance = 0.01


def close_enough(left, right):
    return abs(float(left) - float(right)) <= tolerance


checks = {
    "outflow_partition": close_enough(
        metrics_m2["cultivated_outflow_area"],
        metrics_m2["reasonable_outflow_area"]
        + metrics_m2["unreasonable_outflow_area"],
    ),
    "non_grain_partition": close_enough(
        metrics_m2["non_grain_area"],
        metrics_m2["forest_garden_area"]
        + metrics_m2["other_agricultural_area"],
    ),
    "unreasonable_outflow_category_partition": close_enough(
        metrics_m2["unreasonable_outflow_area"],
        metrics_m2["non_agricultural_area"]
        + metrics_m2["non_grain_area"]
        + metrics_m2["unused_outflow_area"],
    ),
    "unreasonable_permanent_within_total": (
        metrics_m2["unreasonable_outflow_permanent_area"]
        <= metrics_m2["unreasonable_outflow_area"] + tolerance
    ),
    "non_agricultural_permanent_within_total": (
        metrics_m2["non_agricultural_permanent_area"]
        <= metrics_m2["non_agricultural_area"] + tolerance
    ),
    "non_grain_permanent_within_total": (
        metrics_m2["non_grain_permanent_area"]
        <= metrics_m2["non_grain_area"] + tolerance
    ),
    "cultivated_change_formula": close_enough(
        metrics_m2["cultivated_change_area"],
        metrics_m2["unreasonable_outflow_area"]
        - metrics_m2["added_cultivated_area"],
    ),
    "current_cultivated_formula": close_enough(
        metrics_m2["current_cultivated_area"],
        metrics_m2["previous_cultivated_area"]
        - metrics_m2["unreasonable_outflow_area"]
        + metrics_m2["added_cultivated_area"],
    ),
}
quality["checks"] = checks
failed_checks = [name for name, passed in checks.items() if not passed]
if failed_checks:
    raise ValueError("指标平衡校验失败：" + "、".join(failed_checks))

area_factor = unit_factors[parameters["area_unit"]]
metrics = {
    name: value / area_factor for name, value in metrics_m2.items()
}
increment_count = int(increment_layer.featureCount())
skipped_feature_counts = {
    role: int(quality[role]["skipped_feature_count"])
    for role in ("previous", "increment", "management", "permanent")
}
skipped_feature_total = sum(skipped_feature_counts.values())

with metrics_output.open("w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(["metric", "value_m2", "value", "unit"])
    writer.writerow(["increment_count", "", increment_count, "个"])
    for name, value_m2 in metrics_m2.items():
        writer.writerow(
            [name, f"{value_m2:.6f}", f"{metrics[name]:.6f}", unit_labels[parameters["area_unit"]]]
        )

quality.update(
    {
        "management_area_name": parameters["management_area_name"],
        "analysis_year": parameters["analysis_year"],
        "previous_year": parameters["previous_year"],
        "increment_count": increment_count,
        "increment_area_feature_count": increment_area_feature_count,
        "processed_increment_count": len(increment_records),
        "skipped_feature_total": skipped_feature_total,
        "skipped_feature_counts": skipped_feature_counts,
        "code_domains": {
            "previous_cultivated": dict(sorted(previous_code_counts.items())),
            "previous_non_cultivated": dict(
                sorted(previous_non_cultivated_code_counts.items())
            ),
            "increment": dict(sorted(increment_code_counts.items())),
            "non_cultivated_increment_count": non_cultivated_increment_count,
        },
        "metrics_m2": metrics_m2,
        "metrics": metrics,
        "unit_label": unit_labels[parameters["area_unit"]],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
)
quality_output.write_text(
    json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
)


def format_statistic_date(raw_value):
    value = str(raw_value).strip()
    try:
        parsed = datetime.fromisoformat(value)
        return f"统计时间：{parsed.year}年{parsed.month}月{parsed.day}日"
    except ValueError:
        return value if value.startswith("统计时间") else f"统计时间：{value}"


def fill_excel_report():
    template_path = Path(parameters["template_path"])
    if not template_path.is_file():
        raise ValueError(f"Excel 模板不存在：{template_path}")
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError(
            "插件内置 openpyxl 不可用，无法填报 .xlsx 模板。"
        ) from exc

    try:
        workbook = load_workbook(template_path)
        sheet_index = int(parameters["report_mapping"].get("sheet_index") or 1)
        if sheet_index < 1 or sheet_index > len(workbook.worksheets):
            raise RuntimeError(f"Excel 模板不存在第 {sheet_index} 张工作表。")
        worksheet = workbook.worksheets[sheet_index - 1]

        cells = parameters["report_mapping"]["cells"]

        def set_cell(key, value):
            address = str(cells[key])
            worksheet[address] = value

        def set_formula(key, formula):
            set_cell(key, str(formula))

        set_cell("title", f"{parameters['analysis_year']}年度国土变更调查情况明细表")
        set_cell("statistic_date", format_statistic_date(parameters["statistic_date"]))
        set_cell("unit_label", f"单位:{unit_labels[parameters['area_unit']]}、个")
        set_cell("management_area_name", parameters["management_area_name"])
        set_cell("increment_count", increment_count)
        set_cell("increment_area", round(metrics["increment_area"], 2))
        set_cell(
            "unreasonable_outflow_area",
            round(metrics["unreasonable_outflow_area"], 2),
        )
        set_cell(
            "unreasonable_outflow_permanent_area",
            round(metrics["unreasonable_outflow_permanent_area"], 2),
        )
        set_cell("non_agricultural_area", round(metrics["non_agricultural_area"], 2))
        set_cell(
            "non_agricultural_permanent_area",
            round(metrics["non_agricultural_permanent_area"], 2),
        )
        set_cell("non_grain_area", round(metrics["non_grain_area"], 2))
        set_cell(
            "non_grain_permanent_area",
            round(metrics["non_grain_permanent_area"], 2),
        )
        set_cell("forest_garden_area", round(metrics["forest_garden_area"], 2))
        set_cell(
            "forest_garden_permanent_area",
            round(metrics["forest_garden_permanent_area"], 2),
        )
        set_cell(
            "other_agricultural_area", round(metrics["other_agricultural_area"], 2)
        )
        set_cell(
            "other_agricultural_permanent_area",
            round(metrics["other_agricultural_permanent_area"], 2),
        )
        set_cell("added_cultivated_area", round(metrics["added_cultivated_area"], 2))
        set_cell(
            "previous_cultivated_area",
            round(metrics["previous_cultivated_area"], 2),
        )
        formulas = parameters["report_mapping"]["formulas"]
        set_formula("cultivated_change_area", formulas["cultivated_change_area"])
        set_formula("current_cultivated_area", formulas["current_cultivated_area"])
        note = (
            "备注：本年度耕地面积=上年度耕地面积-耕地不合理流出面积+新增耕地面积；"
            "耕地变化面积=耕地不合理流出面积-新增耕地面积"
        )
        if quality["warnings"]:
            note += (
                f"；数据质量提示：共 {len(quality['warnings'])} 条警告，"
                "完整明细见 cultivated_land_loss_quality.json"
            )
        set_cell("note", note)
        for address in parameters["report_mapping"].get("unused_template_cells", []):
            worksheet[str(address)] = None
        workbook.calculation.calcMode = "auto"
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
        workbook.save(report_output)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"无法填报 Excel 模板：{exc}") from exc


fill_excel_report()

summary = {
    "management_area_name": parameters["management_area_name"],
    "analysis_year": parameters["analysis_year"],
    "rule_version": classification["version"],
    "increment_count": increment_count,
    "area_unit": parameters["area_unit"],
    "unit_label": unit_labels[parameters["area_unit"]],
    "metrics": metrics,
    "code_domains": quality["code_domains"],
    "checks": checks,
    "data_quality": {
        "has_warnings": bool(quality["warnings"]),
        "warning_count": len(quality["warnings"]),
        "skipped_feature_total": skipped_feature_total,
        "skipped_feature_counts": skipped_feature_counts,
        "messages": quality["warnings"][:50],
        "messages_truncated": len(quality["warnings"]) > 50,
        "quality_report": str(quality_output),
    },
    "outputs": [
        str(report_output),
        str(details_output),
        str(metrics_output),
        str(quality_output),
    ],
}
print(json.dumps(summary, ensure_ascii=False))
