from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from ai_gis_qgis.backend.processing.algorithm_evidence import (
    processing_algorithm_ids,
    processing_evidence_state_key,
    read_processing_evidence,
)
from ai_gis_qgis.backend.processing.toolbox_catalog import QGISToolboxCatalog
from ai_gis_qgis.backend.skills.skill_manager import SkillManager
from ai_gis_qgis.backend.tools.code_execution import validate_execute_gis_code_arguments
from ai_gis_qgis.backend.tools.land_use_building_metrics import (
    EXPECTED_OUTPUTS as LAND_USE_BUILDING_EXPECTED_OUTPUTS,
)
from ai_gis_qgis.backend.tools.land_use_building_metrics import (
    _collect_value_domain,
    build_land_use_building_metrics_code,
)
from ai_gis_qgis.backend.tools.qgis_toolbox import build_qgis_toolbox_tools
from ai_gis_qgis.backend.tools.registry import ToolEntry, ToolRegistry
from ai_gis_qgis.backend.tools.school_service_coverage import (
    EXPECTED_OUTPUTS,
    _collect_school_type_domain,
    _resolve_execution_layer_ids,
    _validate_target_crs,
    build_school_service_coverage_code,
)
from ai_gis_qgis.backend.tools.skill_management import build_search_skills_tool
from ai_gis_qgis.database.session_db import SessionDB


def test_qgis_toolbox_catalog_searches_domains_and_tools():
    catalog = QGISToolboxCatalog()

    domains = catalog.search_domains("坡度 DEM 地形分析")
    assert domains
    assert domains[0]["domain"] == "qgis-raster-terrain"

    tools = catalog.search_tools("slope 坡度 DEM", domain="qgis-raster-terrain")
    assert any(tool["tool_id"] == "gdal:slope" for tool in tools)


def test_qgis_business_terms_recall_expected_processing_algorithms():
    catalog = QGISToolboxCatalog()
    cases = {
        "筛选中小学": {"native:extractbyattribute", "native:extractbyexpression"},
        "500米服务半径": {"native:buffer"},
        "与住宅区重叠": {"native:intersection"},
        "按街道统计汇总": {"native:aggregate", "qgis:statisticsbycategories"},
        "计算覆盖率并添加字段": {"native:fieldcalculator"},
        "统一坐标系": {"native:reprojectlayer", "gdal:warpreproject"},
    }

    for query, expected_ids in cases.items():
        result_ids = {
            item["tool_id"] for item in catalog.search_tools(query, limit=12)
        }
        assert result_ids.intersection(expected_ids), query


def test_qgis_toolbox_get_tool_returns_catalog_detail():
    registry = ToolRegistry()
    for entry in build_qgis_toolbox_tools(session_db=None, session_id="s"):
        registry.register(entry)

    result, _ = registry.execute("get_qgis_processing_tool", {"tool_id": "gdal:slope"})
    assert result["success"] is True
    assert result["tool"]["tool_id"] == "gdal:slope"
    assert "parameters" in result["tool"]


def test_qgis_toolbox_searches_multiple_queries_in_one_call():
    registry = ToolRegistry()
    for entry in build_qgis_toolbox_tools(session_db=None, session_id="s"):
        registry.register(entry)

    result, _ = registry.execute(
        "search_qgis_processing_tools",
        {
            "queries": ["计算建筑面积并添加字段", "field calculator", "area attribute expression"],
            "domain": "qgis-vector-table",
        },
    )

    assert result["success"] is True
    assert len(result["queries"]) == 3
    assert result["tools"][0]["tool_id"] == "native:fieldcalculator"
    assert result["tools"][0]["matched_queries"]


def test_qgis_toolbox_gets_multiple_tool_details_in_one_call():
    registry = ToolRegistry()
    for entry in build_qgis_toolbox_tools(session_db=None, session_id="s"):
        registry.register(entry)

    result, _ = registry.execute(
        "get_qgis_processing_tool",
        {"tool_ids": ["native:fieldcalculator", "gdal:slope"]},
    )

    assert result["success"] is True
    assert [tool["tool_id"] for tool in result["tools"]] == ["native:fieldcalculator", "gdal:slope"]
    assert "tool" not in result


