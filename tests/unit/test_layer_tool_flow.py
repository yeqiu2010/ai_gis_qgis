from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

from ai_gis_qgis.backend.agent_core import AgentCore
from ai_gis_qgis.backend.context.prompt_builder import PromptBuilder
from ai_gis_qgis.backend.context.qgis_context import QGISContext
from ai_gis_qgis.backend.executor.artifact_verifier import ArtifactVerifier
from ai_gis_qgis.backend.executor.qgis_executor import (
    QGISCodeExecutor,
    _process_qt_events,
)
from ai_gis_qgis.backend.failure_analysis import classify_failure
from ai_gis_qgis.backend.llm.base_provider import ChatMessage, ChatResponse, ToolCall
from ai_gis_qgis.backend.llm.errors import LLMRequestRejected
from ai_gis_qgis.backend.tools.code_execution import (
    _load_output_layers,
    build_execute_gis_code_tool,
    find_generated_code_issues,
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


class CompletedSamPlanProvider:
    name = "completed-sam-plan-test"
    model = "completed-sam-plan-test-model"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools=None):
        del system, messages, tools
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("已完成的 SAM3 计划不应再次调用模型")
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="segment-completed-plan",
                    name="segment_remote_sensing_image",
                    arguments={
                        "input_layer_id": "whch-layer-id",
                        "mode": "text",
                        "prompt": "building",
                        "scope_mode": "aoi",
                        "aoi_layer_id": "buffer-layer-id",
                        "confidence_threshold": 0.1,
                        "output_types": ["vector"],
                        "output_name": "sam3_buildings",
                    },
                )
            ],
        )


class RepeatedFailedPostSamProvider(CompletedSamPlanProvider):
    name = "repeated-failed-post-sam-test"
    model = "repeated-failed-post-sam-test-model"

    def chat(self, system, messages, tools=None):
        del system, messages, tools
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="segment-before-export",
                        name="segment_remote_sensing_image",
                        arguments={
                            "input_layer_id": "whch-layer-id",
                            "mode": "text",
                            "prompt": "building",
                            "confidence_threshold": 0.1,
                            "output_types": ["vector"],
                        },
                    )
                ],
            )
        if self.calls > 3:
            raise AssertionError("相同的预检失败应在第二次后熔断")
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id=f"invalid-export-{self.calls}",
                    name="execute_gis_code",
                    arguments={
                        "code": "print('未写入声明的文件')",
                        "expected_outputs": [
                            {
                                "path": "qingnian_road_buffer.geojson",
                                "name": "qingnian_road_buffer",
                                "type": "vector",
                            }
                        ],
                    },
                )
            ],
        )


def _completed_plan_sam_factory(output_path: Path, execution_count: dict[str, int]):
    def build_fake_sam3_tools(**kwargs):
        del kwargs
        parameters = {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }

        def segment(arguments):
            execution_count["value"] += 1
            return {
                "success": True,
                "job_id": "job-completed-plan",
                "parameters": arguments,
                "object_count": 808,
                "outputs": [
                    {
                        "path": str(output_path),
                        "name": "sam3_buildings_objects",
                        "type": "vector",
                    }
                ],
                "loaded_layers": [
                    {
                        "id": "sam3-buildings-layer-id",
                        "name": "sam3_buildings_objects",
                        "type": "vector",
                    }
                ],
            }

        def artifacts(result):
            return [
                {
                    "artifact_type": "vector",
                    "name": "sam3_buildings_objects",
                    "uri": str(output_path),
                    "verified": True,
                },
                {
                    "artifact_type": "qgis_layer",
                    "name": "sam3_buildings_objects",
                    "uri": "sam3-buildings-layer-id",
                    "verified": True,
                },
                {
                    "artifact_type": "segmentation_outputs",
                    "name": "SAM3 segmentation outputs",
                    "payload": {"job_id": result["job_id"]},
                    "verified": True,
                },
            ]

        return [
            ToolEntry(
                name="check_sam3_service",
                description="Fake SAM3 health check.",
                parameters=parameters,
                handler=lambda arguments: {"success": True, "status": "ok"},
                category="sam3",
            ),
            ToolEntry(
                name="inspect_sam3_segmentation_inputs",
                description="Fake SAM3 inspection.",
                parameters=parameters,
                handler=lambda arguments: {
                    "success": True,
                    "inspection_complete": True,
                },
                category="sam3",
            ),
            ToolEntry(
                name="segment_remote_sensing_image",
                description="Fake successful SAM3 segmentation.",
                parameters=parameters,
                handler=segment,
                category="sam3",
                requires_confirmation=True,
                writes_project=True,
                preflight=lambda arguments: {
                    "success": True,
                    "arguments": arguments,
                },
                artifact_mapper=artifacts,
            ),
        ]

    return build_fake_sam3_tools


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
        self.messages: list[list[ChatMessage]] = []

    def chat(self, system, messages, tools=None):
        self.calls += 1
        self.messages.append(list(messages))
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                reasoning_content="需要执行生成的代码。",
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


class RetryThenEquivalentExecutionProvider:
    name = "retry-equivalent-output-test"
    model = "retry-equivalent-output-test-model"

    def __init__(self):
        self.calls = 0

    @staticmethod
    def _execute_call(call_id: str, code: str) -> ChatResponse:
        return ChatResponse(
            content="",
            model="retry-equivalent-output-test-model",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id=call_id,
                    name="execute_gis_code",
                    arguments={
                        "code": code,
                        "expected_outputs": [
                            {
                                "path": "terrain_morphology.tif",
                                "name": "terrain_morphology",
                                "type": "file",
                            }
                        ],
                    },
                )
            ],
        )

    def chat(self, system, messages, tools=None):
        del system, messages, tools
        self.calls += 1
        if self.calls == 1:
            return self._execute_call("initial", "raise RuntimeError('bad table')")
        if self.calls == 2:
            return self._execute_call(
                "retry-success",
                "Path('terrain_morphology.tif').write_bytes(b'first')",
            )
        if self.calls == 3:
            return self._execute_call(
                "equivalent-output",
                "# changed implementation\nPath('terrain_morphology.tif').write_bytes(b'second')",
            )
        if self.calls == 4:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="complete-followup",
                        name="complete_plan_step",
                        arguments={"step_id": "followup", "outputs": {"done": True}},
                    )
                ],
            )
        if self.calls == 5:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="finalize-output-replay",
                        name="finalize_task",
                        arguments={"summary": "任务完成"},
                    )
                ],
            )
        raise AssertionError("复用输出契约后不应继续调用模型")


