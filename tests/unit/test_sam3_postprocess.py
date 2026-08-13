from __future__ import annotations

from pathlib import Path

from ai_gis_qgis.backend.sam3.postprocess import cleanup_intermediate_files


def test_locked_intermediate_file_is_retained_as_warning(tmp_path: Path, monkeypatch):
    raw_path = tmp_path / "sam3_polygonized_raw.gpkg"
    output_path = tmp_path / "sam3_objects.gpkg"
    raw_path.write_bytes(b"raw")
    output_path.write_bytes(b"final")
    original_unlink = Path.unlink

    def locked_unlink(path: Path, *args, **kwargs):
        if path.resolve() == raw_path.resolve():
            raise PermissionError(32, "另一个程序正在使用此文件", str(path))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)

    retained, warnings = cleanup_intermediate_files(
        [raw_path, output_path], keep_path=output_path
    )

    assert retained == [str(raw_path.resolve())]
    assert len(warnings) == 1
    assert "已保留在任务目录" in warnings[0]
    assert raw_path.exists()
    assert output_path.exists()


def test_cleanup_removes_unlocked_intermediate_and_keeps_final_output(tmp_path: Path):
    raw_path = tmp_path / "sam3_polygonized_raw.gpkg"
    output_path = tmp_path / "sam3_objects.gpkg"
    raw_path.write_bytes(b"raw")
    output_path.write_bytes(b"final")

    retained, warnings = cleanup_intermediate_files(
        [raw_path, output_path], keep_path=output_path
    )

    assert retained == []
    assert warnings == []
    assert not raw_path.exists()
    assert output_path.exists()
