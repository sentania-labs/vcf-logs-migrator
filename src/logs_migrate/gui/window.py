"""Workspace window: header, sidebar, page stack. Routes worker results into AppState."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, Qt, QTimer, QUrl
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
    QProgressBar,
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

PROGRESS_JOBS = {"probe", "connect", "selftest", "plan", "start", "resume", "verify", "discover"}

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
        self.progress_label = QLabel()
        self.activity_current = ""
        self.activity_completed = []
        self.activity_job = ""
        self.activity_page = None
        self.activity_pending = []
        self.worker.progress.connect(self._show_progress)
        self.worker.start()
        self.pending_credentials: dict[str, str] = {}
        # Tests replace this to answer the identity dialog without a window.
        self.ask_trust = self._ask_trust
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_status)
        self.probe_clock = QElapsedTimer()
        self.probe_timer = QTimer(self)
        self.probe_timer.setInterval(250)
        self.probe_timer.timeout.connect(self._update_probe_elapsed)
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
        content = QVBoxLayout()
        self.activity_panel = QFrame(self.stack)
        self.activity_panel.setProperty("lattice", "card")
        self.activity_panel.setFixedWidth(360)
        activity = QVBoxLayout(self.activity_panel)
        activity.setContentsMargins(12, 10, 12, 10)
        self.page_progress = label("", "body", wrap=True)
        self.page_progress.setAccessibleName("Current operation progress")
        activity.addWidget(self.page_progress)
        self.upcoming = label("", "hint", wrap=True)
        self.upcoming.hide()
        activity.addWidget(self.upcoming)
        self.probe_activity = QProgressBar()
        self.probe_activity.setRange(0, 0)
        self.probe_activity.setTextVisible(False)
        self.probe_activity.setAccessibleName("Operation in progress")
        self.probe_activity.hide()
        activity.addWidget(self.probe_activity)
        self.probe_elapsed = label("", "hint")
        self.probe_elapsed.hide()
        activity.addWidget(self.probe_elapsed)
        self.progress_details = QPlainTextEdit()
        self.progress_details.setReadOnly(True)
        self.progress_details.setMaximumHeight(100)
        self.progress_details.hide()
        self.progress_details_button = button("Show details", "", self._toggle_progress_details)
        self.progress_details_button.hide()
        activity.addWidget(self.progress_details_button)
        activity.addWidget(self.progress_details)
        self.activity_panel.hide()
        content.addWidget(self.stack, 1)
        middle.addLayout(content, 1)
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
        if name != current:
            self.activity_panel.hide()
            self.progress_details.hide()
            self.progress_details_button.setText("Show details")
            self.activity_page = None
        if name == "settings" and current != "settings":
            self.return_page = current
        self.nav.blockSignals(True)
        self.nav.setCurrentRow(keys.index(name) if name in keys else -1)
        self.nav.blockSignals(False)
        self.stack.setCurrentWidget(page)
        if (
            name == "plan"
            and self.state.connected
            and self.state.discovered
            and self.state.discovered.get("inventory_pending")
            and "discover" not in self.worker.pending
        ):
            self.submit("discover", "discover")
        page.refresh()
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

    def _place_activity(self) -> None:
        self.activity_panel.adjustSize()
        self.activity_panel.move(
            max(0, self.stack.width() - self.activity_panel.width() - 12),
            max(8, self.stack.height() - self.activity_panel.height() - 100),
        )
        self.activity_panel.raise_()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "activity_panel"):
            self._place_activity()

    def _toggle_progress_details(self) -> None:
        show = not self.progress_details.isVisible()
        self.progress_details.setVisible(show)
        self.progress_details_button.setText("Hide details" if show else "Show details")
        self._place_activity()

    def _update_probe_elapsed(self) -> None:
        seconds = self.probe_clock.elapsed() // 1000
        self.probe_elapsed.setText(f"Elapsed {seconds // 60}:{seconds % 60:02d}")

    def _show_progress(self, message: str) -> None:
        self.progress_details.appendPlainText(message)
        if message.startswith("Contacting "):
            return
        if self.activity_job == "plan" and message == "Checking versions…":
            message = "Preparing preview…"
        if message.startswith("Self-test:"):
            message = "Checking " + message.partition(":")[2].strip().lower() + "…"
        elif message.startswith("Reading cluster members"):
            message = "Checking cluster members…"
        if self.activity_current and self.activity_current != message:
            if self.activity_current not in self.activity_completed:
                self.activity_completed.append(self.activity_current)
        self.activity_current = message
        for pending in tuple(self.activity_pending):
            if message.lower().startswith(pending.lower()):
                self.activity_pending.remove(pending)
        self.upcoming.setText(
            "Next: " + " · ".join(self.activity_pending) if self.activity_pending else ""
        )
        self.upcoming.setVisible(bool(self.activity_pending))
        completed = "\n".join("✓ " + item for item in self.activity_completed[-4:])
        self.page_progress.setText((completed + "\n" if completed else "") + "◌ " + message)
        if self.activity_page == self.current_page():
            self.activity_panel.show()
            self._place_activity()

    def submit(self, job: str, method: str, **kwargs) -> bool:
        accepted = self.worker.submit(job, method, **kwargs)
        if accepted:
            if job.split(":")[0] in PROGRESS_JOBS:
                self.activity_job = job
                self.activity_page = self.current_page()
                self.activity_current = ""
                self.activity_completed = []
                kind = job.split(":")[0]
                self.activity_pending = (
                    {
                        "connect": [
                            "Testing destination",
                            "Checking cluster members",
                            "Checking versions",
                            "Listing buckets",
                        ],
                        "probe": ["Testing destination"],
                        "plan": ["Listing buckets", "Sizing buckets", "Checking space", "Planning"],
                    }.get(kind, [])
                    if self.state.live or kind == "probe"
                    else []
                )
                self.progress_details.clear()
                self.progress_details.hide()
                self.progress_details_button.setText("Show details")
                self.probe_clock.start()
                self._update_probe_elapsed()
                self.probe_activity.show()
                self.probe_elapsed.show()
                self.progress_details_button.show()
                self.probe_timer.start()
                names = {
                    "probe": "Testing source…",
                    "connect": "Testing source…" if self.state.live else "Checking versions…",
                    "selftest": "Checking versions…",
                    "plan": "Preparing preview…",
                    "start": "Checking migration…",
                    "resume": "Checking versions…",
                    "verify": "Verifying counts…",
                    "discover": "Listing buckets…",
                }
                self._show_progress(names.get(job.split(":")[0], "Working…"))
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

    def probe_environments(self, credentials: dict[str, str]) -> None:
        self.pending_credentials = dict(credentials)
        self.timer.stop()
        self.generation += 1
        self.state.reset_connection()
        self.submit("probe", "probe_endpoints", connection=self.connection_values())
        self.refresh_all()

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
        from ..trust import shared_ssh_keys

        lines = "\n\n".join(i["display"] for i in identities)
        shared = dict(shared_ssh_keys(identities))
        shared.update({i["name"]: i["shared_with"] for i in identities if i.get("shared_with")})
        warning = "".join(
            f"\n{name} presents the same SSH key as {first}." for name, first in shared.items()
        )
        answer = QMessageBox.question(
            self,
            "Trust appliance identities",
            "These appliance identities are not enrolled yet. Compare each fingerprint with "
            "the appliance before trusting it. Nothing has been sent to them.\n\n"
            + lines
            + warning
            + "\n\nTrust and remember them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _trust_required(self, job: str, identities: list) -> None:
        if not self._settle(job):
            return
        if self.ask_trust(list(identities)):
            self.pending_trust_job = job
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
        self.state.resume_error = None
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
        if job == self.activity_job:
            self.probe_timer.stop()
            self.probe_activity.hide()
            self.upcoming.hide()
            label = {
                "plan": "Plan ready",
                "resume": "Resume preview complete",
                "verify": "Verification complete",
                "connect": "Connected to both appliances",
                "discover": "Inventory ready",
                "start": "Migration started",
                "probe": "Connection identities checked",
                "selftest": "Checks complete",
            }.get(job.split(":")[0], "Operation complete")
            self.page_progress.setText("✓ " + label)
            self.progress_details.hide()
            self.progress_details_button.hide()
            self.probe_elapsed.hide()
            self._place_activity()
            self.activity_job = ""
            QTimer.singleShot(4000, self._dismiss_finished_activity)
        return self.job_generation.pop(job, self.generation) == self.generation

    def _dismiss_finished_activity(self) -> None:
        if not self.activity_job:
            self.activity_panel.hide()

    def _done(self, job: str, result) -> None:
        state = self.state
        if not self._settle(job):
            return
        if job == "trust" and getattr(self, "pending_trust_job", "connect") == "probe":
            self.submit("probe", "probe_endpoints", connection=self.connection_values())
            return
        if job == "probe":
            page = self.pages["connections"]
            missing = vm.missing_fields(page.values(), bool(page.ops_url.text().strip()))
            if missing:
                page.validation = "Enter " + ", ".join(missing) + " before testing."
                self.pending_credentials = {}
                self.refresh_all()
            else:
                self.connect_environments(self.pending_credentials)
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
            self.page_progress.setText(
                "✕ Checking versions failed. See the reason below."
                if result.get("compatible")
                else "✓ Connected to both appliances"
            )
            if result.get("compatible"):
                self.activity_panel.hide()
            state.connected = result.get("compatible") is None
            state.discovered = result
            state.connect_error = result.get("compatible")
            state.mode = result.get("mode", "simulation")
            state.checks = [tuple(row) for row in result.get("checks", [])]
            if state.connected:
                self.timer.start(state.settings.refresh_seconds * 1000)
                self.submit("runs", "runs")
                self.poll_status()
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
            state.plan_affected = []
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
        elif job == "resume":
            state.plan_run = result
            state.run_id = result["id"]
            state.status = None
            state.status_error = None
            state.start_error = None
            state.resume_error = None
            state.verification = None
            state.verify_error = None
            state.verify_run_id = None
            state.report = None
            state.exported = []
            state.export_error = None
            state.acknowledged = bool(result.get("active_ack"))
            self.load_report(result["id"])
            self.submit("runs", "runs")
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
            if job == "stop" and state.run_id:
                self.poll_status()
        elif job == "discover":
            result["version_check"] = (state.discovered or {}).get("version_check", {})
            state.discovered = result
            state.plan_error = None
            if self.seed_pending:
                self.seed_pending = False
                self.pages["plan"].seed_dates()
        elif job.startswith("verify:"):
            run_id = job[len("verify:") :]
            if state.verify_run_id == run_id:
                state.verify_run_id = None
            if run_id == state.run_id:
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
                    state.plan_affected = []
                # The journal's copy is the plan every page describes: it carries what the
                # engine recorded at start (per-group counts) and the latest verification.
                state.plan_run = result["run"]
                verification = result["run"]["verification"]
                if state.verification is None and verification["outcome"] != "verification_pending":
                    state.verification = verification
        elif job.startswith("export:"):
            if job[len("export:") :] == state.run_id:
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
        if job.split(":")[0] in PROGRESS_JOBS:
            self.page_progress.setText(
                "✕ " + (self.activity_current or "Work") + " failed. " + vm.describe_error(code)
            )
        self.last_error = f"{job}: {code}"
        self.details_button.show()
        if job in {"connect", "trust", "probe"}:
            self.pending_credentials = {}
        if job in {"connect", "probe"}:
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
            state.plan_affected = self.worker.affected.pop("plan", [])
        elif job == "resume":
            state.resume_error = "gui:pre_start:" + vm.describe_error(code)
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
            state.plan_error = code
        elif job.startswith("verify:"):
            run_id = job[len("verify:") :]
            if state.verify_run_id == run_id:
                state.verify_run_id = None
            if run_id == state.run_id:
                state.verify_error = code
        elif job.startswith("export:"):
            if job[len("export:") :] == state.run_id:
                state.export_error = code
        elif job == "selftest":
            state.selftest = None
            state.selftest_error = code
        elif job == "trust":
            state.connect_error = code
        elif job.startswith("report:"):
            if job[len("report:") :] == state.run_id:
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
