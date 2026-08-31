from __future__ import annotations

from pathlib import Path

import pytest
from ai_gis_qgis.backend.agent_core import AgentCore
from ai_gis_qgis.backend.llm.base_provider import ChatMessage, ChatResponse, ToolCall
from ai_gis_qgis.backend.plugin_system import PluginManager
from ai_gis_qgis.backend.skills.skill_manager import SkillManager
from ai_gis_qgis.backend.tools.registry import ToolEntry, ToolRegistry
from ai_gis_qgis.database.session_db import SessionDB


class StructuredToolProvider:
    name = "structured-tool-test"
    model = "structured-tool-test"

    def __init__(self):
        self.calls = 0
        self.messages: list[list[ChatMessage]] = []

    def chat(self, system, messages, tools=None):
        del system, tools
        self.calls += 1
        self.messages.append(list(messages))
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                reasoning_content="需要先调用图层工具。",
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="layers-1", name="list_layers", arguments={})],
            )
        return ChatResponse(content="完成", model=self.model)


def test_agent_uses_native_assistant_tool_chain(tmp_path: Path):
    database = SessionDB(tmp_path / "state.db")
    session = database.create_session(title="structured")
    provider = StructuredToolProvider()

    AgentCore(session_db=database, llm_provider=provider, iface=None).run(
        session_id=session.id,
        user_message="列出图层",
    )

    second_call = provider.messages[1]
    assert second_call[-2].role == "assistant"
    assert second_call[-2].reasoning_content == "需要先调用图层工具。"
    assert second_call[-2].tool_calls[0]["id"] == "layers-1"
    assert second_call[-1].role == "tool"
    assert second_call[-1].tool_call_id == "layers-1"
    assert "以下是刚刚执行的 QGIS 工具结果" not in second_call[-1].content


def test_builtin_sam3_plugin_registers_tools_and_namespaced_skill(tmp_path: Path):
    database = SessionDB(tmp_path / "state.db")
    session = database.create_session(title="plugin")
    manager = SkillManager(Path(__file__).resolve().parents[2] / "skills")
    registry = ToolRegistry()
    plugins = PluginManager(
        skill_manager=manager,
        runtime={
            "session_db": database,
            "session_id": session.id,
            "plugin_config": {"sam3": {}},
        },
    )

    diagnostics = plugins.register_all(registry)

    assert diagnostics[0].status == "loaded"
    assert "segment_remote_sensing_image" in registry.tool_names()
    assert manager.get("sam3:remote-segmentation") is not None
    assert manager.get("sam3-remote-segmentation").name == "sam3:remote-segmentation"


def test_tool_registry_rejects_cross_source_collisions():
    registry = ToolRegistry()
    first = ToolEntry(
        name="same",
        description="first",
        parameters={"type": "object"},
        handler=lambda _: {"success": True},
        category="test",
        source="plugin:first",
    )
    registry.register(first)

    with pytest.raises(ValueError, match="Tool name collision"):
        registry.register(
            ToolEntry(
                name="same",
                description="second",
                parameters={"type": "object"},
                handler=lambda _: {"success": True},
                category="test",
                source="plugin:second",
            )
        )


def test_recipe_candidates_require_review_before_activation(tmp_path: Path):
    database = SessionDB(tmp_path / "state.db")
    candidate_id = database.save_knowledge_candidate(
        candidate_type="recipe",
        target_name="buffer-segment-export",
        payload={
            "name": "buffer-segment-export",
            "intent": "buffer_segment_export",
            "steps": [],
        },
        evidence={"success_count": 1},
        evaluation={"passed": False},
    )

    assert database.search_recipes("buffer") == []
    proposal = database.curator_dry_run()[0]
    assert proposal["candidate_id"] == candidate_id
    assert proposal["action"] == "retain_candidate"

    recipe_id = database.promote_candidate(candidate_id)
    recipe = database.get_recipe(recipe_id)
    assert recipe is not None
    assert recipe["status"] == "active"
    assert database.search_recipes("buffer")[0]["id"] == recipe_id
