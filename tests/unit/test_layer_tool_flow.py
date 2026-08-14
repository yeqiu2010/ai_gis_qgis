from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

from ai_gis_qgis.backend.agent_core import AgentCore
from ai_gis_qgis.backend.context.prompt_builder import PromptBuilder
from ai_gis_qgis.backend.context.qgis_context import QGISContext
from ai_gis_qgis.backend.executor.qgis_executor import (
    QGISCodeExecutor,
    _process_qt_events,
)
from ai_gis_qgis.backend.failure_analysis import classify_failure
from ai_gis_qgis.backend.llm.base_provider import ChatResponse, ToolCall
from ai_gis_qgis.backend.tools.code_execution import (
    build_execute_gis_code_tool,
    find_generated_code_issues,
    find_uncreated_workspace_inputs,
    find_unwritten_expected_outputs,
    infer_expected_outputs_from_code,
    validate_execute_gis_code_arguments,
)
from ai_gis_qgis.backend.tools.layer_ops import _layer_name_aliases, _normalize_source
from ai_gis_qgis.backend.tools.pipeline import _validate_stage_artifact
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


class Sam3ContinuationProvider:
    name = "sam3-continuation-test"
    model = "sam3-continuation-test-model"

    def __init__(self):
        self.calls = 0
        self.messages_by_call = []

    def chat(self, system, messages, tools=None):
        self.calls += 1
        self.messages_by_call.append(messages)
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="plan-vegetation",
                        name="create_plan",
                        arguments={
                            "objective": "使用 SAM3 提取植被并统计面积占比",
                            "steps": [
                                {
                                    "id": "segment",
                                    "skill_name": "sam3-remote-segmentation",
                                    "instruction": "使用 SAM3 分割植被",
                                    "dependencies": [],
                                },
                                {
                                    "id": "statistics",
                                    "skill_name": "gis-pipeline",
                                    "instruction": "计算植被面积及影像面积占比",
                                    "dependencies": ["segment"],
                                },
                            ],
                        },
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
                        id="load-sam3",
                        name="load_skill",
                        arguments={"skill_name": "sam3-remote-segmentation"},
                    )
                ],
            )
        if self.calls == 3:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="segment-vegetation",
                        name="segment_remote_sensing_image",
                        arguments={
                            "input_layer_id": "satellite-id",
                            "mode": "text",
                            "prompt": "vegetation",
                            "output_types": ["vector"],
                        },
                    )
                ],
            )
        if self.calls == 4:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="load-pipeline",
                        name="load_skill",
                        arguments={"skill_name": "gis-pipeline"},
                    )
                ],
            )
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="calculate-ratio",
                    name="execute_gis_code",
                    arguments={
                        "code": "print('vegetation_area_ratio=25.0%')",
                        "expected_outputs": [],
                    },
                )
            ],
        )


class BufferThenSamProvider:
    name = "buffer-then-sam-test"
    model = "buffer-then-sam-test-model"

    def __init__(self):
        self.calls = 0
        self.messages_by_call = []
        self.systems_by_call = []

    def chat(self, system, messages, tools=None):
        self.calls += 1
        self.messages_by_call.append(messages)
        self.systems_by_call.append(system)
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="create-buffer",
                        name="execute_gis_code",
                        arguments={
                            "code": (
                                "with open('youth_road_500m_buffer.gpkg', 'w', "
                                "encoding='utf-8') as handle:\n    handle.write('buffer')"
                            ),
                            "expected_outputs": [
                                {
                                    "path": "youth_road_500m_buffer.gpkg",
                                    "name": "youth_road_500m_buffer",
                                    "type": "vector",
                                }
                            ],
                        },
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
                        id="load-sam3-after-buffer",
                        name="load_skill",
                        arguments={"skill_name": "sam3-remote-segmentation"},
                    )
                ],
            )
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="segment-buildings-in-buffer",
                    name="segment_remote_sensing_image",
                    arguments={
                        "input_layer_id": "whch-layer-id",
                        "mode": "text",
                        "prompt": "building",
                        "scope_mode": "aoi",
                        "aoi_layer_id": "buffer-layer-id",
                        "output_types": ["vector"],
                        "output_name": "building",
                    },
                )
            ],
        )


class DuplicateThresholdProvider:
    name = "duplicate-threshold-test"
    model = "duplicate-threshold-test-model"

    def __init__(self):
        self.calls = 0
        self.messages_by_call = []

    def chat(self, system, messages, tools=None):
        self.calls += 1
        self.messages_by_call.append(messages)
        if self.calls >= 4:
            return ChatResponse(
                content="阈值 0.5 和 0.3 的建筑物分割均已完成。",
                model=self.model,
            )
        threshold = 0.5 if self.calls <= 2 else 0.3
        suffix = str(threshold).replace(".", "_")
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id=f"segment-{suffix}-{self.calls}",
                    name="segment_remote_sensing_image",
                    arguments={
                        "input_layer_id": "whch-layer-id",
                        "mode": "text",
                        "prompt": "building",
                        "confidence_threshold": threshold,
                        "output_types": ["vector"],
                        "output_name": f"building_threshold_{suffix}",
                    },
                )
            ],
        )


class Sam3InspectionContinuationProvider:
    name = "sam3-inspection-continuation-test"
    model = "sam3-inspection-continuation-test-model"

    def __init__(self):
        self.calls = 0
        self.system_prompts = []
        self.messages_by_call = []

    def chat(self, system, messages, tools=None):
        self.calls += 1
        self.system_prompts.append(system)
        self.messages_by_call.append(messages)
        if self.calls == 1:
            return ChatResponse(
                content=(
                    "以下是刚刚执行的 QGIS 工具结果。如果任务尚未完成，继续调用必要工具："
                    '[{"name":"inspect_sam3_segmentation_inputs",'
                    '"result":{"success":true,"inspection_complete":true}}]'
                ),
                model=self.model,
            )
        if self.calls == 2:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="inspect-10000",
                        name="inspect_sam3_segmentation_inputs",
                        arguments={
                            "input_layer_id": "10000-layer-id",
                            "scope_mode": "full",
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
                    id="segment-road",
                    name="segment_remote_sensing_image",
                    arguments={
                        "input_layer_id": "10000-layer-id",
                        "mode": "text",
                        "prompt": "road",
                        "scope_mode": "full",
                        "output_types": ["vector"],
                        "output_name": "road",
                    },
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


class RepeatingSmoothingProvider:
    name = "repeating-smoothing-test"
    model = "repeating-smoothing-test-model"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools=None):
        self.calls += 1
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id=f"smooth-building-{self.calls}",
                    name="execute_gis_code",
                    arguments={
                        "code": (
                            "with open('building_smoothed.geojson', 'w', "
                            "encoding='utf-8') as handle:\n    handle.write('{}')\n"
                            "print('平滑处理完成！')"
                        ),
                        "expected_outputs": [
                            {
                                "path": "building_smoothed.geojson",
                                "name": "building_smoothed",
                                "type": "vector",
                            }
                        ],
                    },
                )
            ],
        )