class DuplicateRetryCodeExecutionProvider:
    name = "duplicate-retry-code-test"
    model = "duplicate-retry-code-test-model"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools=None):
        del system, messages, tools
        self.calls += 1
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id=f"duplicate-{self.calls}",
                    name="execute_gis_code",
                    arguments={
                        "code": "print('missing.txt')",
                        "expected_outputs": [
                            {"path": "missing.txt", "name": "missing", "type": "file"}
                        ],
                    },
                )
            ],
        )


class RepeatedRuntimeFailureProvider(DuplicateRetryCodeExecutionProvider):
    name = "repeated-runtime-failure-test"
    model = "repeated-runtime-failure-test-model"

    def chat(self, system, messages, tools=None):
        del system, messages, tools
        self.calls += 1
        if self.calls > 2:
            raise AssertionError("相同运行时失败出现两次后不应继续生成代码")
        return ChatResponse(
            content="",
            model=self.model,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id=f"runtime-failure-{self.calls}",
                    name="execute_gis_code",
                    arguments={
                        "code": f"print('attempt {self.calls}, still missing output')",
                        "expected_outputs": [
                            {"path": "missing.txt", "name": "missing", "type": "file"}
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


class RejectedLLMProvider:
    name = "rejected-llm-test"
    model = "rejected-llm-test-model"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools=None):
        self.calls += 1
        raise LLMRequestRejected("System message must be at the beginning.")


class InlineThinkingQuestionProvider:
    name = "inline-thinking-question-test"
    model = "inline-thinking-question-test-model"

    def chat(self, system, messages, tools=None):
        return ChatResponse(
            content=(
                "<think>我需要分析图层和字段。用户没有说明分类字段，"
                "应该先询问，不能自行猜测。</think>\n\n"
                "请补充用于区分各类用地的字段名。"
            ),
            model=self.model,
            reasoning_content="结构化推理仍供模型调用链使用。",
        )


class ReasoningContinuationProvider:
    name = "deepseek-continuation-test"
    model = "deepseek-continuation-test-model"

    def __init__(self):
        self.calls = 0
        self.messages_by_call: list[list[ChatMessage]] = []

    def chat(self, system, messages, tools=None):
        del system, tools
        self.calls += 1
        self.messages_by_call.append(list(messages))
        assert all(
            message.reasoning_content is not None
            for message in messages
            if message.role == "assistant"
        )
        if self.calls == 1:
            return ChatResponse(
                content="还需要完成计划步骤。",
                model=self.model,
                reasoning_content="检查到任务仍在运行。",
            )
        if self.calls == 2:
            return ChatResponse(
                content="",
                model=self.model,
                reasoning_content="先登记计划步骤完成。",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="complete-analysis",
                        name="complete_plan_step",
                        arguments={"step_id": "analysis", "outputs": {"done": True}},
                    )
                ],
            )
        if self.calls == 3:
            return ChatResponse(
                content="",
                model=self.model,
                reasoning_content="所有步骤完成，可以结束任务。",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="finalize-analysis",
                        name="finalize_task",
                        arguments={"summary": "任务完成"},
                    )
                ],
            )
        return ChatResponse(
            content="任务完成",
            model=self.model,
            reasoning_content="确认最终工具结果后回复用户。",
        )


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
    assert "中间产物，不是任务完成信号" in provider.systems_by_call[3]
    assert all(message.role != "system" for message in provider.messages_by_call[3])
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
    assert all(
        message.role != "system"
        for call_messages in provider.messages_by_call
        for message in call_messages
    )
    assert "[CONFIRMED TOOL CONTINUATION]" in provider.systems_by_call[1]
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
                            "valid": True,
                            "verified": True,
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


def test_completed_sam3_plan_stops_without_another_model_or_tool_call(
    tmp_path: Path,
    monkeypatch,
):
    output_path = tmp_path / "sam3_buildings.gpkg"
    output_path.touch()
    execution_count = {"value": 0}
    monkeypatch.setattr(
        "ai_gis_qgis.backend.agent_core.build_sam3_tools",
        _completed_plan_sam_factory(output_path, execution_count),
    )
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="completed SAM3 plan",
        model="completed-sam-plan-test-model",
        source="test",
    )
    task_id = session_db.create_task(
        session.id,
        "提取青年路周边500m范围内whch影像中的建筑物",
    )
    session_db.replace_plan_steps(
        task_id,
        [
            {
                "id": "buffer",
                "skill_name": "qgis-toolbox",
                "instruction": "生成青年路500m缓冲区",
                "dependencies": [],
                "status": "completed",
                "outputs": {"layer_id": "buffer-layer-id"},
            },
            {
                "id": "segment",
                "skill_name": "sam3-remote-segmentation",
                "instruction": "使用SAM3在缓冲区内提取建筑物",
                "dependencies": ["buffer"],
                "status": "in_progress",
            },
        ],
    )
    session_db.set_state(f"{session.id}:active_skill", "sam3-remote-segmentation")
    provider = CompletedSamPlanProvider()
    core = AgentCore(session_db=session_db, llm_provider=provider, iface=None)

    events = core.run(
        session_id=session.id,
        user_message="使用SAM3完成建筑物提取，阈值0.1",
    )
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    assert provider.calls == 1
    assert execution_count["value"] == 1
    assert session_db.get_plan_step(task_id, "segment")["status"] == "completed"
    assert session_db.get_task(task_id)["status"] == "completed"
    assert not any(event["type"] == "confirm_request" for event in confirmed_events)
    final_message = next(
        event for event in reversed(confirmed_events) if event["type"] == "message"
    )
    assert "SAM3 分割已完成" in final_message["payload"]["content"]
    assert "808" in final_message["payload"]["content"]


