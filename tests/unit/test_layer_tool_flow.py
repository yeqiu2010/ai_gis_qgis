from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

from ai_gis_qgis.backend.agent_core import AgentCore
from ai_gis_qgis.backend.context.prompt_builder import PromptBuilder
from ai_gis_qgis.backend.context.qgis_context import QGISContext
from ai_gis_qgis.backend.executor.qgis_executor import QGISCodeExecutor
from ai_gis_qgis.backend.llm.base_provider import ChatResponse, ToolCall
from ai_gis_qgis.backend.tools.code_execution import (
    build_execute_gis_code_tool,
    infer_expected_outputs_from_code,
)
from ai_gis_qgis.backend.tools.layer_ops import _normalize_source
from ai_gis_qgis.backend.tools.registry import ToolEntry, ToolRegistry
from ai_gis_qgis.database.session_db import SessionDB


class ToolCallingProvider:
    name = "tool-calling-test"
    model = "tool-test-model"

    def __init__(self):
        self.calls = 0
        self.first_call_tools: list[str] = []

    def chat(self, system, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            self.first_call_tools = [tool["function"]["name"] for tool in tools or []]
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="call-1", name="list_layers", arguments={})],
            )
        return ChatResponse(content="工具调用完成。", model=self.model)


class ConfirmingProvider:
    name = "confirming-test"
    model = "confirming-test-model"

    def chat(self, system, messages, tools=None):
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="remove_layer",
                    arguments={"layer_name": "roads"},
                )
            ],
        )


class CodeExecutionProvider:
    name = "code-execution-test"
    model = "code-execution-test-model"

    def chat(self, system, messages, tools=None):
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="execute_gis_code",
                    arguments={
                        "code": "with open('result.txt', 'w', encoding='utf-8') as handle:\n    handle.write('ok')\nprint('done')",
                        "expected_outputs": [
                            {"path": "result.txt", "name": "result", "type": "file"}
                        ],
                        "timeout_seconds": 10,
                    },
                )
            ],
        )


class RetryCodeExecutionProvider:
    name = "retry-code-test"
    model = "retry-code-test-model"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_gis_code",
                        arguments={
                            "code": "raise NameError(\"name 'QgsProject' is not defined\")",
                            "expected_outputs": [
                                {"path": "parks.txt", "name": "parks", "type": "file"}
                            ],
                        },
                    )
                ],
            )
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="call-2",
                    name="execute_gis_code",
                    arguments={
                        "code": "with open('parks.txt', 'w', encoding='utf-8') as handle:\n    handle.write('fixed')",
                        "expected_outputs": [
                            {"path": "parks.txt", "name": "parks", "type": "file"}
                        ],
                    },
                )
            ],
        )


class PipelineStageProvider:
    name = "pipeline-stage-test"
    model = "pipeline-stage-test-model"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="record_pipeline_stage",
                        arguments={
                            "stage_name": "generated_code",
                            "summary": "已生成测试代码。",
                            "artifact": {
                                "summary": "已生成测试代码。",
                                "code": "print('ok')",
                                "expected_outputs": [],
                            },
                        },
                    )
                ],
            )
        return ChatResponse(content="Pipeline 阶段已记录。", model=self.model)


class SequentialToolProvider:
    name = "sequential-tool-test"
    model = "sequential-tool-test-model"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="inspect_layer",
                        arguments={"layer_name": "建筑物"},
                    )
                ],
            )
        if self.calls == 2:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="execute_gis_code",
                        arguments={
                            "code": "print('filter parks')",
                            "expected_outputs": [
                                {"path": "parks.geojson", "name": "parks", "type": "vector"}
                            ],
                        },
                    )
                ],
            )
        return ChatResponse(content="已完成。", model=self.model)


class QVariantLike:
    __module__ = "qgis.PyQt.QtCore"

    def __init__(self, value):
        self._value = value

    def isNull(self):
        return self._value is None

    def toPyObject(self):
        return self._value