class SchoolCoverageExecutionProvider:
    name = "school-coverage-test"
    model = "school-coverage-test-model"

    def chat(self, system, messages, tools=None):
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="school-coverage",
                    name="execute_school_service_coverage",
                    arguments={
                        "school_layer_id": "school-id",
                        "residential_layer_id": "residential-id",
                        "school_type_field": "CCN",
                        "school_type_values": ["小学", "初中"],
                        "residential_area_field": "面积",
                        "area_unit": "square_meter",
                        "group_field": "XZQMC",
                        "service_distance_m": 500,
                        "target_crs": "EPSG:4547",
                    },
                )
            ],
        )


class IncompleteSchoolCoverageProvider:
    name = "incomplete-school-coverage-test"
    model = "incomplete-school-coverage-test-model"

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
                        id="incomplete-school-coverage",
                        name="execute_school_service_coverage",
                        arguments={
                            "school_layer_id": "school-id",
                            "residential_layer_id": "residential-id",
                            "school_type_field": "CCN",
                            "school_type_values": ["中小学"],
                            "area_unit": "square_meter",
                            "group_field": "XZQMC",
                            "service_distance_m": 1000,
                            "target_crs": "EPSG:4526",
                        },
                    )
                ],
            )
        return ChatResponse(
            content="还缺少已确认的居住区面积字段，请补全后再执行。",
            model=self.model,
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


class UsageTrackingProvider:
    name = "usage-tracking-test"
    model = "usage-tracking-model"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="call-1", name="list_layers", arguments={})],
                input_tokens=100,
                output_tokens=10,
                total_tokens=110,
            )
        return ChatResponse(
            content="统计完成。",
            model=self.model,
            input_tokens=200,
            output_tokens=20,
            total_tokens=220,
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
    event_types = [event["type"] for event in events if event["type"] != "run_metrics"]
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


def test_agent_core_accumulates_and_persists_run_metrics(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="metrics", model="usage-tracking-model")

    events = AgentCore(
        session_db=session_db,
        llm_provider=UsageTrackingProvider(),
        iface=None,
    ).run(session_id=session.id, user_message="统计本轮消耗")

    metric_events = [event for event in events if event["type"] == "run_metrics"]
    assert len(metric_events) == 3
    final_metrics = metric_events[-1]["payload"]
    assert final_metrics["input_tokens"] == 300
    assert final_metrics["output_tokens"] == 30
    assert final_metrics["total_tokens"] == 330
    assert final_metrics["llm_calls"] == 2
    assert final_metrics["usage_estimated"] is False
    assert final_metrics["running"] is False
    assert final_metrics["status"] == "completed"
    assert final_metrics["duration_ms"] >= 0

    saved = session_db.get_messages(session.id, limit=20)
    assistant = [message for message in saved if message["role"] == "assistant"][-1]
    assert assistant["run_id"] == final_metrics["run_id"]
    assert assistant["total_tokens"] == 330
    assert assistant["llm_calls"] == 2
    assert assistant["metrics_status"] == "completed"
    stored_session = session_db.get_session(session.id)
    assert stored_session is not None
    assert stored_session["input_tokens"] == 300
    assert stored_session["output_tokens"] == 30


def test_agent_core_pauses_destructive_layer_tools_for_confirmation(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="confirm flow", model="confirming-test-model", source="test")

    events = AgentCore(session_db=session_db, llm_provider=ConfirmingProvider(), iface=None).run(
        session_id=session.id,
        user_message="删除 roads 图层。",
    )

    assert [event["type"] for event in events if event["type"] != "run_metrics"] == [
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

    confirmed_event_types = [
        event["type"] for event in confirmed_events if event["type"] != "run_metrics"
    ]
    assert confirmed_event_types[:4] == [
        "run_start",
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


def test_confirmed_sam3_segmentation_resumes_remaining_user_request(
    tmp_path: Path,
    monkeypatch,
):
    output_path = tmp_path / "vegetation.gpkg"
    output_path.touch()

    def build_fake_sam3_tools(**kwargs):
        del kwargs
        arguments_schema = {
            "type": "object",
            "properties": {
                "input_layer_id": {"type": "string"},
                "mode": {"type": "string"},
                "prompt": {"type": "string"},
                "output_types": {"type": "array"},
            },
            "required": ["input_layer_id", "mode"],
        }
        read_parameters = {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }
        return [
            ToolEntry(
                name="check_sam3_service",
                description="Fake SAM3 health check.",
                parameters=read_parameters,
                handler=lambda arguments: {
                    "success": True,
                    "status": "ok",
                    "model_loaded": True,
                },
                category="sam3",
            ),
            ToolEntry(
                name="inspect_sam3_segmentation_inputs",
                description="Fake SAM3 input inspection.",
                parameters=read_parameters,
                handler=lambda arguments: {
                    "success": True,
                    "inspection_complete": True,
                },
                category="sam3",
            ),
            ToolEntry(
                name="segment_remote_sensing_image",
                description="Fake SAM3 segmentation for confirmation-resume testing.",
                parameters=arguments_schema,
                handler=lambda arguments: {
                    "success": True,
                    "job_id": "job-vegetation-1",
                    "mode": arguments["mode"],
                    "prompt": arguments.get("prompt"),
                    "source_layer": {
                        "id": arguments["input_layer_id"],
                        "name": "satellite",
                    },
                    "outputs": [
                        {
                            "path": str(output_path),
                            "name": "vegetation_objects",
                            "type": "vector",
                        }
                    ],
                    "loaded_layers": [
                        {
                            "id": "vegetation-layer-id",
                            "name": "vegetation_objects",
                            "type": "vector",
                        }
                    ],
                    "object_count": 8,
                    "crs": "EPSG:3857",
                },
                category="sam3",
                requires_confirmation=True,
                writes_project=True,
                preflight=lambda arguments: {
                    "success": True,
                    "arguments": arguments,
                },
            )
        ]

    monkeypatch.setattr(
        "ai_gis_qgis.backend.agent_core.build_sam3_tools",
        build_fake_sam3_tools,
    )
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="sam3 continuation",
        model="sam3-continuation-test-model",
        source="test",
    )
    provider = Sam3ContinuationProvider()
    core = AgentCore(session_db=session_db, llm_provider=provider, iface=None)

    events = core.run(
        session_id=session.id,
        user_message="提取satellite图层中的植被区域，并统计植被面积相当于影像面积的占比",
    )
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    assert provider.calls == 5
    continuation_context = "\n".join(
        message.content for message in provider.messages_by_call[3]
    )
    assert "提取satellite图层中的植被区域" in continuation_context
    assert "vegetation-layer-id" in continuation_context
    assert "中间产物，不是任务完成信号" in continuation_context
    next_confirmation = next(
        event for event in confirmed_events if event["type"] == "confirm_request"
    )
    assert next_confirmation["payload"]["tool_name"] == "execute_gis_code"
    assert session_db.get_state(f"{session.id}:active_skill") == "gis-pipeline"
    steps = session_db.get_plan_steps(
        str(session_db.get_active_task(session.id)["id"])
    )
    assert [step["skill_name"] for step in steps] == [
        "sam3-remote-segmentation",
        "gis-pipeline",
    ]
    assert steps[0]["status"] == "completed"
    assert steps[1]["status"] == "pending"


def test_confirmed_buffer_resumes_plan_and_passes_aoi_layer_to_sam3(
    tmp_path: Path,
    monkeypatch,
):
    buffer_path = tmp_path / "youth_road_500m_buffer.gpkg"
    buffer_path.touch()

    def build_fake_execute_tool(**kwargs):
        del kwargs
        return ToolEntry(
            name="execute_gis_code",
            description="Fake confirmed buffer execution.",
            parameters={"type": "object", "properties": {}},
            handler=lambda arguments: {
                "success": True,
                "stdout": "500m缓冲区创建完成",
                "workspace_dir": str(tmp_path),
                "outputs": [
                    {
                        "path": str(buffer_path),
                        "name": "youth_road_500m_buffer",
                        "type": "vector",
                        "exists": True,
                    }
                ],
                "loaded_layers": [
                    {
                        "id": "buffer-layer-id",
                        "name": "youth_road_500m_buffer",
                        "type": "vector",
                    }
                ],
            },
            category="analysis",
            requires_confirmation=True,
            writes_project=True,
        )

    def build_fake_sam3_tools(**kwargs):
        del kwargs
        read_parameters = {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }
        segment_parameters = {
            "type": "object",
            "properties": {
                "input_layer_id": {"type": "string"},
                "mode": {"type": "string"},
                "prompt": {"type": "string"},
                "scope_mode": {"type": "string"},
                "aoi_layer_id": {"type": "string"},
                "output_types": {"type": "array"},
                "output_name": {"type": "string"},
            },
            "required": ["input_layer_id", "mode"],
        }
        return [
            ToolEntry(
                name="check_sam3_service",
                description="Fake SAM3 health check.",
                parameters=read_parameters,
                handler=lambda arguments: {
                    "success": True,
                    "status": "ok",
                    "model_loaded": True,
                },
                category="sam3",
            ),
            ToolEntry(
                name="inspect_sam3_segmentation_inputs",
                description="Fake SAM3 input inspection.",
                parameters=read_parameters,
                handler=lambda arguments: {
                    "success": True,
                    "inspection_complete": True,
                },
                category="sam3",
            ),
            ToolEntry(
                name="segment_remote_sensing_image",
                description="Fake SAM3 AOI segmentation.",
                parameters=segment_parameters,
                handler=lambda arguments: {"success": True},
                category="sam3",
                requires_confirmation=True,
                writes_project=True,
                preflight=lambda arguments: {
                    "success": True,
                    "arguments": arguments,
                },
            ),
        ]

    monkeypatch.setattr(
        "ai_gis_qgis.backend.agent_core.build_execute_gis_code_tool",
        build_fake_execute_tool,
    )
    monkeypatch.setattr(
        "ai_gis_qgis.backend.agent_core.build_sam3_tools",
        build_fake_sam3_tools,
    )
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="buffer then SAM3",
        model="buffer-then-sam-test-model",
        source="test",
    )
    task_id = session_db.create_task(
        session.id,
        "提取青年路500m范围内whch影像中的建筑物并保存building.geojson",
    )
    session_db.replace_plan_steps(
        task_id,
        [
            {
                "id": "buffer",
                "skill_name": "qgis-toolbox",
                "instruction": "生成青年路500m缓冲区",
                "dependencies": [],
            },
            {
                "id": "segment",
                "skill_name": "sam3-remote-segmentation",
                "instruction": "在缓冲区内使用SAM3分割建筑物",
                "dependencies": ["buffer"],
            },
            {
                "id": "export",
                "skill_name": "qgis-toolbox",
                "instruction": "保存为building.geojson",
                "dependencies": ["segment"],
            },
        ],
    )
    session_db.set_state(f"{session.id}:active_skill", "qgis-toolbox")
    provider = BufferThenSamProvider()
    core = AgentCore(session_db=session_db, llm_provider=provider, iface=None)

    events = core.run(
        session_id=session.id,
        user_message="使用SAM3进行建筑物分割，青年路在line1图层中",
    )
    buffer_confirmation = next(
        event for event in events if event["type"] == "confirm_request"
    )
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=buffer_confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    assert provider.calls == 3
    resumed_context = "\n".join(
        message.content for message in provider.messages_by_call[1]
    )
    assert "buffer-layer-id" in resumed_context
    assert "道路缓冲区应作为后续 SAM3 的" in provider.systems_by_call[2]
    assert "aoi_layer_id" in provider.systems_by_call[2]
    sam_confirmation = next(
        event for event in confirmed_events if event["type"] == "confirm_request"
    )
    assert sam_confirmation["payload"]["tool_name"] == "segment_remote_sensing_image"
    assert sam_confirmation["payload"]["arguments"]["aoi_layer_id"] == "buffer-layer-id"
    steps = session_db.get_plan_steps(task_id)
    assert steps[0]["status"] == "completed"
    assert steps[1]["status"] == "pending"


def test_duplicate_confirmed_sam3_threshold_is_reused_and_next_threshold_runs(
    tmp_path: Path,
    monkeypatch,
):
    execution_count = {"value": 0}
    output_path = tmp_path / "building_threshold_0_5.gpkg"
    output_path.touch()

    def build_fake_sam3_tools(**kwargs):
        del kwargs
        read_parameters = {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }

        def segment(arguments):
            execution_count["value"] += 1
            threshold = float(arguments["confidence_threshold"])
            return {
                "success": True,
                "job_id": f"job-{threshold}",
                "mode": "text",
                "prompt": "building",
                "confidence_threshold": threshold,
                "parameters": {
                    key: value
                    for key, value in arguments.items()
                    if key != "_inspection"
                },
                "outputs": [
                    {
                        "path": str(output_path),
                        "name": arguments["output_name"],
                        "type": "vector",
                    }
                ],
                "loaded_layers": [
                    {
                        "id": f"building-layer-{threshold}",
                        "name": arguments["output_name"],
                        "type": "vector",
                    }
                ],
                "object_count": 4,
                "crs": "EPSG:2385",
            }

        return [
            ToolEntry(
                name="check_sam3_service",
                description="Fake SAM3 health check.",
                parameters=read_parameters,
                handler=lambda arguments: {
                    "success": True,
                    "status": "ok",
                    "model_loaded": True,
                },
                category="sam3",
            ),
            ToolEntry(
                name="inspect_sam3_segmentation_inputs",
                description="Fake SAM3 inspection.",
                parameters=read_parameters,
                handler=lambda arguments: {
                    "success": True,
                    "inspection_complete": True,
                },
                category="sam3",
            ),
            ToolEntry(
                name="segment_remote_sensing_image",
                description="Fake threshold segmentation.",
                parameters=read_parameters,
                handler=segment,
                category="sam3",
                requires_confirmation=True,
                writes_project=True,
                preflight=lambda arguments: {
                    "success": True,
                    "arguments": arguments,
                },
            ),
        ]

    monkeypatch.setattr(
        "ai_gis_qgis.backend.agent_core.build_sam3_tools",
        build_fake_sam3_tools,
    )
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="duplicate SAM3 threshold",
        model="duplicate-threshold-test-model",
        source="test",
    )
    session_db.set_state(f"{session.id}:active_skill", "sam3-remote-segmentation")
    provider = DuplicateThresholdProvider()
    core = AgentCore(session_db=session_db, llm_provider=provider, iface=None)

    events = core.run(
        session_id=session.id,
        user_message="使用sam3提取whch图层中的建筑物，阈值分别设为0.5，0.3",
    )
    first_confirmation = next(
        event for event in events if event["type"] == "confirm_request"
    )
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=first_confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    assert execution_count["value"] == 1
    assert provider.calls == 3
    replay = next(
        event
        for event in confirmed_events
        if event["type"] == "tool_end"
        and event["payload"]["result"].get("duplicate_prevented")
    )
    assert replay["payload"]["result"]["confidence_threshold"] == 0.5
    second_confirmation = next(
        event for event in confirmed_events if event["type"] == "confirm_request"
    )
    assert second_confirmation["payload"]["arguments"]["confidence_threshold"] == 0.3
    assert second_confirmation["payload"]["arguments"]["output_name"] == (
        "building_threshold_0_3"
    )
    completed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=second_confirmation["payload"]["confirmation_id"],
        approved=True,
    )
    assert execution_count["value"] == 2
    assert provider.calls == 4
    final_message = next(
        event for event in reversed(completed_events) if event["type"] == "message"
    )
    assert "阈值 0.5 和 0.3" in final_message["payload"]["content"]
    completed_context = "\n".join(
        message.content for message in provider.messages_by_call[-1]
    )
    assert '"confidence_threshold": 0.5' in completed_context
    assert '"confidence_threshold": 0.3' in completed_context


