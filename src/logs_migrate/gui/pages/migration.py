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
        self.body.addWidget(columns(*frames))
        nodes_card, nodes_layout = card("Per-node API progress")
        self.nodes = Table(["Destination node", "API state", "Transferred", "Buckets", "Error"])
        nodes_layout.addWidget(self.nodes)
        nodes_layout.addWidget(
            label(
                "Progress and scheduled bucket counts come from the destination API. "
                "Scheduled buckets have not necessarily been received yet.",
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
        self.cancel_button = self.add_action("Cancel API task", "", lambda: self.cancel())
        self.refresh_button = self.add_action("Refresh status", "", self.poll)
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
                "Cancel API task",
                f"Ask the destination to cancel task {status['id']}? Buckets already received "
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
        if state.start_error and view["state"] == "idle" and not uncertain:
            view["banner"] = (
                "bad",
                "Migration did not start",
                vm.start_note(state.start_error),
            )
        if state.stale:
            subtitle = "Connection to status API lost; last observed progress retained"
        elif view["state"] == "failed":
            subtitle = "Server-reported failure; no separate recovery workflow"
        elif state.connected:
            subtitle = f"Direct API status · refreshed every {settings.refresh_seconds} seconds"
        else:
            subtitle = "Connect both environments to read API status"
        self.set_heading("Migration", subtitle)
        status, title, text = view["banner"]
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
        self.buckets_card.setVisible(bool(view["bucket_rows"]))
        self.details.set_rows(view["details"] or [("API task", "None observed")])
        running = view["state"] == "running" and not state.stale
        finished = view["state"] in {"completed", "failed", "cancelled"}
        self.cancel_button.setVisible(running)
        if "stop" not in self.app.worker.pending:
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("Cancel API task")
        self.refresh_button.setText("Reconnect and refresh" if state.stale else "Refresh status")
        self.refresh_button.setProperty("variant", "primary" if state.stale else "")
        self.refresh_button.style().unpolish(self.refresh_button)
        self.refresh_button.style().polish(self.refresh_button)
        self.refresh_button.setEnabled(state.connected)
        self.verify_button.setVisible(finished and not state.stale and state.run_id is not None)
        if state.stale:
            self.note.setText(
                "Refreshing reads existing API state. It does not start or resume another transfer."
            )
        elif view["state"] == "failed":
            self.note.setText("No automatic resubmission. Overlapping new windows remain blocked.")
        elif running:
            self.note.setText(
                "Cancel asks the server to stop and keeps imported data. "
                "COMPLETED alone is not verified success."
            )
        else:
            self.note.setText("Status is read directly from the destination API on every refresh.")
