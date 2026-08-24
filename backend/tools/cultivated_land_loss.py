"""Deterministic tools for cultivated-land loss analysis in annual land surveys."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ...database.session_db import SessionDB
from .code_execution import build_execute_gis_code_tool
from .layer_ops import _find_layer, _layer_type_name, _run_qgis
from .registry import ToolEntry

SKILL_DIR = (
    Path(__file__).resolve().parents[2] / "skills" / "analyze-cultivated-land-loss"
)
SCRIPT_PATH = SKILL_DIR / "scripts" / "calculate_cultivated_land_loss.py"
CLASSIFICATION_PATH = SKILL_DIR / "references" / "land_classification_2025.yaml"
REPORT_MAPPING_PATH = SKILL_DIR / "references" / "report_mapping_2025.yaml"
TEMPLATE_PATH = SKILL_DIR / "assets" / "xx区2025年度国土变更调查情况统计表.xlsx"

EXPECTED_OUTPUTS = [
    {
        "path": "cultivated_land_loss_analysis.xlsx",
        "name": "国土变更调查耕地流失情况统计表",
        "type": "table",
    },
    {
        "path": "cultivated_land_loss_details.gpkg",
        "name": "耕地流失图斑明细",
        "type": "vector",
    },
    {
        "path": "cultivated_land_loss_metrics.csv",
        "name": "耕地流失指标汇总",
        "type": "table",
    },
    {
        "path": "cultivated_land_loss_quality.json",
        "name": "耕地流失分析质量报告",
        "type": "file",
    },
]

AREA_UNITS = {"square_meter", "hectare", "mu", "square_kilometer"}
REQUIRED_LAYER_PARAMETERS = (
    "previous_survey_layer_id",
    "increment_layer_id",
    "land_management_layer_id",
    "permanent_farmland_layer_id",
)
REQUIRED_FIELD_PARAMETERS = (
    "previous_land_code_field",
    "increment_land_code_field",
)
MAX_CODE_VALUES = 500


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    method = getattr(value, "isNull", None)
    return bool(method()) if callable(method) else False


def _normalize_code(value: Any) -> str:
    if _is_null(value):
        return ""
    return str(value).strip().upper()


def _load_rule_bundle() -> dict[str, Any]:
    payload = yaml.safe_load(CLASSIFICATION_PATH.read_text(encoding="utf-8")) or {}
    categories = payload.get("categories")
    if not isinstance(categories, dict):
        raise ValueError("地类分类配置缺少 categories。")
    required = {
        "cultivated",
        "forest_garden",
        "other_agricultural",
        "construction",
        "unused",
    }
    missing = sorted(required - set(categories))
    if missing:
        raise ValueError("地类分类配置缺少类别：" + "、".join(missing))

    normalized: dict[str, dict[str, str]] = {}
    owner: dict[str, str] = {}
    for category, raw_mapping in categories.items():
        if not isinstance(raw_mapping, dict):
            raise ValueError(f"地类类别 {category} 必须是编码到名称的映射。")
        normalized[category] = {}
        for raw_code, raw_name in raw_mapping.items():
            code = _normalize_code(raw_code)
            if not code:
                raise ValueError(f"地类类别 {category} 包含空编码。")
            if code in owner:
                raise ValueError(
                    f"地类编码 {code} 同时属于 {owner[code]} 和 {category}。"
                )
            owner[code] = category
            normalized[category][code] = str(raw_name)
    return {
        "version": str(payload.get("version") or ""),
        "categories": normalized,
        "code_categories": owner,
        "all_codes": sorted(owner),
    }


def _load_report_mapping() -> dict[str, Any]:
    payload = yaml.safe_load(REPORT_MAPPING_PATH.read_text(encoding="utf-8")) or {}
    cells = payload.get("cells")
    formulas = payload.get("formulas")
    if not isinstance(cells, dict) or not isinstance(formulas, dict):
        raise ValueError("报表映射配置缺少 cells 或 formulas。")
    required_cells = {
        "management_area_name",
        "increment_count",
        "increment_area",
        "unreasonable_outflow_area",
        "unreasonable_outflow_permanent_area",
        "non_agricultural_area",
        "non_agricultural_permanent_area",
        "non_grain_area",
        "non_grain_permanent_area",
        "forest_garden_area",
        "forest_garden_permanent_area",
        "other_agricultural_area",
        "other_agricultural_permanent_area",
        "added_cultivated_area",
        "previous_cultivated_area",
        "cultivated_change_area",
        "current_cultivated_area",
    }
    missing = sorted(required_cells - set(cells))
    if missing:
        raise ValueError("报表映射缺少单元格：" + "、".join(missing))
    return payload


def _collect_code_domain(layer, field_name: str) -> dict[str, Any]:
    field_index = layer.fields().indexFromName(field_name)
    if field_index < 0:
        raise ValueError(f"地类编码字段不存在：{field_name}")
    counts: dict[str, int] = {}
    null_or_empty_count = 0
    for feature in layer.getFeatures():
        code = _normalize_code(feature[field_index])
        if not code:
            null_or_empty_count += 1
            continue
        counts[code] = counts.get(code, 0) + 1
        if len(counts) > MAX_CODE_VALUES:
            raise ValueError(
                f"字段 {field_name} 的唯一非空编码超过 {MAX_CODE_VALUES} 个，"
                "不像地类编码字段。"
            )
    return {
        "available_values": sorted(counts),
        "value_counts": [
            {"value": code, "count": counts[code]} for code in sorted(counts)
        ],
        "distinct_count": len(counts),
        "null_or_empty_count": null_or_empty_count,
    }


def _geometry_profile(layer) -> dict[str, int]:
    from qgis.core import QgsWkbTypes

    empty_count = 0
    invalid_count = 0
    unrepairable_count = 0
    for feature in layer.getFeatures():
        if not feature.hasGeometry():
            empty_count += 1
            continue
        geometry = feature.geometry()
        if geometry.isNull() or geometry.isEmpty():
            empty_count += 1
            continue
        if geometry.isGeosValid():
            continue
        invalid_count += 1
        try:
            repaired = geometry.makeValid()
        except Exception:
            repaired = None
        if (
            repaired is None
            or repaired.isNull()
            or repaired.isEmpty()
            or QgsWkbTypes.geometryType(repaired.wkbType())
            != QgsWkbTypes.PolygonGeometry
        ):
            unrepairable_count += 1
    return {
        "empty_geometry_count": empty_count,
        "invalid_geometry_count": invalid_count,
        "unrepairable_geometry_count": unrepairable_count,
    }


def _clone_without_subset(layer, role: str):
    subset = str(layer.subsetString() or "").strip()
    if not subset:
        return layer, ""
    cloned = layer.clone()
    if cloned is None or not cloned.isValid():
        raise ValueError(f"无法为{role}创建无筛选分析副本。")
    if not cloned.setSubsetString(""):
        raise ValueError(f"无法清除{role}分析副本的活动筛选：{subset}")
    return cloned, subset


def _validate_projected_metre_crs(target_crs: str, *, qgis_executor=None) -> None:
    def operation() -> None:
        try:
            from qgis.core import Qgis, QgsCoordinateReferenceSystem
        except Exception as exc:
            raise RuntimeError("当前运行环境未提供 PyQGIS，无法验证 target_crs。") from exc
        crs = QgsCoordinateReferenceSystem(target_crs)
        if not crs.isValid() or crs.isGeographic():
            raise ValueError("target_crs 必须是有效的投影 CRS。")
        metre = getattr(getattr(Qgis, "DistanceUnit", object), "Meters", None)
        if metre is not None and crs.mapUnits() != metre:
            raise ValueError("target_crs 的线性单位必须是米。")

    _run_qgis(qgis_executor, operation)


def build_inspect_cultivated_land_loss_inputs_tool(*, qgis_executor=None) -> ToolEntry:
    """Build the read-only preflight inspection tool."""

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        required = REQUIRED_LAYER_PARAMETERS + REQUIRED_FIELD_PARAMETERS
        values = {name: str(arguments.get(name) or "").strip() for name in required}
        missing = [name for name, value in values.items() if not value]
        if missing:
            return {"success": False, "error": "缺少必要参数：" + "、".join(missing)}
        target_crs = str(arguments.get("target_crs") or "").strip()
        rules = _load_rule_bundle()
        known_codes = set(rules["all_codes"])

        def operation() -> dict[str, Any]:
            from qgis.core import QgsWkbTypes

            visible_layers = {
                name: _find_layer(values[name]) for name in REQUIRED_LAYER_PARAMETERS
            }
            roles = {
                "previous_survey_layer_id": "上年度调查",
                "increment_layer_id": "年度增量包",
                "land_management_layer_id": "用地管理信息",
                "permanent_farmland_layer_id": "永久基本农田",
            }
            layers: dict[str, Any] = {}
            active_subsets: dict[str, str] = {}
            for name, visible_layer in visible_layers.items():
                analysis_layer, subset = _clone_without_subset(
                    visible_layer, roles[name]
                )
                layers[name] = analysis_layer
                active_subsets[name] = subset
            data_warnings: list[str] = []
            layer_profiles: dict[str, Any] = {}
            for name, layer in layers.items():
                if not layer.isValid() or _layer_type_name(layer) != "vector":
                    raise ValueError(f"{name} 必须是有效的矢量图层。")
                if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                    raise ValueError(f"{name} 必须是面图层。")
                geometry = _geometry_profile(layer)
                if geometry["empty_geometry_count"]:
                    data_warnings.append(
                        f"{layer.name()} 存在 {geometry['empty_geometry_count']} 个空几何。"
                    )
                if geometry["unrepairable_geometry_count"]:
                    data_warnings.append(
                        f"{layer.name()} 存在 {geometry['unrepairable_geometry_count']} 个无法修复的几何。"
                    )
                subset = active_subsets[name]
                if subset:
                    data_warnings.append(
                        f"{roles[name]}图层的活动筛选已在分析副本中忽略：{subset}"
                    )
                layer_profiles[name] = {
                    "id": visible_layers[name].id(),
                    "name": layer.name(),
                    "feature_count": int(layer.featureCount()),
                    "visible_feature_count": int(visible_layers[name].featureCount()),
                    "crs": layer.crs().authid() or layer.crs().toWkt(),
                    "active_subset": subset,
                    "subset_ignored": bool(subset),
                    **geometry,
                }

            previous_domain = _collect_code_domain(
                layers["previous_survey_layer_id"],
                values["previous_land_code_field"],
            )
            increment_domain = _collect_code_domain(
                layers["increment_layer_id"],
                values["increment_land_code_field"],
            )
            unknown_codes = sorted(
                (set(previous_domain["available_values"]) | set(increment_domain["available_values"]))
                - known_codes
            )
            if unknown_codes:
                data_warnings.append("存在未配置地类编码：" + "、".join(unknown_codes))
            for label, domain in (
                ("上年度地类字段", previous_domain),
                ("增量包地类字段", increment_domain),
            ):
                if domain["null_or_empty_count"]:
                    data_warnings.append(
                        f"{label}存在 {domain['null_or_empty_count']} 个空值。"
                    )

            return {
                "success": True,
                "inspection_complete": True,
                "rule_version": rules["version"],
                "layers": layer_profiles,
                "field_bindings": {
                    "previous_land_code_field": values["previous_land_code_field"],
                    "increment_land_code_field": values["increment_land_code_field"],
                },
                "code_domains": {
                    "previous": previous_domain,
                    "increment": increment_domain,
                },
                "unknown_codes": unknown_codes,
                "data_warnings": data_warnings,
                "blocking_issues": [],
                "target_crs_checked": bool(target_crs),
            }

        try:
            result = _run_qgis(qgis_executor, operation)
            if target_crs:
                _validate_projected_metre_crs(target_crs, qgis_executor=qgis_executor)
            return result
        except (OSError, RuntimeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    properties = {
        name: {"type": "string"} for name in REQUIRED_LAYER_PARAMETERS + REQUIRED_FIELD_PARAMETERS
    }
    properties["target_crs"] = {
        "type": "string",
        "description": "可选；提供时同时验证为线性单位为米的投影 CRS。",
    }
    return ToolEntry(
        name="inspect_cultivated_land_loss_inputs",
        description=(
            "只读检查耕地流失分析所需的四个面图层、两个地类编码字段、完整编码值域、"
            "未知编码、几何质量和 CRS。在固定执行工具前必须调用；要素级问题通过"
            " data_warnings 返回且不阻断执行，结构性问题才会失败。"
        ),
        parameters={
            "type": "object",
            "properties": properties,
            "required": list(REQUIRED_LAYER_PARAMETERS + REQUIRED_FIELD_PARAMETERS),
            "additionalProperties": False,
        },
        handler=handler,
        category="cultivated_land_loss",
        requires_confirmation=False,
        writes_project=False,
    )


def _normalize_parameters(arguments: dict[str, Any]) -> dict[str, Any]:
    required = REQUIRED_LAYER_PARAMETERS + REQUIRED_FIELD_PARAMETERS + (
        "management_area_name",
        "target_crs",
    )
    parameters: dict[str, Any] = {}
    for name in required:
        value = str(arguments.get(name) or "").strip()
        if not value:
            raise ValueError(f"缺少必要参数：{name}")
        parameters[name] = value

    area_unit = str(arguments.get("area_unit") or "mu").strip()
    if area_unit not in AREA_UNITS:
        raise ValueError(
            "area_unit 必须是 square_meter、hectare、mu 或 square_kilometer。"
        )
    parameters["area_unit"] = area_unit

    try:
        analysis_year = int(arguments.get("analysis_year") or 2025)
        previous_year = int(arguments.get("previous_year") or analysis_year - 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("analysis_year 和 previous_year 必须是整数年份。") from exc
    if not 1900 <= previous_year < analysis_year <= 2200:
        raise ValueError("年份关系无效，必须满足 previous_year < analysis_year。")
    parameters["analysis_year"] = analysis_year
    parameters["previous_year"] = previous_year
    parameters["statistic_date"] = str(
        arguments.get("statistic_date") or date.today().isoformat()
    ).strip()
    return parameters


def _resolve_execution_layers(arguments: dict[str, Any], *, qgis_executor=None) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        from qgis.core import QgsWkbTypes

        resolved = dict(arguments)
        for name in REQUIRED_LAYER_PARAMETERS:
            reference = str(arguments.get(name) or "").strip()
            layer = _find_layer(reference)
            if not layer.isValid() or _layer_type_name(layer) != "vector":
                raise ValueError(f"{name} 必须是有效的矢量图层。")
            if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                raise ValueError(f"{name} 必须是面图层。")
            resolved[name] = layer.id()
        previous = _find_layer(resolved["previous_survey_layer_id"])
        increment = _find_layer(resolved["increment_layer_id"])
        for layer, field_name in (
            (previous, resolved["previous_land_code_field"]),
            (increment, resolved["increment_land_code_field"]),
        ):
            if layer.fields().indexFromName(field_name) < 0:
                raise ValueError(f"字段不存在：{field_name}")
        return resolved

    return _run_qgis(qgis_executor, operation)


def _prepare_execution_parameters(arguments: dict[str, Any], *, qgis_executor=None) -> dict[str, Any]:
    normalized = _normalize_parameters(arguments)
    resolved = _resolve_execution_layers(normalized, qgis_executor=qgis_executor)
    parameters = _normalize_parameters(resolved)
    _validate_projected_metre_crs(parameters["target_crs"], qgis_executor=qgis_executor)
    return parameters


def build_cultivated_land_loss_code(arguments: dict[str, Any]) -> str:
    parameters = dict(arguments)
    if "classification" not in parameters:
        parameters["classification"] = _load_rule_bundle()
    if "report_mapping" not in parameters:
        parameters["report_mapping"] = _load_report_mapping()
    parameters.setdefault("template_path", str(TEMPLATE_PATH.resolve()))
    normalized = _normalize_parameters(parameters)
    parameters.update(normalized)
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    payload = json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
    return f"CULTIVATED_LAND_LOSS_PARAMETERS_JSON = {payload!r}\n{script}"


def _extract_analysis_summary(stdout: object) -> dict[str, Any]:
    for line in reversed(str(stdout or "").splitlines()):
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and "outputs" in payload:
            return payload
    return {}


def build_cultivated_land_loss_analysis_tool(
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

    def preflight(arguments: dict[str, Any]) -> dict[str, Any]:
        if not TEMPLATE_PATH.is_file():
            return {
                "success": False,
                "error": f"Excel 模板不存在：{TEMPLATE_PATH}",
                "preflight_failed": True,
            }
        try:
            parameters = _prepare_execution_parameters(
                arguments, qgis_executor=qgis_executor
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return {"success": False, "error": str(exc), "preflight_failed": True}
        return {"success": True, "arguments": {**arguments, **parameters}}

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            parameters = _prepare_execution_parameters(
                arguments, qgis_executor=qgis_executor
            )
            classification = _load_rule_bundle()
            code = build_cultivated_land_loss_code(
                {
                    **parameters,
                    "classification": classification,
                    "report_mapping": _load_report_mapping(),
                    "template_path": str(TEMPLATE_PATH.resolve()),
                }
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        result = code_executor.handler(
            {
                "code": code,
                "expected_outputs": EXPECTED_OUTPUTS,
                "timeout_seconds": int(arguments.get("timeout_seconds") or 1800),
            }
        )
        analysis_summary = _extract_analysis_summary(result.get("stdout"))
        return {
            **result,
            "analysis_parameters": parameters,
            "analysis_summary": analysis_summary,
            "data_quality": analysis_summary.get("data_quality") or {},
            "rule_version": classification["version"],
            "fixed_script": SCRIPT_PATH.name,
        }

    properties = {
        name: {"type": "string"} for name in REQUIRED_LAYER_PARAMETERS + REQUIRED_FIELD_PARAMETERS
    }
    properties.update(
        {
            "management_area_name": {"type": "string"},
            "target_crs": {
                "type": "string",
                "description": "适合研究区且线性单位为米的投影 CRS。",
            },
            "area_unit": {
                "type": "string",
                "enum": sorted(AREA_UNITS),
                "default": "mu",
            },
            "analysis_year": {"type": "integer", "default": 2025},
            "previous_year": {"type": "integer", "default": 2024},
            "statistic_date": {"type": "string"},
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3600,
                "default": 1800,
            },
        }
    )
    return ToolEntry(
        name="execute_cultivated_land_loss_analysis",
        description=(
            "使用内置固定脚本按输入图层的底层完整要素分析国土变更调查中的耕地流失情况；"
            "活动子集筛选仅记录为警告，不参与统计口径。生成 XLSX 统计表、图斑明细、"
            "指标 CSV、质量报告和面向用户的结构化指标总结。AI 只能传入已检查确认的参数。"
        ),
        parameters={
            "type": "object",
            "properties": properties,
            "required": list(
                REQUIRED_LAYER_PARAMETERS
                + REQUIRED_FIELD_PARAMETERS
                + ("management_area_name", "target_crs")
            ),
            "additionalProperties": False,
        },
        handler=handler,
        category="cultivated_land_loss",
        requires_confirmation=True,
        writes_project=True,
        preflight=preflight,
        execution_affinity="main_thread",
        timeout_seconds=1800,
    )
