"""The real window against the simulator, offscreen. Skipped when PySide6 is not installed."""

import pytest

pytest.importorskip("PySide6")

from gui_driver import FIXTURES, Driver  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from logs_migrate.gui import viewmodel as vm  # noqa: E402


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
    assert driver.pages["verification"].tiles[0][0].text() == "Expected from selected groups"
    assert "Arrived at destination" in driver.pages["verification"].tiles[1][0].text()
    driver.export()
    assert len(state.exported) == 2 and all(
        path.startswith(str(driver.exports)) for path in state.exported
    )
    driver.plan()
    assert state.plan_error is None
    assert not state.plan_run["plan"]["selected"]
    assert state.plan_run["plan"]["already_received"]
    assert not plan_page.start_button.isEnabled()


def test_resume_preview_and_finish_in_the_window():
    import json

    driver = Driver(FIXTURES / "sealed-populated.fixture", knobs={"cancel_after": 1})
    try:
        driver.connect()
        driver.plan()
        driver.acknowledge()
        driver.start()
        driver.poll_until_finished()
        assert driver.state.status["status"] == "cancelled"
        page = driver.pages["migration"]
        assert page.remainder_card.isVisible()
        assert "Already received: 1 bucket" in page.remainder.text()
        assert "Still to transfer: 2 buckets" in page.remainder.text()
        assert "Estimated time:" in page.remainder.text()
        old_run = driver.state.run_id
        driver.verify()
        assert driver.state.verification is not None
        assert driver.state.verification["outcome"] != "verified_success"
        driver.export()
        assert driver.state.report["run"]["id"] == old_run and driver.state.exported
        driver.show("migration")
        state_path = driver.journal.with_suffix(".simulated-state")
        data = json.loads(state_path.read_text())
        del data["cancel_after"]
        state_path.write_text(json.dumps(data))
        assert page.resume_button.isVisible()
        page.resume_button.click()
        driver.until(lambda: "resume" not in driver.window.worker.pending)
        driver.idle()
        assert page.remainder_card.isVisible()
        assert "Already received: 1 bucket" in page.remainder.text()
        assert "Estimated space fits on each destination." in page.remainder.text()
        driver.state.plan_run["plan"]["node_count_unchecked"] = True
        page.refresh()
        assert "assuming the node count is unchanged" in page.remainder.text()
        driver.state.plan_run["plan"].pop("node_count_unchecked")
        page.refresh()
        assert driver.state.run_id != old_run
        assert driver.state.verification is None and driver.state.verify_error is None
        assert not driver.state.exported and driver.state.export_error is None
        assert (
            driver.state.report is None or driver.state.report["run"]["id"] == driver.state.run_id
        )
        driver.show("verification")
        assert driver.state.verification is None
        driver.show("reports")
        assert (
            driver.state.report is None or driver.state.report["run"]["id"] == driver.state.run_id
        )
        driver.show("migration")
        assert page.start_remainder_button.isEnabled()
        page.start_remainder_button.click()
        driver.until(lambda: "start" not in driver.window.worker.pending)
        driver.idle()
        driver.poll_until_finished()
        driver.verify()
        assert driver.state.verification["outcome"] == "verified_success"
    finally:
        driver.close()


def test_go_back_from_resume_preview_returns_to_the_stopped_run():
    import json

    driver = Driver(FIXTURES / "sealed-populated.fixture", knobs={"cancel_after": 1})
    try:
        driver.connect()
        driver.plan()
        driver.acknowledge()
        driver.start()
        driver.poll_until_finished()
        old_run = driver.state.run_id
        driver.show("migration")
        state_path = driver.journal.with_suffix(".simulated-state")
        data = json.loads(state_path.read_text())
        del data["cancel_after"]
        state_path.write_text(json.dumps(data))
        page = driver.pages["migration"]
        page.resume_button.click()
        driver.until(lambda: "resume" not in driver.window.worker.pending)
        driver.idle()
        assert driver.state.run_id != old_run
        assert page.start_remainder_button.isVisible()
        page.back_button.click()
        driver.idle()
        assert driver.state.run_id == old_run
        assert not page.start_remainder_button.isVisible()
        assert not page.back_button.isVisible()
    finally:
        driver.close()


