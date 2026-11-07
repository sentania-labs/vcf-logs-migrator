"""Entry point of the standalone download's CLI, which also opens the window.

Arguments always mean the CLI, exactly as the installed `logs-migrate` console script.
With no arguments, a Finder or `open` launch of the macOS app opens the window instead
of printing usage. A terminal, a script or an agent calling with no arguments gets the
CLI's usage error as before. Linux has no reliable desktop signal, so `logs-migrate gui`
is explicit there; Windows double-clicks use the separate windowed gui_entry.py program.
"""

import os
import sys

from logs_migrate.cli import main

NO_GUI = "LOGS_MIGRATE_NO_GUI"
# Packaging smoke only: the window renders, writes a screenshot and a launch report
# next to this path, then exits.
GUI_SMOKE = "LOGS_MIGRATE_GUI_SMOKE"


def desktop_launch() -> bool:
    if os.environ.get(NO_GUI) or not getattr(sys, "frozen", False):
        return False
    # Finder and `open` start an app through launchd; a shell or script is the parent
    # otherwise. The macOS app runs in-process (one-dir), so there is no bootloader.
    return sys.platform == "darwin" and os.getppid() == 1


def _install_gui_smoke(report: str) -> None:
    """Exit the real `gui` path after the first rendered frame, keeping evidence."""
    import json
    from pathlib import Path

    from logs_migrate.gui import app as gui_app

    create = gui_app.create

    def create_and_exit(*args, **kwargs):
        from PySide6.QtCore import QTimer

        application, window = create(*args, **kwargs)

        def record() -> None:
            image = window.grab()
            saved = not image.isNull() and image.save(report)
            evidence = {
                "platform": application.platformName(),
                "visible": window.isVisible(),
                "width": window.width(),
                "height": window.height(),
                "saved": saved,
            }
            if sys.platform == "win32":
                import ctypes

                kernel = ctypes.windll.kernel32
                kernel.GetConsoleWindow.restype = ctypes.c_void_p
                evidence["console_attached"] = bool(kernel.GetConsoleWindow())
            Path(report).with_suffix(".json").write_text(json.dumps(evidence), encoding="utf-8")
            # Close as a user would, so the engine worker thread stops before exit.
            window.close()
            application.exit(0 if saved and evidence["visible"] else 1)

        QTimer.singleShot(1500, record)
        return application, window

    gui_app.create = create_and_exit


def run(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv and desktop_launch():
        argv = ["gui"]
    if os.environ.get(GUI_SMOKE):
        _install_gui_smoke(os.environ[GUI_SMOKE])
    return main(argv)


if __name__ == "__main__":
    sys.exit(run())
