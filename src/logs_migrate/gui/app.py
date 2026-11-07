"""`logs-migrate gui`: build the Qt application around the shared engine."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .settings import Settings
from .theme import apply_theme
from .window import MainWindow


def create(
    fixture: str | None = None, journal: str | None = None, argv=None
) -> tuple[QApplication, MainWindow]:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName("VCF Logs Migrator")
    settings = Settings.load()
    if fixture:
        settings.fixture_path = str(fixture)
    if journal:
        settings.journal_path = str(journal)
    apply_theme(app, settings.theme)
    window = MainWindow(settings)
    return app, window


def run(fixture: str | None = None, journal: str | None = None) -> int:
    app, window = create(fixture, journal)
    window.show()
    return app.exec()
