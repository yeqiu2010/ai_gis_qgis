from __future__ import annotations

import json
from pathlib import Path

from ai_gis_qgis.backend.agent_core import AgentCore
from ai_gis_qgis.backend.llm.base_provider import ChatResponse, ToolCall
from ai_gis_qgis.backend.skills.skill_manager import SkillManager
from ai_gis_qgis.backend.tools.delegation import build_invoke_skill_tool
from ai_gis_qgis.backend.tools.pipeline import build_record_pipeline_stage_tool
from ai_gis_qgis.backend.tools.registry import ToolEntry, ToolRegistry
from ai_gis_qgis.backend.tools.skill_management import (
    build_list_loaded_skills_tool,
    build_load_skill_tool,
    build_unload_skill_tool,
    read_loaded_skills,
)
from ai_gis_qgis.backend.tools.task_planning import build_task_planning_tools
from ai_gis_qgis.database.session_db import SessionDB


class DelegationProvider:
    name = "delegation-test"
    model = "delegation-test"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="read", name="read_context", arguments={})],
            )
        return ChatResponse(
            content=json.dumps(
                {
                    "status": "completed",
                    "summary": "read complete",
                    "artifacts": [],
                    "missing_inputs": [],
                    "evidence": ["context"],
                    "error": None,
                }
            ),
            model=self.model,
        )


class MultiSkillProvider:
    name = "multi-skill-test"
    model = "multi-skill-test"

    def __init__(self):
        self.calls = 0
        self.tool_names_by_call: list[set[str]] = []

    def chat(self, system, messages, tools=None):
        self.calls += 1
        self.tool_names_by_call.append(
            {definition["function"]["name"] for definition in tools or []}
        )
        if self.calls == 1:
            return ChatResponse(
                content="",
                model=self.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="load-school",
                        name="load_skill",
                        arguments={"skill_name": "calculate-school-service-coverage"},
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
                        id="load-map",
                        name="load_skill",
                        arguments={"skill_name": "generate-land-cover-map"},
                    )
                ],
            )
        return ChatResponse(content="Skills loaded.", model=self.model)


