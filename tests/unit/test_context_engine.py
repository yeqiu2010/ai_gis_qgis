from __future__ import annotations

import json

import pytest
from ai_gis_qgis.backend.agent_core import AgentCore
from ai_gis_qgis.backend.context.context_engine import QGISContextEngine
from ai_gis_qgis.backend.context.qgis_context import QGISContext
from ai_gis_qgis.backend.llm.base_provider import ChatMessage, ChatResponse
from ai_gis_qgis.backend.llm.errors import ContextWindowExceeded
from ai_gis_qgis.backend.llm.openai_provider import OpenAICompatibleProvider
from ai_gis_qgis.backend.tools.pipeline import start_pipeline_cycle
from ai_gis_qgis.backend.tools.skill_management import (
    read_loaded_skills,
    write_loaded_skills,
)
from ai_gis_qgis.database.session_db import SessionDB


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def test_context_engine_externalizes_completed_code_and_reduces_result():
    engine = QGISContextEngine(
        context_window_tokens=8000,
        max_output_tokens=4096,
        minimum_output_tokens=2048,
    )
    messages = [
        ChatMessage(role="user", content="执行复杂GIS分析"),
        ChatMessage(
            role="assistant",
            tool_calls=[
                _tool_call(
                    "code-1",
                    "execute_gis_code",
                    {"code": "print('x')\n" * 3000, "expected_outputs": []},
                )
            ],
        ),
        ChatMessage(
            role="tool",
            name="execute_gis_code",
            tool_call_id="code-1",
            content=json.dumps(
                {
                    "success": True,
                    "stdout": "done\n" * 3000,
                    "outputs": [{"path": "result.geojson", "type": "vector"}],
                }
            ),
        ),
    ]

    prepared = engine.prepare(system="system", messages=messages, tools=[])

    assert prepared.compacted is True
    arguments = json.loads(prepared.messages[1].tool_calls[0]["function"]["arguments"])
    assert arguments["code"]["externalized"] is True
    assert arguments["code"]["chars"] > 1000
    result = json.loads(prepared.messages[2].content)
    assert result["success"] is True
    assert result["outputs"][0]["path"] == "result.geojson"
    assert result["context_compacted"] is True
    assert prepared.estimated_input_tokens <= prepared.input_limit_tokens


def test_context_engine_never_externalizes_unresolved_tool_arguments():
    code = "print('pending')\n" * 1000
    engine = QGISContextEngine(
        context_window_tokens=5000,
        max_output_tokens=2048,
        minimum_output_tokens=1024,
    )
    messages = [
        ChatMessage(role="user", content="等待确认"),
        ChatMessage(
            role="assistant",
            tool_calls=[_tool_call("pending-1", "execute_gis_code", {"code": code})],
        ),
    ]

    with pytest.raises(ContextWindowExceeded):
        engine.prepare(system="system", messages=messages, tools=[])

    # The caller-owned provider-native chain is never mutated by a failed
    # compaction attempt, so pending confirmation parameters remain exact.
    arguments = json.loads(messages[1].tool_calls[0]["function"]["arguments"])
    assert arguments["code"] == code


def test_context_engine_rejects_fixed_prompt_that_starves_output():
    engine = QGISContextEngine(
        context_window_tokens=4096,
        max_output_tokens=2048,
        minimum_output_tokens=2048,
    )

    with pytest.raises(ContextWindowExceeded, match="系统提示词和工具定义"):
        engine.prepare(system="规则" * 3000, messages=[], tools=[])


def test_compaction_summary_is_not_a_synthetic_assistant_turn():
    engine = QGISContextEngine()
    messages = [
        ChatMessage(role="user", content="旧请求" * 1200),
        ChatMessage(
            role="assistant",
            content="旧回答" * 1200,
            reasoning_content="旧推理",
        ),
        ChatMessage(role="user", content="中间请求" * 1200),
        ChatMessage(
            role="assistant",
            content="中间回答" * 1200,
            reasoning_content="中间推理",
        ),
        ChatMessage(role="user", content="当前请求"),
    ]

    compacted = engine._compact_middle_messages(messages, message_budget=512)

    summary = next(
        message
        for message in compacted
        if message.content.startswith("[CONTEXT COMPACTION")
    )
    assert summary.role == "user"