def test_sam3_inspection_cannot_leak_internal_result_or_ask_natural_confirmation(
    tmp_path: Path,
    monkeypatch,
):
    def build_fake_sam3_tools(**kwargs):
        del kwargs
        parameters = {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }
        return [
            ToolEntry(
                name="check_sam3_service",
                description="Fake health check.",
                parameters=parameters,
                handler=lambda arguments: {
                    "success": True,
                    "status": "ok",
                    "model_loaded": True,
                },
                category="sam3",
            ),
            ToolEntry(
                name="inspect_sam3_segmentation_inputs",
                description="Fake inspection.",
                parameters=parameters,
                handler=lambda arguments: {
                    "success": True,
                    "inspection_complete": True,
                    "input_layer_id": arguments["input_layer_id"],
                    "input_layer_name": "10000",
                    "width": 2048,
                    "height": 2048,
                    "rgb_bands": [1, 2, 3],
                    "scope_mode": "full",
                    "warnings": [],
                },
                category="sam3",
            ),
            ToolEntry(
                name="segment_remote_sensing_image",
                description="Fake road segmentation.",
                parameters=parameters,
                handler=lambda arguments: {"success": True},
                category="sam3",
                requires_confirmation=True,
                writes_project=True,
                preflight=lambda arguments: {
                    "success": True,
                    "arguments": arguments,
                },
            ),
        ]

    monkeypatch.setattr(
        "ai_gis_qgis.backend.agent_core.build_sam3_tools",
        build_fake_sam3_tools,
    )
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="SAM3 inspection continuation",
        model="sam3-inspection-continuation-test-model",
        source="test",
    )
    session_db.set_state(f"{session.id}:active_skill", "sam3-remote-segmentation")
    provider = Sam3InspectionContinuationProvider()
    core = AgentCore(session_db=session_db, llm_provider=provider, iface=None)

    events = core.run(
        session_id=session.id,
        user_message="使用SAM3模型从10000图层中提取出道路",
    )

    assert provider.calls == 3
    confirmations = [event for event in events if event["type"] == "confirm_request"]
    assert len(confirmations) == 1
    assert confirmations[0]["payload"]["tool_name"] == "segment_remote_sensing_image"
    assert confirmations[0]["payload"]["arguments"]["prompt"] == "road"
    messages = [event["payload"]["content"] for event in events if event["type"] == "message"]
    assert len(messages) == 1
    assert "需要确认后才能执行" in messages[0]
    assert "以下是刚刚执行的 QGIS 工具结果" not in messages[0]
    correction_context = "\n".join(
        message.content for message in provider.messages_by_call[1]
    )
    assert "不是实际工具调用" in correction_context
    real_inspection = next(
        event
        for event in events
        if event["type"] == "tool_end"
        and event["payload"]["name"] == "inspect_sam3_segmentation_inputs"
    )
    assert real_inspection["payload"]["result"]["inspection_complete"] is True


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

    assert [event["type"] for event in events if event["type"] != "run_metrics"] == [
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


def test_completed_single_step_plan_does_not_repeat_confirmed_gis_code(
    tmp_path: Path,
    monkeypatch,
):
    output_path = tmp_path / "building_smoothed.geojson"
    output_path.write_text("{}", encoding="utf-8")

    def build_fake_execute_tool(**kwargs):
        del kwargs
        return ToolEntry(
            name="execute_gis_code",
            description="Fake smoothing execution.",
            parameters={"type": "object", "properties": {}},
            handler=lambda arguments: {
                "success": True,
                "stdout": "平滑处理完成！",
                "workspace_dir": str(tmp_path),
                "outputs": [
                    {
                        "path": str(output_path),
                        "name": "building_smoothed",
                        "type": "vector",
                        "exists": True,
                    }
                ],
                "loaded_layers": [
                    {
                        "id": "building-smoothed-id",
                        "name": "building_smoothed",
                        "type": "vector",
                    }
                ],
            },
            category="analysis",
            requires_confirmation=True,
            writes_project=True,
        )

    monkeypatch.setattr(
        "ai_gis_qgis.backend.agent_core.build_execute_gis_code_tool",
        build_fake_execute_tool,
    )
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="smooth once",
        model="repeating-smoothing-test-model",
        source="test",
    )
    task_id = session_db.create_task(session.id, "消除building图层中的边界锯齿")
    session_db.replace_plan_steps(
        task_id,
        [
            {
                "id": "smooth",
                "skill_name": "qgis-toolbox",
                "instruction": "平滑building边界并输出结果",
                "dependencies": [],
            }
        ],
    )
    session_db.set_state(f"{session.id}:active_skill", "qgis-toolbox")
    provider = RepeatingSmoothingProvider()
    core = AgentCore(session_db=session_db, llm_provider=provider, iface=None)

    events = core.run(
        session_id=session.id,
        user_message="消除building图层中的边界锯齿",
    )
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    assert provider.calls == 1
    assert not any(event["type"] == "confirm_request" for event in confirmed_events)
    assert session_db.get_plan_step(task_id, "smooth")["status"] == "completed"
    assert session_db.get_task(task_id)["status"] == "completed"
    final_message = next(
        event for event in reversed(confirmed_events) if event["type"] == "message"
    )
    assert "平滑处理完成" in final_message["payload"]["content"]


