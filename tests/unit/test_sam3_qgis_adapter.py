from __future__ import annotations

import pytest
from ai_gis_qgis.backend.sam3.qgis_adapter import normalize_rgb_bands


def test_rgb_band_defaults_cover_gray_and_two_band_rasters():
    assert normalize_rgb_bands(None, 1) == [1, 1, 1]
    assert normalize_rgb_bands(None, 2) == [1, 2, 1]
    assert normalize_rgb_bands(None, 4) == [1, 2, 3]


def test_rgb_band_validation_rejects_out_of_range_values():
    with pytest.raises(ValueError, match="超出范围"):
        normalize_rgb_bands([4, 3, 2], 3)
