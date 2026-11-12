from __future__ import annotations

from datetime import date
from html import escape

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QCheckBox,
    QDateEdit,
    QGridLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...errors import Code
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

# Both codes mean the selection is already on the destination; neither is retried.
BLOCKED = {Code.OVERLAP.value, Code.COLLISION.value, Code.TOPOLOGY.value}


def _qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _pydate(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


class PlanPage(Page):
    """Dates in, whole sealed groups out; Start sends the window to the destination API once."""

    def __init__(self, app):
        super().__init__("Discovery & Plan")
        self.app = app
        self.warning_timer = QTimer(self)
        self.warning_timer.setSingleShot(True)
        self.warning_timer.timeout.connect(self._size_warnings)
        self.previewed: tuple[date, date] | None = None  # Dates the current plan was built from.
        self.date_message = ""  # Inline validation of the editors.
        self.blocked_dismissed = False  # The operator asked for the editors back.
        self.stack = QStackedWidget()
        self.body.addWidget(self.stack)
        self.stack.addWidget(self._plan_view())
        self.stack.addWidget(self._blocked_view())
        self.finish_body()
        self.change_button = self.add_action("Change dates", "", self.change_dates)
        self.refresh_button = self.add_action("Preview dates", "primary", self.preview)
        self.start_button = self.add_action("Start migration", "primary", self.start)

    def _plan_view(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.preview_badge = badge("9.1.1 Preview", "warn")
        self.badges = row(
            badge("Source stays running", "info"),
            badge("Logs still being written are left for later", "warn"),
            self.preview_badge,
        )
        layout.addWidget(self.badges)
        self.error = banner("", "", "bad")
        self.error.hide()
        layout.addWidget(self.error)
        self.error_ids = QPlainTextEdit()
        self.error_ids.setReadOnly(True)
        self.error_ids.setMaximumHeight(120)
        self.error_ids.hide()
        self.error_ids_toggle = button("Show group ids", "", self._toggle_error_ids)
        self.error_ids_copy = button("Copy group ids", "", self._copy_error_ids)
        self.error_ids_toggle.hide()
        self.error_ids_copy.hide()
        layout.addWidget(row(self.error_ids_toggle, self.error_ids_copy))
        layout.addWidget(self.error_ids)

        window_card, window_layout = card("Choose a window")
        self.start_date = QDateEdit()
        self.end_date = QDateEdit()
        self.date_hint = label("", "hint", "bad", wrap=True)
        self.date_hint.hide()
        for edit in (self.start_date, self.end_date):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.dateChanged.connect(self._dates_changed)
        window_layout.addWidget(
            columns(
                field("Start date (UTC)", self.start_date),
                field("End date, inclusive (UTC)", self.end_date),
            )
        )
        window_layout.addWidget(self.date_hint)
        presets = [
            button(name, "", lambda _checked=False, n=name: self.apply_preset(n))
            for name, _ in vm.PRESETS
        ]
        self.preset_buttons = presets
        for widget in presets:
            widget.setAccessibleName(f"Preset {widget.text()}")
        window_layout.addWidget(row(*presets))
        self.axis_labels = [label("", "caption") for _ in range(3)]
        self.axis_labels[1].setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.axis_labels[2].setAlignment(Qt.AlignmentFlag.AlignRight)
        axis = QWidget()
        axis_layout = QHBoxLayout(axis)
        axis_layout.setContentsMargins(0, 0, 0, 0)
        for widget in self.axis_labels:
            axis_layout.addWidget(widget, 1)
        self.bars: dict[str, TimelineBar] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(3)
        grid.setColumnStretch(1, 1)
        grid.addWidget(label("", "caption"), 0, 0)
        grid.addWidget(axis, 0, 1)
        for index, caption in enumerate(
            [
                "Source history",
                "Requested dates",
                "Included copy",
                "Destination overlap",
                "Latest excluded",
            ],
            start=1,
        ):
            bar = TimelineBar(caption)
            self.bars[caption] = bar
            display = {
                "Included copy": "Logs to copy",
                "Destination overlap": "Already at destination",
                "Latest excluded": "Still being written",
            }.get(caption, caption)
            grid.addWidget(label(display, "muted"), index, 0)
            grid.addWidget(bar, index, 1)
        grid_holder = QWidget()
        grid_holder.setLayout(grid)
        window_layout.addWidget(grid_holder)
        self.included = label("", "body", wrap=True, rich=True)
        self.sealed = label("", "hint", wrap=True)
        window_layout.addWidget(self.included)
        window_layout.addWidget(self.sealed)

        groups_card, groups_layout = card("Stored log groups in these dates")
        self.groups_title = label("", "subhead")
        groups_layout.addWidget(self.groups_title)
        self.groups = Table(["Stored logs", "Dates UTC", "Size / events", "Action"])
        groups_layout.addWidget(self.groups)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)
        left_layout.addWidget(window_card)
        left_layout.addWidget(groups_card)
        left_layout.addStretch(1)

        checks_card, checks_layout = card("Checks")
        self.checks = KeyValues(compact=True)
        checks_layout.addWidget(self.checks)
        estimate_card, estimate_layout = card("Estimated time")
        self.estimate = label("Preview the dates to see an estimate.", "body", wrap=True)
        estimate_layout.addWidget(self.estimate)
        storage_card, storage_layout = card("Destination storage")
        self.storage = Table(["Node", "Available", "Estimated share", "Spare"])
        storage_layout.addWidget(self.storage)
        storage_layout.addWidget(
            label(
                "Estimated share includes a margin for uneven group sizes and the 95% disk limit.",
                "hint",
                wrap=True,
            )
        )
        retention_card, retention_layout = card("Retention and warnings")
        self.warnings = label("", "hint", wrap=True)
        retention_layout.addWidget(self.warnings)
        self.warning_ids = QPlainTextEdit()
        self.warning_ids.setReadOnly(True)
        self.warning_ids.setMaximumHeight(160)
        self.warning_ids.hide()
        self.warning_ids_toggle = button("Show group ids", "", self._toggle_warning_ids)
        self.warning_ids_toggle.hide()
        self.warning_ids_copy = button("Copy group ids", "", self._copy_warning_ids)
        self.warning_ids_copy.hide()
        retention_layout.addWidget(row(self.warning_ids_toggle, self.warning_ids_copy))
        retention_layout.addWidget(self.warning_ids)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)
        right_layout.addWidget(checks_card)
        right_layout.addWidget(estimate_card)
        right_layout.addWidget(storage_card)
        right_layout.addWidget(retention_card)
        right_layout.addStretch(1)
        self.split = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.split.setContentsMargins(0, 0, 0, 0)
        self.split.setSpacing(16)
        self.split.addWidget(left, 5)
        self.split.addWidget(right, 4)
        self.split_holder = QWidget()
        self.split_holder.setLayout(self.split)
        layout.addWidget(self.split_holder)
        self.ack = QCheckBox("I understand you aren't going to copy the latest data.")
        self.ack.setAccessibleName("Latest-data acknowledgment")
        self.ack.stateChanged.connect(self._ack_changed)
        self.footer.layout().insertWidget(0, self.ack)
        return holder

    def _blocked_view(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.blocked_banner = banner("Preview unavailable", "Review the details below.", "bad")
        layout.addWidget(self.blocked_banner)
        evidence, evidence_layout = card("Preview details")
        self.overlap_rows = KeyValues()
        evidence_layout.addWidget(self.overlap_rows)
        self.group_ids = QPlainTextEdit()
        self.group_ids.setReadOnly(True)
        self.group_ids.setMaximumHeight(160)
        self.group_ids.hide()
        self.group_ids_toggle = button("Show group ids", "", self._toggle_group_ids)
        self.group_ids_copy = button("Copy group ids", "", self._copy_group_ids)
        self.group_ids_copy.hide()
        self.group_ids_row = row(self.group_ids_toggle, self.group_ids_copy)
        evidence_layout.addWidget(self.group_ids_row)
        evidence_layout.addWidget(self.group_ids)
        layout.addWidget(evidence)
        next_card, next_layout = card("Next step")
        self.blocked_next = label("", "body", wrap=True)
        next_layout.addWidget(self.blocked_next)
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

    def _size_warnings(self) -> None:
        width = self.warnings.width()
        if width > 0:
            height = self.warnings.heightForWidth(width)
            if height > 0 and self.warnings.minimumHeight() != height:
                self.warnings.setMinimumHeight(height)

    def _toggle_warning_ids(self) -> None:
        shown = not self.warning_ids.isVisible()
        self.warning_ids.setVisible(shown)
        self.warning_ids_copy.setVisible(shown)
        self.warning_ids_toggle.setText("Hide group ids" if shown else "Show group ids")

    def _copy_warning_ids(self) -> None:
        QApplication.clipboard().setText(self.warning_ids.toPlainText())

    def _toggle_error_ids(self) -> None:
        shown = not self.error_ids.isVisible()
        self.error_ids.setVisible(shown)
        self.error_ids_copy.setVisible(shown)
        self.error_ids_toggle.setText("Hide group ids" if shown else "Show group ids")

    def _copy_error_ids(self) -> None:
        QApplication.clipboard().setText(self.error_ids.toPlainText())

    def _toggle_group_ids(self) -> None:
        shown = not self.group_ids.isVisible()
        self.group_ids.setVisible(shown)
        self.group_ids_copy.setVisible(shown)
        self.group_ids_toggle.setText("Hide group ids" if shown else "Show group ids")

    def _copy_group_ids(self) -> None:
        QApplication.clipboard().setText(self.group_ids.toPlainText())

    # Operator actions ---------------------------------------------------------------------

    def _ack_changed(self, _state) -> None:
        self.app.state.acknowledged = self.ack.isChecked()
        self.refresh()

    def _dates_changed(self, _date) -> None:
        """Editing a date retires the current preview and its acknowledgment."""
        start, end = self.current_dates()
        self.date_message = vm.window_error(start, end) or ""
        if self.previewed is not None and (start, end) != self.previewed:
            self.app.state.acknowledged = False
        self.refresh()

    def current_dates(self) -> tuple[date, date]:
        return (_pydate(self.start_date.date()), _pydate(self.end_date.date()))

    def change_dates(self) -> None:
        """Return to the editors after a blocked preview, keeping the block visible."""
        self.blocked_dismissed = True
        self.refresh()
        self.start_date.setFocus(Qt.FocusReason.OtherFocusReason)

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self.warning_timer.start(0)
        # Keep the plan and its checks together at common desktop widths.
        narrow = self.width() < 800
        direction = QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        if self.split.direction() != direction:
            self.split.setDirection(direction)
            # A hidden stack page does not invalidate its parents' caches, so the stacked
            # widget would keep the wide two-column minimum until the page is shown again.
            self.split_holder.updateGeometry()
            self.stack.widget(0).layout().invalidate()
            self.stack.layout().invalidate()
            self.scroll.widget().updateGeometry()

    def _set_dates(self, days: tuple[date, date]) -> None:
        for edit in (self.start_date, self.end_date):
            edit.blockSignals(True)
        self.start_date.setDate(_qdate(days[0]))
        self.end_date.setDate(_qdate(days[1]))
        for edit in (self.start_date, self.end_date):
            edit.blockSignals(False)

    def apply_preset(self, name: str) -> None:
        extent = (
            vm.history_extent(self.app.state.discovered["source"])
            if self.app.state.discovered
            else None
        )
        days = vm.preset_days(name, extent)
        if days is None:
            return
        self._set_dates(days)
        for widget in self.preset_buttons:
            widget.setProperty("variant", "primary" if widget.text() == name else "")
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.app.state.plan_run = None
        self.refresh()

    def preview(self) -> None:
        state = self.app.state
        if not state.connected or "discover" in self.app.worker.pending:
            return
        if (state.discovered or {}).get("inventory_pending"):
            state.plan_error = None
            self.app.submit("discover", "discover")
            self.refresh()
            return
        start, end = self.current_dates()
        self.date_message = vm.window_error(start, end) or ""
        if self.date_message:
            self.refresh()
            return
        window = vm.window_from_days(start, end)
        self.previewed = (start, end)
        self.blocked_dismissed = False
        state.acknowledged = False
        settings = state.settings
        self.app.submit(
            "plan",
            "plan",
            start=window.start,
            end=window.end,
            attempts=settings.verify_attempts,
            poll_seconds=float(settings.count_poll_seconds),
        )
        self.refresh()

    def start(self) -> None:
        state = self.app.state
        if state.plan_run is None or not self._preview_is_current():
            return
        self.start_button.setEnabled(False)
        self.start_button.setText("Preparing migration…")
        self.note.setText(
            "Checking the source and destination counts before starting. "
            "This can take several minutes; keep this window open."
        )
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
            self._set_dates(days)

    # Rendering ----------------------------------------------------------------------------

    def _preview_is_current(self) -> bool:
        """The displayed plan was built from the dates in the editors and no preview is pending."""
        state = self.app.state
        if state.plan_run is None or "plan" in self.app.worker.pending:
            return False
        window = state.plan_run["plan"]["window"]
        current = vm.window_from_days(*self.current_dates())
        return current is not None and (current.start, current.end) == (
            window["start"],
            window["end"],
        )

    def _update_actions(self) -> None:
        state = self.app.state
        view = vm.plan_view(state.plan_run, state.discovered) if state.plan_run else None
        # Started in this session, or recorded by the engine at copy time in the journal.
        started = bool(state.plan_run) and (
            state.started_run_id == state.plan_run["id"] or "source_by_bucket" in state.plan_run
        )
        enabled, note = vm.start_state(view, state.acknowledged, state.connected, started)
        pending = "plan" in self.app.worker.pending
        if state.start_error:
            note = vm.start_note(state.start_error)
        elif self.date_message:
            enabled, note = False, self.date_message
        elif pending:
            enabled, note = False, "Previewing the selected dates…"
        elif view is not None and not started and not self._preview_is_current():
            enabled, note = (
                False,
                "The dates changed since the preview. Refresh the preview before starting.",
            )
        self.note.setText(note)
        self.start_button.setEnabled(enabled and "start" not in self.app.worker.pending)
        self.start_button.setText(
            "Start migration"
            if enabled
            else "Start migration · submitted"
            if started
            else "Start migration · unavailable"
        )
        self.refresh_button.setEnabled(
            state.connected
            and not pending
            and "discover" not in self.app.worker.pending
            and (
                not (state.discovered or {}).get("inventory_pending", False)
                or bool(state.plan_error)
            )
        )
        self.refresh_button.setText(
            "Retry inventory"
            if (state.discovered or {}).get("inventory_pending") and state.plan_error
            else "Preview dates"
        )
        ready_to_start = view is not None and self._preview_is_current() and not started
        for widget, primary in (
            (self.refresh_button, not ready_to_start),
            (self.start_button, ready_to_start),
        ):
            widget.setProperty("variant", "primary" if primary else "")
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def refresh(self) -> None:
        state = self.app.state
        discovered = state.discovered
        self.ack.blockSignals(True)
        self.ack.setChecked(state.acknowledged)
        self.ack.blockSignals(False)
        self.preview_badge.setVisible(state.preview)
        code = (state.plan_error or "").partition(": ")[0]
        blocked = bool(
            state.plan_error
            and (code in BLOCKED or (code == Code.INCOMPLETE.value and state.plan_affected))
            and not vm.is_duplicate_identity(state.plan_error)
        )
        self.change_button.setVisible(blocked and not self.blocked_dismissed)
        if blocked and not self.blocked_dismissed:
            self.stack.setCurrentIndex(1)
            detail = vm.plan_error_text(
                state.plan_error,
                discovered,
                vm.window_from_days(*self.current_dates()),
            )
            set_banner(
                self.blocked_banner,
                "Preview unavailable",
                "Review the affected groups below.",
                "bad",
            )
            self.blocked_next.setText(
                "Resolve the problem described above, then refresh the preview."
            )
            self.set_heading(
                "Migration needs attention",
                "Review the reason before continuing",
            )
            self.overlap_rows.set_rows(
                [
                    (
                        "Refused",
                        detail,
                    ),
                    (
                        "Request",
                        f"{self.start_date.date().toString('yyyy-MM-dd')} – "
                        f"{self.end_date.date().toString('yyyy-MM-dd')} UTC",
                    ),
                ]
            )
            lines = (
                state.plan_affected
                or vm.conflict_group_lines(state.plan_error, discovered)
                or vm.warning_group_lines(
                    [state.plan_error], (discovered or {}).get("source", {}).get("buckets", [])
                )
            )
            self.group_ids.setPlainText("\n".join(lines))
            self.group_ids_row.setVisible(bool(lines))
            if not lines:
                self.group_ids.hide()
            self.note.setText("Resolve the issue, then refresh the preview.")
            self.start_button.setEnabled(False)
            self.start_button.setText("Start migration · unavailable")
            self.refresh_button.setEnabled(
                state.connected and "plan" not in self.app.worker.pending
            )
            return
        self.stack.setCurrentIndex(0)
        error_ids = state.plan_affected or vm.warning_group_lines(
            [state.plan_error or ""], (discovered or {}).get("source", {}).get("buckets", [])
        )
        self.error_ids.setPlainText("\n".join(error_ids))
        self.error_ids_toggle.setVisible(bool(state.plan_error and error_ids))
        if not (state.plan_error and error_ids):
            self.error_ids.hide()
            self.error_ids_copy.hide()
        self.date_hint.setText(self.date_message)
        self.date_hint.setVisible(bool(self.date_message))
        if blocked:
            set_banner(
                self.error,
                "Migration needs attention",
                vm.plan_error_text(
                    state.plan_error,
                    discovered,
                    vm.window_from_days(*self.current_dates()),
                )
                + " Adjust the dates or refresh the preview.",
                "bad",
            )
            self.error.show()
        elif state.start_error:
            before_post = vm.is_pre_post(state.start_error)
            set_banner(
                self.error,
                "Start failed" if before_post else "Start outcome uncertain",
                vm.start_note(state.start_error)
                + (
                    " Fix the cause, refresh the preview, then start again."
                    if before_post
                    else " Check Migration for the API's current task; refresh the preview here "
                    "to plan again once no task is running."
                ),
                "bad" if before_post else "warn",
            )
            self.error.show()
        elif state.plan_error:
            window = vm.window_from_days(*self.current_dates())
            kind = vm.plan_error_kind(state.plan_error, discovered, window)
            set_banner(
                self.error,
                "Nothing to migrate in these dates" if kind == "info" else "Preview unavailable",
                vm.plan_error_text(state.plan_error, discovered, window),
                kind,
            )
            self.error.show()
        else:
            self.error.hide()
        if state.plan_run is None:
            self.set_heading(
                "Discovery & Plan",
                "Choose how far back to migrate, then Preview. "
                "Whole finished log groups are copied."
                if state.connected
                else "Connect both environments to preview history",
            )
            extent = vm.history_extent(discovered["source"]) if discovered else None
            for widget, text in zip(self.axis_labels, vm.axis_labels(extent), strict=True):
                widget.setText(text)
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
                text = (
                    "Preparing preview…"
                    if "plan" in self.app.worker.pending
                    else "Choose dates, then Preview"
                )
                self.bars[caption].set_span(extent, None, text, "soft")
            self.included.setText("")
            self.sealed.setText("")
            self.groups_title.setText(
                "Listing buckets…"
                if "discover" in self.app.worker.pending
                else "Preparing the preview…"
                if "plan" in self.app.worker.pending
                else "Choose dates, then select Preview dates."
            )
            self.groups.set_rows([])
            self.checks.set_rows(
                [("Preview", "Running…" if "plan" in self.app.worker.pending else "Not run")]
            )
            self.storage.set_rows([])
            self.estimate.setText("Preview the dates to see an estimate.")
            self.warnings.setText("")
            self.ack.setVisible(False)
            self._update_actions()
            return
        view = vm.plan_view(state.plan_run, discovered)
        self.set_heading(
            "Discovery & Plan",
            view["subtitle"]
            if not state.acknowledged or not view["needs_ack"]
            else "Ready · latest-data exclusion acknowledged",
        )
        extent = view["extent"] or view["coverage"]
        for widget, text in zip(self.axis_labels, vm.axis_labels(extent), strict=True):
            widget.setText(text)
        self.bars["Source history"].set_span(
            extent,
            extent,
            vm.format_day_range(*extent, suffix=""),
            "solid",
        )
        self.bars["Requested dates"].set_span(
            extent, view["window"], view["requested"].replace(" UTC", ""), "soft"
        )
        self.bars["Included copy"].set_span(
            extent, view["coverage"], "~" + view["included"], "dashed"
        )
        self.bars["Destination overlap"].set_span(extent, extent, view["overlap_text"], "solid")
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
            f"<b>Included: {escape(view['included'])}.</b> "
            "Whole groups can extend beyond the requested dates."
        )
        utc_dates = vm.format_day_range(*view["window"])
        self.sealed.setText(
            f"Requested dates in UTC: {utc_dates}. "
            + view["sealed_text"]
            + (
                " Open groups stay behind, even when cutover stops new events."
                if view["active_count"]
                else ""
            )
        )
        count = len(view["groups"]) - view["active_count"]
        self.groups_title.setText(
            f"{count} sealed group{'s' if count != 1 else ''} to import · "
            f"{view['active_count']} open group{'s' if view['active_count'] != 1 else ''} excluded"
        )
        self.groups.set_rows(view["groups"], tips=view["group_tips"])
        self.checks.set_rows(view["checks"])
        self.storage.set_rows(view["storage"])
        plan = state.plan_run["plan"]
        estimate = plan.get("estimate")
        if estimate:
            received = plan.get("already_received", [])
            group_count = estimate["bucket_count"]
            self.estimate.setText(
                f"{vm.format_eta(estimate['low_seconds'])} to "
                f"{vm.format_eta(estimate['high_seconds'])} for {group_count} "
                f"group{'s' if group_count != 1 else ''} "
                f"({vm.format_bytes(estimate['selected_bytes'])}) "
                f"on {estimate['destination_nodes']} destination nodes"
                + (" (assumed; not inspected)" if estimate.get("destination_nodes_assumed") else "")
                + ". "
                f"Rate: {vm.format_rate(estimate['rate_bytes_per_second'])} "
                f"({estimate['rate_basis']}). "
                + (
                    f"Source listing: {estimate['listing_seconds']:.1f} seconds "
                    f"({estimate['listing_basis']}). "
                    if estimate.get("listing_seconds") is not None
                    else ""
                )
                + "Actual time varies. "
                + f"Already received: {len(received)} group{'s' if len(received) != 1 else ''} "
                f"({vm.format_bytes(sum(b['size'] for b in received))})."
            )
        else:
            self.estimate.setText("No time estimate is available for this earlier preview.")
        self.warnings.setText("\n".join(view["warnings"]))
        self.warning_ids.setPlainText("\n".join(view["overlap_ids"]))
        self.warning_ids_toggle.setVisible(bool(view["overlap_ids"]))
        if not view["overlap_ids"]:
            self.warning_ids.hide()
            self.warning_ids_copy.hide()
        self.warning_timer.start(0)
        self.ack.setVisible(view["needs_ack"] and self._preview_is_current())
        self._update_actions()

    def showEvent(self, event):  # noqa: N802 (Qt override)
        super().showEvent(event)
        if (
            self.app.state.connected
            and self.app.state.plan_run is None
            and self.start_date.date().year() < 2000
        ):
            self.seed_dates()
