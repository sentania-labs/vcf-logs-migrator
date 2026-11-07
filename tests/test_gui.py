"""The real window against the simulator, offscreen. Skipped when PySide6 is not installed."""

import pytest

pytest.importorskip("PySide6")

from gui_driver import FIXTURES, Driver  # noqa: E402


@pytest.fixture
def driver():
    instance = Driver(FIXTURES / "sealed-populated.fixture", running_polls=2)
    yield instance
    instance.close()


def test_window_walks_the_workflow_off_the_ui_thread(driver):
    state = driver.state
    assert not state.connected
    driver.connect()
    assert state.connected and state.connect_error is None
    assert state.discovered["destination"]["version"].startswith("9.0.")
    assert driver.pages["connections"].source_badge.text() == "8.18.7 · simulator"
    driver.plan()
    assert state.plan_run is not None and state.run_id == state.plan_run["id"]
    plan_page = driver.pages["plan"]
    assert not plan_page.start_button.isEnabled()
    driver.acknowledge()
    assert plan_page.start_button.isEnabled()
    driver.start()
    assert state.status["status"] == "running"
    assert driver.window.current_page() == "migration"
    assert driver.pages["migration"].cancel_button.isVisible()
    driver.poll_until_finished()
    assert state.status["status"] == "completed"
    driver.verify()
    assert state.verification["outcome"] == "verified_success"
    assert driver.pages["verification"].daily.rowCount() >= 1
    driver.export()
    assert len(state.exported) == 2 and all(
        path.startswith(str(driver.exports)) for path in state.exported
    )
    driver.plan()
    assert state.plan_error in {"overlapping_transfer", "bucket_identity_conflict"}
    assert driver.pages["plan"].stack.currentIndex() == 1
    driver.theme("dark")
    assert state.settings.theme == "dark"


def _live_simulator(monkeypatch, driver, *, refuse=False):
    """Make Session open the simulator as if it were real appliances: the first attempt
    asks to trust two identities, the second succeeds. Counts carry no attribution."""
    import json

    from logs_migrate.adapters.simulated import SimulatedEnvironment
    from logs_migrate.gui import session
    from logs_migrate.trust import Identity, TrustRequired

    attempts = []

    def open_live(self):
        attempts.append(1)
        if len(attempts) == 1:
            identities = [
                Identity("tls", "aria-logs.lab.example:9543", "a" * 64),
                Identity("ssh", "aria-logs.lab.example:22", "b" * 64, "ssh-ed25519"),
            ]
            self.pending_trust = {i.name: (i, None) for i in identities}
            raise TrustRequired(identities)
        env = SimulatedEnvironment(
            json.loads(driver.fixture.read_text()),
            driver.journal.with_suffix(".simulated-state"),
        )
        env.mode = "live"
        env.attribution = False
        return env

    class Store:
        def __init__(self):
            self.enrolled = []

        def trust(self, identity, line):
            self.enrolled.append(identity.name)

    store = Store()
    monkeypatch.setattr(session.Session, "_open_live", open_live)
    monkeypatch.setattr(session, "TrustStore", lambda: store)
    asked = []

    def ask(identities):
        asked.append([i["display"] for i in identities])
        return not refuse

    driver.window.ask_trust = ask
    driver.state.settings.fixture_path = ""
    # Live counts are spaced by the real clock; the test does not wait 30 s between them.
    driver.state.settings.count_poll_seconds = 0
    return attempts, asked, store


def test_live_connect_trusts_identities_then_runs_the_workflow(driver, monkeypatch):
    attempts, asked, store = _live_simulator(monkeypatch, driver)
    state = driver.state
    driver.connect()
    assert len(asked) == 1 and all("SHA256" in text for text in asked[0])
    assert store.enrolled == ["aria-logs.lab.example:9543", "aria-logs.lab.example:22"]
    assert len(attempts) == 2 and state.connected and state.live
    page = driver.pages["connections"]
    driver.show("connections")
    assert page.source_badge.text() == "8.18.7 · live"
    rows = dict(page.checks.rows())
    assert rows["Source Logs API"].startswith("Passed") and "Destination repository budget" in rows
    assert page.selftest_button.isVisible()
    from logs_migrate.gui import session

    monkeypatch.setattr(
        session.Session,
        "selftest",
        lambda self: {
            "steps": [{"name": "Source API version", "status": "pass", "detail": "8.18.7"}],
            "summary": {"pass": 1, "fail": 0, "skip": 0},
            "passed": True,
            "notes": {},
        },
    )
    driver.window.run_selftest()
    driver.idle()
    assert dict(page.checks.rows())["Self-test"].startswith("1 passed")
    driver.plan()
    plan_page = driver.pages["plan"]
    checks = dict(plan_page.checks.rows())
    assert checks["Source ingest"].endswith("source quiet")
    driver.acknowledge()
    driver.start()
    driver.poll_until_finished()
    driver.verify()
    assert state.verification["outcome"] == "verified_success"
    assert state.verification["partial_days"]
    verification = driver.pages["verification"]
    assert "partial day" in verification.daily.rows()[0][3]
    assert "fully covered day" in verification.banner.text()