def test_refused_resume_explains_which_action_failed():
    import json

    driver = Driver(FIXTURES / "sealed-populated.fixture", knobs={"cancel_after": 1})
    try:
        driver.connect()
        driver.plan()
        driver.acknowledge()
        driver.start()
        driver.poll_until_finished()
        state_path = driver.journal.with_suffix(".simulated-state")
        data = json.loads(state_path.read_text())
        before = len([item for item in data["trace"] if item["operation"] == "start"])
        data["destination"]["nodes"].append(dict(data["destination"]["nodes"][0], id="dst-extra"))
        state_path.write_text(json.dumps(data))
        page = driver.pages["migration"]
        page.resume_button.click()
        driver.until(lambda: "resume" not in driver.window.worker.pending)
        driver.idle()
        assert "Could not resume" in page.banner.text()
        assert "node count changed" in page.banner.text()
        after = json.loads(state_path.read_text())
        assert len([item for item in after["trace"] if item["operation"] == "start"]) == before
    finally:
        driver.close()


def _live_simulator(monkeypatch, driver, *, refuse=False):
    """Make Session open the simulator as if it were real appliances: the first attempt
    asks to trust two identities, the second succeeds. Counts carry no attribution."""
    import json

    from logs_migrate.adapters.simulated import SimulatedEnvironment
    from logs_migrate.gui import session
    from logs_migrate.trust import Identity, TrustRequired

    attempts = []

    def open_live(self, *, probe_only=False):
        attempts.append(1)
        if len(attempts) == 1:
            identities = [
                Identity("tls", "aria-logs.lab.example:9543", "a" * 64),
                Identity("ssh", "aria-logs.lab.example:22", "b" * 64, "ssh-ed25519"),
            ]
            self.pending_trust = {i.name: (i, None) for i in identities}
            raise TrustRequired(identities)
        data = json.loads(driver.fixture.read_text())
        data["ingest_during_run"] = []  # Live counts cannot attribute ingest; keep it out.
        env = SimulatedEnvironment(data, driver.journal.with_suffix(".simulated-state"))
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
    assert len(attempts) == 3 and state.connected and state.live
    assert driver.window.pending_credentials == {}
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
    assert checks["Source ingest"] == "No open group growth in 5 s"
    driver.acknowledge()
    driver.start()
    driver.poll_until_finished()
    driver.verify()
    assert state.verification["outcome"] == "verified_success"
    assert state.verification["partial_days"] == ["2026-03-03"]
    verification = driver.pages["verification"]
    assert "fully covered day" in verification.banner.text()
    assert "1 partial day" in verification.banner.text()
    assert "matches too" in verification.banner.text()


def test_live_connect_refused_trust_sends_nothing(driver, monkeypatch):
    attempts, asked, store = _live_simulator(monkeypatch, driver, refuse=True)
    driver.connect()
    assert len(attempts) == 1 and store.enrolled == [] and not driver.state.connected
    assert "not trusted" in driver.pages["connections"].error.text()
    assert driver.window.pending_credentials == {}


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
    assert "start_outcome" not in driver.state.status


@pytest.mark.parametrize("stage", ["host", "auth"])
def test_ssh_failure_at_start_stays_a_pre_post_refusal(driver, monkeypatch, stage):
    from logs_migrate.adapters.ssh_errors import SSHFailure
    from logs_migrate.gui.session import Session

    error = SSHFailure(stage)

    def fail(self, **kwargs):
        raise error

    driver.connect()
    driver.plan()
    driver.acknowledge()
    monkeypatch.setattr(Session, "start", fail)
    driver.start()
    assert driver.state.started_run_id is None
    page = driver.pages["plan"]
    assert error.message in page.note.text()
    assert "No API request was submitted." in page.note.text()
    assert "may have reached the API" not in page.note.text()
    assert page.start_button.isEnabled()


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
        assert "Transfer cancelled" in driver.pages["migration"].banner.text()
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
        migration = driver.pages["migration"]
        assert "Some history is missing" in migration.banner.text()
        assert migration.banner.property("status") == "warn"
        assert "Still to transfer: 1 bucket ·" in migration.remainder.text()
        assert "1 buckets" not in migration.remainder.text()
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
    driver.window.worker.pending.add("export:" + run_a)
    driver.window.job_generation["export:" + run_a] = driver.window.generation
    driver.window._done("export:" + run_a, ["old-report.md"])
    assert not driver.state.exported, "run A's export must not appear on run B"
    driver.window.worker.pending.add("verify:" + run_b)
    driver.window.job_generation["verify:" + run_b] = driver.window.generation
    driver.window._done("verify:" + run_b, stray)
    assert driver.state.verification is stray


