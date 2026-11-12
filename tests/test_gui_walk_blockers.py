"""Regressions for the independent desktop walk's four blockers."""

import json

import pytest

pytest.importorskip("PySide6")

from gui_driver import FIXTURES, Driver  # noqa: E402


def fixture_with_versions(tmp_path, source, destination):
    data = json.loads((FIXTURES / "sealed-populated.fixture").read_text())
    data["source"]["version"] = source
    data["destination"]["version"] = destination
    path = tmp_path / "versions.fixture"
    path.write_text(json.dumps(data))
    return path


def test_refused_version_names_the_problem_and_marks_source(tmp_path):
    driver = Driver(fixture_with_versions(tmp_path, "9.2.0", "9.0.2.0200"))
    try:
        driver.show("connections")
        driver.connect()
        page = driver.pages["connections"]
        assert "Versions not compatible" in page.error.text()
        assert "No migration request" not in page.error.text()
        assert "9.0.2.0200" in page.error.text() and "9.0.2.0200" in page.destination_badge.text()
        assert page.source_badge.property("status") == "bad"
        assert not driver.state.connected
    finally:
        driver.close()


def test_untested_destination_warning_survives_inventory(tmp_path):
    driver = Driver(fixture_with_versions(tmp_path, "8.18.7", "9.2.1"))
    try:
        driver.show("connections")
        driver.connect()
        assert driver.state.connected
        assert driver.pages["connections"].version_caution.isVisible()
        assert "9.2.1" in driver.pages["connections"].version_caution.text()
        driver.plan()
        assert driver.pages["plan"].untested_badge.isVisible()
        assert "9.2.1" in driver.pages["plan"].untested_badge.text()
    finally:
        driver.close()


def test_activity_is_page_owned_and_does_not_shift_page():
    driver = Driver(FIXTURES / "sealed-populated.fixture")
    try:
        driver.show("connections")
        window = driver.window
        y = (
            driver.pages["connections"]
            .mapTo(window, driver.pages["connections"].rect().topLeft())
            .y()
        )
        window.activity_job = "plan"
        window.activity_page = "connections"
        window._show_progress("Checking versions…")
        driver.wait()
        assert window.activity_panel.isVisible()
        assert "Preparing preview" in window.page_progress.text()
        assert (
            driver.pages["connections"]
            .mapTo(window, driver.pages["connections"].rect().topLeft())
            .y()
            == y
        )
        driver.show("configuration")
        assert not window.activity_panel.isVisible()
        window._show_progress("Sizing buckets… node 1 of 2")
        assert not window.activity_panel.isVisible()
    finally:
        driver.close()


def test_small_plan_footer_has_readable_note():
    from PySide6.QtWidgets import QBoxLayout

    driver = Driver(FIXTURES / "sealed-populated.fixture")
    try:
        driver.window.resize(1024, 700)
        driver.connect()
        driver.plan()
        page = driver.pages["plan"]
        driver.wait(100)
        assert page.footer.layout().direction() == QBoxLayout.Direction.TopToBottom
        assert page.note.width() >= 300
        assert page.split.direction() == QBoxLayout.Direction.TopToBottom
    finally:
        driver.close()