def test_qgis_toolbox_persists_exact_algorithm_evidence_for_pipeline(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="evidence", model="test", source="test")
    registry = ToolRegistry()
    for entry in build_qgis_toolbox_tools(
        session_db=session_db,
        session_id=session.id,
    ):
        registry.register(entry)

    result, _ = registry.execute(
        "get_qgis_processing_tool",
        {"tool_ids": ["gdal:cliprasterbyextent", "gdal:rastercalculator"]},
    )

    assert result["success"] is True
    evidence = read_processing_evidence(session_db, session.id)
    assert set(evidence) == {"gdal:cliprasterbyextent", "gdal:rastercalculator"}
    clip_evidence = evidence["gdal:cliprasterbyextent"]
    assert "PROJWIN: Clipping extent" in clip_evidence["parameters"]
    assert "EXTENT: Clipping extent" not in clip_evidence["parameters"]
    assert "'PROJWIN': '0,10,0,10'" in clip_evidence["code_example"]


def test_heatmap_catalog_example_uses_singular_output_value_and_compiles():
    catalog = QGISToolboxCatalog()
    detail = catalog.get("qgis:heatmapkerneldensityestimation").detail()

    assert "OUTPUT_VALUE: Output value scaling" in detail["parameters"]
    assert "OUTPUT_VALUES" not in detail["parameters"]
    assert "'OUTPUT_VALUE': 0" in detail["code_example"]
    compile(detail["code_example"], "<heatmap-catalog-example>", "exec")


def test_processing_evidence_repairs_clip_extent_key_saved_by_older_build(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="legacy evidence", model="test", source="test")
    session_db.set_state(
        processing_evidence_state_key(session.id),
        json.dumps(
            {
                "gdal:cliprasterbyextent": {
                    "tool_id": "gdal:cliprasterbyextent",
                    "parameters": "INPUT: Input raster\nEXTENT: Clipping extent\nOUTPUT: Result",
                    "code_example": "params = {'INPUT': raster, 'EXTENT': road, 'OUTPUT': out}",
                }
            }
        ),
    )

    detail = read_processing_evidence(session_db, session.id)[
        "gdal:cliprasterbyextent"
    ]

    assert "PROJWIN: Clipping extent" in detail["parameters"]
    assert "'PROJWIN': road" in detail["code_example"]


def test_processing_algorithm_ids_include_fallback_algorithms_but_not_crs_ids():
    assert processing_algorithm_ids(
        {
            "algorithm": "qgis:heatmapkerneldensityestimation",
            "crs_strategy": "统一使用 EPSG:2385",
            "fallback_plan": "必要时使用 gdal:cliprasterbyextent 裁剪",
        }
    ) == {"qgis:heatmapkerneldensityestimation", "gdal:cliprasterbyextent"}


def test_qgis_toolbox_exposes_search_only_without_direct_runner():
    registry = ToolRegistry()
    for entry in build_qgis_toolbox_tools(session_db=None, session_id="s"):
        registry.register(entry)

    names = [entry["function"]["name"] for entry in registry.definitions_for_skill("unknown")]
    assert names == [
        "search_qgis_toolbox_domains",
        "search_qgis_processing_tools",
        "get_qgis_processing_tool",
    ]
    assert "run_qgis_processing" not in names


def test_pipeline_skill_combines_algorithm_discovery_with_single_code_executor():
    skills_dir = Path(__file__).resolve().parents[2] / "skills"
    manager = SkillManager(skills_dir)

    tools = manager.tool_allowlist()["gis-pipeline"]

    assert "search_qgis_processing_tools" in tools
    assert "get_qgis_processing_tool" in tools
    assert "execute_gis_code" in tools
    assert "set_active_skill" not in tools
    assert "run_qgis_processing" not in tools


def test_school_service_coverage_skill_uses_dynamic_inspection_and_single_executor():
    skills_dir = Path(__file__).resolve().parents[2] / "skills"
    manager = SkillManager(skills_dir)

    skill = manager.get("calculate-school-service-coverage")

    assert skill is not None
    assert skill.version == "2.1.2"
    assert skill.lifecycle == "one-shot"
    assert skill.tools == [
        "list_layers",
        "inspect_layers",
        "inspect_school_service_coverage_inputs",
        "get_task_context",
        "execute_school_service_coverage",
    ]
    assert "record_pipeline_stage" not in skill.tools
    assert "execute_gis_code" not in skill.tools
    assert "不计算覆盖率简单平均值" in skill.body
    assert "不得假定图层固定名为“学校”" in skill.body
    assert "实际值为 `[\"中小学\", \"高等院校\", \"幼托机构\"]`" in skill.body


