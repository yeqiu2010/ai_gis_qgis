"""Register SAM3 tools and workflow knowledge through the Plugin contract."""

from __future__ import annotations

from dataclasses import replace
from typing import Any


def _sam3_context_reducer(
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Project a completed segmentation into a compact, reusable GIS artifact."""
    parameters = result.get("parameters")
    parameters = dict(parameters) if isinstance(parameters, dict) else {}
    return {
        key: value
        for key, value in {
            "success": result.get("success", True),
            "error": result.get("error"),
            "error_code": result.get("error_code"),
            "job_id": result.get("job_id"),
            "input_layer_id": parameters.get("input_layer_id")
            or arguments.get("input_layer_id"),
            "prompt": parameters.get("prompt") or arguments.get("prompt"),
            "confidence_threshold": parameters.get("confidence_threshold")
            or result.get("confidence_threshold")
            or arguments.get("confidence_threshold"),
            "scope_mode": parameters.get("scope_mode") or arguments.get("scope_mode"),
            "object_count": result.get("object_count") or result.get("count"),
            "outputs": result.get("outputs") or result.get("output_files"),
            "loaded_layers": result.get("loaded_layers"),
            "artifacts": result.get("artifacts"),
            "context_compacted": True,
        }.items()
        if value is not None
    }


def register(context: Any) -> None:
    package = __package__ or "plugins.sam3"
    root = package.rsplit(".plugins", 1)[0] if ".plugins" in package else ""
    if root:
        module = __import__(
            f"{root}.backend.tools.sam3_segmentation",
            fromlist=["build_sam3_tools"],
        )
    else:
        module = __import__("backend.tools.sam3_segmentation", fromlist=["build_sam3_tools"])
    runtime = context.runtime
    factory = runtime.get("sam3_tool_factory") or module.build_sam3_tools
    entries = factory(
            config=context.config,
            executor_config=runtime.get("executor_config") or {},
            session_db=runtime["session_db"],
            session_id=runtime["session_id"],
            iface=runtime.get("iface"),
            qgis_executor=runtime.get("qgis_executor"),
            should_cancel=runtime.get("should_cancel"),
        )
    context.register_tools(
        replace(
            entry,
            resume_policy="continue_plan",
            context_reducer=_sam3_context_reducer,
            idempotency_key_fields=(
                entry.idempotency_key_fields
                or (
                    "input_layer_id",
                    "scope_mode",
                    "aoi_layer_id",
                    "boxes_layer_id",
                    "mode",
                    "prompt",
                    "confidence_threshold",
                    "min_size_pixels",
                    "max_size_pixels",
                    "output_types",
                    "rgb_bands",
                )
            ),
        )
        if entry.name == "segment_remote_sensing_image"
        else entry
        for entry in entries
    )
    context.register_skill(
        "remote-segmentation",
        "skills/remote-segmentation/SKILL.md",
        aliases=("sam3-remote-segmentation",),
    )