def _write_skill(root: Path, name: str, content: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(content, encoding="utf-8")


def test_loader_parses_hermes_metadata_and_qgis_contract(tmp_path: Path):
    _write_skill(
        tmp_path,
        "coverage",
        """---
name: coverage
description: Coverage analysis
version: 1.2.3
author: Tester
license: MIT
platforms: [linux, Windows]
metadata:
  hermes:
    tags: [GIS, Coverage]
    related_skills: [report]
    requires_tools: [inspect_layers]
  qgis_agent:
    inputs:
      required:
        source_layer: {type: qgis_vector_layer}
    outputs:
      coverage_layer: {type: qgis_vector_layer}
    completion:
      required_artifacts: [coverage_layer]
required_environment_variables:
  - name: COVERAGE_TOKEN
    prompt: Enter token
---
# Coverage

Do the work.
""",
    )

    skill = SkillManager(tmp_path).get("coverage")

    assert skill is not None
    assert skill.version == "1.2.3"
    assert skill.tags == ["GIS", "Coverage"]
    assert skill.related_skills == ["report"]
    assert skill.requires_tools == ["inspect_layers"]
    assert skill.execution_contract["outputs"]["coverage_layer"]["type"] == "qgis_vector_layer"
    available, reasons = skill.availability(
        available_tools={"inspect_layers"}, environment={}, current_platform="linux"
    )
    assert available is False
    assert reasons == ["缺少环境变量: COVERAGE_TOKEN"]


def test_skill_catalog_filters_missing_runtime_tools(tmp_path: Path):
    _write_skill(
        tmp_path,
        "unavailable",
        """---
name: unavailable
description: needs a missing tool
tools: [missing_tool]
metadata:
  hermes:
    requires_tools: [missing_tool]
---
# Unavailable
""",
    )
    manager = SkillManager(tmp_path)
    manager.set_runtime_capabilities(
        available_tools={"load_skill"},
        active_toolsets={"default"},
    )

    available, reasons = manager.availability("unavailable")

    assert available is False
    assert any("missing_tool" in reason for reason in reasons)
    assert manager.routing_catalog() == []


def test_progressive_skill_tools_preserve_multiple_loaded_skills(tmp_path: Path):
    for name, tool in (("main-orchestrator", "search_skills"), ("one", "tool_one"), ("two", "tool_two")):
        _write_skill(
            tmp_path,
            name,
            f"""---
name: {name}
description: {name}
tools: [{tool}]
---
# {name}
""",
        )
    manager = SkillManager(tmp_path)
    state: dict[str, str] = {}
    get_state = state.get
    set_state = state.__setitem__
    load = build_load_skill_tool(manager, get_state, set_state, "session")
    unload = build_unload_skill_tool(manager, get_state, set_state, "session")
    listing = build_list_loaded_skills_tool(manager, get_state, "session")

    first = load.handler({"skill_name": "one"})
    second = load.handler({"skill_name": "two"})

    assert first["success"] is True
    assert second["loaded_skills"] == ["main-orchestrator", "one", "two"]
    assert read_loaded_skills(get_state, "session", manager) == [
        "main-orchestrator",
        "one",
        "two",
    ]
    assert [item["name"] for item in listing.handler({})["loaded_skills"]] == [
        "main-orchestrator",
        "one",
        "two",
    ]

    result = unload.handler({"skill_name": "one"})
    assert result["loaded_skills"] == ["main-orchestrator", "two"]
    assert json.loads(state["session:loaded_skills"]) == ["main-orchestrator", "two"]


def test_registry_uses_controlled_union_for_loaded_skills():
    registry = ToolRegistry()
    registry.set_skill_tools({"one": ["tool_one"], "two": ["tool_two"]})
    for name, category in (
        ("tool_one", "domain"),
        ("tool_two", "domain"),
        ("hidden", "domain"),
        ("load_skill", "skill"),
        ("finalize_task", "planning"),
    ):
        registry.register(
            ToolEntry(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
                handler=lambda arguments: {"success": True},
                category=category,
            )
        )

    names = {
        definition["function"]["name"]
        for definition in registry.definitions_for_skills(["one", "two"])
    }

    assert names == {"tool_one", "tool_two", "load_skill", "finalize_task"}


def test_plan_requires_completed_steps_and_contract_artifacts(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "analysis",
        """---
name: analysis
description: analysis
metadata:
  qgis_agent:
    completion:
      required_artifacts: [result_layer]
---
# Analysis
""",
    )
    manager = SkillManager(skills_dir)
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="plan")
    registry = ToolRegistry()
    for entry in build_task_planning_tools(session_db, session.id, manager):
        registry.register(entry)

    created, _ = registry.execute(
        "create_plan",
        {
            "objective": "run analysis and report",
            "steps": [
                {
                    "id": "analysis",
                    "skill_name": "analysis",
                    "instruction": "run analysis",
                    "dependencies": [],
                },
                {
                    "id": "report",
                    "instruction": "write report",
                    "dependencies": ["analysis"],
                },
            ],
        },
    )
    assert created["success"] is True

    incomplete, _ = registry.execute("finalize_task", {"summary": "done"})
    assert incomplete["success"] is False
    assert any("analysis" in error for error in incomplete["completion_errors"])

    analysis_done, _ = registry.execute(
        "complete_plan_step",
        {
            "step_id": "analysis",
            "outputs": {"result_layer": "layer-id"},
            "artifacts": [
                {
                    "artifact_type": "result_layer",
                    "name": "coverage",
                    "payload": {"layer_id": "layer-id"},
                    "verified": False,
                }
            ],
        },
    )
    assert analysis_done["success"] is True
    report_done, _ = registry.execute(
        "complete_plan_step", {"step_id": "report", "outputs": {"text": "ok"}}
    )
    assert report_done["success"] is True

    unverified, _ = registry.execute("finalize_task", {"summary": "done"})
    assert unverified["success"] is False
    assert any("已验证" in error for error in unverified["completion_errors"])

    registered, _ = registry.execute(
        "register_artifact",
        {
            "artifact_type": "result_layer",
            "name": "coverage_verified",
            "payload": {"layer_id": "layer-id"},
            "verified": True,
        },
    )
    assert registered["success"] is True

    finalized, _ = registry.execute("finalize_task", {"summary": "done"})
    assert finalized["success"] is True
    task = session_db.get_active_task(session.id)
    assert task is not None
    assert task["status"] == "completed"