def test_live_connect_refused_trust_sends_nothing(driver, monkeypatch):
    attempts, asked, store = _live_simulator(monkeypatch, driver, refuse=True)
    driver.connect()
    assert len(attempts) == 1 and store.enrolled == [] and not driver.state.connected
    assert "not trusted" in driver.pages["connections"].error.text()


def _stub_config(monkeypatch, applied):
    from logs_migrate.gui import session

    comparison = {
        "rules": [
            {
                "selector": "filters:dropper",
                "type": "filters",
                "name": "dropper",
                "status": "differs",
                "changed_fields": ["enabled"],
                "warnings": [],
            },
            {
                "selector": "forwarding:fw",
                "type": "forwarding",
                "name": "fw",
                "status": "missing",
                "changed_fields": [],
                "warnings": ["acceptCert"],
            },
        ],
        "destination_only": [],
    }

    def apply(self, *, selection):
        applied.append(list(selection))
        return {
            "rules": [
                {**rule, "status": "failed", "reason": "invalid_input", "restart_required": False}
                for rule in comparison["rules"]
                if rule["selector"] in selection
            ],
            "complete": False,
            "restart_required": False,
            "maintenance": None,
        }

    monkeypatch.setattr(session.Session, "config_compare", lambda self: comparison)
    monkeypatch.setattr(session.Session, "config_apply", apply)


def test_unticked_rules_stay_unticked_and_retry_applies(driver, monkeypatch):
    applied = []
    _stub_config(monkeypatch, applied)
    driver.connect()
    driver.compare()
    page = driver.pages["configuration"]
    page.boxes[0].setChecked(False)
    assert page.selection() == ["forwarding:fw"]
    driver.window.poll_status()
    driver.idle()
    assert page.selection() == ["forwarding:fw"], "a status poll must not re-tick rules"
    driver.apply()
    assert applied == [["forwarding:fw"]]
    assert page.stack.currentIndex() == 1
    page.secondary.click()  # Compare again
    driver.idle()
    assert page.stack.currentIndex() == 0 and page.primary.text().startswith("Apply")
    page.boxes[0].setChecked(False)
    page.primary.click()
    driver.until(lambda: len(applied) == 2)
    driver.idle()
    assert applied[-1] == ["forwarding:fw"] and driver.window.current_page() == "configuration"


def test_selected_run_report_and_plan_follow_the_selection(driver):
    driver.connect()
    driver.plan()
    run_a = driver.state.run_id
    driver.plan("90 days")
    run_b = driver.state.run_id
    assert run_a != run_b
    driver.window.select_run(run_b)
    driver.window.select_run(run_a)
    driver.idle()
    assert driver.state.run_id == run_a
    assert driver.state.report["run"]["id"] == run_a
    assert driver.state.plan_run["id"] == run_a


def test_start_failure_after_post_never_claims_nothing_was_sent(driver):
    driver.connect()
    driver.plan()
    driver.acknowledge()
    env = driver.window.worker.session.env
    env.data["faults"] = {"start.after": "connection"}
    env._persist()
    driver.start()
    assert driver.state.start_error == "remote_outcome_uncertain"
    page = driver.pages["plan"]
    assert "may have reached the API" in page.note.text()
    assert not page.start_button.isEnabled()
    assert page.start_button.text() == "Start migration · submitted"
    driver.poll_until_finished()
    assert driver.state.status["id"] == "sim-1"


def test_results_from_a_previous_connection_are_dropped(driver):
    driver.connect()
    driver.window.poll_status()
    driver.disconnect = driver.window.disconnect_environments
    driver.disconnect()
    driver.idle()
    assert driver.state.status is None and not driver.state.connected
    assert not driver.window.worker.pending


SKIPPED = "src-2/00000000-0000-0000-0000-000000000002"


def test_cancel_button_confirms_then_cancels_the_task():
    driver = Driver(FIXTURES / "sealed-populated.fixture", running_polls=20)
    try:
        driver.connect()
        driver.plan()
        driver.acknowledge()
        driver.start()
        assert driver.state.status["status"] == "running"
        assert driver.cancel(), "the real Cancel button must send the stop request"
        assert driver.state.status["status"] == "cancelled"
        assert "CANCELLED" in driver.pages["migration"].banner.text()
    finally:
        driver.close()


