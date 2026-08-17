"""Tools for searching QGIS Processing algorithm documentation."""

from __future__ import annotations

from typing import Any

from ..processing.algorithm_evidence import record_processing_evidence
from ..processing.toolbox_catalog import default_catalog
from .registry import ToolEntry


def build_qgis_toolbox_tools(
    *,
    session_db: Any = None,
    session_id: str = "",
    **_: Any,
) -> list[ToolEntry]:
    catalog = default_catalog()

    def search_domains(arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "query 不能为空"}
        limit = int(arguments.get("limit") or 5)
        return {"domains": catalog.search_domains(query, limit=limit)}

    def search_tools(arguments: dict[str, Any]) -> dict[str, Any]:
        queries = _string_list(arguments.get("queries"))
        query = str(arguments.get("query") or "").strip()
        if query:
            queries.insert(0, query)
        queries = list(dict.fromkeys(item for item in queries if item))
        if not queries:
            return {"success": False, "error": "query 或 queries 至少需要提供一项"}
        domain = str(arguments.get("domain") or "").strip() or None
        limit = int(arguments.get("limit") or 12)
        return {
            "queries": queries,
            "tools": catalog.search_tools_many(queries, domain=domain, limit=limit),
        }

    def get_tool(arguments: dict[str, Any]) -> dict[str, Any]:
        tool_ids = _string_list(arguments.get("tool_ids"))
        tool_id = str(arguments.get("tool_id") or "").strip()
        if tool_id:
            tool_ids.insert(0, tool_id)
        tool_ids = list(dict.fromkeys(item for item in tool_ids if item))
        if not tool_ids:
            return {"success": False, "error": "tool_id 或 tool_ids 至少需要提供一项"}
        tools = [spec.detail() for item in tool_ids if (spec := catalog.get(item)) is not None]
        missing = [item for item in tool_ids if catalog.get(item) is None]
        if not tools:
            return {"success": False, "error": f"找不到 QGIS Processing 工具：{', '.join(missing)}"}
        record_processing_evidence(session_db, session_id, tools)
        result: dict[str, Any] = {"tools": tools}
        if len(tool_ids) == 1:
            result["tool"] = tools[0]
        if missing:
            result["missing_tool_ids"] = missing
        return result

    return [
        ToolEntry(
            name="search_qgis_toolbox_domains",
            description="根据标准 GIS 术语搜索最匹配的 QGIS Toolbox domain。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "标准化后的 GIS 任务或术语。"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 5},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=search_domains,
            category="qgis_toolbox",
        ),
        ToolEntry(
            name="search_qgis_processing_tools",
            description=(
                "根据已经拆解的标准 GIS 操作批量召回 QGIS Processing 候选算法。"
                "先完整分析任务，再用 queries 一次提交全部操作的中英文术语；不要逐步重复搜索。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "单个检索表达式。"},
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 12,
                        "description": "完整任务中所有 GIS 操作的中英文检索表达式。",
                    },
                    "domain": {"type": "string", "description": "可选的 QGIS Toolbox domain。"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 12},
                },
                "additionalProperties": False,
            },
            handler=search_tools,
            category="qgis_toolbox",
        ),
        ToolEntry(
            name="get_qgis_processing_tool",
            description=(
                "批量读取候选 QGIS Processing 算法的真实描述、参数和代码示例，"
                "供方案选择、代码生成和代码审查使用；本工具不执行算法。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tool_id": {"type": "string", "description": "单个 Processing 算法 ID。"},
                    "tool_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 12,
                        "description": "一次读取的候选算法 ID。",
                    },
                },
                "additionalProperties": False,
            },
            handler=get_tool,
            category="qgis_toolbox",
        ),
    ]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
