"""Step 1. Sign in to both systems with one test, and learn whether they can migrate."""

from __future__ import annotations

from html import escape

from PySide6.QtWidgets import QLineEdit

from .. import copies, copyview
from .. import viewmodel as vm
from ..widgets import (
    GridTable,
    Page,
    Slot,
    banner,
    card,
    columns,
    field,
    label,
    rich,
    set_status,
)

READY = (
    ("1. Both addresses", "The name you type in a browser to open each log system."),
    (
        "2. An admin sign-in for each",
        "The user and password you use on its web page as an administrator.",
    ),
    (
        "3. The source servers’ root password",
        "Whoever installed the old system has it. The destination uses it to fetch the files.",
    ),
    (
        "4. The destination servers’ root password",
        "Recommended. Without it, free space is not checked and a stopped copy cannot be "
        "continued.",
    ),
)
WITHOUT_SERVER = (
    "<b>Without it you can still copy, but</b> the app cannot check free disk space first, and "
    "a copy that stops part-way cannot be continued from this app."
)


def _secret() -> QLineEdit:
    widget = QLineEdit()
    widget.setEchoMode(QLineEdit.EchoMode.Password)
    return widget


class ConnectPage(Page):
    def __init__(self, app):
        super().__init__(
            app,
            "Connect to both systems",
            "Sign in to the system the logs are in now and the one they are going to.",
        )
        settings = app.state.settings
        self.editing = False  # Back at the form after a version refusal, reason kept in view.
        self.top = Slot(spacing=10)
        self.form = self._form(settings)
        self.form.persistent = True
        for edit in self.inputs():
            edit.returnPressed.connect(self.test)
            edit.textEdited.connect(lambda _text, e=edit: self._edited(e))

    def _form(self, settings):
        source, layout = card("Source", note="the system the logs are in now")
        self.source_host = QLineEdit(settings.source_host)
        self.source_host.setPlaceholderText("logs-old.example.com")
        self.source_user = QLineEdit(settings.source_api_user)
        self.source_password = _secret()
        self.ssh_user = QLineEdit(settings.source_ssh_user)
        self.ssh_password = _secret()
        self.source_host_hint = label("", "hint", wrap=True)
        self.source_password_hint = label("", "hint", "bad", wrap=True)
        self.source_server_hint = rich("", "hint")
        layout.addWidget(field("Address", self.source_host)[0])
        layout.addWidget(self.source_host_hint)
        layout.addWidget(
            columns(
                field("Logs admin user", self.source_user)[0],
                field("Logs admin password", self.source_password)[0],
            )
        )
        layout.addWidget(self.source_password_hint)
        layout.addWidget(
            columns(
                field("Server user (usually root)", self.ssh_user)[0],
                field("Server password", self.ssh_password)[0],
            )
        )
        layout.addWidget(self.source_server_hint)
        layout.addStretch(1)

        destination, layout = card("Destination", note="the system the logs are going to")
        self.destination_host = QLineEdit(settings.destination_host)
        self.destination_host.setPlaceholderText("logs-new.example.com")
        self.destination_user = QLineEdit(settings.destination_api_user)
        self.destination_password = _secret()
        self.destination_ssh_user = QLineEdit(settings.destination_ssh_user)
        self.destination_ssh_password = _secret()
        self.ops_url = QLineEdit(settings.ops_url)
        self.ops_url.setPlaceholderText("ops.example.com")
        self.ops_token = _secret()
        self.destination_host_hint = label("", "hint", wrap=True)
        self.destination_password_hint = rich("", "hint", self._link)
        self.destination_server_hint = rich("", "hint")
        layout.addWidget(field("Address", self.destination_host)[0])
        layout.addWidget(self.destination_host_hint)
        self.password_row = columns(
            field("Logs admin user", self.destination_user)[0],
            field("Logs admin password", self.destination_password)[0],
        )
        self.token_row = columns(
            field("VCF Operations address", self.ops_url)[0],
            field("Access token", self.ops_token)[0],
        )
        layout.addWidget(self.password_row)
        layout.addWidget(self.token_row)
        layout.addWidget(self.destination_password_hint)
        server_user, self.server_user_caption = field(
            "Server user (recommended)", self.destination_ssh_user
        )
        self.server_row = columns(
            server_user, field("Server password", self.destination_ssh_password)[0]
        )
        layout.addWidget(self.server_row)
        layout.addWidget(self.destination_server_hint)
        layout.addStretch(1)
        return columns(source, destination)

    # Values ------------------------------------------------------------------------------

    def inputs(self) -> list[QLineEdit]:
        return [
            self.source_host,
            self.source_user,
            self.source_password,
            self.ssh_user,
            self.ssh_password,
            self.destination_host,
            self.destination_user,
            self.destination_password,
            self.ops_url,
            self.ops_token,
            self.destination_ssh_user,
            self.destination_ssh_password,
        ]

    def values(self) -> dict[str, str]:
        return {
            "source_host": self.source_host.text(),
            "source_password": self.source_password.text(),
            "ssh_password": self.ssh_password.text(),
            "destination_host": self.destination_host.text(),
            "destination_password": self.destination_password.text(),
            "destination_ssh_user": self.destination_ssh_user.text(),
            "destination_ssh_password": self.destination_ssh_password.text(),
            "ops_url": self.ops_url.text(),
            "ops_token": self.ops_token.text(),
        }

    def credentials(self) -> dict[str, str]:
        token = self.app.state.settings.destination_token
        return {
            "source_ssh": self.ssh_password.text(),
            "source_api": self.source_password.text(),
            "destination_api": "" if token else self.destination_password.text(),
            "destination_ssh": ""
            if token or not self.destination_ssh_user.text().strip()
            else self.destination_ssh_password.text(),
            "ops_token": self.ops_token.text() if token else "",
        }

    def need(self) -> list[str]:
        state = self.app.state
        return vm.need_list(self.values(), state.settings.destination_token, state.server_needed)

    def clear_secrets(self) -> None:
        for widget in (
            self.ssh_password,
            self.source_password,
            self.destination_password,
            self.destination_ssh_password,
            self.ops_token,
        ):
            widget.clear()
        self.editing = False

    def remember(self) -> None:
        """Addresses and user names are remembered; passwords never are."""
        settings = self.app.state.settings
        settings.source_host = self.source_host.text().strip()
        settings.source_ssh_user = self.ssh_user.text().strip() or "root"
        settings.source_api_user = self.source_user.text().strip() or "admin"
        settings.destination_host = self.destination_host.text().strip()
        settings.destination_api_user = self.destination_user.text().strip() or "admin"
        settings.destination_ssh_user = self.destination_ssh_user.text().strip()
        settings.ops_url = self.ops_url.text().strip()
        self.app.save_settings()

    # Actions -----------------------------------------------------------------------------

    def test(self) -> None:
        if self.need() or self.app.busy("connect"):
            return
        self.remember()
        self.editing = False
        self.app.test_connections(self.credentials())

    def edit(self) -> None:
        """Back to the boxes; a version refusal stays in view above them."""
        self.editing = True
        self.app.refresh_all()

    def sign_out(self) -> None:
        self.app.ask(
            "Sign out of both systems?",
            [
                "The passwords are forgotten. A copy that is running carries on at the "
                "destination. Past runs stay on this computer."
            ],
            [("Stay signed in", None), ("Sign out", self.app.sign_out, "primary")],
        )

    def _edited(self, edit: QLineEdit) -> None:
        state = self.app.state
        failure = state.failure
        if failure and not failure.get("edited"):
            marked = self._marked(failure)
            if edit in marked:
                failure["edited"] = True
        if (
            state.server_needed
            and self.destination_ssh_user.text().strip()
            and (self.destination_ssh_password.text())
        ):
            pass  # Both boxes are filled; the marks clear on the next test.
        self.refresh()

    def _marked(self, failure: dict) -> list[QLineEdit]:
        side, name = failure.get("side"), failure.get("field")
        boxes = {
            ("source", "address"): [self.source_host],
            ("source", "password"): [self.source_password],
            ("source", "server_password"): [self.ssh_password],
            ("destination", "address"): [self.destination_host],
            ("destination", "password"): [self.destination_password],
            ("destination", "server_password"): [self.destination_ssh_password],
            ("destination", "token"): [self.ops_token],
            ("destination", "ops_address"): [self.ops_url],
        }
        return boxes.get((side, name), [])

    def _link(self, href: str) -> None:
        settings = self.app.state.settings
        if href in {"token", "password"}:
            settings.destination_token = href == "token"
            self.app.state.token_notice = False
            self.app.save_settings()
            self.refresh()
        elif href == "codes":
            self.app.submit("codes", "identity_codes")

    def show_codes(self, codes: dict) -> None:
        """The recorded identity of the two addresses and of every server behind them."""
        names = {"source": "source", "destination": "destination", "ops": "VCF Operations"}
        addresses = GridTable(
            ["System", "Certificate fingerprint (SHA-256), as a browser shows it"], stretch=[2, 5]
        )
        rows = []
        for role, host, digest in codes["addresses"]:
            pairs = vm.fingerprint_pairs(digest)
            half = len(pairs) // 2 + 1
            rows.append(
                (
                    f"{escape(host)} ({names[role]})",
                    '<span style="font-family:monospace;font-size:11.5px">'
                    f"{pairs[:half]}<br>{pairs[half:]}</span>",
                )
            )
        addresses.set_rows(rows)
        servers = GridTable(
            ["Server", "Server key fingerprint, as that server’s administrator sees it"],
            stretch=[2, 5],
        )
        server_rows = [
            (escape(name), f'<span style="font-family:monospace;font-size:11.5px">{key}</span>')
            for name, key in codes["servers"]
        ]
        if not codes["destination_contacted"]:
            server_rows.append(
                (
                    "The destination’s servers",
                    "Not contacted: no destination server sign-in was given",
                )
            )
        servers.set_rows(server_rows)
        everything = "\n".join(
            [
                f"{host}  {vm.fingerprint_pairs(digest)}"
                for _role, host, digest in codes["addresses"]
            ]
            + [f"{name}  {key}" for name, key in codes["servers"]]
        )
        self.app.ask(
            "Identity codes",
            [
                rich(
                    "Recorded the first time each was contacted. The app warns you if one "
                    "ever changes.",
                    "soft",
                ),
                label("The two addresses", "subhead"),
                addresses,
                label("The servers behind them", "subhead"),
                servers,
            ],
            [
                ("Close", None, "primary"),
                ("Copy all codes", lambda: self.app.copy_text(everything, "Codes copied")),
            ],
        )

    # Drawing -----------------------------------------------------------------------------

    def _resume_text(self, signed_in: bool) -> tuple[str, str, str] | None:
        state = self.app.state
        resume = state.resume
        if resume is None:
            return None
        zone = state.settings.timezone
        copy = state.copy(resume["copy"])
        if resume["kind"] == "back":
            if signed_in:
                if not state.server_sign_in:
                    return (
                        "warn",
                        "The destination’s server sign-in is still missing.",
                        "The stopped copy cannot be continued without it. Change the sign-in "
                        "details, fill in the destination’s server user and password, and "
                        "test again.",
                    )
                return (
                    "info",
                    "Now the stopped copy can be continued.",
                    "The app can now see what arrived on the destination. Go back to the copy "
                    "to continue it.",
                )
            return None
        if copy is None:
            return None
        dates = copyview.dates_text(copy)
        when = vm.format_clock(copy["started_at"], zone)
        percent = int(100 * copy["done_bytes"] / copy["total_bytes"]) if copy["total_bytes"] else 0
        if resume["kind"] == "missing":
            result = copies.check(copy)
            ranges = vm.format_ranges(
                [(i["first"], i["last"]) for i in result["issues"] if i["kind"] == "missing"]
                or copies.day_ranges(
                    day for g in copies.remaining_groups(copy) for day in copies.group_days(g)
                )
            )
            if signed_in:
                return (
                    "info",
                    "Ready to copy what is missing.",
                    f"Part of {ranges} from the run started {when} never arrived. The next "
                    "screen copies just that.",
                )
            return (
                "info",
                "Sign in to copy what is missing.",
                f"In the run started {when}, part of {ranges} never arrived. Enter the "
                "passwords and test the connections, and the app takes you straight to "
                "copying it.",
            )
        running = copy["state"] in {"running", "unsure"}
        if signed_in:
            if running:
                return (
                    "info",
                    "Your copy is ready to look at.",
                    f"The copy of {dates} was still running when it was last seen. The next "
                    "screen shows how far it has got.",
                )
            return (
                "info",
                "Your unfinished copy is ready to continue.",
                f"The copy of {dates} stopped at {percent}%. What arrived is still on the "
                "destination. The next screen shows what is left; nothing starts until you "
                "say so.",
            )
        title = (
            "Sign in to continue this copy."
            if resume.get("chosen")
            else "Welcome back. A copy is still unfinished."
        )
        if running:
            return (
                "info",
                title if resume.get("chosen") else "Welcome back. A copy may still be running.",
                f"The copy of {dates}, started {when}, was still running when the app was last "
                "closed. Enter the passwords and test the connections, and the app takes you "
                "back to it. Passwords are never saved, so they are asked for each time.",
            )
        return (
            "info",
            title,
            f"The copy of {dates}, started {when}, stopped at {percent}%. Enter the passwords "
            "and test the connections, and the app takes you back to it so you can continue. "
            "Passwords are never saved, so they are asked for each time.",
        )

    def _result_cards(self, view: dict):
        state = self.app.state
        discovered = state.discovered
        inventory = state.inventory

        def one(side: str, title: str):
            tag = view[f"{side}_badge"]
            frame, layout = card(title, ("bad", tag) if tag else ("ok", "Signed in"))
            info = discovered[side]
            layout.addWidget(label(info["host"], "big", wrap=True))
            servers = info["servers"]
            layout.addWidget(
                rich(
                    f"Version <b>{escape(info['version'])}</b>"
                    + (f" · {vm.plural(servers, 'server')}" if servers else ""),
                    "text",
                )
            )
            return frame, layout

        source, source_layout = one("source", "Source")
        destination, destination_layout = one("destination", "Destination")
        extent = vm.history((inventory or {}).get("source"))
        if extent:
            source_layout.addWidget(
                label(
                    f"Has logs from {vm.format_day(extent['first'])} to today.", "subtle", wrap=True
                )
            )
        elif self.app.busy("discover"):
            source_layout.addWidget(label("Listing its stored logs…", "subtle"))
        if state.connected:
            if not state.server_sign_in:
                text = "Free space not checked: no server sign-in was given."
            else:
                nodes = (inventory or {}).get("destination", {}).get("nodes", [])
                free = [n["repository_available"] for n in nodes]
                text = (
                    f"{vm.format_bytes(sum(free))} free across its "
                    f"{vm.plural(len(free), 'server')}."
                    if free and all(value is not None for value in free)
                    else ""
                )
            if text:
                destination_layout.addWidget(label(text, "subtle", wrap=True))
        source_layout.addStretch(1)
        destination_layout.addStretch(1)
        return columns(source, destination)

    def _update_form(self, busy: bool) -> None:
        state = self.app.state
        token = state.settings.destination_token
        failure = state.failure if not busy else None
        for edit in self.inputs():
            edit.setEnabled(not busy)
            set_status(edit, "")
        for edit in self._marked(failure) if failure and not failure.get("edited") else []:
            set_status(edit, "bad")
        self.password_row.setVisible(not token)
        self.token_row.setVisible(token)
        self.server_row.setVisible(not token)

        def hint(widget, bad_text, normal):
            widget.setText(bad_text or normal)
            set_status(widget, "bad" if bad_text else "")
            widget.setVisible(bool(bad_text or normal))

        servers = (failure or {}).get("servers")
        hint(
            self.source_host_hint,
            "This address answers, but this computer cannot reach its servers directly, and "
            "the destination must be able to as well. Ask whoever runs the network. "
            "Settings has a troubleshooting file with the details."
            if servers and failure["side"] == "source"
            else vm.field_hint(failure, "source", "address"),
            "Any one server in the cluster. The app finds the others.",
        )
        hint(self.source_password_hint, vm.field_hint(failure, "source", "password"), "")
        hint(
            self.source_server_hint,
            vm.field_hint(failure, "source", "server_password"),
            "The server sign-in lets the destination fetch the log files. It is sent to the "
            "destination during the copy and never saved.",
        )
        hint(
            self.destination_host_hint,
            vm.field_hint(failure, "destination", "address"),
            "Just the name. The app adds the port itself.",
        )
        bad = vm.field_hint(failure, "destination", "token" if token else "password") or (
            vm.field_hint(failure, "destination", "ops_address")
        )
        switch = ""
        if token:
            switch = '<a href="password">Sign in with a user and password instead</a>'
        elif failure and failure.get("maybe_token") and not failure.get("edited"):
            switch = (
                'If the destination is version 9.1, <a href="token">sign in with an access '
                "token instead</a>."
            )
        self.destination_password_hint.setText(" ".join(t for t in (bad, switch) if t))
        set_status(self.destination_password_hint, "bad" if bad else "")
        self.destination_password_hint.setVisible(bool(bad or switch))
        needed = state.server_needed and not token
        self.server_user_caption.setText(
            "Server user (needed to continue the copy)" if needed else "Server user (recommended)"
        )
        self.destination_ssh_user.setAccessibleName(self.server_user_caption.text())
        self.destination_ssh_user.setPlaceholderText("root" if needed else "root, or leave empty")
        if needed and not busy:
            if not self.destination_ssh_user.text().strip():
                set_status(self.destination_ssh_user, "bad")
            if not self.destination_ssh_password.text():
                set_status(self.destination_ssh_password, "bad")
        server_bad = vm.field_hint(failure, "destination", "server_password")
        if token:
            hint(
                self.destination_server_hint,
                None,
                "A version 9.1 destination has no server sign-in for this app, so free space "
                "is not checked and a copy that stops part-way cannot be continued from this "
                "app.",
            )
        else:
            hint(
                self.destination_server_hint,
                server_bad
                or (
                    "Add the destination servers’ root user and password here, then test again."
                    if needed
                    else None
                ),
                WITHOUT_SERVER,
            )

    def refresh(self) -> None:
        state = self.app.state
        busy = self.app.busy("connect")
        discovered = state.discovered
        show_form = busy or discovered is None or self.editing
        zone = state.settings.timezone
        if show_form:
            self._update_form(busy)
            top: list = []
            lead = None
            if self.editing and state.refused:
                view = vm.verdict(discovered["version_check"])
                top.append(banner("bad", view["title"], view["text"]))
            resume = self._resume_text(False)
            if busy:
                lead = None
            elif state.server_needed and not state.settings.destination_token:
                lead = (
                    "warn",
                    "One thing to add: the destination’s server sign-in.",
                    "The stopped copy cannot be continued without it. Fill in the two boxes "
                    "marked in red on the right, then test again. Everything else is still "
                    "filled in."
                    if state.resume and state.resume["kind"] == "back"
                    else "With it the app can check free space before a copy, and continue a "
                    "copy that stops part-way. Fill in the two boxes marked in red on the "
                    "right, then test again. Everything else is still filled in.",
                )
            elif state.failure:
                lead = vm.failure_banner(state.failure)
            elif state.token_notice:
                lead = (
                    "info",
                    "This destination is version 9.1, which signs in a different way.",
                    "Version 9.1 does not take a Logs password. It takes an access token: a "
                    "sign-in key created in VCF Operations, the product that manages this "
                    "destination. Whoever administers VCF Operations can create one for you. "
                    "Enter its address and the token below, then test again.",
                )
            elif resume:
                lead = resume
            if lead:
                top.append(banner(*lead))
            if not top and not busy:
                ready, layout = card("Have these ready")
                layout.addWidget(
                    columns(
                        *[
                            rich(
                                f"<b>{escape(title)}</b><br>"
                                f'<span style="font-size:12.5px">{escape(text)}</span>',
                                "soft",
                            )
                            for title, text in READY
                        ]
                    )
                )
                top.append(ready)
            self.top.set(*top)
            self.set_body(
                self.top if top else None,
                self.form,
                self.activity if busy else None,
            )
            need = self.need()
            again = bool(state.failure or state.token_notice or self.editing or state.server_needed)
            if busy:
                self.set_footer("Testing only reads. Nothing is changed on either system.")
            else:
                self.set_footer(
                    f"Still needed before testing: <b>{escape(', '.join(need))}</b>."
                    if need
                    else "Ready. Testing signs in to both systems and reads their versions. It "
                    "changes nothing.",
                    [
                        (
                            "Test again" if again else "Test connections",
                            self.test,
                            "primary",
                            not need,
                        )
                    ],
                )
            return
        view = vm.verdict(discovered["version_check"])
        tested = vm.format_clock(discovered["tested_at"], zone)
        tested = tested[0].upper() + tested[1:]
        if state.connected:
            widgets = [
                banner(
                    "ok", view["title"], f"{view['text']} Tested {tested[0].lower() + tested[1:]}."
                )
            ]
            if view["caution"]:
                widgets.append(banner("warn", *view["caution"]))
            widgets.append(self._result_cards(view))
            found = discovered["servers_found"]
            widgets.append(
                rich(
                    (
                        f"The app found the {vm.plural(found, 'server')} behind these two "
                        "addresses and recorded each one’s identity code. "
                        if found
                        else "The app recorded the identity of both addresses. "
                    )
                    + '<a href="codes">Show the recorded identity codes</a> · One thing cannot '
                    "be tested from here: whether the destination’s servers can reach the "
                    "source’s servers. That is checked when a copy starts.",
                    "subtle",
                    self._link,
                )
            )
            resume = self._resume_text(True)
            if resume:
                widgets.append(banner(*resume))
                kind = state.resume["kind"]
                text = {
                    "back": "Back to the stopped copy →",
                    "missing": "Copy what is missing →",
                }.get(kind, "Go to the unfinished copy →")
                copy = state.copy(state.resume["copy"])
                if kind == "unfinished" and copy and copy["state"] in {"running", "unsure"}:
                    text = "Go to the copy →"
                primary = (text, self.app.resume_go, "primary")
                actions = [("Sign out of both", self.sign_out)]
                if kind == "back" and not state.server_sign_in:
                    actions.append(("Change sign-in details", self.app.add_sign_in))
                actions.append(primary)
            else:
                nxt, layout = card("What happens next")
                layout.addWidget(
                    rich(
                        "Next the app compares the rules on both systems (how new logs are "
                        "forwarded, stored, hidden or dropped) and offers to copy any that are "
                        "missing. Then you choose how far back to copy.",
                        "text",
                    )
                )
                widgets.append(nxt)
                actions = [
                    ("Sign out of both", self.sign_out),
                    ("Continue →", lambda: self.app.go("rules"), "primary"),
                ]
            self.set_body(*widgets)
            self.set_footer("Connections are ready.", actions)
        else:
            self.set_body(banner("bad", view["title"], view["text"]), self._result_cards(view))
            self.set_footer(
                "Both systems signed in and nothing on either was changed. Tested "
                f"{tested[0].lower() + tested[1:]}. Fix the version problem, then test again.",
                [
                    ("Change sign-in details", self.edit),
                    ("Test again", self.retest, "primary"),
                ],
            )

    def retest(self) -> None:
        if not self.need():
            self.test()
        else:
            self.edit()
