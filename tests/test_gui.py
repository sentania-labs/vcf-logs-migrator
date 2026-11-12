"""The real window against stand-in systems, offscreen, driven by the buttons a person presses.

Each test is one of the jobs the screens exist for. Skipped when PySide6 is not installed.
"""

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_driver import DESTINATION, SOURCE, Driver  # noqa: E402

# Words that belong in the troubleshooting file and saved detail files, never on a screen.
BANNED = re.compile(
    r"\b(bucket|buckets|group id|sealed|active tail|api task|ingest|reconciliation|journal|"
    r"fixture|node|nodes|ssh)\b",
    re.IGNORECASE,
)


def plain(driver: Driver) -> str:
    """The current screen's words, checked for anything a non-expert should not meet."""
    text = driver.texts()
    found = BANNED.search(text.replace(str(driver.home), ""))
    assert found is None, (
        f"{found.group(0)!r} on screen: …{text[max(0, found.start() - 60) : found.end() + 60]}…"
    )
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", text), "an identifier on screen"
    return text


@pytest.fixture
def driver():
    instance = Driver("plain", delay=0)
    yield instance
    instance.close()


def ready(scenario: str = "plain", **options) -> Driver:
    """Signed in, rules copied, on Choose dates."""
    driver = Driver(scenario, delay=0, **options)
    driver.sign_in()
    driver.copy_rules()
    return driver


# Job 1 -------------------------------------------------------------------------------------


def test_first_launch_opens_on_connect_and_says_what_is_missing(driver):
    window = driver.window
    assert window.current_page() == "connect"
    text = plain(driver)
    assert "Have these ready" in text.title() or "HAVE THESE READY" in text
    for step, reason in (
        ("rules", "After connecting"),
        ("dates", "After connecting"),
        ("copy", "After choosing dates"),
        ("check", "After a copy"),
    ):
        assert not window.unlocked(step)
        assert window.steps[step].sub.text() == reason
        driver.step(step)
        assert window.current_page() == "connect"
    assert not driver.button("Test connections").isEnabled()
    assert "source address, source password, source server password" in driver.page.hint.text()
    driver.fill(source_host=SOURCE, source_password="x", ssh_password="x")
    assert "source" not in driver.page.hint.text().split(":", 1)[1].split("destination")[0]
    driver.fill(destination_host=DESTINATION, destination_password="x")
    assert driver.button("Test connections").isEnabled()
    assert "Ready." in driver.page.hint.text()
    # The user names came filled in, and nobody typed a port.
    assert driver.page.source_user.text() == "admin" and driver.page.ssh_user.text() == "root"


def test_one_test_asks_about_the_two_addresses_before_any_password_is_sent(driver):
    window = driver.window
    driver.sign_in(confirm=False)
    assert window.modal.isVisible() and "First time connecting" in window.modal.title
    text = plain(driver)
    assert "No password has been sent yet" in text
    assert re.search(r"([0-9A-F]{2}:){15}[0-9A-F]{2}", text), "fingerprints as a browser shows them"
    assert SOURCE in text and DESTINATION in text
    driver.click("Cancel. Send nothing.")
    assert not driver.state.connected and "Nothing was sent." in window.toasts
    driver.click("Test connections")
    driver.until(lambda: window.modal.isVisible())
    driver.click("These are my systems. Continue.")
    driver.idle()
    assert driver.state.connected
    text = plain(driver)
    assert "Connected. These two systems can migrate." in text
    assert "Version 8.18.7 · 3 servers" in text and "Version 9.0.2 · 3 servers" in text
    assert window.chip_texts == [
        f"✓ Source: {SOURCE} · 8.18.7",
        f"✓ Destination: {DESTINATION} · 9.0.2",
    ]
    assert "6 servers behind these two addresses" in text
    # The same pair is not asked about again; a changed address is.
    driver.click("Sign out of both")
    driver.click("Sign out")
    assert not driver.state.connected and driver.page.source_password.text() == ""
    driver.sign_in(confirm=False)
    driver.idle()
    assert driver.state.connected and not window.modal.isVisible()


def test_identity_codes_list_both_addresses_and_every_server(driver):
    driver.sign_in()
    driver.link("codes")
    driver.idle()
    text = driver.texts()
    assert "Identity codes" in driver.window.modal.title
    assert text.count("SHA256:") == 6 and "logs-old-2" in text and "logs-new-3" in text
    driver.click("Close")