def test_identical_confirmed_gis_code_is_idempotent_within_user_turn(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="dedupe", model="test", source="test")
    core = AgentCore(session_db=session_db, llm_provider=ToolCallingProvider(), iface=None)
    arguments = {
        "code": "print('smooth')",
        "expected_outputs": [
            {
                "path": "building_smoothed.geojson",
                "name": "building_smoothed",
                "type": "vector",
            }
        ],
    }
    result = {
        "success": True,
        "stdout": "平滑处理完成",
        "outputs": [{"path": "building_smoothed.geojson", "type": "vector"}],
        "loaded_layers": [{"id": "smoothed-layer-id"}],
    }

    registry = core._build_tool_registry(session.id)
    session_db.set_state(f"{session.id}:request_scope", "request-1")
    core._remember_confirmed_tool_call(
        session.id,
        "execute_gis_code",
        arguments,
        result,
        registry,
    )
    replay = core._confirmed_tool_replay(
        session.id,
        "execute_gis_code",
        arguments,
        registry,
    )

    assert replay is not None
    assert replay["duplicate_prevented"] is True
    assert replay["already_completed"] is True
    assert replay["loaded_layers"] == [{"id": "smoothed-layer-id"}]


def test_one_shot_business_skill_resets_after_confirmed_execution(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        "ai_gis_qgis.backend.tools.school_service_coverage._prepare_execution_parameters",
        lambda arguments, qgis_executor=None: dict(arguments),
    )
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="one shot", model="test", source="test")
    session_db.set_state(
        f"{session.id}:active_skill",
        "calculate-school-service-coverage",
    )
    core = AgentCore(
        session_db=session_db,
        llm_provider=SchoolCoverageExecutionProvider(),
        iface=None,
        executor_config={"workspace_dir": str(tmp_path / "workspaces")},
    )
    tool = core._build_tool_registry(session.id).get("execute_school_service_coverage")
    assert tool.requires_confirmation is True
    assert "code" not in tool.parameters["properties"]
    assert "expected_outputs" not in tool.parameters["properties"]
    inspect_tool = core._build_tool_registry(session.id).get(
        "inspect_school_service_coverage_inputs"
    )
    assert inspect_tool.requires_confirmation is False
    assert inspect_tool.writes_project is False

    events = core.run(session_id=session.id, user_message="计算中小学服务半径覆盖率")
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    assert confirmation["payload"]["tool_name"] == "execute_school_service_coverage"
    core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    assert session_db.get_state(f"{session.id}:active_skill") == "main-orchestrator"


