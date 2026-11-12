"""Walk every screen and state against stand-in systems and save a picture of each.

    QT_QPA_PLATFORM=offscreen python tests/gui_screenshots.py --output /tmp/gui-shots

Each picture is taken at 1280 by 800. Every state is also measured at 1440 by 900 and 1024 by
700: a screen that would need page scrolling at any of the three sizes is reported and makes
the script exit non-zero. `--only NAME` runs one walk (see WALKS at the bottom).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_driver import DESTINATION, SOURCE, Driver  # noqa: E402

SIZES = ((1440, 900), (1280, 800), (1024, 700))


class Shots:
    def __init__(self, output: Path):
        self.output = output
        self.saved: list[Path] = []
        self.overflows: list[str] = []

    def take(self, driver: Driver, name: str, sizes: bool = True) -> None:
        if sizes:
            for width, height in SIZES:
                driver.resize(width, height)
                driver.wait(40)
                extra = driver.overflow()
                if extra:
                    self.overflows.append(f"{name} at {width}x{height}: {extra}px do not fit")
            driver.resize(1280, 800)
        path = self.output / f"{name}.png"
        driver.grab(path)
        self.saved.append(path)


def connect_walk(shots: Shots) -> None:
    """Job 1: first launch to connected, the identity question and signing out."""
    d = Driver("plain", speed=1500, delay=0.3)
    shots.take(d, "01-connect-first-launch")
    d.fill(
        source_host=SOURCE,
        source_password="letmein",
        ssh_password="letmein",
        destination_host=DESTINATION,
        destination_password="letmein",
        destination_ssh_user="root",
        destination_ssh_password="letmein",
    )
    shots.take(d, "02-connect-ready-to-test")
    d.click("Test connections")
    d.until(lambda: d.window.modal.isVisible())
    shots.take(d, "04-connect-first-time-identity", sizes=False)
    d.click("These are my systems. Continue.")
    d.wait(500)
    shots.take(d, "03-connect-testing")
    d.idle()
    shots.take(d, "05-connect-connected")
    d.link("codes")
    d.idle()
    shots.take(d, "12c-connect-identity-codes", sizes=False)
    d.click("Close")
    d.click("Sign out of both")
    shots.take(d, "05b-connect-sign-out-question", sizes=False)
    d.click("Stay signed in")
    d.close()


def main_walk(shots: Shots) -> None:
    """Jobs 3, 5, 6, 7, 8, 9 on the path where everything works."""
    d = Driver("plain", speed=220, delay=0.3)
    d.sign_in()
    d.click("Continue →")
    d.wait(250)
    d.idle()
    shots.take(d, "13-rules-differences")
    d.click(d.buttons()[-1])
    shots.take(d, "14-rules-confirm", sizes=False)
    d.click(d.buttons()[-1])
    d.idle()
    shots.take(d, "17-rules-copied")
    d.click("Continue to dates →")
    d.idle()
    shots.take(d, "19-dates-no-plan-yet")
    d.page.end_date.setDate(d.page.start_date.date().addDays(-5))
    shots.take(d, "19b-dates-end-before-start")
    d.click("Last 6 months")
    d.page.end_date.setDate(d.page.end_date.date().addDays(2))
    shots.take(d, "19c-dates-still-being-written")
    d.link("use-last")
    d.click("Last 3 months")
    d.click("Check these dates")
    d.wait(600)
    shots.take(d, "20-dates-checking")
    d.idle()
    shots.take(d, "21-dates-plan-ready")
    d.link("space")
    shots.take(d, "23-dates-plan-space-per-server", sizes=False)
    d.click("Close")
    d.link("list")
    shots.take(d, "24-dates-plan-full-list", sizes=False)
    d.click("Close")
    d.page.ack.click()
    shots.take(d, "22-dates-plan-ready-agreed")
    d.resize(1440, 900)
    d.grab(shots.output / "67-large-window-plan-ready.png")
    d.resize(1024, 700)
    d.grab(shots.output / "64-small-window-plan-ready.png")
    d.resize(1280, 800)
    d.click("Start copying →")
    d.wait(450)
    shots.take(d, "33-copy-getting-ready")
    d.until(lambda: d.window.copy_phase() == "running")
    shots.take(d, "34-copy-just-started")
    d.progress(0.25)
    shots.take(d, "35-copy-running")
    d.resize(1024, 700)
    d.grab(shots.output / "65-small-window-copy-running.png")
    d.resize(1280, 800)
    d.theme("dark")
    d.grab(shots.output / "63-copy-running-dark.png")
    d.theme("light")
    d.step("dates")
    d.click("Last 6 months")
    shots.take(d, "35b-dates-while-a-copy-runs")
    d.click("Check these dates")
    d.idle()
    shots.take(d, "26-dates-refused-already-being-copied")
    d.click("Watch the running copy")
    d.click("Stop the copy…")
    shots.take(d, "36-copy-stop-question", sizes=False)
    d.click("Stop the copy")
    d.wait(350)
    shots.take(d, "36c-copy-stopping")
    d.finish()
    shots.take(d, "41-copy-stopped")
    d.click("Check what arrived so far")
    d.idle()
    shots.take(d, "53-check-part-way")
    d.step("runs")
    shots.take(d, "55b-past-runs-one-unfinished")
    d.click("Open")
    shots.take(d, "59-past-run-unfinished")
    d.step("copy")
    d.click("Continue copying")
    d.until(lambda: d.window.copy_phase() == "running")
    d.wait(2200)
    shots.take(d, "42-copy-continued")
    d.finish(120000)
    shots.take(d, "39-copy-finished")
    d.click("Check what arrived →")
    d.wait(450)
    shots.take(d, "47-check-counting")
    d.idle()
    shots.take(d, "48-check-everything-arrived")
    d.click("Save report")
    d.idle()
    d.wait(2800)
    shots.take(d, "49-check-report-saved")
    d.click("Copy more dates")
    d.idle()
    d.click("Last 3 months")
    d.click("Check these dates")
    d.idle()
    shots.take(d, "29-dates-nothing-to-copy")
    d.click("Last 12 months")
    d.click("Check these dates")
    d.idle()
    shots.take(d, "25b-dates-plan-part-already-there")
    d.resize(1024, 700)
    d.grab(shots.output / "66c-small-window-plan-part-there.png")
    d.resize(1280, 800)
    d.step("runs")
    shots.take(d, "55-past-runs")
    d.click("Open")
    shots.take(d, "57-past-run-everything-arrived")
    d.click("Settings")
    shots.take(d, "60-settings")
    edit = d.page.refresh_seconds
    edit.setFocus()
    edit.clear()
    from PySide6.QtTest import QTest

    QTest.keyClicks(edit, "99")
    shots.take(d, "61-settings-bad-number")
    d.page.debug.setFocus()
    d.wait(100)
    d.click("Dark")
    shots.take(d, "62-settings-dark")
    d.click("Light")
    d.click("Put all settings back to how they came")
    shots.take(d, "61b-settings-reset-question", sizes=False)
    d.click("Cancel")
    d.click("Move…")
    shots.take(d, "61c-settings-move-question", sizes=False)
    d.click("Leave it where it is")
    d.close()


def everything_walk(shots: Shots) -> None:
    """A plan for the whole history, with the oldest days about to be deleted."""
    d = Driver("plain", delay=0)
    d.sign_in()
    d.copy_rules()
    d.choose("Everything")
    shots.take(d, "25-dates-plan-everything-oldest-at-risk")
    d.page.start_date.setDate(d.page.start_date.date().addDays(-300))
    d.page.end_date.setDate(d.page.start_date.date().addDays(40))
    d.click("Check these dates")
    d.idle()
    shots.take(d, "31-dates-no-logs-in-dates")
    d.close()


def sign_in_trouble_walk(shots: Shots) -> None:
    """Job 1's wrong turns: a wrong password and an address that is not found."""
    d = Driver("plain", delay=0.2)
    d.sign_in(password="letmein", confirm=False)
    d.click("Cancel. Send nothing.")
    d.fill(destination_password="wrong")
    d.click("Test connections")
    d.until(lambda: d.window.modal.isVisible())
    d.click("These are my systems. Continue.")
    d.idle()
    shots.take(d, "06-connect-wrong-password")
    d.fill(destination_password="letmein")
    shots.take(d, "06b-connect-password-retyped")
    d.fill(source_host="logs-odl.example.com")
    d.click("Test again")
    d.idle()
    shots.take(d, "06c-connect-address-not-found")
    d.close()