def test_a_wrong_password_marks_only_that_box_and_keeps_the_side_that_worked(driver):
    driver.sign_in(confirm=False)
    driver.click("Cancel. Send nothing.")
    driver.fill(destination_password="wrong")
    driver.click("Test connections")
    driver.until(lambda: driver.window.modal.isVisible())
    driver.click("These are my systems. Continue.")
    driver.idle()
    page = driver.page
    text = plain(driver)
    assert "The source signed in. The destination did not." in text
    assert page.destination_password.property("status") == "bad"
    assert page.source_password.property("status") != "bad"
    assert "did not accept this user and password" in page.destination_password_hint.text()
    assert driver.window.chip_texts == ["✓ Source signed in", "✕ Destination not signed in"]
    assert page.source_password.text() == "letmein"  # Nothing else needs retyping.
    driver.fill(destination_password="letmein")
    assert "Password changed. Test again to check it." in driver.texts()
    assert page.destination_password.property("status") != "bad"
    driver.click("Test again")
    driver.idle()
    assert driver.state.connected


def test_an_address_that_is_not_found_is_named_and_no_password_is_sent(driver):
    driver.fill(
        source_host="logs-odl.example.com",
        source_password="x",
        ssh_password="x",
        destination_host=DESTINATION,
        destination_password="x",
    )
    driver.click("Test connections")
    driver.idle()
    text = plain(driver)
    assert "Could not find the source system." in text
    assert "The destination was found. No password was sent to either." in text
    assert driver.page.source_host.property("status") == "bad"
    assert driver.window.chip_texts == ["✕ Source not found", "Destination found"]
    assert not driver.window.modal.isVisible()


# Job 2 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "title", "todo", "badge"),
    [
        (
            "srcnewer",
            "the source is newer than the destination",
            "upgrade the destination to 9.0.2",
            "NEEDS UPGRADING",
        ),
        (
            "dstold",
            "the destination is too old",
            "check that the destination address really is",
            "TOO OLD",
        ),
        (
            "mixed",
            "the source servers are on different versions",
            "finish upgrading the source",
            "SERVERS DISAGREE",
        ),
    ],
)
def test_two_systems_that_cannot_migrate_say_which_and_what_to_do(scenario, title, todo, badge):
    driver = Driver(scenario, delay=0)
    try:
        driver.sign_in()
        text = plain(driver)
        assert not driver.state.connected and driver.state.refused
        assert title in text and todo in text and badge in text
        assert driver.window.chip_texts[-1] == "Cannot migrate"
        assert driver.buttons() == ["Change sign-in details", "Test again"]
        for step in ("rules", "dates", "copy", "check"):
            assert not driver.window.unlocked(step)
        driver.click("Change sign-in details")
        assert title in driver.texts() and driver.page.source_host.isVisible()
    finally:
        driver.close()


def test_a_version_the_app_has_not_been_tried_with_connects_and_says_so():
    driver = Driver("untested", delay=0)
    try:
        driver.sign_in()
        assert driver.state.connected
        assert "Version 9.2.0 has not been tried with this app." in plain(driver)
    finally:
        driver.close()


def test_a_9_1_destination_asks_for_an_access_token_only_once_it_is_seen():
    driver = Driver("ops9", delay=0)
    try:
        page = driver.window.pages["connect"]
        assert not page.token_row.isVisible()
        driver.sign_in()
        text = plain(driver)
        assert not driver.state.connected
        assert "This destination is version 9.1, which signs in a different way." in text
        assert page.token_row.isVisible() and not page.password_row.isVisible()
        assert "VCF Operations address, access token" in driver.page.hint.text()
        driver.fill(ops_url="ops.example.com", ops_token="a-token")
        driver.click("Test again")
        driver.until(lambda: driver.window.modal.isVisible() or not driver.window.busy("connect"))
        if driver.window.modal.isVisible():
            driver.click("These are my systems. Continue.")
        driver.idle()
        assert driver.state.connected and not driver.state.server_sign_in
    finally:
        driver.close()


# Copy rules --------------------------------------------------------------------------------