@pytest.mark.parametrize("outcome", ["result", "error"])
def test_leaving_a_run_does_not_leave_verify_stuck(driver, outcome):
    driver.connect()
    driver.plan()
    run_a = driver.state.run_id
    driver.plan()
    run_b = driver.state.run_id
    assert run_a != run_b
    job = "verify:" + run_a
    driver.state.verify_run_id = run_a
    driver.window.worker.pending.add(job)
    driver.window.job_generation[job] = driver.window.generation
    if outcome == "result":
        driver.window._done(job, {"outcome": "verified_success", "complete": True, "daily": []})
    else:
        driver.window._failed(job, "connection_failed")
    assert driver.state.verify_run_id is None
    assert driver.state.verification is None and driver.state.verify_error is None
    driver.window.select_run(run_a)
    assert driver.pages["verification"].verify_button.isEnabled()


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


def test_blocked_preview_offers_refresh_without_asking_for_dates(driver):
    driver.connect()
    driver.plan()
    driver.acknowledge()
    driver.start()
    driver.poll_until_finished()
    driver.show("plan")
    driver.state.plan_error = "overlapping_transfer: An in-flight transfer holds these groups."
    page = driver.pages["plan"]
    page.refresh()
    assert page.stack.currentIndex() == 1 and page.change_button.isVisible()
    assert page.refresh_button.isEnabled() and not page.start_button.isEnabled()
    assert "refresh" in page.blocked_next.text().lower()
    page.change_dates()
    driver.wait(30)
    assert page.stack.currentIndex() == 0
    assert page.start_date.isEnabled() and page.refresh_button.isEnabled()
    assert "needs attention" in page.error.text().lower() and page.error.isVisible()


def test_blocked_page_shows_the_refusal_it_received(driver):
    driver.connect()
    driver.plan()
    driver.acknowledge()
    driver.start()
    driver.poll_until_finished()
    driver.show("plan")
    driver.state.plan_error = "overlapping_transfer: An in-flight transfer holds these groups."
    page = driver.pages["plan"]
    page.refresh()
    assert page.stack.currentIndex() == 1
    refused = dict(page.overlap_rows.rows())["Refused"]
    assert refused == vm.plan_error_text(
        driver.state.plan_error,
        driver.state.discovered,
        vm.window_from_days(*page.current_dates()),
    )
    assert "in-flight transfer" in refused


def test_destination_conflict_shows_and_copies_the_affected_bucket(driver):
    driver.connect()
    driver.show("plan")
    bucket_id = "00000000-0000-0000-0000-000000000001"
    driver.state.plan_error = (
        "bucket_identity_conflict: 1 destination bucket needs attention: "
        f"{bucket_id} (2026-03-01 to 2026-03-01 UTC; destination: dst-1; "
        "source size 1000 bytes, destination size 1 bytes). "
        "The named copies on the destination must be dealt with there."
    )
    driver.state.plan_affected = [bucket_id]
    page = driver.pages["plan"]
    page.refresh()
    assert page.stack.currentIndex() == 1
    assert "source size" in dict(page.overlap_rows.rows())["Refused"]
    assert "transfer" not in page.blocked_banner.text().lower()
    page.group_ids_toggle.click()
    assert page.group_ids.toPlainText() == bucket_id
    page.group_ids_copy.click()
    assert QApplication.clipboard().text() == bucket_id


def test_blocked_page_is_chosen_by_error_code_not_by_message_wording(driver):
    driver.connect()
    driver.show("plan")
    page = driver.pages["plan"]
    driver.state.plan_error = "topology_changed: Wording nobody matches on."
    page.refresh()
    assert page.stack.currentIndex() == 1
    driver.state.plan_error = "incomplete_evidence: A destination bucket needs attention."
    page.refresh()
    assert page.stack.currentIndex() == 0
    driver.state.plan_affected = ["b1"]
    page.refresh()
    assert page.stack.currentIndex() == 1