def versions_walk(shots: Shots) -> None:
    """Job 2: the three ways two systems cannot migrate, and the 9.1 sign-in."""
    for scenario, name in (
        ("srcnewer", "07-connect-cannot-migrate-source-newer"),
        ("dstold", "08-connect-cannot-migrate-destination-old"),
        ("mixed", "09-connect-cannot-migrate-servers-disagree"),
        ("untested", "09b-connect-version-not-tried"),
    ):
        d = Driver(scenario, delay=0)
        d.sign_in()
        shots.take(d, name)
        if scenario == "srcnewer":
            d.click("Change sign-in details")
            shots.take(d, "07b-connect-change-sign-in-after-refusal")
        d.close()
    d = Driver("ops9", delay=0)
    d.sign_in()
    shots.take(d, "10-connect-destination-needs-other-sign-in")
    d.fill(ops_url="ops.example.com", ops_token="a-token")
    d.click("Test again")
    d.until(lambda: d.window.modal.isVisible() or not d.window.busy("connect"))
    if d.window.modal.isVisible():
        d.click("These are my systems. Continue.")
    d.idle()
    shots.take(d, "10b-connect-connected-with-token")
    d.close()


def rules_walk(shots: Shots) -> None:
    for scenario, name in (
        ("rules-none", "16-rules-nothing-to-copy"),
        ("rules-many", "15-rules-many"),
    ):
        d = Driver(scenario, delay=0)
        d.sign_in()
        d.click("Continue →")
        d.idle()
        shots.take(d, name)
        if scenario == "rules-many":
            d.resize(1024, 700)
            d.grab(shots.output / "66-small-window-rules-many.png")
            d.resize(1280, 800)
        d.close()
    d = Driver("rules-partial", delay=0)
    d.sign_in()
    d.click("Continue →")
    d.idle()
    d.click(d.buttons()[-1])
    d.click(d.buttons()[-1])
    d.idle()
    shots.take(d, "18-rules-one-not-copied")
    d.click("Try that rule again")
    d.idle()
    shots.take(d, "18b-rules-copied-on-retry")
    d.close()