def test_rules_compare_by_themselves_and_the_needed_one_stays_ticked(driver):
    driver.sign_in()
    driver.click("Continue →")
    driver.idle()
    page = driver.page
    text = plain(driver)
    assert "5 rules on the source are missing or different on the destination." in text
    assert "NEEDED" in text and "Storage group “Firewall”" in text
    needed = page.boxes["partitions:Firewall"]
    assert needed.isChecked() and not needed.isEnabled()
    assert driver.buttons() == ["Copy only the needed one…", "Copy 5 rules…"]
    page.boxes["masking:hide-secrets"].click()
    assert driver.buttons()[-1] == "Copy 4 rules…"
    driver.click("Copy 4 rules…")
    text = driver.texts()
    assert "adds 3 rules" in text and "changes 1" in text and "Nothing is deleted" in text
    assert "Forwarding “to-siem”" in text and "hide-secrets" not in text
    driver.click("Copy 4 rules")
    driver.idle()
    text = plain(driver)
    assert "4 rules copied to the destination." in text
    assert "LEFT OUT" in text and "You chose not to copy it" in text
    assert driver.window.step_mark("rules") == "done"
    assert driver.buttons() == ["Continue to dates →"]


def test_matching_rules_finish_the_step_without_a_visit():
    driver = Driver("rules-none", delay=0)
    try:
        driver.sign_in()
        assert driver.window.step_mark("rules") == "done"
        driver.click("Continue →")
        driver.idle()
        assert "Nothing to copy" in plain(driver)
        assert driver.buttons() == ["Continue to dates →"]
    finally:
        driver.close()


def test_many_rules_are_all_visible_as_a_tick_list_that_scrolls_inside_its_box():
    driver = Driver("rules-many", delay=0, size=(1024, 700))
    try:
        driver.sign_in()
        driver.click("Continue →")
        driver.idle()
        text = plain(driver)
        assert "14 rules on the source" in text and "3 THAT NEED A WORD" in text
        assert "11 MORE, ALL MISSING ON THE DESTINATION AND SIMPLY ADDED" in text
        assert len(driver.page.boxes) == 14
        assert driver.overflow() == 0
        assert "More rules below" in text
        driver.link("needed")
        assert driver.buttons()[-1] == "Copy 1 rule…"
    finally:
        driver.close()


def test_a_rule_that_cannot_be_copied_is_named_and_can_be_tried_again():
    driver = Driver("rules-partial", delay=0)
    try:
        driver.sign_in()
        driver.click("Continue →")
        driver.idle()
        driver.click("Copy 5 rules…")
        driver.click("Copy 5 rules")
        driver.idle()
        text = plain(driver)
        assert "4 rules copied. 1 was not." in text and "“to-siem” could not be added" in text
        assert driver.window.step_mark("rules") == "warn"
        assert driver.buttons() == ["Try that rule again", "Continue to dates →"]
        driver.click("Try that rule again")
        driver.idle()
        assert "1 rule copied to the destination." in driver.texts()
    finally:
        driver.close()


# Job 3 -------------------------------------------------------------------------------------


def test_the_plan_is_six_short_lines_and_start_waits_for_the_tick():
    driver = ready()
    try:
        driver.step("dates")
        driver.idle()
        page = driver.page
        assert list(page.presets) == [
            "Last 3 months",
            "Last 6 months",
            "Last 12 months",
            "Everything",
        ]
        assert "No plan yet" in plain(driver)
        driver.click("Last 3 months")
        driver.click("Check these dates")
        driver.idle()
        text = plain(driver)
        keys = [key for key, _text, _small in page.plan_lines.texts]
        assert keys == ["Copies", "Size", "Time", "Room", "Left out"]
        assert "Enough free space on all 3 destination servers" in text
        assert "will not be copied this time" in page.ack.text()
        assert not driver.button("Start copying →").isEnabled()
        page.ack.click()
        assert driver.button("Start copying →").isEnabled()
        # Changing a date retires the plan and the tick that went with it.
        page.start_date.setDate(page.start_date.date().addDays(-10))
        driver.wait(40)
        assert driver.state.plan is None and not driver.state.acknowledged
        assert driver.buttons() == ["Check these dates"]
        # A plan that has to start earlier than asked says why, once, in plain words.
        driver.click("Check these dates")
        driver.idle()
        text = plain(driver)
        assert "more than you picked. The source stores" in text
        assert "before your From date" in page.date_hint.text()
        driver.link("list")
        assert (
            "What will be copied" in driver.window.modal.title
            and "Yes, all of it" in driver.texts()
        )
        driver.click("Close")
        driver.link("space")
        assert "logs-new-2" in driver.texts()
        driver.click("Close")
    finally:
        driver.close()


