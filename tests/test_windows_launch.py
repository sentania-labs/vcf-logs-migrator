"""Windows console ownership must not disturb an existing terminal."""

import ctypes
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "launcher", Path(__file__).resolve().parents[1] / "packaging" / "launcher.py"
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.mark.parametrize(
    "count,window,tty,expected",
    [
        (2, 0x123456789, True, True),
        (3, 0x123456789, True, False),
        (0, 0x123456789, True, False),
        (2, 0x123456789, False, False),
        (2, 0, True, False),
    ],
)
def test_console_ownership(monkeypatch, count, window, tty, expected):
    kernel = SimpleNamespace(
        GetConsoleWindow=Mock(return_value=window),
        GetConsoleProcessList=Mock(return_value=count),
        FreeConsole=Mock(),
    )
    user = SimpleNamespace(ShowWindow=Mock(), IsWindowVisible=Mock(return_value=False))
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=kernel, user32=user), raising=False
    )
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "stdout", SimpleNamespace(isatty=lambda: tty))
    monkeypatch.setattr(launcher.sys, "stderr", sys.stderr)
    assert launcher.detach_owned_console() is expected
    assert user.ShowWindow.called is expected
    assert kernel.FreeConsole.called is expected
    if expected:
        assert kernel.GetConsoleWindow.restype == ctypes.c_void_p
        user.ShowWindow.assert_called_once_with(0x123456789, 0)
        assert launcher.sys.stdout.name == os.devnull
        assert launcher.sys.stderr is launcher.sys.stdout
        launcher.sys.stdout.write("discarded")


def test_arguments_never_trigger_desktop_detection(monkeypatch):
    desktop = Mock()
    main = Mock(return_value=7)
    monkeypatch.setattr(launcher, "desktop_launch", desktop)
    monkeypatch.setattr(launcher, "main", main)
    assert launcher.run(["--version"]) == 7
    desktop.assert_not_called()
    main.assert_called_once_with(["--version"])
