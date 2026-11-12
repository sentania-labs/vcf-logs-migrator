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
        "line-soft": "#eef1f5",
        "ink": "#1d2430",
        "ink-muted": "#5a6676",
        "ink-subtle": "#6c7788",
        "accent": "#1f5fbf",
        "accent-soft": "#e8f0fc",
        "accent-ink": "#12376f",
        "on-accent": "#ffffff",
        "ok": "#1a7f4b",
        "ok-soft": "#e6f4ec",
        "ok-ink": "#14532d",
        "warn": "#8a6100",
        "warn-soft": "#fdf3d8",
        "warn-ink": "#8a6100",
        "bad": "#a22c1c",
        "bad-soft": "#fbe9e6",
        "bad-ink": "#7f1d1d",
    },
    # The dark soft tints are Lattice's translucent tints laid over its dark surface.
    "dark": {
        "bg": "#1b1f24",
        "surface": "#23282f",
        "surface-sunken": "#2a3038",
        "line": "#363d47",
        "line-soft": "#2a3038",
        "ink": "#f2f4f7",
        "ink-muted": "#b7bfca",
        "ink-subtle": "#868f9c",
        "accent": "#3987e5",
        "accent-soft": "#2b3a4f",
        "accent-ink": "#8fbcf5",
        "on-accent": "#0b1220",
        "ok": "#199e70",
        "ok-soft": "#213b39",
        "ok-ink": "#67c9a2",
        "warn": "#e0a400",
        "warn-soft": "#34332b",
        "warn-ink": "#e0a400",
        "bad": "#d95926",
        "bad-soft": "#392e2e",
        "bad-ink": "#ea8c64",
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
        )
    # Longest names first, so `@accent-soft` is not read as `@accent` plus text.
    for name in sorted(TOKENS[theme], key=len, reverse=True):
        extra = extra.replace("@" + name, TOKENS[theme][name])
    app.setStyleSheet(_asset("lattice.qss" if theme == "light" else "lattice-dark.qss") + extra)


