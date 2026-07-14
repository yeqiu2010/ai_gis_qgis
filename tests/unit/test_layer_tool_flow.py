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
    find_generated_code_issues,
    find_unwritten_expected_outputs,
    infer_expected_outputs_from_code,
)
from ai_gis_qgis.backend.tools.layer_ops import _layer_name_aliases, _normalize_source
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
                            "code": (
                                "if False:\n"
                                "    open('parks.txt', 'w').write('unreachable')\n"
                                "raise NameError(\"name 'QgsProject' is not defined\")"
                            ),
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
                                "code": "open('result.txt', 'w').write('ok')",
                                "expected_outputs": [
                                    {"path": "result.txt", "name": "result", "type": "file"}
                                ],
                                "review": {"passed": True},
                            },
                        },
                    )
                ],
            )
        return ChatResponse(content="Pipeline 阶段已记录。", model=self.model)


class InvalidOutputContractProvider:
    name = "invalid-output-contract-test"
    model = "invalid-output-contract-test-model"

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
                        id="bad-output",
                        name="execute_gis_code",
                        arguments={
                            "code": "print('公园')",
                            "expected_outputs": [
                                {
                                    "path": "type_unique_values.txt",
                                    "name": "字段值",
                                    "type": "file",
                                }
                            ],
                        },
                    )
                ],
            )
        return ChatResponse(content="已改为直接生成最终筛选结果。", model=self.model)


class SwitchPipelineProvider:
    name = "switch-pipeline-test"
    model = "switch-pipeline-test-model"

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
                        id="switch-pipeline",
                        name="set_active_skill",
                        arguments={"skill_name": "gis-pipeline"},
                    )
                ],
            )
        return ChatResponse(content="等待开始数据盘点。", model=self.model)


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
                            "code": (
                                "with open('parks.geojson', 'w', encoding='utf-8') as handle:\n"
                                "    handle.write('{\"type\":\"FeatureCollection\",\"features\":[]}')"
                            ),
                            "expected_outputs": [
                                {"path": "parks.geojson", "name": "parks", "type": "vector"}
                            ],
                        },
                    )
                ],
            )
        return ChatResponse(content="已完成。", model=self.model)


class FlakyLLMProvider:
    name = "flaky-llm-test"
    model = "flaky-llm-test-model"

    def __init__(self, fail_times: int):
        self.calls = 0
        self.fail_times = fail_times

    def chat(self, system, messages, tools=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("timed out")
        return ChatResponse(content="模型已恢复。", model=self.model)


class SingleToolProvider:
    name = "single-tool-test"
    model = "single-tool-test-model"

    def chat(self, system, messages, tools=None):
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="call-1", name="list_layers", arguments={})],
        )


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
    assert "execute_gis_code" not in provider.first_call_tools
    assert "search_skills" in provider.first_call_tools
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


def test_layer_name_aliases_extract_leaf_from_combined_display_name():
    assert _layer_name_aliases("地理底图 — 院落") == {"院落"}
    assert _layer_name_aliases("database::Roads") == {"roads"}
    assert _layer_name_aliases("院落") == set()


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


def test_responsive_processing_injects_feedback_and_restores_run(monkeypatch, tmp_path: Path):
    calls = []
    event_pumps = []

    class FakeFeedback:
        def setProgress(self, progress):
            calls.append(("progress", progress))

    class FakeCoreApplication:
        @staticmethod
        def processEvents(*args):
            event_pumps.append(args)

    class FakeProcessingContext:
        def setInvalidGeometryCheck(self, value):
            self.invalid_geometry_check = value

    class FakeQgis:
        class InvalidGeometryCheck:
            GeometrySkipInvalid = "skip"

    fake_processing = types.ModuleType("processing")

    def original_run(*args, **kwargs):
        feedback = kwargs["feedback"]
        feedback.setProgress(25)
        return {"OUTPUT": "result.gpkg"}

    fake_processing.run = original_run
    fake_qgis_core = types.ModuleType("qgis.core")
    fake_qgis_core.QgsProcessingFeedback = FakeFeedback
    fake_qgis_core.QgsProcessingContext = FakeProcessingContext
    fake_qgis_core.Qgis = FakeQgis
    fake_qt_core = types.ModuleType("qgis.PyQt.QtCore")
    fake_qt_core.QCoreApplication = FakeCoreApplication
    fake_qt_core.QEventLoop = type("QEventLoop", (), {"AllEvents": 0})
    monkeypatch.setitem(sys.modules, "processing", fake_processing)
    monkeypatch.setitem(sys.modules, "qgis.core", fake_qgis_core)
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtCore", fake_qt_core)

    executor = QGISCodeExecutor({"workspace_dir": str(tmp_path / "workspaces")})
    with executor._responsive_processing():
        assert fake_processing.run("native:test", {}) == {"OUTPUT": "result.gpkg"}
        assert fake_processing.run is not original_run

    assert fake_processing.run is original_run
    assert calls == [("progress", 25)]
    assert len(event_pumps) == 1