def test_dates_that_cannot_be_checked_say_so_beside_the_boxes():
    driver = ready()
    try:
        driver.step("dates")
        driver.idle()
        page = driver.page
        page.end_date.setDate(page.start_date.date().addDays(-3))
        driver.wait(40)
        assert "The end date is before the start date." in page.date_hint.text()
        assert not driver.button("Check these dates").isEnabled()
        driver.click("Last 6 months")
        page.end_date.setDate(page.end_date.date().addDays(1))
        driver.wait(40)
        assert "still being written and cannot be copied yet" in page.date_hint.text()
        driver.link("use-last")
        assert driver.button("Check these dates").isEnabled()
    finally:
        driver.close()


def test_the_oldest_days_are_flagged_only_when_the_source_is_about_to_delete_them():
    driver = ready()
    try:
        driver.choose("Everything")
        assert "about to be deleted by the source" in plain(driver)
        driver.click("Last 3 months")
        driver.click("Check these dates")
        driver.idle()
        assert "about to be deleted" not in driver.texts()
    finally:
        driver.close()


# Job 4 -------------------------------------------------------------------------------------


def test_broken_copies_are_shown_as_dates_and_the_plan_leaves_them_out():
    driver = ready("broken")
    try:
        driver.choose("Last 6 months")
        text = plain(driver)
        assert (
            "8 days cannot be copied: the destination already holds a broken copy of them" in text
        )
        assert "Only part of it arrived" in text and "Size differs from the source" in text
        assert driver.buttons() == ["Plan without these 8 days"]
        driver.link("save-details")
        driver.idle()
        saved = driver.reports / "broken-copies.txt"
        assert saved.exists() and "identifier:" in saved.read_text()
        assert "Saved “broken-copies.txt”" in driver.texts()
        driver.click("Plan without these 8 days")
        driver.idle()
        text = plain(driver)
        assert "except 8 days" in text and "Skipped" in text
        assert "the 8 skipped days and the newest logs" in driver.page.ack.text()
        assert len(driver.state.plan["parts"]) == 3
        driver.start()
        driver.finish(120000)
        assert driver.window.copy_phase() == "done"
        driver.click("Check what arrived →")
        driver.idle()
        text = plain(driver)
        assert "Everything arrived" in text and "All 176 days match." in text
        assert "(in 3 parts)" in text and "The copy ran as 3 parts back to back" in text
    finally:
        driver.close()


def test_dates_already_being_copied_offer_the_copy_and_the_free_dates():
    driver = ready(speed=150)
    try:
        driver.choose("Last 3 months")
        driver.start()
        driver.progress(0.05)
        driver.step("dates")
        assert "Running now ·" in driver.window.steps["copy"].sub.text()
        driver.click("Last 6 months")
        driver.click("Check these dates")
        driver.idle()
        text = plain(driver)
        assert "Part of these dates is already being copied" in text
        assert "In the copy running now" in text and "Free to copy" in text
        assert driver.buttons()[0] == "Watch the running copy"
        assert driver.buttons()[1].startswith("Copy only ")
        driver.click("Watch the running copy")
        assert driver.window.current_page() == "copy"
        assert driver.buttons() == ["Stop the copy…"]
    finally:
        driver.close()


def test_other_refusals_name_the_fix():
    driver = ready("space")
    try:
        driver.choose("Last 12 months")
        text = plain(driver)
        assert "Not enough room on the destination" in text and "logs-new-2" in text
        assert "short" in text and driver.buttons()[-1] == "Check the last 3 months instead"
        driver.page.start_date.setDate(driver.page.start_date.date().addYears(-3))
        driver.page.end_date.setDate(driver.page.start_date.date().addDays(30))
        driver.click("Check these dates")
        driver.idle()
        text = plain(driver)
        assert "The source has no finished logs in these dates" in text
        assert driver.buttons()[-1].startswith("Use ")
    finally:
        driver.close()
    driver = Driver("plain", delay=0)
    try:
        driver.sign_in()
        driver.choose("Last 3 months")
        assert "A rule the copy depends on is missing on the destination" in plain(driver)
        assert driver.buttons() == ["Go to Copy rules"]
    finally:
        driver.close()


def test_a_source_server_that_does_not_answer_stops_the_check_with_try_again():
    driver = ready("listfail")
    try:
        driver.choose("Last 6 months")
        text = plain(driver)
        assert "Could not read the list of logs from the source" in text
        assert driver.buttons() == ["Try again"]
        driver.click("Try again")
        driver.idle()
        assert driver.state.plan is not None
    finally:
        driver.close()


# Jobs 5 and 6 ------------------------------------------------------------------------------