def no_server_sign_in_walk(shots: Shots) -> None:
    """Job 6, step 6: what is lost without the destination's server sign-in, and the fix."""
    d = Driver("plain", speed=700, delay=0.2)
    d.sign_in(server=False)
    shots.take(d, "05c-connect-connected-no-server-sign-in")
    d.copy_rules()
    d.choose("Last 3 months")
    shots.take(d, "21b-dates-plan-ready-no-server-sign-in")
    d.start()
    d.progress(0.2)
    d.click("Stop the copy…")
    shots.take(d, "36b-copy-stop-question-no-server-sign-in", sizes=False)
    d.click("Stop anyway")
    d.finish()
    shots.take(d, "41b-copy-stopped-no-server-sign-in")
    d.click("Check what arrived so far")
    d.idle()
    d.click("Continue copying")
    d.idle()
    shots.take(d, "45-copy-cannot-continue")
    d.click("Add the destination server sign-in")
    shots.take(d, "12b-connect-add-server-sign-in")
    d.resize(1024, 700)
    d.grab(shots.output / "66b-small-window-connect.png")
    d.resize(1280, 800)
    d.fill(destination_ssh_user="root", destination_ssh_password="letmein")
    d.click("Test again")
    d.until(lambda: d.window.modal.isVisible() or not d.window.busy("connect"))
    d.idle()
    shots.take(d, "12d-connect-stopped-copy-can-continue")
    d.click("Back to the stopped copy →")
    d.idle()
    shots.take(d, "41c-copy-stopped-now-can-continue")
    d.click("Continue copying")
    d.finish(120000)
    shots.take(d, "39b-copy-finished-after-adding-sign-in")
    d.close()


