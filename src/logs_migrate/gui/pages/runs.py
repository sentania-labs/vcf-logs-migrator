"""Past runs. Every copy that was started, readable without signing in."""

from __future__ import annotations

from html import escape

from .. import copyview
from .. import viewmodel as vm
from ..widgets import GridTable, Page, button, card, empty, rich, scrolling
from . import result


class RunsPage(Page):
    def __init__(self, app):
        super().__init__(
            app,
            "Past runs",
            "Every copy you have started, newest first. Open one to read or save its report.",
        )

    def open(self, copy_id: str) -> None:
        self.app.state.open_run = copy_id
        self.app.refresh_all()
        self.focus_primary()

    def watch(self, copy_id: str) -> None:
        """A copy still in flight: straight to it when signed in, otherwise sign in first."""
        state = self.app.state
        copy = state.copy(copy_id)
        if copy is None:
            return
        if state.connected:
            self.app.adopt(copy)
            self.app.show_page("copy")
        else:
            self.app.fix_past(copy_id)

    def back(self) -> None:
        self.app.state.open_run = None
        self.app.refresh_all()

    def refresh(self) -> None:
        state = self.app.state
        zone = state.settings.timezone
        copy = state.copy(state.open_run) if state.open_run else None
        if copy is not None:
            view, found = result.describe(self.app, copy, past=True)
            self.view = view
            self.set_heading(
                f"Run from {vm.format_when(copy['started_at'], zone)}",
                "A past run, as it was recorded. You do not need to be connected to read it.",
            )
            self.set_body(*result.widgets(self.app, copy, view, found))
            self.set_footer(
                view["hint"],
                [("← Back to past runs", self.back), *result.actions(self.app, copy, view)],
            )
            return
        self.set_heading(
            "Past runs",
            "Every copy you have started, newest first. Open one to read or save its report.",
        )
        rows = copyview.run_rows(state.copies, zone)
        self.rows = rows
        if not rows:
            self.set_body(
                empty(
                    "No runs yet",
                    "Each time you copy logs, the run is listed here with its result,<br>and you "
                    "can open it to read or save its report.",
                    None
                    if state.connected
                    else button("Start by connecting", "primary", lambda: self.app.go("connect")),
                ),
                fill=True,
            )
            self.set_footer("")
            return
        table = GridTable(
            ["Started", "Dates copied", "From → to", "Size", "Result", ""],
            stretch=[4, 5, 6, 2, 5, 0],
            right=(3,),
            nowrap=(0, 3),
        )
        cells = []
        for row in rows:
            act = button(
                "Watch" if row["live"] else "Open",
                "",
                (lambda i=row["id"]: self.watch(i))
                if row["live"]
                else (lambda i=row["id"]: self.open(i)),
            )
            act.setAccessibleName(
                f"{'Watch' if row['live'] else 'Open'} the run from {row['when']}"
            )
            cells.append(
                (
                    f"<b>{escape(row['when'])}</b>",
                    escape(row["dates"]),
                    escape(row["systems"]),
                    row["size"],
                    ("badge", *row["result"]),
                    act,
                )
            )
        table.set_rows(cells)
        self.table = table
        frame, layout = card()
        layout.addWidget(scrolling(table, 16777215) if len(rows) > 8 else table)
        self.set_body(
            frame,
            rich(
                "Runs are kept on this computer. You do not need to be connected to read them.",
                "subtle",
            ),
            fill=len(rows) > 8,
        )
        self.set_footer("Open a run to read its result or save its report.")
