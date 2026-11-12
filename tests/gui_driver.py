"""Drive the real window through the simulator, offscreen. Shared by tests and screenshots."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class Driver:
    """Runs the Qt event loop in short waits so each step observes the worker's result."""

    def __init__(
        self,
        fixture: Path,
        *,
        running_polls: int = 1,
        config: Path | None = None,
        knobs: dict | None = None,
    ):
        from PySide6.QtWidgets import QApplication

        from logs_migrate.gui.app import create

        self.directory = tempfile.TemporaryDirectory(prefix="logs-migrate-gui-")
        base = Path(self.directory.name)
        os.environ["XDG_CONFIG_HOME"] = str(base / "config")
        data = json.loads(fixture.read_text())
        if config is not None:
            extra = json.loads(config.read_text())
            for scope in ("source", "destination"):
                data[scope]["config"] = extra[scope].get("config", {})
                data[scope]["partitions"] = extra[scope]["partitions"]
        data["complete_after_polls"] = running_polls
        data["api_poll_seconds"] = 12
        data.update(knobs or {})  # Simulator knobs: skip_buckets, cancel_after, faults, ...
        self.fixture = base / "fixture.fixture"
        self.fixture.write_text(json.dumps(data))
        self.journal = base / "journal.sqlite"
        self.exports = base / "reports"
        self.app, self.window = create(fixture=str(self.fixture), journal=str(self.journal))
        self.window.state.settings.export_folder = str(self.exports)
        self.window.state.settings.timezone = "America/Chicago"
        self.window.refresh_all()
        self.window.resize(1440, 900)
        self.window.show()
        self.wait(50)
        self._qapp = QApplication

    @property
    def state(self):
        return self.window.state

    @property
    def pages(self):
        return self.window.pages

    def wait(self, milliseconds: int = 50) -> None:
        from PySide6.QtTest import QTest

        QTest.qWait(milliseconds)

    def until(self, predicate, timeout_ms: int = 10000) -> None:
        waited = 0
        while not predicate():
            self.wait(25)
            waited += 25
            if waited > timeout_ms:
                raise TimeoutError("GUI step did not complete")

    def idle(self, timeout_ms: int = 10000) -> None:
        self.until(lambda: not self.window.worker.pending, timeout_ms)
        self.wait(30)

    def show(self, name: str) -> None:
        self.window.show_page(name)
        self.wait(30)

    def connect(self) -> None:
        page = self.pages["connections"]
        page.source_host.setText("aria-logs.lab.example")
        page.destination_host.setText("vcf-logs.lab.example:9543")
        page.ssh_password.setText("fixture")
        page.source_password.setText("fixture")
        page.destination_password.setText("fixture")
        page.connect()
        self.until(lambda: self.state.connected or self.state.connect_error)
        self.idle()

    def plan(self, preset: str = "All history") -> None:
        self.show("plan")
        self.until(lambda: "discover" not in self.window.worker.pending)
        self.pages["plan"].apply_preset(preset)
        self.pages["plan"].preview()
        self.until(lambda: self.state.plan_run is not None or self.state.plan_error)
        self.idle()

    def acknowledge(self) -> None:
        self.pages["plan"].ack.setChecked(True)
        self.wait(20)

    def start(self) -> None:
        self.pages["plan"].start()
        self.until(lambda: self.state.status is not None or self.state.start_error)
        self.idle()

    def poll_until_finished(self, timeout_ms: int = 20000) -> None:
        def finished():
            return self.state.status is not None and self.state.status["status"] != "running"

        waited = 0
        while not finished():
            self.window.poll_status()
            self.idle()
            waited += 100
            if waited > timeout_ms:
                raise TimeoutError("task did not finish")

    def verify(self) -> None:
        self.show("verification")
        self.pages["verification"].verify()
        self.until(lambda: self.state.verification is not None or self.state.verify_error)
        self.idle()

    def cancel(self) -> bool:
        """Press the real Cancel button with the confirmation answered Yes."""
        from PySide6.QtWidgets import QMessageBox

        original = QMessageBox.question
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        try:
            self.pages["migration"].cancel_button.click()
        finally:
            QMessageBox.question = original
        self.idle()
        return self.state.status is not None and self.state.status["status"] == "cancelled"

    def compare(self) -> None:
        self.show("configuration")
        self.pages["configuration"].compare()
        self.until(lambda: self.state.comparison is not None or self.state.comparison_error)
        self.idle()

    def apply(self) -> None:
        self.pages["configuration"].apply()
        self.until(lambda: self.state.config_result is not None or self.state.comparison_error)
        self.idle()

    def export(self) -> None:
        self.show("reports")
        self.pages["reports"].export()
        self.until(lambda: self.state.exported or self.state.export_error)
        self.idle()

    def theme(self, name: str) -> None:
        settings = self.state.settings
        settings.theme = name
        self.window.apply_settings(settings)
        self.wait(50)

    def grab(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.wait(40)
        self.window.grab().save(str(path), "PNG", 0)  # 0 = smallest PNG, same pixels

    def close(self) -> None:
        self.window.close()
        self.window.deleteLater()
        from PySide6.QtCore import QCoreApplication, QEvent

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.wait(50)
        self.directory.cleanup()
