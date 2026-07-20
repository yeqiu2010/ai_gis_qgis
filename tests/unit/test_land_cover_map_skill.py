from __future__ import annotations

from pathlib import Path

from ai_gis_qgis.backend.skills.skill_manager import SkillManager
from ai_gis_qgis.backend.tools.code_execution import validate_execute_gis_code_arguments
from ai_gis_qgis.backend.tools.land_cover_map import (
    CLIPPED_RASTER_OUTPUT,
    CLIPPED_VECTOR_OUTPUT,
    DEFAULT_CLASS_MAPPING,
    EXPECTED_OUTPUTS,
    _collect_vector_domain,
    _expected_outputs,
    _infer_legend_title,
    _normalize_parameters,
    build_generate_land_cover_map_tool,
    build_inspect_land_cover_map_inputs_tool,
    build_land_cover_map_code,
)


def test_land_cover_map_skill_uses_inspection_and_fixed_executor():
    manager = SkillManager(Path(__file__).resolve().parents[2] / "skills")

    skill = manager.get("generate-land-cover-map")

    assert skill is not None
    assert skill.version == "1.3.0"
    assert skill.lifecycle == "one-shot"
    assert skill.tools == [
        "list_layers",
        "inspect_layers",
        "inspect_land_cover_map_inputs",
        "get_task_context",
        "generate_land_cover_map",
    ]
    assert "execute_gis_code" not in skill.tools
    assert "Cropland" in skill.body
    assert "指南针" in skill.body
    assert "分类栅格" in skill.body
    assert "boundary_layer_id" in skill.body
    assert "裁剪 + 制图" in skill.body
    assert "gis-pipeline" in skill.description
    assert "不显示图层名" in skill.body
    assert "达到 10 千米" in skill.body


