from __future__ import annotations

from .. import viewmodel as vm
from ..widgets import KeyValues, Page, Table, banner, card, columns, metric, set_banner
from .reports import RunSelector


class VerificationPage(Page):
    def __init__(self, app):
        super().__init__("Verification")
        self.app = app
        self.runs = RunSelector(app)
        self.body.addWidget(self.runs)
        self.banner = banner("", "", "info")
        self.body.addWidget(self.banner)
        self.tiles = []
        frames = []
        for title in (
            "Expected in included dates",
            "Destination in included dates",
            "Excluded latest data",
        ):
            frame, value, caption = metric(title, "Pending", "")
            self.tiles.append((frame.layout().itemAt(0).widget(), value, caption))
            frames.append(frame)
        self.body.addWidget(columns(*frames))
        daily_card, daily_layout = card("Daily reconciliation · UTC")
        self.daily = Table(["Day / period", "Expected", "Destination", "Result"])
        daily_layout.addWidget(self.daily)
        self.body.addWidget(daily_card)
        checks_card, checks_layout = card("Checks")
        self.checks = KeyValues()
        checks_layout.addWidget(self.checks)
        self.body.addWidget(checks_card)
        self.finish_body()
        self.verify_button = self.add_action("Verify counts", "", self.verify)
        self.report_button = self.add_action(
            "Open report", "primary", lambda: self.app.show_page("reports")
        )

    def verify(self) -> None:
        run_id = self.app.state.run_id
        if run_id:
            self.verify_button.setEnabled(False)
            self.app.verify_run(run_id)

    def refresh(self) -> None:
        state = self.app.state
        self.runs.refresh()
        preview = state.preview
        run = state.plan_run or (state.report["run"] if state.report else None)
        view = vm.verification_view(
            state.verification, state.status, state.active_count, preview, run
        )
        title = "Verification" + (" · 9.1.1 Preview" if preview else "")
        if view["outcome"] == "verified_discrepancy":
            title += " · daily mismatch"
        elif view["outcome"] == "incomplete_verification":
            title += " · incomplete"
        elif view["outcome"] == "partial":
            title += " · group not received"
        if run:
            ids = [r["id"] for r in state.runs]
            number = f"Run {ids.index(run['id']) + 1}" if run["id"] in ids else "this run"
            subtitle = f"Count evidence for {number} · " + vm.outcome_label(state.verification)
        else:
            subtitle = (
                "Choose a run to verify"
                if state.connected
                else "Connect both environments to verify counts"
            )
        self.set_heading(title, subtitle)
        status, heading, text = view["banner"]
        if state.verify_error:
            status, heading, text = (
                "bad",
                "Verification request failed",
                vm.describe_error(state.verify_error),
            )
        elif state.verify_run_id and state.verify_run_id == state.run_id:
            status, heading, text = ("info", "Verifying…", "Counting on both appliances.")
        set_banner(self.banner, heading, text, status)
        for (title_label, value, caption), (title, text_value, text_caption) in zip(
            self.tiles, view["tiles"], strict=True
        ):
            title_label.setText(title)
            value.setText(text_value)
            caption.setText(text_caption)
        self.daily.set_rows(view["rows"] or [("No complete counts yet", "", "", "")])
        self.checks.set_rows(view["checks"])
        self.verify_button.setText("Retry counts" if state.verification else "Verify counts")
        self.verify_button.setEnabled(
            bool(state.run_id) and state.connected and state.verify_run_id is None
        )
        self.report_button.setEnabled(bool(state.run_id))
        if view["outcome"] == "partial":
            self.note.setText(
                "A selected group never arrived. This is not a complete migration of the "
                "requested dates; plan the missing history again."
            )
        elif view["outcome"] == "verified_success":
            self.note.setText(
                "Counts cover included history only; API completion can hide skipped groups."
            )
        elif view["outcome"] == "verified_discrepancy":
            self.note.setText(
                "Check date boundaries, ongoing ingest and retention. "
                "This tool does not repair or delete imported history."
            )
        elif view["outcome"] == "incomplete_verification":
            self.note.setText(
                "No success result is shown until all daily counts complete "
                "and repeat consistently."
            )
        else:
            self.note.setText(
                "Verification compares complete, repeated per-day counts against "
                "selected source history, baseline and new ingest."
            )
