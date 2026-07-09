from __future__ import annotations

from pathlib import Path

from ai_gis_qgis.backend.processing.toolbox_catalog import QGISToolboxCatalog
from ai_gis_qgis.backend.skills.skill_manager import SkillManager
from ai_gis_qgis.backend.tools.qgis_toolbox import build_qgis_toolbox_tools
from ai_gis_qgis.backend.tools.registry import ToolEntry, ToolRegistry


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
