"""Trusted SAM3 remote-segmentation tools."""

from __future__ import annotations

import time
from typing import Any

from ...database.session_db import SessionDB
from ..sam3.client import Sam3Client
from ..sam3.errors import Sam3Error
from ..sam3.postprocess import load_outputs, polygonize_mask, validate_mask
from ..sam3.qgis_adapter import (
    collect_prompt_boxes,
    create_request_snapshot,
    inspect_inputs,
)
from ..sam3.workspace import Sam3Workspace
from .layer_ops import _find_layer, _zoom_canvas_to_layer
from .registry import ToolEntry


def build_sam3_tools(
    *,
    config: dict[str, Any] | None,
    executor_config: dict[str, Any] | None,
    session_db: SessionDB | None,
    session_id: str,
    iface=None,
    qgis_executor=None,
    should_cancel=None,
) -> list[ToolEntry]:
    sam3_config = dict(config or {})

    def client() -> Sam3Client:
        if not bool(sam3_config.get("enabled", True)):
            raise Sam3Error("SAM3 功能已在设置中禁用。", code="service_disabled")
        return Sam3Client(sam3_config, should_cancel=should_cancel)

    def check_service(arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        try:
            result = client().health()
            ready = result.get("status") == "ok" and bool(result.get("model_loaded"))
            return {
                "success": ready,
                **result,
                "service_url": str(sam3_config.get("base_url") or ""),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": None if ready else "SAM3 服务已响应，但模型尚未就绪。",
                "error_code": None if ready else "model_not_ready",
            }
        except Sam3Error as exc:
            return {
                **exc.as_payload(),
                "service_url": str(sam3_config.get("base_url") or ""),
            }

    def inspect(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = inspect_inputs(
                input_layer_id=_required(arguments, "input_layer_id"),
                scope_mode=str(arguments.get("scope_mode") or "full"),
                aoi_layer_id=str(arguments.get("aoi_layer_id") or ""),
                boxes_layer_id=str(arguments.get("boxes_layer_id") or ""),
                selected_only=bool(arguments.get("selected_only", True)),
                rgb_bands=_int_list(arguments.get("rgb_bands")),
                iface=iface,
                qgis_executor=qgis_executor,
                max_pixels=int(sam3_config.get("max_pixels") or 100_000_000),
                max_boxes=int(sam3_config.get("max_boxes_per_request") or 64),
            )
            service = check_service({})
            result["service"] = service
            if not service.get("success"):
                result["success"] = False
                result["error"] = service.get("error")
                result["error_code"] = service.get("error_code")
            elif result.get("warnings"):
                result["error"] = "；".join(result["warnings"])
                result["error_code"] = "input_limit_exceeded"
            return result
        except (RuntimeError, ValueError, Sam3Error) as exc:
            if isinstance(exc, Sam3Error):
                return exc.as_payload()
            return {"success": False, "error": str(exc), "error_code": "invalid_input"}

    def preflight(arguments: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize_arguments(arguments, sam3_config)
        result = inspect(normalized)
        if not result.get("success"):
            return {**result, "preflight_failed": True}
        return {"success": True, "arguments": {**normalized, "_inspection": result}}

    def segment(arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        workspace: Sam3Workspace | None = None
        try:
            arguments = _normalize_arguments(arguments, sam3_config)
            inspection = inspect(arguments)
            if not inspection.get("success"):
                return {**inspection, "preflight_failed": True}
            workspace_root = str(
                (executor_config or {}).get("workspace_dir")
                or "~/.qgis_hermes_agent/workspaces"
            )
            workspace = Sam3Workspace.create(workspace_root)
            snapshot = create_request_snapshot(
                input_layer_id=arguments["input_layer_id"],
                output_path=workspace.request_path,
                scope_mode=arguments["scope_mode"],
                aoi_layer_id=arguments.get("aoi_layer_id") or "",
                rgb_bands=inspection.get("rgb_bands"),
                stretch_percentiles=arguments["stretch_percentiles"],
                iface=iface,
                qgis_executor=qgis_executor,
            )
            max_upload_bytes = int(sam3_config.get("max_upload_mb") or 512) * 1024 * 1024
            if int(snapshot["bytes"]) > max_upload_bytes:
                raise Sam3Error(
                    f"请求快照为 {snapshot['bytes'] / (1024 * 1024):.1f} MB，"
                    f"超过插件限制 {max_upload_bytes / (1024 * 1024):.0f} MB。",
                    code="payload_too_large",
                )
            api_client = client()
            mode = arguments["mode"]
            if mode == "text":
                request_result = api_client.segment_text(
                    workspace.request_path,
                    workspace.mask_path,
                    prompt=arguments["prompt"],
                    confidence_threshold=arguments.get("confidence_threshold"),
                    min_size=arguments["min_size_pixels"],
                    max_size=arguments.get("max_size_pixels"),
                )
            elif mode == "automatic":
                request_result = api_client.segment_automatic(
                    workspace.request_path,
                    workspace.mask_path,
                    unique=True,
                    min_size=arguments["min_size_pixels"],
                    max_size=arguments.get("max_size_pixels"),
                )
            else:
                boxes, box_crs = collect_prompt_boxes(
                    boxes_layer_id=arguments["boxes_layer_id"],
                    selected_only=arguments["selected_only"],
                    qgis_executor=qgis_executor,
                )
                max_boxes = int(sam3_config.get("max_boxes_per_request") or 64)
                if len(boxes) > max_boxes:
                    raise Sam3Error(
                        f"边界框数量 {len(boxes)} 超过单次限制 {max_boxes}。",
                        code="input_limit_exceeded",
                    )
                request_result = api_client.segment_boxes(
                    workspace.request_path,
                    workspace.mask_path,
                    boxes=boxes,
                    box_crs=box_crs,
                    min_size=arguments["min_size_pixels"],
                    max_size=arguments.get("max_size_pixels"),
                )
            mask_info = validate_mask(workspace.mask_path, qgis_executor=qgis_executor)
            if (
                int(mask_info["width"]) != int(snapshot["width"])
                or int(mask_info["height"]) != int(snapshot["height"])
            ):
                raise Sam3Error(
                    "SAM3 掩码尺寸与请求影像不一致。",
                    code="invalid_response",
                )
            source_layer = inspection["input_layer"]
            output_name = arguments["output_name"]
            outputs: list[dict[str, Any]] = []
            warnings: list[str] = []
            object_count: int | None = None
            if "raster" in arguments["output_types"]:
                outputs.append(
                    {
                        "path": str(workspace.mask_path),
                        "name": f"{output_name}_mask",
                        "type": "raster",
                    }
                )
            if "vector" in arguments["output_types"]:
                try:
                    vector_info = polygonize_mask(
                        workspace.mask_path,
                        workspace.vector_path,
                        job_id=workspace.job_id,
                        prompt=arguments.get("prompt") or mode,
                        class_name=output_name,
                        source_layer_name=str(source_layer.get("name") or ""),
                        aoi_layer_id=(
                            arguments.get("aoi_layer_id") or ""
                            if arguments["scope_mode"] == "aoi"
                            else ""
                        ),
                        boxes_layer_id=(
                            arguments.get("boxes_layer_id") or "" if mode == "boxes" else ""
                        ),
                        selected_only=arguments["selected_only"],
                        qgis_executor=qgis_executor,
                    )
                    object_count = int(vector_info["object_count"])
                    warnings.extend(str(item) for item in vector_info.get("warnings") or [])
                    if int(vector_info.get("unmatched_source_count") or 0):
                        warnings.append(
                            f"{vector_info['unmatched_source_count']} 个结果未能关联到来源边界框。"
                        )
                    outputs.append(
                        {
                            "path": str(workspace.vector_path),
                            "name": f"{output_name}_objects",
                            "type": "vector",
                            "feature_count": vector_info["feature_count"],
                            "retained_intermediate_files": vector_info.get(
                                "retained_intermediate_files"
                            )
                            or [],
                        }
                    )
                except Exception as exc:
                    warnings.append(f"掩码已生成，但矢量化失败：{exc}")
                    if "raster" not in arguments["output_types"]:
                        raise
            loaded_layers = load_outputs(
                outputs,
                session_db=session_db,
                session_id=session_id,
                qgis_executor=qgis_executor,
            )
            if arguments["zoom_to_result"] and loaded_layers and iface is not None:
                target_id = str(loaded_layers[-1]["id"])
                qgis_executor(
                    lambda: _zoom_to_layer(target_id, iface)
                ) if qgis_executor is not None else _zoom_to_layer(target_id, iface)
            result = {
                "success": True,
                "job_id": workspace.job_id,
                "mode": mode,
                "prompt": arguments.get("prompt") or "",
                "confidence_threshold": arguments.get("confidence_threshold"),
                "parameters": {
                    key: value
                    for key, value in arguments.items()
                    if key != "_inspection"
                },
                "source_layer": source_layer,
                "workspace_dir": str(workspace.directory),
                "request_snapshot": snapshot,
                "service": inspection.get("service") or {},
                "request": request_result,
                "mask": mask_info,
                "outputs": outputs,
                "loaded_layers": loaded_layers,
                "object_count": object_count,
                "crs": mask_info.get("crs") or "",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "warnings": warnings,
            }
            workspace.write_manifest(
                {
                    **result,
                    "arguments": {
                        key: value for key, value in arguments.items() if key != "_inspection"
                    },
                    "service_url": str(sam3_config.get("base_url") or ""),
                }
            )
            return result
        except Sam3Error as exc:
            payload = exc.as_payload()
        except Exception as exc:
            payload = {"success": False, "error": str(exc), "error_code": "sam3_processing_error"}
        if workspace is not None:
            payload["job_id"] = workspace.job_id
            payload["workspace_dir"] = str(workspace.directory)
            workspace.write_manifest(
                {
                    **payload,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "service_url": str(sam3_config.get("base_url") or ""),
                }
            )
        return payload

    common_properties = {
        "input_layer_id": {"type": "string", "description": "输入遥感栅格图层 ID。"},
        "scope_mode": {
            "type": "string",
            "enum": ["full", "canvas", "aoi"],
            "default": "full",
        },
        "aoi_layer_id": {"type": "string", "description": "可选 AOI 面图层 ID。"},
        "boxes_layer_id": {"type": "string", "description": "边界框提示来源矢量图层 ID。"},
        "selected_only": {"type": "boolean", "default": True},
        "rgb_bands": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
            "minItems": 3,
            "maxItems": 3,
        },
    }

    def segmentation_artifacts(result: dict[str, Any]) -> list[dict[str, Any]]:
        artifacts = []
        for output in result.get("outputs") or []:
            if not isinstance(output, dict):
                continue
            artifacts.append(
                {
                    "artifact_type": str(output.get("type") or "segmentation_output"),
                    "name": output.get("name") or output.get("path"),
                    "uri": output.get("path") or output.get("absolute_path"),
                    "payload": output,
                    "verified": True,
                }
            )
        artifacts.append(
            {
                "artifact_type": "segmentation_outputs",
                "name": "SAM3 segmentation outputs",
                "payload": {
                    "job_id": result.get("job_id"),
                    "parameters": result.get("parameters") or {},
                    "loaded_layers": result.get("loaded_layers") or [],
                    "object_count": result.get("object_count"),
                },
                "verified": True,
            }
        )
        return artifacts
    return [
        ToolEntry(
            name="check_sam3_service",
            description="检查已配置的 SAM3-Geo-API 服务和模型是否就绪，不上传任何影像。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=check_service,
            category="sam3",
        ),
        ToolEntry(
            name="inspect_sam3_segmentation_inputs",
            description=(
                "只读检查 SAM3 分割影像、RGB 波段、处理范围、AOI/边界框、"
                "预计像元数和服务状态。执行分割前必须调用。"
            ),
            parameters={
                "type": "object",
                "properties": common_properties,
                "required": ["input_layer_id"],
                "additionalProperties": False,
            },
            handler=inspect,
            category="sam3",
        ),
        ToolEntry(
            name="segment_remote_sensing_image",
            description=(
                "通过已配置的 SAM3 API 对 QGIS 遥感栅格执行文本、自动或矢量框提示分割；"
                "生成 GeoTIFF 掩码和/或 GeoPackage 面图层并加载到当前工程。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    **common_properties,
                    "mode": {"type": "string", "enum": ["text", "automatic", "boxes"]},
                    "prompt": {"type": "string", "description": "文本模式使用的简短英文类别。"},
                    "stretch_percentiles": {
                        "type": "array",
                        "items": {"type": "number", "minimum": 0, "maximum": 100},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "confidence_threshold": {"type": "number", "minimum": 0, "maximum": 1},
                    "min_size_pixels": {"type": "integer", "minimum": 0},
                    "max_size_pixels": {"type": ["integer", "null"], "minimum": 1},
                    "output_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["vector", "raster"]},
                        "minItems": 1,
                        "maxItems": 2,
                    },
                    "output_name": {"type": "string"},
                    "zoom_to_result": {"type": "boolean", "default": True},
                },
                "required": ["input_layer_id", "mode"],
                "additionalProperties": False,
            },
            handler=segment,
            category="sam3",
            requires_confirmation=True,
            writes_project=True,
            preflight=preflight,
            artifact_mapper=segmentation_artifacts,
            idempotency_key_fields=(
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
            ),
            resume_policy="continue_plan",
            execution_affinity="main_thread",
            timeout_seconds=int(sam3_config.get("request_timeout_seconds") or 1200),
        ),
    ]


def _normalize_arguments(arguments: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    mode = str(arguments.get("mode") or "text").strip().lower()
    if mode not in {"text", "automatic", "boxes"}:
        raise ValueError("mode 必须是 text、automatic 或 boxes。")
    prompt = str(arguments.get("prompt") or "").strip()
    if mode == "text" and not prompt:
        raise ValueError("文本分割必须提供 prompt。")
    boxes_layer_id = str(arguments.get("boxes_layer_id") or "").strip()
    if mode == "boxes" and not boxes_layer_id:
        raise ValueError("边界框分割必须提供 boxes_layer_id。")
    output_types = arguments.get("output_types") or [config.get("default_output") or "vector"]
    output_types = list(dict.fromkeys(str(value) for value in output_types))
    if not output_types or any(value not in {"vector", "raster"} for value in output_types):
        raise ValueError("output_types 只能包含 vector 和 raster。")
    min_size = int(arguments.get("min_size_pixels") or 0)
    max_size_raw = arguments.get("max_size_pixels")
    max_size = int(max_size_raw) if max_size_raw not in {None, "", 0} else None
    if min_size < 0 or (max_size is not None and max_size < max(1, min_size)):
        raise ValueError("对象像素大小范围无效。")
    confidence = arguments.get("confidence_threshold")
    if confidence is not None and not 0 <= float(confidence) <= 1:
        raise ValueError("confidence_threshold 必须在 0..1 之间。")
    name = str(arguments.get("output_name") or prompt or "sam3").strip()
    safe_name = "".join(char if char.isalnum() or char in "_-" else "_" for char in name)
    return {
        **arguments,
        "input_layer_id": _required(arguments, "input_layer_id"),
        "mode": mode,
        "prompt": prompt,
        "scope_mode": str(arguments.get("scope_mode") or "full"),
        "aoi_layer_id": str(arguments.get("aoi_layer_id") or ""),
        "boxes_layer_id": boxes_layer_id,
        "selected_only": bool(arguments.get("selected_only", True)),
        "rgb_bands": _int_list(arguments.get("rgb_bands")),
        "stretch_percentiles": [float(value) for value in (arguments.get("stretch_percentiles") or [2, 98])],
        "confidence_threshold": float(confidence) if confidence is not None else None,
        "min_size_pixels": min_size,
        "max_size_pixels": max_size,
        "output_types": output_types,
        "output_name": safe_name or "sam3",
        "zoom_to_result": bool(arguments.get("zoom_to_result", True)),
    }


def _required(arguments: dict[str, Any], key: str) -> str:
    value = str(arguments.get(key) or "").strip()
    if not value:
        raise ValueError(f"必须提供 {key}。")
    return value


def _int_list(value: Any) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("RGB 波段必须是数组。")
    return [int(item) for item in value]


def _zoom_to_layer(layer_id: str, iface) -> None:
    layer = _find_layer(layer_id)
    _zoom_canvas_to_layer(layer, iface)
