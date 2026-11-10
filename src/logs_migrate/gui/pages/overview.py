from __future__ import annotations

from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from .. import viewmodel as vm
from ..widgets import KeyValues, Page, button, card, columns, label, row


class OverviewPage(Page):
    def __init__(self, app):
        super().__init__("Migrate log history")
        self.app = app
        self.stack = QStackedWidget()
        self.body.addWidget(self.stack)
        self.stack.addWidget(self._empty())
        self.stack.addWidget(self._connected())
        self.finish_body()

    def _empty(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        connect, connect_layout = card("Connect your environments")
        connect_layout.addWidget(
            label(
                "Connect a source and destination, copy the configuration rules you need, "
                "then select the history to migrate.",
                "body",
                wrap=True,
            )
        )
        connect_layout.addWidget(
            label(
                "Historical data transfers directly between the appliances. "
                "The source is read only.",
                "body",
                wrap=True,
            )
        )
        connect_layout.addWidget(
            row(
                button("Connect environments", "primary", lambda: self.app.show_page("connections"))
            )
        )
        layout.addWidget(connect)
        layout.addStretch(1)
        return holder

    def _connected(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        source, source_layout = card("Source")
        self.source_rows = KeyValues()
        source_layout.addWidget(self.source_rows)
        destination, destination_layout = card("Destination")
        self.destination_rows = KeyValues()
        destination_layout.addWidget(self.destination_rows)
        layout.addWidget(columns(source, destination))
        config, config_layout = card("Configuration")
        config_layout.addWidget(
            label("Forwarding, partitions, masking and filtering rules.", "body", wrap=True)
        )
        self.config_line = label("", "body", wrap=True)
        config_layout.addWidget(self.config_line)
        config_layout.addWidget(
            row(button("Review rules", "", lambda: self.app.show_page("configuration")))
        )
        config_layout.addStretch(1)
        plan, plan_layout = card("History plan")
        self.plan_rows = KeyValues()
        plan_layout.addWidget(self.plan_rows)
        plan_layout.addWidget(
            row(button("Review dates", "primary", lambda: self.app.show_page("plan")))
        )
        plan_layout.addStretch(1)
        layout.addWidget(columns(config, plan))
        return holder

    def refresh(self) -> None:
        state = self.app.state
        if not state.connected or state.discovered is None:
            self.set_heading(
                "Migrate log history", "Configuration and historical data through the product APIs"
            )
            self.note.setText("")
            self.stack.setCurrentIndex(0)
            return
        source, destination = state.discovered["source"], state.discovered["destination"]
        task = state.status["status"] if state.status else "idle"
        self.set_heading(
            "Overview",
            "Connected · "
            + (
                "no history transfer running"
                if task in {"idle", "completed", "cancelled", "failed"}
                else f"API task {task.upper()}"
            ),
        )
        extent = vm.history_extent(source)
        sealed = [b for b in source["buckets"] if b["status"] != "active"]
        hosts = self.app.hosts() or (source["id"], destination["id"])
        self.source_rows.set_rows(
            [
                (
                    "Cluster",
                    f"{hosts[0]} ({source['id']})" if hosts[0] != source["id"] else source["id"],
                ),
                ("Version", vm.cluster_summary(source)),
                (
                    "Available history",
                    vm.format_day_range(*extent) if extent else vm.UNAVAILABLE,
                ),
                ("Sealed events", vm.format_count(vm.selected_message_count(sealed))),
            ]
        )
        self.destination_rows.set_rows(
            [
                (
                    "Cluster",
                    f"{hosts[1]} ({destination['id']})"
                    if hosts[1] != destination["id"]
                    else destination["id"],
                ),
                ("Version", vm.cluster_summary(destination)),
                (
                    "Existing history",
                    f"{len(destination['buckets'])} bucket"
                    + ("s" if len(destination["buckets"]) != 1 else "")
                    if not state.preview
                    else "Not exposed by 9.1.1",
                ),
                ("API connection", "Connected" if not state.stale else "Status unavailable"),
            ]
        )
        if state.config_result:
            applied = sum(
                1 for r in state.config_result["rules"] if r["status"] in {"created", "updated"}
            )
            text = f"{applied} rule{'s' if applied != 1 else ''} applied"
            if state.config_result["restart_required"]:
                text += "; new partition active after destination restart"
            self.config_line.setText(text + ".")
        elif state.comparison:
            pending = sum(1 for r in state.comparison["rules"] if r["status"] != "identical")
            self.config_line.setText(
                f"{pending} rule{'s' if pending != 1 else ''} differ from the destination."
            )
        else:
            self.config_line.setText("Not compared yet.")
        if state.plan_run:
            view = vm.plan_view(state.plan_run, state.discovered)
            started = "source_by_bucket" in state.plan_run
            verification = state.plan_run.get("verification")
            self.plan_rows.set_rows(
                [
                    ("Requested", view["requested"]),
                    ("Sealed coverage", view["included"]),
                    (
                        "Imported" if started else "To import",
                        f"{vm.format_count(view['events'])} events · "
                        f"{vm.format_bytes(view['size'])}",
                    ),
                    (
                        "Latest data",
                        "Open; excluded" if view["active_count"] else "No open groups",
                    ),
                    (
                        "Verification",
                        vm.outcome_label(verification).capitalize() if started else "Not started",
                    ),
                ]
            )
        else:
            self.plan_rows.set_rows([("Requested", "No window previewed")])
        self.note.setText(
            "API migration does not stop services. Source data is never changed or deleted."
        )
        self.stack.setCurrentIndex(1)