def test_default_land_cover_mapping_matches_required_palette():
    assert DEFAULT_CLASS_MAPPING == [
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


def test_land_cover_map_builds_preflight_safe_raster_code():
    code = build_land_cover_map_code(
        {
            "input_layer_id": "raster-id",
            "source_type": "raster",
            "raster_band": 1,
            "title": "2025 年土地覆盖专题图",
        }
    )

    assert "QgsPalettedRasterRenderer" in code
    assert "QgsPrintLayout" in code
    assert "iface.openLayoutDesigner" in code
    assert "land_cover_map.png" in code
    assert "font.setBold(bool(bold))" in code
    assert "QFont.Bold" not in code
    assert "Qt.AlignHCenter" not in code
    assert 'getattr(Qt, "AlignmentFlag", None)' not in code
    assert 'for enum_name in ("AlignmentFlag", "Alignment")' in code
    assert "QVariant.Int" not in code
    assert "map_item.zoomToExtent(extent)\nlayout.addLayoutItem(map_item)" in code
    assert "map_item.setCrs(layer.crs())" in code
    assert "map_item.refresh()" in code
    assert "scale_bar.setUnitsPerSegment" in code
    assert "QgsLegendRenderer.setNodeLegendStyle" in code
    assert "QgsMapLayerLegendUtils.setLegendNodeOrder" in code
    assert "legend_root.removeAllChildren()" in code
    assert "legend_layer_node.setName(\"\")" in code
    assert "first_category_index = len(legend_nodes) - category_count" in code
    assert "legend_node_indices = list(range(first_category_index, len(legend_nodes)))" in code
    assert "scale_bar.setSegmentSizeMode(fit_width_mode)" in code
    assert "scale_bar.setMinimumBarWidth" in code
    assert "scale_bar.setMaximumBarWidth" in code
    assert 'scale_bar_unit = "m"' in code
    assert 'scale_bar_unit = "km"' in code
    assert "NorthArrow_04.svg" in code
    assert "NorthArrow_01.svg" not in code
    assert validate_execute_gis_code_arguments(
        {"code": code, "expected_outputs": EXPECTED_OUTPUTS}
    ) is None


def test_land_cover_map_builds_boundary_clip_into_fixed_raster_workflow():
    arguments = {
        "input_layer_id": "clcd-raster-id",
        "boundary_layer_id": "wuhan-boundary-id",
        "source_type": "raster",
        "raster_band": 1,
        "title": "武汉市土地覆盖专题图",
    }

    parameters = _normalize_parameters(arguments)
    code = build_land_cover_map_code(arguments)
    outputs = _expected_outputs(parameters)

    assert parameters["scope_mode"] == "boundary_layer"
    assert parameters["boundary_layer_id"] == "wuhan-boundary-id"
    assert 'processing.run(\n            "gdal:cliprasterbymasklayer"' in code
    assert '"CROP_TO_CUTLINE": True' in code
    assert '"KEEP_RESOLUTION": True' in code
    assert '"ALPHA_BAND": True' in code
    assert "land_cover_clipped.tif" in code
    assert outputs == [*EXPECTED_OUTPUTS, CLIPPED_RASTER_OUTPUT]
    assert validate_execute_gis_code_arguments(
        {"code": code, "expected_outputs": outputs}
    ) is None


def test_land_cover_map_scope_defaults_to_full_and_vector_clip_has_fixed_output():
    full_parameters = _normalize_parameters(
        {
            "input_layer_id": "land-cover-id",
            "source_type": "raster",
            "title": "土地覆盖专题图",
        }
    )
    clipped_parameters = _normalize_parameters(
        {
            "input_layer_id": "land-cover-vector-id",
            "boundary_layer_id": "boundary-id",
            "source_type": "vector",
            "classification_field": "Class_ID",
            "title": "土地覆盖专题图",
        }
    )

    assert full_parameters["scope_mode"] == "full_layer"
    assert full_parameters["boundary_layer_id"] is None
    assert _expected_outputs(full_parameters) == EXPECTED_OUTPUTS
    assert _expected_outputs(clipped_parameters) == [*EXPECTED_OUTPUTS, CLIPPED_VECTOR_OUTPUT]
    vector_code = build_land_cover_map_code(clipped_parameters)
    assert 'processing.run(\n            "native:clip"' in vector_code
    assert validate_execute_gis_code_arguments(
        {
            "code": vector_code,
            "expected_outputs": _expected_outputs(clipped_parameters),
        }
    ) is None


def test_legend_title_matches_class_label_language_and_allows_override():
    assert _infer_legend_title(DEFAULT_CLASS_MAPPING) == "Legend"
    assert _infer_legend_title([{"label": "耕地"}, {"label": "林地"}]) == "图例"
    assert _infer_legend_title([{"label": "森林"}, {"label": "水域"}]) == "图例"
    assert _infer_legend_title([{"label": "森林地"}, {"label": "水域マップ"}]) == "凡例"
    assert _infer_legend_title([{"label": "산림"}, {"label": "수역"}]) == "범례"

    chinese_parameters = _normalize_parameters(
        {
            "input_layer_id": "raster-id",
            "source_type": "raster",
            "title": "土地覆盖专题图",
            "class_mapping": [
                {"value": 1, "label": "耕地", "color": [250, 227, 156]},
                {"value": 2, "label": "林地", "color": [68, 111, 51]},
            ],
        }
    )
    custom_parameters = _normalize_parameters(
        {
            "input_layer_id": "raster-id",
            "source_type": "raster",
            "title": "Land Cover Map",
            "legend_title": "Map Key",
        }
    )

    assert chinese_parameters["legend_title"] == "图例"
    assert custom_parameters["legend_title"] == "Map Key"


def test_layout_export_methods_count_as_expected_output_writes():
    code = """
from pathlib import Path
png_output = Path(QGIS_AGENT_WORKSPACE) / "map.png"
pdf_output = Path(QGIS_AGENT_WORKSPACE) / "map.pdf"
exporter.exportToImage(str(png_output), image_settings)
exporter.exportToPdf(str(pdf_output), pdf_settings)
"""

    assert validate_execute_gis_code_arguments(
        {
            "code": code,
            "expected_outputs": [
                {"path": "map.png", "name": "map", "type": "file"},
                {"path": "map.pdf", "name": "map", "type": "file"},
            ],
        }
    ) is None


def test_land_cover_tools_are_read_only_then_confirmed_without_code_parameter():
    inspect_tool = build_inspect_land_cover_map_inputs_tool()
    execute_tool = build_generate_land_cover_map_tool(session_db=None, session_id="test")

    assert inspect_tool.requires_confirmation is False
    assert inspect_tool.writes_project is False
    assert execute_tool.requires_confirmation is True
    assert execute_tool.writes_project is True
    assert "code" not in execute_tool.parameters["properties"]
    assert "expected_outputs" not in execute_tool.parameters["properties"]
    assert "boundary_layer_id" in inspect_tool.parameters["properties"]
    assert "boundary_layer_id" in execute_tool.parameters["properties"]


def test_vector_domain_preserves_actual_string_and_numeric_values():
    class Fields:
        def indexFromName(self, name):
            return 0 if name == "Class_ID" else -1

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
            return iter([Feature(1), Feature(1), Feature("Forest"), Feature(None)])

    domain = _collect_vector_domain(Layer(), "Class_ID")

    assert domain["available_values"] == [1, "Forest"]
    assert domain["value_counts"][0] == {"value": 1, "count": 2}
    assert domain["null_count"] == 1
    assert domain["domain_complete"] is True
