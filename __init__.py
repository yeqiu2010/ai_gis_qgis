"""QGIS plugin entrypoint for AI GIS Agent."""


def classFactory(iface):
    from .plugin import QGISHermesAgentPlugin

    return QGISHermesAgentPlugin(iface)
