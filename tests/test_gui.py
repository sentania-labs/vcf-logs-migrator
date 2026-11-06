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
    assert driver.pages["connections"].source_badge.text() == "8.18.7 · connected"
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
