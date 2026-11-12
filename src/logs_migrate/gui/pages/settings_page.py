"""Settings. Every change is saved as it is made, and says so."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from zoneinfo import available_timezones

from PySide6.QtWidgets import QCheckBox, QComboBox, QGridLayout, QLineEdit, QWidget

from .. import viewmodel as vm
from ..settings import RESETTABLE, Settings, local_timezone
from ..widgets import Page, button, card, label, link, rich, row, set_prop, set_status

COMMON_ZONES = (
    "America/Chicago",
    "America/New_York",
    "America/Denver",
    "America/Los_Angeles",
    "Europe/London",
    "UTC",
)


class SettingsPage(Page):
    def __init__(self, app):
        super().__init__(
            app,
            "Settings",
            "Changes are saved as you make them. Passwords are never stored.",
        )
        self.saved: dict[str, object] = {}
        self.notes: dict[str, object] = {}
        self.detected = local_timezone()

        everyday, layout = card("Everyday")
        grid = self._grid(layout)
        self.themes = {
            "light": button("Light", "", lambda: self.set_theme("light")),
            "dark": button("Dark", "", lambda: self.set_theme("dark")),
        }
        self._line(grid, 0, "Colours", "theme", *self.themes.values())
        self.timezone = QComboBox()
        self.timezone.setAccessibleName("Show clock times in")
        zones = [self.detected] + [z for z in COMMON_ZONES if z != self.detected]
        zones += sorted(z for z in available_timezones() if z not in zones)
        self.zones = zones
        self.timezone.addItems([f"{zones[0]} (detected)"] + zones[1:])
        self.timezone.activated.connect(self._zone_chosen)
        self._line(grid, 1, "Show clock times in", "timezone", self.timezone)
        self.folder = label("", "path")
        self._line(
            grid,
            2,
            "Save reports in",
            "export_folder",
            self.folder,
            button("Change…", "", self.change_folder),
            button("Open folder", "", self.open_folder),
        )

        help_card, layout = card("Help with problems")
        grid = self._grid(layout)
        self.debug = QCheckBox(
            "Keep a detailed log of what the app does. Never includes passwords."
        )
        self.debug.setAccessibleName("Keep a detailed log")
        self.debug.toggled.connect(self._debug_toggled)
        self._line(grid, 0, "Detailed log", "debug_logging", self.debug)
        self.trouble_note = rich("", "saved", lambda _href: self.open_folder())
        self._line(
            grid,
            1,
            "Troubleshooting file",
            "trouble",
            button("Save troubleshooting file…", "", self.app.save_troubleshooting),
            label("One file to send to whoever is helping you.", "subtle"),
        )

        tuning, layout = card("Fine tuning", ("", "Rarely needed"))
        grid = self._grid(layout)
        self.refresh_seconds = QLineEdit()
        self.refresh_seconds.setFixedWidth(70)
        self.refresh_seconds.setAccessibleName("Refresh copy progress every (seconds)")
        self._line(
            grid,
            0,
            "Refresh copy progress every",
            "refresh_seconds",
            self.refresh_seconds,
            label("seconds", "text"),
        )
        self.give_up = QLineEdit()
        self.give_up.setFixedWidth(70)
        self.give_up.setAccessibleName("When checking, keep trying for (minutes)")
        self._line(
            grid,
            1,
            "When checking, keep trying for",
            "verify_budget_minutes",
            self.give_up,
            label("minutes at most", "text"),
            label("A busy destination can take a while to give a steady count.", "subtle"),
        )
        self.numbers = {
            "refresh_seconds": (self.refresh_seconds, vm.REFRESH_RANGE),
            "verify_budget_minutes": (self.give_up, vm.GIVE_UP_RANGE),
        }
        for name, (edit, _range) in self.numbers.items():
            edit.textEdited.connect(lambda text, n=name: self._number_typed(n, text))
            edit.editingFinished.connect(lambda n=name: self._number_left(n))
        self.record = label("", "path")
        self._line(
            grid,
            2,
            "Past runs are stored in",
            "journal_path",
            self.record,
            button("Move…", "", self.move_record),
        )
        reset = link("Put all settings back to how they came", self.ask_reset)
        self.set_body(everyday, help_card, tuning, row(reset))

    def _grid(self, layout) -> QGridLayout:
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(7)
        grid.setColumnMinimumWidth(0, 230)
        grid.setColumnStretch(1, 1)
        layout.addWidget(holder)
        return grid

    def _line(self, grid, index: int, caption: str, name: str, *widgets) -> None:
        grid.addWidget(label(caption, "soft"), index, 0)
        note = rich("", "saved", lambda _href: self.open_folder())
        note.setWordWrap(False)
        self.notes[name] = note
        grid.addWidget(row(*widgets, note), index, 1)

    # Saving ------------------------------------------------------------------------------

    def _saved(self, name: str) -> None:
        self.saved[name] = True
        self.app.apply_settings()

    def set_theme(self, name: str) -> None:
        self.app.state.settings.theme = name
        self._saved("theme")

    def _zone_chosen(self, index: int) -> None:
        self.app.state.settings.timezone = self.zones[index]
        self._saved("timezone")

    def _debug_toggled(self, on: bool) -> None:
        if on != self.app.state.settings.debug_logging:
            self.app.state.settings.debug_logging = on
            self._saved("debug_logging")

    def change_folder(self) -> None:
        settings = self.app.state.settings
        chosen = self.app.choose_folder("Choose a folder for reports", settings.export_folder)
        if not chosen:
            return
        problem = vm.folder_problem(chosen)
        if problem:
            self.saved.pop("export_folder", None)
            self.problems["export_folder"] = problem
            self.refresh()
            return
        self.problems.pop("export_folder", None)
        settings.export_folder = chosen
        self._saved("export_folder")

    def open_folder(self) -> None:
        folder = Path(self.app.state.settings.export_folder).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.app.open_file(str(folder))

    problems: dict[str, str] = {}

    def _number_typed(self, name: str, text: str) -> None:
        settings = self.app.state.settings
        edit, (low, high) = self.numbers[name]
        problem = vm.number_problem(text, low, high, getattr(settings, name))
        if problem:
            self.problems[name] = problem
            self.saved.pop(name, None)
            self.refresh()
            return
        self.problems.pop(name, None)
        setattr(settings, name, int(text.strip()))
        self._saved(name)

    def _number_left(self, name: str) -> None:
        """Leaving a rejected number clears it: nothing stays red over a value in use."""
        if name in self.problems:
            current = getattr(self.app.state.settings, name)
            del self.problems[name]
            self.refresh()
            self.app.toast(f"That number was out of range, so it is still {current}.")

    def move_record(self) -> None:
        def choose():
            folder = self.app.choose_folder(
                "Choose a folder for past runs",
                str(Path(self.app.state.settings.journal_path).parent),
            )
            if folder:
                self.app.move_record(folder)

        self.app.ask(
            "Move where past runs are stored?",
            [
                "The list of past runs and their results is kept in one folder on this "
                "computer. Moving it signs you out of both systems; a running copy carries on "
                "at the destination. You then pick the new folder."
            ],
            [("Leave it where it is", None, "primary"), ("Sign out and move…", choose)],
        )

    def ask_reset(self) -> None:
        self.app.ask(
            "Put all settings back to how they came?",
            [
                "This resets colours, time zone, the reports folder and the fine-tuning "
                "numbers. It does not touch past runs, saved reports or the log systems."
            ],
            [("Cancel", None, "primary"), ("Reset settings", self.reset)],
        )

    def reset(self) -> None:
        settings = self.app.state.settings
        fresh = Settings(timezone=self.detected)
        for item in fields(Settings):
            if item.name in RESETTABLE:
                setattr(settings, item.name, getattr(fresh, item.name))
        self.saved = {}
        self.problems = {}
        self.app.apply_settings()
        self.app.toast("Settings are back to how they came")

    def arrive(self) -> None:
        self.saved = {}
        self.problems = {}

    # Drawing -----------------------------------------------------------------------------

    def refresh(self) -> None:
        state = self.app.state
        settings = state.settings
        for name, widget in self.themes.items():
            set_prop(widget, "seg", "on" if settings.theme == name else "off")
        zone = vm.zone_name(settings.timezone)
        if zone in self.zones:
            self.timezone.setCurrentIndex(self.zones.index(zone))
        self.folder.setText(settings.export_folder)
        self.record.setText(str(Path(settings.journal_path).parent))
        self.debug.blockSignals(True)
        self.debug.setChecked(settings.debug_logging)
        self.debug.blockSignals(False)
        for name, (edit, _range) in self.numbers.items():
            bad = name in self.problems
            if not bad and not edit.hasFocus():
                edit.setText(str(getattr(settings, name)))
            elif not bad and edit.text().strip() != str(getattr(settings, name)):
                edit.setText(str(getattr(settings, name)))
            set_status(edit, "bad" if bad else "")
        for name, note in self.notes.items():
            if name in self.problems:
                note.setText(self.problems[name])
                set_prop(note, "lattice", "hint")
                set_status(note, "bad")
            elif name == "trouble":
                saved = getattr(state, "trouble_saved", None)
                set_prop(note, "lattice", "saved")
                set_status(note, "")
                note.setText(
                    f"✓ Saved “{Path(saved).name}” in your reports folder. "
                    '<a href="open">Open folder</a>'
                    if saved
                    else ""
                )
            else:
                set_prop(note, "lattice", "saved")
                set_status(note, "")
                note.setText("✓ Saved" if self.saved.get(name) else "")
        back = vm.STEP_NAMES.get(self.app.return_page, "Connect")
        self.set_footer(
            "These settings affect only this app on this computer, not the log systems.",
            [(f"← Back to {back}", self.app.leave_settings, "primary")],
        )
