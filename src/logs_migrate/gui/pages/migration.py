from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QProgressBar

from .. import viewmodel as vm
from ..widgets import KeyValues, Page, Table, banner, card, columns, label, metric, set_banner


class MigrationPage(Page):
    """One API status view: running, stale connection, failed, finished."""

    def __init__(self, app):
        super().__init__("Migration")
        self.app = app
        self.banner = banner("", "", "info")
        self.body.addWidget(self.banner)
        self.remainder_card, remainder_layout = card("Ready to resume")
        self.remainder = label("", "body", wrap=True)
        remainder_layout.addWidget(self.remainder)
        self.body.addWidget(self.remainder_card)
        self.remainder_card.hide()
        self.tiles = []
        frames = []
        for title in ("Transferred", "Transfer rate", "Elapsed", "ETA"):
            frame, value, caption = metric(title, vm.UNAVAILABLE, "")
            self.tiles.append((value, caption))
            frames.append(frame)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setAccessibleName("Migration progress")
        frames[0].layout().insertWidget(2, self.progress)
        self.metrics = columns(*frames)
        self.body.addWidget(self.metrics)
        nodes_card, nodes_layout = card("Destination progress")
        self.nodes_card = nodes_card
        self.nodes = Table(
            [
                "Destination node",
                "State",
                "Transferred",
                "Buckets",
                "Rate",
                "Time remaining",
                "Error",
            ]
        )
        nodes_layout.addWidget(self.nodes)
        nodes_layout.addWidget(
            label(
                "Scheduled buckets have not necessarily arrived yet.",
                "hint",
                wrap=True,
            )
        )
        self.body.addWidget(nodes_card)
        self.buckets_card, bucket_layout = card("Buckets by source node")
        self.buckets = Table(["Destination node", "Source node", "Scheduled", "Total"])
        bucket_layout.addWidget(self.buckets)
        self.body.addWidget(self.buckets_card)
        details_card, details_layout = card("Transfer details")
        self.details_card = details_card
        self.details = KeyValues()
        details_layout.addWidget(self.details)
        details_layout.addWidget(
            label(
                "Rate and ETA are unavailable when counters are missing or stale. Closing this app "
                "does not cancel server tasks.",
                "hint",
                wrap=True,
            )
        )
        self.body.addWidget(details_card)
        self.finish_body()
        # `clicked` carries a `checked` flag; the lambda keeps it out of cancel().
        self.cancel_button = self.add_action("Cancel transfer", "", lambda: self.cancel())
        self.refresh_button = self.add_action("Refresh status", "", self.poll)
        self.resume_button = self.add_action("Resume migration", "primary", self.resume)
        self.back_button = self.add_action("Go back", "", self.back_from_remainder)
        self.start_remainder_button = self.add_action(
            "Start remaining transfer", "primary", self.start_remainder
        )
        self.verify_button = self.add_action("Verify imported history", "primary", self.verify)

    def poll(self) -> None:
        if self.app.state.stale:
            self.app.reconnect_environments()
        else:
            self.app.poll_status()

    def cancel(self, confirmed: bool | None = None) -> bool:
        """Confirm, then ask the destination to cancel the observed task. Returns whether sent."""
        status = self.app.state.status
        if not status or status["status"] != "running":
            return False
        if confirmed is None:
            answer = QMessageBox.question(
                self,
                "Cancel transfer",
                "Ask the destination to cancel this transfer? Buckets already received "
                "stay on the destination; the cancellation cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            confirmed = answer == QMessageBox.StandardButton.Yes
        if not confirmed:
            return False
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling…")
        self.app.submit("stop", "stop", task_id=status["id"])
        return True

    def resume(self) -> None:
        if self.app.state.run_id:
            self.app.submit("resume", "resume", run_id=self.app.state.run_id)
            self.refresh()

    def back_from_remainder(self) -> None:
        run = self.app.state.plan_run
        if run and run.get("resume_parent"):
            self.app.select_run(run["resume_parent"])

    def start_remainder(self) -> None:
        state = self.app.state
        if state.plan_run and state.plan_run.get("resume_root"):
            self.app.submit(
                "start", "start", run_id=state.plan_run["id"], acknowledged=state.acknowledged
            )
            self.refresh()

    def verify(self) -> None:
        self.app.show_page("verification")
        self.app.pages["verification"].verify()

    def refresh(self) -> None:
        state = self.app.state
        settings = state.settings
        uncertain = (bool(state.start_error) and not vm.is_pre_post(state.start_error)) or bool(
            state.status and state.status.get("start_outcome") == "unknown"
        )
        view = vm.progress_view(
            state.status,
            stale=state.stale,
            last_ok=state.status_at,
            zone=settings.timezone,
            hosts=self.app.hosts(),
            start_uncertain=uncertain,
        )
        if (
            state.start_error
            and not uncertain
            and (view["state"] == "idle" or vm.is_pre_post(state.start_error))
        ):
            view["banner"] = (
                "bad",
                "Migration did not start",
                vm.start_note(state.start_error),
            )
        if state.stale:
            subtitle = "Connection to status API lost; last observed progress retained"
        elif view["state"] == "failed":
            subtitle = "Server-reported failure; review what remains"
        elif state.connected:
            subtitle = f"Refreshed every {settings.refresh_seconds} seconds"
        else:
            subtitle = "Connect both environments to read API status"
        preview = bool(
            state.plan_run
            and state.plan_run.get("resume_root")
            and "source_by_bucket" not in state.plan_run
            and state.started_run_id != state.plan_run["id"]
        )
        if preview:
            plan = state.plan_run["plan"]
            received = plan.get("already_received", [])
            remaining = plan["selected"]
            estimate = plan.get("estimate", {})
            self.remainder.setText(
                f"Already received: {len(received)} bucket{'s' if len(received) != 1 else ''} · "
                f"{vm.format_bytes(sum(b['size'] for b in received))}.\n"
                f"Still to transfer: {len(remaining)} buckets · "
                f"{vm.format_bytes(sum(b['size'] for b in remaining))}.\n"
                f"Estimated time: {vm.format_eta(estimate.get('low_seconds'))} to "
                f"{vm.format_eta(estimate.get('high_seconds'))}. "
                + (
                    "Estimated space fits on each destination. "
                    if plan["capacity"]["fits"] and not plan["capacity"]["unknown"]
                    else "Check the destination space warning before continuing. "
                )
                + "Space is checked again before starting."
            )
        self.remainder_card.setVisible(preview)
        self.metrics.setVisible(not preview)
        self.nodes_card.setVisible(not preview)
        self.details_card.setVisible(not preview)
        self.set_heading(
            "Ready to resume" if preview else "Migration",
            "Review what arrived and what remains" if preview else subtitle,
        )
        status, title, text = view["banner"]
        if preview and not state.start_error:
            status, title, text = (
                "info",
                "Ready to resume",
                "Review what remains before starting.",
            )
        set_banner(self.banner, title, text, status)
        for (value, caption), (_title, text_value, text_caption) in zip(
            self.tiles, view["tiles"], strict=True
        ):
            value.setText(text_value)
            caption.setText(text_caption)
        if view["progress"] is None:
            self.progress.hide()
        else:
            self.progress.setValue(view["progress"])
            self.progress.show()
        self.nodes.set_rows(view["nodes"])
        self.buckets.set_rows(view["bucket_rows"])
        self.buckets_card.setVisible(not preview and bool(view["bucket_rows"]))
        self.details.set_rows(view["details"] or [("API task", "None observed")])
        running = view["state"] == "running" and not state.stale
        finished = view["state"] in {"completed", "failed", "cancelled"}
        self.cancel_button.setVisible(running)
        if "stop" not in self.app.worker.pending:
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("Cancel transfer")
        self.refresh_button.setText("Reconnect and refresh" if state.stale else "Refresh status")
        self.refresh_button.setProperty("variant", "primary" if state.stale else "")
        self.refresh_button.style().unpolish(self.refresh_button)
        self.refresh_button.style().polish(self.refresh_button)
        self.refresh_button.setEnabled(state.connected)
        self.refresh_button.setVisible(not preview)
        self.back_button.setVisible(preview)
        self.verify_button.setVisible(finished and not state.stale and state.run_id is not None)
        self.resume_button.setVisible(
            finished and not state.stale and not preview and bool(state.run_id)
        )
        self.resume_button.setEnabled("resume" not in self.app.worker.pending)
        self.start_remainder_button.setVisible(preview)
        can_start = False
        if preview:
            can_start, _ = vm.start_state(
                vm.plan_view(state.plan_run, state.discovered),
                state.acknowledged,
                state.connected,
                False,
            )
        self.start_remainder_button.setEnabled(can_start and "start" not in self.app.worker.pending)
        if preview:
            self.note.setText("Review the remainder, then start the remaining transfer.")
        elif state.stale:
            self.note.setText(
                "Refreshing reads existing API state. It does not start or resume another transfer."
            )
        elif view["state"] == "failed":
            self.note.setText("Resume checks what arrived before offering the remainder.")
        elif running:
            self.note.setText(
                "Cancel asks the server to stop and keeps imported data. "
                "Verify the imported history after the transfer finishes."
            )
        else:
            self.note.setText("Refresh checks the destination for the latest status.")
