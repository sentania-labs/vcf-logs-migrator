from __future__ import annotations

from zoneinfo import available_timezones

from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QLineEdit, QSpinBox

from .. import viewmodel as vm
from ..settings import THEMES, Settings
from ..widgets import Page, button, card, columns, field, label, row

COMMON_ZONES = (
    "UTC",
    "America/Chicago",
    "America/New_York",
    "America/Los_Angeles",
    "Europe/London",
)


class SettingsPage(Page):
    """Every client setting is editable here; the INI file is storage, not the interface."""

    def __init__(self, app):
        super().__init__("Settings")
        self.app = app
        self.subtitle.setText(
            "Display, polling, verification limits, local report destination and mode"
        )
        self.hints: dict[str, object] = {}
        display, display_layout = card("Display")
        self.theme = QComboBox()
        self.theme.addItems([t.title() for t in THEMES])
        self.timezone = QComboBox()
        self.timezone.setEditable(True)
        zones = list(COMMON_ZONES) + sorted(
            z for z in available_timezones() if z not in COMMON_ZONES
        )
        self.timezone.addItems(zones)
        display_layout.addWidget(field("Theme", self.theme))
        display_layout.addWidget(field("Display timezone", self.timezone, self._hint("timezone")))
        display_layout.addStretch(1)
        api, api_layout = card("API and verification")
        self.refresh_seconds = QSpinBox()
        self.refresh_seconds.setRange(1, 3600)
        self.refresh_seconds.setSuffix(" s")
        self.budget = QSpinBox()
        self.budget.setRange(1, 1440)
        self.budget.setSuffix(" min")
        self.poll = QSpinBox()
        self.poll.setRange(1, 3600)
        self.poll.setSuffix(" s")
        api_layout.addWidget(field("Status refresh (seconds)", self.refresh_seconds))
        api_layout.addWidget(field("Verification retry budget (minutes)", self.budget))
        api_layout.addWidget(field("Count query interval (seconds)", self.poll))
        api_layout.addWidget(
            label(
                "Two agreeing complete count observations are required; "
                "the budget bounds the retries. Storage headroom is not a setting: the plan "
                "uses each destination node's reported repository budget.",
                "hint",
                wrap=True,
            )
        )
        self.body.addWidget(columns(display, api))
        reports, reports_layout = card("Local reports")
        self.debug_logging = QCheckBox("Enable detailed troubleshooting log")
        reports_layout.addWidget(self.debug_logging)
        self.export_folder = QLineEdit()
        reports_layout.addWidget(field("Export folder", self.export_folder))
        reports_layout.addWidget(row(button("Choose folder…", "", self.choose_folder)))
        reports_layout.addWidget(
            label(
                "Passwords and tokens stay in session memory. "
                "No configuration-file editing is required.",
                "hint",
                wrap=True,
            )
        )
        self.body.addWidget(reports)
        simulator, simulator_layout = card("Appliances or simulator, and journal")
        self.fixture = QLineEdit()
        self.fixture.setPlaceholderText("Empty: connect to real appliances from Connections")
        self.journal = QLineEdit()
        simulator_layout.addWidget(
            field("Simulator fixture file (optional)", self.fixture, self._hint("fixture"))
        )
        simulator_layout.addWidget(row(button("Choose fixture…", "", self.choose_fixture)))
        simulator_layout.addWidget(
            field("Private journal file", self.journal, self._hint("journal"))
        )
        simulator_layout.addWidget(row(button("Choose journal…", "", self.choose_journal)))
        simulator_layout.addWidget(
            label(
                "Leave the fixture empty to connect to real appliances; set one to drive the "
                "deterministic simulator for demos. "
                "The journal keeps plans, sent groups and count evidence, never credentials. "
                "Changing the fixture or journal while connected disconnects the session; "
                "reconnect from Connections.",
                "hint",
                wrap=True,
            )
        )
        self.body.addWidget(simulator)
        self.finish_body()
        self.note.setText(
            "Settings affect this client; they do not change appliance configuration."
        )
        self.add_action("Cancel", "", self.cancel)
        self.add_action("Save settings", "primary", self.save)
        self.load()

    def _hint(self, name: str):
        widget = label("", "hint", "bad", wrap=True)
        widget.hide()
        self.hints[name] = widget
        return widget

    def choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Export folder", self.export_folder.text())
        if chosen:
            self.export_folder.setText(chosen)

    def choose_fixture(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Simulator fixture", self.fixture.text(), "Fixtures (*.fixture);;All files (*)"
        )
        if chosen:
            self.fixture.setText(chosen)

    def choose_journal(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Private journal", self.journal.text(), "SQLite (*.sqlite);;All files (*)"
        )
        if chosen:
            self.journal.setText(chosen)

    def load(self) -> None:
        settings = self.app.state.settings
        self.debug_logging.setChecked(settings.debug_logging)
        self.theme.setCurrentIndex(THEMES.index(settings.theme))
        self.timezone.setCurrentText(settings.timezone)
        self.refresh_seconds.setValue(settings.refresh_seconds)
        self.budget.setValue(settings.verify_budget_minutes)
        self.poll.setValue(max(1, settings.count_poll_seconds))
        self.export_folder.setText(settings.export_folder)
        self.fixture.setText(settings.fixture_path)
        self.journal.setText(settings.journal_path)
        self.show_problems({})

    def show_problems(self, problems: dict[str, str]) -> None:
        inputs = {"fixture": self.fixture, "journal": self.journal, "timezone": self.timezone}
        for name, widget in self.hints.items():
            widget.setText(problems.get(name, ""))
            widget.setVisible(name in problems)
            field_widget = inputs[name]
            field_widget.setProperty("status", "bad" if name in problems else "")
            field_widget.style().unpolish(field_widget)
            field_widget.style().polish(field_widget)
        self.note.setText(
            "Fix the highlighted fields, then save."
            if problems
            else "Settings affect this client; they do not change appliance configuration."
        )

    def cancel(self) -> None:
        self.load()
        self.app.leave_settings()

    def save(self) -> bool:
        """Validate inline; nothing is written while a field is wrong."""
        current = self.app.state.settings
        fixture = self.fixture.text().strip()
        journal = self.journal.text().strip()
        timezone = self.timezone.currentText().strip() or "UTC"
        problems = vm.validate_settings(fixture, journal, timezone)
        self.show_problems(problems)
        if problems:
            return False
        updated = Settings(
            debug_logging=self.debug_logging.isChecked(),
            theme=THEMES[self.theme.currentIndex()],
            timezone=timezone,
            refresh_seconds=self.refresh_seconds.value(),
            verify_budget_minutes=self.budget.value(),
            count_poll_seconds=self.poll.value(),
            export_folder=self.export_folder.text().strip(),
            journal_path=journal,
            fixture_path=fixture,
            source_host=current.source_host,
            source_ssh_user=current.source_ssh_user,
            source_api_user=current.source_api_user,
            destination_host=current.destination_host,
            destination_api_user=current.destination_api_user,
            destination_ssh_user=current.destination_ssh_user,
            ops_url=current.ops_url,
        )
        self.app.apply_settings(updated)
        self.app.leave_settings()
        return True

    def refresh(self) -> None:
        pass
