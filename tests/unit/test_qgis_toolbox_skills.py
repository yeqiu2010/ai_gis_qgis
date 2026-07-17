from __future__ import annotations

from pathlib import Path

from ai_gis_qgis.backend.processing.toolbox_catalog import QGISToolboxCatalog
from ai_gis_qgis.backend.skills.skill_manager import SkillManager
from ai_gis_qgis.backend.tools.code_execution import validate_execute_gis_code_arguments
from ai_gis_qgis.backend.tools.qgis_toolbox import build_qgis_toolbox_tools
from ai_gis_qgis.backend.tools.registry import ToolEntry, ToolRegistry
from ai_gis_qgis.backend.tools.school_service_coverage import (
    EXPECTED_OUTPUTS,
    _collect_school_type_domain,
    build_school_service_coverage_code,
)
from ai_gis_qgis.backend.tools.skill_management import build_search_skills_tool


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
    assert skill.version == "2.1.0"
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
