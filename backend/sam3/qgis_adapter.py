"""QGIS input inspection and deterministic SAM3 request snapshots."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..tools.layer_ops import _find_layer, _layer_summary, _layer_type_name, _run_qgis


def inspect_inputs(
    *,
    input_layer_id: str,
    scope_mode: str = "full",
    aoi_layer_id: str = "",
    boxes_layer_id: str = "",
    selected_only: bool = True,
    rgb_bands: list[int] | None = None,
    iface=None,
    qgis_executor=None,
    max_pixels: int = 100_000_000,
    max_boxes: int = 64,
) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        layer = _find_layer(input_layer_id)
        if _layer_type_name(layer) != "raster" or not layer.isValid():
            raise ValueError("SAM3 输入必须是有效的栅格图层。")
        width, height = int(layer.width()), int(layer.height())
        band_count = int(layer.bandCount())
        bands = normalize_rgb_bands(rgb_bands, band_count)
        scope_extent = _scope_extent(layer, scope_mode, aoi_layer_id, iface)
        estimated_pixels = _estimate_pixels(layer, scope_extent)
        warnings: list[str] = []
        if estimated_pixels > max_pixels:
            warnings.append(
                f"预计处理 {estimated_pixels:,} 像元，超过限制 {max_pixels:,}；"
                "请改用当前画布范围或 AOI。"
            )
        boxes_info = None
        if boxes_layer_id:
            boxes, box_crs = _collect_boxes(boxes_layer_id, selected_only)
            if len(boxes) > max_boxes:
                warnings.append(
                    f"边界框数量 {len(boxes)} 超过单次限制 {max_boxes}，执行时将阻断。"
                )
            boxes_info = {"count": len(boxes), "crs": box_crs}
        provider = layer.dataProvider()
        source = str(provider.dataSourceUri() if provider is not None else layer.source())
        return {
            "success": not warnings,
            "inspection_complete": True,
            "input_layer": _layer_summary(layer),
            "source_uri": source,
            "width": width,
            "height": height,
            "band_count": band_count,
            "rgb_bands": bands,
            "pixel_count": width * height,
            "estimated_pixels": estimated_pixels,
            "estimated_rgb_upload_mb": round(estimated_pixels * 3 / (1024 * 1024), 2),
            "scope_mode": scope_mode,
            "scope_extent": scope_extent,
            "boxes": boxes_info,
            "warnings": warnings,
        }

    return _run_qgis(qgis_executor, operation)


def create_request_snapshot(
    *,
    input_layer_id: str,
    output_path: str | Path,
    scope_mode: str,
    aoi_layer_id: str,
    rgb_bands: list[int] | None,
    stretch_percentiles: list[float] | None,
    iface=None,
    qgis_executor=None,
) -> dict[str, Any]:
    output_path = Path(output_path).resolve()

    def operation() -> dict[str, Any]:
        try:
            from osgeo import gdal
        except Exception as exc:
            raise RuntimeError("当前 QGIS 环境缺少 GDAL Python 绑定，无法准备 SAM3 影像。") from exc

        layer = _find_layer(input_layer_id)
        if _layer_type_name(layer) != "raster" or not layer.isValid():
            raise ValueError("SAM3 输入必须是有效的栅格图层。")
        provider = layer.dataProvider()
        source_uri = str(provider.dataSourceUri() if provider is not None else layer.source())
        source_path = source_uri.split("|", 1)[0]
        dataset = gdal.Open(source_path)
        if dataset is None:
            dataset = gdal.Open(str(layer.source()).split("|", 1)[0])
        if dataset is None:
            raise ValueError(
                "无法用 GDAL 打开栅格数据源；请先将 WMS/虚拟图层导出为本地 GeoTIFF。"
            )

        band_list = normalize_rgb_bands(rgb_bands, int(dataset.RasterCount))
        extent = _scope_extent(layer, scope_mode, aoi_layer_id, iface)
        percentiles = stretch_percentiles or [2.0, 98.0]
        if len(percentiles) != 2 or not 0 <= percentiles[0] < percentiles[1] <= 100:
            raise ValueError("stretch_percentiles 必须是 0..100 内递增的两个数。")
        scales = [
            [*_percentile_range(dataset.GetRasterBand(index), percentiles), 0, 255]
            for index in band_list
        ]
        options: dict[str, Any] = {
            "format": "GTiff",
            "bandList": band_list,
            "outputType": gdal.GDT_Byte,
            "scaleParams": scales,
            "creationOptions": ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
        }
        if extent:
            options["projWin"] = [
                extent["xmin"],
                extent["ymax"],
                extent["xmax"],
                extent["ymin"],
            ]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        translated = gdal.Translate(str(output_path), dataset, options=gdal.TranslateOptions(**options))
        if translated is None:
            raise RuntimeError("GDAL 无法生成 SAM3 请求快照。")
        width, height = int(translated.RasterXSize), int(translated.RasterYSize)
        projection = str(translated.GetProjection() or "")
        translated.FlushCache()
        translated = None
        dataset = None
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("SAM3 请求快照未生成。")
        return {
            "path": str(output_path),
            "width": width,
            "height": height,
            "pixel_count": width * height,
            "bytes": output_path.stat().st_size,
            "rgb_bands": band_list,
            "stretch_percentiles": [float(percentiles[0]), float(percentiles[1])],
            "projection_present": bool(projection),
            "scope_extent": extent,
        }

    return _run_qgis(qgis_executor, operation)


def collect_prompt_boxes(
    *,
    boxes_layer_id: str,
    selected_only: bool,
    qgis_executor=None,
) -> tuple[list[list[float]], str]:
    return _run_qgis(
        qgis_executor,
        lambda: _collect_boxes(boxes_layer_id, selected_only),
    )


def normalize_rgb_bands(values: list[int] | None, band_count: int) -> list[int]:
    if band_count < 1:
        raise ValueError("输入栅格没有可用波段。")
    if values:
        if len(values) != 3:
            raise ValueError("rgb_bands 必须包含 3 个波段编号。")
        bands = [int(value) for value in values]
    elif band_count == 1:
        bands = [1, 1, 1]
    elif band_count == 2:
        bands = [1, 2, 1]
    else:
        bands = [1, 2, 3]
    if any(index < 1 or index > band_count for index in bands):
        raise ValueError(f"RGB 波段超出范围；输入栅格共有 {band_count} 个波段。")
    return bands


def _scope_extent(layer, scope_mode: str, aoi_layer_id: str, iface) -> dict[str, float] | None:
    if scope_mode == "full":
        return None
    if scope_mode == "canvas":
        if iface is None:
            raise ValueError("当前没有 QGIS 地图画布，不能使用 canvas 范围。")
        canvas = iface.mapCanvas()
        return _transform_extent(canvas.extent(), canvas.mapSettings().destinationCrs(), layer.crs())
    if scope_mode == "aoi":
        if not aoi_layer_id:
            raise ValueError("scope_mode=aoi 时必须提供 aoi_layer_id。")
        aoi = _find_layer(aoi_layer_id)
        if _layer_type_name(aoi) != "vector" or not aoi.isValid() or aoi.featureCount() == 0:
            raise ValueError("AOI 必须是有效且非空的矢量图层。")
        extent = _transform_extent(aoi.extent(), aoi.crs(), layer.crs())
        layer_extent = _rect_to_dict(layer.extent())
        xmin = max(layer_extent["xmin"], extent["xmin"])
        ymin = max(layer_extent["ymin"], extent["ymin"])
        xmax = min(layer_extent["xmax"], extent["xmax"])
        ymax = min(layer_extent["ymax"], extent["ymax"])
        if xmin >= xmax or ymin >= ymax:
            raise ValueError("AOI 与输入影像范围不相交。")
        return {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}
    raise ValueError("scope_mode 必须是 full、canvas 或 aoi。")


def _transform_extent(extent, source_crs, target_crs) -> dict[str, float]:
    from qgis.core import QgsCoordinateTransform, QgsProject

    if source_crs.isValid() and target_crs.isValid() and source_crs != target_crs:
        extent = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance()).transformBoundingBox(extent)
    return _rect_to_dict(extent)


def _rect_to_dict(rect) -> dict[str, float]:
    return {
        "xmin": float(rect.xMinimum()),
        "ymin": float(rect.yMinimum()),
        "xmax": float(rect.xMaximum()),
        "ymax": float(rect.yMaximum()),
    }


def _estimate_pixels(layer, extent: dict[str, float] | None) -> int:
    total = max(0, int(layer.width()) * int(layer.height()))
    if extent is None or total == 0:
        return total
    full = layer.extent()
    full_area = float(full.width()) * float(full.height())
    requested_area = max(0.0, extent["xmax"] - extent["xmin"]) * max(
        0.0, extent["ymax"] - extent["ymin"]
    )
    return min(total, max(1, int(math.ceil(total * requested_area / full_area)))) if full_area else total


def _collect_boxes(layer_id: str, selected_only: bool) -> tuple[list[list[float]], str]:
    layer = _find_layer(layer_id)
    if _layer_type_name(layer) != "vector" or not layer.isValid():
        raise ValueError("边界框来源必须是有效矢量图层。")
    selected = list(layer.selectedFeatures()) if selected_only else []
    features = selected if selected else list(layer.getFeatures())
    if selected_only and not selected:
        raise ValueError("边界框图层没有选中要素。")
    boxes: list[list[float]] = []
    for feature in features:
        geometry = feature.geometry()
        if geometry is None or geometry.isEmpty():
            continue
        box = geometry.boundingBox()
        if box.width() <= 0 or box.height() <= 0:
            continue
        boxes.append(
            [box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()]
        )
    if not boxes:
        raise ValueError("边界框图层没有可用的非空要素。")
    crs = layer.crs().authid() if layer.crs().isValid() else ""
    if not crs:
        raise ValueError("边界框图层必须具有有效 CRS。")
    return boxes, crs


def _percentile_range(band, percentiles: list[float]) -> tuple[float, float]:
    stats = band.ComputeStatistics(True)
    minimum, maximum = float(stats[0]), float(stats[1])
    if not math.isfinite(minimum) or not math.isfinite(maximum) or maximum <= minimum:
        return 0.0, 1.0
    histogram = band.GetHistogram(minimum, maximum, 256, True, True)
    total = sum(histogram or [])
    if not total:
        return minimum, maximum
    targets = [total * float(value) / 100.0 for value in percentiles]
    indexes: list[int] = []
    cumulative = 0
    target_index = 0
    for index, count in enumerate(histogram):
        cumulative += count
        while target_index < len(targets) and cumulative >= targets[target_index]:
            indexes.append(index)
            target_index += 1
    while len(indexes) < 2:
        indexes.append(255)
    step = (maximum - minimum) / 255.0
    low, high = minimum + indexes[0] * step, minimum + indexes[1] * step
    return (low, high) if high > low else (minimum, maximum)