def welcome_back_walk(shots: Shots) -> None:
    """Job 6, step 4: close the app with a copy unfinished, and come back to it."""
    with tempfile.TemporaryDirectory(prefix="logs-migrate-walk-") as folder:
        home = Path(folder)
        d = Driver("plain", speed=700, delay=0.2, home=home)
        d.sign_in()
        d.copy_rules()
        d.choose("Last 3 months")
        d.start()
        d.progress(0.3)
        d.click("Stop the copy…")
        d.click("Stop the copy")
        d.finish()
        d.close()
        d = Driver("plain", home=home)
        shots.take(d, "11-connect-welcome-back")
        d.fill(
            source_password="letmein",
            ssh_password="letmein",
            destination_password="letmein",
            destination_ssh_password="letmein",
        )
        d.click("Test connections")
        d.until(lambda: d.window.modal.isVisible() or not d.window.busy("connect"))
        d.idle()
        shots.take(d, "12-connect-welcome-back-connected")
        d.click("Go to the unfinished copy →")
        d.idle()
        shots.take(d, "41d-copy-stopped-after-reopening")
        d.close()
    d = Driver("plain", delay=0)
    d.step("runs")
    shots.take(d, "56-past-runs-empty")
    d.close()


def refusal_walk(shots: Shots) -> None:
    """Job 4: broken copies on the destination, and a plan that leaves those days out."""
    d = Driver("broken", speed=4000, delay=0.2)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 6 months")
    shots.take(d, "27-dates-refused-broken-copies")
    d.link("save-details")
    d.idle()
    shots.take(d, "27b-dates-refused-broken-copies-details-saved")
    d.click(d.buttons()[-1])
    d.idle()
    shots.take(d, "28-dates-plan-without-broken-days")
    d.start()
    d.wait(1500)
    shots.take(d, "35c-copy-running-in-parts")
    d.finish(180000)
    shots.take(d, "39c-copy-in-parts-finished")
    d.click("Check what arrived →")
    d.idle()
    shots.take(d, "48b-check-everything-arrived-in-parts")
    d.close()
    d = Driver("space", delay=0)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 12 months")
    shots.take(d, "30-dates-refused-no-room")
    d.close()
    d = Driver("listfail", delay=0.2)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 6 months")
    shots.take(d, "32-dates-source-server-not-answering")
    d.click("Try again")
    d.idle()
    shots.take(d, "32b-dates-plan-after-trying-again")
    d.close()
    d = Driver("plain", delay=0)
    d.sign_in()
    d.choose("Last 3 months")
    shots.take(d, "30b-dates-refused-needed-rule-missing")
    d.close()