def test_post_sam_output_contract_is_deferred_to_confirmed_runtime(
    tmp_path: Path,
    monkeypatch,
):
    output_path = tmp_path / "sam3_buildings.gpkg"
    output_path.touch()
    execution_count = {"value": 0}
    monkeypatch.setattr(
        "ai_gis_qgis.backend.agent_core.build_sam3_tools",
        _completed_plan_sam_factory(output_path, execution_count),
    )
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="post SAM failure circuit breaker",
        model="repeated-failed-post-sam-test-model",
        source="test",
    )
    task_id = session_db.create_task(session.id, "分割建筑物并导出附加结果")
    session_db.replace_plan_steps(
        task_id,
        [
            {
                "id": "segment",
                "skill_name": "sam3-remote-segmentation",
                "instruction": "使用SAM3提取建筑物",
                "dependencies": [],
                "status": "in_progress",
            },
            {
                "id": "export",
                "skill_name": "qgis-toolbox",
                "instruction": "导出附加结果",
                "dependencies": ["segment"],
                "status": "pending",
            },
        ],
    )
    session_db.set_state(f"{session.id}:active_skill", "sam3-remote-segmentation")
    provider = RepeatedFailedPostSamProvider()
    core = AgentCore(session_db=session_db, llm_provider=provider, iface=None)

    events = core.run(session_id=session.id, user_message="继续执行计划")
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    assert provider.calls == 2
    assert execution_count["value"] == 1
    failed_preflights = [
        event
        for event in confirmed_events
        if event["type"] == "tool_end"
        and event["payload"]["name"] == "execute_gis_code"
        and not event["payload"]["result"].get("success")
    ]
    assert failed_preflights == []
    next_confirmation = next(
        event for event in confirmed_events if event["type"] == "confirm_request"
    )
    assert next_confirmation["payload"]["tool_name"] == "execute_gis_code"
    assert session_db.get_plan_step(task_id, "export")["status"] == "pending"


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


def test_artifact_verifier_reports_runtime_file_evidence(tmp_path: Path):
    report_path = tmp_path / "report.json"
    report_path.write_text('{"status":"ok"}', encoding="utf-8")

    result = ArtifactVerifier().verify(
        {
            "path": str(report_path),
            "name": "report",
            "type": "file",
            "required": True,
        }
    )

    assert result["exists"] is True
    assert result["valid"] is True
    assert result["verified"] is True
    assert result["size_bytes"] > 0
    assert result["metadata"]["json_type"] == ""


def test_artifact_verifier_rejects_empty_and_missing_required_files(tmp_path: Path):
    empty_path = tmp_path / "empty.txt"
    empty_path.touch()
    verifier = ArtifactVerifier()

    empty = verifier.verify(
        {"path": str(empty_path), "name": "empty", "type": "file"}
    )
    missing = verifier.verify(
        {"path": str(tmp_path / "missing.tif"), "name": "missing", "type": "raster"}
    )

    assert empty["verified"] is False
    assert empty["validation_errors"] == ["输出文件为空"]
    assert missing["verified"] is False
    assert missing["validation_errors"] == ["预期输出文件不存在"]


def test_optional_missing_output_does_not_fail_execution(tmp_path: Path):
    executor = QGISCodeExecutor(
        {"workspace_dir": str(tmp_path / "workspaces"), "timeout_seconds": 5}
    )

    result = executor.execute(
        code="print('optional output intentionally omitted')",
        expected_outputs=[
            {
                "path": "optional.txt",
                "name": "optional",
                "type": "file",
                "required": False,
            }
        ],
    )

    assert result["success"] is True
    assert result["outputs"][0]["exists"] is False
    assert result["outputs"][0]["verified"] is False


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


def test_current_qgis_relative_outputs_are_created_in_workspace_and_cwd_is_restored(
    tmp_path: Path,
):
    executor = QGISCodeExecutor(
        {
            "execution_mode": "current_qgis",
            "workspace_dir": str(tmp_path / "workspaces"),
        }
    )
    previous_cwd = Path.cwd()

    result = executor.execute_current_qgis(
        code="Path('terrain_morphology.tif').write_bytes(b'fake-raster')",
        expected_outputs=[
            {
                "path": "terrain_morphology.tif",
                "name": "terrain_morphology",
                "type": "file",
            }
        ],
    )

    output_path = Path(result["outputs"][0]["path"])
    assert result["success"] is True
    assert output_path.parent == Path(result["workspace_dir"])
    assert output_path.read_bytes() == b"fake-raster"
    assert Path.cwd() == previous_cwd


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


def test_processing_clip_extent_uses_runtime_projwin_parameter_name():
    code = '''
processing.run("gdal:cliprasterbyextent", {
    "INPUT": raster_layer,
    "EXTENT": road_layer.extent(),
    "OUTPUT": output_path,
})
'''

    issues = find_generated_code_issues(code)
    message = next(issue for issue in issues if "EXTENT" in issue)

    assert "允许参数" in message
    assert "PROJWIN" in message
    assert "Catalog 代码示例" in message
    assert "'PROJWIN': '0,10,0,10'" in message

    valid_code = '''
processing.run("gdal:cliprasterbyextent", {
    "INPUT": raster_layer,
    "PROJWIN": road_layer,
    "OUTPUT": output_path,
})
'''
    assert find_generated_code_issues(valid_code) == []


def test_heatmap_output_value_typo_has_precise_correction():
    invalid_code = '''
processing.run("qgis:heatmapkerneldensityestimation", {
    "INPUT": points,
    "RADIUS": 400,
    "PIXEL_SIZE": 25,
    "OUTPUT_VALUES": 0,
    "OUTPUT": output_path,
})
'''
    valid_code = invalid_code.replace('"OUTPUT_VALUES"', '"OUTPUT_VALUE"')

    issues = find_generated_code_issues(invalid_code)

    assert len(issues) == 1
    assert "单数 OUTPUT_VALUE" in issues[0]
    assert find_generated_code_issues(valid_code) == []


