"""The result of one copy: a verdict, then only the days that differ. Shared by Check result
(this copy) and Past runs (any earlier one)."""

from __future__ import annotations

from html import escape
from pathlib import Path

from .. import copies, copyview
from .. import viewmodel as vm
from ..widgets import GridTable, banner, card, facts, rich, scrolling, verdict


def all_days(app, copy: dict, result: dict) -> None:
    table = GridTable(
        ["Days", "On the source", "On the destination", "Result"],
        stretch=[3, 2, 2, 3],
        right=(1, 2),
    )
    table.set_rows(copyview.all_days_rows(copy, result))
    app.ask(
        "All days",
        [scrolling(table, 380), rich("Runs of matching days are shown as one row.", "subtle")],
        [("Close", None, "primary")],
    )


def widgets(app, copy: dict, view: dict, result: dict) -> list:
    """The body of a result screen for `view` (from `copyview.check_view`)."""

    def link(href: str) -> None:
        if href == "all-days":
            all_days(app, copy, result)
        elif href == "open-report":
            app.open_file(view["saved"][0])
        elif href == "open-folder":
            app.open_file(str(Path(view["saved"][0]).parent))

    days = f'<a href="all-days">See all {vm.plural(result["days"], "day")}</a>'
    frame, layout = card()
    layout.addWidget(verdict(*view["verdict"]))
    if view["table"]:
        headers, rows = view["table"]
        table = GridTable(headers, stretch=[3, 2, 2, 3, 7], right=(1, 2), nowrap=(0, 1, 2))
        table.set_rows(
            [
                (f"<b>{escape(row[0])}</b>", row[1], row[2], ("badge", *row[3]), escape(row[4]))
                for row in rows
            ]
        )
        frame.table = table
        layout.addWidget(scrolling(table, 230) if len(rows) > 4 else table)
        note = (view["note"] + " " if view["note"] else "") + days
        layout.addWidget(rich(note, "subtle", link))
    elif view["all_days"] and result["days"] > 1:
        layout.addWidget(rich(days, "subtle", link))
    found = [frame, facts(view["facts"])]
    if view["left_out"]:
        outside, outside_layout = card("Not part of this copy")
        outside_layout.addWidget(rich(escape(view["left_out"]), "text"))
        found.append(outside)
    if view["saved"]:
        report = Path(view["saved"][0])
        found.append(
            banner(
                "ok",
                f"✓ Saved “{escape(report.name)}” and its data file",
                f'in <span style="font-family:monospace">{escape(str(report.parent))}</span> '
                '&nbsp; <a href="open-report">Open the report</a> · '
                '<a href="open-folder">Open the folder</a>',
                link,
            )
        )
    return found


def actions(app, copy: dict, view: dict) -> list[tuple]:
    slots = {
        "save": lambda: app.save_report(copy["id"]),
        "more-dates": app.more_dates,
        "copy-missing": lambda: app.continue_copy(missing=True),
        "continue": app.continue_copy,
        "check": app.check_result,
        "fix-past": lambda: app.fix_past(copy["id"]),
    }
    return [(action[0], slots[action[1]], *(action[3:] or ("",))) for action in view["actions"]]


def describe(app, copy: dict, *, past: bool) -> tuple[dict, dict]:
    state = app.state
    result = copies.check(copy)
    view = copyview.check_view(
        copy,
        result,
        past=past,
        saved=state.saved.get(copy["id"]),
        error=None if past else state.verify_error,
    )
    return view, result