def copy_trouble_walk(shots: Shots) -> None:
    """Jobs 5 and 6: quiet spells, lost contact, a failure and a copy that does not start."""
    d = Driver("stall", speed=120, delay=0)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 3 months")
    d.start()
    d.until(lambda: d.window.quiet_minutes() >= 6, 90000, "a quiet spell")
    d.wait(2100)
    shots.take(d, "37-copy-no-new-logs")
    d.step("check") if d.window.unlocked("check") else None
    d.close()

    d = Driver("offline", speed=500, delay=0)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 3 months")
    d.start()
    d.until(lambda: d.state.lost_at is not None, 90000, "contact to be lost")
    shots.take(d, "38-copy-lost-contact")
    d.step("dates")
    shots.take(d, "38b-header-out-of-touch")
    d.step("copy")
    d.click("Reconnect")
    d.until(lambda: d.state.lost_at is None, 60000, "the destination to answer again")
    d.idle()
    shots.take(d, "38c-copy-reconnected")
    d.close()

    d = Driver("fail", speed=1500, delay=0.2)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 3 months")
    d.start()
    d.finish()
    shots.take(d, "43-copy-failed")
    d.click("Continue copying")
    d.finish(120000)
    shots.take(d, "43b-copy-finished-after-failure")
    d.close()

    d = Driver("notstarted", delay=0.2)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 3 months")
    d.page.ack.click()
    d.click("Start copying →")
    d.idle()
    shots.take(d, "44-copy-did-not-start")
    d.click("Start again")
    d.until(lambda: d.window.copy_phase() == "running", what="second start")
    shots.take(d, "44b-copy-started-on-second-try")
    d.close()

    d = Driver("unsure", delay=0.2)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 3 months")
    d.page.ack.click()
    d.click("Start copying →")
    d.until(lambda: d.window.copy_phase() == "unsure", what="an unanswered start")
    d.wait(1200)
    shots.take(d, "46-copy-not-sure-it-started")
    d.close()


def result_trouble_walk(shots: Shots) -> None:
    """Job 7: days that need attention, copying what is missing, and a check that cannot finish."""
    d = Driver("missing", speed=6000, delay=0.2)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 6 months")
    d.start()
    d.finish()
    shots.take(d, "40-copy-finished-days-missing")
    d.click("Check what arrived anyway")
    d.idle()
    shots.take(d, "50-check-days-need-attention")
    d.link("all-days")
    shots.take(d, "51-check-all-days", sizes=False)
    d.click("Close")
    d.step("runs")
    d.click("Open")
    shots.take(d, "58-past-run-needs-attention")
    d.step("check")
    d.click("Copy what is missing")
    d.wait(600)
    shots.take(d, "40b-copy-what-was-missing-getting-ready")
    d.finish(60000)
    shots.take(d, "40c-copy-what-was-missing-finished")
    d.click("Check what arrived →")
    d.idle()
    shots.take(d, "50b-check-after-copying-what-was-missing")
    d.close()

    d = Driver("short", speed=6000, delay=0)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 6 months")
    d.start()
    d.finish()
    d.click("Check what arrived →")
    d.idle()
    shots.take(d, "52-check-one-day-short")
    d.close()

    d = Driver("unsteady", speed=6000, delay=0)
    d.sign_in()
    d.copy_rules()
    d.choose("Last 3 months")
    d.start()
    d.finish()
    d.click("Check what arrived →")
    d.idle(60000)
    shots.take(d, "54-check-could-not-finish")
    d.close()


WALKS = {
    "connect": connect_walk,
    "main": main_walk,
    "everything": everything_walk,
    "sign-in-trouble": sign_in_trouble_walk,
    "versions": versions_walk,
    "rules": rules_walk,
    "no-server-sign-in": no_server_sign_in_walk,
    "welcome-back": welcome_back_walk,
    "refusal": refusal_walk,
    "copy-trouble": copy_trouble_walk,
    "result-trouble": result_trouble_walk,
}


def capture(output: Path, only: str | None = None) -> Shots:
    shots = Shots(output)
    for name, walk in WALKS.items():
        if only is None or name == only:
            print(f"walk: {name}", flush=True)
            walk(shots)
    return shots


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", choices=sorted(WALKS))
    args = parser.parse_args(argv)
    shots = capture(args.output, args.only)
    for path in shots.saved:
        print(path)
    for problem in shots.overflows:
        print("DOES NOT FIT:", problem)
    return 1 if shots.overflows else 0


if __name__ == "__main__":
    sys.exit(main())