def test_rejects_string_land_area_as_implicit_geometric_area():
    invalid_code = '''
processing.run("native:fieldcalculator", {
    "INPUT": parcels,
    "FIELD_NAME": "land_area_num",
    "FIELD_TYPE": 0,
    "FORMULA": 'to_real("land_area")',
    "OUTPUT": "memory:",
})
'''
    valid_code = invalid_code.replace(
        "'to_real(\"land_area\")'",
        "'$area'",
    )

    issues = find_generated_code_issues(invalid_code)

    assert any("String 字段 land_area" in issue and "$area" in issue for issue in issues)
    assert find_generated_code_issues(valid_code) == []


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


def test_execute_gis_code_rejects_missing_output_after_runtime_verification(tmp_path: Path):
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
    assert result.get("preflight_failed") is not True
    assert result["error_code"] == "missing_expected_output"
    assert "运行时验证" in result["error"]
    assert "missing.txt" in result["error"]
    assert result["outputs"][0]["exists"] is False
    assert result["outputs"][0]["verified"] is False

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
    assert failures[-1]["error_code"] == "missing_output"
    assert failures[-1]["source_type"] == "tool_call"
    assert failures[-1]["generated_code"] == "print('missing.txt')"


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
    assert "尚未创建" not in result["error"]


def test_preflight_does_not_guess_processing_output_data_flow():
    code = """
import os
workspace = QGIS_AGENT_WORKSPACE
dem_out = os.path.join(workspace, "wuhan_dem.tif")
res = processing.run("custom:clip", {"INPUT": vrt, "MASK": mask, "OUTPUT": dem_out})
clipped = res["OUTPUT"]
slope_out = os.path.join(workspace, "wuhan_slope.tif")
processing.run("custom:slope", {"INPUT": clipped, "OUTPUT": slope_out})
"""

    result = validate_execute_gis_code_arguments(
        {
            "code": code,
            "expected_outputs": [
                {"path": "wuhan_dem.tif", "name": "wuhan_dem", "type": "raster"},
                {"path": "wuhan_slope.tif", "name": "wuhan_slope", "type": "raster"},
            ],
        }
    )

    assert result is None


def test_output_loader_reuses_layer_already_loaded_by_generated_code(
    tmp_path: Path,
    monkeypatch,
):
    output_path = (tmp_path / "wuhan_dem.tif").resolve()
    output_path.touch()

    class FakeCrs:
        def isValid(self):
            return True

        def authid(self):
            return "EPSG:32649"

    class FakeExtent:
        def isNull(self):
            return True

    class FakeLayer:
        def id(self):
            return "already-loaded-raster"

        def name(self):
            return "wuhan_dem"

        def type(self):
            return 1

        def source(self):
            return output_path.as_uri()

        def dataProvider(self):
            return None

        def crs(self):
            return FakeCrs()

        def extent(self):
            return FakeExtent()

        def isValid(self):
            return True

    existing_layer = FakeLayer()

    class FakeProject:
        def __init__(self):
            self.added = []

        def mapLayers(self):
            return {existing_layer.id(): existing_layer}

        def addMapLayer(self, layer):
            self.added.append(layer)

    project = FakeProject()
    constructed = []

    class FakeProjectClass:
        @staticmethod
        def instance():
            return project

    class FakeRasterLayer:
        def __init__(self, path, name):
            constructed.append((path, name))

    monkeypatch.setattr(
        "ai_gis_qgis.backend.tools.code_execution._qgis_classes",
        lambda: {
            "QgsProject": FakeProjectClass,
            "QgsRasterLayer": FakeRasterLayer,
            "QgsVectorLayer": object,
        },
    )

    loaded = _load_output_layers(
        [
            {
                "path": str(output_path),
                "name": "wuhan_dem",
                "type": "raster",
                "verified": True,
            }
        ],
        session_db=None,
        session_id="session",
        qgis_executor=lambda operation: operation(),
    )

    assert project.added == []
    assert constructed == []
    assert loaded[0]["id"] == "already-loaded-raster"
    assert loaded[0]["reused"] is True


def test_output_loader_reuses_new_same_named_layer_when_provider_uri_is_opaque(
    tmp_path: Path,
    monkeypatch,
):
    output_path = (tmp_path / "wuhan_slope.tif").resolve()
    output_path.touch()

    class FakeCrs:
        def isValid(self):
            return True

        def authid(self):
            return "EPSG:32649"

    class FakeExtent:
        def isNull(self):
            return True

    class FakeLayer:
        def __init__(self, layer_id, name, source):
            self._layer_id = layer_id
            self._name = name
            self._source = source

        def id(self):
            return self._layer_id

        def name(self):
            return self._name

        def type(self):
            return 1

        def source(self):
            return self._source

        def dataProvider(self):
            return None

        def crs(self):
            return FakeCrs()

        def extent(self):
            return FakeExtent()

        def isValid(self):
            return True

    old_layer = FakeLayer("old-slope", "wuhan_slope", "opaque:old-output")
    generated_layer = FakeLayer(
        "generated-slope",
        "wuhan_slope",
        "opaque:generated-output",
    )

    class FakeProject:
        def __init__(self):
            self.added = []

        def mapLayers(self):
            return {
                old_layer.id(): old_layer,
                generated_layer.id(): generated_layer,
            }

        def addMapLayer(self, layer):
            self.added.append(layer)

    project = FakeProject()
    constructed = []

    class FakeProjectClass:
        @staticmethod
        def instance():
            return project

    class FakeRasterLayer:
        def __init__(self, path, name):
            constructed.append((path, name))

    monkeypatch.setattr(
        "ai_gis_qgis.backend.tools.code_execution._qgis_classes",
        lambda: {
            "QgsProject": FakeProjectClass,
            "QgsRasterLayer": FakeRasterLayer,
            "QgsVectorLayer": object,
        },
    )

    loaded = _load_output_layers(
        [
            {
                "path": str(output_path),
                "name": "wuhan_slope",
                "type": "raster",
                "verified": True,
            }
        ],
        session_db=None,
        session_id="session",
        qgis_executor=lambda operation: operation(),
        preexisting_layer_ids={"old-slope"},
    )

    assert project.added == []
    assert constructed == []
    assert loaded[0]["id"] == "generated-slope"
    assert loaded[0]["reused"] is True