def test_skipped_selected_group_is_never_reported_as_success():
    driver = Driver(
        FIXTURES / "sealed-populated.fixture", running_polls=1, knobs={"skip_buckets": [SKIPPED]}
    )
    try:
        driver.connect()
        driver.plan()
        driver.acknowledge()
        driver.start()
        driver.poll_until_finished()
        assert SKIPPED in driver.state.status["skipped"]
        driver.verify()
        page = driver.pages["verification"]
        assert driver.state.verification["buckets"]["not_received"] == [SKIPPED]
        assert "not received" in page.banner.text()
        assert page.banner.property("status") == "bad"
        assert "group not received" in page.title.text()
        assert "not received" in page.subtitle.text()
        rows = [page.daily.item(r, 3).text() for r in range(page.daily.rowCount())]
        assert "Not received" in rows
        assert "Mar 2" in [page.daily.item(r, 0).text() for r in range(page.daily.rowCount())]
        combo_text = driver.pages["reports"].runs.combo.currentText()
        assert "not received" in combo_text and "success" not in combo_text
        driver.show("reports")
        assert driver.pages["reports"].banner.property("status") == "bad"
        driver.export()
        exported = next(path for path in driver.state.exported if path.endswith(".md"))
        text = open(exported, encoding="utf-8").read()
        assert "verified_success" not in text and "success" not in text.lower()
        assert "not received" in text and "Mar 2" in text and SKIPPED not in text
        assert "Verification: Incomplete" in text
    finally:
        driver.close()


def test_verification_results_bind_to_the_run_that_requested_them(driver):
    driver.connect()
    driver.plan()
    run_a = driver.state.run_id
    driver.plan()
    run_b = driver.state.run_id
    assert run_a != run_b and driver.state.run_id == run_b
    stray = {"outcome": "verified_success", "complete": True, "daily": [], "buckets": {}}
    driver.window.worker.pending.add("verify:" + run_a)
    driver.window.job_generation["verify:" + run_a] = driver.window.generation
    driver.window._done("verify:" + run_a, stray)
    assert driver.state.verification is None, "run A's result must not land on run B"
    driver.window.worker.pending.add("verify:" + run_b)
    driver.window.job_generation["verify:" + run_b] = driver.window.generation
    driver.window._done("verify:" + run_b, stray)
    assert driver.state.verification is stray


def test_editing_a_date_retires_the_preview_and_acknowledgment(driver):
    from PySide6.QtCore import QDate

    driver.connect()
    driver.plan()
    driver.acknowledge()
    page = driver.pages["plan"]
    assert page.start_button.isEnabled()
    page.end_date.setDate(QDate(2026, 3, 2))
    driver.wait(30)
    assert not page.start_button.isEnabled()
    assert not driver.state.acknowledged and not page.ack.isVisible()
    assert "changed since the preview" in page.note.text()
    page.start()  # Must be a no-op while the preview is stale.
    driver.idle()
    assert driver.state.started_run_id is None
    assert driver.state.status is None or driver.state.status["status"] == "idle"
    page.preview()
    driver.until(lambda: "plan" not in driver.window.worker.pending)
    driver.idle()
    assert page.ack.isVisible() and not page.start_button.isEnabled()
    driver.acknowledge()
    assert page.start_button.isEnabled()


def test_blocked_preview_keeps_the_date_editors_reachable(driver):
    driver.connect()
    driver.plan()
    driver.acknowledge()
    driver.start()
    driver.poll_until_finished()
    driver.plan()
    page = driver.pages["plan"]
    assert page.stack.currentIndex() == 1 and page.change_button.isVisible()
    page.change_dates()
    driver.wait(30)
    assert page.stack.currentIndex() == 0
    assert page.start_date.isEnabled() and page.refresh_button.isEnabled()
    assert "blocked" in page.error.text().lower() and page.error.isVisible()


def test_validation_names_what_is_wrong(driver):
    page = driver.pages["connections"]
    page.destination_host.clear()
    page.connect()
    assert "Logs host and port" in page.error.text()
    assert driver.state.connect_error is None and not driver.window.worker.pending
    page.destination_host.setText("vcf-logs.lab.example:9543")
    driver.state.settings.fixture_path = "/nowhere/missing.fixture"
    driver.connect()
    assert driver.state.connect_error.startswith("gui:")
    assert "not found" in page.error.text() and "/nowhere/missing.fixture" in page.error.text()
    driver.state.settings.fixture_path = str(driver.fixture)
    driver.connect()
    driver.show("plan")
    plan = driver.pages["plan"]
    from PySide6.QtCore import QDate

    plan.start_date.setDate(QDate(2025, 1, 1))
    plan.end_date.setDate(QDate(2025, 1, 31))
    plan.preview()
    driver.until(lambda: driver.state.plan_error is not None)
    driver.idle()
    assert "No sealed history in these dates" in plan.error.text()
    plan.end_date.setDate(QDate(2024, 12, 1))
    driver.wait(20)
    assert plan.date_hint.isVisible() and "on or after" in plan.date_hint.text()
    plan.preview()
    assert "plan" not in driver.window.worker.pending