def test_school_coverage_preflight_blocks_incomplete_call_before_confirmation(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="preflight", model="test", source="test")
    session_db.set_state(
        f"{session.id}:active_skill",
        "calculate-school-service-coverage",
    )
    provider = IncompleteSchoolCoverageProvider()
    core = AgentCore(session_db=session_db, llm_provider=provider, iface=None)

    events = core.run(
        session_id=session.id,
        user_message="CRS 使用 EPSG:4526",
        qgis_context=QGISContext(project_path="", layer_count=0, layers=[]),
    )

    assert provider.calls == 2
    assert not any(event["type"] == "confirm_request" for event in events)
    tool_end = next(event for event in events if event["type"] == "tool_end")
    assert tool_end["payload"]["result"]["preflight_failed"] is True
    assert "residential_area_field" in tool_end["payload"]["result"]["error"]


def test_land_use_building_metrics_tools_are_registered_with_expected_safety(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="land metrics", model="test", source="test")
    core = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
        executor_config={"workspace_dir": str(tmp_path / "workspaces")},
    )

    registry = core._build_tool_registry(session.id)
    inspect_tool = registry.get("inspect_land_use_building_metrics_inputs")
    execute_tool = registry.get("execute_land_use_building_metrics")

    assert inspect_tool.requires_confirmation is False
    assert inspect_tool.writes_project is False
    assert execute_tool.requires_confirmation is True
    assert execute_tool.writes_project is True
    assert "code" not in execute_tool.parameters["properties"]
    assert "expected_outputs" not in execute_tool.parameters["properties"]


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


def test_execute_gis_code_allows_stdout_or_project_result_without_files(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="direct result", model="test", source="test")
    fake_iface = types.SimpleNamespace(style_updated=False)
    tool = build_execute_gis_code_tool(
        session_db=session_db,
        session_id=session.id,
        iface=fake_iface,
        qgis_executor=lambda func: func(),
        executor_config={
            "execution_mode": "current_qgis",
            "workspace_dir": str(tmp_path / "workspaces"),
        },
    )

    assert tool.parameters["required"] == ["code"]
    assert "minItems" not in tool.parameters["properties"]["expected_outputs"]

    result = tool.handler(
        {
            "code": (
                "iface.style_updated = True\n"
                "print('surface1 总面积：123.45 平方米；样式已更新')"
            )
        }
    )

    assert result["success"] is True
    assert result["expected_outputs"] == []
    assert result["outputs"] == []
    assert result["expected_outputs_inferred"] is False
    assert fake_iface.style_updated is True
    assert "123.45" in result["stdout"]


def test_pipeline_generated_code_allows_empty_file_outputs_for_direct_result():
    artifact = {
        "code": "print('总面积：123.45 平方米')",
        "expected_outputs": [],
        "review": {"passed": True},
    }

    assert _validate_stage_artifact("generated_code", artifact) is None


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
    fake_qt_core.QEventLoop = type(
        "QEventLoop",
        (),
        {"ProcessEventsFlag": type("ProcessEventsFlag", (), {"AllEvents": "qt6-all"})},
    )
    monkeypatch.setitem(sys.modules, "processing", fake_processing)
    monkeypatch.setitem(sys.modules, "qgis.core", fake_qgis_core)
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtCore", fake_qt_core)

    executor = QGISCodeExecutor({"workspace_dir": str(tmp_path / "workspaces")})
    with executor._responsive_processing():
        assert fake_processing.run("native:test", {}) == {"OUTPUT": "result.gpkg"}
        assert fake_processing.run is not original_run

    assert fake_processing.run is original_run
    assert calls == [("progress", 25)]
    assert event_pumps == [("qt6-all", 25)]


def test_responsive_processing_wraps_explicit_feedback(monkeypatch, tmp_path: Path):
    received_feedback = []
    event_pumps = []

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
            event_pumps.append(args)

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
    assert event_pumps == [(0, 25)]


def test_classifies_qt6_event_loop_enum_failure_as_executor_bug():
    failure = classify_failure(
        "AttributeError: type object 'QEventLoop' has no attribute 'AllEvents'"
    )

    assert failure["error_code"] == "executor_qt_compatibility"
    assert failure["retryable"] is False


def test_qt_event_pump_falls_back_when_binding_rejects_flag_overload():
    calls = []

    class FakeCoreApplication:
        @staticmethod
        def processEvents(*args):
            calls.append(args)
            if args:
                raise TypeError("unsupported overload")

    fake_event_loop = type("QEventLoop", (), {"AllEvents": 0})

    _process_qt_events(FakeCoreApplication, fake_event_loop)

    assert calls == [(0, 25), ()]


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


