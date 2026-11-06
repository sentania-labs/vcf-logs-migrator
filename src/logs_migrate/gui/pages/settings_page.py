from __future__ import annotations

from zoneinfo import available_timezones

from PySide6.QtWidgets import QComboBox, QFileDialog, QLineEdit, QSpinBox

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
            "Display, polling, verification limits, local report destination and simulator"
        )
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
        display_layout.addWidget(field("Display timezone", self.timezone))
        display_layout.addStretch(1)
        api, api_layout = card("API and verification")
        self.refresh_seconds = QSpinBox()
        self.refresh_seconds.setRange(1, 3600)
        self.refresh_seconds.setSuffix(" s")
        self.budget = QSpinBox()
        self.budget.setRange(1, 1440)
        self.budget.setSuffix(" min")
        self.poll = QSpinBox()
        self.poll.setRange(0, 3600)
        self.poll.setSuffix(" s")
        api_layout.addWidget(field("Status refresh (seconds)", self.refresh_seconds))
        api_layout.addWidget(field("Verification retry budget (minutes)", self.budget))
        api_layout.addWidget(field("Count query interval (seconds)", self.poll))
        api_layout.addWidget(
            label(
                "Two agreeing complete count observations are required; "
                "the budget bounds the retries.",
                "hint",
                wrap=True,
            )
        )
        self.body.addWidget(columns(display, api))
        reports, reports_layout = card("Local reports")
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
        simulator, simulator_layout = card("Simulator and journal")
        self.fixture = QLineEdit()
        self.fixture.setPlaceholderText("tests/fixtures/sealed-populated.fixture")
        self.journal = QLineEdit()
        simulator_layout.addWidget(field("Simulator fixture file", self.fixture))
        simulator_layout.addWidget(row(button("Choose fixture…", "", self.choose_fixture)))
        simulator_layout.addWidget(field("Private journal file", self.journal))
        simulator_layout.addWidget(row(button("Choose journal…", "", self.choose_journal)))
        simulator_layout.addWidget(
            label(
                "This build runs the deterministic simulator only; "
                "live appliance connections are not qualified. "
                "The journal keeps plans, sent groups and count evidence, never credentials.",
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
        self.theme.setCurrentIndex(THEMES.index(settings.theme))
        self.timezone.setCurrentText(settings.timezone)
        self.refresh_seconds.setValue(settings.refresh_seconds)
        self.budget.setValue(settings.verify_budget_minutes)
        self.poll.setValue(settings.count_poll_seconds)
        self.export_folder.setText(settings.export_folder)
        self.fixture.setText(settings.fixture_path)
        self.journal.setText(settings.journal_path)

    def cancel(self) -> None:
        self.load()
        self.app.show_page("overview")

    def save(self) -> None:
        current = self.app.state.settings
        updated = Settings(
            theme=THEMES[self.theme.currentIndex()],
            timezone=self.timezone.currentText().strip() or "UTC",
            refresh_seconds=self.refresh_seconds.value(),
            verify_budget_minutes=self.budget.value(),
            count_poll_seconds=self.poll.value(),
            export_folder=self.export_folder.text().strip(),
            journal_path=self.journal.text().strip(),
            fixture_path=self.fixture.text().strip(),
            source_host=current.source_host,
            source_ssh_user=current.source_ssh_user,
            source_api_user=current.source_api_user,
            destination_host=current.destination_host,
            destination_api_user=current.destination_api_user,
            ops_url=current.ops_url,
        )
        self.app.apply_settings(updated)
        self.app.show_page("overview")

    def refresh(self) -> None:
        pass