def test_finalize_resolves_relative_artifacts_in_execution_workspace(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="relative GIS artifacts")
    manager = SkillManager(tmp_path / "skills")
    registry = ToolRegistry()
    for entry in build_task_planning_tools(session_db, session.id, manager):
        registry.register(entry)

    created, _ = registry.execute(
        "create_plan",
        {
            "objective": "create DEM and slope outputs",
            "steps": [
                {
                    "id": "analysis",
                    "instruction": "generate all outputs",
                    "dependencies": [],
                }
            ],
        },
    )
    task_id = created["task"]["id"]
    workspace = tmp_path / "workspaces" / "run-id"
    workspace.mkdir(parents=True)
    dem_path = workspace / "wuhan_dem.txt"
    slope_path = workspace / "wuhan_slope.txt"
    merged_path = workspace / "merged_dem.txt"
    for path in (dem_path, slope_path, merged_path):
        path.write_text("verified GIS output", encoding="utf-8")

    # execute_gis_code has already registered verified absolute outputs.
    for name, path in (("wuhan_dem", dem_path), ("wuhan_slope", slope_path)):
        session_db.register_artifact(
            task_id,
            "file",
            step_id="analysis",
            name=name,
            uri=str(path),
            verified=True,
        )

    completed, _ = registry.execute(
        "complete_plan_step",
        {
            "step_id": "analysis",
            "outputs": {"output": "wuhan_dem.txt"},
            "artifacts": [
                {
                    "artifact_type": "file",
                    "name": "merged_dem",
                    "uri": "merged_dem.txt",
                    "verified": True,
                },
                {
                    "artifact_type": "file",
                    "name": "wuhan_dem",
                    "uri": "wuhan_dem.txt",
                    "verified": True,
                },
            ],
        },
    )

    assert completed["success"] is True
    state = session_db.get_task_state(task_id)
    assert state is not None
    registered = state["artifacts"][-2:]
    assert [artifact["uri"] for artifact in registered] == [
        str(merged_path.resolve()),
        str(dem_path.resolve()),
    ]
    assert all(artifact["verified"] for artifact in registered)

    finalized, _ = registry.execute("finalize_task", {"summary": "done"})
    assert finalized["success"] is True


def test_finalize_ignores_superseded_relative_artifact_records(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="legacy duplicate artifacts")
    manager = SkillManager(tmp_path / "skills")
    registry = ToolRegistry()
    for entry in build_task_planning_tools(session_db, session.id, manager):
        registry.register(entry)

    created, _ = registry.execute(
        "create_plan",
        {
            "objective": "finish an already successful execution",
            "steps": [
                {
                    "id": "analysis",
                    "instruction": "generate output",
                    "dependencies": [],
                }
            ],
        },
    )
    task_id = created["task"]["id"]
    completed, _ = registry.execute(
        "complete_plan_step",
        {"step_id": "analysis", "outputs": {"status": "done"}},
    )
    assert completed["success"] is True

    workspace = tmp_path / "workspaces" / "legacy-run"
    workspace.mkdir(parents=True)
    output_path = workspace / "wuhan_dem.txt"
    intermediate_path = workspace / "merged_dem.txt"
    output_path.write_text("verified output", encoding="utf-8")
    intermediate_path.write_text("existing intermediate", encoding="utf-8")
    session_db.register_artifact(
        task_id,
        "file",
        step_id="analysis",
        name="wuhan_dem",
        uri=str(output_path),
        verified=True,
    )
    # These records reproduce task 390: relative paths were downgraded to
    # unverified even though the files exist in the execution workspace.
    session_db.register_artifact(
        task_id,
        "file",
        step_id="analysis",
        name="wuhan_dem",
        uri="wuhan_dem.txt",
        verified=False,
    )
    session_db.register_artifact(
        task_id,
        "file",
        step_id="analysis",
        name="merged_dem",
        uri="merged_dem.txt",
        verified=False,
    )

    finalized, _ = registry.execute("finalize_task", {"summary": "done"})

    assert finalized["success"] is True
    assert finalized["status"] == "completed"


def test_cancelled_plan_cannot_be_revised_or_resumed(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="cancelled plan")
    manager = SkillManager(tmp_path / "skills")
    registry = ToolRegistry()
    for entry in build_task_planning_tools(session_db, session.id, manager):
        registry.register(entry)

    created, _ = registry.execute(
        "create_plan",
        {
            "objective": "旧任务",
            "steps": [{"id": "old", "instruction": "执行旧任务"}],
        },
    )
    old_task_id = created["task"]["id"]
    session_db.update_task(old_task_id, status="cancelled")

    revised, _ = registry.execute(
        "revise_plan",
        {"steps": [{"id": "new", "instruction": "错误恢复旧任务"}]},
    )
    replacement, _ = registry.execute(
        "create_plan",
        {
            "objective": "新任务",
            "steps": [{"id": "new", "instruction": "执行新任务"}],
        },
    )

    assert revised["success"] is False
    assert replacement["success"] is True
    assert replacement["task"]["id"] != old_task_id