def test_a_running_copy_updates_in_place_and_the_stop_button_keeps_its_click():
    driver = ready(speed=120)
    try:
        driver.choose("Last 3 months")
        driver.start()
        page = driver.page
        driver.wait(80)
        stop = driver.button("Stop the copy…")
        first = page.progress.percent.text()
        driver.progress(0.1)
        driver.wait(2500)
        assert page.progress.percent.text() != first
        assert driver.button("Stop the copy…") is stop, "the button was rebuilt while the copy ran"
        text = plain(driver)
        assert "You can close this window." in text and "Latest to arrive: logs up to" in text
        assert "Running for" in text and "Should finish around" in text
        assert "logs-new-1" in text and "COPYING" in text
        driver.click("Stop the copy…")
        assert "What has already arrived stays there." in driver.texts()
        driver.click("Keep copying")
        assert driver.window.copy_phase() == "running"
        driver.click("Stop the copy…")
        driver.click("Stop the copy")
        driver.finish()
        text = plain(driver)
        assert "You stopped the copy today at" in text
        assert "Arrived and safe on the destination" in text and "Still to copy" in text
        assert driver.buttons() == ["Check what arrived so far", "Continue copying"]
        # A copy that did not finish is never called complete.
        driver.click("Check what arrived so far")
        driver.idle()
        text = plain(driver)
        assert "So far so good" in text and "Everything arrived" not in text
        assert "have not been copied yet" in text
        assert driver.buttons()[-1] == "Continue copying"
        driver.click("Continue copying")
        driver.until(lambda: driver.window.copy_phase() == "running")
        driver.wait(2300)
        assert "Continued at" in driver.texts() and "Running again for" in driver.texts()
        driver.knob(pace={"bytes_per_second": 11_500_000, "time_scale": 30000})
        driver.finish(60000)
        assert driver.window.copy_phase() == "done"
        driver.click("Check what arrived →")
        driver.idle()
        assert "Everything arrived" in plain(driver) and "(in 2 parts)" in driver.texts()
    finally:
        driver.close()


def test_without_the_destination_server_sign_in_the_cost_is_said_everywhere_it_bites():
    driver = Driver("plain", delay=0, speed=150)
    try:
        assert "a copy that stops part-way cannot be continued from this app" in driver.texts()
        driver.sign_in(server=False)
        assert "Free space not checked: no server sign-in was given." in plain(driver)
        driver.copy_rules()
        driver.choose("Last 3 months")
        text = plain(driver)
        assert "Not checked." in text and "it cannot be continued from this app" in text
        driver.start()
        driver.progress(0.05)
        driver.click("Stop the copy…")
        assert "after stopping, this copy cannot be continued from this app" in driver.texts()
        assert driver.buttons() == ["Keep copying", "Stop anyway"]
        driver.click("Stop anyway")
        driver.finish()
        text = plain(driver)
        assert "This copy cannot be continued yet." in text
        assert driver.buttons() == [
            "Check what arrived so far",
            "Add the destination server sign-in",
        ]
        driver.click("Add the destination server sign-in")
        page = driver.page
        assert driver.window.current_page() == "connect"
        assert "One thing to add: the destination’s server sign-in." in driver.texts()
        assert page.destination_ssh_user.property("status") == "bad"
        assert page.source_password.text() == "letmein"  # Everything else is still filled in.
        assert "destination server sign-in" in page.hint.text()
        driver.fill(destination_ssh_user="root", destination_ssh_password="letmein")
        driver.click("Test again")
        driver.idle()
        assert "Now the stopped copy can be continued." in driver.texts()
        driver.click("Back to the stopped copy →")
        assert driver.buttons() == ["Check what arrived so far", "Continue copying"]
        driver.knob(pace={"bytes_per_second": 11_500_000, "time_scale": 30000})
        driver.click("Continue copying")
        driver.finish(60000)
        assert driver.window.copy_phase() == "done"
    finally:
        driver.close()


def test_a_copy_that_fails_by_itself_says_what_happened_and_continues():
    driver = ready("fail", speed=3000)
    try:
        driver.choose("Last 3 months")
        driver.start()
        driver.finish()
        text = plain(driver)
        assert "The copy stopped by itself today at" in text
        assert "its server logs-new-2 failed" in text and "save a troubleshooting file" in text
        assert driver.window.step_mark("copy") == "warn"
        driver.link("trouble")
        driver.idle()
        files = list(driver.reports.glob("troubleshooting-*.zip"))
        assert len(files) == 1
        driver.click("Continue copying")
        driver.finish(60000)
        assert driver.window.copy_phase() == "done"
    finally:
        driver.close()