def test_preflight_rejects_text_field_type_for_density_calculation():
    code = """
from pathlib import Path
workspace = Path(QGIS_AGENT_WORKSPACE)
building_land_stats_path = str(workspace / "building_land_stats.gpkg")
output_path = str(workspace / "result.gpkg")
processing.run(
    "native:fieldcalculator",
    {
        "INPUT": building_land_stats_path,
        "FIELD_NAME": "dense",
        "FIELD_TYPE": 2,
        "FIELD_LENGTH": 30,
        "FIELD_PRECISION": 10,
        "FORMULA": '"sum_FAREA" / "land_sum_Shape_Area"',
        "OUTPUT": output_path,
    },
)
"""

    result = validate_execute_gis_code_arguments(
        {
            "code": code,
            "expected_outputs": [{"path": "result.gpkg", "name": "result", "type": "vector"}],
        }
    )

    assert result is not None
    assert result["preflight_failed"] is True
    assert "FIELD_TYPE=2 是 Text/String，不是 Double" in result["error"]
    assert "building_land_stats.gpkg" in result["error"]
    assert "全新的空工作目录" in result["error"]


def test_workspace_input_is_valid_when_current_script_creates_it_first():
    code = """
from pathlib import Path
workspace = Path(QGIS_AGENT_WORKSPACE)
intermediate_path = str(workspace / "building_land_stats.gpkg")
output_path = str(workspace / "result.gpkg")
processing.run(
    "native:joinattributestable",
    {"INPUT": source_layer, "INPUT_2": stats_layer, "OUTPUT": intermediate_path},
)
processing.run(
    "native:fieldcalculator",
    {
        "INPUT": intermediate_path,
        "FIELD_NAME": "dense",
        "FIELD_TYPE": 0,
        "FORMULA": '"sum_FAREA" / "land_sum_Shape_Area"',
        "OUTPUT": output_path,
    },
)
"""

    assert find_uncreated_workspace_inputs(code) == []


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


def test_accepts_qgis_named_style_output_writes():
    code = """
from pathlib import Path
workspace = Path(QGIS_AGENT_WORKSPACE)
stretch_style_path = workspace / "surface1_stretch.qml"
class_style_path = workspace / "surface1_class7.qml"
stretch_layer.saveNamedStyle(str(stretch_style_path))
class_layer.saveNamedStyle(uri=str(class_style_path))
"""
    expected_outputs = [
        {"path": "surface1_stretch.qml", "name": "stretch style", "type": "file"},
        {"path": "surface1_class7.qml", "name": "class style", "type": "file"},
    ]

    assert find_unwritten_expected_outputs(code, expected_outputs) == []
    assert validate_execute_gis_code_arguments(
        {"code": code, "expected_outputs": expected_outputs}
    ) is None


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


def test_rejects_color_ramp_shader_passed_directly_to_pseudocolor_renderer():
    invalid_constructor_code = """
color_function = QgsColorRampShader(minimum, maximum)
color_function_alias = color_function
renderer = QgsSingleBandPseudoColorRenderer(
    layer.dataProvider(), 1, color_function_alias
)
"""
    invalid_setter_code = """
color_function = QgsColorRampShader(minimum, maximum)
renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1)
renderer_alias = renderer
renderer_alias.setShader(color_function)
"""

    for code in (invalid_constructor_code, invalid_setter_code):
        issues = find_generated_code_issues(code)
        assert any(
            "QgsSingleBandPseudoColorRenderer 需要 QgsRasterShader" in issue
            and "setRasterShaderFunction" in issue
            for issue in issues
        )


def test_accepts_color_ramp_function_wrapped_in_raster_shader():
    code = """
color_function_1 = QgsColorRampShader(minimum, maximum)
raster_shader_1 = QgsRasterShader()
raster_shader_1.setRasterShaderFunction(color_function_1)
renderer_1 = QgsSingleBandPseudoColorRenderer(
    layer.dataProvider(), 1, raster_shader_1
)

color_function_2 = QgsColorRampShader(minimum, maximum)
raster_shader_2 = QgsRasterShader()
raster_shader_2.setRasterShaderFunction(color_function_2)
renderer_2 = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1)
renderer_2.setShader(raster_shader_2)
"""

    assert find_generated_code_issues(code) == []


def test_classifies_pseudocolor_shader_type_failure_as_generated_code_api():
    failure = classify_failure(
        "QgsSingleBandPseudoColorRenderer.setShader(): argument 1 has unexpected type "
        "'QgsColorRampShader'"
    )

    assert failure["error_code"] == "generated_code_api"
    assert failure["retryable"] is False


def test_detects_feature_count_on_processing_output_path_variants():
    code = '''
result = processing.run("native:extractbylocation", {
    "INPUT": buildings,
    "PREDICATE": [0],
    "INTERSECT": buffer,
    "OUTPUT": output_path,
})
direct_count = result["OUTPUT"].featureCount()
first_alias = result["OUTPUT"]
second_alias = first_alias
aliased_count = second_alias.featureCount()
'''

    issues = find_generated_code_issues(code)

    assert len([issue for issue in issues if "featureCount" in issue]) == 1


def test_allows_feature_count_on_memory_processing_outputs():
    code = '''
memory_result = processing.run("native:extractbyexpression", {
    "INPUT": layer,
    "EXPRESSION": "1=1",
    "OUTPUT": "memory:",
})
memory_layer = memory_result["OUTPUT"]
print(memory_layer.featureCount())

temporary_result = processing.run("native:buffer", {
    "INPUT": memory_layer,
    "DISTANCE": 500,
    "OUTPUT": "TEMPORARY_OUTPUT",
})
print(temporary_result["OUTPUT"].featureCount())

constant_result = processing.run("native:centroids", {
    "INPUT": memory_layer,
    "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
})
print(constant_result["OUTPUT"].featureCount())
'''

    assert find_generated_code_issues(code) == []


def test_detects_generated_code_syntax_error():
    issues = find_generated_code_issues("with open('result.csv', 'w'\n    pass")

    assert len(issues) == 1
    assert "SyntaxError" in issues[0]
    assert "第 1 行" in issues[0]


def test_detects_invalid_geometry_enum_on_processing_context():
    invalid_code = '''
context = QgsProcessingContext()
context.setInvalidGeometryCheck(
    QgsProcessingContext.InvalidGeometryCheck.GeometrySkipInvalid
)
'''
    valid_code = '''
context = QgsProcessingContext()
context.setInvalidGeometryCheck(
    Qgis.InvalidGeometryCheck.GeometrySkipInvalid
)
'''

    issues = find_generated_code_issues(invalid_code)

    assert any("枚举属于 Qgis" in issue for issue in issues)
    assert find_generated_code_issues(valid_code) == []


def test_classifies_invalid_geometry_enum_runtime_failure():
    classification = classify_failure(
        "type object 'QgsProcessingContext' has no attribute 'InvalidGeometryCheck'"
    )

    assert classification["error_code"] == "generated_code_api"
    assert "Qgis.InvalidGeometryCheck" in classification["cause"]


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