def test_unrecognized_writer_is_not_rejected_before_confirmation(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="preflight", model="test", source="test")
    provider = InvalidOutputContractProvider()

    events = AgentCore(
        session_db=session_db,
        llm_provider=provider,
        iface=None,
    ).run(session_id=session.id, user_message="按 type 提取公园")

    confirmation = next(event for event in events if event["type"] == "confirm_request")
    assert confirmation["payload"]["tool_name"] == "execute_gis_code"
    assert provider.calls == 1


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
    assert not any("mapLayersByName" in issue for issue in issues)
    assert any("QgsProcessingFeedback" in issue for issue in issues)
    assert any("featureCount" in issue for issue in issues)
    assert any("QgsVectorFileWriter" in issue for issue in issues)
    assert any("addVectorLayer" in issue for issue in issues)


def test_rejects_manual_loading_of_processing_output_and_invalid_raster_apis():
    code = '''
import gdal
import processing
from qgis.core import QgsProject, QgsRasterLayer

result = processing.run("native:reclassifybytable", {
    "INPUT_RASTER": source,
    "RASTER_BAND": 1,
    "TABLE": [0, 5, 1, 5, 15, 2],
    "OUTPUT": output_path,
})
final_layer = QgsRasterLayer(result["OUTPUT"], "terrain_morphology")
QgsProject.instance().addMapLayer(final_layer)
print(final_layer.resX())
project_layer = QgsProject.instance().mapLayer(
    "terrain_de2304ab_33b3_4f19_8288_41554604c949"
)
if project_layer is None:
    raise ValueError("missing layer")
print(project_layer.resolution())
'''

    issues = find_generated_code_issues(code)

    assert any("from osgeo import gdal" in issue for issue in issues)
    assert any("不得再创建 QgsRasterLayer" in issue for issue in issues)
    assert any("QgsRasterLayer 没有 resX()" in issue for issue in issues)
    assert any("QgsRasterLayer 没有 resolution()" in issue for issue in issues)


def test_direct_map_layers_by_name_index_is_not_a_hard_code_check_failure():
    code = '''
from qgis.core import QgsProject

layer = QgsProject.instance().mapLayersByName("建筑物")[0]
print(layer.featureCount())
'''

    assert find_generated_code_issues(code) == []


def test_classifies_projwin_runtime_message_as_public_processing_parameter():
    classification = classify_failure("无法执行算法\nPROJWIN的参数值错误")

    assert classification["error_code"] == "processing_parameters"
    assert "公开 Processing" in classification["cause"]
    assert "EXTENT" in classification["cause"]


def test_rejects_qgis_layer_id_strings_and_unguarded_map_layer_in_processing():
    invalid_code = r'''
from qgis.core import QgsProject
import processing

input_points = "popu_smp_1d56cb20_ee00_49a7_90e1_e776bcd5e944"
road_layer = QgsProject.instance().mapLayer(
    "road_0ffde1b3_371e_4058_9a34_dd7a5b8717e3"
)
extent = road_layer.extent()
processing.run("qgis:heatmapkerneldensityestimation", {
    "INPUT": input_points,
    "RADIUS": 400,
    "PIXEL_SIZE": 25,
    "EXTENT": extent,
    "OUTPUT": "kdst_400.tif",
})
'''

    issues = find_generated_code_issues(invalid_code)

    assert any("不能直接传 QGIS 图层 ID 字符串" in issue for issue in issues)
    assert any("road_layer 未检查是否为 None" in issue for issue in issues)
    assert any("参数不在 Catalog 证据中：EXTENT" in issue for issue in issues)

    valid_code = r'''
from qgis.core import QgsProject
import processing

project = QgsProject.instance()
input_points = project.mapLayer(
    "popu_smp_1d56cb20_ee00_49a7_90e1_e776bcd5e944"
)
road_layer = project.mapLayer("road_0ffde1b3_371e_4058_9a34_dd7a5b8717e3")
if input_points is None or road_layer is None:
    raise ValueError("找不到输入点图层或范围图层")
processing.run("qgis:heatmapkerneldensityestimation", {
    "INPUT": input_points,
    "RADIUS": 400,
    "PIXEL_SIZE": 25,
    "OUTPUT": "kdst_400.tif",
})
'''

    valid_issues = find_generated_code_issues(valid_code)
    assert not any("QGIS 图层 ID" in issue for issue in valid_issues)
    assert not any("未检查是否为 None" in issue for issue in valid_issues)
    assert not any("参数不在 Catalog" in issue for issue in valid_issues)


def test_rejects_layer_name_passed_to_qgs_project_map_layer():
    code = '''
from qgis.core import QgsProject

dem_layer = QgsProject.instance().mapLayer("ASTGTM_N31E114Q")
if dem_layer is None:
    raise ValueError("missing DEM")
'''

    issues = find_generated_code_issues(code)

    assert any("mapLayer() 只接受图层 ID" in issue for issue in issues)
    assert any("ASTGTM_N31E114Q" in issue for issue in issues)


def test_rejects_nested_reclassification_table_before_execution():
    code = '''
import processing

processing.run("native:reclassifybytable", {
    "INPUT_RASTER": slope_layer,
    "RASTER_BAND": 1,
    "TABLE": [[0, 5, 1], [5, 15, 2], [15, 30, 3]],
    "OUTPUT": output_path,
})
'''

    issues = find_generated_code_issues(code)

    assert any("TABLE" in issue and "一维列表" in issue for issue in issues)


def test_generated_code_literal_analysis_handles_reassigned_variables():
    code = '''
layer_id = "first_1d56cb20_ee00_49a7_90e1_e776bcd5e944"
layer_id = "second_0ffde1b3_371e_4058_9a34_dd7a5b8717e3"
print(layer_id)
'''

    assert isinstance(find_generated_code_issues(code), list)


def test_pipeline_rejects_unapproved_algorithm_substitution():
    artifact = {
        "summary": (
            "两次核密度估计，并使用 Uniform 核函数替代用户要求的点密度分析"
        ),
        "steps": [
            {
                "name": "点密度",
                "algorithm": "qgis:heatmapkerneldensityestimation",
            }
        ],
    }

    error = _validate_stage_artifact("solution_plan", artifact)

    assert error is not None
    assert "没有用户批准证据" in error
    assert _validate_stage_artifact(
        "solution_plan",
        {**artifact, "substitution_approved": True},
    ) is None