def test_agent_core_registers_and_executes_layer_tools(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="tool flow", model="tool-test-model", source="test")
    provider = ToolCallingProvider()

    events = AgentCore(session_db=session_db, llm_provider=provider, iface=None).run(
        session_id=session.id,
        user_message="当前有哪些图层？",
    )

    assert provider.calls == 2
    assert "list_layers" in provider.first_call_tools
    assert "inspect_layers" in provider.first_call_tools
    assert "load_layer" in provider.first_call_tools
    assert "remove_layer" in provider.first_call_tools
    assert "execute_gis_code" in provider.first_call_tools
    event_types = [event["type"] for event in events]
    assert event_types[:4] == [
        "run_start",
        "thinking",
        "tool_start",
        "tool_end",
    ]
    assert "message_delta" in event_types
    assert event_types[-2:] == ["message", "complete"]

    tool_end = next(event for event in events if event["type"] == "tool_end")
    assert tool_end["payload"]["name"] == "list_layers"
    assert tool_end["payload"]["result"]["success"] is False
    assert "PyQGIS" in tool_end["payload"]["result"]["error"]


def test_agent_core_pauses_destructive_layer_tools_for_confirmation(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="confirm flow", model="confirming-test-model", source="test")

    events = AgentCore(session_db=session_db, llm_provider=ConfirmingProvider(), iface=None).run(
        session_id=session.id,
        user_message="删除 roads 图层。",
    )

    assert [event["type"] for event in events] == [
        "run_start",
        "thinking",
        "confirm_request",
        "message",
        "complete",
    ]
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    confirmation_id = confirmation["payload"]["confirmation_id"]
    assert confirmation["payload"]["tool_name"] == "remove_layer"
    assert confirmation["payload"]["destructive"] is True

    confirmed_events = AgentCore(
        session_db=session_db,
        llm_provider=ConfirmingProvider(),
        iface=None,
    ).confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation_id,
        approved=True,
    )

    confirmed_event_types = [event["type"] for event in confirmed_events]
    assert confirmed_event_types[:3] == [
        "confirm_resolved",
        "tool_start",
        "tool_end",
    ]
    assert "message_delta" in confirmed_event_types
    assert confirmed_event_types[-2:] == ["message", "complete"]
    tool_end = next(event for event in confirmed_events if event["type"] == "tool_end")
    assert tool_end["payload"]["name"] == "remove_layer"
    assert tool_end["payload"]["result"]["success"] is False
    assert "PyQGIS" in tool_end["payload"]["result"]["error"]


def test_load_layer_source_normalization_does_not_truncate_by_extension():
    assert _normalize_source(r"E:\Desktop\test\1.shp数据") == r"E:\Desktop\test\1.shp数据"
    assert _normalize_source('"E:/Desktop/test/2.gpkg"') == "E:/Desktop/test/2.gpkg"


def test_execute_gis_code_requires_confirmation_and_runs_worker(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="code flow", model="code-execution-test-model", source="test")
    core = AgentCore(
        session_db=session_db,
        llm_provider=CodeExecutionProvider(),
        iface=None,
        executor_config={"workspace_dir": str(tmp_path / "workspaces"), "timeout_seconds": 10},
    )

    events = core.run(session_id=session.id, user_message="生成一个结果文件。")

    assert [event["type"] for event in events] == [
        "run_start",
        "thinking",
        "confirm_request",
        "message",
        "complete",
    ]
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    assert confirmation["payload"]["tool_name"] == "execute_gis_code"
    assert confirmation["payload"]["writes_project"] is True

    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    tool_end = next(event for event in confirmed_events if event["type"] == "tool_end")
    result = tool_end["payload"]["result"]
    assert result["success"] is True
    assert result["outputs"][0]["exists"] is True
    assert result["outputs"][0]["name"] == "result"
    assert "done" in result["stdout"]
    final_message = next(event for event in reversed(confirmed_events) if event["type"] == "message")
    assert "done" in final_message["payload"]["content"]
    assert "已生成输出：result" in final_message["payload"]["content"]
    assert "工作目录" not in final_message["payload"]["content"]

    with sqlite3.connect(tmp_path / "state.db") as connection:
        row = connection.execute(
            "SELECT success, stdout, stderr FROM code_execution_log WHERE session_id = ?",
            (session.id,),
        ).fetchone()
        message_rows = connection.execute(
            "SELECT role, content, event_type FROM messages WHERE session_id = ? ORDER BY id",
            (session.id,),
        ).fetchall()
    assert row == (1, "done\n", "")
    assert ("user", "生成一个结果文件。", "user") in message_rows
    assert any(
        role == "system" and event_type == "process" and "调用工具：execute_gis_code" in content
        for role, content, event_type in message_rows
    )
    assert any(
        role == "assistant" and event_type == "summary" and "done" in content
        for role, content, event_type in message_rows
    )