def test_detects_aggregate_group_key_missing_from_output_schema():
    invalid_code = '''
land_result = processing.run("native:aggregate", {
    "INPUT": land_layer,
    "GROUP_BY": '"用地_1"',
    "AGGREGATES": [
        {
            "aggregate": "sum",
            "input": '"Shape_Area"',
            "name": "sum_Shape_Area",
            "type": 6,
        },
    ],
    "OUTPUT": "memory:",
})
land_stats = land_result["OUTPUT"]
processing.run("native:joinattributestable", {
    "INPUT": building_stats,
    "FIELD": "用地_1",
    "INPUT_2": land_stats,
    "FIELD_2": "用地_1",
    "OUTPUT": "memory:",
})
'''
    valid_code = '''
land_result = processing.run("native:aggregate", {
    "INPUT": land_layer,
    "GROUP_BY": '"用地_1"',
    "AGGREGATES": [
        {
            "aggregate": "first_value",
            "input": '"用地_1"',
            "name": "land_type",
            "type": 10,
        },
        {
            "aggregate": "sum",
            "input": '"Shape_Area"',
            "name": "sum_land_area",
            "type": 6,
        },
    ],
    "OUTPUT": "memory:",
})
'''

    issues = find_generated_code_issues(invalid_code)

    assert any("GROUP_BY 不会自动写入输出字段" in issue for issue in issues)
    assert any("ASCII 别名" in issue for issue in issues)
    assert find_generated_code_issues(valid_code) == []


def test_classifies_invalid_join_field_after_aggregate():
    classification = classify_failure(
        'Invalid join field from layer 1: “用地_1” does not exist'
    )

    assert classification["error_code"] == "field_not_found"
    assert "没有显式输出连接键" in classification["cause"]


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
            "layer": {
                "id": "buildings-live-id",
                "name": "建筑物",
                "type": "vector",
                "crs": "EPSG:4326",
            },
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
    assert "buildings-live-id" in memory
    assert "leisure" in memory
    assert "landuse" in memory
    assert "中山公园" in memory
    assert messages[-1].content == "导出公园地块"


def test_agent_core_preserves_school_coverage_binding_for_unit_followup(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="school coverage memory", model="test", source="test")
    session_db.log_tool_call(
        session.id,
        "inspect_school_service_coverage_inputs",
        {"school_layer_id": "school-live-id", "school_type_field": "CCN"},
        {
            "success": True,
            "school_layer": {"id": "school-live-id", "name": "学校"},
            "school_type_field": "CCN",
            "available_values": ["中小学", "高等院校"],
            "domain_complete": True,
        },
        duration_ms=5,
    )
    session_db.save_message(session.id, "user", "面积的单位是平方米", event_type="user")

    messages = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_conversation_messages(session.id)

    memory = messages[0].content
    assert "school_layer_id=school-live-id" in memory
    assert "school_type_field=CCN" in memory
    assert 'available_values=["中小学", "高等院校"]' in memory
    assert "domain_complete=True" in memory


def test_agent_core_preserves_failed_school_execution_arguments_for_crs_followup(
    tmp_path: Path,
):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="failed binding memory", model="test", source="test")
    arguments = {
        "school_layer_id": "school-live-id",
        "residential_layer_id": "residential-live-id",
        "school_type_field": "CCN",
        "school_type_values": ["中小学"],
        "residential_area_field": "面积",
        "area_unit": "square_meter",
        "group_field": "XZQMC",
        "service_distance_m": 1000,
        "target_crs": "EPSG:4490",
    }
    session_db.log_tool_call(
        session.id,
        "execute_school_service_coverage",
        arguments,
        {"success": False, "error": "target_crs 必须是有效的投影 CRS。"},
        duration_ms=15,
    )
    session_db.save_message(session.id, "user", "CRS 使用 EPSG:4526", event_type="user")

    messages = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_conversation_messages(session.id)

    memory = messages[0].content
    assert '"residential_area_field": "面积"' in memory
    assert '"area_unit": "square_meter"' in memory
    assert '"group_field": "XZQMC"' in memory
    assert '"target_crs": "EPSG:4490"' in memory


def test_pipeline_stage_context_passes_only_structured_query_and_previous_stage(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="compact pipeline", model="test", source="test")
    session_db.save_message(session.id, "user", "计算学校服务覆盖率", event_type="user")
    session_db.log_stage_artifact(
        session.id,
        stage_name="data_overview",
        artifact={"layers": ["学校", "城镇住宅区"], "fields": ["CCN", "面积", "XZQMC"]},
        summary="数据已检查",
    )
    session_db.log_stage_artifact(
        session.id,
        stage_name="structured_query",
        artifact={"task": "计算整体及分街道覆盖率"},
        summary="需求已结构化",
    )
    session_db.log_stage_artifact(
        session.id,
        stage_name="solution_plan",
        artifact={"algorithm_evidence": ["native:buffer", "native:intersection"]},
        summary="方案已确认",
    )
    core = AgentCore(session_db=session_db, llm_provider=ToolCallingProvider(), iface=None)
    raw_arguments = "x" * 20000
    tool_results = [
        {
            "name": "record_pipeline_stage",
            "arguments": {"_raw_arguments": raw_arguments},
            "result": {
                "success": False,
                "error_code": "truncated_tool_arguments",
                "error": "参数 JSON 不完整或被截断",
                "expected_stage": "generated_code",
            },
        }
    ]

    messages = core._build_pipeline_stage_messages(session.id)
    compact_results = core._compact_pipeline_tool_results(tool_results)

    assert len(messages) == 1
    assert messages[0].role == "user"
    envelope = json.loads(messages[0].content)
    assert envelope["next_pipeline_stage"] == "generated_code"
    assert envelope["structured_query"] == {"task": "计算整体及分街道覆盖率"}
    assert envelope["previous_stage"] == {
        "stage_name": "solution_plan",
        "artifact": {"algorithm_evidence": ["native:buffer", "native:intersection"]},
    }
    assert "original_user_request" not in envelope
    assert "data_overview" not in envelope
    assert "CCN" not in messages[0].content
    assert core._pipeline_context_should_compact(tool_results) is True
    assert "arguments" not in compact_results[0]
    assert raw_arguments not in json.dumps(compact_results, ensure_ascii=False)
    assert compact_results[0]["result"]["expected_stage"] == "generated_code"


def test_structured_query_context_receives_original_request_and_data_overview(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="structured handoff", model="test", source="test")
    session_db.save_message(
        session.id,
        "assistant",
        "旧任务产生的历史回复，不应进入阶段交接。",
        event_type="summary",
    )
    session_db.save_message(
        session.id,
        "user",
        "计算中小学服务半径覆盖率",
        event_type="user",
    )
    data_overview = {
        "layers": ["学校", "城镇住宅区"],
        "fields": ["CCN", "面积", "XZQMC"],
    }
    session_db.log_stage_artifact(
        session.id,
        stage_name="data_overview",
        artifact=data_overview,
        summary="数据已检查",
    )
    core = AgentCore(session_db=session_db, llm_provider=ToolCallingProvider(), iface=None)

    messages = core._build_pipeline_stage_messages(session.id)

    assert len(messages) == 1
    envelope = json.loads(messages[0].content)
    assert envelope["next_pipeline_stage"] == "structured_query"
    assert envelope["original_user_request"] == "计算中小学服务半径覆盖率"
    assert envelope["previous_stage"] == {
        "stage_name": "data_overview",
        "artifact": data_overview,
    }
    assert "旧任务产生的历史回复" not in messages[0].content


