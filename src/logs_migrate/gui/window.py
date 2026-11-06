"""Workspace window: header, sidebar, page stack. Routes worker results into AppState."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
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
        self.state = AppState(settings)
        self.worker = EngineWorker()
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_status)
        # Results from a previous connection are dropped instead of landing in a reset state.
        self.generation = 0
        self.job_generation: dict[str, int] = {}

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
        self.resize(1440, 900)
        self.setMinimumSize(900, 600)
        self.refresh_all()
        self.show_page("overview")

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
        self.theme_combo.addItems([f"Theme: {t.title()}" for t in THEMES])
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        layout.addWidget(self.theme_combo)
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
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        self.nav.blockSignals(True)
        self.nav.setCurrentRow(keys.index(name) if name in keys else -1)
        self.nav.blockSignals(False)
        page.refresh()
        self.stack.setCurrentWidget(page)

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
        self.refresh_all()

    def save_settings(self) -> None:
        try:
            self.state.settings.save()
        except OSError:
            pass  # A read-only profile still lets the session run.

    # Engine jobs -------------------------------------------------------------------------

    def submit(self, job: str, method: str, **kwargs) -> bool:
        accepted = self.worker.submit(job, method, **kwargs)
        if accepted:
            self.job_generation[job] = self.generation
        return accepted

    def connect_environments(self, credentials: dict[str, str]) -> None:
        settings = self.state.settings
        self.generation += 1
        self.state.reset_connection()
        self.submit(
            "connect",
            "connect",
            fixture=settings.fixture_path,
            journal=settings.journal_path,
            credentials=credentials,
        )

    def reconnect_environments(self) -> None:
        """Re-open the session after a lost connection, keeping what was observed so far."""
        settings = self.state.settings
        self.submit(
            "connect", "reconnect", fixture=settings.fixture_path, journal=settings.journal_path
        )

    def disconnect_environments(self) -> None:
        self.timer.stop()
        self.generation += 1
        self.state.reset_connection()
        self.submit("disconnect", "disconnect")
        self.refresh_all()

    def poll_status(self) -> None:
        if self.state.connected:
            self.submit("status", "status")

    def select_run(self, run_id: str) -> None:
        self.state.run_id = run_id
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
        return self.job_generation.pop(job, self.generation) == self.generation

    def _done(self, job: str, result) -> None:
        state = self.state
        if not self._settle(job):
            return
        if job == "connect":
            state.connected = True
            state.discovered = result
            state.connect_error = None
            self.timer.start(state.settings.refresh_seconds * 1000)
            self.submit("runs", "runs")
            self.poll_status()
            self.pages["plan"].seed_dates()
        elif job == "disconnect":
            return
        elif job == "config_compare":
            state.comparison = result
            state.comparison_error = None
        elif job == "config_apply":
            state.config_result = result
            state.comparison_error = None
        elif job == "plan":
            state.plan_run = result
            state.plan_error = None
            state.start_error = None
            state.acknowledged = False
            state.run_id = result["id"]
            state.verification = None
            state.verify_error = None
            state.exported = []
            state.export_error = None
            self.submit("runs", "runs")
            self.load_report(result["id"])
        elif job == "start":
            state.status = result["task"]
            state.status_at = vm.now_iso()
            state.status_error = None
            state.start_error = None
            state.started_run_id = result["run_id"]
            self.load_report(result["run_id"])
            self.show_page("migration")
        elif job in {"status", "stop"}:
            previous = state.status["status"] if state.status else None
            state.status = result
            state.status_at = vm.now_iso()
            state.status_error = None
            if previous == "running" and result["status"] != "running" and state.run_id:
                self.load_report(state.run_id)
        elif job == "verify":
            state.verification = result
            state.verify_error = None
            if state.run_id:
                self.load_report(state.run_id)
                self.submit("runs", "runs")
        elif job == "runs":
            state.runs = result
            if state.run_id is None and result:
                state.run_id = result[-1]["id"]
                self.load_report(state.run_id)
        elif job.startswith("report:"):
            if result["run"]["id"] == state.run_id:
                state.report = result
                if state.plan_run is None or state.plan_run["id"] != state.run_id:
                    # The selected run is the plan every page describes.
                    state.plan_run = result["run"]
                    state.plan_error = None
                verification = result["run"]["verification"]
                if state.verification is None and verification["outcome"] != "verification_pending":
                    state.verification = verification
        elif job == "export":
            state.exported = result
            state.export_error = None
        self.refresh_all()

    def _failed(self, job: str, code: str) -> None:
        state = self.state
        if not self._settle(job):
            return
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
            try:
                before_post = vm.Code(code) in vm.PRE_POST_CODES
            except ValueError:
                before_post = False
            if not before_post and state.plan_run:
                # The POST may have landed; never offer this plan again.
                state.started_run_id = state.plan_run["id"]
            self.poll_status()
        elif job in {"status", "stop"}:
            state.status_error = code
        elif job == "verify":
            state.verify_error = code
        elif job == "export":
            state.export_error = code
        elif job.startswith("report:"):
            state.report = None
        self.refresh_all()

    def closeEvent(self, event):  # noqa: N802 (Qt override)
        self.timer.stop()
        self.worker.stop()
        super().closeEvent(event)
