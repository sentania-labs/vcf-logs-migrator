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


def test_connect_without_a_fixture_explains_the_release_gate(driver):
    driver.state.settings.fixture_path = ""
    driver.connect()
    assert driver.state.connect_error == "unsupported_capability"
    assert "not qualified" in driver.pages["connections"].error.text()


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
