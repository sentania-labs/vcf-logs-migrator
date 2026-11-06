from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QDateEdit,
    QGridLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import viewmodel as vm
from ..widgets import (
    KeyValues,
    Page,
    Table,
    TimelineBar,
    badge,
    banner,
    button,
    card,
    columns,
    field,
    label,
    row,
    set_banner,
)
from . import SNAPSHOT

# Both codes mean the selection is already on the destination; neither is retried.
BLOCKED = {"overlapping_transfer", "bucket_identity_conflict"}


def _qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _pydate(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


class PlanPage(Page):
    """Dates in, whole sealed groups out; Start sends the window to the destination API once."""

    def __init__(self, app):
        super().__init__("Discovery & Plan")
        self.app = app
        self.stack = QStackedWidget()
        self.body.addWidget(self.stack)
        self.stack.addWidget(self._plan_view())
        self.stack.addWidget(self._blocked_view())
        self.finish_body()
        self.refresh_button = self.add_action("Refresh preview", "", self.preview)
        self.start_button = self.add_action("Start migration", "primary", self.start)

    def _plan_view(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.badges = row(
            badge("No migration service stop", "info"), badge("Active tail excluded", "warn")
        )
        layout.addWidget(self.badges)
        self.error = banner("", "", "bad")
        self.error.hide()
        layout.addWidget(self.error)

        window_card, window_layout = card("Choose a window")
        self.start_date = QDateEdit()
        self.end_date = QDateEdit()
        for edit in (self.start_date, self.end_date):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
        window_layout.addWidget(
            columns(
                field("Start date (UTC)", self.start_date),
                field("End date, inclusive (UTC)", self.end_date),
            )
        )
        presets = [
            button(name, "", lambda _checked=False, n=name: self.apply_preset(n))
            for name, _ in vm.PRESETS
        ]
        window_layout.addWidget(row(*presets))
        self.bars: dict[str, TimelineBar] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        for index, caption in enumerate(
            [
                "Source history",
                "Requested dates",
                "Included copy",
                "Destination overlap",
                "Latest excluded",
            ]
        ):
            bar = TimelineBar(caption)
            self.bars[caption] = bar
            grid.addWidget(label(caption, "muted"), index, 0)
            grid.addWidget(bar, index, 1)
        grid_holder = QWidget()
        grid_holder.setLayout(grid)
        window_layout.addWidget(grid_holder)
        self.included = label("", "body", wrap=True)
        self.sealed = label("", "hint", wrap=True)
        window_layout.addWidget(self.included)
        window_layout.addWidget(self.sealed)

        groups_card, groups_layout = card("Groups in this window")
        self.groups_title = label("", "subhead")
        groups_layout.addWidget(self.groups_title)
        self.groups = Table(["Group", "Dates UTC", "Size / events", "Action"])
        groups_layout.addWidget(self.groups)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)
        left_layout.addWidget(window_card)
        left_layout.addWidget(groups_card)
        left_layout.addStretch(1)

        checks_card, checks_layout = card("Checks")
        self.checks = KeyValues()
        checks_layout.addWidget(self.checks)
        storage_card, storage_layout = card("Destination storage")
        self.storage = Table(["Node", "Available", "Required", "Spare"])
        storage_layout.addWidget(self.storage)
        storage_layout.addWidget(
            label(
                "Within each node's repository budget. The API assigns groups to nodes.",
                "hint",
                wrap=True,
            )
        )
        retention_card, retention_layout = card("Retention and warnings")
        self.warnings = label("", "hint", wrap=True)
        retention_layout.addWidget(self.warnings)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)
        right_layout.addWidget(checks_card)
        right_layout.addWidget(storage_card)
        right_layout.addWidget(retention_card)
        right_layout.addStretch(1)
        self.split = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.split.setContentsMargins(0, 0, 0, 0)
        self.split.setSpacing(16)
        self.split.addWidget(left, 7)
        self.split.addWidget(right, 3)
        self.split_holder = QWidget()
        self.split_holder.setLayout(self.split)
        layout.addWidget(self.split_holder)
        layout.addWidget(label(SNAPSHOT, "hint", wrap=True))
        self.ack = QCheckBox("I understand you aren't going to copy the latest data.")
        self.ack.stateChanged.connect(self._ack_changed)
        layout.addWidget(self.ack)
        return holder

    def _blocked_view(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(
            banner(
                "Migration blocked · overlapping history",
                "The requested window selects groups already present on the destination or held by "
                "an in-flight task. No second API request will be sent.",
                "bad",
            )
        )
        evidence, evidence_layout = card("Overlap evidence")
        self.overlap_rows = KeyValues()
        evidence_layout.addWidget(self.overlap_rows)
        layout.addWidget(evidence)
        next_card, next_layout = card("Next step")
        next_layout.addWidget(
            label(
                "Choose a window that selects only history not already imported, then refresh the "
                "preview. If no separate window is possible, export the evidence for the appliance "
                "administrator.",
                "body",
                wrap=True,
            )
        )
        next_layout.addWidget(
            label(
                "The tool never deletes, overwrites or repairs destination history "
                "to clear a conflict.",
                "body",
                wrap=True,
            )
        )
        layout.addWidget(next_card)
        layout.addStretch(1)
        return holder

    def _ack_changed(self, _state) -> None:
        self.app.state.acknowledged = self.ack.isChecked()
        self.refresh()

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        # Below 1200px the side cards stack under the window card, as the 1024x700 study shows.
        narrow = self.width() < 1200
        direction = QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        if self.split.direction() != direction:
            self.split.setDirection(direction)
            # A hidden stack page does not invalidate its parents' caches, so the stacked
            # widget would keep the wide two-column minimum until the page is shown again.
            self.split_holder.updateGeometry()
            self.stack.widget(0).layout().invalidate()
            self.stack.layout().invalidate()
            self.scroll.widget().updateGeometry()

    def apply_preset(self, name: str) -> None:
        extent = (
            vm.history_extent(self.app.state.discovered["source"])
            if self.app.state.discovered
            else None
        )
        days = vm.preset_days(name, extent)
        if days is None:
            return
        self.start_date.setDate(_qdate(days[0]))
        self.end_date.setDate(_qdate(days[1]))
        self.preview()

    def preview(self) -> None:
        state = self.app.state
        if not state.connected:
            return
        window = vm.window_from_days(_pydate(self.start_date.date()), _pydate(self.end_date.date()))
        if window is None:
            state.plan_error = "invalid_input"
            state.plan_run = None
            self.refresh()
            return
        settings = state.settings
        self.app.submit(
            "plan",
            "plan",
            start=window.start,
            end=window.end,
            attempts=settings.verify_attempts,
            poll_seconds=float(settings.count_poll_seconds),
        )

    def start(self) -> None:
        state = self.app.state
        if state.plan_run is None:
            return
        self.start_button.setEnabled(False)
        self.start_button.setText("Starting…")
        self.app.submit(
            "start", "start", run_id=state.plan_run["id"], acknowledged=state.acknowledged
        )

    def seed_dates(self) -> None:
        extent = (
            vm.history_extent(self.app.state.discovered["source"])
            if self.app.state.discovered
            else None
        )
        days = vm.preset_days("All history", extent)
        if days:
            self.start_date.setDate(_qdate(days[0]))
            self.end_date.setDate(_qdate(days[1]))

    def _update_actions(self) -> None:
        state = self.app.state
        view = vm.plan_view(state.plan_run, state.discovered) if state.plan_run else None
        enabled, note = vm.start_state(view, state.acknowledged, state.connected)
        if state.start_error:
            note = vm.describe_error(state.start_error) + " No API request was submitted."
        self.note.setText(note)
        self.start_button.setEnabled(enabled and "start" not in self.app.worker.pending)
        self.start_button.setText("Start migration" if enabled else "Start migration · unavailable")
        self.refresh_button.setEnabled(state.connected and "plan" not in self.app.worker.pending)

    def refresh(self) -> None:
        state = self.app.state
        discovered = state.discovered
        self.ack.blockSignals(True)
        self.ack.setChecked(state.acknowledged)
        self.ack.blockSignals(False)
        if state.plan_error in BLOCKED:
            self.stack.setCurrentIndex(1)
            self.set_heading(
                "Selected history overlaps a previous import",
                "Applies to both 9.0 and 9.1.1 destinations",
            )
            self.overlap_rows.set_rows(
                [
                    (
                        "Request",
                        f"{self.start_date.date().toString('yyyy-MM-dd')} – "
                        f"{self.end_date.date().toString('yyyy-MM-dd')} UTC",
                    ),
                    ("Rule", "Whole groups already received or in flight are never sent twice"),
                    (
                        "Why dates alone are insufficient",
                        "The API imports whole overlapping groups",
                    ),
                ]
            )
            self.note.setText(
                "No bypass or overlapping retry action is offered. "
                "Change the dates, then refresh the preview."
            )
            self.start_button.setEnabled(False)
            self.start_button.setText("Start migration · unavailable")
            self.refresh_button.setEnabled(True)
            return
        self.stack.setCurrentIndex(0)
        if state.plan_error:
            set_banner(
                self.error, "Preview unavailable", vm.describe_error(state.plan_error), "bad"
            )
            self.error.show()
        else:
            self.error.hide()
        if state.plan_run is None:
            self.set_heading(
                "Discovery & Plan",
                "Choose dates; whole sealed groups are selected"
                if state.connected
                else "Connect both environments to preview history",
            )
            extent = vm.history_extent(discovered["source"]) if discovered else None
            self.bars["Source history"].set_span(
                extent,
                extent,
                vm.format_day_range(*extent, suffix="") if extent else "Not connected",
                "solid",
            )
            for caption in (
                "Requested dates",
                "Included copy",
                "Destination overlap",
                "Latest excluded",
            ):
                self.bars[caption].set_span(extent, None, "No preview yet", "soft")
            self.included.setText("")
            self.sealed.setText("")
            self.groups_title.setText("No preview yet")
            self.groups.set_rows([])
            self.checks.set_rows([("Preview", "Not run")])
            self.storage.set_rows([])
            self.warnings.setText("")
            self.ack.setVisible(False)
            self._update_actions()
            return
        view = vm.plan_view(state.plan_run, discovered)
        self.set_heading(
            "Discovery & Plan",
            view["subtitle"]
            if not state.acknowledged or not view["needs_ack"]
            else "Ready · snapshots reminded · latest-data exclusion acknowledged",
        )
        extent = view["extent"] or view["coverage"]
        self.bars["Source history"].set_span(
            extent, extent, vm.format_day_range(*extent, suffix=""), "solid"
        )
        self.bars["Requested dates"].set_span(
            extent, view["window"], view["requested"].replace(" UTC", ""), "soft"
        )
        self.bars["Included copy"].set_span(
            extent, view["coverage"], "~" + view["included"], "dashed"
        )
        self.bars["Destination overlap"].set_span(
            extent, extent, "None in selected groups", "solid"
        )
        if view["active_count"]:
            tail = (
                (view["coverage"][1], extent[1])
                if extent[1] > view["coverage"][1]
                else (extent[1] - 1, extent[1])
            )
            self.bars["Latest excluded"].set_span(extent, tail, "", "marker")
        else:
            self.bars["Latest excluded"].set_span(extent, None, "No open groups", "soft")
        self.included.setText(
            f"<b>Included: {view['included']}.</b> "
            "Whole groups can extend beyond the requested dates. "
            "Date edges come from group contents."
        )
        sealed = view["last_sealed_day"]
        self.sealed.setText(
            (
                f"Last fully sealed day: {vm.format_day_label(sealed)}. "
                if sealed
                else "Last fully sealed day: unknown. "
            )
            + (
                "Open groups stay behind, even when cutover stops new events."
                if view["active_count"]
                else ""
            )
        )
        count = len(view["groups"]) - view["active_count"]
        self.groups_title.setText(
            f"{count} sealed group{'s' if count != 1 else ''} to import · "
            f"{view['active_count']} open group{'s' if view['active_count'] != 1 else ''} excluded"
        )
        self.groups.set_rows(view["groups"])
        self.checks.set_rows(view["checks"])
        self.storage.set_rows(view["storage"])
        self.warnings.setText("<br>".join(view["warnings"]))
        self.ack.setVisible(view["needs_ack"])
        self._update_actions()

    def showEvent(self, event):  # noqa: N802 (Qt override)
        super().showEvent(event)
        if (
            self.app.state.connected
            and self.app.state.plan_run is None
            and self.start_date.date().year() < 2000
        ):
            self.seed_dates()
