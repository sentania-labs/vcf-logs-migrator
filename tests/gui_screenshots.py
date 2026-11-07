"""Render every workspace state against the simulator and save PNGs for review.

QT_QPA_PLATFORM=offscreen python tests/gui_screenshots.py --output /tmp/gui-shots
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_driver import FIXTURES, Driver  # noqa: E402


def capture(output: Path) -> list[Path]:
    shots: list[Path] = []
    config = FIXTURES / "config.fixture"
    if config.exists():
        # The config fixture carries rules but no history; its own session shows compare/apply.
        rules = Driver(config)

        def rule_shot(name: str) -> None:
            path = output / f"{name}.png"
            rules.grab(path)
            shots.append(path)

        rules.connect()
        rules.compare()
        rule_shot("80-config-compare")
        rules.apply()
        rule_shot("83-config-results")
        rules.theme("dark")
        rules.show("configuration")
        rule_shot("83b-config-results-dark")
        rules.close()
    driver = Driver(FIXTURES / "sealed-populated.fixture", running_polls=3)

    def shot(name: str) -> None:
        path = output / f"{name}.png"
        driver.grab(path)
        shots.append(path)

    driver.show("overview")
    shot("01-overview-empty")
    driver.show("connections")
    shot("04-connections-before")
    driver.connect()
    driver.show("connections")
    shot("04-connections")
    driver.show("overview")
    shot("02-overview-light")
    driver.plan()
    shot("07-plan-light")
    driver.window.resize(1024, 700)
    shot("23-plan-small")
    driver.window.resize(1440, 900)
    driver.acknowledge()
    shot("27-plan-ready")
    driver.start()
    driver.show("migration")
    shot("11-migration-light")
    driver.state.status_error = "connection_failed"
    driver.window.refresh_all()
    shot("13-status-unavailable")
    driver.state.status_error = None
    driver.poll_until_finished()
    driver.show("migration")
    shot("11b-migration-completed")
    driver.verify()
    shot("15-verified-light")
    driver.export()
    shot("20-reports")
    driver.plan()
    shot("47-overlap-blocked")
    driver.show("settings")
    shot("21-settings")
    driver.theme("dark")
    driver.show("overview")
    shot("03-overview-dark")
    driver.show("plan")
    shot("08-plan-dark")
    driver.show("migration")
    shot("12-migration-dark")
    driver.show("verification")
    shot("16-verified-dark")
    driver.window.resize(1024, 700)
    driver.show("plan")
    shot("64-plan-small-dark")
    driver.close()
    return shots


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    for path in capture(args.output):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