def test_generated_code_preserves_structured_output_names():
    structured_query = {
        "output": {
            "expected_outputs": [
                {"path": "kdst_400.tif", "name": "kdst_400", "type": "raster"},
                {"path": "kdst_800.tif", "name": "kdst_800", "type": "raster"},
                {"path": "pdst_250.tif", "name": "pdst_250", "type": "raster"},
            ]
        }
    }
    generated = {
        "code": '''
open("kdst_400.tif", "wb").close()
open("kdst_800.tif", "wb").close()
open("pdst_255.tif", "wb").close()
''',
        "expected_outputs": [
            {"path": "kdst_400.tif", "name": "kdst_400", "type": "raster"},
            {"path": "kdst_800.tif", "name": "kdst_800", "type": "raster"},
            {"path": "pdst_255.tif", "name": "pdst_255", "type": "raster"},
        ],
        "review": {"passed": True},
    }

    error = _validate_stage_artifact(
        "generated_code",
        generated,
        structured_query=structured_query,
    )

    assert error is not None
    assert "pdst_250" in error
    assert "数字后缀" in error


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


def test_rejects_nonexistent_singular_color_ramp_item_setter():
    invalid_code = """
color_ramp_shader = QgsColorRampShader(minimum, maximum)
color_ramp_shader.setColorRampItem(
    QgsColorRampShader.ColorRampItem(minimum, QColor("blue"), "low")
)
"""
    issues = find_generated_code_issues(invalid_code)

    assert any(
        "QgsColorRampShader 没有 setColorRampItem" in issue
        and "setColorRampItemList" in issue
        for issue in issues
    )


def test_accepts_color_ramp_item_list_setter():
    valid_code = """
color_ramp_shader = QgsColorRampShader(minimum, maximum)
items = [
    QgsColorRampShader.ColorRampItem(minimum, QColor("blue"), "low"),
    QgsColorRampShader.ColorRampItem(maximum, QColor("red"), "high"),
]
color_ramp_shader.setColorRampItemList(items)
raster_shader = QgsRasterShader()
raster_shader.setRasterShaderFunction(color_ramp_shader)
renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, raster_shader)
"""

    assert find_generated_code_issues(valid_code) == []


def test_rejects_nonexistent_color_ramp_classification_range_setters():
    invalid_code = """
color_ramp_shader = QgsColorRampShader()
color_ramp_shader.setClassificationMin(minimum)
color_ramp_shader.setClassificationMax(maximum)
"""
    issues = find_generated_code_issues(invalid_code)

    assert any(
        "没有 setClassificationMin" in issue and "setMinimumValue" in issue
        for issue in issues
    )
    assert any(
        "没有 setClassificationMax" in issue and "setMaximumValue" in issue
        for issue in issues
    )


def test_classifies_pseudocolor_shader_type_failure_as_generated_code_api():
    failure = classify_failure(
        "QgsSingleBandPseudoColorRenderer.setShader(): argument 1 has unexpected type "
        "'QgsColorRampShader'"
    )

    assert failure["error_code"] == "generated_code_api"
    assert failure["retryable"] is False


def test_classifies_nonexistent_color_ramp_item_setter_as_generated_code_api():
    failure = classify_failure(
        "AttributeError: 'QgsColorRampShader' object has no attribute "
        "'setColorRampItem'"
    )

    assert failure["error_code"] == "generated_code_api"
    assert "setColorRampItemList" in str(failure["cause"])


def test_classifies_nonexistent_color_ramp_range_setter_as_generated_code_api():
    failure = classify_failure(
        "AttributeError: 'QgsColorRampShader' object has no attribute "
        "'setClassificationMin'"
    )

    assert failure["error_code"] == "generated_code_api"
    assert "setMinimumValue" in str(failure["cause"])


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


def test_execute_gis_code_does_not_infer_output_contract_from_code(tmp_path: Path):
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
    assert result["expected_outputs_inferred"] is False
    assert result["expected_outputs"] == []
    assert result["outputs"] == []


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
    assert messages[0].role == "user"
    assert "会话记忆" in memory
    assert "建筑物" in memory
    assert "buildings-live-id" in memory
    assert "leisure" in memory
    assert "landuse" in memory
    assert "中山公园" in memory
    assert messages[-1].content == "导出公园地块"


def test_managed_continuation_preserves_every_assistant_reasoning_turn(
    tmp_path: Path,
):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="reasoning continuation",
        model="deepseek-continuation-test-model",
        source="test",
    )
    task_id = session_db.create_task(session.id, "完成分析")
    session_db.replace_plan_steps(
        task_id,
        [
            {
                "id": "analysis",
                "position": 1,
                "instruction": "完成分析",
                "dependencies": [],
                "status": "pending",
                "inputs": {},
                "outputs": {},
            }
        ],
    )
    provider = ReasoningContinuationProvider()
    core = AgentCore(session_db=session_db, llm_provider=provider, iface=None)

    events = core.run(session_id=session.id, user_message="继续完成任务")

    assert provider.calls == 4
    second_call = provider.messages_by_call[1]
    assert second_call[-2].role == "assistant"
    assert second_call[-2].reasoning_content == "检查到任务仍在运行。"
    assert second_call[-1].role == "user"
    assert second_call[-1].content.startswith("[AGENT CONTINUATION]")
    final_message = next(
        event for event in reversed(events) if event["type"] == "message"
    )
    assert final_message["payload"]["content"] == "任务完成"


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


