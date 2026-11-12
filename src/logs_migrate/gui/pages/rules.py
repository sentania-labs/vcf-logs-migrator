"""Step 2. Compare the rules on both systems and copy the ones that are missing."""

from __future__ import annotations

from html import escape

from PySide6.QtWidgets import QCheckBox, QGridLayout, QWidget

from .. import viewmodel as vm
from ..widgets import (
    GridTable,
    Page,
    badge_html,
    banner,
    card,
    label,
    rich,
    scrolling,
    stack,
    subtle_html,
    verdict,
)

LONG = 6  # More rules than this are shown as a compact tick list.


class RulesPage(Page):
    def __init__(self, app):
        super().__init__(
            app,
            "Copy rules to the destination",
            "Rules decide what happens to new logs: where copies are sent, how long they are "
            "kept, what is hidden or thrown away.",
        )
        self.selected: dict[str, bool] = {}
        self.boxes: dict[str, QCheckBox] = {}
        self._drawn = None

    def arrive(self) -> None:
        """The comparison runs by itself on arrival; there is no button for it."""
        state = self.app.state
        state.rules_seen = True
        if not state.connected:
            return
        if self.app.busy("compare"):
            self.app.begin("compare", "rules")
        elif state.comparison is None and state.config_result is None:
            self.app.compare_rules()

    # Selection ---------------------------------------------------------------------------

    def rows(self) -> list[dict]:
        return vm.rule_rows(self.app.state.comparison)

    def ticked(self) -> list[str]:
        return [
            row["selector"]
            for row in self.rows()
            if row["needed"] or self.selected.get(row["selector"], True)
        ]

    def _tick(self, selector: str, on: bool) -> None:
        self.selected[selector] = on
        self._footer()

    def tick_all(self, on: bool) -> None:
        for row in self.rows():
            self.selected[row["selector"]] = on or row["needed"]
        self._drawn = None
        self.refresh()

    def _box(self, row: dict, text: str = "") -> QCheckBox:
        box = QCheckBox(text)
        box.setAccessibleName(f"Copy {row['name']}")
        box.setChecked(row["needed"] or self.selected.get(row["selector"], True))
        box.setEnabled(not row["needed"])
        box.toggled.connect(lambda on, selector=row["selector"]: self._tick(selector, on))
        self.boxes[row["selector"]] = box
        return box

    # Actions -----------------------------------------------------------------------------

    def ask_copy(self, only_needed: bool = False) -> None:
        rows = self.rows()
        if only_needed:
            for row in rows:
                self.selected[row["selector"]] = row["needed"]
            self._drawn = None
            self.refresh()
        chosen = [row for row in rows if row["selector"] in set(self.ticked())]
        if not chosen:
            return
        changes = [row for row in chosen if row["changes"]]
        adds = len(chosen) - len(changes)
        destination = self.app.state.discovered["destination"]["host"]
        count = vm.plural(len(chosen), "rule")
        what = []
        if adds:
            what.append(f"<b>adds {vm.plural(adds, 'rule')}</b>")
        if changes:
            names = ", ".join(f"“{escape(row['short'])}”" for row in changes[:3])
            what.append(f"<b>changes {len(changes)}</b> ({names})")
        names = QWidget()
        grid = QGridLayout(names)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setVerticalSpacing(2)
        for index, row in enumerate(chosen):
            grid.addWidget(label(row["name"], "ui", wrap=True), index // 2, index % 2)
        box = scrolling(names, 190)
        self.app.ask(
            f"Copy {count} to {destination}?",
            [
                f"This writes to the destination: {' and '.join(what)}. Nothing is deleted "
                "there, and the source is not changed. You can remove a rule later in the "
                "destination’s own settings.",
                box,
            ],
            [
                ("Cancel", None),
                (
                    f"Copy {count}",
                    lambda: self.app.apply_rules([row["selector"] for row in chosen]),
                    "primary",
                ),
            ],
        )

    def retry(self) -> None:
        failed = [
            row["selector"]
            for row in vm.rule_results(self.app.state.config_result)
            if row.get("failed")
        ]
        if failed:
            self.app.apply_rules(failed)

    def onward(self) -> None:
        self.app.go("dates")

    # Drawing -----------------------------------------------------------------------------

    def _footer(self) -> None:
        count = len(self.ticked())
        needed = sum(row["needed"] for row in self.rows())
        if not count:
            self.set_footer(
                "No rules are ticked, so nothing will be copied to the destination.",
                [("Continue to dates →", self.onward, "primary")],
            )
            return
        actions = []
        if needed and len(self.rows()) > needed:
            only = "Copy only the needed one…" if needed == 1 else "Copy only the needed ones…"
            actions.append((only, lambda: self.ask_copy(True)))
        actions.append((f"Copy {vm.plural(count, 'rule')}…", self.ask_copy, "primary"))
        self.set_footer(
            "Ticked rules are added to the destination. Untick any you do not want. You are "
            "asked to confirm before anything is written.",
            actions,
        )

    def _difference(self, rows: list[dict]):
        state = self.app.state
        summary = vm.rules_summary(state.comparison)
        self.boxes = {}
        frame, layout = card()
        if len(rows) > LONG:
            wordy = [row for row in rows if row["wordy"]]
            simple = [row for row in rows if not row["wordy"]]
            inner = QWidget()
            grid = QGridLayout(inner)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(5)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(3, 1)
            line = 0
            if wordy:
                title = f"{len(wordy)} that need a word"
                grid.addWidget(label(title.upper(), "card-title"), line, 0, 1, 4)
                line += 1
                for row in wordy:
                    note = (badge_html("accent", "Needed") + " " if row["needed"] else "") + row[
                        "note"
                    ]
                    text = (
                        f"<b>{escape(row['name'])}</b> {escape(row['does'])} On the destination "
                        f"now: {row['now'].lower()}. If copied: "
                        f"{row['will'][0].lower() + row['will'][1:]}. {subtle_html(note)}"
                    )
                    grid.addWidget(self._box(row), line, 0)
                    grid.addWidget(rich(text, "ui"), line, 1, 1, 3)
                    line += 1
            if simple:
                title = (
                    f"{len(simple)} more, all missing on the destination and simply added"
                    if wordy
                    else f"{len(simple)} rules, all missing on the destination and simply added"
                )
                cap = label(title.upper(), "card-title")
                cap.setContentsMargins(0, 8, 0, 0)
                grid.addWidget(cap, line, 0, 1, 4)
                line += 1
                for index, row in enumerate(simple):
                    grid.addWidget(self._box(row), line + index // 2, (index % 2) * 2)
                    grid.addWidget(
                        rich(
                            f"<b>{escape(row['name'])}</b> {subtle_html(escape(row['does']))}",
                            "ui",
                        ),
                        line + index // 2,
                        (index % 2) * 2 + 1,
                    )
            # A long list scrolls inside its own box and says so; the page never scrolls.
            layout.addWidget(scrolling(inner, 16777215))
            links = (
                '<a href="all">Tick all</a> · <a href="needed">Untick all but the needed '
                "ones</a> · "
                if any(row["needed"] for row in rows)
                else '<a href="all">Tick all</a> · <a href="none">Untick all</a> · '
            )
        else:
            table = GridTable(
                [
                    "Copy",
                    "Rule on the source",
                    "What it does",
                    "On the destination now",
                    "If copied",
                ],
                stretch=[0, 3, 4, 2, 5],
                nowrap=(0,),
                lead=1,
            )
            cells = []
            for row in rows:
                note = (badge_html("accent", "Needed") + " " if row["needed"] else "") + row["note"]
                will = escape(row["will"]) + (f"<br>{subtle_html(note)}" if note else "")
                cells.append(
                    (
                        self._box(row),
                        f"<b>{escape(row['name'])}</b>",
                        escape(row["does"]),
                        row["now"],
                        will,
                    )
                )
            table.set_rows(cells)
            self.table = table
            layout.addWidget(table)
            links = ""
        tail = vm.rules_tail(summary)
        if links or tail:
            layout.addWidget(
                rich(
                    links + escape(tail),
                    "subtle",
                    lambda href: self.tick_all(href == "all"),
                )
            )
        return frame

    def _results(self, rows: list[dict]):
        frame, layout = card()
        table = GridTable(["Rule", "Result"], stretch=[2, 5])
        table.set_rows(
            [(escape(row["name"]), ("badge", *row["badge"], escape(row["text"]))) for row in rows]
        )
        self.results = table
        layout.addWidget(scrolling(table, 16777215) if len(rows) > LONG else table)
        return frame

    def refresh(self) -> None:
        state = self.app.state
        job = self.app.owner("rules")
        if job:
            self._drawn = None
            self.set_body(self.activity)
            self.set_footer(
                "Reading only. Nothing is changed on either system."
                if job == "compare"
                else "Writing the rules you confirmed. Nothing is deleted."
            )
            return
        if self.app.busy("compare"):
            self.app.begin("compare", "rules")
            self.set_body(self.activity)
            self.set_footer("Reading only. Nothing is changed on either system.")
            return
        where = vm.rules_state(state.comparison, state.config_result)
        if state.comparison_error:
            self.set_body(
                banner(
                    "bad",
                    "The rules could not be copied."
                    if state.comparison
                    else "The rules could not be read.",
                    vm.what_happened(state.comparison_error),
                )
            )
            self.set_footer(
                "Nothing was changed. You can try again, or go on and come back to this later.",
                [
                    ("Continue to dates →", self.onward),
                    ("Try again", self.app.compare_rules, "primary"),
                ],
            )
            return
        if where == "unknown":
            self.set_body(
                banner(
                    "info",
                    "Connect first.",
                    "The rules are compared once both systems are signed in.",
                )
            )
            self.set_footer("")
            return
        if where == "none":
            total = vm.rules_summary(state.comparison)["total"]
            frame, layout = card()
            layout.addStretch(1)
            layout.addWidget(
                verdict(
                    "ok",
                    "✓",
                    "Nothing to copy",
                    f"The destination already has the same {vm.plural(total, 'rule')} as the "
                    "source. This step is done."
                    if total
                    else "The source has no rules of the kinds this app copies. This step is done.",
                )
            )
            layout.addStretch(1)
            self.set_body(frame)
            self.set_footer(
                "No rules need copying.", [("Continue to dates →", self.onward, "primary")]
            )
            return
        if where == "diff":
            rows = self.rows()
            key = ("diff", id(state.comparison))
            if self._drawn != key:
                self._drawn = key
                title, text = vm.rules_banner(rows)
                self.set_body(
                    banner("info", title, text), self._difference(rows), fill=len(rows) > LONG
                )
            self._footer()
            return
        self._drawn = None
        rows = vm.rule_results(state.config_result)
        copied = [row for row in rows if row["badge"][1] == "Copied"]
        failed = [row for row in rows if row.get("failed")]
        left_out = [row for row in rows if row["badge"][1] == "Left out"]
        groups = any(
            rule["type"] == "partitions" and rule["status"] == "created"
            for rule in state.config_result["rules"]
        )
        outstanding = vm.needed_outstanding(state.comparison, state.config_result)
        restart = (
            " New logs start using a new storage group after the destination’s next restart. "
            "You do not need to restart for the copy."
            if groups
            else ""
        )
        if where == "done":
            only_needed = bool(left_out) and all(
                row["needed"] for row in self.rows() if row["selector"] in set(self.ticked())
            )
            title = (
                "✓ The needed rule was copied."
                if only_needed and len(copied) == 1
                else f"✓ {vm.plural(len(copied), 'rule')} copied to the destination."
            )
            self.set_body(
                banner(
                    "ok",
                    title,
                    "Each one was read back from the destination to confirm it matches. Old "
                    "logs can be copied straight away." + restart,
                ),
                self._results(rows),
                fill=len(rows) > LONG,
            )
            self.set_footer(
                "Rules are done. Next, choose how far back to copy logs.",
                [("Continue to dates →", self.onward, "primary")],
            )
            return
        names = ", ".join(
            f"“{escape(row['name'].split('“')[-1].rstrip('”'))}”" for row in failed[:3]
        )
        reason = failed[0]["text"][0].lower() + failed[0]["text"][1:] if failed else ""
        others = len(copied)
        self.set_body(
            banner(
                "warn",
                f"{vm.plural(others, 'rule')} copied. {len(failed)} "
                f"{'was' if len(failed) == 1 else 'were'} not.",
                f"{names} could not be added: {escape(reason)}. "
                + (
                    f"The other {vm.plural(others, 'rule')} {'is' if others == 1 else 'are'} in "
                    "place and stay there. "
                    if others
                    else ""
                )
                + (
                    "<b>A rule marked Needed is among them, so logs cannot be copied until it "
                    "is in place.</b>"
                    if outstanding
                    else "You can carry on without "
                    f"{'this rule' if len(failed) == 1 else 'these rules'} and add "
                    f"{'it' if len(failed) == 1 else 'them'} later."
                ),
            ),
            self._results(rows),
            fill=len(rows) > LONG,
        )
        retry = "Try that rule again" if len(failed) == 1 else "Try those rules again"
        if outstanding:
            self.set_footer(
                "The needed rule was not copied, so logs cannot be copied yet.",
                [("Continue to dates →", self.onward), (retry, self.retry, "primary")],
            )
        else:
            self.set_footer(
                "You can go on to copy logs.",
                [(retry, self.retry), ("Continue to dates →", self.onward, "primary")],
            )


__all__ = ["RulesPage", "stack"]