# Rules the generated Lattice sheet does not carry: the window frame, the step list and the
# few components this app adds. Every colour is a Lattice token (`@name`).
EXTRA = """
QFrame[lattice="header"], QFrame[lattice="rail"], QFrame[lattice="footer"] { border-radius: 0px; }
QFrame[lattice="header"] { border: none; border-bottom: 1px solid @line; }
QFrame[lattice="rail"] { background: @surface; border: none; border-right: 1px solid @line; }
QFrame[lattice="footer"] { background: @surface; border: none; border-top: 1px solid @line; }
QLabel[lattice="app-name"] { font-size: 16px; font-weight: 650; }
QLabel[lattice="page-title"] { font-size: 19px; font-weight: 650; }
QLabel[lattice="page-sub"] { font-size: 13.5px; color: @ink-muted; }
QLabel[lattice="rail-cap"] { font-size: 11px; font-weight: 650; color: @ink-subtle; }
QLabel[lattice="card-title"] { font-size: 12px; font-weight: 650; color: @ink-subtle; }
QLabel[lattice="card-note"] { font-size: 13px; color: @ink-muted; }
QLabel[lattice="big"] { font-size: 17px; font-weight: 650; }
QLabel[lattice="huge"] { font-size: 26px; font-weight: 650; }
QLabel[lattice="verdict-title"] { font-size: 20px; font-weight: 650; }
QLabel[lattice="text"] { font-size: 13.5px; }
QLabel[lattice="soft"] { font-size: 13.5px; color: @ink-muted; }
QLabel[lattice="subtle"] { font-size: 12px; color: @ink-subtle; }
QLabel[lattice="mono"] { font-family: monospace; font-size: 11.5px; }
QLabel[lattice="path"] { font-family: monospace; font-size: 12px; background: @surface-sunken;
  padding: 4px 8px; border-radius: 4px; }
QLabel[lattice="saved"] { font-size: 12.5px; color: @ok-ink; }
QLabel[lattice="th"] { font-size: 10.5px; font-weight: 500; color: @ink-subtle;
  padding: 4px 8px; border-bottom: 1px solid @line; }
QLabel[lattice="td"] { font-size: 13px; color: @ink-muted; padding: 5px 8px;
  border-bottom: 1px solid @line-soft; }
QLabel[lattice="td"][lead="true"] { color: @ink; }
QLabel[lattice="td"][num="true"] { font-family: monospace; color: @ink; }
QWidget[lattice="td"] { border-bottom: 1px solid @line-soft; }
QLabel[lattice="chip"] { background: @surface-sunken; color: @ink-muted; border-radius: 10px;
  padding: 2px 10px; font-size: 12.5px; }
QLabel[lattice="chip"][status="ok"] { background: @ok-soft; color: @ok-ink; }
QLabel[lattice="chip"][status="warn"] { background: @warn-soft; color: @warn-ink; }
QLabel[lattice="chip"][status="bad"] { background: @bad-soft; color: @bad-ink; }
QLabel[lattice="badge"] { font-size: 10.5px; font-weight: 500; border-radius: 3px;
  padding: 1px 6px; background: @surface-sunken; color: @ink-muted; }
QLabel[lattice="badge"][status="ok"] { background: @ok-soft; color: @ok-ink; }
QLabel[lattice="badge"][status="warn"] { background: @warn-soft; color: @warn-ink; }
QLabel[lattice="badge"][status="bad"] { background: @bad-soft; color: @bad-ink; }
QLabel[lattice="badge"][status="accent"] { background: @accent-soft; color: @accent-ink; }
QLabel[lattice="banner"] { padding: 9px 12px; font-size: 13px; border-left: 3px solid @line; }
QLabel[lattice="banner"][status="info"] { background: @accent-soft; color: @accent-ink;
  border-left: 3px solid @accent; }
QLabel[lattice="banner"][status="bad"] { background: @bad-soft; color: @bad-ink;
  border-left: 3px solid @bad; }
QLabel[lattice="mark"] { color: #ffffff; font-size: 24px; font-weight: 700;
  border-radius: 22px; background: @accent; }
QLabel[lattice="mark"][status="ok"] { background: @ok; }
QLabel[lattice="mark"][status="warn"] { background: @warn; }
QLabel[lattice="mark"][status="bad"] { background: @bad; }
QLabel[lattice="icon"] { font-weight: 700; font-size: 13.5px; }
QLabel[lattice="icon"][status="ok"] { color: @ok; }
QLabel[lattice="icon"][status="warn"] { color: @warn; }
QLabel[lattice="icon"][status="bad"] { color: @bad; }
QLabel[lattice="key"] { font-size: 12.5px; color: @ink-subtle; }
QFrame[lattice="fact"] { background: @surface-sunken; border-radius: 6px; }
QLabel[lattice="fact-key"] { font-size: 11.5px; color: @ink-subtle; }
QLabel[lattice="fact-value"] { font-size: 15px; font-weight: 650; }
QFrame[lattice="activity"] { background: @accent-soft; border-radius: 9px; }
QFrame[lattice="activity"][status="bad"] { background: @bad-soft; }
QLabel[lattice="stage"] { font-size: 13.5px; color: @ink-subtle; }
QLabel[lattice="stage"][status="done"] { color: @ok-ink; }
QLabel[lattice="stage"][status="now"] { color: @ink; font-weight: 600; }
QLabel[lattice="stage"][status="bad"] { color: @bad-ink; font-weight: 600; }
QFrame[lattice="card"][edge="bad"] { border-left: 3px solid @bad; }
QFrame[lattice="card"][edge="warn"] { border-left: 3px solid @warn; }
QFrame[lattice="card"][edge="info"] { border-left: 3px solid @accent; }
QFrame[lattice="sunken"] { background: @surface-sunken; border-radius: 6px; }
QFrame[lattice="rule"] { background: @line-soft; max-height: 1px; min-height: 1px; border: none; }
QFrame[lattice="more"] { background: @accent-soft; border-radius: 6px; }
QPushButton[lattice="step"] { text-align: left; border: none; border-left: 3px solid transparent;
  border-radius: 0px; background: transparent; padding: 0px; }
QPushButton[lattice="step"]:hover { background: @surface-sunken; }
QPushButton[lattice="step"][current="true"] { background: @accent-soft;
  border-left: 3px solid @accent; }
QPushButton[lattice="step"][locked="true"]:hover { background: transparent; }
QPushButton[lattice="step"]:focus { border-left: 3px solid @accent; }
QLabel[lattice="step-name"] { font-size: 13.5px; color: @ink-muted; }
QLabel[lattice="step-name"][current="true"] { color: @accent-ink; font-weight: 600; }
QLabel[lattice="step-name"][locked="true"] { color: @ink-subtle; }
QLabel[lattice="step-sub"] { font-size: 11px; color: @ink-subtle; }
QLabel[lattice="step-sub"][live="true"] { color: @accent-ink; font-weight: 650; }
QLabel[lattice="step-n"] { font-size: 11px; border: 1px solid @ink-subtle; border-radius: 10px;
  color: @ink-muted; }
QLabel[lattice="step-n"][mark="on"] { border-color: @accent; color: @accent-ink; }
QLabel[lattice="step-n"][mark="done"] { background: @ok; border-color: @ok; color: #ffffff; }
QLabel[lattice="step-n"][mark="warn"] { background: @warn; border-color: @warn; color: #ffffff; }
QLabel[lattice="step-n"][mark="runs"] { border-style: dashed; }
QPushButton[seg="on"] { background: @accent-soft; border-color: @accent; color: @accent-ink;
  font-weight: 600; }
QPushButton[lattice="foot"] { padding: 8px 16px; font-size: 13.5px; }
QPushButton[lattice="link"] { border: none; background: transparent; color: @accent;
  padding: 0px; text-decoration: underline; font-size: 12.5px; text-align: left; }
QCheckBox[lattice="ack"] { background: @warn-soft; padding: 7px 10px; border-radius: 6px;
  font-size: 13px; }
QLabel[lattice="toast"] { background: @ink; color: @surface; padding: 7px 14px;
  border-radius: 6px; font-size: 13px; }
QFrame[lattice="modal"] { background: @surface; border: 1px solid @line; border-radius: 9px; }
QLabel[lattice="modal-title"] { font-size: 17px; font-weight: 650; }
QProgressBar { background: @surface-sunken; border: none; border-radius: 4px; }
QProgressBar::chunk { background: @accent; border-radius: 4px; }
QProgressBar[still="true"]::chunk { background: @ink-subtle; }
QFrame[lattice="activity"] QProgressBar { background: @surface; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid @ink-subtle;
  border-radius: 3px; background: @surface; }
QCheckBox::indicator:checked { background: @accent; border-color: @accent;
  image: url(CHECK_SVG); }
QCheckBox::indicator:disabled { background: @surface-sunken; border-color: @line; }
QCheckBox::indicator:checked:disabled { background: @ink-subtle; border-color: @ink-subtle; }
QSpinBox::up-button, QSpinBox::down-button { width: 18px; border: none; background: transparent; }
QSpinBox::up-arrow { image: url(ARROW_UP); width: 10px; height: 6px; }
QSpinBox::down-arrow { image: url(ARROW_DOWN); width: 10px; height: 6px; }
QLineEdit, QDateEdit, QComboBox { border: 1px solid @ink-subtle; }
QComboBox::drop-down, QDateEdit::drop-down { border: none; width: 22px; }
QComboBox::down-arrow, QDateEdit::down-arrow { image: url(ARROW_DOWN); width: 10px; height: 6px; }
QLabel[lattice="hint"][status="bad"] { color: @bad-ink; }
QLineEdit[status="bad"], QComboBox[status="bad"] { border: 1px solid @bad; background: @bad-soft; }
"""