def test_a_copy_that_cannot_start_says_why_and_keeps_the_plan():
    driver = ready("notstarted")
    try:
        driver.choose("Last 3 months")
        driver.page.ack.click()
        driver.click("Start copying →")
        driver.idle()
        text = plain(driver)
        assert "The copy did not start. No logs were copied." in text
        assert "the destination’s servers could not reach the source’s servers" in text
        assert driver.buttons() == [
            "Save details for the network team",
            "Back to the plan",
            "Start again",
        ]
        assert driver.state.plan is not None and driver.state.copies == []
        driver.click("Save details for the network team")
        driver.idle()
        assert (driver.reports / "network-path-needed.txt").exists()
        driver.click("Start again")
        driver.until(lambda: driver.window.copy_phase() == "running", what="the second start")
    finally:
        driver.close()


def test_an_unanswered_start_is_never_offered_again_and_locks_the_dates():
    driver = ready("unsure")
    try:
        driver.choose("Last 3 months")
        driver.page.ack.click()
        driver.click("Start copying →")
        driver.until(lambda: driver.window.copy_phase() == "unsure", what="an unanswered start")
        text = plain(driver)
        assert "Not sure yet whether the copy started" in text and "locked for 15 minutes" in text
        assert driver.buttons() == ["Ask now"]
        assert driver.state.plan is None  # The plan that was sent is not on offer again.
        assert driver.window.step_mark("copy") == "warn"
    finally:
        driver.close()


def test_losing_contact_keeps_the_last_figures_and_offers_reconnect():
    driver = ready("offline", speed=300)
    try:
        driver.knob(offline={"at_fraction": 0.03, "seconds": 4})
        driver.choose("Last 3 months")
        driver.start()
        driver.until(lambda: driver.state.lost_at is not None, 60000, "contact to be lost")
        text = plain(driver)
        assert "Lost contact with the destination at" in text
        assert "Do not start the copy again." in text and "not known right now" in text
        assert driver.buttons() == ["Reconnect"]
        assert "out of touch since" in driver.window.chip_texts[-1]
        driver.wait(4200)
        driver.click("Reconnect")
        driver.until(lambda: driver.state.lost_at is None, 30000, "reconnecting")
        driver.idle()
        assert driver.window.copy_phase() in {"running", "done"}
        assert any("Reconnected." in toast for toast in driver.window.toasts)
    finally:
        driver.close()


def test_a_quiet_spell_is_named_as_quiet_not_as_failure():
    driver = ready("stall", speed=400)
    try:
        driver.choose("Last 3 months")
        driver.start()
        driver.until(lambda: driver.window.quiet_minutes() >= 6, 90000, "a quiet spell")
        driver.wait(2200)
        text = plain(driver)
        assert re.search(r"No new logs have arrived for \d+ minutes\.", text)
        assert "The destination still answers, so the copy has not failed." in text
        assert "NOTHING NEW FOR" in text
        assert driver.window.step_mark("copy") == "warn"
        assert driver.buttons() == ["Stop the copy…"]
    finally:
        driver.close()


# Job 7 -------------------------------------------------------------------------------------


def test_missing_days_are_listed_alone_and_one_button_copies_just_those():
    driver = ready("missing")
    try:
        driver.choose("Last 6 months")
        driver.start()
        driver.finish()
        text = plain(driver)
        assert "Copy finished, but part of 8 days did not arrive" in text
        assert "the logs that logs-old-2 holds for" in text
        assert driver.buttons() == ["Check what arrived anyway", "Copy what is missing"]
        driver.click("Check what arrived anyway")
        driver.idle()
        text = plain(driver)
        assert (
            "8 of 184 days need attention" in text and "The other 176 days match exactly." in text
        )
        assert text.count("PART NEVER ARRIVED") == 2
        assert driver.window.step_mark("check") == "warn"
        driver.link("all-days")
        assert "Runs of matching days are shown as one row." in driver.texts()
        driver.click("Close")
        driver.click("Copy what is missing")
        driver.finish(60000)
        assert driver.page.title.text() == "Copy what was missing"
        assert "Finished copying what was missing" in plain(driver)
        driver.click("Check what arrived →")
        driver.idle()
        text = plain(driver)
        assert "Everything arrived" in text and "All 184 days match." in text
    finally:
        driver.close()