def test_responsive_processing_wraps_explicit_feedback(monkeypatch, tmp_path: Path):
    received_feedback = []

    class FakeFeedback:
        def __init__(self):
            self.progress = []

        def setProgress(self, progress):
            self.progress.append(progress)

        def isCanceled(self):
            return False

    class FakeCoreApplication:
        @staticmethod
        def processEvents(*args):
            pass

    class FakeProcessingContext:
        def setInvalidGeometryCheck(self, value):
            self.invalid_geometry_check = value

    class FakeQgis:
        class InvalidGeometryCheck:
            GeometrySkipInvalid = "skip"

    fake_processing = types.ModuleType("processing")

    def original_run(*args, **kwargs):
        received_feedback.append(kwargs["feedback"])
        kwargs["feedback"].setProgress(50)
        return {}

    fake_processing.run = original_run
    fake_qgis_core = types.ModuleType("qgis.core")
    fake_qgis_core.QgsProcessingFeedback = FakeFeedback
    fake_qgis_core.QgsProcessingContext = FakeProcessingContext
    fake_qgis_core.Qgis = FakeQgis
    fake_qt_core = types.ModuleType("qgis.PyQt.QtCore")
    fake_qt_core.QCoreApplication = FakeCoreApplication
    fake_qt_core.QEventLoop = type("QEventLoop", (), {"AllEvents": 0})
    monkeypatch.setitem(sys.modules, "processing", fake_processing)
    monkeypatch.setitem(sys.modules, "qgis.core", fake_qgis_core)
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtCore", fake_qt_core)

    explicit_feedback = FakeFeedback()
    executor = QGISCodeExecutor({"workspace_dir": str(tmp_path / "workspaces")})
    with executor._responsive_processing():
        fake_processing.run("native:test", {}, feedback=explicit_feedback)

    assert received_feedback[0] is not explicit_feedback
    assert explicit_feedback.progress == [50]


def test_execute_gis_code_rejects_unwritten_expected_output(tmp_path: Path):
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
    assert result["preflight_failed"] is True
    assert "代码没有写入" in result["error"]
    assert "missing.txt" in result["error"]

    session_db.log_tool_call(
        session.id,
        "execute_gis_code",
        {
            "code": "print('missing.txt')",
            "expected_outputs": [{"path": "missing.txt", "name": "missing", "type": "file"}],
        },
        result,
        duration_ms=0,
    )
    failures = session_db.get_failure_records(session_id=session.id)
    assert failures[-1]["error_code"] == "output_contract"
    assert failures[-1]["source_type"] == "tool_call"
    assert failures[-1]["generated_code"] == "print('missing.txt')"


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


def test_detects_expected_output_that_is_only_printed():
    code = "print('type_values_check.txt')\nprint('公园')"

    assert find_unwritten_expected_outputs(
        code,
        [{"path": "type_values_check.txt", "name": "values", "type": "file"}],
    ) == ["type_values_check.txt"]


def test_invalid_output_contract_is_rejected_before_confirmation(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="preflight", model="test", source="test")
    provider = InvalidOutputContractProvider()

    events = AgentCore(
        session_db=session_db,
        llm_provider=provider,
        iface=None,
    ).run(session_id=session.id, user_message="按 type 提取公园")

    assert not any(event["type"] == "confirm_request" for event in events)
    tool_end = next(event for event in events if event["type"] == "tool_end")
    assert tool_end["payload"]["result"]["preflight_failed"] is True
    assert provider.calls == 2