def test_school_service_coverage_builds_preflight_safe_fixed_code():
    code = build_school_service_coverage_code(
        {
            "school_layer_id": "school-id",
            "residential_layer_id": "residential-id",
            "school_type_field": "CCN",
            "school_type_values": ["小学", "初中"],
            "residential_area_field": "面积",
            "area_unit": "square_meter",
            "group_field": "XZQMC",
            "service_distance_m": 500,
            "target_crs": "EPSG:4547",
        }
    )

    assert "SCHOOL_SERVICE_PARAMETERS_JSON" in code
    assert "native:buffer" in code
    assert "DISSOLVE\": True" in code
    assert "school_service_coverage.gpkg" in code
    assert "missing_school_type_values" in code
    assert validate_execute_gis_code_arguments(
        {"code": code, "expected_outputs": EXPECTED_OUTPUTS}
    ) is None


def test_school_service_coverage_resolves_layer_names_to_live_ids(monkeypatch):
    class Layer:
        def __init__(self, layer_id):
            self._layer_id = layer_id

        def id(self):
            return self._layer_id

        def isValid(self):
            return True

    layers = {
        "学校": Layer("school-live-id"),
        "城镇住宅区": Layer("residential-live-id"),
    }
    monkeypatch.setattr(
        "ai_gis_qgis.backend.tools.school_service_coverage._find_layer",
        lambda reference: layers[reference],
    )
    monkeypatch.setattr(
        "ai_gis_qgis.backend.tools.school_service_coverage._layer_type_name",
        lambda layer: "vector",
    )

    resolved = _resolve_execution_layer_ids(
        {
            "school_layer_id": "学校",
            "residential_layer_id": "城镇住宅区",
            "area_unit": "square_meter",
        }
    )

    assert resolved["school_layer_id"] == "school-live-id"
    assert resolved["residential_layer_id"] == "residential-live-id"
    assert resolved["area_unit"] == "square_meter"


def test_school_service_coverage_rejects_geographic_target_crs_before_execution(
    monkeypatch,
):
    qgis_module = types.ModuleType("qgis")
    qgis_core_module = types.ModuleType("qgis.core")

    class FakeCrs:
        def __init__(self, auth_id):
            self.auth_id = auth_id

        def isValid(self):
            return True

        def isGeographic(self):
            return self.auth_id == "EPSG:4490"

        def mapUnits(self):
            return "meters"

    class FakeQgis:
        class DistanceUnit:
            Meters = "meters"

    qgis_core_module.__dict__["Qgis"] = FakeQgis
    qgis_core_module.__dict__["QgsCoordinateReferenceSystem"] = FakeCrs
    monkeypatch.setitem(sys.modules, "qgis", qgis_module)
    monkeypatch.setitem(sys.modules, "qgis.core", qgis_core_module)

    with pytest.raises(ValueError, match="必须是有效的投影 CRS"):
        _validate_target_crs("EPSG:4490")

    _validate_target_crs("EPSG:4526")


def test_school_type_domain_uses_actual_complete_field_values():
    class Fields:
        def indexFromName(self, name):
            return 0 if name == "类别" else -1

    class Feature:
        def __init__(self, value):
            self.value = value

        def __getitem__(self, index):
            assert index == 0
            return self.value

    class Layer:
        def fields(self):
            return Fields()

        def getFeatures(self):
            return iter(
                [
                    Feature("中小学"),
                    Feature("高等院校"),
                    Feature("中小学"),
                    Feature("幼托机构"),
                    Feature(None),
                ]
            )

    domain = _collect_school_type_domain(Layer(), "类别")

    assert domain["available_values"] == ["中小学", "幼托机构", "高等院校"]
    assert domain["value_counts"][0] == {"value": "中小学", "count": 2}
    assert domain["null_count"] == 1
    assert domain["domain_complete"] is True


def test_land_use_building_metrics_skill_uses_fixed_inspection_and_execution():
    skills_dir = Path(__file__).resolve().parents[2] / "skills"
    manager = SkillManager(skills_dir)

    skill = manager.get("calculate-land-use-building-metrics")

    assert skill is not None
    assert skill.version == "1.0.0"
    assert skill.lifecycle == "one-shot"
    assert skill.tools == [
        "list_layers",
        "inspect_layers",
        "inspect_land_use_building_metrics_inputs",
        "get_task_context",
        "execute_land_use_building_metrics",
    ]
    assert "execute_gis_code" not in skill.tools
    assert "scope_mode=full_layer" in skill.body
    assert "面内点" in skill.body
    assert "建筑密度固定为" in skill.body


