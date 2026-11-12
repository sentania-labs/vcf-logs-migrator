"""The window: header, the five steps down the left, one screen at a time.

Engine work runs on the worker thread; results land here, update AppState, and the screens
redraw from it. Nothing on a screen is a migration decision.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import copies, copyview, theme
from . import viewmodel as vm
from .pages.check import CheckPage
from .pages.connect import ConnectPage
from .pages.copy import CopyPage
from .pages.dates import DatesPage
from .pages.rules import RulesPage
from .pages.runs import RunsPage
from .pages.settings_page import SettingsPage
from .settings import Settings
from .state import AppState
from .widgets import GridTable, Overlay, button, label, rich, set_prop
from .worker import EngineWorker

STAGES = {
    "connect": (
        "Finding both systems and confirming who they are",
        "Testing the source sign-in, on the system and on each of its servers",
        "Testing the destination sign-in",
        "Checking the versions",
    ),
    "compare": (
        "Reading the rules on the source",
        "Reading the rules on the destination",
        "Comparing them",
    ),
    "apply": ("Adding the rules to the destination", "Reading them back to confirm"),
    "plan": (
        "Listing the stored logs on the source",
        "Measuring how big they are",
        "Checking free space on the destination",
        "Checking what is already on the destination",
        "Building the plan",
    ),
    "discover": ("Listing the stored logs on the source", "Measuring how big they are"),
    "start": (
        "Checking the plan and free space once more",
        "Counting the logs on the source, so the result can be checked later",
        "Counting what is already on the destination",
        "Starting the copy",
    ),
    "continue": (
        "Checking every destination server is up",
        "Checking what already arrived",
        "Checking free space and counting what is left",
        "Starting the copy",
    ),
    "stop": (
        "Asking the destination to stop",
        "Confirming it has stopped",
        "Checking what arrived",
    ),
    "verify": (
        "Checking that the copy has ended",
        "Counting the logs on the destination, day by day",
        "Counting a second time to be sure",
        "Comparing with the counts taken on the source before the copy",
    ),
}
NOTES = {
    "plan": "On a big source the first two steps can take several minutes. The moving bar "
    "means the app is still working.",
    "start": "This can take several minutes on a large source. Keep this window open until "
    "the copy has started.",
    "verify": "Usually a few minutes. Counting twice makes sure the destination has finished "
    "sorting what arrived.",
}
# Work that only reads can be abandoned at once. Work that asks a system to change
# something is asked to stop before the request goes out, and says so if it was too late.
READ_ONLY = {"connect", "compare", "plan", "discover", "verify"}
ORDER = ("connect", "rules", "dates", "copy", "check", "runs", "settings")


class StepButton(QPushButton):
    """One line of the step list: a numbered mark, the step, and one short line under it."""

    def __init__(self, key: str, number: str, name: str):
        super().__init__()
        self.key = key
        self.number = number
        self.name = name
        self.setProperty("lattice", "step")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(46)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 6, 5)
        layout.setSpacing(9)
        self.mark = label(number, "step-n")
        self.mark.setFixedSize(20, 20)
        self.mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        words = QVBoxLayout()
        words.setSpacing(0)
        self.title = label(name, "step-name")
        self.sub = label("", "step-sub")
        words.addWidget(self.title)
        words.addWidget(self.sub)
        layout.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(words, 1)
        for child in (self.mark, self.title, self.sub):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            child.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def show_state(self, *, current: bool, locked: bool, mark: str, sub: str, live: bool) -> None:
        flag = "true" if current else "false"
        lock = "true" if locked else "false"
        set_prop(self, "current", flag)
        set_prop(self, "locked", lock)
        set_prop(self.title, "current", flag)
        set_prop(self.title, "locked", lock)
        set_prop(self.sub, "live", "true" if live else "false")
        self.mark.setText({"done": "✓", "warn": "!"}.get(mark, self.number))
        set_prop(
            self.mark, "mark", mark or ("on" if current else "runs" if self.key == "runs" else "")
        )
        self.sub.setText(sub)
        self.setCursor(Qt.CursorShape.ArrowCursor if locked else Qt.CursorShape.PointingHandCursor)
        state = "not available yet: " + sub.lower() if locked else sub
        self.setAccessibleName(f"{self.name}, {state}" if state else self.name)
        self.setToolTip(sub if locked else "")


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.setWindowTitle("VCF Logs Migrator")
        about = self.menuBar().addMenu("About")
        about.addAction("About VCF Logs Migrator", self.show_about)
        about.addAction("Licences", self.show_licenses)
        self.state = AppState(settings)
        self.worker = EngineWorker()
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.progress.connect(self._progress)
        self.worker.start()
        self.serial = 0
        self.active: dict[str, int] = {}  # Job name to the serial whose result is wanted.
        self.owners: dict[str, str] = {}  # Job name to the screen showing its progress.
        self.context: dict[str, dict] = {}
        self.pending_credentials: dict[str, str] = {}
        self.toasts: list[str] = []
        self.opened: list[str] = []
        # Tests replace these to answer without a real file manager or folder chooser.
        self.open_path = self._open_path
        self.choose_folder = self._choose_folder
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_status)
        self.focus_timer = QTimer(self)
        self.focus_timer.setSingleShot(True)
        self.focus_timer.timeout.connect(self._keep_focus)
        self.return_page = "connect"

        root = QWidget()
        root.setObjectName("lattice-root")
        self.setCentralWidget(root)
        self.frame = QWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)
        layout = QVBoxLayout(self.frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._header())
        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        middle.addWidget(self._rail())
        self.stack = QStackedWidget()
        middle.addWidget(self.stack, 1)
        layout.addLayout(middle, 1)
        self.modal = Overlay(root)
        self.modal.closed.connect(lambda: self.frame.setEnabled(True))
        self.toast_label = label("", "toast")
        self.toast_label.setParent(root)
        self.toast_label.setAccessibleName("Notice")
        self.toast_label.hide()
        self.toast_timer = QTimer(self)
        self.toast_timer.setSingleShot(True)
        self.toast_timer.timeout.connect(self.toast_label.hide)

        self.pages = {
            "connect": ConnectPage(self),
            "rules": RulesPage(self),
            "dates": DatesPage(self),
            "copy": CopyPage(self),
            "check": CheckPage(self),
            "runs": RunsPage(self),
            "settings": SettingsPage(self),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        for number, key in enumerate(ORDER, start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{number}"), self)
            shortcut.activated.connect(lambda name=key: self.go(name))
        self.resize(1440, 900)
        self.setMinimumSize(900, 600)
        self.submit("record", "open_record", journal=settings.journal_path)
        self.show_page("connect")

    # Frame -------------------------------------------------------------------------------

    def _header(self) -> QFrame:
        header = QFrame()
        header.setProperty("lattice", "header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 9, 16, 9)
        layout.setSpacing(8)
        layout.addWidget(label("VCF Logs Migrator", "app-name"))
        layout.addSpacing(8)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(8)
        layout.addLayout(self.chips)
        layout.addStretch(1)
        self.settings_button = button("Settings", "", lambda: self.go("settings"))
        layout.addWidget(self.settings_button)
        return header

    def _rail(self) -> QFrame:
        self.rail = QFrame()
        self.rail.setProperty("lattice", "rail")
        self.rail.setFixedWidth(200)
        layout = QVBoxLayout(self.rail)
        layout.setContentsMargins(0, 14, 10, 14)
        layout.setSpacing(2)
        cap = label("MOVE YOUR LOGS", "rail-cap")
        cap.setContentsMargins(18, 0, 0, 6)
        layout.addWidget(cap)
        self.steps: dict[str, StepButton] = {}
        for number, (key, name, _sub) in enumerate(vm.STEPS, start=1):
            step = StepButton(key, str(number), name)
            step.clicked.connect(lambda _checked=False, name=key: self.go(name))
            self.steps[key] = step
            layout.addWidget(step)
        line = QFrame()
        line.setProperty("lattice", "rule")
        holder = QVBoxLayout()
        holder.setContentsMargins(14, 8, 4, 8)
        holder.addWidget(line)
        layout.addLayout(holder)
        runs = StepButton("runs", "≡", "Past runs")
        runs.clicked.connect(lambda _checked=False: self.go("runs"))
        self.steps["runs"] = runs
        layout.addWidget(runs)
        layout.addStretch(1)
        self.rail.setAccessibleName("Steps")
        return self.rail

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self.rail.setFixedWidth(168 if self.width() < 1100 else 200)
        if self.modal.isVisible():
            self.modal.setGeometry(self.centralWidget().rect())
        self._place_toast()

    def current_page(self) -> str:
        for name, page in self.pages.items():
            if page is self.stack.currentWidget():
                return name
        return "connect"

    # Steps -------------------------------------------------------------------------------

    def running_copy(self) -> dict | None:
        """A copy the record shows as still in flight, whether or not it was started here."""
        return next(
            (c for c in reversed(self.state.copies) if c["state"] in {"running", "unsure"}), None
        )

    def copy_phase(self) -> str:
        """Where the current copy stands: the one word the Copy logs screen is drawn from."""
        state = self.state
        for job in ("start", "continue", "stop"):
            if self.busy(job):
                return {"start": "preparing", "continue": "continuing", "stop": "stopping"}[job]
        if state.start_failure:
            return "notstarted"
        copy = state.copy()
        if copy is None:
            return "idle"
        if state.continue_failure:
            return "cantcontinue"
        if state.lost_at:
            return "lost"
        status = state.status
        if state.unsure or copy["state"] == "unsure":
            if not (status and status["status"] == "running"):
                return "unsure"
        if status and status["status"] == "running":
            return "running"
        if copy["state"] == "running":
            return "running" if status is None else "settling"
        return {
            "finished": "done",
            "missing": "missing",
            "stopped": "stopped",
            "between": "stopped",
            "unsettled": "unsettled",
            "failed": "failed",
        }.get(copy["state"], "idle")

    def unlocked(self, step: str) -> bool:
        state = self.state
        if step in {"connect", "runs", "settings"}:
            return True
        if not state.connected:
            return False
        if step in {"rules", "dates"}:
            return True
        phase = self.copy_phase()
        if step == "copy":
            return phase != "idle" or self.running_copy() is not None
        return self.busy("verify") or phase in {
            "done",
            "missing",
            "stopped",
            "failed",
            "unsettled",
            "cantcontinue",
        }

    def step_mark(self, step: str) -> str:
        state = self.state
        phase = self.copy_phase()
        if step == "connect":
            return "done" if state.connected else "warn" if state.refused or state.failure else ""
        if step == "rules":
            if not state.connected:
                return ""
            where = vm.rules_state(state.comparison, state.config_result)
            if where in {"none", "done"}:
                return "done"
            return "warn" if where == "partial" or state.comparison_error else ""
        if step == "dates":
            if not state.connected:
                return ""
            if phase not in {"idle", "notstarted"}:
                return "done"
            return "warn" if state.plan_error or state.inventory_error else ""
        if step == "copy":
            if phase == "done":
                return "done"
            quiet = phase == "running" and self.quiet_minutes() >= copyview.QUIET_MINUTES
            return (
                "warn"
                if quiet
                or phase
                in {
                    "missing",
                    "stopped",
                    "failed",
                    "unsettled",
                    "lost",
                    "notstarted",
                    "cantcontinue",
                    "unsure",
                }
                else ""
            )
        if step == "check":
            copy = state.copy()
            if copy is None or not state.connected:
                return ""
            result = copies.check(copy)
            if state.verify_error or result["phase"] in {"diff", "incomplete", "partial"}:
                return "warn"
            return "done" if result["phase"] == "ok" else ""
        return ""

    def update_frame(self) -> None:
        state = self.state
        current = self.current_page()
        for key, _name, sub in vm.STEPS:
            locked = not self.unlocked(key)
            live = False
            text = vm.LOCK_REASON[key] if locked else sub
            if key == "copy" and current != "copy" and not locked:
                percent = self.live_percent()
                if percent is not None:
                    text, live = f"Running now · {percent}%", True
            self.steps[key].show_state(
                current=current == key,
                locked=locked,
                mark=self.step_mark(key),
                sub=text,
                live=live,
            )
        self.steps["runs"].show_state(
            current=current == "runs", locked=False, mark="", sub="Results, reports", live=False
        )
        set_prop(self.settings_button, "seg", "on" if current == "settings" else "off")
        while self.chips.count():
            item = self.chips.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        frame = {
            "connected": state.connected,
            "refused": state.refused,
            "failure": state.failure,
            "source": (state.discovered or {}).get("source"),
            "destination": (state.discovered or {}).get("destination"),
            "out_of_touch": vm.format_time(state.lost_at, state.settings.timezone)
            if state.lost_at
            else None,
        }
        chips = vm.header_chips(frame)
        for index, item in enumerate(chips):
            if index == 1 and (state.connected or state.refused):
                self.chips.addWidget(label("→", "subtle"))
            widget = label(item["text"], "chip", item["kind"])
            self.chips.addWidget(widget)
        self.chip_texts = [item["text"] for item in chips]

    def live_percent(self) -> int | None:
        """Percent of the copy that is running now, for the step list on other screens."""
        state = self.state
        copy = state.copy() or self.running_copy()
        if copy is None or not copy["total_bytes"]:
            return None
        status = state.status if state.copy() is copy else None
        if status and status["status"] == "running" and not state.lost_at:
            done = copy["done_bytes"] + (status.get("bytes_transferred") or 0)
            return min(100, int(100 * done / copy["total_bytes"]))
        if copy["state"] == "running" and status is None:
            return int(100 * copy["done_bytes"] / copy["total_bytes"])
        return None

    def quiet_minutes(self) -> float:
        state = self.state
        status = state.status
        if not status or status["status"] != "running" or state.quiet_since is None:
            return 0.0
        return max(0.0, ((status.get("elapsed_seconds") or 0) - state.quiet_since) / 60)

    def go(self, step: str) -> None:
        """Move to a step if it is open. A locked step says why and stays put."""
        if not self.unlocked(step):
            return
        state = self.state
        if step == "copy" and state.copy() is None and not state.start_failure:
            running = self.running_copy()
            if running is not None:
                self.adopt(running)
        if step == "runs":
            state.open_run = None
        self.show_page(step)

    def show_page(self, name: str) -> None:
        current = self.current_page()
        if name == "settings" and current != "settings":
            self.return_page = current
        page = self.pages[name]
        self.stack.setCurrentWidget(page)
        if hasattr(page, "arrive"):
            page.arrive()
        self.refresh_all()
        if self.isVisible():
            page.focus_primary()

    def refresh_all(self) -> None:
        self.update_frame()
        self.pages[self.current_page()].refresh()
        self.focus_timer.start(0)

    def _keep_focus(self) -> None:
        """A screen that was redrawn may have lost the button that had the keyboard; give
        it to the new main action instead of leaving the keyboard nowhere."""
        if self.modal.isVisible() or not self.isVisible():
            return
        focus = QApplication.focusWidget()
        if focus is None or not focus.isVisible() or not self.isAncestorOf(focus):
            self.pages[self.current_page()].focus_primary()

    def leave_settings(self) -> None:
        target = self.return_page if self.return_page != "settings" else "connect"
        self.show_page(target if self.unlocked(target) else "connect")

    # Dialogs, notices, files -------------------------------------------------------------

    def ask(self, title: str, body, buttons) -> None:
        """A question inside the window. `buttons` are `(text, slot[, variant])`; each
        closes the question first. The first button also answers Escape."""
        widgets = [rich(item, "text") if isinstance(item, str) else item for item in body]

        def closing(slot):
            def run():
                self.modal.close_box()
                if slot is not None:
                    slot()

            return run

        self.frame.setEnabled(False)
        self.modal.open(title, widgets, [(b[0], closing(b[1]), *(b[2:] or ("",))) for b in buttons])

    def answer(self, text: str) -> None:
        """Press a button of the open question (used by tests and the screenshot walk)."""
        self.modal.buttons[text].click()

    def toast(self, text: str) -> None:
        self.toasts.append(text)
        self.toast_label.setText(text)
        self.toast_label.adjustSize()
        self._place_toast()
        self.toast_label.show()
        self.toast_label.raise_()
        self.toast_timer.start(2600)

    def _place_toast(self) -> None:
        root = self.centralWidget()
        self.toast_label.move(
            max(0, (root.width() - self.toast_label.width()) // 2),
            max(0, root.height() - self.toast_label.height() - 74),
        )

    def _open_path(self, path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).expanduser().resolve())))

    def _choose_folder(self, title: str, start: str) -> str:
        from PySide6.QtWidgets import QFileDialog

        return QFileDialog.getExistingDirectory(self, title, start)

    def open_file(self, path: str) -> None:
        self.opened.append(path)
        self.open_path(path)

    def copy_text(self, text: str, notice: str) -> None:
        QApplication.clipboard().setText(text)
        self.toast(notice)

    def show_about(self) -> None:
        from logs_migrate import __version__

        self.ask(
            "About VCF Logs Migrator",
            [
                f"Version {__version__}. An independent community tool, not a Broadcom product.",
                rich(
                    "This app includes open-source software: Qt for Python (LGPL 3), Paramiko "
                    "(LGPL 2.1), cryptography (Apache 2.0) and others.",
                    "soft",
                ),
            ],
            [("Close", None, "primary"), ("Show all licences", self.show_licenses)],
        )

    def show_licenses(self) -> None:
        from ..licenses import text

        dialog = QDialog(self)
        dialog.setWindowTitle("Open-source licences")
        dialog.resize(800, 600)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text())
        layout.addWidget(view)
        dialog.exec()

    # Settings ----------------------------------------------------------------------------

    def apply_settings(self) -> None:
        settings = self.state.settings
        from ..diagnostics import configure

        try:
            configure(enabled=settings.debug_logging)
        except OSError:
            pass
        theme.apply_theme(QApplication.instance(), settings.theme)
        if self.timer.isActive():
            self.timer.start(settings.refresh_seconds * 1000)
        self.save_settings()
        self.refresh_all()

    def save_settings(self) -> None:
        try:
            self.state.settings.save()
        except OSError:
            pass  # A read-only profile still lets the session run.

    def move_record(self, folder: str) -> None:
        """Keep past runs in another folder. Signed out first: a session uses one record."""
        self.sign_out(quiet=True)
        self.submit("moved", "move_record", folder=folder)

    # Jobs --------------------------------------------------------------------------------

    def busy(self, job: str) -> bool:
        return job in self.active

    def submit(self, job: str, method: str, **kwargs) -> bool:
        if job in self.active:
            return False
        self.serial += 1
        self.active[job] = self.serial
        self.worker.submit(f"{job}#{self.serial}", method, **kwargs)
        return True

    def begin(self, job: str, page: str, on_cancel: bool = True, labels=None) -> None:
        """Show staged progress for a job on the screen that owns it."""
        self.owners[job] = page
        self.pages[page].activity.start(
            labels or STAGES[job],
            NOTES.get(job, ""),
            (lambda: self.cancel(job)) if on_cancel else None,
        )

    def owner(self, page: str) -> str | None:
        """The job whose progress this screen is showing, if any."""
        return next(
            (job for job, name in self.owners.items() if name == page and job in self.active),
            None,
        )

    def cancel(self, job: str) -> None:
        if job in READ_ONLY:
            self.active.pop(job, None)
            if job == "connect":
                for name in ("probe", "trust", "signin"):
                    self.active.pop(name, None)
                self.pending_credentials = {}
                self.state.reset_connection()
                self.submit("disconnect", "disconnect")
            if job == "plan":
                self.active.pop("discover", None)
            self.pages[self.owners.pop(job, self.current_page())].activity.stop()
            self.toast("Cancelled. Nothing was changed.")
            if job == "verify":
                self.show_page("copy")
            elif job == "compare" and self.current_page() == "rules":
                self.state.rules_seen = False
                self.show_page("connect")
            else:
                self.refresh_all()
        else:
            self.worker.request_cancel(f"{job}#{self.active.get(job)}")
            self.pages[self.owners[job]].activity.cancelling()

    def _progress(self, job_id: str, message: str) -> None:
        job = job_id.partition("#")[0]
        if job in {"probe", "trust", "signin"}:
            job = "connect"
        if not message.startswith("stage:") or job not in self.active:
            return
        page = self.owners.get(job)
        if page:
            self.pages[page].activity.advance(int(message[6:]))

    def _settle(self, job_id: str) -> str | None:
        """The job's name when its result is still wanted; None for one that was abandoned
        or belongs to a session that has been signed out since."""
        job, _, serial = job_id.partition("#")
        self.worker.pending.discard(job_id)
        if self.active.get(job) != int(serial or 0):
            return None
        del self.active[job]
        return job

    # Connect -----------------------------------------------------------------------------

    def connection_values(self) -> dict[str, str]:
        """Addresses and user names; never secrets."""
        settings = self.state.settings
        return {
            "source_host": settings.source_host,
            "destination": settings.destination_host,
            "source_api_user": settings.source_api_user,
            "source_ssh_user": settings.source_ssh_user,
            "destination_api_user": settings.destination_api_user,
            "destination_ssh_user": ""
            if settings.destination_token
            else settings.destination_ssh_user,
            "ops_url": settings.ops_url if settings.destination_token else "",
        }

    def pair(self) -> str:
        settings = self.state.settings
        pair = f"{settings.source_host.strip().lower()}|{settings.destination_host.strip().lower()}"
        # A pair confirmed against stand-in systems says nothing about real ones.
        return f"stand-in:{pair}" if settings.fixture_path else pair

    def test_connections(self, credentials: dict[str, str]) -> None:
        """One test for both systems. Nothing is sent until their identities are confirmed."""
        state = self.state
        keep = (state.resume, state.copy_id, state.run_id, state.server_needed, state.token_notice)
        self.timer.stop()
        self.active = {k: v for k, v in self.active.items() if k == "record"}
        state.reset_connection()
        state.resume, state.server_needed, state.token_notice = keep[0], keep[3], keep[4]
        if state.resume:
            state.copy_id, state.run_id = keep[1], keep[2]
        self.pending_credentials = dict(credentials)
        self.begin("connect", "connect")
        self.active["connect"] = -1  # The test is in hand from the first probe to the verdict.
        self.submit(
            "probe",
            "probe",
            fixture=state.settings.fixture_path,
            connection=self.connection_values(),
        )
        self.refresh_all()

    def _ask_identities(self, identities: list[dict]) -> None:
        names = {"source": "source", "destination": "destination", "ops": "VCF Operations"}
        table = GridTable(["System", "Certificate fingerprint (SHA-256)"], stretch=[2, 5])
        rows = []
        for item in identities:
            pairs = vm.fingerprint_pairs(item["fingerprint"])
            half = len(pairs) // 2 + 1
            host = item["name"].rsplit(":", 1)[0]
            rows.append(
                (
                    f"{host} ({names[item['role']]})",
                    '<span style="font-family:monospace;font-size:11.5px">'
                    f"{pairs[:half]}<br>{pairs[half:]}</span>",
                )
            )
        table.set_rows(rows)
        both = "\n".join(
            f"{item['name'].rsplit(':', 1)[0]}  {vm.fingerprint_pairs(item['fingerprint'])}"
            for item in identities
        )
        changed = [item for item in identities if item["state"] == "changed"]
        intro = (
            "No password has been sent yet. First, check that these two addresses are the "
            "systems you meant. If they are, and you are on your own network, that is "
            "normally enough."
        )
        if changed:
            who = " and ".join(names[item["role"]] for item in changed)
            intro = (
                f"No password has been sent. <b>The {who} now presents a different identity "
                "than the one recorded when you first connected.</b> That is expected if its "
                "certificate was replaced or the system was rebuilt. If neither happened, "
                "cancel and ask whoever runs it."
            )
        more = rich(
            "<b>For extra certainty,</b> compare the fingerprints: open each system’s web "
            "page in a browser, click the padlock, open the certificate, and find “SHA-256 "
            "fingerprint”. It is shown in the same form as above (pairs of letters and "
            "digits). The app remembers these and warns you if one ever changes. After "
            "signing in it also records an identity code for each server behind these "
            'addresses; you can see those on the next screen. <a href="copy">Copy both '
            "fingerprints</a>",
            "subtle",
            lambda _href: self.copy_text(both, "Fingerprints copied"),
        )

        def refuse():
            self.cancel_test("Nothing was sent.")

        def accept():
            self.submit("trust", "trust", identities=identities, replace=bool(changed))

        self.ask(
            "These systems have changed identity"
            if changed
            else "First time connecting to these systems",
            [intro, table, more],
            [
                ("Cancel. Send nothing.", refuse),
                (
                    "This is still my system. Continue."
                    if changed
                    else "These are my systems. Continue.",
                    accept,
                    "primary",
                ),
            ],
        )

    def cancel_test(self, notice: str) -> None:
        for job in ("connect", "probe", "trust", "signin"):
            self.active.pop(job, None)
        self.owners.pop("connect", None)
        self.pending_credentials = {}
        self.pages["connect"].activity.stop()
        self.submit("disconnect", "disconnect")
        self.toast(notice)
        self.refresh_all()

    def _connect(self) -> None:
        settings = self.state.settings
        self.submit(
            "signin",
            "connect",
            fixture=settings.fixture_path,
            journal=settings.journal_path,
            credentials=self.pending_credentials,
            connection=self.connection_values(),
        )

    def sign_out(self, quiet: bool = False) -> None:
        self.timer.stop()
        self.active = {k: v for k, v in self.active.items() if k == "record"}
        self.pending_credentials = {}
        self.state.reset_connection()
        self.state.resume = None
        self.state.server_needed = False
        self.pages["connect"].clear_secrets()
        self.submit("disconnect", "disconnect")
        if not quiet:
            self.toast("Signed out. Addresses are remembered, passwords are not.")
        self.show_page("connect")

    def reconnect(self) -> None:
        settings = self.state.settings
        self.submit(
            "reconnect", "reconnect", fixture=settings.fixture_path, journal=settings.journal_path
        )
        self.refresh_all()

    # Rules -------------------------------------------------------------------------------

    def compare_rules(self, show: bool = True) -> None:
        state = self.state
        if not state.connected or self.busy("compare"):
            return
        state.comparison_error = None
        state.config_result = None
        if self.submit("compare", "config_compare"):
            self.begin("compare", "rules")
        self.refresh_all()

    def apply_rules(self, selection: list[str]) -> None:
        if self.submit("apply", "config_apply", selection=selection):
            self.begin("apply", "rules", on_cancel=False)
        self.refresh_all()

    # Dates -------------------------------------------------------------------------------

    def list_source(self) -> None:
        state = self.state
        if state.connected and not self.busy("discover"):
            state.inventory_error = None
            self.submit("discover", "discover")

    def check_dates(self, start, end, exclude: list[dict] | None = None) -> None:
        state = self.state
        settings = state.settings
        state.clear_plan()
        state.excluded = list(exclude or [])
        state.start_failure = None
        first, last = vm.window_ms(start, end)
        extent = vm.history((state.inventory or {}).get("source"))
        discovered = state.discovered or {}
        self.context["plan"] = {"requested": (start, end)}
        if self.submit(
            "plan",
            "plan",
            start=first,
            end=last,
            attempts=settings.verify_attempts,
            poll_seconds=float(settings.count_poll_seconds),
            exclude=state.excluded,
            labels={
                "source": discovered["source"]["host"],
                "destination": discovered["destination"]["host"],
            },
            newest=extent["newest"].isoformat() if extent and extent["newest"] else None,
        ):
            self.begin("plan", "dates")
        self.refresh_all()

    # Copy --------------------------------------------------------------------------------

    def adopt(self, copy: dict) -> None:
        """Make a recorded copy the one this window is working on, and ask how it stands."""
        state = self.state
        if state.copy_id != copy["id"]:
            state.clear_copy()
            state.copy_id = copy["id"]
        state.start_failure = None
        run = copies.current_run(copy)
        state.run_id = run["id"] if run else None
        self._watch()

    def _watch(self) -> None:
        self.timer.start(self.state.settings.refresh_seconds * 1000)
        self.poll_status()

    def poll_status(self) -> None:
        state = self.state
        if state.connected and state.run_id and not self.busy("stop"):
            self.submit("status", "status", run_id=state.run_id)

    def start_copy(self) -> None:
        state = self.state
        if state.plan is None or self.busy("start"):
            return
        run = state.plan["parts"][0]
        preview = copies.preview(state.plan["parts"], state.plan["excluded"])
        self.context["start"] = {"run_id": run["id"], "copy_id": preview["id"]}
        state.start_failure = None
        state.clear_copy()
        if self.submit("start", "start", run_id=run["id"], acknowledged=state.acknowledged):
            self.begin("start", "copy")
        self.show_page("copy")

    def stop_copy(self) -> None:
        """Ask first, and say what stopping costs when the copy could not be continued."""
        state = self.state
        status = state.status
        if not status or status["status"] != "running":
            return
        if state.server_sign_in:
            body = [
                "The destination will stop fetching logs. <b>What has already arrived stays "
                "there.</b> You can continue later from where it stopped, and only what is "
                "missing will be sent."
            ]
            stop = "Stop the copy"
        else:
            body = [
                "The destination will stop fetching logs. <b>What has already arrived stays "
                "there.</b>",
                "<b>You gave no destination server sign-in, so after stopping, this copy "
                "cannot be continued from this app.</b> To be able to continue later, keep "
                "copying, add that sign-in on Connect, then stop.",
            ]
            stop = "Stop anyway"

        def confirmed():
            if self.submit("stop", "stop", task_id=status["id"], run_id=state.run_id):
                self.begin("stop", "copy", on_cancel=False)
            self.refresh_all()

        self.ask(
            "Stop the copy?", body, [("Keep copying", None, "primary"), (stop, confirmed, "danger")]
        )

    def continue_copy(self, missing: bool = False) -> None:
        """One step: check what arrived and send only the rest."""
        state = self.state
        copy = state.copy()
        if copy is None or self.busy("continue"):
            return
        settings = state.settings
        state.continue_failure = None
        self.context["continue"] = {
            "was": copy["state"],
            "missing": missing,
            "stopped_at": (copies.current_run(copy) or {}).get("finished", {}).get("at"),
        }
        part = copies.next_part(copy)
        waiting = copy["state"] == "between" or (
            part is not None
            and copy["current"] is not None
            and copy["parts"][copy["current"]] is part
        )
        if waiting and part is not None:
            root = part["root"]
            client = {k: v for k, v in (root.get("client") or {}).items() if k != "series"}
            submitted = self.submit(
                "continue",
                "start_part",
                series={k: v for k, v in copy["series"].items() if k != "index"},
                index=part["index"],
                attempts=settings.verify_attempts,
                poll_seconds=float(settings.count_poll_seconds),
                client=client,
                acknowledged=True,
                preview=root["id"],
            )
            labels = (
                "Checking the dates of the next part",
                "Checking the plan and free space once more",
                "Counting both sides, so the result can be checked later",
                "Starting the copy",
            )
        else:
            run = copies.current_run(copy)
            submitted = self.submit(
                "continue", "continue_copy", run_id=run["id"], acknowledged=copy["acknowledged"]
            )
            labels = None
        if submitted:
            self.begin("continue", "copy", labels=labels)
        self.show_page("copy")

    def check_result(self) -> None:
        """Count both systems for the current copy. Starts as soon as the screen opens."""
        state = self.state
        copy = state.copy()
        if copy is None:
            return
        state.verify_error = None
        run_ids = [part["last"]["id"] for part in copy["parts"] if part["last"] is not None]
        if self.submit("verify", "verify", run_ids=run_ids):
            self.begin("verify", "check")
        self.show_page("check")

    def more_dates(self) -> None:
        state = self.state
        self.timer.stop()
        state.clear_copy()
        state.clear_plan()
        self.list_source()
        self.show_page("dates")

    # Hand-offs ---------------------------------------------------------------------------

    def fix_past(self, copy_id: str) -> None:
        """From a past run to signing in, then straight back to what that run needs."""
        state = self.state
        copy = state.copy(copy_id)
        if copy is None:
            return
        missing = copy["state"] == "missing"
        state.resume = {
            "kind": "missing" if missing else "unfinished",
            "copy": copy_id,
            "chosen": True,
        }
        self.show_page("connect")

    def add_sign_in(self, back: bool = True) -> None:
        """To Connect, with the two boxes that are needed marked, and a way back."""
        state = self.state
        state.server_needed = True
        if back and state.copy_id:
            state.resume = {"kind": "back", "copy": state.copy_id}
        self.pages["connect"].edit()
        self.show_page("connect")

    def resume_go(self) -> None:
        state = self.state
        resume, state.resume = state.resume, None
        state.server_needed = False
        if resume is None:
            return
        copy = state.copy(resume["copy"])
        if copy is None:
            self.show_page("runs")
            return
        if resume["kind"] == "back":
            state.continue_failure = None
            self.show_page("copy")
            return
        self.adopt(copy)
        if resume["kind"] == "missing":
            self.continue_copy(missing=True)
        else:
            self.show_page("copy")

    # Files -------------------------------------------------------------------------------

    def save_report(self, copy_id: str) -> None:
        state = self.state
        state.save_error = None
        self.context["save"] = {"copy_id": copy_id}
        self.submit(
            "save",
            "save_report",
            copy_id=copy_id,
            folder=state.settings.export_folder,
            zone=vm.zone_name(state.settings.timezone),
            rules=vm.rule_results(state.config_result) or None,
        )

    def save_text(self, name: str, text: str, key: str) -> None:
        self.context["text"] = {"key": key, "name": name}
        self.submit(
            "text", "save_text", folder=self.state.settings.export_folder, name=name, text=text
        )

    def save_troubleshooting(self) -> None:
        from dataclasses import asdict

        settings = {
            key: value
            for key, value in asdict(self.state.settings).items()
            if key not in {"fixture_path"}
        }
        self.submit(
            "trouble",
            "save_troubleshooting",
            folder=self.state.settings.export_folder,
            settings=settings,
        )

    # Results -----------------------------------------------------------------------------

    def _done(self, job_id: str, result) -> None:
        state = self.state
        job = self._settle(job_id)
        if job is None:
            return
        if job == "record":
            first = state.record is None
            state.set_record(result)
            if first and state.resume is None and not state.connected:
                waiting = copyview.unfinished(state.copies)
                if waiting is not None:
                    state.resume = {"kind": "unfinished", "copy": waiting["id"]}
            self._after_record()
        elif job == "probe":
            state.identities = result["identities"]
            unknown = any(item["state"] != "trusted" for item in result["identities"])
            if unknown or state.settings.confirmed_pair != self.pair():
                self._ask_identities(result["identities"])
            else:
                self._connect()
        elif job == "trust":
            state.settings.confirmed_pair = self.pair()
            self.save_settings()
            self._connect()
        elif job == "codes":
            self.pages["connect"].show_codes(result)
        elif job == "moved":
            state.settings.journal_path = result["journal"]
            self.save_settings()
            state.set_record(result["record"])
            self.toast("Past runs moved. Sign in again to carry on.")
        elif job in {"signin", "reconnect"}:
            if job == "signin":
                self.active.pop("connect", None)
                self.owners.pop("connect", None)
                self.pages["connect"].activity.stop()
            self.pending_credentials = {}
            state.discovered = result
            state.connected = result["passed"]
            state.failure = None
            state.token_notice = False
            state.lost_at = state.status_error = None
            if state.connected:
                if job == "signin":
                    state.server_needed = False
                    if state.resume and state.resume["kind"] in {"unfinished", "missing"}:
                        # The rules were dealt with when this copy was first made.
                        state.rules_seen = True
                    self.submit("compare", "config_compare")
                    self.list_source()
                    self.submit("record", "record")
                else:
                    self.toast(
                        "Reconnected. The copy carried on while this window was out of touch."
                    )
                if state.run_id:
                    self._watch()
        elif job == "compare":
            state.comparison = result
            state.comparison_error = None
        elif job == "apply":
            state.config_result = result
            state.comparison_error = None
        elif job == "discover":
            state.inventory = result
            state.inventory_error = None
            self.pages["dates"].seed()
        elif job == "plan":
            state.plan = result
            state.plan_error = state.plan_refusal = None
            state.acknowledged = False
        elif job == "start":
            context = self.context.pop("start")
            state.clear_plan()
            self.pages["dates"].planned = None
            state.copy_id, state.run_id = context["copy_id"], result["run_id"]
            state.status = result["task"] if result["task"].get("id") else None
            state.status_at = vm.now_iso()
            self.submit("record", "record")
            self._watch()
            self.show_page("copy")
        elif job == "continue":
            context = self.context.pop("continue")
            if result.get("skipped"):
                self.submit("record", "record")
                self.context["after_record"] = "advance"
            else:
                zone = state.settings.timezone
                state.run_id = result["run_id"]
                state.status = result["task"] if result["task"].get("id") else None
                state.status_at = vm.now_iso()
                state.latest, state.arrived_seen = None, set()
                state.quiet_since = state.bytes_seen = None
                if context["missing"]:
                    state.continued_note = ""
                else:
                    after = "the failure" if context["was"] == "failed" else "the stop"
                    when = (
                        f" {vm.format_clock(context['stopped_at'], zone)}"
                        if context["stopped_at"]
                        else ""
                    )
                    state.continued_note = (
                        f"Continued at {vm.format_time(vm.now_iso(), zone)} after {after}{when}."
                    )
                self.submit("record", "record")
                self._watch()
        elif job in {"status", "stop"}:
            if result.get("run_id") != state.run_id:
                return
            self._status(result)
            if job == "stop":
                self.poll_status()  # Reads the copy once more, which records how it ended.
        elif job == "verify":
            state.set_record(result)
            state.verify_error = None
        elif job == "save":
            context = self.context.pop("save")
            state.saved[context["copy_id"]] = result
            self.toast("Report saved")
        elif job == "text":
            context = self.context.pop("text")
            if context["key"] == "details":
                state.details_saved = result
            else:
                folder = self.state.settings.export_folder
                self.toast(f"Saved “{context['name']}” in {folder}")
        elif job == "trouble":
            state.trouble_saved = result
            self.toast(f"Saved “{Path(result).name}” to your reports folder")
        if job in self.owners:
            self.pages[self.owners.pop(job)].activity.stop()
        self.refresh_all()

    def _after_record(self) -> None:
        """With the record fresh: keep a multi-part copy moving to its next part."""
        state = self.state
        advance = self.context.pop("after_record", None) == "advance"
        copy = state.copy()
        if copy is None and state.unsure and state.copy_id:
            # The lock on an unanswered start ran out and the destination shows no copy.
            self.timer.stop()
            state.clear_copy()
            self.toast("No copy appeared, so nothing was started. You can start again.")
            self.show_page("dates")
            return
        if copy is None or not state.connected:
            return
        if copy["state"] == "between" and (advance or state.status):
            if state.status and state.status["status"] != "completed" and not advance:
                return
            state.status = None
            self.continue_copy()
        elif copy["state"] in {"finished", "missing"}:
            self.timer.stop()

    def _status(self, result: dict) -> None:
        state = self.state
        copy = state.copy()
        previous = state.status["status"] if state.status else None
        state.status = result
        state.status_at = vm.now_iso()
        state.status_error = state.lost_at = None
        state.unsure = result.get("start_outcome") == "unknown"
        arrived = result.get("arrived") or {}
        new = set(arrived) - state.arrived_seen
        if new and copy is not None:
            ends = {group["id"]: group["end"] for group in copy["groups"]}
            newest = max(new, key=lambda item: ends.get(item, 0))
            if newest in ends:
                state.latest = {
                    "day": copies.day_of(ends[newest]),
                    "node": arrived[newest],
                    "at": vm.now_iso(),
                }
        state.arrived_seen |= set(arrived)
        moved = result.get("bytes_transferred")
        if new or moved != state.bytes_seen or state.quiet_since is None:
            state.bytes_seen = moved
            state.quiet_since = result.get("elapsed_seconds") or 0
        if result["status"] != "running" and not state.unsure:
            ended = result["status"] in {"completed", "cancelled", "failed"}
            noted = bool(copy) and bool((copies.current_run(copy) or {}).get("finished"))
            if (
                previous == "running"
                or copy is None
                or copy["state"] in {"running", "unsure"}
                or (ended and not noted)  # How it ended is recorded on the next reading.
            ):
                self.submit("record", "record")
            if result["status"] == "idle" or (ended and noted):
                self.timer.stop()

    def _failed(self, job_id: str, code: str, data: dict) -> None:
        state = self.state
        job = self._settle(job_id)
        if job is None:
            return
        failure = {**data, "code": code}
        activity = self.pages[self.owners[job]].activity if job in self.owners else None
        if job in {"probe", "trust", "signin"}:
            self.active.pop("connect", None)
            self.owners.pop("connect", None)
            self.pending_credentials = {}
            state.connected = False
            state.discovered = None
            if data.get("kind") == "token_needed":
                state.settings.destination_token = True
                self.save_settings()
                state.token_notice = True
                state.failure = None
            else:
                failure.setdefault("side", "source")
                failure.setdefault("kind", "other")
                state.failure = failure
            self.pages["connect"].activity.stop()
        elif job == "reconnect":
            state.status_error = code
        elif job == "compare":
            state.comparison = None
            state.comparison_error = code
        elif job == "apply":
            state.config_result = None
            state.comparison_error = code
        elif job == "discover":
            state.inventory_error = code
        elif job == "plan":
            state.plan = None
            state.plan_error = code
            state.plan_refusal = data.get("refusal")
            if (data.get("refusal") or {}).get("kind") == "listfail" and activity is not None:
                self.owners.pop(job, None)
                what, todo = vm.plain_error(code)
                activity.fail(
                    "Could not read the list of logs from the source",
                    "A source server did not answer. <b>What to do:</b> check that every "
                    "source server is running and reachable, then press Try again. Nothing was "
                    "changed.",
                )
                self.refresh_all()
                return
        elif job == "start":
            context = self.context.pop("start", {})
            if data.get("sent"):
                # The request went out and no answer came back: never offer it again.
                state.clear_plan()
                self.pages["dates"].planned = None
                state.copy_id, state.run_id = context.get("copy_id"), context.get("run_id")
                state.unsure = True
                self.submit("record", "record")
                self._watch()
            else:
                state.start_failure = failure
                if data.get("stage") == "cancelled":
                    state.start_failure = None
                    self.toast("Cancelled. Nothing was started.")
                    self.pages[self.owners.pop(job, "copy")].activity.stop()
                    self.show_page("dates")
                    return
        elif job == "continue":
            self.context.pop("continue", None)
            if data.get("sent"):
                # Watch the run that was asked for, not the one that had stopped.
                state.run_id = data.get("run_id") or state.run_id
                state.status = None
                state.unsure = True
                self.submit("record", "record")
                self._watch()
            elif data.get("stage") == "cancelled":
                self.toast("Cancelled. Nothing was started.")
            else:
                state.continue_failure = failure
        elif job == "status":
            state.status_error = code
            copy = state.copy()
            running = (
                state.status["status"] == "running"
                if state.status is not None
                else bool(copy) and copy["state"] in {"running", "unsure"}
            )
            if running:
                # Out of touch with a copy that was running when last seen.
                state.lost_at = state.lost_at or state.status_at or vm.now_iso()
        elif job == "stop":
            state.status_error = code
            self.toast("The destination did not confirm the stop. Checking how the copy stands.")
            self.poll_status()
        elif job == "verify":
            state.verify_error = code
        elif job == "save":
            self.context.pop("save", None)
            state.save_error = code
            self.toast(vm.plain_error(code)[0])
        elif job in {"text", "trouble"}:
            self.context.pop("text", None)
            self.toast(vm.plain_error(code)[0])
        elif job == "record":
            state.record_error = code
        elif job == "moved":
            self.toast(vm.plain_error(code)[0])
        if job in self.owners:
            self.pages[self.owners.pop(job)].activity.stop()
        self.refresh_all()

    def closeEvent(self, event):  # noqa: N802 (Qt override)
        self.timer.stop()
        self.focus_timer.stop()
        self.worker.stop()
        super().closeEvent(event)


def chip_labels(window: MainWindow) -> list[str]:
    return [
        window.chips.itemAt(i).widget().text()
        for i in range(window.chips.count())
        if isinstance(window.chips.itemAt(i).widget(), QLabel)
    ]