def test_accepts_processing_and_text_file_output_writes():
    code = """
from pathlib import Path
output_path = Path(QGIS_AGENT_WORKSPACE) / "parks.gpkg"
processing.run("native:extractbyexpression", {"INPUT": layer, "OUTPUT": str(output_path)})
report_path = Path(QGIS_AGENT_WORKSPACE) / "report.txt"
report_path.write_text("ok", encoding="utf-8")
"""

    assert find_unwritten_expected_outputs(
        code,
        [
            {"path": "parks.gpkg", "name": "parks", "type": "vector"},
            {"path": "report.txt", "name": "report", "type": "file"},
        ],
    ) == []


def test_detects_recurrent_generated_pyqgis_mistakes():
    code = """
from PyQt5.QtCore import QVariant
from qgis.core import QgsProject, QgsVectorFileWriter
import processing

layer = QgsProject.instance().mapLayersByName("建筑物")[0]
feedback = processing.QgsProcessingFeedback()
result = processing.run("native:extractbyexpression", {
    "INPUT": layer,
    "EXPRESSION": "1=1",
    "OUTPUT": "result.gpkg",
})
output = result["OUTPUT"]
print(output.featureCount())
QgsVectorFileWriter.writeAsVectorFormatV3(layer, "result.gpkg", "UTF-8")
QgsProject.instance().addVectorLayer("result.gpkg", "result", "ogr")
"""

    issues = find_generated_code_issues(code)

    assert any("qgis.PyQt" in issue for issue in issues)
    assert any("mapLayersByName" in issue for issue in issues)
    assert any("QgsProcessingFeedback" in issue for issue in issues)
    assert any("featureCount" in issue for issue in issues)
    assert any("QgsVectorFileWriter" in issue for issue in issues)
    assert any("addVectorLayer" in issue for issue in issues)


def test_allows_guarded_processing_output_code():
    code = """
from pathlib import Path
from qgis.core import QgsProject
import processing

matches = QgsProject.instance().mapLayersByName("建筑物")
if not matches:
    raise ValueError("找不到图层")
output_path = str(Path(QGIS_AGENT_WORKSPACE) / "result.gpkg")
processing.run("native:extractbyexpression", {
    "INPUT": matches[0],
    "EXPRESSION": "1=1",
    "OUTPUT": output_path,
})
"""

    assert find_generated_code_issues(code) == []


def test_detects_processing_parameter_shapes_from_failure_logs():
    code = """
processing.run("native:joinattributesbylocation", {
    "INPUT": buildings,
    "PREDICATE": ["intersects"],
    "OVERLAY": land,
    "JOIN_FIELDS": [1, 27],
    "OUTPUT": "joined.gpkg",
})
processing.run("native:aggregate", {
    "INPUT": land,
    "GROUP_BY": "district",
    "AGGREGATES": {"aggregate": "sum", "input": "SHAPE_Area"},
    "OUTPUT": "summary.gpkg",
})
if geometry.isGeosEmpty():
    print("empty")
print(feature["??_1"])
"""

    issues = find_generated_code_issues(code)

    assert any("PREDICATE" in issue and "整数枚举" in issue for issue in issues)
    assert any("JOIN_FIELDS" in issue and "字段名" in issue for issue in issues)
    assert any("AGGREGATES" in issue and "object 列表" in issue for issue in issues)
    assert any("参数名是 JOIN" in issue for issue in issues)
    assert any("isGeosEmpty" in issue for issue in issues)
    assert any("??" in issue and "字段名" in issue for issue in issues)


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


def test_conversation_limit_is_applied_after_role_filtering(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="role filtered history", model="test", source="test")
    session_db.save_message(session.id, "user", "请分析建筑物", event_type="user")
    for index in range(60):
        session_db.save_message(
            session.id,
            "system",
            f"工具调用过程 {index}",
            event_type="process",
        )

    core = AgentCore(session_db=session_db, llm_provider=ToolCallingProvider(), iface=None)
    messages = core._build_conversation_messages(session.id)
    retry_messages = core._retry_messages_for_code_failure(
        session.id,
        {"code": "raise ValueError()"},
        {"success": False, "error": "failed"},
    )

    assert any(message.role == "user" and message.content == "请分析建筑物" for message in messages)
    assert any(
        message.role == "user" and message.content == "请分析建筑物"
        for message in retry_messages
    )


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
    assert "search_qgis_processing_tools" in prompt
    assert "get_qgis_processing_tool" in prompt
    assert "只调用一次 `execute_gis_code`" in prompt
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

    assert "两个及以上步骤" in prompt
    assert "必须切换到 gis-pipeline" in prompt
    assert "优先一次调用 inspect_layers" in prompt
    assert "QGIS_AGENT_WORKSPACE 是 execute_gis_code 执行器注入的运行时变量" in prompt
    assert "不要询问保存文件夹" in prompt
    assert "分析结果默认输出到该工作目录" in prompt
    assert "{\"skill_name\":\"qgis-toolbox\"}" in prompt
    assert "不要在 main-orchestrator 中直接调用 execute_gis_code" in prompt
    assert "500m.shp" in prompt