def test_code_executor_rejects_forbidden_imports(tmp_path: Path):
    executor = QGISCodeExecutor({"workspace_dir": str(tmp_path / "workspaces"), "timeout_seconds": 5})

    result = executor.execute(
        code="import subprocess\nsubprocess.run(['echo', 'bad'])",
        expected_outputs=[{"path": "result.txt", "name": "result", "type": "file"}],
    )

    assert result["success"] is False
    assert "禁止导入模块" in result["error"]


def test_code_executor_rejects_new_qgis_application(tmp_path: Path):
    executor = QGISCodeExecutor({"workspace_dir": str(tmp_path / "workspaces"), "timeout_seconds": 5})

    result = executor.execute(
        code="from qgis.core import QgsApplication\napp = QgsApplication([], False)\nQgsApplication.initQgis()",
        expected_outputs=[{"path": "result.txt", "name": "result", "type": "file"}],
    )

    assert result["success"] is False
    assert "QgsApplication" in result["error"]


def test_current_qgis_namespace_includes_processing_symbols(monkeypatch, tmp_path: Path):
    fake_core = types.ModuleType("qgis.core")
    symbol_names = [
        "QgsCoordinateReferenceSystem",
        "QgsCoordinateTransform",
        "QgsFeature",
        "QgsFeatureRequest",
        "QgsField",
        "QgsFields",
        "QgsGeometry",
        "QgsProject",
        "QgsProcessing",
        "QgsProcessingContext",
        "QgsProcessingFeedback",
        "QgsRasterLayer",
        "QgsRectangle",
        "QgsVectorFileWriter",
        "QgsVectorLayer",
    ]
    for name in symbol_names:
        setattr(fake_core, name, type(name, (), {}))

    fake_qgis = types.ModuleType("qgis")
    fake_processing = types.ModuleType("processing")
    monkeypatch.setitem(sys.modules, "qgis", fake_qgis)
    monkeypatch.setitem(sys.modules, "qgis.core", fake_core)
    monkeypatch.setitem(sys.modules, "processing", fake_processing)

    namespace = QGISCodeExecutor(
        {"workspace_dir": str(tmp_path / "workspaces")}
    )._current_qgis_namespace(tmp_path, iface=None)

    assert namespace["QgsProcessing"] is fake_core.QgsProcessing
    assert namespace["QgsProcessingContext"] is fake_core.QgsProcessingContext
    assert namespace["QgsProcessingFeedback"] is fake_core.QgsProcessingFeedback
    assert namespace["processing"] is fake_processing


def test_execute_gis_code_uses_current_qgis_mode_when_executor_is_available(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="current qgis", model="test", source="test")
    tool = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
        qgis_executor=lambda func: func(),
        executor_config={
            "execution_mode": "current_qgis",
            "workspace_dir": str(tmp_path / "workspaces"),
            "timeout_seconds": 10,
        },
    )._build_tool_registry(session.id).get("execute_gis_code")

    result = tool.handler(
        {
            "code": "with open('result.txt', 'w', encoding='utf-8') as handle:\n    handle.write('ok')\nprint('current')",
            "expected_outputs": [{"path": "result.txt", "name": "result", "type": "file"}],
        }
    )

    assert result["success"] is True
    assert result["execution_mode"] == "current_qgis"
    assert result["outputs"][0]["exists"] is True
    assert "current" in result["stdout"]


def test_execute_gis_code_uses_stderr_as_error_fallback(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="stderr fallback", model="test", source="test")
    tool = build_execute_gis_code_tool(
        session_db=session_db,
        session_id=session.id,
        qgis_executor=lambda func: func(),
        executor_config={
            "execution_mode": "current_qgis",
            "workspace_dir": str(tmp_path / "workspaces"),
        },
    )

    result = tool.handler(
        {
            "code": "import sys\nsys.stderr.write('first\\nlast error\\n')",
            "expected_outputs": [{"path": "missing.txt", "name": "missing", "type": "file"}],
        }
    )

    assert result["success"] is False
    assert result["error"] == "代码执行结束，但缺少预期输出文件：" + result["outputs"][0]["path"]


