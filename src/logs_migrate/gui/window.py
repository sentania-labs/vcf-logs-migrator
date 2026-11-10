"""Workspace window: header, sidebar, page stack. Routes worker results into AppState."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from . import viewmodel as vm
from .pages.configuration import ConfigurationPage
from .pages.connections import ConnectionsPage
from .pages.migration import MigrationPage
from .pages.overview import OverviewPage
from .pages.plan import PlanPage
from .pages.reports import ReportsPage
from .pages.settings_page import SettingsPage
from .pages.verification import VerificationPage
from .settings import THEMES, Settings
from .state import AppState
from .widgets import button, label, section
from .worker import EngineWorker

NAV = [
    ("overview", "Overview"),
    ("connections", "Connections"),
    ("configuration", "Configuration"),
    ("plan", "Discovery & Plan"),
    ("migration", "Migration"),
    ("verification", "Verification"),
    ("reports", "Reports"),
]


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.setWindowTitle("VCF Logs Migrator")
        about = self.menuBar().addMenu("About")
        about.addAction("Licenses", self.show_licenses)
        self.state = AppState(settings)
        self.worker = EngineWorker()
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.trust_required.connect(self._trust_required)
        self.progress_lines = {}
        self.progress_label = QLabel()
        self.progress_label.setAccessibleName("Operation progress")
        self.statusBar().addWidget(self.progress_label, 1)
        self.worker.progress.connect(self._show_progress)
        self.worker.start()
        self.pending_credentials: dict[str, str] = {}
        # Tests replace this to answer the identity dialog without a window.
        self.ask_trust = self._ask_trust
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_status)
        # Results from a previous connection are dropped instead of landing in a reset state.
        self.generation = 0
        self.job_generation: dict[str, int] = {}
        self.seed_pending = True

        root = QWidget()
        root.setObjectName("lattice-root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._header())
        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        middle.addWidget(self._sidebar())
        self.stack = QStackedWidget()
        middle.addWidget(self.stack, 1)
        outer.addLayout(middle, 1)

        self.pages = {
            "overview": OverviewPage(self),
            "connections": ConnectionsPage(self),
            "configuration": ConfigurationPage(self),
            "plan": PlanPage(self),
            "migration": MigrationPage(self),
            "verification": VerificationPage(self),
            "reports": ReportsPage(self),
            "settings": SettingsPage(self),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.return_page = "overview"
        for number, (key, _title) in enumerate([*NAV, ("settings", "Settings")], start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{number}"), self)
            shortcut.activated.connect(lambda name=key: self.show_page(name))
        self.resize(1440, 900)
        self.setMinimumSize(900, 600)
        self.refresh_all()
        self.show_page("overview")

    def show_licenses(self) -> None:
        from ..licenses import text

        dialog = QDialog(self)
        dialog.setWindowTitle("Third-party licenses")
        dialog.resize(800, 600)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text())
        layout.addWidget(view)
        dialog.exec()

    # Chrome ------------------------------------------------------------------------------

    def _header(self) -> QFrame:
        header = QFrame()
        header.setProperty("lattice", "header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 10, 24, 10)
        layout.setSpacing(16)
        layout.addWidget(label("VCF Logs Migrator", "page-title"))
        layout.addWidget(label("Historical log migration", "muted"))
        layout.addStretch(1)
        self.display_label = label("", "muted")
        layout.addWidget(self.display_label)
        self.theme_combo = QComboBox()
        self.theme_combo.setAccessibleName("Theme")
        self.theme_combo.addItems([f"Theme: {t.title()}" for t in THEMES])
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        layout.addWidget(self.theme_combo)
        self.details_button = button("Show details", "", self.show_details)
        self.details_button.hide()
        layout.addWidget(self.details_button)
        layout.addWidget(button("Settings", "", lambda: self.show_page("settings")))
        return header

    def _sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setProperty("lattice", "sidebar")
        sidebar.setFixedWidth(208)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 18, 12, 18)
        layout.setSpacing(8)
        layout.addWidget(section("Migration workspace"))
        self.nav = QListWidget()
        self.nav.setProperty("lattice", "steps")
        self.nav.setAccessibleName("Migration workspace")
        self.nav.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.nav.setToolTip("Ctrl+1 to Ctrl+7 switch pages; Ctrl+8 opens Settings")
        for key, title in NAV:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._nav_changed)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setFixedHeight(self.nav.sizeHintForRow(0) * len(NAV) + 2 * len(NAV) + 8)
        layout.addWidget(self.nav)
        layout.addStretch(1)
        return sidebar

    def _nav_changed(self, index: int) -> None:
        if index >= 0:
            self.show_page(NAV[index][0])

    def show_page(self, name: str) -> None:
        page = self.pages[name]
        keys = [key for key, _ in NAV]
        current = self.current_page()
        if name == "settings" and current != "settings":
            self.return_page = current
        self.nav.blockSignals(True)
        self.nav.setCurrentRow(keys.index(name) if name in keys else -1)
        self.nav.blockSignals(False)
        page.refresh()
        self.stack.setCurrentWidget(page)
        if self.isVisible() and not self.nav.hasFocus():
            page.focus_primary()

    def leave_settings(self) -> None:
        self.show_page(self.return_page if self.return_page != "settings" else "overview")

    def hosts(self) -> tuple[str, str] | None:
        """Typed appliance addresses for display; cluster ids when none were typed."""
        settings = self.state.settings
        if not self.state.discovered:
            return None
        source = settings.source_host or self.state.discovered["source"]["id"]
        destination = settings.destination_host or self.state.discovered["destination"]["id"]
        return (source, destination)

    def current_page(self) -> str:
        for name, page in self.pages.items():
            if page is self.stack.currentWidget():
                return name
        return "overview"

    def refresh_all(self) -> None:
        self.display_label.setText(f"Display: {self.state.settings.timezone}")
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(THEMES.index(self.state.settings.theme))
        self.theme_combo.blockSignals(False)
        for page in self.pages.values():
            page.refresh()

    # Settings ----------------------------------------------------------------------------

    def _theme_changed(self, index: int) -> None:
        self.state.settings.theme = THEMES[index]
        self.apply_settings(self.state.settings, reload_page=False)
        self.pages["settings"].theme.setCurrentIndex(index)

    def apply_settings(self, settings: Settings, *, reload_page: bool = True) -> None:
        previous = self.state.settings
        self.state.settings = settings
        if previous.debug_logging != settings.debug_logging:
            from ..diagnostics import configure

            try:
                configure(enabled=settings.debug_logging)
            except OSError:
                pass
        theme.apply_theme(QApplication.instance(), settings.theme)
        self.timer.setInterval(settings.refresh_seconds * 1000)
        self.save_settings()
        if reload_page:
            self.pages["settings"].load()
        if self.state.connected and (
            previous.fixture_path != settings.fixture_path
            or previous.journal_path != settings.journal_path
        ):
            # A new fixture or journal only takes effect through a fresh connection.
            self.disconnect_environments()
            self.return_page = "connections"
        self.refresh_all()

    def save_settings(self) -> None:
        try:
            self.state.settings.save()
        except OSError:
            pass  # A read-only profile still lets the session run.

    # Engine jobs -------------------------------------------------------------------------

    def _show_progress(self, message: str) -> None:
        key = message.split(": sizing", 1)[0] if ": sizing" in message else "query"
        self.progress_lines[key] = message
        self.progress_label.setText("\n".join(self.progress_lines.values()))

    def submit(self, job: str, method: str, **kwargs) -> bool:
        accepted = self.worker.submit(job, method, **kwargs)
        if accepted:
            if job in {"connect", "selftest", "plan", "start"}:
                self.progress_lines.clear()
                self.progress_label.clear()
            self.job_generation[job] = self.generation
        return accepted

    def connection_values(self) -> dict[str, str]:
        """Addresses and user names for a live connection; never secrets."""
        settings = self.state.settings
        return {
            "source_host": settings.source_host,
            "destination": settings.destination_host,
            "source_api_user": settings.source_api_user,
            "source_ssh_user": settings.source_ssh_user,
            "destination_api_user": settings.destination_api_user,
            "destination_ssh_user": settings.destination_ssh_user,
            "ops_url": settings.ops_url,
        }

    def connect_environments(self, credentials: dict[str, str]) -> None:
        settings = self.state.settings
        self.generation += 1
        self.state.reset_connection()
        self.seed_pending = True
        fixture = settings.fixture_path
        if fixture and not Path(fixture).expanduser().is_file():
            self.state.connect_error = (
                f"gui:The simulator fixture file was not found: {fixture}. "
                "Choose an existing fixture in Settings, or clear it to connect to appliances."
            )
            self.refresh_all()
            return
        self.pending_credentials = dict(credentials)
        self.state.mode = "live" if not fixture else "simulation"
        self.submit(
            "connect",
            "connect",
            fixture=settings.fixture_path,
            journal=settings.journal_path,
            credentials=credentials,
            connection=self.connection_values(),
        )

    def reconnect_environments(self) -> None:
        """Re-open the session after a lost connection, keeping what was observed so far."""
        settings = self.state.settings
        self.submit(
            "connect", "reconnect", fixture=settings.fixture_path, journal=settings.journal_path
        )

    def run_selftest(self) -> None:
        if self.state.connected and self.state.live:
            self.state.selftest_error = None
            self.submit("selftest", "selftest")

    def _ask_trust(self, identities: list[dict]) -> bool:
        lines = "\n\n".join(i["display"] for i in identities)
        answer = QMessageBox.question(
            self,
            "Trust appliance identities",
            "These appliance identities are not enrolled yet. Compare each fingerprint with "
            "the appliance before trusting it. Nothing has been sent to them.\n\n"
            + lines
            + "\n\nTrust and remember them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _trust_required(self, job: str, identities: list) -> None:
        if not self._settle(job):
            return
        if self.ask_trust(list(identities)):
            self.submit(
                "trust",
                "trust",
                identities=[
                    {"name": i["name"], "fingerprint": i["fingerprint"]} for i in identities
                ],
            )
        else:
            self.pending_credentials = {}
            self.state.connected = False
            self.state.discovered = None
            self.state.connect_error = (
                "gui:The appliance identities were not trusted, so nothing was sent. "
                "Compare the fingerprints and test again to enroll them."
            )
            self.refresh_all()

    def disconnect_environments(self) -> None:
        self.pending_credentials = {}
        self.timer.stop()
        self.generation += 1
        self.state.reset_connection()
        self.submit("disconnect", "disconnect")
        self.refresh_all()

    def poll_status(self) -> None:
        if self.state.connected:
            self.submit("status", "status", run_id=self.state.run_id)

    def verify_run(self, run_id: str) -> None:
        self.state.verify_run_id = run_id
        self.state.verify_error = None
        self.submit("verify:" + run_id, "verify", run_id=run_id)

    def select_run(self, run_id: str) -> None:
        self.state.run_id = run_id
        self.state.status = None
        self.poll_status()
        self.state.report = None
        self.state.verification = None
        self.state.verify_error = None
        self.state.exported = []
        self.state.export_error = None
        self.load_report(run_id)

    def load_report(self, run_id: str) -> None:
        self.submit("report:" + run_id, "report", run_id=run_id)

    def _settle(self, job: str) -> bool:
        """Clear the job and say whether its result belongs to the current connection."""
        self.worker.pending.discard(job)
        if job in {"connect", "selftest", "plan", "start"}:
            self.progress_lines.clear()
            self.progress_label.clear()
        return self.job_generation.pop(job, self.generation) == self.generation

    def _done(self, job: str, result) -> None:
        state = self.state
        if not self._settle(job):
            return
        if job == "trust":
            # Identities enrolled; the connection that needed them runs again.
            self.generation += 1
            self.submit(
                "connect",
                "connect",
                fixture=state.settings.fixture_path,
                journal=state.settings.journal_path,
                credentials=self.pending_credentials,
                connection=self.connection_values(),
            )
            return
        if job == "connect":
            self.pending_credentials = {}
            state.connected = True
            state.discovered = result
            state.connect_error = None
            state.mode = result.get("mode", "simulation")
            state.checks = [tuple(row) for row in result.get("checks", [])]
            self.timer.start(state.settings.refresh_seconds * 1000)
            self.submit("runs", "runs")
            self.poll_status()
            if self.seed_pending:
                self.seed_pending = False
                self.pages["plan"].seed_dates()
        elif job == "disconnect":
            return
        elif job == "config_compare":
            state.comparison = result
            state.comparison_error = None
        elif job == "config_apply":
            state.config_result = result
            state.comparison_error = None
            state.restart_pending = sorted(
                set(state.restart_pending)
                | {r["name"] for r in result["rules"] if r.get("restart_required")}
            )
        elif job == "plan":
            state.plan_run = result
            state.plan_error = None
            state.start_error = None
            state.acknowledged = False
            state.run_id = result["id"]
            state.status = None
            self.poll_status()
            state.verification = None
            state.verify_error = None
            state.exported = []
            state.export_error = None
            self.submit("runs", "runs")
            self.load_report(result["id"])
        elif job == "start":
            state.status = result["task"] if result["task"].get("id") else None
            state.status_at = vm.now_iso()
            state.status_error = None
            state.start_error = None
            state.started_run_id = result["run_id"]
            self.load_report(result["run_id"])
            self.show_page("migration")
            self.poll_status()
        elif job in {"status", "stop"}:
            if job == "status" and result.get("run_id") != state.run_id:
                self.poll_status()
                return
            previous = state.status["status"] if state.status else None
            state.status = result
            state.status_at = vm.now_iso()
            state.status_error = None
            if previous == "running" and result["status"] != "running":
                if state.run_id:
                    self.load_report(state.run_id)
                self.submit("discover", "discover")  # Inventory moved; the Overview must too.
        elif job == "discover":
            state.discovered = result
        elif job.startswith("verify:"):
            run_id = job[len("verify:") :]
            state.verify_run_id = None
            if run_id != state.run_id:
                return  # The operator moved to another run; this result is not its evidence.
            state.verification = result
            state.verify_error = None
            self.load_report(run_id)
            self.submit("runs", "runs")
        elif job == "runs":
            state.runs = result
            if state.run_id is None and result:
                state.run_id = result[-1]["id"]
                self.load_report(state.run_id)
            self.poll_status()
        elif job.startswith("report:"):
            if result["run"]["id"] == state.run_id:
                state.report = result
                if state.plan_run is None or state.plan_run["id"] != state.run_id:
                    state.plan_error = None
                # The journal's copy is the plan every page describes: it carries what the
                # engine recorded at start (per-group counts) and the latest verification.
                state.plan_run = result["run"]
                verification = result["run"]["verification"]
                if state.verification is None and verification["outcome"] != "verification_pending":
                    state.verification = verification
        elif job == "export":
            state.exported = result
            state.export_error = None
        elif job == "selftest":
            state.selftest = result
            state.selftest_error = None
        self.refresh_all()

    def _failed(self, job: str, code: str) -> None:
        state = self.state
        if not self._settle(job):
            return
        self.last_error = f"{job}: {code}"
        self.details_button.show()
        if job in {"connect", "trust"}:
            self.pending_credentials = {}
        if job == "connect":
            state.connected = False
            state.discovered = None
            state.connect_error = code
        elif job == "config_compare":
            state.comparison = None
            state.comparison_error = code
        elif job == "config_apply":
            state.config_result = None
            state.comparison_error = code
            self.pages["configuration"].applying = False
            self.pages["configuration"].progress.hide()
        elif job == "plan":
            state.plan_run = None
            state.plan_error = code
        elif job == "start":
            state.start_error = code
            state.status = None
            if not vm.is_pre_post(code) and state.plan_run:
                # The POST may have landed; never offer this plan again.
                state.started_run_id = state.plan_run["id"]
            self.poll_status()
        elif job in {"status", "stop"}:
            state.status_error = code
        elif job == "discover":
            return
        elif job.startswith("verify:"):
            state.verify_run_id = None
            if job[len("verify:") :] == state.run_id:
                state.verify_error = code
        elif job == "export":
            state.export_error = code
        elif job == "selftest":
            state.selftest = None
            state.selftest_error = code
        elif job == "trust":
            state.connect_error = code
        elif job.startswith("report:"):
            state.report = None
        self.refresh_all()

    def show_details(self) -> None:
        from ..diagnostics import log_path

        path = log_path()
        box = QMessageBox(self)
        box.setWindowTitle("Troubleshooting details")
        box.setText(getattr(self, "last_error", "No error recorded"))
        if path and path.exists():
            box.setDetailedText(path.read_text(encoding="utf-8", errors="replace"))
            folder = box.addButton("Open log folder", QMessageBox.ButtonRole.ActionRole)
            save = box.addButton("Save log", QMessageBox.ButtonRole.ActionRole)
        else:
            box.setInformativeText("Enable detailed troubleshooting logging in Settings and retry.")
            folder = save = None
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if path and box.clickedButton() == folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent.resolve())))
        elif path and box.clickedButton() == save:
            target, _ = QFileDialog.getSaveFileName(self, "Save troubleshooting log", path.name)
            if target and Path(target).resolve() != path.resolve():
                shutil.copyfile(path, target)

    def closeEvent(self, event):  # noqa: N802 (Qt override)
        self.timer.stop()
        self.worker.stop()
        super().closeEvent(event)
