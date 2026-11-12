"""Step 4. Watch the copy run, stop it, continue it, and see plainly when it has not worked."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QElapsedTimer, QTimer

from .. import copies, copyview
from .. import viewmodel as vm
from ..widgets import Page, ProgressCard, Slot, banner, card, empty, facts, label, rich, verdict

WAIT_NOTE = (
    "Nothing to do but wait. When it finishes, the next step is to check that everything arrived."
)
CLOSE_NOTE = (
    "You can close this window. The copy runs on the destination and carries on without it. "
    "Open the app again any time to see how it is going."
)


class CopyPage(Page):
    def __init__(self, app):
        super().__init__(app, "Copy logs")
        self.progress = ProgressCard()
        self.top = Slot(spacing=10)
        self._drawn = None
        self.missing_job = False
        self.asked = QElapsedTimer()
        self.asking = label("", "subtle")
        self.ticker = QTimer(self)
        self.ticker.setInterval(1000)
        self.ticker.timeout.connect(self._tick)
        self.ticker.start()

    def _tick(self) -> None:
        if self.asked.isValid():
            self.asking.setText(
                f"Asking the destination… last asked {self.asked.elapsed() // 1000} s ago"
            )

    # Actions -----------------------------------------------------------------------------

    def _link(self, href: str) -> None:
        if href == "trouble":
            self.app.save_troubleshooting()

    def back_to_plan(self) -> None:
        self.app.state.start_failure = None
        self.app.show_page("dates")

    def start_again(self) -> None:
        self.app.state.start_failure = None
        self.app.start_copy()

    def check_dates_again(self) -> None:
        self.app.state.start_failure = None
        self.app.show_page("dates")
        self.app.pages["dates"].check(self.app.state.excluded or None)

    def network_details(self) -> None:
        state = self.app.state
        discovered = state.discovered
        text = (
            "VCF Logs Migrator: a network path is needed before logs can be copied\n\n"
            f"Source log system: {discovered['source']['host']}\n"
            f"Destination log system: {discovered['destination']['host']}\n\n"
            "During a copy, each server of the destination log system connects to each server "
            "of the source log system and fetches the stored logs over secure shell (SSH, TCP "
            "port 22), signing in with the source servers' own user and password.\n\n"
            "Please allow: from every destination server, to every source server, TCP port 22.\n"
            "The computer running the migrator can already reach both systems; this path is "
            "between the two systems themselves.\n"
        )
        self.app.save_text("network-path-needed.txt", text, "network")

    def choose_again(self) -> None:
        state = self.app.state
        state.continue_failure = None
        self.app.more_dates()

    def see_result(self) -> None:
        self.app.show_page("check")

    def ask_now(self) -> None:
        self.asked.start()
        self.app.poll_status()

    # Drawing -----------------------------------------------------------------------------

    def _heading(self, copy: dict | None, missing: bool) -> None:
        state = self.app.state
        discovered = state.discovered or {}
        where = ""
        if discovered:
            where = f" · {discovered['source']['host']} to {discovered['destination']['host']}"
        if copy is None and state.plan is not None:
            copy = copies.preview(state.plan["parts"], state.plan["excluded"])
        dates = copyview.dates_text(copy) if copy else ""
        if missing and copy is not None:
            left = copies.day_ranges(
                day for group in copies.remaining_groups(copy) for day in copies.group_days(group)
            )
            dates = vm.format_ranges(left) if left else dates
        self.set_heading("Copy what was missing" if missing else "Copy logs", dates + where)

    def _stopped(self, copy: dict, phase: str):
        state = self.app.state
        zone = state.settings.timezone
        run = copies.current_run(copy) or {}
        finished = run.get("finished") or {}
        when = vm.format_clock(finished.get("at"), zone) if finished.get("at") else ""
        sign_in = state.server_sign_in
        frame, layout = card()
        if copy["state"] == "between":
            part = copies.next_part(copy)
            layout.addWidget(
                verdict(
                    "info",
                    "❙❙",
                    "This copy is waiting to send its next part",
                    f"Part {part['index'] + 1} of {len(copy['parts'])} has not been sent yet. "
                    "What arrived stays on the destination. Continue whenever you like; only "
                    "what is missing gets sent.",
                )
            )
        elif phase == "stopped":
            layout.addWidget(
                verdict(
                    "info",
                    "❙❙",
                    f"You stopped the copy {when}".strip(),
                    "What arrived stays on the destination."
                    if not sign_in
                    else "What arrived stays on the destination. You can continue from here "
                    "whenever you like; only what is missing gets sent.",
                )
            )
        else:
            nodes = finished.get("failed_nodes") or []
            if nodes:
                who = " and ".join(nodes)
                happened = (
                    f"the destination reported that its server {escape(who)} failed, and ended "
                    "the copy. That is all it reported."
                )
                check = f", and tells you if {escape(who)} is not"
            else:
                happened = (
                    "the destination ended the copy and reported it as failed. That is all "
                    "it reported."
                )
                check = ""
            body = rich(
                f"<b>What happened:</b> {happened}<br><b>What to do:</b> continue the copy. "
                "Continuing first checks that every destination server is up again"
                f"{check}. What arrived is kept and only what is missing gets sent. If it stops "
                'again, <a href="trouble">save a troubleshooting file</a>: it records exactly '
                "where each attempt stopped, for whoever helps you.",
                "soft",
                self._link,
            )
            layout.addWidget(verdict("bad", "✕", f"The copy stopped by itself {when}".strip(), ""))
            layout.addWidget(body)
        widgets = [
            frame,
            facts(copyview.remainder_facts(copy, server_sign_in=sign_in, failed=phase == "failed")),
        ]
        if not sign_in:
            widgets.append(
                banner(
                    "warn",
                    "This copy cannot be continued yet.",
                    "No destination server sign-in was given on Connect, so the app cannot see "
                    "what arrived and will not guess. Add it on Connect, then come back here "
                    "with the button it gives you. If you cannot get it, this copy stays as it "
                    "is: what arrived is kept and can be checked.",
                )
            )
        actions = [("Check what arrived so far", self.app.check_result)]
        if sign_in:
            actions.append(("Continue copying", self.app.continue_copy, "primary"))
            hint = "Only what is missing is sent."
        else:
            actions.append(("Add the destination server sign-in", self.app.add_sign_in, "primary"))
            hint = "To continue this copy, the app needs the destination’s server sign-in."
        return widgets, hint, actions

    def refresh(self) -> None:
        state = self.app.state
        zone = state.settings.timezone
        phase = self.app.copy_phase()
        copy = state.copy()
        missing = bool(self.app.context.get("continue", {}).get("missing")) or self.missing_job
        if phase == "continuing":
            self.missing_job = bool(self.app.context.get("continue", {}).get("missing"))
            missing = self.missing_job
        elif phase in {"preparing", "idle", "notstarted", "stopped", "failed", "cantcontinue"}:
            self.missing_job = missing = False
        self._heading(copy, missing and phase in {"continuing", "running", "lost", "done"})
        quiet = self.app.quiet_minutes()
        key = (
            phase,
            state.copy_id,
            state.run_id,
            bool(state.status),
            "long" if quiet >= 30 else quiet >= copyview.QUIET_MINUTES,
        )
        if phase in {"preparing", "continuing", "stopping"}:
            self._drawn = None
            self.set_body(self.activity)
            self.set_footer(
                "Stopping. What has arrived stays on the destination."
                if phase == "stopping"
                else "Getting ready. The copy has not started yet."
            )
            return
        if phase == "idle" or (copy is None and phase != "notstarted"):
            self._drawn = None
            self.set_body(
                empty(
                    "No copy yet", "Choose dates and check them, then start the copy from there."
                ),
                fill=True,
            )
            self.set_footer("", [("Choose dates", lambda: self.app.go("dates"), "primary")])
            return
        if phase == "notstarted":
            self._drawn = None
            what, todo, replan = copyview.start_failure(state.start_failure)
            frame, layout = card()
            layout.addWidget(
                verdict(
                    "bad",
                    "✕",
                    "The copy did not start. No logs were copied.",
                    f"<b>What happened:</b> {escape(what)}<br><b>What to do:</b> {escape(todo)}",
                )
            )
            self.set_body(frame)
            actions = []
            if state.start_failure.get("stage") == "mover":
                actions.append(("Save details for the network team", self.network_details))
            actions.append(("Back to the plan", self.back_to_plan))
            if replan:
                actions.append(("Check the dates again", self.check_dates_again, "primary"))
                hint = "Nothing was sent. The plan has to be made again before starting."
            else:
                actions.append(("Start again", self.start_again, "primary"))
                hint = "Your plan is kept. Start again once that is fixed."
            self.set_footer(hint, actions)
            return
        if phase in {"running", "lost", "settling"}:
            view = copyview.progress_view(
                copy,
                state.status,
                zone=zone,
                now=vm.now_iso(),
                lost_at=state.lost_at,
                quiet_minutes=quiet,
                latest=state.latest,
                continued=bool(state.continued_note),
            )
            if state.continued_note and view["latest"]:
                view["latest"] += " " + state.continued_note
            elif state.continued_note:
                view["latest"] = state.continued_note
            self.progress.show_view(view, fresh=not state.lost_at)
            if self._drawn == key:
                return  # Same state: the figures changed in place and the buttons are untouched.
            self._drawn = key
            if phase == "lost":
                top = banner(
                    "warn",
                    f"Lost contact with the destination at {view['lost_at']}.",
                    "The copy does not need this window, so it is almost certainly still running "
                    "on the destination. The numbers below are the last ones seen and are not "
                    "being updated. <b>Do not start the copy again.</b> Reconnect to see where "
                    "it has got to.",
                )
                hint = "This window lost touch. The copy does not depend on it."
                actions = [
                    ("Reconnect", self.app.reconnect, "primary", not self.app.busy("reconnect"))
                ]
            elif view["mode"] == "quiet":
                if quiet >= 30:
                    top = banner(
                        "warn",
                        "No new logs have arrived for 30 minutes or more.",
                        "The destination still answers, but nothing is arriving. "
                        '<a href="trouble">Save a troubleshooting file</a> and send it to whoever '
                        "supports the destination. You can leave the copy running, or stop it; "
                        "what has arrived is kept either way.",
                        self._link,
                    )
                else:
                    top = banner(
                        "warn",
                        f"No new logs have arrived for {int(quiet)} minutes.",
                        "The destination still answers, so the copy has not failed. It is most "
                        "likely working through an unusually large day. Nothing to do yet. If "
                        "this reaches 30 minutes, this message will say so and offer to save a "
                        "troubleshooting file.",
                    )
                hint = WAIT_NOTE
                actions = [("Stop the copy…", self.app.stop_copy)]
            else:
                waiting = [part for part in copy["parts"] if part["state"] == "planned"]
                note = CLOSE_NOTE
                if waiting:
                    now = len(copy["parts"]) - len(waiting)
                    note = (
                        f"This copy runs in {len(copy['parts'])} parts, and this is part {now}. "
                        "<b>Keep this window open</b>: the app starts each part when the one "
                        "before finishes. If you close it, the part that is running carries on, "
                        "and you continue the rest when you open the app again."
                    )
                top = banner(
                    "info",
                    "Copying what was missing."
                    if missing
                    else "Copying. The destination is fetching logs from the source.",
                    note,
                )
                hint = WAIT_NOTE
                live = phase == "running" and bool(state.status)
                actions = [("Stop the copy…", self.app.stop_copy)] if live else []
            warning = (state.status or {}).get("capacity_warning") or []
            extra = None
            if warning and phase == "running":
                names = " and ".join(item["node"] for item in warning)
                extra = banner(
                    "warn",
                    f"{names} may run out of room.",
                    "What is still to arrive there is more than the free space seen before the "
                    "copy. The destination stops by itself before a disk fills, and the check "
                    "afterwards shows exactly what arrived.",
                )
            self.top.set(top, extra)
            self.set_body(self.top, self.progress)
            self.set_footer(hint, actions)
            return
        self._drawn = None
        total = copy["total_bytes"]
        if phase == "unsure":
            hold = (state.status or {}).get("hold_until")
            until = f", until <b>{vm.format_time(hold, zone)}</b>" if hold else ""
            frame, layout = card()
            layout.addWidget(
                verdict(
                    "warn",
                    "?",
                    "Not sure yet whether the copy started",
                    "<b>What happened:</b> the start request was sent but the destination did "
                    "not answer.<br><b>What the app is doing:</b> asking the destination every "
                    f"{state.settings.refresh_seconds} seconds whether a copy is running. To "
                    f"avoid copying twice, these dates stay locked for 15 minutes{until}. If no "
                    "copy has appeared by then, you can start again from Choose dates.",
                )
            )
            if not self.asked.isValid():
                self.asked.start()
            asking, asking_layout = card()
            self.asking = label("", "subtle")
            asking_layout.addWidget(self.asking)
            self._tick()
            self.set_body(frame, asking)
            self.set_footer(
                "Nothing to do yet. Please do not start another copy of these dates.",
                [("Ask now", self.ask_now)],
            )
            return
        if phase == "done":
            run = copies.current_run(copy) or {}
            ended = (run.get("finished") or {}).get("at")
            frame, layout = card()
            layout.addWidget(
                verdict(
                    "ok",
                    "✓",
                    "Finished copying what was missing" if missing else "Copy finished",
                    f"{vm.format_bytes(total)} sent to the destination in "
                    f"{vm.format_duration(copy['elapsed_seconds'])}"
                    + (f", finishing {vm.format_clock(ended, zone)}." if ended else "."),
                )
            )
            checked = copies.check(copy)["phase"] != "unchecked" and not missing
            if checked:
                self.set_body(frame)
                self.set_footer(
                    "This copy has been checked.",
                    [("See the result →", self.see_result, "primary")],
                )
            else:
                self.set_body(
                    frame,
                    banner(
                        "info",
                        "One step left: check that everything arrived.",
                        "A finished copy can still be missing days, for example if a "
                        "destination server filled up on the way. The check counts the logs on "
                        "both sides, day by day. It takes a few minutes and changes nothing.",
                    ),
                )
                self.set_footer(
                    "The copy is finished but not yet checked.",
                    [("Check what arrived →", self.app.check_result, "primary")],
                )
            return
        if phase == "missing":
            left = copies.remaining_groups(copy)
            by_node: dict[str, set] = {}
            for group in left:
                by_node.setdefault(group["node"], set()).update(copies.group_days(group))
            days = {day for node_days in by_node.values() for day in node_days}
            pieces = " and ".join(
                f"the logs that {escape(node)} holds for <b>"
                f"{vm.format_ranges(copies.day_ranges(node_days))}</b>"
                for node, node_days in sorted(by_node.items())
            )
            size = vm.format_bytes(sum(group["size"] for group in left))
            frame, layout = card()
            layout.addWidget(
                verdict(
                    "warn",
                    "!",
                    f"Copy finished, but part of {vm.plural(len(days), 'day')} did not arrive",
                    f"The destination skipped {pieces} ({size} in all). Everything else arrived.",
                )
            )
            widgets = [frame]
            if state.server_sign_in:
                widgets.append(
                    banner(
                        "info",
                        "What to do",
                        "Copy what is missing now. It sends only those logs and takes a few "
                        "minutes. Then check the result.",
                    )
                )
                self.set_body(*widgets)
                self.set_footer(
                    "Only what is missing is sent. Nothing is copied twice.",
                    [
                        ("Check what arrived anyway", self.app.check_result),
                        (
                            "Copy what is missing",
                            lambda: self.app.continue_copy(missing=True),
                            "primary",
                        ),
                    ],
                )
            else:
                widgets.append(
                    banner(
                        "warn",
                        "What is missing cannot be copied yet.",
                        "No destination server sign-in was given on Connect, so the app cannot "
                        "see what arrived and will not guess. Add it on Connect, then come back "
                        "here with the button it gives you.",
                    )
                )
                self.set_body(*widgets)
                self.set_footer(
                    "To copy what is missing, the app needs the destination’s server sign-in.",
                    [
                        ("Check what arrived anyway", self.app.check_result),
                        ("Add the destination server sign-in", self.app.add_sign_in, "primary"),
                    ],
                )
            return
        if phase in {"stopped", "failed"}:
            widgets, hint, actions = self._stopped(copy, phase)
            self.set_body(*widgets)
            self.set_footer(hint, actions)
            return
        failure = copyview.continue_failure(
            state.continue_failure, server_sign_in=state.server_sign_in
        )
        frame, layout = card()
        layout.addWidget(verdict("warn", "!", "Can’t continue yet", failure["text"]))
        self.set_body(
            frame,
            facts(
                copyview.remainder_facts(
                    copy, server_sign_in=state.server_sign_in, failed=False, blocked=True
                )
            ),
        )
        actions = [("Check what arrived so far", self.app.check_result)]
        if failure.get("sign_in"):
            actions.append(("Add the destination server sign-in", self.app.add_sign_in, "primary"))
        elif failure.get("replan"):
            actions.append(("Choose dates again", self.choose_again, "primary"))
        elif failure.get("nothing_left"):
            actions = [("Check what arrived →", self.app.check_result, "primary")]
        else:
            actions.append(("Try again", self.app.continue_copy, "primary"))
        self.set_footer("Nothing was sent.", actions)
