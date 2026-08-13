from __future__ import annotations

import sys
import types

from ai_gis_qgis.backend.tools.layer_ops import _zoom_canvas_to_layer


class _Crs:
    def __init__(self, name: str):
        self.name = name

    def isValid(self):
        return True

    def __eq__(self, other):
        return isinstance(other, _Crs) and self.name == other.name


class _Extent:
    def __init__(self, xmin: float, ymin: float, xmax: float, ymax: float):
        self.values = (xmin, ymin, xmax, ymax)

    def xMinimum(self):
        return self.values[0]

    def yMinimum(self):
        return self.values[1]

    def xMaximum(self):
        return self.values[2]

    def yMaximum(self):
        return self.values[3]


class _Layer:
    def __init__(self):
        self.updated = False
        self.layer_crs = _Crs("EPSG:4326")

    def updateExtents(self):
        self.updated = True

    def extent(self):
        return _Extent(100, 20, 101, 21)

    def crs(self):
        return self.layer_crs


class _MapSettings:
    def destinationCrs(self):
        return _Crs("EPSG:3857")


class _Canvas:
    def __init__(self):
        self.received_extent = None
        self.refreshed = False
        self.rotation_value = 0.0

    def mapSettings(self):
        return _MapSettings()

    def setExtent(self, extent):
        self.received_extent = extent
        # Simulate a canvas/provider side effect seen in QGIS: zooming may
        # temporarily alter the map rotation while recalculating the view.
        self.rotation_value = 17.5

    def rotation(self):
        return self.rotation_value

    def setRotation(self, value):
        self.rotation_value = float(value)

    def refresh(self):
        self.refreshed = True


class _Iface:
    def __init__(self):
        self.canvas = _Canvas()

    def mapCanvas(self):
        return self.canvas


def test_zoom_transforms_layer_extent_to_canvas_crs(monkeypatch):
    transformed = _Extent(1000, 2000, 1100, 2100)

    class _CoordinateTransform:
        def __init__(self, source, target, project):
            assert source.name == "EPSG:4326"
            assert target.name == "EPSG:3857"

        def transformBoundingBox(self, extent):
            assert extent.values == (100, 20, 101, 21)
            return transformed

    qgis_core = types.ModuleType("qgis.core")
    qgis_core.QgsCoordinateTransform = _CoordinateTransform
    qgis_core.QgsProject = types.SimpleNamespace(instance=lambda: object())
    qgis_package = types.ModuleType("qgis")
    qgis_package.core = qgis_core
    monkeypatch.setitem(sys.modules, "qgis", qgis_package)
    monkeypatch.setitem(sys.modules, "qgis.core", qgis_core)
    layer = _Layer()
    iface = _Iface()

    result = _zoom_canvas_to_layer(layer, iface)

    assert layer.updated is True
    assert iface.canvas.received_extent is transformed
    assert iface.canvas.refreshed is True
    assert iface.canvas.rotation_value == 0.0
    assert result == {"xmin": 1000.0, "ymin": 2000.0, "xmax": 1100.0, "ymax": 2100.0}
