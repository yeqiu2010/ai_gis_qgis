"""Collect lightweight QGIS project context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QGISContext:
    project_path: str | None
    layer_count: int
    layers: list[dict[str, Any]]

    @classmethod
    def collect(cls, iface=None) -> QGISContext:
        try:
            from qgis.core import QgsProject
        except Exception:
            return cls(project_path=None, layer_count=0, layers=[])

        project = QgsProject.instance()
        layers = []
        for layer in project.mapLayers().values():
            layers.append(
                {
                    "id": layer.id(),
                    "name": layer.name(),
                    "source": layer.source(),
                    "crs": layer.crs().authid() if layer.crs().isValid() else "",
                    "type": layer.type(),
                }
            )
        return cls(
            project_path=project.fileName() or None,
            layer_count=len(layers),
            layers=layers,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_path": self.project_path,
            "layer_count": self.layer_count,
            "layers": self.layers,
        }