def test_a_day_that_arrives_short_says_how_many_and_that_it_cannot_be_repaired():
    driver = ready("short")
    try:
        driver.choose("Last 6 months")
        driver.start()
        driver.finish()
        driver.click("Check what arrived →")
        driver.idle()
        text = plain(driver)
        assert "1 of 184 days needs attention" in text and "123 FEWER" in text
        assert "The app cannot add entries to a day that has arrived." in text
        assert "keep the source until you have looked into it" in text
        assert "Copy what is missing" not in driver.buttons()
        assert driver.buttons() == ["Copy more dates", "Save report"]
    finally:
        driver.close()


def test_counts_that_do_not_settle_are_reported_as_no_result_yet():
    driver = ready("unsteady")
    try:
        driver.choose("Last 3 months")
        driver.start()
        driver.finish()
        driver.click("Check what arrived →")
        driver.idle(60000)
        text = plain(driver)
        assert "Could not finish checking" in text and "Everything arrived" not in text
        assert driver.buttons() == ["Check again"]
    finally:
        driver.close()


def test_save_writes_the_report_and_its_data_file_and_says_where():
    driver = ready()
    try:
        driver.choose("Last 3 months")
        driver.start()
        driver.finish()
        driver.click("Check what arrived →")
        driver.wait(60)
        driver.idle()
        assert "Everything arrived" in plain(driver)
        assert driver.buttons() == ["Copy more dates", "Save report"]
        driver.click("Save report")
        driver.idle()
        written = sorted(path.suffix for path in driver.reports.iterdir())
        assert written == [".json", ".md"]
        text = driver.texts()
        assert "and its data file" in text and "Open the report" in text
        assert driver.buttons() == ["Save report again", "Copy more dates"]
        report = next(driver.reports.glob("*.md")).read_text()
        assert "Everything arrived" in report and "## All days" in report
        assert BANNED.search(report) is None
        driver.link("open-report")
        assert driver.window.opened[-1].endswith(".md")
        # The same dates again: nothing to copy, said plainly.
        driver.click("Copy more dates")
        driver.idle()
        driver.click("Last 3 months")
        driver.click("Check these dates")
        driver.idle()
        assert "Nothing to copy: these dates are already on the destination" in plain(driver)
        driver.click("Last 12 months")
        driver.click("Check these dates")
        driver.idle()
        assert [key for key, *_ in driver.page.plan_lines.texts][:2] == ["Copies", "Already there"]
    finally:
        driver.close()


# Job 8 and reopening -----------------------------------------------------------------------


def test_past_runs_read_without_signing_in_and_lead_back_to_an_unfinished_copy(tmp_path):
    driver = Driver("plain", delay=0, speed=150, home=tmp_path)
    driver.step("runs")
    assert "No runs yet" in plain(driver)
    driver.sign_in()
    driver.copy_rules()
    driver.choose("Last 3 months")
    driver.click("Last 6 months")
    driver.click("Check these dates")
    driver.idle()  # A plan that is never started is not a run.
    driver.click("Last 3 months")
    driver.click("Check these dates")
    driver.idle()
    driver.start()
    driver.progress(0.05)
    driver.click("Stop the copy…")
    driver.click("Stop the copy")
    driver.finish()
    driver.close()

    driver = Driver("plain", delay=0, home=tmp_path)
    try:
        text = plain(driver)
        assert not driver.state.connected
        assert "Welcome back. A copy is still unfinished." in text
        driver.step("runs")
        rows = driver.page.rows
        assert len(rows) == 1 and rows[0]["result"][1].startswith("Stopped at ")
        driver.click("Open")
        text = plain(driver)
        assert "This copy was stopped at" in text and "never finished" in text
        assert driver.buttons() == [
            "← Back to past runs",
            "Save report",
            "Connect and continue this copy",
        ]
        driver.click("Save report")
        driver.idle()
        assert len(list(driver.reports.glob("*.md"))) == 1
        driver.click("Connect and continue this copy")
        assert driver.window.current_page() == "connect"
        assert "Sign in to continue this copy." in driver.texts()
        driver.fill(
            source_password="x",
            ssh_password="x",
            destination_password="x",
            destination_ssh_password="x",
        )
        driver.click("Test connections")
        driver.idle()
        assert "Your unfinished copy is ready to continue." in driver.texts()
        driver.click("Go to the unfinished copy →")
        driver.idle()
        assert driver.window.current_page() == "copy"
        assert driver.buttons() == ["Check what arrived so far", "Continue copying"]
    finally:
        driver.close()