def test_record_pipeline_stage_emits_events_and_stores_artifact(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="pipeline", model="pipeline-stage-test-model", source="test")
    for stage_name in ("data_overview", "structured_query", "solution_plan"):
        session_db.log_stage_artifact(
            session.id,
            stage_name=stage_name,
            artifact={"summary": stage_name},
            summary=stage_name,
        )

    events = AgentCore(session_db=session_db, llm_provider=PipelineStageProvider(), iface=None).run(
        session_id=session.id,
        user_message="记录 pipeline 阶段。",
    )

    event_types = [event["type"] for event in events]
    assert "stage_start" in event_types
    assert "stage_end" in event_types
    assert "code_generated" in event_types
    assert "confirm_request" in event_types
    stage_end = next(event for event in events if event["type"] == "stage_end")
    assert stage_end["payload"]["stage_name"] == "generated_code"
    assert stage_end["payload"]["artifact"]["code"] == "open('result.txt', 'w').write('ok')"
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    assert confirmation["payload"]["tool_name"] == "execute_gis_code"
    assert confirmation["payload"]["arguments"]["code"] == "open('result.txt', 'w').write('ok')"

    with sqlite3.connect(tmp_path / "state.db") as connection:
        row = connection.execute(
            "SELECT stage_name, stage_artifact FROM messages "
            "WHERE event_type = 'stage_artifact' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row[0] == "generated_code"
    assert "result.txt" in row[1]


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


def test_record_pipeline_stage_rejects_skipped_and_fills_empty_summary(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="pipeline order", model="test", source="test")
    tool = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_tool_registry(session.id).get("record_pipeline_stage")

    skipped = tool.handler(
        {
            "stage_name": "execution_result",
            "summary": "执行完成",
            "artifact": {"success": True},
        }
    )
    empty = tool.handler(
        {
            "stage_name": "data_overview",
            "artifact": {"available_layers": []},
        }
    )

    assert skipped["success"] is False
    assert skipped["expected_stage"] == "data_overview"
    assert "顺序错误" in skipped["error"]
    assert empty["success"] is True
    assert empty["summary"] == "数据盘点已完成。"
    assert session_db.list_pipeline_stage_names(session.id) == ["data_overview"]


def test_record_pipeline_stage_accepts_json_artifact_and_duplicate(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="pipeline tolerant", model="test", source="test")
    tool = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_tool_registry(session.id).get("record_pipeline_stage")

    first = tool.handler(
        {
            "stage_name": "data_overview",
            "artifact": '{"summary":"图层盘点完成"}',
        }
    )
    duplicate = tool.handler(
        {
            "stage_name": "data_overview",
            "summary": "重复提交",
            "artifact": {"summary": "重复提交"},
        }
    )

    assert first["success"] is True
    assert duplicate["success"] is True
    assert duplicate["already_recorded"] is True
    assert duplicate["expected_stage"] == "structured_query"
    assert session_db.list_pipeline_stage_names(session.id) == ["data_overview"]


def test_record_pipeline_stage_recovers_wrapped_nested_json_artifact(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="pipeline wrapped artifact", model="test", source="test")
    tool = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_tool_registry(session.id).get("record_pipeline_stage")

    result = tool.handler(
        {
            "stage_name": "data_overview",
            "artifact": '{"summary":"图层盘点完成"}\n</invoke>}',
        }
    )

    assert result["success"] is True
    assert result["artifact"]["summary"] == "图层盘点完成"


def test_generated_code_stage_requires_passed_review(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="review gate", model="test", source="test")
    for stage_name in ("data_overview", "structured_query", "solution_plan"):
        session_db.log_stage_artifact(
            session.id,
            stage_name=stage_name,
            artifact={"summary": stage_name},
            summary=stage_name,
        )
    tool = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_tool_registry(session.id).get("record_pipeline_stage")

    result = tool.handler(
        {
            "stage_name": "generated_code",
            "summary": "代码审查未通过",
            "artifact": {
                "summary": "代码审查未通过",
                "code": "print('unsafe')",
                "expected_outputs": [{"path": "result.txt", "name": "result", "type": "file"}],
                "review": {"passed": False, "blocking_issues": ["统计分母错误"]},
            },
        }
    )

    assert result["success"] is False
    assert result["expected_stage"] == "generated_code"
    assert "review.passed" in result["error"]
    assert session_db.list_pipeline_stage_names(session.id)[-1] == "solution_plan"


def test_generated_code_stage_rejects_unwritten_output(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="output gate", model="test", source="test")
    for stage_name in ("data_overview", "structured_query", "solution_plan"):
        session_db.log_stage_artifact(
            session.id,
            stage_name=stage_name,
            artifact={"summary": stage_name},
            summary=stage_name,
        )
    tool = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_tool_registry(session.id).get("record_pipeline_stage")

    result = tool.handler(
        {
            "stage_name": "generated_code",
            "summary": "打印唯一值",
            "artifact": {
                "code": "print('公园')",
                "expected_outputs": [
                    {"path": "type_unique_values.txt", "name": "values", "type": "file"}
                ],
                "review": {"passed": True},
            },
        }
    )

    assert result["success"] is False
    assert "没有写入" in result["error"]
    assert session_db.list_pipeline_stage_names(session.id)[-1] == "solution_plan"


def test_switching_to_pipeline_starts_new_cycle_after_stale_artifacts(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="new pipeline cycle", model="test", source="test")
    for stage_name in ("data_overview", "structured_query"):
        session_db.log_stage_artifact(
            session.id,
            stage_name=stage_name,
            artifact={"summary": f"旧任务 {stage_name}"},
            summary=f"旧任务 {stage_name}",
        )
    core = AgentCore(
        session_db=session_db,
        llm_provider=SwitchPipelineProvider(),
        iface=None,
    )

    core.run(session_id=session.id, user_message="开始新的复杂分析任务")
    tool = core._build_tool_registry(session.id).get("record_pipeline_stage")
    result = tool.handler(
        {
            "stage_name": "data_overview",
            "summary": "新任务数据盘点",
            "artifact": {"summary": "新任务数据盘点"},
        }
    )

    assert result["success"] is True
    assert result["stage_name"] == "data_overview"
    assert session_db.list_pipeline_stage_names(session.id) == [
        "data_overview",
        "structured_query",
        "data_overview",
    ]


def test_pipeline_completion_gate_blocks_incomplete_reply_but_allows_questions(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    core = AgentCore(session_db=session_db, llm_provider=ToolCallingProvider(), iface=None)

    incomplete = session_db.create_session(title="incomplete", model="test", source="test")
    session_db.log_stage_artifact(
        incomplete.id,
        stage_name="data_overview",
        artifact={"summary": "数据已检查"},
        summary="数据已检查",
    )
    session_db.log_stage_artifact(
        incomplete.id,
        stage_name="structured_query",
        artifact={"summary": "需求已结构化", "questions": []},
        summary="需求已结构化",
    )

    waiting = session_db.create_session(title="waiting", model="test", source="test")
    session_db.log_stage_artifact(
        waiting.id,
        stage_name="data_overview",
        artifact={"summary": "数据已检查"},
        summary="数据已检查",
    )
    session_db.log_stage_artifact(
        waiting.id,
        stage_name="structured_query",
        artifact={"summary": "需要确认字段", "questions": ["学校类型字段取值是什么？"]},
        summary="需要确认字段",
    )

    assert core._pipeline_must_continue(incomplete.id, "gis-pipeline") is True
    assert core._pipeline_must_continue(waiting.id, "gis-pipeline") is False


def test_confirmed_pipeline_execution_records_final_stage(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="pipeline execute", model="test", source="test")
    session_db.set_state(f"{session.id}:active_skill", "gis-pipeline")
    for stage_name in ("data_overview", "structured_query", "solution_plan", "generated_code"):
        session_db.log_stage_artifact(
            session.id,
            stage_name=stage_name,
            artifact={"summary": stage_name},
            summary=stage_name,
        )
    core = AgentCore(
        session_db=session_db,
        llm_provider=CodeExecutionProvider(),
        iface=None,
        executor_config={"workspace_dir": str(tmp_path / "workspaces")},
    )

    events = core.run(session_id=session.id, user_message="执行已审查脚本")
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    assert session_db.list_pipeline_stage_names(session.id) == [
        "data_overview",
        "structured_query",
        "solution_plan",
        "generated_code",
        "execution_result",
    ]
    assert session_db.get_state(f"{session.id}:active_skill") == "main-orchestrator"
    assert any(
        event["type"] == "stage_end"
        and event["payload"]["stage_name"] == "execution_result"
        for event in confirmed_events
    )


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


def test_agent_core_retries_llm_provider_failures(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="llm retry", model="flaky", source="test")
    provider = FlakyLLMProvider(fail_times=2)

    events = AgentCore(
        session_db=session_db,
        llm_provider=provider,
        iface=None,
        llm_retry_delay_seconds=0,
    ).run(session_id=session.id, user_message="你好")

    assert provider.calls == 3
    retry_messages = [
        event["payload"]["message"]
        for event in events
        if event["type"] == "thinking" and "LLM 提供商调用失败，正在重试" in event["payload"]["message"]
    ]
    assert len(retry_messages) == 2
    final_message = next(event for event in reversed(events) if event["type"] == "message")
    assert final_message["payload"]["content"] == "模型已恢复。"
    failures = session_db.get_failure_records(session_id=session.id)
    assert [item["error_code"] for item in failures] == ["timeout", "timeout"]
    assert [item["attempt"] for item in failures] == [1, 2]

    export_path = session_db.export_failure_records(tmp_path / "failures.jsonl")
    exported = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
    assert len(exported) == 2
    assert exported[0]["source_type"] == "llm_call"


def test_search_messages_treats_dotted_query_as_literal_terms(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="search", model="test", source="test")
    session_db.save_message(
        session.id,
        "assistant",
        "generated_code 使用 processing.run 计算建筑密度和用地类型。",
    )

    matches = session_db.search_messages(
        "processing.run 建筑密度 用地类型",
        session_id=session.id,
    )

    assert len(matches) == 1
    assert "processing.run" in matches[0]["content"]


def test_agent_core_returns_agent_message_after_llm_retries_exhausted(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="llm exhausted", model="flaky", source="test")
    provider = FlakyLLMProvider(fail_times=10)

    events = AgentCore(
        session_db=session_db,
        llm_provider=provider,
        iface=None,
        llm_retry_delay_seconds=0,
    ).run(session_id=session.id, user_message="你好")

    assert provider.calls == 3
    final_message = next(event for event in reversed(events) if event["type"] == "message")
    assert "Agent 暂时无法从 LLM 提供商获得响应" in final_message["payload"]["content"]
    assert "LLM 调用失败，已重试 3 次" in final_message["payload"]["content"]
    assert any(event["type"] == "message_delta" for event in events)
    assert events[-1]["type"] == "complete"

    saved = session_db.get_messages(session.id, limit=10)
    assert saved[-1]["event_type"] == "error"
    assert "timed out" in saved[-1]["content"]


def test_agent_core_stops_after_tool_returns_without_publishing_stale_result(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="cancel after tool", model="single-tool", source="test")
    cancel_checks = 0

    def should_cancel():
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks >= 6

    events = AgentCore(
        session_db=session_db,
        llm_provider=SingleToolProvider(),
        iface=None,
        should_cancel=should_cancel,
    ).run(session_id=session.id, user_message="列出图层")

    event_types = [event["type"] for event in events]
    assert "tool_start" in event_types
    assert "tool_end" not in event_types
    assert event_types[-2:] == ["message", "complete"]
    assert events[-2]["payload"]["content"] == "任务已停止。"

    saved = session_db.get_messages(session.id, limit=20)
    assert saved[-1]["content"] == "任务已停止。"
    assert not any(message["content"].startswith("工具完成：") for message in saved)