def test_solution_plan_context_receives_only_structured_query(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="solution handoff", model="test", source="test")
    session_db.save_message(session.id, "user", "原始自然语言需求", event_type="user")
    session_db.log_stage_artifact(
        session.id,
        stage_name="data_overview",
        artifact={"large_samples": ["不应传递"]},
        summary="数据已检查",
    )
    structured_query = {
        "task": "计算整体及分街道覆盖率",
        "operations": ["筛选", "缓冲", "相交", "分组统计"],
    }
    session_db.log_stage_artifact(
        session.id,
        stage_name="structured_query",
        artifact=structured_query,
        summary="需求已结构化",
    )
    core = AgentCore(session_db=session_db, llm_provider=ToolCallingProvider(), iface=None)

    messages = core._build_pipeline_stage_messages(session.id)

    envelope = json.loads(messages[0].content)
    assert envelope["next_pipeline_stage"] == "solution_plan"
    assert envelope["structured_query"] == structured_query
    assert "previous_stage" not in envelope
    assert "original_user_request" not in envelope
    assert "large_samples" not in messages[0].content


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
    retry_instruction = retry_messages[-1].content
    assert "每次 execute_gis_code 都会创建全新的空工作目录" in retry_instruction
    assert "严禁写‘中间结果已存在’" in retry_instruction


def test_retry_prompt_preserves_original_full_script_after_partial_retry(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="retry context", model="test", source="test")
    core = AgentCore(session_db=session_db, llm_provider=ToolCallingProvider(), iface=None)

    messages = core._retry_messages_for_code_failure(
        session.id,
        {"code": "PARTIAL_STEP_6", "expected_outputs": []},
        {"success": False, "error": "missing intermediate"},
        original_failed_arguments={
            "code": "FULL_STEPS_1_TO_6",
            "expected_outputs": [{"path": "result.gpkg", "type": "vector"}],
        },
    )

    retry_instruction = messages[-1].content
    assert "FULL_STEPS_1_TO_6" in retry_instruction
    assert "PARTIAL_STEP_6" in retry_instruction
    assert "从当前 QGIS 原始图层开始" in retry_instruction


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
    assert "`dense` 必须规划为 Double" in prompt
    assert "QgsField(\"dense\", double_type" in prompt
    assert "若原字段是文本型，必须先重构为唯一的 Double 字段" in prompt
    assert "FIELD_TYPE=2` 是 Text/String" in prompt
    assert "上一次失败执行的中间文件不会继承" in prompt
    assert "expected_outputs=[]" in prompt
    assert "stdout 本身就是最终答案" in prompt
    assert "setRasterShaderFunction" in prompt


def test_prompt_builder_routes_complex_analysis_to_pipeline():
    prompt = PromptBuilder().build(
        "main-orchestrator",
        QGISContext(project_path="", layer_count=1, layers=[{"name": "建筑物"}]),
    )

    assert "两个及以上步骤" in prompt
    assert "只有未匹配专用 Skill" in prompt
    assert "calculate-school-service-coverage" in prompt
    assert "由当前 AI 根据用户原始请求选择" in prompt
    assert "不得依赖程序分词、关键词计数或相关性分数" in prompt
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

    event_types = [event["type"] for event in events if event["type"] != "run_metrics"]
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


def test_generated_code_stage_recovers_raw_arguments_wrapper(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="raw arguments", model="test", source="test")
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
    raw_arguments = json.dumps(
        {
            "stage_name": "generated_code",
            "summary": "代码生成完成",
            "artifact": {
                "code": "open('result.txt', 'w').write('ok')",
                "expected_outputs": [
                    {"path": "result.txt", "name": "result", "type": "file"}
                ],
                "review": {"passed": True},
            },
        },
        ensure_ascii=False,
    )

    result = tool.handler({"_raw_arguments": raw_arguments})

    assert result["success"] is True
    assert result["stage_name"] == "generated_code"
    assert result["artifact"]["code"] == "open('result.txt', 'w').write('ok')"


def test_pipeline_reports_unrecoverable_raw_arguments(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="bad raw arguments", model="test", source="test")
    tool = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_tool_registry(session.id).get("record_pipeline_stage")

    result = tool.handler({"_raw_arguments": "not json"})

    assert result["success"] is False
    assert "_raw_arguments 不是可恢复的 JSON object" in result["error"]


def test_pipeline_identifies_truncated_generated_code_arguments(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="truncated arguments", model="test", source="test")
    tool = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )._build_tool_registry(session.id).get("record_pipeline_stage")
    truncated = (
        '{"stage_name":"generated_code","artifact":{"code":"from pathlib import Path\\n'
        "output_path = Path(QGIS_AGENT_WORKSPACE) / \\\"result.csv\\\"\\n"
    )

    result = tool.handler({"_raw_arguments": truncated})

    assert result["success"] is False
    assert result["error_code"] == "truncated_tool_arguments"
    assert result["retryable"] is True
    assert result["received_stage"] == "generated_code"
    assert "max_tokens" in result["error"]
    classification = classify_failure(result["error"])
    assert classification["error_code"] == "truncated_tool_arguments"
    assert classification["retryable"] is True


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


def test_generated_code_stage_rejects_feature_count_on_processing_output(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="processing output gate", model="test", source="test")
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
    code = '''
result4 = processing.run("native:extractbylocation", {
    "INPUT": buildings,
    "PREDICATE": [0],
    "INTERSECT": youth_road_buffer,
    "OUTPUT": output_path,
})
output_layer = result4["OUTPUT"]
final_count = output_layer.featureCount()
'''

    result = tool.handler(
        {
            "stage_name": "generated_code",
            "artifact": {
                "code": code,
                "expected_outputs": [
                    {
                        "path": "youth_road_500m_buildings.gpkg",
                        "name": "青年路500m建筑物",
                        "type": "vector",
                    }
                ],
                "review": {"passed": True},
            },
        }
    )

    assert result["success"] is False
    assert "服务端代码检查" in result["error"]
    assert "featureCount" in result["error"]
    assert session_db.list_pipeline_stage_names(session.id)[-1] == "solution_plan"


def test_switching_to_pipeline_starts_new_cycle_after_stale_artifacts(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="new pipeline cycle", model="test", source="test")
    task_id = session_db.create_task(session.id, "开始新的复杂分析任务")
    session_db.replace_plan_steps(
        task_id,
        [
            {
                "id": "pipeline",
                "skill_name": "gis-pipeline",
                "instruction": "执行复杂分析",
                "dependencies": [],
            }
        ],
    )
    session_db.update_task(task_id, status="waiting_for_user")
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
    event_types = [event["type"] for event in events if event["type"] != "run_metrics"]
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

    event_types = [event["type"] for event in events if event["type"] != "run_metrics"]
    assert "tool_start" in event_types
    assert "tool_end" not in event_types
    assert event_types[-2:] == ["message", "complete"]
    final_message = next(event for event in reversed(events) if event["type"] == "message")
    assert final_message["payload"]["content"] == "任务已停止。"

    saved = session_db.get_messages(session.id, limit=20)
    assert saved[-1]["content"] == "任务已停止。"
    assert not any(message["content"].startswith("工具完成：") for message in saved)