# Job 9 -------------------------------------------------------------------------------------


def test_settings_save_as_they_change_and_a_bad_number_keeps_the_old_one(driver):
    from PySide6.QtTest import QTest

    driver.click("Settings")
    page = driver.page
    text = plain(driver)
    for heading in ("EVERYDAY", "HELP WITH PROBLEMS", "FINE TUNING"):
        assert heading in text
    assert "stand-in" not in text.lower() and "simulator" not in text.lower()
    driver.click("Dark")
    assert driver.state.settings.theme == "dark" and page.notes["theme"].text() == "✓ Saved"
    driver.click("Light")
    edit = page.refresh_seconds
    edit.setFocus()
    edit.clear()
    QTest.keyClicks(edit, "99")
    # The first 9 was a usable number and was saved; 99 is not, and 9 stays in use.
    assert (
        "Enter a whole number from 2 to 60. Still using 9." in page.notes["refresh_seconds"].text()
    )
    assert edit.property("status") == "bad" and driver.state.settings.refresh_seconds == 9
    page.debug.setFocus()
    driver.wait(40)
    assert edit.text() == "9" and edit.property("status") != "bad"
    assert "so it is still 9" in driver.window.toasts[-1]
    edit.setFocus()
    edit.clear()
    QTest.keyClicks(edit, "7")
    assert driver.state.settings.refresh_seconds == 7
    assert page.notes["refresh_seconds"].text() == "✓ Saved"
    driver.window.choose_folder = lambda title, start: str(driver.home / "elsewhere")
    driver.click("Change…")
    assert driver.state.settings.export_folder.endswith("elsewhere")
    driver.click("Put all settings back to how they came")
    assert "does not touch past runs" in driver.texts()
    driver.click("Reset settings")
    assert driver.state.settings.refresh_seconds == 5
    assert driver.buttons() == ["← Back to Connect"]
    driver.click("← Back to Connect")
    assert driver.window.current_page() == "connect"
    # The stand-in systems come from the command line only and are never written down.
    saved = (driver.home / "config" / "vcf-logs-migrator" / "settings.ini").read_text()
    assert "fixture" not in saved


# The frame ---------------------------------------------------------------------------------


def test_every_screen_fits_three_window_sizes_and_has_one_main_button_last():
    driver = ready()
    try:
        visited = []

        def check(name: str) -> None:
            visited.append(name)
            for size in ((1440, 900), (1280, 800), (1024, 700)):
                driver.resize(*size)
                assert driver.overflow() == 0, f"{name} needs page scrolling at {size}"
            driver.resize(1280, 800)
            primary = [
                text
                for text, button in driver.page.buttons.items()
                if button.property("variant") == "primary"
            ]
            assert len(primary) <= 1, f"{name} has more than one main button"
            if primary:
                assert driver.buttons()[-1] == primary[0], f"{name}: main button is not last"

        driver.step("connect")
        check("connect")
        driver.step("rules")
        check("rules")
        driver.choose("Last 12 months")
        check("plan")
        driver.start()
        driver.finish()
        check("copy finished")
        driver.click("Check what arrived →")
        driver.idle()
        check("result")
        driver.step("runs")
        check("past runs")
        driver.click("Settings")
        check("settings")
        assert len(visited) == 7
    finally:
        driver.close()


def test_every_input_is_named_and_the_keyboard_lands_on_the_main_action(driver):
    from PySide6.QtWidgets import QAbstractButton, QCheckBox, QComboBox, QDateEdit, QLineEdit

    window = driver.window
    for page in window.pages.values():
        for kind in (QLineEdit, QComboBox, QDateEdit, QCheckBox, QAbstractButton):
            for widget in page.findChildren(kind):
                if widget.objectName().startswith("qt_"):
                    continue
                assert widget.accessibleName() or widget.text(), (
                    f"unnamed {kind.__name__} on {page.title.text()}"
                )
    for step in window.steps.values():
        assert step.accessibleName()
    driver.sign_in()
    driver.wait(60)
    assert window.focusWidget() is driver.button("Continue →")
    driver.click("Sign out of both")
    assert window.focusWidget() is driver.button("Sign out")


def test_results_from_a_session_that_was_signed_out_are_dropped(driver):
    driver.sign_in()
    driver.window.submit("compare", "config_compare")
    driver.window.sign_out(quiet=True)
    driver.idle()
    assert driver.state.comparison is None and not driver.state.connected
