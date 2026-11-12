"""`logs-migrate gui`: build the Qt application around the shared engine."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from logs_migrate import __version__

from .settings import Settings
from .theme import apply_theme
from .window import MainWindow

ICON_SVG = Path(__file__).parent / "assets" / "app.svg"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def app_icon() -> QIcon:
    """The app icon, drawn from the SVG at each size so it needs no Qt icon plugin."""
    renderer = QSvgRenderer(str(ICON_SVG))
    icon = QIcon()
    for size in ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def create(
    fixture: str | None = None, journal: str | None = None, argv=None
) -> tuple[QApplication, MainWindow]:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName("VCF Logs Migrator")
    app.setApplicationVersion(__version__)
    # Title bar, taskbar or dock, and every dialog; the desktop file name lets Linux
    # shells match the running window to an installed launcher.
    app.setWindowIcon(app_icon())
    app.setDesktopFileName("vcf-logs-migrator")
    settings = Settings.load()
    if fixture:
        settings.fixture_path = str(fixture)
    if journal:
        settings.journal_path = str(journal)
    if settings.debug_logging:
        from ..diagnostics import configure, log_path

        if log_path() is None:
            try:
                configure()
            except OSError:
                pass
    apply_theme(app, settings.theme)
    window = MainWindow(settings)
    return app, window


def run(fixture: str | None = None, journal: str | None = None) -> int:
    app, window = create(fixture, journal)
    window.show()
    return app.exec()