def test_infers_expected_outputs_from_generated_output_path():
    code = """
from pathlib import Path
input_path = "E:/Desktop/test/buildings.shp"
output_path = Path(QGIS_AGENT_WORKSPACE) / "500m.shp"
processing.run(
    "native:intersection",
    {"INPUT": input_path, "OVERLAY": buffer_layer, "OUTPUT": str(output_path)},
)
"""

    assert infer_expected_outputs_from_code(code) == [
        {"path": "500m.shp", "name": "500m", "type": "vector"}
    ]


def test_execute_gis_code_infers_missing_file_expected_outputs(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="infer expected outputs", model="test", source="test")
    tool = build_execute_gis_code_tool(
        session_db=session_db,
        session_id=session.id,
        executor_config={
            "workspace_dir": str(tmp_path / "workspaces"),
            "timeout_seconds": 10,
        },
    )

    result = tool.handler(
        {
            "code": "output_path = 'result.txt'\nwith open(output_path, 'w', encoding='utf-8') as handle:\n    handle.write('ok')",
        }
    )

    assert result["success"] is True
    assert result["expected_outputs_inferred"] is True
    assert result["expected_outputs"][0]["path"].endswith("result.txt")
    assert result["outputs"][0]["exists"] is True


def test_execute_gis_code_delivers_outputs_to_external_path(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="delivery", model="test", source="test")
    target = tmp_path / "exports" / "result.geojson"
    tool = build_execute_gis_code_tool(
        session_db=session_db,
        session_id=session.id,
        executor_config={
            "workspace_dir": str(tmp_path / "workspaces"),
            "timeout_seconds": 10,
        },
    )

    result = tool.handler(
        {
            "code": "output_path = 'result.geojson'\nwith open(output_path, 'w', encoding='utf-8') as handle:\n    handle.write('{\"type\":\"FeatureCollection\",\"features\":[]}')",
            "expected_outputs": [
                {"path": str(target), "name": "result", "type": "file"}
            ],
        }
    )

    assert result["success"] is True
    assert result["expected_outputs"][0]["path"].endswith("result.geojson")
    assert result["delivered_outputs"][0]["target_path"] == str(target)
    assert target.exists()


def test_agent_core_injects_tool_memory_for_followup(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="memory", model="test", source="test")
    session_db.save_message(session.id, "user", "从建筑物图层中找出公园地块", event_type="user")
    session_db.save_message(
        session.id,
        "assistant",
        "我看到建筑物图层中有 leisure、landuse 和 name 等字段。",
        event_type="summary",
    )
    session_db.log_tool_call(
        session.id,
        "inspect_layer",
        {"layer_name": "建筑物"},
        {
            "success": True,
            "layer": {"name": "建筑物", "type": "vector", "crs": "EPSG:4326"},
            "fields": [
                {"name": "leisure", "type": "String"},
                {"name": "landuse", "type": "String"},
                {"name": "name", "type": "String"},
            ],
            "sample_features": [{"leisure": "park", "name": "中山公园"}],
        },
        duration_ms=12,
    )
    session_db.save_message(session.id, "user", "导出公园地块", event_type="user")

    messages = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_conversation_messages(session.id)

    memory = messages[0].content
    assert messages[0].role == "assistant"
    assert "会话记忆" in memory
    assert "建筑物" in memory
    assert "leisure" in memory
    assert "landuse" in memory
    assert "中山公园" in memory
    assert messages[-1].content == "导出公园地块"


def test_confirmed_code_execution_retries_after_failure(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="retry code", model="retry-code-test-model", source="test")
    provider = RetryCodeExecutionProvider()
    core = AgentCore(
        session_db=session_db,
        llm_provider=provider,
        iface=None,
        qgis_executor=lambda func: func(),
        executor_config={
            "execution_mode": "current_qgis",
            "workspace_dir": str(tmp_path / "workspaces"),
        },
    )

    events = core.run(session_id=session.id, user_message="提取公园")
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    tool_end_events = [event for event in confirmed_events if event["type"] == "tool_end"]
    assert len(tool_end_events) == 2
    assert tool_end_events[0]["payload"]["result"]["success"] is False
    assert tool_end_events[1]["payload"]["result"]["success"] is True
    assert tool_end_events[1]["payload"]["result"]["retry_count"] == 1
    final_message = next(event for event in reversed(confirmed_events) if event["type"] == "message")
    assert "自动修复并重试 1 次" in final_message["payload"]["content"]