def test_keyboard_reaches_the_sidebar_and_every_input_is_named(driver):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QComboBox, QDateEdit, QLineEdit, QSpinBox

    assert driver.window.nav.focusPolicy() == Qt.FocusPolicy.StrongFocus
    unnamed = [
        type(widget).__name__
        for cls in (QLineEdit, QComboBox, QSpinBox, QDateEdit)
        for widget in driver.window.findChildren(cls)
        if not widget.accessibleName()
        and widget.parent() is not None
        and not isinstance(widget.parent(), (QComboBox, QSpinBox, QDateEdit))
    ]
    assert unnamed == []


def test_settings_validate_inline_and_return_to_the_previous_page(driver):
    driver.show("migration")
    driver.window.show_page("settings")
    page = driver.pages["settings"]
    page.fixture.setText("/nowhere/missing.fixture")
    page.timezone.setCurrentText("Mars/Olympus")
    assert page.save() is False
    assert page.hints["fixture"].isVisible() and "not found" in page.hints["fixture"].text()
    assert page.hints["timezone"].isVisible()
    assert driver.state.settings.timezone != "Mars/Olympus"
    page.fixture.setText(str(driver.fixture))
    page.timezone.setCurrentText("UTC")
    assert page.save() is True
    assert driver.window.current_page() == "migration"


def test_banners_and_values_render_remote_text_literally(driver):
    from logs_migrate.gui.widgets import KeyValues, Table, badge, banner, label

    hostile = "x</b><br><b>Selected rules applied"
    shown = banner("Applied", hostile, "ok")
    assert hostile not in shown.text() and "&lt;/b&gt;" in shown.text()
    values = KeyValues()
    values.set_rows([("Task", hostile)])
    value = values.grid.itemAtPosition(0, 1).widget()
    assert value.textFormat().name == "PlainText" and value.text() == hostile
    assert label(hostile).textFormat().name == "PlainText"
    assert badge(hostile).textFormat().name == "PlainText"
    table = Table(["Rule"])
    table.set_rows([(hostile,)])
    tip = table.item(0, 0).toolTip()
    assert hostile not in tip and "&lt;/b&gt;" in tip


def test_export_to_an_unwritable_folder_names_the_folder_not_the_api(driver, tmp_path):
    driver.connect()
    driver.plan()
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    driver.state.settings.export_folder = str(blocker / "reports")
    driver.export()
    assert driver.state.exported == []
    message = driver.pages["reports"].exported.text()
    assert "Could not write to" in message and str(blocker) in message
    assert "Query the API" not in message


def test_reconnect_keeps_the_operators_dates(driver):
    driver.connect()
    driver.plan()
    plan_page = driver.pages["plan"]
    start = plan_page.current_dates()[0]
    plan_page.start_date.setDate(plan_page.start_date.date().addDays(1))
    chosen = plan_page.current_dates()
    assert chosen[0] != start
    driver.window.reconnect_environments()
    driver.idle()
    assert driver.state.connected
    assert plan_page.current_dates() == chosen


def test_reconnect_before_a_preview_keeps_the_operators_dates(driver):
    driver.connect()
    plan_page = driver.pages["plan"]
    plan_page.start_date.setDate(plan_page.start_date.date().addDays(1))
    chosen = plan_page.current_dates()
    assert driver.state.plan_run is None
    driver.window.reconnect_environments()
    driver.idle()
    assert driver.state.connected
    assert plan_page.current_dates() == chosen


def test_reconnect_after_a_failed_reconnect_keeps_the_operators_dates(driver):
    driver.connect()
    plan_page = driver.pages["plan"]
    plan_page.start_date.setDate(plan_page.start_date.date().addDays(1))
    chosen = plan_page.current_dates()
    settings = driver.state.settings
    fixture = settings.fixture_path
    settings.fixture_path = fixture + ".missing"
    driver.window.reconnect_environments()
    driver.until(lambda: driver.state.connect_error)
    driver.idle()
    assert not driver.state.connected
    settings.fixture_path = fixture
    driver.window.reconnect_environments()
    driver.until(lambda: driver.state.connected)
    driver.idle()
    assert plan_page.current_dates() == chosen
