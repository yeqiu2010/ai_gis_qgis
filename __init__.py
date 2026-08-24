"""QGIS plugin entrypoint for AI GIS Agent."""

import sys
from pathlib import Path


def _enable_vendored_dependencies() -> None:
    vendor_dir = Path(__file__).resolve().parent / "vendor"
    vendor_path = str(vendor_dir)
    if vendor_dir.is_dir() and vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)


_enable_vendored_dependencies()


def classFactory(iface):
    from .plugin import QGISHermesAgentPlugin

    return QGISHermesAgentPlugin(iface)
