from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QWidget

from .. import viewmodel as vm
from ..widgets import (
    KeyValues,
    Page,
    Table,
    banner,
    button,
    card,
    columns,
    elide,
    label,
    row,
    set_banner,
)


class RunSelector(QWidget):
    """Picks the journal run that Verification and Reports describe."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.combo = QComboBox()
        self.combo.setAccessibleName("Run")
        self.combo.setMinimumWidth(420)
        self.combo.currentIndexChanged.connect(self._changed)
        layout = row(label("Run", "label"), self.combo)
        layout.setParent(self)
        self.setLayout(layout.layout())
        self._ids: list[str] = []
        self._labels: list[str] = []

    def _changed(self, index: int) -> None:
        if 0 <= index < len(self._ids) and self._ids[index] != self.app.state.run_id:
            self.app.select_run(self._ids[index])

    def refresh(self) -> None:
        state = self.app.state
        ids = [run["id"] for run in state.runs]
        labels = [vm.run_label(run, index) for index, run in enumerate(state.runs, start=1)]
        if ids != self._ids or labels != self._labels:
            self._ids, self._labels = ids, labels
            self.combo.blockSignals(True)
            self.combo.clear()
            self.combo.addItems(labels)
            self.combo.blockSignals(False)
        if state.run_id in self._ids:
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(self._ids.index(state.run_id))
            self.combo.blockSignals(False)
        self.setVisible(bool(self._ids))


class ReportsPage(Page):
    def __init__(self, app):
        super().__init__("Report")
        self.app = app
        self.runs = RunSelector(app)
        self.body.addWidget(self.runs)
        self.banner = banner("", "", "info")
        self.body.addWidget(self.banner)
        migration_card, migration_layout = card("Migration")
        self.rows = KeyValues()
        migration_layout.addWidget(self.rows)
        migration_layout.addStretch(1)
        config_card, config_layout = card("Configuration")
        self.config = Table(["Rule type", "Result"])
        config_layout.addWidget(self.config)
        self.config_note = label("", "hint", wrap=True)
        config_layout.addWidget(self.config_note)
        config_layout.addStretch(1)
        self.body.addWidget(columns(migration_card, config_card))
        ack_card, ack_layout = card("Latest-data acknowledgment")
        self.ack_text = label("", "body")
        self.ack_recorded = label("", "hint", wrap=True)
        ack_layout.addWidget(self.ack_text)
        ack_layout.addWidget(self.ack_recorded)
        self.body.addWidget(ack_card)
        self.markdown = QCheckBox("Markdown")
        self.markdown.setChecked(True)
        self.json = QCheckBox("JSON")
        self.json.setChecked(True)
        for box in (self.markdown, self.json):
            box.stateChanged.connect(lambda _state: self.refresh())
        self.folder = label("", "hint")
        self.body.addWidget(
            row(
                self.markdown,
                self.json,
                button("Choose export folder…", "", self.choose_folder),
                self.folder,
            )
        )
        self.exported = banner("", "", "ok")
        self.exported.hide()
        self.body.addWidget(self.exported)
        self.finish_body()
        self.note.setText(
            "Plain report includes the plan, API observations, daily counts and exclusions. "
            "No credentials."
        )
        self.export_button = self.add_action("Export selected formats", "primary", self.export)

    def choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Export folder", self.app.state.settings.export_folder
        )
        if chosen:
            self.app.state.settings.export_folder = chosen
            self.app.save_settings()
            self.refresh()

    def export(self) -> None:
        state = self.app.state
        formats = [
            name
            for name, box in (("markdown", self.markdown), ("json", self.json))
            if box.isChecked()
        ]
        if not state.run_id or not formats:
            return
        self.export_button.setEnabled(False)
        self.app.submit(
            "export",
            "export",
            run_id=state.run_id,
            folder=state.settings.export_folder,
            formats=formats,
        )

    def refresh(self) -> None:
        state = self.app.state
        self.runs.refresh()
        elide(self.folder, state.settings.export_folder, 520)
        report = state.report
        if report is None:
            self.set_heading(
                "Report",
                "No run selected"
                if state.connected
                else "Connect both environments and plan a window",
            )
            set_banner(
                self.banner,
                "No report yet",
                "A report becomes available once a window has been previewed.",
                "info",
            )
            self.rows.set_rows([])
            self.config.set_rows([])
            self.ack_text.setText("")
            self.ack_recorded.setText("")
            self.export_button.setEnabled(False)
            self.exported.hide()
            return
        ids = [run["id"] for run in state.runs]
        view = vm.report_view(
            report["run"],
            report["events"],
            state.status,
            state.settings.timezone,
            hosts=self.app.hosts(),
            index=ids.index(report["run"]["id"]) + 1 if report["run"]["id"] in ids else None,
        )
        self.set_heading("Report" + (" · 9.1.1 Preview" if state.preview else ""), view["subtitle"])
        status, title, text = view["banner"]
        set_banner(self.banner, title, text, status)
        self.rows.set_rows(view["rows"])
        if state.config_result:
            self.config.set_rows(
                [(rule, result) for rule, result, _ in vm.config_result_rows(state.config_result)]
            )
            self.config_note.setText(
                "Configuration results are from this session and separate from count verification."
            )
        else:
            self.config.set_rows([])
            self.config_note.setText("No configuration applied in this session.")
        self.ack_text.setText(view["ack_text"])
        self.ack_recorded.setText(view["ack_recorded"])
        any_format = self.markdown.isChecked() or self.json.isChecked()
        self.export_button.setEnabled(any_format and "export" not in self.app.worker.pending)
        self.note.setText(
            "Plain report includes the plan, API observations, daily counts and exclusions. "
            "No credentials."
            if any_format
            else "Choose at least one format to export."
        )
        if state.export_error:
            set_banner(self.exported, "Export failed", vm.describe_error(state.export_error), "bad")
            self.exported.show()
        elif state.exported:
            metrics = self.exported.fontMetrics()
            shown = "<br>".join(
                metrics.elidedText(path, Qt.TextElideMode.ElideMiddle, 720)
                for path in state.exported
            )
            set_banner(self.exported, "Exported", shown, "ok")
            self.exported.setToolTip("\n".join(state.exported))
            self.exported.show()
        else:
            self.exported.hide()