def test_pipeline_preserves_inspected_layer_id_until_code_generation(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="layer binding handoff",
        model="test",
        source="test",
    )
    session_db.save_message(
        session.id,
        "user",
        "对ASTGTM_N31E114Q图层进行地形形态的分类",
        event_type="user",
    )
    session_db.log_tool_call(
        session.id,
        "inspect_layer",
        {"layer_name": "ASTGTM_N31E114Q"},
        {
            "layer": {
                "id": "ASTGTM_N31E114Q_7f012345_1234_4567_89ab_0123456789ab",
                "name": "ASTGTM_N31E114Q",
                "type": "raster",
                "source": "D:/dem/ASTGTM_N31E114Q.tif",
                "crs": "EPSG:32649",
            },
            "fields": [],
        },
        duration_ms=1,
    )
    core = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
    )
    stage_tool = core._build_tool_registry(session.id).get("record_pipeline_stage")
    overview = stage_tool.handler(
        {
            "stage_name": "data_overview",
            "artifact": {
                "available_layers": ["ASTGTM_N31E114Q"],
                "summary": "DEM已确认",
            },
        }
    )
    session_db.log_stage_artifact(
        session.id,
        stage_name="structured_query",
        artifact={"task": "地形形态分类"},
        summary="需求已结构化",
    )
    session_db.log_stage_artifact(
        session.id,
        stage_name="solution_plan",
        artifact={"steps": []},
        summary="方案已确认",
    )

    envelope = json.loads(core._build_pipeline_stage_messages(session.id)[0].content)

    assert overview["success"] is True
    assert overview["artifact"]["resolved_layers"][0]["name"] == "ASTGTM_N31E114Q"
    assert envelope["next_pipeline_stage"] == "generated_code"
    assert envelope["resolved_layers"][0]["id"].startswith("ASTGTM_N31E114Q_")
    assert "不得把 name 传给 mapLayer()" in envelope["layer_binding_instruction"]


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


def test_pipeline_preserves_processing_details_and_rejects_unverified_plan_algorithms(
    tmp_path: Path,
):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="processing evidence", model="test", source="test")
    session_db.log_stage_artifact(
        session.id,
        stage_name="data_overview",
        artifact={"layers": ["popu_smp", "road"]},
        summary="数据已检查",
    )
    session_db.log_stage_artifact(
        session.id,
        stage_name="structured_query",
        artifact={"task": "按 road 范围生成核密度栅格"},
        summary="需求已结构化",
    )
    core = AgentCore(session_db=session_db, llm_provider=ToolCallingProvider(), iface=None)
    registry = core._build_tool_registry(session.id)
    details = registry.get("get_qgis_processing_tool")
    stage = registry.get("record_pipeline_stage")

    details.handler({"tool_id": "qgis:heatmapkerneldensityestimation"})
    missing_detail = stage.handler(
        {
            "stage_name": "solution_plan",
            "artifact": {
                "operations": [
                    {
                        "algorithm": "qgis:heatmapkerneldensityestimation",
                        "parameters": {"INPUT": "popu_smp", "RADIUS": 400},
                    },
                        {
                            "algorithm": "gdal:cliprasterbyextent",
                            "parameters": {"INPUT": "heatmap", "PROJWIN": "road"},
                    },
                ]
            },
        }
    )

    assert missing_detail["success"] is False
    assert "尚未读取真实详情" in missing_detail["error"]
    assert "gdal:cliprasterbyextent" in missing_detail["error"]

    details.handler({"tool_id": "gdal:cliprasterbyextent"})
    guessed_parameter = stage.handler(
        {
            "stage_name": "solution_plan",
            "artifact": {
                "operations": [
                    {
                        "algorithm": "qgis:heatmapkerneldensityestimation",
                        "parameters": {
                            "INPUT": "popu_smp",
                            "RADIUS": 400,
                            "EXTENT": "road",
                        },
                    },
                        {
                            "algorithm": "gdal:cliprasterbyextent",
                            "parameters": {"INPUT": "heatmap", "PROJWIN": "road"},
                    },
                ]
            },
        }
    )

    assert guessed_parameter["success"] is False
    assert "qgis:heatmapkerneldensityestimation" in guessed_parameter["error"]
    assert "不存在的参数：EXTENT" in guessed_parameter["error"]
    assert "Catalog 示例" in guessed_parameter["error"]

    accepted = stage.handler(
        {
            "stage_name": "solution_plan",
            "artifact": {
                "operations": [
                    {
                        "algorithm": "qgis:heatmapkerneldensityestimation",
                        "parameters": {"INPUT": "popu_smp", "RADIUS": 400},
                    },
                        {
                            "algorithm": "gdal:cliprasterbyextent",
                            "parameters": {"INPUT": "heatmap", "PROJWIN": "road"},
                    },
                ],
                "algorithm_evidence": [
                    "qgis:heatmapkerneldensityestimation",
                    "gdal:cliprasterbyextent",
                ],
            },
        }
    )

    assert accepted["success"] is True
    envelope = json.loads(core._build_pipeline_stage_messages(session.id)[0].content)
    persisted = envelope["processing_algorithm_evidence"]
    assert set(persisted) == {
        "qgis:heatmapkerneldensityestimation",
        "gdal:cliprasterbyextent",
    }
    assert "PROJWIN" in persisted["gdal:cliprasterbyextent"]["parameter_names"]
    assert "'PROJWIN': '0,10,0,10'" in persisted["gdal:cliprasterbyextent"]["code_example"]
    assert "不得凭记忆改名" in envelope["processing_evidence_instruction"]


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
    retry_messages = provider.messages[1]
    assert retry_messages[-1].role == "user"
    original_tool_call = next(
        message
        for message in retry_messages
        if message.role == "assistant" and message.tool_calls
    )
    assert original_tool_call.reasoning_content == "需要执行生成的代码。"
    assert any(
        message.role == "tool" and message.tool_call_id == "call-1"
        for message in retry_messages
    )
    persisted = session_db.get_context_messages(session.id)
    assert any(
        message.get("role") == "assistant"
        and any(
            call.get("id") == "call-2"
            for call in message.get("tool_calls") or []
        )
        for message in persisted
    )
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "call-2"
        for message in persisted
    )
    final_message = next(event for event in reversed(confirmed_events) if event["type"] == "message")
    assert "自动修复并重试 1 次" in final_message["payload"]["content"]


