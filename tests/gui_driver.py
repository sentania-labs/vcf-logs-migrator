"""Drive the real window through stand-in systems, offscreen. Shared by tests and screenshots.

The driver presses the same buttons and types into the same boxes a person would; it never
calls the engine. `Driver(scenario)` builds its systems from `tests/standin.py`; pass
`fixture=` to use a stock fixture instead.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import standin  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCE = "logs-old.example.com"
DESTINATION = "logs-new.example.com"


class Driver:
    """Runs the Qt event loop in short waits so each step observes the worker's result."""

    def __init__(
        self,
        scenario: str = "plain",
        *,
        fixture: Path | None = None,
        knobs: dict | None = None,
        speed: float = 20000,
        delay: float = 0,
        size: tuple[int, int] = (1280, 800),
        home: Path | None = None,
    ):
        from logs_migrate.gui.app import create

        self.directory = None
        if home is None:
            self.directory = tempfile.TemporaryDirectory(prefix="logs-migrate-gui-")
            home = Path(self.directory.name)
        self.home = home
        os.environ["XDG_CONFIG_HOME"] = str(home / "config")
        os.environ["LOGS_MIGRATE_CONFIG_DIR"] = str(home / "config" / "vcf-logs-migrator")
        self.fixture = home / "standin.fixture"
        if not self.fixture.exists():
            data = (
                json.loads(fixture.read_text())
                if fixture is not None
                else standin.build(scenario, speed=speed, delay=delay)
            )
            data.update(knobs or {})
            self.fixture.write_text(json.dumps(data))
        self.journal = home / "journal.sqlite"
        self.reports = home / "reports"
        self.app, self.window = create(fixture=str(self.fixture), journal=str(self.journal))
        settings = self.window.state.settings
        settings.export_folder = str(self.reports)
        settings.timezone = "America/Chicago"
        settings.refresh_seconds = 2
        settings.count_poll_seconds = 0
        self.window.open_path = lambda path: None  # No file manager in a test.
        self.window.resize(*size)
        self.window.show()
        self.idle()

    # Looking -----------------------------------------------------------------------------

    @property
    def state(self):
        return self.window.state

    @property
    def page(self):
        return self.window.pages[self.window.current_page()]

    def wait(self, milliseconds: int = 50) -> None:
        from PySide6.QtTest import QTest

        QTest.qWait(milliseconds)

    def until(self, predicate, timeout_ms: int = 20000, what: str = "") -> None:
        started = time.monotonic()
        while not predicate():
            self.wait(20)
            if (time.monotonic() - started) * 1000 > timeout_ms:
                raise TimeoutError(f"GUI step did not complete: {what or self.window.active}")

    def idle(self, timeout_ms: int = 20000) -> None:
        """Until no engine work is in flight (a status poll now and then does not count)."""
        self.until(
            lambda: not (set(self.window.active) - {"status"}), timeout_ms, "work still in flight"
        )
        self.wait(40)

    def texts(self) -> str:
        """Every piece of text on the current screen (and an open question), for assertions."""
        from PySide6.QtWidgets import QAbstractButton, QLabel, QLineEdit

        import re

        roots = [self.page]
        if self.window.modal.isVisible():
            roots = [self.window.modal]
        found = []
        for root in roots:
            for widget in root.findChildren(QLabel):
                if widget.isVisibleTo(root) and widget.text():
                    found.append(re.sub(r"<[^>]+>", " ", widget.text()))
            for widget in root.findChildren(QAbstractButton):
                if widget.isVisibleTo(root) and widget.text():
                    found.append(widget.text())
            for widget in root.findChildren(QLineEdit):
                if widget.isVisibleTo(root) and widget.echoMode() == QLineEdit.EchoMode.Normal:
                    found.append(widget.text())
        text = " ".join(found).replace("&nbsp;", " ").replace("&amp;", "&")
        return re.sub(r"\s+", " ", text)

    def buttons(self) -> list[str]:
        """The footer buttons, in order; the main one is last."""
        if self.window.modal.isVisible():
            return list(self.window.modal.buttons)
        return list(self.page.buttons)

    def button(self, text: str):
        from PySide6.QtWidgets import QAbstractButton

        root = self.window.modal if self.window.modal.isVisible() else self.window.frame
        for widget in root.findChildren(QAbstractButton):
            if widget.text() == text and widget.isVisibleTo(root):
                return widget
        raise LookupError(f"No visible button {text!r}; footer has {self.buttons()}")

    # Acting ------------------------------------------------------------------------------

    def click(self, text: str) -> None:
        """Press a visible button by its words, as a person would."""
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        widget = self.button(text)
        if not widget.isEnabled():
            raise LookupError(f"Button {text!r} is disabled")
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
        self.wait(30)

    def link(self, href: str) -> None:
        """Follow a link in the text of the current screen or question."""
        from PySide6.QtWidgets import QLabel

        root = self.window.modal if self.window.modal.isVisible() else self.page
        for widget in root.findChildren(QLabel):
            if widget.isVisibleTo(root) and f'href="{href}"' in widget.text():
                widget.linkActivated.emit(href)
                self.wait(30)
                return
        raise LookupError(f"No visible link {href!r}")

    def step(self, name: str) -> None:
        """Press a step in the list on the left."""
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        QTest.mouseClick(self.window.steps[name], Qt.MouseButton.LeftButton)
        self.wait(40)

    def fill(self, **values: str) -> None:
        """Type into the Connect boxes by attribute name."""
        page = self.window.pages["connect"]
        for name, text in values.items():
            edit = getattr(page, name)
            edit.setFocus()
            edit.clear()
            from PySide6.QtTest import QTest

            QTest.keyClicks(edit, text)
        self.wait(20)

    def sign_in(
        self, *, server: bool = True, password: str = "letmein", confirm: bool = True
    ) -> None:
        """Fill in Connect and press Test connections, answering the identity question."""
        self.step("connect")
        values = {
            "source_host": SOURCE,
            "source_password": password,
            "ssh_password": password,
            "destination_host": DESTINATION,
            "destination_password": password,
        }
        if server:
            values.update(destination_ssh_user="root", destination_ssh_password=password)
        self.fill(**values)
        self.click(self.buttons()[-1])
        self.until(
            lambda: self.window.modal.isVisible() or not self.window.busy("connect"),
            what="identity question",
        )
        if self.window.modal.isVisible() and confirm:
            self.click("These are my systems. Continue.")
            self.idle()

    def copy_rules(self) -> None:
        """Copy every ticked rule, so a copy of logs can be planned."""
        self.step("rules")
        self.idle()
        main = self.buttons()[-1]
        if main.startswith("Copy "):
            self.click(main)
            self.click(self.buttons()[-1])
            self.idle()

    def choose(self, preset: str = "Last 6 months") -> None:
        self.step("dates")
        self.idle()
        self.click(preset)
        self.click("Check these dates")
        self.idle()

    def start(self) -> None:
        if self.page.ack is not None:
            self.page.ack.click()
        self.click("Start copying →")
        self.until(
            lambda: self.window.copy_phase() not in {"preparing", "idle"}, what="copy to start"
        )

    def finish(self, timeout_ms: int = 60000) -> None:
        """Until the copy has stopped running, however it ended."""
        self.until(
            lambda: (
                self.window.copy_phase() in {"done", "missing", "stopped", "failed", "cantcontinue"}
            ),
            timeout_ms,
            "copy to end",
        )
        self.idle()

    def progress(self, fraction: float, timeout_ms: int = 60000) -> None:
        """Until the running copy has passed this fraction."""

        def reached():
            percent = self.window.live_percent()
            return (percent is not None and percent >= fraction * 100) or (
                self.window.copy_phase() != "running"
            )

        self.until(reached, timeout_ms, f"{int(fraction * 100)}%")

    def knob(self, **values) -> None:
        """Change the stand-in systems while they run (their state is a file)."""
        path = self.journal.with_suffix(".simulated-state")
        data = json.loads(path.read_text())
        for key, value in values.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        path.write_text(json.dumps(data))

    def theme(self, name: str) -> None:
        self.state.settings.theme = name
        self.window.apply_settings()
        self.wait(50)

    def resize(self, width: int, height: int) -> None:
        self.window.resize(width, height)
        self.wait(80)

    def overflow(self) -> int:
        """Pixels of the current screen that would need page scrolling. Must be zero."""
        self.wait(30)
        return self.page.overflow()

    def grab(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.wait(60)
        self.window.grab().save(str(path), "PNG", 0)  # 0 = smallest PNG, same pixels

    def close(self) -> None:
        self.window.close()
        self.window.deleteLater()
        from PySide6.QtCore import QCoreApplication, QEvent

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.wait(50)
        if self.directory is not None:
            self.directory.cleanup()