def test_invoke_skill_runs_isolated_read_only_loop_and_logs_invocation(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "reader",
        """---
name: reader
description: read context
tools: [read_context]
metadata:
  qgis_agent:
    side_effects:
      writes_files: false
      modifies_qgis_project: false
---
# Reader
Use read_context.
""",
    )
    manager = SkillManager(skills_dir)
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="delegation")
    task_id = session_db.create_task(session.id, "read")
    registry = ToolRegistry()
    registry.set_skill_tools(manager.tool_allowlist())
    registry.register(
        ToolEntry(
            name="read_context",
            description="read",
            parameters={"type": "object", "properties": {}},
            handler=lambda arguments: {"success": True, "value": 42},
            category="read",
        )
    )
    provider = DelegationProvider()
    invoke = build_invoke_skill_tool(
        session_db=session_db,
        session_id=session.id,
        llm_provider=provider,
        skill_manager=manager,
        tool_registry=registry,
    )

    result = invoke.handler(
        {"skill_name": "reader", "task": "read context", "inputs": {}}
    )

    assert result["success"] is True
    assert provider.calls == 2
    invocations = session_db.list_skill_invocations(task_id)
    assert len(invocations) == 1
    assert invocations[0]["execution_mode"] == "delegated"
    assert invocations[0]["status"] == "completed"


def test_invoke_skill_rejects_declared_qgis_side_effects(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "writer",
        """---
name: writer
description: write project
metadata:
  qgis_agent:
    side_effects:
      modifies_qgis_project: true
---
# Writer
""",
    )
    manager = SkillManager(skills_dir)
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="delegation")
    provider = DelegationProvider()
    invoke = build_invoke_skill_tool(
        session_db=session_db,
        session_id=session.id,
        llm_provider=provider,
        skill_manager=manager,
        tool_registry=ToolRegistry(),
    )

    result = invoke.handler(
        {"skill_name": "writer", "task": "write", "inputs": {}}
    )

    assert result["success"] is False
    assert result["recommended_mode"] == "main_loop"
    assert provider.calls == 0


def test_invoke_skill_returns_missing_contract_inputs_without_calling_llm(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "reader",
        """---
name: reader
description: read context
metadata:
  qgis_agent:
    inputs:
      required:
        source: {type: string}
---
# Reader
""",
    )
    manager = SkillManager(skills_dir)
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="delegation inputs")
    task_id = session_db.create_task(session.id, "read")
    provider = DelegationProvider()
    invoke = build_invoke_skill_tool(
        session_db=session_db,
        session_id=session.id,
        llm_provider=provider,
        skill_manager=manager,
        tool_registry=ToolRegistry(),
    )

    result = invoke.handler({"skill_name": "reader", "task": "read", "inputs": {}})

    assert result["status"] == "waiting_for_user"
    assert result["missing_inputs"] == ["source"]
    assert provider.calls == 0
    invocation = session_db.list_skill_invocations(task_id)[0]
    assert invocation["status"] == "waiting_for_user"


def test_agent_core_progressively_exposes_union_of_multiple_skills(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="multi skill")
    provider = MultiSkillProvider()

    AgentCore(session_db=session_db, llm_provider=provider, iface=None).run(
        session_id=session.id,
        user_message="计算覆盖率并制作地图",
    )

    assert provider.calls == 3
    assert "execute_school_service_coverage" not in provider.tool_names_by_call[0]
    assert "execute_school_service_coverage" in provider.tool_names_by_call[1]
    assert "execute_school_service_coverage" in provider.tool_names_by_call[2]
    assert "generate_land_cover_map" in provider.tool_names_by_call[2]
    loaded = json.loads(session_db.get_state(f"{session.id}:loaded_skills") or "[]")
    assert loaded == [
        "main-orchestrator",
        "calculate-school-service-coverage",
        "generate-land-cover-map",
    ]


def test_pipeline_stage_is_synchronized_to_generic_plan(tmp_path: Path):
    session_db = SessionDB(tmp_path / "state.db")
    session = session_db.create_session(title="pipeline plan")
    session_db.save_message(session.id, "user", "执行复杂空间分析")
    tool = build_record_pipeline_stage_tool(session_db, session.id)

    result = tool.handler(
        {
            "stage_name": "data_overview",
            "artifact": {"summary": "数据已确认"},
        }
    )

    assert result["success"] is True
    task = session_db.get_active_task(session.id)
    assert task is not None
    state = session_db.get_task_state(str(task["id"]))
    assert state is not None
    assert [step["id"] for step in state["steps"]] == [
        "pipeline_data_overview",
        "pipeline_structured_query",
        "pipeline_solution_plan",
        "pipeline_generated_code",
        "pipeline_execution_result",
    ]
    assert state["steps"][0]["status"] == "completed"
    assert state["steps"][1]["status"] == "pending"