def test_new_user_task_does_not_reload_old_raw_tool_exchange(tmp_path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="bounded history", model="test", source="test")
    for index in range(12):
        session_db.save_message(session.id, "user", f"历史任务 {index}", event_type="user")
        call_id = f"old-code-{index}"
        session_db.save_message(
            session.id,
            "assistant",
            "",
            event_type="tool_call",
            tool_calls=[
                _tool_call(
                    call_id,
                    "execute_gis_code",
                    {"code": f"OLD_RAW_CODE_{index}\n" * 500},
                )
            ],
        )
        session_db.save_message(
            session.id,
            "tool",
            json.dumps({"success": True, "stdout": "large\n" * 1000}),
            event_type="tool_result",
            tool_call_id=call_id,
            tool_name="execute_gis_code",
        )
        session_db.save_message(
            session.id,
            "assistant",
            f"历史任务 {index} 已完成。",
            event_type="summary",
        )
    session_db.save_message(session.id, "user", "现在统计建筑密度", event_type="user")

    rows = session_db.get_context_messages(session.id, historical_limit=8)
    serialized = json.dumps(rows, ensure_ascii=False)

    assert "OLD_RAW_CODE" not in serialized
    assert rows[-1]["content"] == "现在统计建筑密度"
    assert len(rows) <= 9


def test_active_task_chain_keeps_native_tool_pair(tmp_path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="active chain", model="test", source="test")
    session_db.save_message(session.id, "user", "当前任务", event_type="user")
    call = _tool_call("live-1", "inspect_layer", {"layer_name": "building"})
    session_db.save_message(
        session.id,
        "assistant",
        "",
        event_type="tool_call",
        tool_calls=[call],
        reasoning_content="需要先检查建筑图层。",
    )
    session_db.save_message(
        session.id,
        "tool",
        json.dumps({"success": True, "layer": {"id": "building-id"}}),
        event_type="tool_result",
        tool_call_id="live-1",
        tool_name="inspect_layer",
    )

    rows = session_db.get_context_messages(session.id)

    assert rows[-2]["tool_calls"][0]["id"] == "live-1"
    assert rows[-2]["reasoning_content"] == "需要先检查建筑图层。"
    assert rows[-1]["tool_call_id"] == "live-1"

    restored = AgentCore._chat_message_from_row(rows[-2])
    assert restored.as_api_message()["reasoning_content"] == "需要先检查建筑图层。"


def test_pipeline_runtime_loads_only_current_stage_include(tmp_path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="stage disclosure", model="test", source="test")
    start_pipeline_cycle(session_db, session.id)
    core = AgentCore(session_db=session_db, llm_provider=_NoopProvider(), iface=None)

    prompt = core._build_system_prompt(
        session.id,
        "gis-pipeline",
        QGISContext(project_path="", layer_count=0, layers=[]),
        loaded_skills=["main-orchestrator", "gis-pipeline"],
    )

    assert "Included Skill: data-overview" in prompt
    assert "Included Skill: code-generator" not in prompt
    assert "Included Skill: code-reviewer" not in prompt


def test_task_scoped_skill_is_unloaded_after_terminal_task(tmp_path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="task lifecycle", model="test", source="test")
    core = AgentCore(session_db=session_db, llm_provider=_NoopProvider(), iface=None)
    write_loaded_skills(
        session_db.set_state,
        session.id,
        core.prompt_builder.skill_manager,
        ["main-orchestrator", "qgis-toolbox"],
    )

    core._reset_task_scoped_skills(session.id)

    assert read_loaded_skills(
        session_db.get_state,
        session.id,
        core.prompt_builder.skill_manager,
    ) == ["main-orchestrator"]


def test_openai_provider_refuses_one_token_output_budget():
    provider = OpenAICompatibleProvider(
        model="test",
        max_tokens=4096,
        max_context_tokens=4096,
    )

    with pytest.raises(ContextWindowExceeded, match="最低输出预算"):
        provider._bounded_max_tokens(
            [{"role": "user", "content": "x" * 12000}],
            None,
        )


class _NoopProvider:
    name = "noop"
    model = "noop"

    def chat(self, system, messages, tools=None):
        return ChatResponse(content="done", model=self.model)
