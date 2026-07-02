"""Read-only GIS analysis tools for Phase 1."""

from __future__ import annotations

from ..context.qgis_context import QGISContext
from .registry import ToolEntry


def build_get_task_context_tool(iface=None, qgis_executor=None) -> ToolEntry:
    def handler(arguments: dict) -> dict:
        if qgis_executor is not None:
            context = qgis_executor(lambda: QGISContext.collect(iface))
        else:
            context = QGISContext.collect(iface)
        return {"success": True, "context": context.to_dict()}

    return ToolEntry(
        name="get_task_context",
        description="Return lightweight QGIS project and layer context.",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        category="analysis",
    )