def test_land_use_building_metrics_builds_preflight_safe_fixed_code():
    code = build_land_use_building_metrics_code(
        {
            "land_layer_id": "land-id",
            "building_layer_id": "building-id",
            "land_type_field": "用地_1",
            "land_area_field": "Shape_Area",
            "land_area_unit": "square_meter",
            "building_height_field": "HEIGHT",
            "building_footprint_field": "FAREA",
            "building_floor_area_field": "GBAREA",
            "building_area_unit": "square_meter",
            "scope_mode": "boundary_layer",
            "boundary_layer_id": "street-id",
            "target_crs": "EPSG:4547",
        }
    )

    assert "LAND_USE_BUILDING_PARAMETERS_JSON" in code
    assert "pointOnSurface" in code
    assert "scope_ratio" in code
    assert "land_use_building_metrics.gpkg" in code
    assert validate_execute_gis_code_arguments(
        {"code": code, "expected_outputs": LAND_USE_BUILDING_EXPECTED_OUTPUTS}
    ) is None


def test_land_use_domain_uses_actual_complete_field_values():
    class Fields:
        def indexFromName(self, name):
            return 0 if name == "用地类型" else -1

    class Feature:
        def __init__(self, value):
            self.value = value

        def __getitem__(self, index):
            assert index == 0
            return self.value

    class Layer:
        def fields(self):
            return Fields()

        def getFeatures(self):
            return iter(
                [
                    Feature("居住用地"),
                    Feature("商业用地"),
                    Feature("居住用地"),
                    Feature(None),
                ]
            )

    domain = _collect_value_domain(Layer(), "用地类型")

    assert domain["available_values"] == ["居住用地", "商业用地"]
    assert domain["value_counts"][0] == {"value": "居住用地", "count": 2}
    assert domain["null_count"] == 1
    assert domain["domain_complete"] is True


def test_skill_catalog_leaves_semantic_selection_to_ai():
    skills_dir = Path(__file__).resolve().parents[2] / "skills"
    manager = SkillManager(skills_dir)
    tool = build_search_skills_tool(manager)

    result = tool.handler(
        {
            "query": "从学校图层和城镇住宅区图层中计算出不同街道的中小学服务半径覆盖率",
            "include_builtin": True,
        }
    )

    cards = {item["name"]: item for item in result["skills"]}
    assert result["matching"] == "ai_semantic_selection_required"
    assert "calculate-school-service-coverage" in cards
    assert "score" not in cards["calculate-school-service-coverage"]
    assert "solution-planner" not in cards
    assert "code-generator" not in cards
    assert "gis-pipeline" in cards


def test_catalog_domains_are_not_registered_as_skills():
    skills_dir = Path(__file__).resolve().parents[2] / "skills"
    manager = SkillManager(skills_dir)

    loaded = set(manager.all())

    assert "qgis-toolbox" in loaded
    assert not loaded.intersection(
        {
            "qgis-data-management",
            "qgis-raster-analysis",
            "qgis-raster-terrain",
            "qgis-vector-geometry",
            "qgis-vector-overlay",
            "qgis-vector-selection",
            "qgis-vector-table",
        }
    )


def test_tool_registry_filters_definitions_by_skill_tools():
    registry = ToolRegistry()
    registry.set_skill_tools({"sample-skill": ["allowed_tool"]})
    registry.register(
        ToolEntry(
            name="allowed_tool",
            description="allowed",
            parameters={"type": "object"},
            handler=lambda arguments: {},
            category="test",
        )
    )
    registry.register(
        ToolEntry(
            name="hidden_tool",
            description="hidden",
            parameters={"type": "object"},
            handler=lambda arguments: {},
            category="test",
        )
    )
    registry.register(
        ToolEntry(
            name="set_active_skill",
            description="switch",
            parameters={"type": "object"},
            handler=lambda arguments: {},
            category="skill",
        )
    )

    names = [definition["function"]["name"] for definition in registry.definitions_for_skill("sample-skill")]
    assert names == ["allowed_tool", "set_active_skill"]
    unfiltered = [definition["function"]["name"] for definition in registry.definitions_for_skill("other")]
    assert "hidden_tool" in unfiltered


def test_skill_manager_loads_builtin_and_custom_skill_dirs(tmp_path: Path):
    builtin = tmp_path / "builtin"
    custom = tmp_path / "custom"
    (builtin / "main").mkdir(parents=True)
    (custom / "landuse").mkdir(parents=True)
    (builtin / "main" / "SKILL.md").write_text("---\nname: main\n---\n# Main\n", encoding="utf-8")
    (custom / "landuse" / "SKILL.md").write_text(
        "---\nname: landuse\ntools:\n  - create_landuse_status_map\n---\n# Landuse\n",
        encoding="utf-8",
    )

    manager = SkillManager([builtin, custom])

    assert manager.get("main") is not None
    assert manager.get("landuse") is not None
    assert manager.tool_allowlist()["landuse"] == ["create_landuse_status_map"]
