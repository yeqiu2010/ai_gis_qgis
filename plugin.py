"""Minimal QGIS plugin shell for Phase 0."""

from __future__ import annotations

from pathlib import Path

try:
    from qgis.PyQt.QtCore import QCoreApplication, Qt
    from qgis.PyQt.QtGui import QIcon
    from qgis.PyQt.QtWidgets import QAction, QDockWidget
except Exception:  # pragma: no cover - QGIS supplies these modules at runtime.
    QCoreApplication = None
    QIcon = None
    QAction = None
    QDockWidget = None
    Qt = None


class QGISHermesAgentPlugin:
    """QGIS plugin lifecycle adapter."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = Path(__file__).resolve().parent
        self.action = None
        self.dock_widget = None
        self.web_view = None

    def tr(self, message: str) -> str:
        if QCoreApplication is None:
            return message
        return QCoreApplication.translate("QGISHermesAgentPlugin", message)

    def initGui(self):
        if QAction is None:
            return

        icon_path = self.plugin_dir / "resources" / "icon.png"
        icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
        self.action = QAction(icon, self.tr("AI GIS Agent"), self.iface.mainWindow())
        self.action.triggered.connect(self.show_panel)
        self.iface.addPluginToMenu(self.tr("&AI GIS Agent"), self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action is not None:
            self.iface.removePluginMenu(self.tr("&AI GIS Agent"), self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

        if self.dock_widget is not None:
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget.deleteLater()
            self.dock_widget = None
            self.web_view = None

    def show_panel(self):
        if self.dock_widget is None:
            self._create_panel()
        self.dock_widget.show()
        if hasattr(self.dock_widget, "raise_"):
            self.dock_widget.raise_()
        elif hasattr(self.dock_widget, "raise"):
            getattr(self.dock_widget, "raise")()

    def _create_panel(self):
        if QDockWidget is None:
            return

        from .qwebengine.web_engine_view import HermesWebEngineView

        self.dock_widget = QDockWidget(self.tr("AI GIS Agent"), self.iface.mainWindow())
        self.dock_widget.setObjectName("HermesGISAgentDock")
        self.web_view = HermesWebEngineView(self.plugin_dir, self.dock_widget, iface=self.iface)
        self.dock_widget.setWidget(self.web_view)
        self.iface.addDockWidget(self._right_dock_area(), self.dock_widget)

    def _right_dock_area(self):
        if hasattr(Qt, "RightDockWidgetArea"):
            return Qt.RightDockWidgetArea
        return Qt.DockWidgetArea.RightDockWidgetArea
