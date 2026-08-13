from __future__ import annotations

from pathlib import Path

from ai_gis_qgis.backend.agent_core import AgentCore
from ai_gis_qgis.backend.context.prompt_builder import PromptBuilder
from ai_gis_qgis.backend.context.qgis_context import QGISContext
from ai_gis_qgis.backend.llm.base_provider import ChatResponse
from ai_gis_qgis.database.session_db import SessionDB


class NoopProvider:
    name = "noop"
    model = "noop"

    def chat(self, system, messages, tools=None):
        return ChatResponse(content="noop", model=self.model)


def test_sam3_routing_catalog_explicitly_covers_vegetation_and_followup_statistics():
    builder = PromptBuilder()
    catalog = {
        str(item["name"]): item
        for item in builder.skill_manager.routing_catalog(include_unavailable=True)
    }

    description = str(catalog["sam3-remote-segmentation"]["description"])
    assert "植被" in description
    assert "面积/占比统计" in description


def test_main_prompt_requires_plan_then_sam3_then_pipeline_for_vegetation_ratio():
    prompt = PromptBuilder().build(
        "main-orchestrator",
        QGISContext(project_path="", layer_count=0, layers=[]),
        loaded_skills=["main-orchestrator"],
    )

    plan_position = prompt.index("先 create_plan")
    sam3_position = prompt.index("再加载 SAM3 Skill")
    pipeline_position = prompt.index("SAM3 真实分割图层产生前不得加载 gis-pipeline")
    assert plan_position < sam3_position < pipeline_position
    assert "提取/识别植被" in prompt
    assert "只有用户明确要求光谱指数或分类方法时才使用 NDVI" in prompt
    assert "每个阈值建立独立步骤和唯一输出名" in prompt
    assert "duplicate_prevented=true" in prompt


def test_sam3_artifact_preserves_completed_threshold_parameters():
    artifacts = AgentCore._artifacts_from_tool_result(
        "segment_remote_sensing_image",
        {
            "success": True,
            "job_id": "job-05",
            "confidence_threshold": 0.5,
            "parameters": {
                "input_layer_id": "whch-id",
                "prompt": "building",
                "confidence_threshold": 0.5,
            },
            "outputs": [
                {
                    "path": "/tmp/building_threshold_0_5.gpkg",
                    "name": "building_threshold_0_5",
                    "type": "vector",
                }
            ],
            "loaded_layers": [{"id": "building-05"}],
        },
    )

    aggregate = next(
        artifact
        for artifact in artifacts
        if artifact["artifact_type"] == "segmentation_outputs"
    )
    assert aggregate["payload"]["confidence_threshold"] == 0.5
    assert aggregate["payload"]["parameters"]["confidence_threshold"] == 0.5


def test_standalone_sam3_artifact_registration_is_a_safe_noop(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="standalone SAM3", model="noop")
    core = AgentCore(session_db=session_db, llm_provider=NoopProvider(), iface=None)

    result, _ = core._build_tool_registry(session.id).execute(
        "register_artifact",
        {
            "artifact_type": "segmentation_outputs",
            "name": "SAM3 road outputs",
            "payload": {"job_id": "job-road"},
            "verified": True,
        },
    )

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["already_captured"] is True


def test_pipeline_activation_requires_a_persisted_plan(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="routing guard", model="noop")
    core = AgentCore(session_db=session_db, llm_provider=NoopProvider(), iface=None)

    failure = core._skill_activation_precondition(
        session.id,
        "load_skill",
        {"skill_name": "gis-pipeline"},
    )

    assert failure is not None
    assert failure["error_code"] == "plan_required_before_pipeline"


def test_pipeline_cannot_bypass_planned_sam3_step(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="routing order", model="noop")
    task_id = session_db.create_task(
        session.id,
        "提取 satellite 中的植被并统计面积占比",
    )
    session_db.replace_plan_steps(
        task_id,
        [
            {
                "id": "segment",
                "skill_name": "sam3-remote-segmentation",
                "instruction": "使用 SAM3 分割植被",
                "dependencies": [],
            },
            {
                "id": "statistics",
                "skill_name": "gis-pipeline",
                "instruction": "使用真实分割图层统计面积占比",
                "dependencies": ["segment"],
            },
        ],
    )
    core = AgentCore(session_db=session_db, llm_provider=NoopProvider(), iface=None)

    blocked = core._skill_activation_precondition(
        session.id,
        "load_skill",
        {"skill_name": "gis-pipeline"},
    )
    assert blocked is not None
    assert blocked["error_code"] == "skill_order_violation"
    assert blocked["required_skill"] == "sam3-remote-segmentation"

    session_db.update_plan_step(task_id, "segment", status="completed")
    allowed = core._skill_activation_precondition(
        session.id,
        "load_skill",
        {"skill_name": "gis-pipeline"},
    )
    assert allowed is None
