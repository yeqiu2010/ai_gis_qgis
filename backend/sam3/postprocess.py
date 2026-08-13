"""Validate, vectorize and load SAM3 outputs in QGIS."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...database.session_db import SessionDB
from ..tools.layer_ops import build_load_layer_tool


def cleanup_intermediate_files(
    paths: list[str | Path],
    *,
    keep_path: str | Path | None = None,
) -> tuple[list[str], list[str]]:
    """Best-effort cleanup that never invalidates a completed segmentation.

    QGIS/GDAL may keep GeoPackage handles open briefly on Windows.  Locked
    intermediate files are retained in the job workspace and reported as a
    warning instead of turning a valid final output into a failed tool call.
    """
    keep = Path(keep_path).resolve() if keep_path is not None else None
    retained: list[str] = []
    warnings: list[str] = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if keep is not None and path == keep:
            continue
        candidates = [path, Path(f"{path}-wal"), Path(f"{path}-shm")]
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                candidate.unlink()
            except OSError as exc:
                retained.append(str(candidate))
                warnings.append(
                    f"中间文件暂时被 QGIS/GDAL 占用，已保留在任务目录："
                    f"{candidate.name}（{exc}）"
                )
    return retained, warnings


def validate_mask(mask_path: str | Path, *, qgis_executor=None) -> dict[str, Any]:
    mask_path = Path(mask_path).resolve()

    def operation() -> dict[str, Any]:
        from qgis.core import QgsRasterLayer

        layer = QgsRasterLayer(str(mask_path), "SAM3 mask validation")
        if not layer.isValid() or layer.width() <= 0 or layer.height() <= 0:
            raise ValueError("SAM3 返回的 GeoTIFF 无效或无法由 QGIS 打开。")
        return {
            "path": str(mask_path),
            "width": int(layer.width()),
            "height": int(layer.height()),
            "crs": layer.crs().authid() if layer.crs().isValid() else "",
            "extent": {
                "xmin": layer.extent().xMinimum(),
                "ymin": layer.extent().yMinimum(),
                "xmax": layer.extent().xMaximum(),
                "ymax": layer.extent().yMaximum(),
            },
        }

    return qgis_executor(operation) if qgis_executor is not None else operation()


def polygonize_mask(
    mask_path: str | Path,
    output_path: str | Path,
    *,
    job_id: str,
    prompt: str,
    class_name: str,
    source_layer_name: str,
    aoi_layer_id: str = "",
    boxes_layer_id: str = "",
    selected_only: bool = True,
    qgis_executor=None,
) -> dict[str, Any]:
    mask_path = Path(mask_path).resolve()
    output_path = Path(output_path).resolve()

    def operation() -> dict[str, Any]:
        import processing
        from osgeo import gdal
        from qgis.core import (
            QgsCoordinateTransform,
            QgsField,
            QgsGeometry,
            QgsProject,
            QgsVectorLayer,
        )
        from qgis.PyQt.QtCore import QVariant

        raw_path = output_path.with_name("sam3_polygonized_raw.gpkg")
        filtered_path = output_path.with_name("sam3_polygonized_filtered.gpkg")
        processing.run(
            "gdal:polygonize",
            {
                "INPUT": str(mask_path),
                "BAND": 1,
                "FIELD": "object_id",
                "EIGHT_CONNECTEDNESS": False,
                "EXTRA": "",
                "OUTPUT": str(raw_path),
            },
        )
        processing.run(
            "native:extractbyexpression",
            {
                "INPUT": str(raw_path),
                "EXPRESSION": '"object_id" <> 0',
                "OUTPUT": str(filtered_path if aoi_layer_id else output_path),
            },
        )
        if aoi_layer_id:
            from ..tools.layer_ops import _find_layer

            processing.run(
                "native:clip",
                {
                    "INPUT": str(filtered_path),
                    "OVERLAY": _find_layer(aoi_layer_id),
                    "OUTPUT": str(output_path),
                },
            )
        layer = QgsVectorLayer(str(output_path), output_path.stem, "ogr")
        if not layer.isValid():
            raise RuntimeError("SAM3 掩码矢量化输出无效。")
        provider = layer.dataProvider()
        new_fields = [
            QgsField("class_name", QVariant.String),
            QgsField("prompt", QVariant.String),
            QgsField("area_px", QVariant.LongLong),
            QgsField("area_map", QVariant.Double),
            QgsField("source", QVariant.String),
            QgsField("job_id", QVariant.String),
        ]
        if boxes_layer_id:
            new_fields.extend(
                [
                    QgsField("source_fid", QVariant.LongLong),
                    QgsField("association", QVariant.String),
                ]
            )
        provider.addAttributes(new_fields)
        layer.updateFields()
        field_indexes = {field.name(): index for index, field in enumerate(layer.fields())}
        dataset = gdal.Open(str(mask_path))
        transform = dataset.GetGeoTransform() if dataset is not None else None
        pixel_area = abs(transform[1] * transform[5] - transform[2] * transform[4]) if transform else 0
        updates: dict[int, dict[int, Any]] = {}
        object_ids: set[int] = set()
        source_geometries: list[tuple[int, Any]] = []
        if boxes_layer_id:
            from ..tools.layer_ops import _find_layer

            boxes_layer = _find_layer(boxes_layer_id)
            candidates = list(boxes_layer.selectedFeatures()) if selected_only else []
            if selected_only and not candidates:
                raise ValueError("边界框图层的选择集在结果关联前已清空。")
            if not selected_only:
                candidates = list(boxes_layer.getFeatures())
            coordinate_transform = None
            if boxes_layer.crs().isValid() and layer.crs().isValid() and boxes_layer.crs() != layer.crs():
                coordinate_transform = QgsCoordinateTransform(
                    boxes_layer.crs(), layer.crs(), QgsProject.instance()
                )
            for candidate in candidates:
                geometry = candidate.geometry()
                if geometry is None or geometry.isEmpty():
                    continue
                geometry = QgsGeometry.fromRect(geometry.boundingBox())
                if coordinate_transform is not None:
                    geometry.transform(coordinate_transform)
                source_geometries.append((int(candidate.id()), geometry))
        unmatched_count = 0
        for feature in layer.getFeatures():
            object_id = int(feature["object_id"])
            object_ids.add(object_id)
            area_map = float(feature.geometry().area())
            area_px = int(round(area_map / pixel_area)) if pixel_area else 0
            values = {
                field_indexes["class_name"]: class_name,
                field_indexes["prompt"]: prompt,
                field_indexes["area_px"]: area_px,
                field_indexes["area_map"]: area_map,
                field_indexes["source"]: source_layer_name,
                field_indexes["job_id"]: job_id,
            }
            if boxes_layer_id:
                best_fid, best_overlap = None, 0.0
                for source_fid, source_geometry in source_geometries:
                    if not feature.geometry().boundingBox().intersects(source_geometry.boundingBox()):
                        continue
                    overlap = feature.geometry().intersection(source_geometry).area()
                    if overlap > best_overlap:
                        best_fid, best_overlap = source_fid, overlap
                if best_fid is None:
                    unmatched_count += 1
                else:
                    values[field_indexes["source_fid"]] = best_fid
                    values[field_indexes["association"]] = "max_overlap"
            updates[feature.id()] = values
        if updates and not provider.changeAttributeValues(updates):
            raise RuntimeError("无法写入 SAM3 矢量结果属性。")
        dataset = None
        retained_files, cleanup_warnings = cleanup_intermediate_files(
            [raw_path, filtered_path],
            keep_path=output_path,
        )
        return {
            "path": str(output_path),
            "feature_count": int(layer.featureCount()),
            "object_count": len(object_ids),
            "unmatched_source_count": unmatched_count,
            "crs": layer.crs().authid() if layer.crs().isValid() else "",
            "retained_intermediate_files": retained_files,
            "warnings": cleanup_warnings,
        }

    return qgis_executor(operation) if qgis_executor is not None else operation()


def load_outputs(
    outputs: list[dict[str, Any]],
    *,
    session_db: SessionDB | None,
    session_id: str,
    qgis_executor=None,
) -> list[dict[str, Any]]:
    loader = build_load_layer_tool(session_db, session_id, qgis_executor)
    loaded: list[dict[str, Any]] = []
    for output in outputs:
        result = loader.handler(
            {
                "source": output["path"],
                "name": output["name"],
                "layer_type": output["type"],
            }
        )
        loaded.append(result["layer"])
    return loaded
