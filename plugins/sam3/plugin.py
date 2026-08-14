"""Register SAM3 tools and workflow knowledge through the Plugin contract."""

from __future__ import annotations

from dataclasses import replace
from typing import Any


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