def test_duplicate_source_identities_are_not_shown_as_prior_import_overlap(driver):
    driver.connect()
    driver.plan()
    first = driver.state.discovered["source"]["buckets"][0]
    driver.state.plan_error = (
        "bucket_identity_conflict: Source groups have duplicate identities: "
        f"{first['node']}/{first['id']}. Inspect the source inventory before retrying."
    )
    page = driver.pages["plan"]
    page.refresh()
    assert page.stack.currentIndex() == 0 and not page.change_button.isVisible()
    text = page.error.text()
    assert "duplicate identities" in text and first["node"] in text
    assert first["id"] not in text
    assert page.error_ids_toggle.isVisible()
    page.error_ids_toggle.click()
    assert first["id"] in page.error_ids.toPlainText()
    assert "Inspect the source inventory" in text
    assert "overlaps a previous import" not in page.title.text()


def test_long_check_rows_wrap_and_take_keyboard_focus(driver):
    from PySide6.QtCore import Qt

    from logs_migrate.gui.widgets import KeyValues

    reason = "8 sealed buckets have no start/end (" + ", ".join(["a" * 36] * 8) + ")"
    values = KeyValues()
    values.set_rows([("Node src-1", "Failed · " + reason)])
    value = values.grid.itemAtPosition(0, 1).widget()
    assert value.wordWrap() and value.text() == "Failed · " + reason
    assert value.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert value.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByKeyboard


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


def test_ssh_failure_survives_worker_and_renders_in_connections(driver, monkeypatch):
    from logs_migrate.adapters.ssh_errors import SSHFailure
    from logs_migrate.gui.session import Session

    error = SSHFailure("auth")

    def fail(self, **kwargs):
        raise error

    monkeypatch.setattr(Session, "connect", fail)
    driver.show("connections")
    driver.connect()
    banner = driver.pages["connections"].error
    assert banner.isVisible()
    shown = banner.text()
    assert error.message in shown
    assert "No migration request was submitted." in shown
    assert "API did not respond" not in shown


def test_window_shows_each_node_inventory_progress(driver):
    driver.show("connections")
    driver.window.worker.progress.emit("node 1/2 (source-1): sizing 2,100 buckets")
    driver.window.worker.progress.emit("node 2/2 (source-2): sizing 2,088 buckets")
    driver.wait(50)
    text = driver.window.progress_label.text()
    assert "source-1" in text and "source-2" in text
    assert len(text.splitlines()) == 2


@pytest.mark.parametrize("job", ["verify:run-1", "discover", "plan"])
def test_progress_lines_clear_when_any_progress_job_settles(driver, job):
    driver.window.worker.progress.emit("Counting destination: 2026-03-01 to 2026-03-03")
    driver.wait(50)
    assert driver.window.progress_label.text()
    driver.window.worker.pending.add(job)
    driver.window._settle(job)
    assert driver.window.progress_label.text() == ""
    assert driver.window.progress_lines == {}


def test_count_timeout_at_start_names_the_query_and_sends_nothing(driver):
    from logs_migrate.errors import QueryTimeout

    driver.connect()
    driver.plan()
    driver.acknowledge()
    env = driver.window.worker.session.env
    message = "source count query from 2026-03-01 to 2026-03-03 timed out"

    def count(scope, window, keys):
        raise QueryTimeout(message)

    env.count = count
    driver.start()
    assert driver.state.started_run_id is None
    page = driver.pages["plan"]
    assert message in page.note.text()
    assert "No API request was submitted." in page.note.text()
    assert "may have reached the API" not in page.note.text()
    assert page.start_button.isEnabled()


