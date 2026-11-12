"""Step 3. Choose how far back to copy, and read the plan or the reason it cannot start."""

from __future__ import annotations

from datetime import UTC, date, datetime
from html import escape

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QDateEdit, QGridLayout, QHBoxLayout, QPushButton, QWidget

from .. import copies, copyview
from .. import viewmodel as vm
from ..widgets import (
    Ack,
    GridTable,
    Page,
    PlanLines,
    Slot,
    Timeline,
    button,
    card,
    columns,
    empty,
    field,
    label,
    legend,
    rich,
    scrolling,
    set_prop,
)


def _qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _pydate(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


class DatesPage(Page):
    def __init__(self, app):
        super().__init__(app, "Choose what to copy")
        self.preset = ""
        self.planned: tuple[date, date] | None = None  # The dates the plan was made for.
        self.touched = False
        self.ack: Ack | None = None
        self.right = Slot(spacing=10)
        self.left = self._left()
        self.left.persistent = True
        self.holder = columns(self.left, self.right, stretch=[5, 6], top=True, exact=True)
        self.holder.persistent = True
        self.set_body(self.holder, fill=True)

    def _left(self):
        frame, layout = card("How far back?")
        self.presets: dict[str, QPushButton] = {}
        self.preset_grid = QGridLayout()
        self.preset_grid.setSpacing(6)
        for name, _months in vm.PRESETS:
            self.presets[name] = button(name, "", lambda n=name: self.apply_preset(n))
        layout.addLayout(self.preset_grid)
        self._wide = None
        self.compact(False)
        self.start_date = QDateEdit()
        self.end_date = QDateEdit()
        for edit in (self.start_date, self.end_date):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("MMM d, yyyy")
            edit.dateChanged.connect(self._dates_changed)
        layout.addWidget(
            columns(
                field("From", self.start_date)[0], field("To (last day copied)", self.end_date)[0]
            )
        )
        self.date_hint = rich("", "hint", self._link)
        layout.addWidget(self.date_hint)
        self.timeline = Timeline()
        layout.addSpacing(4)
        layout.addWidget(self.timeline)
        axis = QHBoxLayout()
        self.axis_first = label("", "subtle")
        axis.addWidget(self.axis_first)
        axis.addStretch(1)
        axis.addWidget(label("today", "subtle"))
        layout.addLayout(axis)
        self.legend = QWidget()
        self.legend_box = QHBoxLayout(self.legend)
        self.legend_box.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.legend)
        layout.addStretch(1)
        return frame

    def compact(self, small: bool) -> None:
        """Four choices in a row, or two rows of two when the window is small."""
        if not hasattr(self, "preset_grid"):
            return
        small = self.width() < 1200  # The left column is under half of the screen.
        if self._wide == (not small):
            return
        self._wide = not small
        for index, widget in enumerate(self.presets.values()):
            self.preset_grid.removeWidget(widget)
            if small:
                self.preset_grid.addWidget(widget, index // 2, index % 2)
            else:
                self.preset_grid.addWidget(widget, 0, index)

    # Dates -------------------------------------------------------------------------------

    def extent(self) -> dict | None:
        return vm.history((self.app.state.inventory or {}).get("source"))

    def dates(self) -> tuple[date, date]:
        return _pydate(self.start_date.date()), _pydate(self.end_date.date())

    def _set_dates(self, start: date, end: date) -> None:
        for edit in (self.start_date, self.end_date):
            edit.blockSignals(True)
        self.start_date.setDate(_qdate(start))
        self.end_date.setDate(_qdate(end))
        for edit in (self.start_date, self.end_date):
            edit.blockSignals(False)

    def seed(self) -> None:
        """Once the source's history is known, start from the last six months."""
        if self.extent() and not self.touched and self.planned is None:
            self.apply_preset("Last 6 months", quiet=True)

    def apply_preset(self, name: str, quiet: bool = False) -> None:
        extent = self.extent()
        if extent is None:
            return
        self.preset = name
        self._set_dates(*vm.preset_days(dict(vm.PRESETS)[name], extent))
        self._forget_plan()
        if not quiet:
            self.touched = True
            self.app.refresh_all()

    def _dates_changed(self, _value) -> None:
        self.preset = ""
        self.touched = True
        self._forget_plan()
        self.app.refresh_all()

    def _forget_plan(self) -> None:
        """Changing a date retires the plan, its refusal and the tick that went with them."""
        self.planned = None
        self.app.state.clear_plan()

    def arrive(self) -> None:
        state = self.app.state
        if state.connected and state.inventory is None and not state.inventory_error:
            self.app.list_source()
        if self.app.busy("discover") and self.app.owner("dates") is None:
            self.app.begin("discover", "dates", on_cancel=False)
        self.seed()

    # Actions -----------------------------------------------------------------------------

    def check(self, exclude: list[dict] | None = None) -> None:
        start, end = self.dates()
        if vm.dates_problem(start, end, self.extent()) or self.app.busy("plan"):
            return
        self.planned = (start, end)
        self.app.check_dates(start, end, exclude)

    def retry_listing(self) -> None:
        self.app.state.inventory_error = None
        self.app.list_source()
        self.app.begin("discover", "dates", on_cancel=False)
        self.app.refresh_all()

    def _act(self, action: str, argument: str) -> None:
        state = self.app.state
        if action == "exclude":
            taken = copyview.excluded_days(state.excluded) + copyview.refused_days(
                state.plan_refusal
            )
            ranges = [
                {"first": a.isoformat(), "last": z.isoformat()} for a, z in copies.day_ranges(taken)
            ]
            self.check(ranges)
        elif action == "use-dates":
            first, last = (date.fromisoformat(part) for part in argument.split("|"))
            self.preset = ""
            self.touched = True
            self._set_dates(first, last)
            self._forget_plan()
            self.check()
        elif action == "watch":
            found = next(
                (
                    c
                    for c in state.copies
                    if any(run["id"] == argument for part in c["parts"] for run in part["runs"])
                ),
                None,
            )
            if found is not None:
                self.app.adopt(found)
                self.app.show_page("copy")
        elif action == "check":
            self.check(state.excluded or None)
        elif action == "preset":
            self.apply_preset(argument)
            if argument != "Everything" or state.plan_refusal:
                self.check()
        elif action == "add-sign-in":
            self.app.add_sign_in(back=False)
        elif action == "rules":
            self.app.go("rules")
        elif action == "runs":
            self.app.go("runs")

    def _link(self, href: str) -> None:
        state = self.app.state
        view = self._view
        if href == "use-last":
            extent = self.extent()
            start, _end = self.dates()
            self._set_dates(min(start, extent["last"]), extent["last"])
            self._dates_changed(None)
        elif href == "space":
            table = GridTable(
                ["Server", "Free now", "This copy needs", "Left afterwards"], right=(1, 2, 3)
            )
            table.set_rows(copyview.space_rows(view["room"]))
            self.app.ask(
                "Free space on the destination",
                [
                    table,
                    rich(
                        "“Needs” includes a safety margin. The destination also stops by "
                        "itself before a disk is completely full.",
                        "subtle",
                    ),
                ],
                [("Close", None, "primary")],
            )
        elif href == "list":
            table = GridTable(
                ["Month", "In your dates", "To copy", "Will it be copied?"],
                stretch=[2, 2, 2, 5],
                right=(2,),
            )
            table.set_rows(copyview.months_view(view))
            pieces = len(view["copy"]["groups"]) + len(view["copy"]["already"])
            self.app.ask(
                "What will be copied",
                [
                    rich(
                        "Month by month. Nothing here needs deciding; it is the plan in more "
                        "detail.",
                        "soft",
                    ),
                    scrolling(table, 330),
                    rich(
                        "The saved file goes one level further and lists every stored piece "
                        f"({pieces:,} of them) with its source server.",
                        "subtle",
                    ),
                ],
                [
                    ("Close", None, "primary"),
                    (
                        "Save the detailed list as a file",
                        lambda: self.app.save_text(
                            "copy-plan-detail.csv", copyview.plan_detail_csv(view), "plan"
                        ),
                    ),
                ],
            )
        elif href == "add-sign-in":
            self.app.add_sign_in(back=False)
        elif href == "save-details":
            labels = {
                "source": state.discovered["source"]["host"],
                "destination": state.discovered["destination"]["host"],
            }
            self.app.save_text(
                "broken-copies.txt", copyview.refusal_details(state.plan_refusal, labels), "details"
            )
        elif href == "open-folder":
            self.app.open_file(state.settings.export_folder)

    def _ack_changed(self, on: bool) -> None:
        self.app.state.acknowledged = on
        self._ready_footer()

    # Drawing -----------------------------------------------------------------------------

    def _refusal_card(self, view: dict):
        tone = view["tone"]
        frame, layout = card(view["card"], edge=tone)
        layout.addWidget(label(view["title"], "big", wrap=True))
        layout.addWidget(rich(view["text"], "soft"))
        if view["table"]:
            headers, rows = view["table"]
            table = GridTable(headers, nowrap=(0,))
            table.set_rows(rows)
            self.table = table
            layout.addWidget(scrolling(table, 190) if len(rows) > 5 else table)
        if view["more"]:
            layout.addWidget(rich(view["more"], "subtle", self._link))
        return frame

    def _ready_footer(self) -> None:
        state = self.app.state
        view = self._view
        ready = not view["needs_ack"] or state.acknowledged
        self.set_footer(
            "" if view["needs_ack"] else "Starting asks the destination to fetch these logs.",
            [("Start copying →", self.app.start_copy, "primary", ready)],
        )

    def refresh(self) -> None:
        state = self.app.state
        extent = self.extent()
        start, end = self.dates()
        zone_today = datetime.now(UTC).date()
        busy = self.app.busy("plan")
        listing = self.app.busy("discover") and extent is None
        problem = vm.dates_problem(start, end, extent) if extent else None
        self.set_heading(
            "Choose what to copy",
            f"The source has logs from {vm.format_day(extent['first'])} to today."
            if extent
            else "Listing what the source holds…"
            if listing
            else "",
        )
        for name, widget in self.presets.items():
            set_prop(widget, "seg", "on" if name == self.preset else "off")
            widget.setEnabled(bool(extent) and not busy)
        for edit in (self.start_date, self.end_date):
            edit.setEnabled(bool(extent) and not busy)
        requested = self.planned or (start, end)
        view = None
        refusal = None
        if state.plan is not None and self.planned is not None:
            preview = copies.preview(state.plan["parts"], state.plan["excluded"])
            view = copyview.plan_view(
                preview, self.planned, extent, server_sign_in=state.server_sign_in
            )
        elif state.plan_error and self.planned is not None and not busy:
            listfail = (state.plan_refusal or {}).get("kind") == "listfail"
            if not listfail:
                refusal = copyview.refusal_view(
                    state.plan_error,
                    state.plan_refusal,
                    self.planned,
                    extent,
                    zone=state.settings.timezone,
                    saved=state.details_saved,
                    server_sign_in=state.server_sign_in,
                )
        self._view = view
        # The left card: one sentence and one bar.
        if problem:
            use = (
                f' <a href="use-last">Use {vm.format_day(extent["last"])}</a>'
                if extent and end > extent["last"]
                else ""
            )
            self.date_hint.setText(escape(problem) + use)
        elif view and view["phase"] == "ready" and view["extra_start"]:
            self.date_hint.setText(
                f"<b>The copy starts {vm.format_day(view['first'], False)}, "
                f"{vm.plural(view['extra_start'], 'day')} before your From date.</b> The plan on "
                "the right says why."
            )
        elif extent and extent["newest"]:
            self.date_hint.setText(
                "Days run midnight to midnight UTC. The latest day you can pick is "
                f"{vm.format_day(extent['last'])}: the source is still writing the days after it."
            )
        else:
            self.date_hint.setText("Days run midnight to midnight UTC.")
        set_prop(self.date_hint, "status", "bad" if problem else "")
        had, broken, had_text = [], [], "Already on the destination"
        selected = (start, end) if extent and not problem else None
        if view:
            had = view["already_ranges"]
            broken = copies.day_ranges(view["skipped"])
            if view["phase"] == "ready":
                selected = (view["first"], view["last"])
        elif refusal:
            if refusal["running"]:
                had, had_text = copies.day_ranges(refusal["days"]), "Being copied now"
            elif refusal["broken"]:
                broken = copies.day_ranges(refusal["days"])
        if extent:
            self.timeline.set(extent["first"], zone_today, selected, had, broken, extent["newest"])
            self.axis_first.setText(f"{vm.format_day(extent['first'])} · oldest log on the source")
        else:
            self.timeline.set(None, None, None)
            self.axis_first.setText("")
        keys = [("accent", "Your dates")]
        if had:
            keys.append(("ok", had_text))
        if broken:
            keys.append(("bad", "Broken copies on the destination"))
        if extent and extent["newest"]:
            keys.append(
                (
                    "newest",
                    f"Newest logs, still being written ({vm.format_day(extent['newest'])} onward)",
                )
            )
        while self.legend_box.count():
            item = self.legend_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.legend_box.addWidget(legend(keys))
        self.set_extra(None)
        self.ack = None

        # The right side: one of nothing yet, working, the plan, or why not.
        owner = self.app.owner("dates")
        if owner or busy or listing:
            if listing and owner is None:
                self.app.begin("discover", "dates", on_cancel=False)
            self.right.set(self.activity)
            self.set_footer(
                "Measuring. You can leave this window open and come back."
                if busy
                else "Listing what the source holds. This only reads."
            )
            return
        if state.inventory_error and extent is None:
            what, todo = vm.plain_error(state.inventory_error)
            frame, layout = card("Can’t start yet", edge="bad")
            layout.addWidget(
                label("Could not read the list of logs from the source", "big", wrap=True)
            )
            layout.addWidget(
                rich(
                    f"<b>What happened:</b> {escape(what)}<br><b>What to do:</b> {escape(todo)}",
                    "soft",
                )
            )
            self.right.set(frame)
            self.set_footer("Nothing was changed.", [("Try again", self.retry_listing, "primary")])
            return
        if state.plan_error and (state.plan_refusal or {}).get("kind") == "listfail":
            self.right.set(self.activity)
            self.set_footer(
                "The check stopped before it finished, so there is no plan yet.",
                [("Try again", lambda: self.check(state.excluded or None), "primary")],
            )
            return
        if refusal:
            self.right.set(self._refusal_card(refusal))
            self.set_footer(
                refusal["hint"],
                [
                    (text, lambda a=action, g=argument: self._act(a, g), variant)
                    for text, action, argument, variant in refusal["actions"]
                ],
            )
            return
        if view is None:
            self.right.set(
                empty(
                    "No plan yet",
                    "Pick how far back, then press <b>Check these dates</b>.<br>The app "
                    "measures how much there is, how long it will take<br>and whether it fits "
                    "on the destination. Nothing is copied yet.",
                ),
                fill=True,
            )
            self.set_footer(
                escape(problem)
                if problem
                else f"Selected: <b>{vm.format_range(start, end)}</b>. Checking reads both "
                "systems and can take a few minutes.",
                [("Check these dates", self.check, "primary", not problem and bool(extent))],
            )
            return
        phase = view["phase"]
        if phase == "ready":
            frame, layout = card("Your plan", ("ok", "Ready"))
            lines = PlanLines(view["lines"], self._link)
            self.plan_lines = lines
            layout.addWidget(lines)
            layout.addWidget(
                rich(
                    "The source keeps working during the copy and nothing on it is changed. "
                    '<a href="list">See the full list of what will be copied</a>',
                    "subtle",
                    self._link,
                )
            )
            self.right.set(frame)
            if view["ack"]:
                self.ack = Ack(view["ack"])
                self.ack.setChecked(state.acknowledged)
                self.ack.toggled.connect(self._ack_changed)
                self.set_extra(self.ack)
            self._ready_footer()
            return
        if phase == "already":
            ranges = view["already_ranges"]
            other = "Last 6 months" if self.preset != "Last 6 months" else "Everything"
            body = {
                "tone": "info",
                "card": "Nothing to do",
                "title": "Nothing to copy: these dates are already on the destination",
                "text": f"Everything from {vm.format_range(*requested)} arrived in "
                f"{'earlier runs' if len(ranges) > 1 else 'an earlier run'}. There is nothing "
                "left to copy for these dates.",
                "table": None,
                "more": "",
            }
            actions = [
                ("See past runs", lambda: self._act("runs", "")),
                (
                    "Choose everything instead"
                    if other == "Everything"
                    else "Choose the last 6 months instead",
                    lambda: self._act("preset", other),
                    "primary",
                ),
            ]
            hint = "Nothing was copied or changed."
        elif phase == "space":
            short = [node for node in view["room"]["nodes"] if node["short"]]
            names = " and ".join(node["id"] for node in short)
            rows = [
                (
                    node["id"],
                    vm.format_bytes(node["free"]),
                    vm.format_bytes(node["need"]),
                    f"<b>{vm.format_bytes(node['short'])} short</b>" if node["short"] else "Fits",
                )
                for node in view["room"]["nodes"]
            ]
            body = {
                "tone": "bad",
                "card": "Can’t start yet",
                "title": "Not enough room on the destination",
                "text": (
                    "One destination server" if len(short) == 1 else "Some destination servers"
                )
                + f" would fill up. <b>What to do:</b> free up space on {escape(names)} (for "
                "example by adding disk, or shortening how long the destination keeps its own "
                "logs) and check again. Or copy fewer months.",
                "table": (["Destination server", "Free", "Needed", "Result"], rows),
                "more": "",
            }
            actions = [("Check again", lambda: self._act("check", ""))]
            if self.preset != "Last 3 months":
                actions.append(
                    (
                        "Check the last 3 months instead",
                        lambda: self._act("preset", "Last 3 months"),
                        "primary",
                    )
                )
            else:
                actions[0] = (*actions[0], "primary")
            hint = "Nothing was copied or changed."
        elif phase == "rules":
            body = {
                "tone": "bad",
                "card": "Can’t start yet",
                "title": "A rule the copy depends on is missing on the destination",
                "text": "Logs are kept in storage groups, and the destination does not have one "
                "the source uses, or has it set up differently. <b>What to do:</b> go to Copy "
                "rules and copy the rule marked Needed, then check the dates again.",
                "table": None,
                "more": "",
            }
            actions = [("Go to Copy rules", lambda: self._act("rules", ""), "primary")]
            hint = "Nothing was copied or changed."
        else:
            body = {
                "tone": "bad",
                "card": "Can’t start yet",
                "title": "The destination does not have some fields these logs use",
                "text": f"Missing: <b>{escape(', '.join(view['fields']))}</b>. <b>What to do:</b> "
                "add them in the destination’s own settings, under fields, then check again.",
                "table": None,
                "more": "",
            }
            actions = [("Check again", lambda: self._act("check", ""), "primary")]
            hint = "Nothing was copied or changed."
        self.right.set(self._refusal_card(body))
        self.set_footer(hint, actions)
