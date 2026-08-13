from __future__ import annotations

from pathlib import Path

from ai_gis_qgis.backend.skills.skill_manager import SkillManager
from ai_gis_qgis.backend.tools.sam3_segmentation import build_sam3_tools


def test_sam3_tools_are_trusted_and_confirmation_gated():
    entries = build_sam3_tools(
        config={"base_url": "http://127.0.0.1:8000"},
        executor_config={},
        session_db=None,
        session_id="session",
    )
    by_name = {entry.name: entry for entry in entries}

    assert set(by_name) == {
        "check_sam3_service",
        "inspect_sam3_segmentation_inputs",
        "segment_remote_sensing_image",
    }
    assert by_name["check_sam3_service"].requires_confirmation is False
    assert by_name["segment_remote_sensing_image"].requires_confirmation is True
    assert by_name["segment_remote_sensing_image"].writes_project is True
    assert by_name["segment_remote_sensing_image"].preflight is not None


def test_sam3_skill_is_discoverable_with_registered_tools():
    skills_dir = Path(__file__).resolve().parents[2] / "skills"
    manager = SkillManager(skills_dir)
    manager.set_runtime_capabilities(
        available_tools={
            "list_layers",
            "inspect_layer",
            "inspect_layers",
            "check_sam3_service",
            "inspect_sam3_segmentation_inputs",
            "segment_remote_sensing_image",
            "create_plan",
            "revise_plan",
            "get_task_state",
            "update_plan_step",
            "complete_plan_step",
            "register_artifact",
            "finalize_task",
            "load_skill",
        },
        active_toolsets={"default"},
    )

    available, reasons = manager.availability("sam3-remote-segmentation")
    document = manager.get("sam3-remote-segmentation")

    assert available is True, reasons
    assert document is not None
    assert "segment_remote_sensing_image" in document.tools
    assert document.execution_contract["outputs"]["segmentation_outputs"]["type"] == "array"
