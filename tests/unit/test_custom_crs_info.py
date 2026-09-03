from __future__ import annotations

from ai_gis_qgis.backend.context.crs_info import describe_crs
from ai_gis_qgis.backend.tools.layer_ops import _layer_summary

WH2000_PROJ = (
    "+proj=tmerc +lat_0=0 +lon_0=114.333333333333 +k=1 "
    "+x_0=800000 +y_0=-3000000 +ellps=GRS80 +units=m +no_defs +type=crs"
)


class FakeCustomCrs:
    def isValid(self):
        return True

    def authid(self):
        return "USER:100001"

    def description(self):
        return "WH2000"

    def toProj(self):
        return WH2000_PROJ

    def toWkt(self):
        return 'PROJCRS["WH2000"]'


class FakeStandardCrs(FakeCustomCrs):
    def authid(self):
        return "EPSG:4490"

    def description(self):
        return "CGCS2000"


class FakeExtent:
    def isNull(self):
        return True


class FakeLayer:
    def crs(self):
        return FakeCustomCrs()

    def id(self):
        return "street-boundary"

    def name(self):
        return "街道界"

    def type(self):
        return 0

    def source(self):
        return "street.gpkg|layername=街道界"

    def featureCount(self):
        return 12

    def extent(self):
        return FakeExtent()

    def isValid(self):
        return True


def test_custom_crs_uses_portable_definition_as_primary_value():
    info = describe_crs(FakeCustomCrs())

    assert info == {
        "crs": WH2000_PROJ,
        "crs_authid": "USER:100001",
        "crs_name": "WH2000",
        "crs_definition": WH2000_PROJ,
        "crs_definition_format": "proj",
    }


def test_standard_crs_keeps_authority_id_as_primary_value():
    info = describe_crs(FakeStandardCrs())

    assert info["crs"] == "EPSG:4490"
    assert info["crs_definition"] == ""


def test_layer_summary_exposes_custom_crs_definition():
    summary = _layer_summary(FakeLayer())

    assert summary["crs"] == WH2000_PROJ
    assert summary["crs_authid"] == "USER:100001"
    assert summary["crs_name"] == "WH2000"
    assert summary["crs_definition"] == WH2000_PROJ
