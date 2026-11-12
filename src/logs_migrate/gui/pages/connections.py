from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QLineEdit

from .. import viewmodel as vm
from ..widgets import (
    Page,
    Table,
    badge,
    banner,
    card,
    columns,
    field,
    label,
    set_banner,
    set_status,
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
        self.validation = ""
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
        source_layout.addWidget(self.source_badge)
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
        self.destination_ssh_user = QLineEdit(settings.destination_ssh_user)
        self.destination_ssh_user.setPlaceholderText("root (optional)")
        self.destination_ssh_password = _secret()
        self.ops_url = QLineEdit(settings.ops_url)
        self.ops_url.setPlaceholderText("https://ops.lab.example")
        self.ops_token = _secret()
        destination_layout.addWidget(field("Logs host and port", self.destination_host))
        self.password_fields = columns(
            field("Destination Logs API user", self.destination_user),
            field("Destination Logs API password", self.destination_password),
        )
        self.ssh_fields = columns(
            field("Destination SSH user (optional)", self.destination_ssh_user),
            field("Destination SSH password", self.destination_ssh_password),
        )
        self.ops_url_field = field("VCF Operations URL (9.1.1 only)", self.ops_url)
        self.token_fields = field("VCF Operations token", self.ops_token)
        self.token_fields.hide()
        destination_layout.addWidget(self.ops_url_field)
        destination_layout.addWidget(self.password_fields)
        destination_layout.addWidget(self.ssh_fields)
        destination_layout.addWidget(self.token_fields)
        destination_layout.addWidget(
            label(
                "Root SSH to the 9.0 destination is optional: it adds each node's repository "
                "budget and bucket inventory to the plan. Without it the API's own 95% disk "
                "stop is the only storage guard.",
                "hint",
                wrap=True,
            )
        )
        self.destination_badge = badge("Not tested")
        destination_layout.addWidget(self.destination_badge)
        self.switch_hint = label(
            "To sign in to 9.1.1 with an Ops token, enter its VCF Operations URL above.",
            "hint",
            wrap=True,
        )
        destination_layout.addWidget(self.switch_hint)
        destination_layout.addStretch(1)
        self.body.addWidget(columns(source, destination))
        # Enter in any field runs the same test the buttons run.
        self.ops_url.textChanged.connect(lambda _text: self.refresh())
        for edit in self.inputs():
            edit.returnPressed.connect(self.connect)
            edit.textChanged.connect(lambda _text: self.refresh())

        simulator, simulator_layout = card("Simulator versions")
        self.sim_source_version = QLineEdit()
        self.sim_destination_version = QLineEdit()
        self.sim_source_nodes = QLineEdit()
        self.sim_destination_nodes = QLineEdit()
        self.sim_source_nodes.setPlaceholderText("Optional: node=version, node=version")
        self.sim_destination_nodes.setPlaceholderText("Optional: node=version, node=version")
        self.sim_api = QComboBox()
        self.sim_api.addItem("Use fixture setting", "")
        self.sim_api.addItem("Migration API available", "present")
        self.sim_api.addItem("Migration API missing", "absent")
        self.sim_api.addItem("Migration API cannot be checked", "unchecked")
        simulator_layout.addWidget(
            columns(
                field("Source version", self.sim_source_version),
                field("Destination version", self.sim_destination_version),
            )
        )
        simulator_layout.addWidget(
            columns(
                field("Source node versions", self.sim_source_nodes),
                field("Destination node versions", self.sim_destination_nodes),
            )
        )
        simulator_layout.addWidget(field("Migration API", self.sim_api))
        self.simulator_card = simulator
        simulator.setVisible(bool(settings.fixture_path))
        self.body.addWidget(simulator)

        checks, checks_layout = card("Connection checks")
        self.checks = Table(["Check", "Result"], wrap=True)
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
        self.test_button = self.add_action("Test connections", "primary", self.connect)
        self.compare_button = self.add_action(
            "Compare configuration", "primary", lambda: self.app.show_page("configuration")
        )
        self.refresh()

    def inputs(self) -> list[QLineEdit]:
        return [
            self.source_host,
            self.ssh_user,
            self.ssh_password,
            self.source_user,
            self.source_password,
            self.destination_host,
            self.destination_user,
            self.destination_password,
            self.destination_ssh_user,
            self.destination_ssh_password,
            self.ops_url,
            self.ops_token,
        ]

    def values(self) -> dict[str, str]:
        return {
            "source_host": self.source_host.text(),
            "ssh_user": self.ssh_user.text(),
            "ssh_password": self.ssh_password.text(),
            "source_user": self.source_user.text(),
            "source_password": self.source_password.text(),
            "destination_host": self.destination_host.text(),
            "destination_user": self.destination_user.text(),
            "destination_password": self.destination_password.text(),
            "destination_ssh_user": self.destination_ssh_user.text(),
            "destination_ssh_password": self.destination_ssh_password.text(),
            "ops_url": self.ops_url.text(),
            "ops_token": self.ops_token.text(),
        }

    def credentials(self) -> dict[str, str]:
        return {
            "source_ssh": self.ssh_password.text(),
            "source_api": self.source_password.text(),
            "destination_api": self.destination_password.text(),
            "destination_ssh": self.destination_ssh_password.text(),
            "ops_token": self.ops_token.text(),
        }

    def remember_addresses(self) -> None:
        settings = self.app.state.settings
        settings.source_host = self.source_host.text().strip()
        settings.source_ssh_user = self.ssh_user.text().strip()
        settings.source_api_user = self.source_user.text().strip()
        settings.destination_host = self.destination_host.text().strip()
        settings.destination_api_user = self.destination_user.text().strip()
        settings.destination_ssh_user = self.destination_ssh_user.text().strip()
        settings.ops_url = self.ops_url.text().strip()
        self.app.save_settings()

    def connect(self) -> None:
        self.remember_addresses()
        values = self.values()
        missing = vm.missing_fields(values, bool(self.ops_url.text().strip()))
        if missing:
            self.validation = "Enter " + ", ".join(missing) + " before testing."
            self.refresh()
            return
        self.validation = ""
        self.source_badge.setText("Testing…")
        self.destination_badge.setText("Testing…")
        if self.app.state.settings.fixture_path:
            self.app.connect_environments(self.credentials())
        else:
            self.app.probe_environments(self.credentials())

    def disconnect(self) -> None:
        for widget in (
            self.ssh_password,
            self.source_password,
            self.destination_password,
            self.destination_ssh_password,
            self.ops_token,
        ):
            widget.clear()
        self.validation = ""
        self.app.disconnect_environments()

    def refresh(self) -> None:
        state = self.app.state
        discovered = state.discovered
        self.simulator_card.setVisible(bool(state.settings.fixture_path))
        if self.validation:
            set_banner(self.error, "Required fields are empty", self.validation, "bad")
            self.error.show()
        elif state.connect_error:
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
            failed = bool(state.connect_error) and not self.validation
            for widget in (self.source_badge, self.destination_badge):
                widget.setText("Failed" if failed else "Not tested")
                set_status(widget, "bad" if failed else "")
            self.set_heading("Connections", "Use a member address for each cluster")
        else:
            source, destination = discovered["source"], discovered["destination"]
            mode = "live" if state.live else "simulator"
            self.source_badge.setText(f"{vm.format_version(source['version'])} · {mode}")
            self.destination_badge.setText(f"{vm.format_version(destination['version'])} · {mode}")
            for widget in (self.source_badge, self.destination_badge):
                set_status(widget, "ok")
            self.set_heading(
                "Connections" + (" · 9.1.1 Preview" if state.preview else ""),
                f"{vm.format_version(source['version'])} → "
                f"{vm.format_version(destination['version'])} · "
                "use a member address for each cluster"
                if not state.preview
                else "Same connection step; version selects the authentication method",
            )
        token = state.preview or bool(self.ops_url.text().strip())
        self.preview.setVisible(state.preview)
        self.before_import.setVisible(state.preview)
        self.token_fields.setVisible(token)
        self.password_fields.setVisible(not token)
        self.ssh_fields.setVisible(not token)
        self.switch_hint.setVisible(not token)
        missing = vm.missing_fields(self.values(), token)
        self.test_button.setEnabled(
            not missing
            and "connect" not in self.app.worker.pending
            and "probe" not in self.app.worker.pending
        )
        self.test_button.setText(
            "Test again" if discovered or state.connect_error else "Test connections"
        )
        self.test_button.setProperty("class", "" if state.connected else "primary")
        self.test_button.style().unpolish(self.test_button)
        self.test_button.style().polish(self.test_button)
        if missing:
            self.note.setText("Enter " + ", ".join(missing) + " to test both connections.")
        elif state.connected:
            self.note.setText("Connections are ready. Continue to configuration.")
        else:
            self.note.setText("Test both connections to continue.")
        rows = vm.connection_checks(
            discovered, discovered.get("compatible") if discovered else None, state.mode
        )
        if state.checks:
            rows = state.checks + rows[1:]
        if state.selftest:
            rows = vm.selftest_rows(state.selftest) + rows
        elif state.selftest_error:
            rows = [("Self-test", "Failed · " + vm.describe_error(state.selftest_error))] + rows
        self.checks.set_rows(rows)
        self.disconnect_button.setEnabled(state.connected)
        self.compare_button.setEnabled(state.connected)
