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
        """Let the window run for a while. The engine thread needs the interpreter too, and
        one long Qt wait would hold it, so the wait is taken in short slices."""
        from PySide6.QtCore import QCoreApplication, QEvent

        deadline = time.monotonic() + milliseconds / 1000
        while True:
            QCoreApplication.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if time.monotonic() >= deadline:
                return
            time.sleep(0.004)

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
        import re

        from PySide6.QtWidgets import QAbstractButton, QLabel, QLineEdit

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

        self.wait(30)  # Let a screen that was just redrawn show its new buttons.
        widget = self.button(text)
        if not widget.isEnabled():
            raise LookupError(f"Button {text!r} is disabled")
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
        self.wait(30)

    def link(self, href: str) -> None:
        """Follow a link in the text of the current screen or question."""
        from PySide6.QtWidgets import QLabel

        self.wait(30)
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
            self.confirm()

    def asked(self) -> bool:
        """Wait for the test to ask something or to finish; True when it asked."""
        self.until(
            lambda: self.window.modal.isVisible() or not self.window.busy("connect"),
            what="a question or the verdict",
        )
        return self.window.modal.isVisible()

    def confirm(self) -> None:
        """Agree to the first-time question, and to the one that follows it when signing
        in finds more servers behind an address."""
        for _ in range(3):
            if not self.asked():
                break
            self.click(self.buttons()[-1])
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

    def clipped(self) -> list[str]:
        """Words on the current screen (or the open question) that are cut off: a label,
        button or tick box with less room than its words need, or lying partly outside
        the area that shows it. Must be empty. Lists that scroll by nature are left out.
        A figure that has just changed is given a moment to be laid out again: only what
        is still cut off after it counts."""
        found = self._cut()
        if found:
            self.wait(150)
            still = self._cut()
            found = {key: words for key, words in still.items() if key in found}
        return list(found.values())

    def _cut(self) -> dict[int, str]:
        from PySide6.QtWidgets import QAbstractButton, QLabel, QWidget

        from logs_migrate.gui.widgets import ScrollBox

        self.wait(30)
        root = self.window.modal if self.window.modal.isVisible() else self.window
        found = {}
        for widget in root.findChildren(QWidget):
            if not isinstance(widget, (QLabel, QAbstractButton)):
                continue
            if not widget.isVisibleTo(root) or not widget.text():
                continue
            parent, listed = widget.parentWidget(), False
            while parent is not None and not listed:
                listed = isinstance(parent, ScrollBox)
                parent = parent.parentWidget()
            if listed:
                continue
            if isinstance(widget, QLabel) and widget.wordWrap():
                short = widget.height() < widget.heightForWidth(widget.width()) - 2
            elif getattr(widget, "shortens", False):
                # Shortened on purpose, with the whole of it in the tooltip: what it shows
                # now must still fit, and the tooltip must hold what was left out.
                short = widget.width() < QLabel.sizeHint(widget).width() - 2 or (
                    widget.shortened() and widget.toolTip() != widget.full
                )
            else:
                short = widget.width() < widget.sizeHint().width() - 2
            shown = widget.visibleRegion().boundingRect()
            cut = shown.width() < widget.width() - 2 or shown.height() < widget.height() - 2
            if short or cut:
                words = " ".join(widget.text().split())[:60]
                found[id(widget)] = f"{words!r} ({'too little room' if short else 'partly hidden'})"
        return found

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