def test_prompt_builder_composes_pipeline_includes():
    prompt = PromptBuilder().build(
        "gis-pipeline",
        QGISContext(project_path="", layer_count=0, layers=[]),
    )

    assert "Active Skill: gis-pipeline" in prompt
    assert "Included Skill: data-overview" in prompt
    assert "Included Skill: code-reviewer" in prompt
    assert "record_pipeline_stage" in prompt
    assert "推荐模板：按属性筛选并输出 GeoJSON" in prompt
    assert "native:extractbyexpression" in prompt
    assert "不允许在 `data_overview`、`structured_query`、`solution_plan`、`generated_code` 四个阶段完成之前调用 `execute_gis_code`" in prompt
    assert "优先一次调用 `inspect_layers`" in prompt
    assert "500m.shp" in prompt


def test_prompt_builder_routes_complex_analysis_to_pipeline():
    prompt = PromptBuilder().build(
        "main-orchestrator",
        QGISContext(project_path="", layer_count=1, layers=[{"name": "建筑物"}]),
    )

    assert "复杂 GIS 分析任务必须先切换到 gis-pipeline" in prompt
    assert "优先一次调用 inspect_layers" in prompt
    assert "QGIS_AGENT_WORKSPACE 是 execute_gis_code 执行器注入的运行时变量" in prompt
    assert "不要询问保存文件夹" in prompt
    assert "分析结果默认输出到该工作目录" in prompt
    assert "{\"skill_name\":\"gis-pipeline\"}" in prompt
    assert "不要在 main-orchestrator 中直接调用 execute_gis_code" in prompt
    assert "500m.shp" in prompt


def test_record_pipeline_stage_emits_events_and_stores_artifact(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="pipeline", model="pipeline-stage-test-model", source="test")

    events = AgentCore(session_db=session_db, llm_provider=PipelineStageProvider(), iface=None).run(
        session_id=session.id,
        user_message="记录 pipeline 阶段。",
    )

    event_types = [event["type"] for event in events]
    assert "stage_start" in event_types
    assert "stage_end" in event_types
    assert "code_generated" in event_types
    stage_end = next(event for event in events if event["type"] == "stage_end")
    assert stage_end["payload"]["stage_name"] == "generated_code"
    assert stage_end["payload"]["artifact"]["code"] == "print('ok')"

    with sqlite3.connect(tmp_path / "state.db") as connection:
        row = connection.execute(
            "SELECT stage_name, stage_artifact FROM messages WHERE event_type = 'stage_artifact'"
        ).fetchone()
    assert row[0] == "generated_code"
    assert "print('ok')" in row[1]


def test_record_pipeline_stage_infers_missing_stage_name(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="pipeline infer", model="test", source="test")
    tool = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_tool_registry(session.id).get("record_pipeline_stage")

    result = tool.handler({"artifact": {"summary": "已完成数据盘点。"}})

    assert result["success"] is True
    assert result["stage_name"] == "data_overview"
    assert session_db.list_pipeline_stage_names(session.id) == ["data_overview"]


def test_agent_core_continues_tool_loop_until_confirmation(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="sequential", model="sequential-tool-test-model", source="test")
    provider = SequentialToolProvider()

    events = AgentCore(session_db=session_db, llm_provider=provider, iface=None).run(
        session_id=session.id,
        user_message="找出建筑物图层中的公园地块",
    )

    assert provider.calls == 2
    event_types = [event["type"] for event in events]
    assert event_types == [
        "run_start",
        "thinking",
        "tool_start",
        "tool_end",
        "confirm_request",
        "message",
        "complete",
    ]
    assert next(event for event in events if event["type"] == "tool_end")["payload"]["name"] == "inspect_layer"
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    assert confirmation["payload"]["tool_name"] == "execute_gis_code"


def test_tool_registry_converts_qvariant_like_results_to_json_safe_values():
    registry = ToolRegistry()
    registry.register(
        ToolEntry(
            name="qvariant_result",
            description="test",
            parameters={"type": "object"},
            category="test",
            handler=lambda arguments: {
                "sample_features": [{"name": QVariantLike("公园"), "empty": QVariantLike(None)}]
            },
        )
    )

    result, _ = registry.execute("qvariant_result", {})

    assert result["sample_features"] == [{"name": "公园", "empty": None}]
    json.dumps(result, ensure_ascii=False)