def test_confirmed_execution_reuses_verified_output_contract_when_code_changes(
    tmp_path: Path,
):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="output replay", model="test", source="test")
    core = AgentCore(
        session_db=session_db,
        llm_provider=ToolCallingProvider(),
        iface=None,
        qgis_executor=lambda func: func(),
    )
    registry = core._build_tool_registry(session.id)
    session_db.set_state(f"{session.id}:request_scope", "same-request")
    first_arguments = {
        "code": "print('first implementation')",
        "expected_outputs": [
            {
                "path": "terrain_morphology.tif",
                "name": "terrain_morphology",
                "type": "raster",
            }
        ],
    }
    successful_result = {
        "success": True,
        "workspace_dir": str(tmp_path / "workspaces" / "first"),
        "outputs": [
            {
                "path": str(tmp_path / "workspaces" / "first" / "terrain_morphology.tif"),
                "name": "terrain_morphology",
                "type": "raster",
                "exists": True,
                "verified": True,
            }
        ],
    }
    core._remember_confirmed_tool_call(
        session.id,
        "execute_gis_code",
        first_arguments,
        successful_result,
        registry,
    )

    replay = core._confirmed_tool_replay(
        session.id,
        "execute_gis_code",
        {
            **first_arguments,
            "code": "print('same outputs, changed comments and implementation')",
        },
        registry,
    )

    assert replay is not None
    assert replay["duplicate_prevented"] is True
    assert replay["reuse_reason"] == "verified_output_contract"
    assert replay["workspace_dir"].endswith("first")


def test_retry_success_does_not_confirm_or_execute_equivalent_output_again(
    tmp_path: Path,
):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="retry output replay",
        model="retry-equivalent-output-test-model",
        source="test",
    )
    task_id = session_db.create_task(session.id, "生成分类结果")
    session_db.replace_plan_steps(
        task_id,
        [
            {"id": "analysis", "instruction": "生成结果", "dependencies": []},
            {
                "id": "followup",
                "instruction": "登记完成",
                "dependencies": ["analysis"],
            },
        ],
    )
    provider = RetryThenEquivalentExecutionProvider()
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

    events = core.run(session_id=session.id, user_message="生成分类结果")
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    assert not any(event["type"] == "confirm_request" for event in confirmed_events)
    replay_event = next(
        event
        for event in confirmed_events
        if event["type"] == "tool_end"
        and event["payload"]["result"].get("reuse_reason")
        == "verified_output_contract"
    )
    assert replay_event["payload"]["result"]["duplicate_prevented"] is True
    generated = list((tmp_path / "workspaces").rglob("terrain_morphology.tif"))
    assert len(generated) == 1
    assert generated[0].read_bytes() == b"first"
    assert session_db.get_active_task(session.id)["status"] == "completed"


def test_confirmed_code_execution_does_not_rerun_identical_failed_code(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="duplicate retry",
        model="duplicate-retry-code-test-model",
        source="test",
    )
    provider = DuplicateRetryCodeExecutionProvider()
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

    events = core.run(session_id=session.id, user_message="生成 missing.txt")
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    tool_end_events = [event for event in confirmed_events if event["type"] == "tool_end"]
    assert len(tool_end_events) == 1
    assert provider.calls == 2
    final_message = next(
        event for event in reversed(confirmed_events) if event["type"] == "message"
    )
    assert "相同代码和输出契约" in final_message["payload"]["content"]


def test_confirmed_code_execution_stops_after_same_runtime_failure_twice(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(
        title="same runtime failure",
        model="repeated-runtime-failure-test-model",
        source="test",
    )
    provider = RepeatedRuntimeFailureProvider()
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

    events = core.run(session_id=session.id, user_message="生成 missing.txt")
    confirmation = next(event for event in events if event["type"] == "confirm_request")
    confirmed_events = core.confirm_tool_call(
        session_id=session.id,
        confirmation_id=confirmation["payload"]["confirmation_id"],
        approved=True,
    )

    tool_end_events = [event for event in confirmed_events if event["type"] == "tool_end"]
    assert len(tool_end_events) == 2
    assert provider.calls == 2
    final_message = next(
        event for event in reversed(confirmed_events) if event["type"] == "message"
    )
    assert "相同的运行时失败已连续出现两次" in final_message["payload"]["content"]


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
    assert "QgsProject.addMapLayer()" in prompt


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


def test_generated_code_stage_defers_output_existence_to_runtime(tmp_path: Path):
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

    assert result["success"] is True
    assert result["requested_tool_call"]["name"] == "execute_gis_code"
    assert session_db.list_pipeline_stage_names(session.id)[-1] == "generated_code"


def test_generated_code_stage_rejects_invalid_output_declaration(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="output declaration", model="test", source="test")
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
            "artifact": {
                "code": "print('done')",
                "expected_outputs": [
                    {"path": "result.tif", "name": "result", "type": "raster"},
                    {"path": "result.tif", "name": "duplicate", "type": "unknown"},
                ],
                "review": {"passed": True},
            },
        }
    )

    assert result["success"] is False
    assert "expected_outputs 契约无效" in result["error"]
    assert "重复路径" in result["error"]
    assert "type 无效" in result["error"]


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


def test_agent_core_does_not_retry_rejected_llm_request(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="llm rejected", model="rejected", source="test")
    provider = RejectedLLMProvider()

    events = AgentCore(
        session_db=session_db,
        llm_provider=provider,
        iface=None,
        llm_retry_delay_seconds=0,
    ).run(session_id=session.id, user_message="你好")

    assert provider.calls == 1
    assert not any(
        event["type"] == "thinking" and "正在重试" in event["payload"].get("message", "")
        for event in events
    )
    final_message = next(event for event in reversed(events) if event["type"] == "message")
    assert "系统已停止无效重试" in final_message["payload"]["content"]


def test_agent_hides_inline_thinking_when_asking_for_user_input(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="clarification", model="thinking", source="test")

    events = AgentCore(
        session_db=session_db,
        llm_provider=InlineThinkingQuestionProvider(),
        iface=None,
    ).run(session_id=session.id, user_message="统计各类用地建筑量")

    final_message = next(event for event in reversed(events) if event["type"] == "message")
    assert final_message["payload"]["content"] == "请补充用于区分各类用地的字段名。"
    deltas = "".join(
        str(event["payload"].get("delta") or "")
        for event in events
        if event["type"] == "message_delta"
    )
    assert deltas == "请补充用于区分各类用地的字段名。"
    assert "think" not in deltas
    assert "自行猜测" not in deltas
    saved = session_db.get_messages(session.id, limit=10)
    assert saved[-1]["content"] == "请补充用于区分各类用地的字段名。"


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
