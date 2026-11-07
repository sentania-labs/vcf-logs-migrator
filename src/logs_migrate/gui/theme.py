"""Lattice for Qt Widgets: Fusion style, generated palette, then the generated QSS.

Assets are vendored unchanged from the Lattice `dist/` directory (commit 57d7a31e). Palette
roles must be applied before the stylesheet so native indicators and unstyled controls match.
"""

from __future__ import annotations

import json
from importlib import resources

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

TOKENS = {
    "light": {
        "bg": "#f6f7f9",
        "surface": "#ffffff",
        "surface-sunken": "#eef1f5",
        "line": "#d9dee5",
        "ink": "#1d2430",
        "ink-muted": "#5a6676",
        "ink-subtle": "#6c7788",
        "accent": "#1f5fbf",
        "accent-soft": "#e8f0fc",
        "accent-ink": "#12376f",
        "ok": "#1a7f4b",
        "warn": "#8a6100",
        "bad": "#a22c1c",
    },
    "dark": {
        "bg": "#1b1f24",
        "surface": "#23282f",
        "surface-sunken": "#2a3038",
        "line": "#363d47",
        "ink": "#f2f4f7",
        "ink-muted": "#b7bfca",
        "ink-subtle": "#868f9c",
        "accent": "#3987e5",
        "accent-soft": "#2b3a4f",
        "accent-ink": "#8fbcf5",
        "ok": "#199e70",
        "warn": "#e0a400",
        "bad": "#d95926",
    },
}

_current = "light"


def _asset(name: str) -> str:
    return resources.files("logs_migrate.gui.lattice").joinpath(name).read_text(encoding="utf-8")


def current_theme() -> str:
    return _current


def token(name: str) -> QColor:
    return QColor(TOKENS[_current][name])


def apply_theme(app: QApplication, theme: str) -> None:
    global _current
    theme = theme if theme in TOKENS else "light"
    _current = theme
    app.setStyle("Fusion")
    palette = QPalette()
    for group, roles in json.loads(_asset("lattice-palette.json"))[theme].items():
        for name, color in roles.items():
            role = getattr(QPalette.ColorRole, name, None)
            if role is not None:  # Accent arrived in Qt 6.6.
                palette.setColor(getattr(QPalette.ColorGroup, group), role, QColor(color))
    app.setPalette(palette)
    assets = resources.files("logs_migrate.gui.lattice")
    suffix = "light" if theme == "light" else "dark"
    with (
        resources.as_file(assets.joinpath("check.svg")) as check,
        resources.as_file(assets.joinpath(f"arrow-up-{suffix}.svg")) as up,
        resources.as_file(assets.joinpath(f"arrow-down-{suffix}.svg")) as down,
    ):
        extra = (
            EXTRA.replace("CHECK_SVG", check.as_posix())
            .replace("ARROW_UP", up.as_posix())
            .replace("ARROW_DOWN", down.as_posix())
            .replace("BOX_BORDER", TOKENS[theme]["ink-subtle"])
            .replace("ACCENT", TOKENS[theme]["accent"])
        )
    app.setStyleSheet(_asset("lattice.qss" if theme == "light" else "lattice-dark.qss") + extra)


# Layout rules QSS cannot express from tokens alone: the workspace frame and the sidebar list.
EXTRA = """
QFrame[lattice="header"], QFrame[lattice="sidebar"] { border-radius: 0px; }
QFrame[lattice="header"] { border-top: none; border-left: none; border-right: none; }
QFrame[lattice="sidebar"] { border-top: none; border-left: none; border-bottom: none; }
QFrame[lattice="footer"] { border-top: 1px solid palette(mid); }
QListWidget[lattice="steps"] { border: none; background: transparent; outline: none; }
QListWidget[lattice="steps"]::item { padding: 8px 12px; border-left: 3px solid transparent; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QTableWidget { border: none; gridline-color: palette(midlight); }
QTableWidget::item { padding: 3px 8px; }
QHeaderView::section { font-size: 10.5px; font-weight: 650; padding: 5px 6px; border: none;
  border-bottom: 1px solid palette(mid); }
QLabel[lattice="metric"] { font-family: monospace; font-size: 17px; font-weight: 650; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid BOX_BORDER;
  border-radius: 3px; background: palette(base); }
QCheckBox::indicator:checked { background: ACCENT; border-color: ACCENT; image: url(CHECK_SVG); }
QCheckBox::indicator:disabled { background: palette(alternate-base); border-color: palette(mid); }
QLabel[lattice="value"] { font-weight: 400; }
QSpinBox::up-button, QSpinBox::down-button { width: 18px; border: none; background: transparent; }
QSpinBox::up-arrow { image: url(ARROW_UP); width: 10px; height: 6px; }
QSpinBox::down-arrow { image: url(ARROW_DOWN); width: 10px; height: 6px; }
QLabel[lattice="hint"][status="bad"] { color: palette(text); }
"""
