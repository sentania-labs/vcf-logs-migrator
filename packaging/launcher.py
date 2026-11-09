"""Entry point of the standalone download's CLI, which also opens the window.

Arguments always mean the CLI, exactly as the installed `logs-migrate` console script.
With no arguments, a Finder or `open` launch of the macOS app opens the window instead
of printing usage. A terminal, a script or an agent calling with no arguments gets the
CLI's usage error as before. Linux has no reliable desktop signal, so `logs-migrate gui`
is explicit there; Windows detects a double-click and detaches its own console.
"""

import os
import sys

from logs_migrate.cli import main

NO_GUI = "LOGS_MIGRATE_NO_GUI"
# Packaging smoke only: the window renders, writes a screenshot and a launch report
# next to this path, then exits.
GUI_SMOKE = "LOGS_MIGRATE_GUI_SMOKE"
SSH_SMOKE = "LOGS_MIGRATE_SSH_SMOKE"


def desktop_launch() -> bool:
    if os.environ.get(NO_GUI) or not getattr(sys, "frozen", False):
        return False
    # Finder and `open` start an app through launchd; a shell or script is the parent
    # otherwise. The macOS app runs in-process (one-dir), so there is no bootloader.
    if sys.platform == "win32":
        return detach_owned_console()
    return sys.platform == "darwin" and os.getppid() == 1


def detach_owned_console() -> bool:
    """Hide and detach only a visible console belonging to this frozen program."""
    try:
        import ctypes

        if not sys.stdout.isatty():
            return False
        kernel = ctypes.windll.kernel32
        user = ctypes.windll.user32
        kernel.GetConsoleWindow.restype = ctypes.c_void_p
        user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user.IsWindowVisible.argtypes = [ctypes.c_void_p]
        processes = (ctypes.c_uint * 4)()
        count = kernel.GetConsoleProcessList(processes, len(processes))
        if not 0 < count <= (2 if getattr(sys, "frozen", False) else 1):
            return False
        window = kernel.GetConsoleWindow()
        if not window or not user.IsWindowVisible(window):
            return False
        user.ShowWindow(window, 0)
        kernel.FreeConsole()
        sys.stdout = sys.stderr = open(os.devnull, "w")
        return True
    except (AttributeError, OSError):
        return False


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


def _ssh_smoke(report: str) -> int:
    """Prove the frozen SSH dependency and crypto backend work without lab access."""
    import json
    import socket
    from pathlib import Path

    import paramiko

    from logs_migrate.adapters.ssh_errors import SSHFailure
    from logs_migrate.errors import Code
    from logs_migrate.trust import probe_ssh

    # Exercise the bundled crypto extension, then the real SSH identity-probe path.
    paramiko.RSAKey.generate(2048)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        # Keep the port reserved without listening so no other process can take it.
        try:
            probe_ssh("127.0.0.1", port, timeout=3)
        except SSHFailure as error:
            status = {
                "paramiko_version": paramiko.__version__,
                "code": error.code.value,
                "message": error.message,
            }
            Path(report).write_text(json.dumps(status), encoding="utf-8")
            return (
                0
                if (
                    error.code == Code.CONNECTION and error.message.startswith("SSH TCP connection")
                )
                else 1
            )
    return 1


def run(argv=None) -> int:
    if os.environ.get(SSH_SMOKE):
        return _ssh_smoke(os.environ[SSH_SMOKE])
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv and desktop_launch():
        argv = ["gui"]
    if os.environ.get(GUI_SMOKE):
        _install_gui_smoke(os.environ[GUI_SMOKE])
    return main(argv)


if __name__ == "__main__":
    sys.exit(run())