def test_trust_dialog_names_shared_host_before_accepting(driver, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    shown = []

    def question(parent, title, text, *args):
        shown.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", question)
    identities = [
        {
            "kind": "ssh",
            "name": "node-b:22",
            "fingerprint": "a" * 64,
            "display": "node-b fingerprint",
            "shared_with": "node-a:22",
        },
    ]
    assert not driver.window._ask_trust(identities)
    assert "node-b:22 presents the same SSH key as node-a:22" in shown[0]


def test_connections_selftest_failure_wraps_and_accepts_keyboard_focus(driver):
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtTest import QTest

    driver.connect()
    driver.show("connections")
    message = (
        "The source node could not complete its inventory listing. "
        "Check the node's disk and network access, then retry the self-test. "
    ) * 5
    driver.state.selftest = {
        "steps": [{"name": "Source node", "status": "fail", "detail": "gui:" + message}],
        "summary": {"pass": 0, "fail": 1, "skip": 0},
        "passed": False,
        "notes": {},
    }
    page = driver.pages["connections"]
    page.refresh()
    driver.wait(100)
    table = page.checks
    row = next(i for i, cells in enumerate(table.rows()) if cells[0] == "Source node")
    item = table.item(row, 1)
    rect = table.fontMetrics().boundingRect(
        QRect(0, 0, table.columnWidth(1) - 12, 10000),
        int(Qt.TextFlag.TextWordWrap),
        item.text(),
    )
    assert table.wordWrap()
    assert table.rowHeight(row) >= rect.height()
    driver.window.resize(900, 900)
    driver.wait(100)
    narrow = table.fontMetrics().boundingRect(
        QRect(0, 0, table.columnWidth(1) - 12, 10000),
        int(Qt.TextFlag.TextWordWrap),
        item.text(),
    )
    assert table.rowHeight(row) >= narrow.height()
    assert table.focusPolicy() != Qt.FocusPolicy.NoFocus
    table.setFocus(Qt.FocusReason.TabFocusReason)
    table.setCurrentCell(row, 1)
    QTest.keyClick(table, Qt.Key.Key_Down)
    assert table.hasFocus() and table.currentRow() > row


def test_live_trust_is_requested_before_credentials(driver, monkeypatch):
    attempts, asked, store = _live_simulator(monkeypatch, driver)
    page = driver.pages["connections"]
    page.source_host.setText("aria-logs.lab.example")
    page.destination_host.setText("vcf-logs.lab.example:9543")
    page.connect()
    driver.until(lambda: bool(page.validation))
    driver.idle()
    assert len(asked) == 1 and store.enrolled and len(attempts) == 2
    assert "password" in page.validation.lower()
    assert not driver.state.connected


def test_probing_endpoints_releases_an_open_connection_first(monkeypatch):
    from logs_migrate.gui.session import Session
    from logs_migrate.security import Credentials

    class Client:
        cleared = False

        def clear_token(self):
            self.cleared = True

    class Env:
        source_client = Client()
        destination_client = Client()

    session = Session()
    session.env = Env()
    session.credentials = Credentials({"source_api": "secret"}, interactive=False)
    held = session.credentials
    monkeypatch.setattr(Session, "_open_live", lambda self, *, probe_only=False: None)
    session.probe_endpoints(connection={"source_host": "s", "destination": "d"})
    assert session.env is None
    assert Env.source_client.cleared and Env.destination_client.cleared
    assert session.credentials is not held
    assert not held.snapshot()


def test_testing_endpoints_from_a_connected_window_drops_the_stale_connection(driver, monkeypatch):
    _live_simulator(monkeypatch, driver)
    driver.connect()
    assert driver.state.connected and driver.window.timer.isActive()
    driver.window.probe_environments({"source_api": "x"})
    assert not driver.state.connected and driver.state.discovered is None
    assert not driver.window.timer.isActive()
    driver.until(lambda: not driver.window.worker.pending)
    driver.idle()
    assert driver.state.status_error is None


def test_plan_overlap_warning_is_readable_and_group_ids_expand(driver):
    driver.connect()
    driver.plan()
    run = driver.state.plan_run
    group = run["plan"]["selected"][0]
    key = group["node"] + "/" + group["id"]
    run["plan"]["destination"]["inventory_available"] = True
    run["plan"]["warnings"].append(
        f"Earlier task old-task ran 2026-01-01; its window was 2024-01-01 to "
        f"2025-01-01 UTC. Selected groups crossing that window: {key}. "
        "Destination identities were checked."
    )
    page = driver.pages["plan"]
    page.refresh()
    driver.wait(100)
    page.scroll.verticalScrollBar().setValue(page.scroll.verticalScrollBar().maximum())
    driver.wait(50)
    assert "Earlier task old-task" in page.warnings.text()
    assert "Group 1" in page.warnings.text() and group["node"] in page.warnings.text()
    assert group["id"] not in page.warnings.text()
    assert page.warnings.height() >= page.warnings.heightForWidth(page.warnings.width())
    driver.window.resize(1000, 700)
    driver.wait(100)
    assert page.warnings.height() >= page.warnings.heightForWidth(page.warnings.width())
    assert "Earlier migration dates overlap" in dict(page.checks.rows())["Destination overlap"]
    assert page.warning_ids_toggle.isVisible()
    page.warning_ids_toggle.click()
    assert group["id"] in page.warning_ids.toPlainText()


def test_overlap_refusal_names_plan_group_and_hides_id(driver):
    driver.connect()
    driver.plan()
    group = driver.state.plan_run["plan"]["selected"][0]
    key = group["node"] + "/" + group["id"]
    driver.state.plan_error = (
        f"overlapping_transfer: Earlier task old ran 2026-01-01; its window was "
        f"2024-01-01 to 2025-01-01 UTC. Selected groups crossing that window: {key}."
    )
    page = driver.pages["plan"]
    page.refresh()
    refused = dict(page.overlap_rows.rows())["Refused"]
    assert "Group 1" in refused and group["node"] in refused and "UTC" in refused
    assert group["id"] not in refused
    page.group_ids_toggle.click()
    assert group["id"] in page.group_ids.toPlainText()


def test_unknown_group_count_explains_retry_and_expands_id(driver):
    driver.connect()
    driver.plan()
    driver.acknowledge()
    driver.start()
    driver.poll_until_finished()
    driver.verify()
    group = driver.state.plan_run["plan"]["selected"][0]
    result = driver.state.verification
    result["outcome"] = "incomplete_verification"
    result["bucket_check"] = {
        "matched_groups": 0,
        "total_groups": len(driver.state.plan_run["plan"]["selected"]),
        "matched_events": 0,
        "total_events": 6,
        "unmatched": [],
        "unknown": [group["node"] + "/" + group["id"]],
    }
    page = driver.pages["verification"]
    page.refresh()
    text = page.banner.text()
    assert "Group 1" in text and group["node"] in text and group["id"] not in text
    assert "incomplete" in text and "Run verify again" in text and "destination" in text
    page.group_ids_toggle.click()
    assert group["id"] in page.group_ids.toPlainText()


@pytest.mark.parametrize("with_bucket_check", [False, True])
def test_incomplete_destination_copy_is_visible_when_daily_counts_match(driver, with_bucket_check):
    driver.connect()
    driver.plan()
    driver.acknowledge()
    driver.start()
    driver.poll_until_finished()
    driver.verify()
    group = driver.state.plan_run["plan"]["selected"][0]
    result = driver.state.verification
    result["outcome"] = "verified_discrepancy"
    result["incomplete_destination_copies"] = ["dst-1/" + group["id"]]
    if with_bucket_check:
        result["daily_outcome"] = "verified_success"
        result["bucket_check"] = {
            "matched_groups": 3,
            "total_groups": 3,
            "matched_events": 6,
            "total_events": 6,
            "unmatched": [],
            "unknown": [],
        }
    page = driver.pages["verification"]
    page.refresh()
    assert "incomplete" in page.banner.text().lower()
    assert "dst-1" in page.banner.text() and group["id"] not in page.banner.text()
    assert page.group_ids_toggle.isVisible()
    page.group_ids_toggle.click()
    assert group["id"] in page.group_ids.toPlainText()
    page.group_ids_copy.click()
    assert group["id"] in QApplication.clipboard().text()


def test_mismatched_group_count_uses_a_label_and_offers_its_id(driver):
    driver.connect()
    driver.plan()
    driver.acknowledge()
    driver.start()
    driver.poll_until_finished()
    driver.verify()
    group = driver.state.plan_run["plan"]["selected"][0]
    result = driver.state.verification
    result["outcome"] = "verified_discrepancy"
    result["daily_outcome"] = "verified_success"
    result["bucket_check"] = {
        "matched_groups": 2,
        "total_groups": 3,
        "matched_events": 4,
        "total_events": 6,
        "unmatched": [group["node"] + "/" + group["id"]],
        "unknown": [],
    }
    page = driver.pages["verification"]
    page.refresh()
    assert "Group 1" in page.banner.text() and group["id"] not in page.banner.text()
    assert page.group_ids_toggle.isVisible()


def test_conflicting_destination_entries_are_visible_without_raw_ids(driver):
    driver.connect()
    driver.plan()
    driver.acknowledge()
    driver.start()
    driver.poll_until_finished()
    driver.verify()
    group = driver.state.plan_run["plan"]["selected"][0]
    result = driver.state.verification
    result["outcome"] = "verified_discrepancy"
    result["conflicting_destination_copies"] = ["dst-1/" + group["id"]]
    page = driver.pages["verification"]
    page.refresh()
    assert "conflicting message counts" in page.banner.text().lower()
    assert "dst-1" in page.banner.text() and group["id"] not in page.banner.text()
    assert page.group_ids_toggle.isVisible()
    page.group_ids_toggle.click()
    assert group["id"] in page.group_ids.toPlainText()


@pytest.mark.parametrize("code", ["bucket_identity_conflict", "incomplete_evidence"])
def test_blocked_reason_stays_visible_when_changing_dates(driver, code):
    from PySide6.QtCore import QDate

    driver.connect()
    driver.plan()
    group = driver.state.plan_run["plan"]["selected"][0]
    driver.state.plan_error = (
        f"{code}: 1 destination bucket needs attention: {group['id']} "
        "(2026-03-01 to 2026-03-01 UTC; destination: dst-1; source size 1000 bytes, "
        "destination size 1 bytes). Inspect the destination."
    )
    driver.state.plan_affected = [group["id"]]
    page = driver.pages["plan"]
    page.refresh()
    assert group["id"] not in page.blocked_banner.text()
    assert group["id"] not in str(page.overlap_rows.rows())
    page.change_dates()
    page.start_date.setDate(QDate(2025, 1, 1))
    page.end_date.setDate(QDate(2025, 1, 31))
    assert "dst-1" in page.error.text() and "2026-03-01" in page.error.text()
    assert group["id"] not in page.error.text()
    assert page.error_ids_toggle.isVisible()
    page.error_ids_toggle.click()
    page.error_ids_copy.click()
    assert group["id"] in QApplication.clipboard().text()


def test_warning_for_already_received_group_hides_id_and_offers_copy(driver):
    driver.connect()
    driver.plan()
    plan = driver.state.plan_run["plan"]
    group = plan["selected"].pop(0)
    plan["already_received"] = [group]
    plan["warnings"].append(f"Multiple destination copies: {group['id']} (destination: dst-1).")
    page = driver.pages["plan"]
    page.refresh()
    assert group["id"] not in page.warnings.text()
    assert page.warning_ids_toggle.isVisible()
    page.warning_ids_toggle.click()
    assert group["id"] in page.warning_ids.toPlainText()


def test_delayed_probe_shows_activity_elapsed_then_prompts_for_trust(driver, monkeypatch):
    import time

    from logs_migrate.gui import session

    attempts, asked, store = _live_simulator(monkeypatch, driver)
    original = session.Session._open_live

    def slow_open(self, *, probe_only=False):
        if probe_only and not attempts:
            self.on_progress("Contacting tls aria-logs.lab.example:9543")
            time.sleep(1.8)
        return original(self, probe_only=probe_only)

    monkeypatch.setattr(session.Session, "_open_live", slow_open)
    page = driver.pages["connections"]
    page.source_host.setText("aria-logs.lab.example")
    page.destination_host.setText("vcf-logs.lab.example:9543")
    page.connect()
    driver.until(lambda: driver.window.probe_activity.isVisible())
    driver.until(lambda: "0:01 elapsed" in driver.window.probe_elapsed.text(), timeout_ms=1500)
    assert driver.window.probe_activity.minimum() == driver.window.probe_activity.maximum() == 0
    assert "Checking this appliance's identity" in driver.window.page_progress.text()
    assert "tls" not in driver.window.page_progress.text()
    driver.until(lambda: bool(page.validation))
    driver.idle()
    assert asked and store.enrolled
    assert not driver.window.probe_activity.isVisible()


def test_failed_job_does_not_keep_groups_from_an_earlier_failure(driver, monkeypatch):
    from logs_migrate.errors import Code, MigrationError
    from logs_migrate.gui.session import Session

    first = MigrationError(Code.COLLISION, "copy differs")
    first.affected_ids = {"stale-bucket"}
    outcomes = [first, RuntimeError("boom")]

    def failing_plan(self, **kwargs):
        raise outcomes.pop(0)

    monkeypatch.setattr(Session, "plan", failing_plan)
    worker = driver.window.worker
    worker.blockSignals(True)
    worker._run("plan", "plan", {})
    assert worker.affected["plan"] == ["stale-bucket"]
    worker._run("plan", "plan", {})
    assert worker.affected.get("plan") is None
