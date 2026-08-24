"""Runtime verification for generated-code output contracts."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile


class ArtifactVerifier:
    """Verify actual output artifacts without reasoning about writer APIs."""

    def verify_all(self, expected_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.verify(expected) for expected in expected_outputs]

    def verify(self, expected: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(expected.get("path") or "")).resolve()
        output_type = str(expected.get("type") or "file").lower()
        required = bool(expected.get("required", True))
        exists = path.is_file()
        size_bytes = path.stat().st_size if exists else None
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {"validation_level": "basic"}

        if not exists:
            if required:
                errors.append("预期输出文件不存在")
        elif not size_bytes:
            errors.append("输出文件为空")
        elif output_type == "raster":
            self._verify_raster(path, metadata, errors, warnings)
        elif output_type == "vector":
            self._verify_vector(path, metadata, errors, warnings)
        elif output_type == "table":
            self._verify_table(path, metadata, errors, warnings)
        else:
            self._verify_generic_file(path, metadata, errors)

        valid = (exists and not errors) or (not required and not exists)
        verified = exists and not errors
        return {
            "path": str(path),
            "name": str(expected.get("name") or path.stem),
            "type": output_type,
            "required": required,
            "exists": exists,
            "valid": valid,
            "verified": verified,
            "size_bytes": size_bytes,
            "validation_errors": errors,
            "validation_warnings": warnings,
            "metadata": metadata,
        }

    @staticmethod
    def _verify_raster(
        path: Path,
        metadata: dict[str, Any],
        errors: list[str],
        warnings: list[str],
    ) -> None:
        try:
            from osgeo import gdal  # type: ignore[import-not-found]
        except ImportError:
            warnings.append("GDAL 不可用，仅完成文件存在性和非空检查")
            return
        try:
            dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
        except Exception as exc:
            errors.append(f"GDAL 无法打开栅格：{exc}")
            return
        if dataset is None:
            errors.append("GDAL 无法打开栅格")
            return
        try:
            spatial_ref = (
                dataset.GetSpatialRef() if hasattr(dataset, "GetSpatialRef") else None
            )
            authority_name = spatial_ref.GetAuthorityName(None) if spatial_ref else None
            authority_code = spatial_ref.GetAuthorityCode(None) if spatial_ref else None
            metadata.update(
                {
                    "validation_level": "structural",
                    "driver": dataset.GetDriver().ShortName if dataset.GetDriver() else "",
                    "width": int(dataset.RasterXSize),
                    "height": int(dataset.RasterYSize),
                    "band_count": int(dataset.RasterCount),
                    "crs": (
                        f"{authority_name}:{authority_code}"
                        if authority_name and authority_code
                        else ""
                    ),
                    "has_projection": bool(dataset.GetProjection()),
                }
            )
            if dataset.RasterXSize <= 0 or dataset.RasterYSize <= 0:
                errors.append("栅格尺寸无效")
            if dataset.RasterCount <= 0:
                errors.append("栅格没有波段")
        except Exception as exc:
            errors.append(f"无法读取栅格元数据：{exc}")
        finally:
            dataset = None

    @staticmethod
    def _verify_vector(
        path: Path,
        metadata: dict[str, Any],
        errors: list[str],
        warnings: list[str],
    ) -> None:
        try:
            from osgeo import ogr  # type: ignore[import-not-found]
        except ImportError:
            if path.suffix.lower() in {".json", ".geojson"}:
                ArtifactVerifier._verify_geojson(path, metadata, errors)
            else:
                warnings.append("OGR 不可用，仅完成文件存在性和非空检查")
            return
        if path.suffix.lower() == ".shp":
            missing_sidecars = [
                suffix
                for suffix in (".shx", ".dbf")
                if not path.with_suffix(suffix).is_file()
            ]
            if missing_sidecars:
                errors.append("Shapefile 缺少必要文件：" + "、".join(missing_sidecars))
                return
        try:
            dataset = ogr.Open(str(path), 0)
        except Exception as exc:
            errors.append(f"OGR 无法打开矢量数据：{exc}")
            return
        if dataset is None:
            errors.append("OGR 无法打开矢量数据")
            return
        try:
            layer_count = int(dataset.GetLayerCount())
            metadata["layer_count"] = layer_count
            metadata["validation_level"] = "structural"
            feature_count = 0
            for index in range(layer_count):
                layer = dataset.GetLayer(index)
                if layer is not None:
                    feature_count += max(0, int(layer.GetFeatureCount()))
            metadata["feature_count"] = feature_count
            if layer_count <= 0:
                errors.append("矢量数据没有可读取图层")
        except Exception as exc:
            errors.append(f"无法读取矢量元数据：{exc}")
        finally:
            dataset = None

    @staticmethod
    def _verify_table(
        path: Path,
        metadata: dict[str, Any],
        errors: list[str],
        warnings: list[str],
    ) -> None:
        if path.suffix.lower() == ".xlsx":
            try:
                with ZipFile(path) as archive:
                    names = set(archive.namelist())
                required_parts = {"[Content_Types].xml", "xl/workbook.xml"}
                missing_parts = sorted(required_parts - names)
                worksheet_count = sum(
                    name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                    for name in names
                )
                if missing_parts:
                    errors.append("XLSX 缺少必要 OOXML 部件：" + "、".join(missing_parts))
                elif worksheet_count <= 0:
                    errors.append("XLSX 不包含可读取的工作表")
                else:
                    metadata.update(
                        {
                            "format": "OOXML/XLSX",
                            "worksheet_count": worksheet_count,
                            "validation_level": "structural",
                        }
                    )
            except (OSError, BadZipFile) as exc:
                errors.append(f"XLSX 无法读取：{exc}")
            return
        if path.suffix.lower() == ".xls":
            try:
                with path.open("rb") as handle:
                    signature = handle.read(8)
                if signature != bytes.fromhex("D0CF11E0A1B11AE1"):
                    errors.append("XLS 文件不是有效的 OLE Compound File")
                else:
                    metadata["format"] = "BIFF8/OLE"
                    metadata["validation_level"] = "signature"
            except OSError as exc:
                errors.append(f"XLS 无法读取：{exc}")
            return
        if path.suffix.lower() != ".csv":
            warnings.append("当前表格格式仅完成文件存在性和非空检查")
            return
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader, None)
            if not header:
                errors.append("CSV 没有可读取的表头")
            else:
                metadata["columns"] = header
                metadata["validation_level"] = "structural"
        except (OSError, UnicodeError, csv.Error) as exc:
            errors.append(f"CSV 无法读取：{exc}")

    @staticmethod
    def _verify_generic_file(
        path: Path,
        metadata: dict[str, Any],
        errors: list[str],
    ) -> None:
        if path.suffix.lower() not in {".json", ".geojson"}:
            return
        ArtifactVerifier._verify_geojson(path, metadata, errors, require_geojson=False)

    @staticmethod
    def _verify_geojson(
        path: Path,
        metadata: dict[str, Any],
        errors: list[str],
        *,
        require_geojson: bool = True,
    ) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"JSON 无法解析：{exc}")
            return
        if not isinstance(payload, dict):
            errors.append("JSON 顶层不是 object")
            return
        metadata["json_type"] = str(payload.get("type") or "")
        metadata["validation_level"] = "structural"
        if require_geojson and payload.get("type") not in {
            "FeatureCollection",
            "Feature",
        }:
            errors.append("文件不是有效的 GeoJSON Feature 或 FeatureCollection")
        if payload.get("type") == "FeatureCollection":
            features = payload.get("features")
            if not isinstance(features, list):
                errors.append("GeoJSON FeatureCollection 缺少 features 列表")
            else:
                metadata["feature_count"] = len(features)
