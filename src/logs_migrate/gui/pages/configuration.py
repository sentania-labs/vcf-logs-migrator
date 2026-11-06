from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QProgressBar, QStackedWidget, QVBoxLayout, QWidget

from .. import viewmodel as vm
from ..widgets import Page, Table, banner, button, card, columns, label, row, set_banner
from . import SNAPSHOT


class ConfigurationPage(Page):
    """Compare, select and apply the four rule types; then show per-rule API results."""

    def __init__(self, app):
        super().__init__("Configuration")
        self.app = app
        self.rows: list[dict] = []
        self.boxes: list[QCheckBox] = []
        self.applying = False
        self._rendered: dict | None = None  # The comparison the checkboxes were built from.
        self._bound: dict[int, object] = {}
        self.stack = QStackedWidget()
        self.body.addWidget(self.stack)
        self.stack.addWidget(self._compare_view())
        self.stack.addWidget(self._results_view())
        self.finish_body()
        self.note.setText(
            "Creates missing rules and updates only the selected difference. Nothing is deleted; "
            "source rules are unchanged."
        )
        self.secondary = self.add_action("Plan history")
        self.primary = self.add_action("Apply selected rules", "primary")
        self._bind(self.secondary, self._plan)
        self._bind(self.primary, self.apply)

    def _compare_view(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.notice = banner("", "", "info")
        self.notice.hide()
        layout.addWidget(self.notice)
        compare, compare_layout = card("Compare and select rules")
        self.table = Table(["Select", "Rule", "Source", "Destination", "Apply action"], fit=(0,))
        compare_layout.addWidget(self.table)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.hide()
        compare_layout.addWidget(self.progress)
        self.destination_only = label("", "hint", wrap=True)
        compare_layout.addWidget(self.destination_only)
        compare_layout.addWidget(row(button("Refresh comparison", "", self.compare)))
        layout.addWidget(compare)
        forwarding, forwarding_layout = card("Forwarding")
        forwarding_layout.addWidget(
            label(
                "<b>TLS targets:</b> Apply sends <code>acceptCert: true</code>, which can add the "
                "target certificate to destination trust. Review each target before applying.",
                "body",
                wrap=True,
            )
        )
        forwarding_layout.addWidget(
            label(
                "Forwarders run from the new cluster. 9.0 refuses a forwarder pointing at itself; "
                "the tool does not retarget rules.",
                "hint",
                wrap=True,
            )
        )
        forwarding_layout.addStretch(1)
        partition, partition_layout = card("Partition activation")
        partition_layout.addWidget(
            label(
                "<b>New partitions need one destination cluster restart</b> before routing. "
                "Apply writes definitions only; it does not restart services.",
                "body",
                wrap=True,
            )
        )
        partition_layout.addWidget(
            label(
                "Restart through your normal maintenance procedure, then compare again.",
                "hint",
                wrap=True,
            )
        )
        partition_layout.addStretch(1)
        layout.addWidget(columns(forwarding, partition))
        self.filter_warning = banner(
            "Selected filter will drop matching new events",
            "Masking and filtering affect ingest; they are not a promise to rewrite imported "
            "history.",
            "warn",
        )
        self.filter_warning.hide()
        layout.addWidget(self.filter_warning)
        layout.addWidget(label(SNAPSHOT, "hint", wrap=True))
        return holder

    def _results_view(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.result_banner = banner("", "", "ok")
        layout.addWidget(self.result_banner)
        results, results_layout = card("Results")
        self.results = Table(["Rule", "API result", "Next step"])
        results_layout.addWidget(self.results)
        layout.addWidget(results)
        self.restart_card, restart_layout = card("Partition restart pending")
        self.restart_text = label("", "body", wrap=True)
        restart_layout.addWidget(self.restart_text)
        restart_layout.addWidget(row(button("Compare again", "", self.compare)))
        layout.addWidget(self.restart_card)
        next_card, next_layout = card("Next: cut over ingest, then migrate history")
        next_layout.addWidget(
            label(
                "Point the ingest VIP and agents at the destination through your normal change "
                "process. Open groups on the source stay excluded from history migration.",
                "body",
                wrap=True,
            )
        )
        next_layout.addWidget(
            label(
                "Applied rules do not migrate historical events. No source rules were changed.",
                "hint",
                wrap=True,
            )
        )
        layout.addWidget(next_card)
        layout.addWidget(label(SNAPSHOT, "hint", wrap=True))
        return holder

    def _bind(self, button, slot) -> None:
        """Give a footer button exactly one handler."""
        if self._bound.get(id(button)) is slot:
            return
        if id(button) in self._bound:
            button.clicked.disconnect()
        button.clicked.connect(slot)
        self._bound[id(button)] = slot

    def selection(self) -> list[str]:
        return [
            row["selector"]
            for row, box in zip(self.rows, self.boxes, strict=True)
            if box.isChecked()
        ]

    def compare(self) -> None:
        self.app.state.config_result = None
        self.app.submit("config_compare", "config_compare")
        self.stack.setCurrentIndex(0)
        self.refresh()

    def apply(self) -> None:
        selection = self.selection()
        if not selection:
            return
        self.applying = True
        self.progress.show()
        self.set_heading(
            "Applying configuration",
            f"Applying {len(selection)} selected rule{'s' if len(selection) != 1 else ''} "
            "to the destination",
        )
        self.primary.setEnabled(False)
        self.app.submit("config_apply", "config_apply", selection=selection)

    def _update_actions(self) -> None:
        count = len(self.selection())
        self.primary.setText(
            f"Apply {count} selected rule{'s' if count != 1 else ''}"
            if count
            else "Apply selected rules"
        )
        self.primary.setEnabled(count > 0 and not self.applying)
        filters = any(
            r["kind"] == "filters" and "enabled" in r["changed"] and box.isChecked()
            for r, box in zip(self.rows, self.boxes, strict=True)
        )
        self.filter_warning.setVisible(filters)

    def refresh(self) -> None:
        state = self.app.state
        if state.config_result is not None:
            self.applying = False
            self.progress.hide()
            self._render_results(state.config_result)
            return
        self.stack.setCurrentIndex(0)
        self.secondary.setText("Plan history")
        self._bind(self.secondary, self._plan)
        self._bind(self.primary, self.apply)
        self.primary.show()
        discovered = state.discovered
        if discovered is None:
            self.set_heading("Configuration", "Connect both environments to compare rules")
        elif not self.applying:
            self.set_heading(
                "Configuration",
                f"{vm.format_version(discovered['source']['version'])} → "
                f"{vm.format_version(discovered['destination']['version'])} · "
                "forwarding, partitions, masking and filtering",
            )
        if state.comparison_error:
            set_banner(
                self.notice,
                "Comparison unavailable",
                vm.describe_error(state.comparison_error),
                "bad",
            )
            self.notice.show()
        elif state.comparison is None:
            set_banner(
                self.notice,
                "Not compared yet",
                "Refresh the comparison to read both rule inventories."
                if state.connected
                else "Connect both environments first.",
                "info",
            )
            self.notice.show()
        else:
            self.notice.hide()
        if state.comparison is self._rendered and self.boxes:
            # Same comparison: keep whatever the operator ticked or unticked.
            self._update_actions()
            return
        self._rendered = state.comparison
        self.rows = vm.config_rows(state.comparison)
        self.boxes = []
        widgets = {}
        for index, entry in enumerate(self.rows):
            box = QCheckBox()
            box.setChecked(entry["selected"])
            box.setEnabled(entry["selectable"])
            box.stateChanged.connect(lambda _state: self._update_actions())
            holder = QWidget()
            layout = QVBoxLayout(holder)
            layout.setContentsMargins(8, 0, 0, 0)
            layout.addWidget(box, 0, Qt.AlignmentFlag.AlignCenter)
            widgets[(index, 0)] = holder
            self.boxes.append(box)
        self.table.set_rows(
            [
                ("", entry["rule"], entry["source"], entry["destination"], entry["action"])
                for entry in self.rows
            ],
            widgets,
        )
        extra = state.comparison["destination_only"] if state.comparison else []
        self.destination_only.setText(
            "Destination-only rules are preserved: "
            + ", ".join(f"{vm.KIND_LABEL.get(r['type'], r['type'])} · {r['name']}" for r in extra)
            if extra
            else ""
        )
        self.destination_only.setVisible(bool(extra))
        self._update_actions()

    def _render_results(self, result: dict) -> None:
        self.stack.setCurrentIndex(1)
        status, title, text = vm.config_banner(result)
        set_banner(self.result_banner, title, text, status)
        self.set_heading(
            "Configuration results" + ("" if result["complete"] else " · attention needed"),
            "Per-rule results from the destination API",
        )
        self.results.set_rows(vm.config_result_rows(result))
        self.restart_card.setVisible(bool(result.get("restart_required")))
        self.restart_text.setText(result.get("maintenance") or "")
        self.note.setText(
            "No restart or rollback is performed automatically. A failed rule does not reapply "
            "other successful rules."
        )
        self.secondary.setText("Compare again")
        self._bind(self.secondary, self.compare)
        self.primary.setText("Plan history")
        self.primary.setEnabled(True)
        self._bind(self.primary, self._plan)
        self.primary.show()

    def _plan(self) -> None:
        self.app.show_page("plan")

    def showEvent(self, event):  # noqa: N802 (Qt override)
        super().showEvent(event)
        if (
            self.app.state.connected
            and self.app.state.comparison is None
            and not self.app.state.comparison_error
        ):
            self.compare()
