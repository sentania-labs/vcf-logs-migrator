from __future__ import annotations

from PySide6.QtWidgets import QLineEdit, QWidget

from .. import viewmodel as vm
from ..widgets import (
    Page,
    Table,
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


def _secret() -> QLineEdit:
    widget = QLineEdit()
    widget.setEchoMode(QLineEdit.EchoMode.Password)
    widget.setPlaceholderText("Session memory only")
    return widget


class ConnectionsPage(Page):
    def __init__(self, app):
        super().__init__("Connections")
        self.app = app
        settings = app.state.settings
        self.error = banner("", "", "bad")
        self.error.hide()
        self.preview = banner(
            "9.1.1 detected · Preview",
            "9.0 → 9.1.1 lab qualification remains outstanding. Verify query counts and "
            "partition routing after import.",
            "warn",
        )
        self.preview.hide()
        self.body.addWidget(self.error)
        self.body.addWidget(self.preview)

        source, source_layout = card("Source")
        self.source_host = QLineEdit(settings.source_host)
        self.source_host.setPlaceholderText("Any cluster member, e.g. aria-logs.lab.example")
        self.ssh_user = QLineEdit(settings.source_ssh_user)
        self.ssh_password = _secret()
        self.source_user = QLineEdit(settings.source_api_user)
        self.source_password = _secret()
        source_layout.addWidget(field("Cluster member", self.source_host))
        source_layout.addWidget(
            columns(field("SSH user", self.ssh_user), field("SSH password", self.ssh_password))
        )
        source_layout.addWidget(
            columns(
                field("Logs API user", self.source_user),
                field("Logs API password", self.source_password),
            )
        )
        self.source_badge = badge("Not tested")
        source_layout.addWidget(row(button("Test source", "", self.connect), self.source_badge))
        source_layout.addWidget(
            label(
                "Root SSH lets destination nodes read history. Tests cover every source node; "
                "no source changes.",
                "hint",
                wrap=True,
            )
        )
        source_layout.addStretch(1)

        destination, destination_layout = card("Destination")
        self.destination_host = QLineEdit(settings.destination_host)
        self.destination_host.setPlaceholderText("vcf-logs.lab.example:9543")
        self.destination_user = QLineEdit(settings.destination_api_user)
        self.destination_password = _secret()
        self.ops_url = QLineEdit(settings.ops_url)
        self.ops_url.setPlaceholderText("https://ops.lab.example")
        self.ops_token = _secret()
        destination_layout.addWidget(field("Logs host and port", self.destination_host))
        self.password_fields = columns(
            field("Logs API user", self.destination_user),
            field("Logs API password", self.destination_password),
        )
        self.token_fields = QWidget()
        token_layout = columns(
            field("VCF Operations URL", self.ops_url), field("VCF Operations token", self.ops_token)
        )
        self.token_fields = token_layout
        self.token_fields.hide()
        destination_layout.addWidget(self.password_fields)
        destination_layout.addWidget(self.token_fields)
        self.destination_badge = badge("Not tested")
        self.destination_button = button("Test destination", "", self.connect)
        destination_layout.addWidget(row(self.destination_button, self.destination_badge))
        destination_layout.addWidget(
            label(
                "Test detects 9.1.1 and switches this card to VCF Operations token sign-in.",
                "hint",
                wrap=True,
            )
        )
        destination_layout.addStretch(1)
        self.body.addWidget(columns(source, destination))

        checks, checks_layout = card("Connection checks")
        self.checks = Table(["Check", "Result"])
        checks_layout.addWidget(self.checks)
        self.body.addWidget(checks)

        self.before_import = banner(
            "Before import",
            "Matching partition filters and required extracted fields must exist on 9.1.1. "
            "Configure the four supported rule types here; prepare missing extracted fields "
            "in the destination UI.",
            "info",
        )
        self.before_import.hide()
        self.body.addWidget(self.before_import)
        self.finish_body()

        self.note.setText("Connection tests are read only.")
        self.disconnect_button = self.add_action("Disconnect", "", self.disconnect)
        self.compare_button = self.add_action(
            "Compare configuration", "primary", lambda: self.app.show_page("configuration")
        )
        self.refresh()

    def credentials(self) -> dict[str, str]:
        return {
            "source_ssh": self.ssh_password.text(),
            "source_api": self.source_password.text(),
            "destination_api": self.destination_password.text(),
            "ops_token": self.ops_token.text(),
        }

    def remember_addresses(self) -> None:
        settings = self.app.state.settings
        settings.source_host = self.source_host.text().strip()
        settings.source_ssh_user = self.ssh_user.text().strip()
        settings.source_api_user = self.source_user.text().strip()
        settings.destination_host = self.destination_host.text().strip()
        settings.destination_api_user = self.destination_user.text().strip()
        settings.ops_url = self.ops_url.text().strip()
        self.app.save_settings()

    def connect(self) -> None:
        self.remember_addresses()
        self.source_badge.setText("Testing…")
        self.destination_badge.setText("Testing…")
        self.app.connect_environments(self.credentials())

    def disconnect(self) -> None:
        for widget in (
            self.ssh_password,
            self.source_password,
            self.destination_password,
            self.ops_token,
        ):
            widget.clear()
        self.app.disconnect_environments()

    def refresh(self) -> None:
        state = self.app.state
        discovered = state.discovered
        if state.connect_error:
            set_banner(
                self.error,
                "Connection failed",
                vm.describe_error(state.connect_error) + " No migration request was submitted.",
                "bad",
            )
            self.error.show()
        else:
            self.error.hide()
        if discovered is None:
            self.source_badge.setText("Not tested" if not state.connect_error else "Failed")
            self.destination_badge.setText("Not tested" if not state.connect_error else "Failed")
            for widget in (self.source_badge, self.destination_badge):
                widget.setProperty("status", "bad" if state.connect_error else "")
                widget.style().unpolish(widget)
                widget.style().polish(widget)
            self.set_heading("Connections", "Use a member address for each cluster")
        else:
            source, destination = discovered["source"], discovered["destination"]
            self.source_badge.setText(f"{vm.format_version(source['version'])} · simulator")
            self.destination_badge.setText(
                f"{vm.format_version(destination['version'])} · simulator"
            )
            for widget in (self.source_badge, self.destination_badge):
                widget.setProperty("status", "ok")
                widget.style().unpolish(widget)
                widget.style().polish(widget)
            self.set_heading(
                "Connections" + (" · 9.1.1 Preview" if state.preview else ""),
                f"{vm.format_version(source['version'])} → "
                f"{vm.format_version(destination['version'])} · "
                "use a member address for each cluster"
                if not state.preview
                else "Same connection step; version selects the authentication method",
            )
        self.preview.setVisible(state.preview)
        self.before_import.setVisible(state.preview)
        self.token_fields.setVisible(state.preview)
        self.password_fields.setVisible(not state.preview)
        self.destination_button.setText(
            "Exchange token and test" if state.preview else "Test destination"
        )
        self.checks.set_rows(
            vm.connection_checks(discovered, discovered.get("compatible") if discovered else None)
        )
        self.disconnect_button.setEnabled(state.connected)
        self.compare_button.setEnabled(state.connected)
