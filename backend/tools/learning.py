"""Tools for conservative, evidence-backed workflow reuse."""

from __future__ import annotations

from typing import Any

from ...database.session_db import SessionDB
from .registry import ToolEntry


def build_learning_tools(session_db: SessionDB, session_id: str) -> list[ToolEntry]:
    def search(arguments: dict[str, Any]) -> dict[str, Any]:
        recipes = session_db.search_recipes(
            str(arguments.get("query") or ""),
            status=str(arguments.get("status") or "active"),
            limit=int(arguments.get("limit") or 20),
        )
        return {"success": True, "recipes": recipes, "count": len(recipes)}

    def inspect(arguments: dict[str, Any]) -> dict[str, Any]:
        recipe = session_db.get_recipe(str(arguments.get("recipe_id") or ""))
        if recipe is None:
            return {"success": False, "error": "Recipe 不存在"}
        return {"success": True, "recipe": recipe}

    def save_candidate(arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = str(arguments.get("source_task_id") or "").strip() or None
        if task_id is not None:
            task = session_db.get_task(task_id)
            if task is None or task.get("session_id") != session_id:
                return {"success": False, "error": "source_task_id 不属于当前会话"}
        candidate_id = session_db.save_knowledge_candidate(
            candidate_type="recipe",
            target_name=str(arguments.get("name") or arguments.get("intent") or "recipe"),
            payload=dict(arguments.get("recipe") or {}),
            evidence=dict(arguments.get("evidence") or {}),
            evaluation=dict(arguments.get("evaluation") or {}),
            source_task_id=task_id,
        )
        return {"success": True, "candidate_id": candidate_id, "status": "candidate"}

    def list_candidates(arguments: dict[str, Any]) -> dict[str, Any]:
        candidates = session_db.list_knowledge_candidates(
            status=str(arguments.get("status") or "").strip() or None,
            limit=int(arguments.get("limit") or 50),
        )
        return {"success": True, "candidates": candidates, "count": len(candidates)}

    def curator_review(arguments: dict[str, Any]) -> dict[str, Any]:
        proposals = session_db.curator_dry_run()
        return {
            "success": True,
            "dry_run": True,
            "proposals": proposals,
            "message": "Curator 仅生成建议，未修改 Skill、Plugin 或激活 Recipe。",
        }

    def promote(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            recipe_id = session_db.promote_candidate(str(arguments.get("candidate_id") or ""))
        except (KeyError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "recipe_id": recipe_id, "status": "active"}

    def record_feedback(arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = str(arguments.get("task_id") or "")
        task = session_db.get_task(task_id)
        if task is None or task.get("session_id") != session_id:
            return {"success": False, "error": "task_id 不属于当前会话"}
        outcome_id = session_db.record_task_outcome(
            task_id,
            status=str(task.get("status") or "unknown"),
            feedback_score=float(arguments["score"]),
            user_feedback=str(arguments.get("feedback") or ""),
        )
        return {"success": True, "outcome_id": outcome_id}

    def set_recipe_status(arguments: dict[str, Any]) -> dict[str, Any]:
        recipe_id = str(arguments.get("recipe_id") or "")
        status = str(arguments.get("status") or "")
        try:
            session_db.set_recipe_status(recipe_id, status)
        except (KeyError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "recipe_id": recipe_id, "status": status}

    return [
        ToolEntry(
            name="search_solution_recipes",
            description="搜索经过验证并已启用的可复用 GIS 处理方案。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "stale", "archived"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
            handler=search,
            category="learning",
        ),
        ToolEntry(
            name="inspect_solution_recipe",
            description="查看一个 Recipe 的完整步骤、前置条件和成功标准。",
            parameters={
                "type": "object",
                "properties": {"recipe_id": {"type": "string"}},
                "required": ["recipe_id"],
                "additionalProperties": False,
            },
            handler=inspect,
            category="learning",
        ),
        ToolEntry(
            name="save_recipe_candidate",
            description="把本次成功轨迹保存为待评估候选；不会直接激活或修改 Skill。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "intent": {"type": "string"},
                    "recipe": {"type": "object"},
                    "evidence": {"type": "object"},
                    "evaluation": {"type": "object"},
                    "source_task_id": {"type": "string"},
                },
                "required": ["name", "intent", "recipe"],
                "additionalProperties": False,
            },
            handler=save_candidate,
            category="learning",
        ),
        ToolEntry(
            name="list_knowledge_candidates",
            description="列出尚未启用的 Recipe/Skill 优化候选及其证据。",
            parameters={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "additionalProperties": False,
            },
            handler=list_candidates,
            category="learning",
        ),
        ToolEntry(
            name="review_knowledge_candidates",
            description="以 dry-run 方式评审候选，返回晋升或保留建议，不执行修改。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=curator_review,
            category="learning",
        ),
        ToolEntry(
            name="promote_recipe_candidate",
            description="经人工确认后把已评估 Recipe 候选晋升为 active，并保存首个版本。",
            parameters={
                "type": "object",
                "properties": {"candidate_id": {"type": "string"}},
                "required": ["candidate_id"],
                "additionalProperties": False,
            },
            handler=promote,
            category="learning",
            requires_confirmation=True,
        ),
        ToolEntry(
            name="record_task_feedback",
            description="记录用户对已完成任务的评分和反馈，作为 Recipe/Skill 评估证据。",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "feedback": {"type": "string"},
                },
                "required": ["task_id", "score"],
                "additionalProperties": False,
            },
            handler=record_feedback,
            category="learning",
        ),
        ToolEntry(
            name="set_solution_recipe_status",
            description="经人工确认后将 Recipe 标记为 active、stale 或 archived；可用于归档和恢复。",
            parameters={
                "type": "object",
                "properties": {
                    "recipe_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["active", "stale", "archived"],
                    },
                },
                "required": ["recipe_id", "status"],
                "additionalProperties": False,
            },
            handler=set_recipe_status,
            category="learning",
            requires_confirmation=True,
        ),
    ]
