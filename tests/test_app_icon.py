"""The app icon: shown by the window, and renderable into the packagers' .ico."""

import importlib.util
import struct
from pathlib import Path

import pytest

pytest.importorskip("PySide6")


def test_window_carries_the_app_icon():
    from logs_migrate.gui import app

    application, window = app.create(argv=["test"])
    assert not application.windowIcon().isNull()
    assert not window.windowIcon().isNull()
    image = application.windowIcon().pixmap(64, 64).toImage()
    assert not image.isNull()
    assert image.pixelColor(0, 0).alpha() == 0
    assert any(
        image.pixelColor(x, y).alpha() > 0 for x in range(image.width()) for y in range(image.height())
    )
    window.close()


def test_missing_icon_fails_loudly(tmp_path, monkeypatch):
    from logs_migrate.gui import app

    monkeypatch.setattr(app, "ICON_SVG", tmp_path / "missing.svg")
    with pytest.raises(RuntimeError):
        app.app_icon()


def test_ico_holds_every_size(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "packaging_icons", Path(__file__).resolve().parents[1] / "packaging" / "icons.py"
    )
    icons = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(icons)
    data = icons.ico(tmp_path / "a.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind, count) == (0, 1, len(icons.ICO_SIZES))
    widths = [data[6 + 16 * i] or 256 for i in range(count)]
    assert widths == list(icons.ICO_SIZES)
    assert data[6 + 16 * count :].startswith(b"\x89PNG")
