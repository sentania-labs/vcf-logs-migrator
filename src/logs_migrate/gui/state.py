"""UI-thread snapshot of the latest engine observations. Pages render from this only."""

from __future__ import annotations

from dataclasses import dataclass, field

from .settings import Settings


@dataclass
class AppState:
    settings: Settings
    connected: bool = False
    discovered: dict | None = None
    connect_error: str | None = None
    comparison: dict | None = None
    comparison_error: str | None = None
    config_result: dict | None = None
    plan_run: dict | None = None
    plan_error: str | None = None
    acknowledged: bool = False
    run_id: str | None = None
    status: dict | None = None
    status_error: str | None = None
    status_at: str | None = None
    start_error: str | None = None
    started_run_id: str | None = None
    verification: dict | None = None
    verify_error: str | None = None
    report: dict | None = None
    runs: list[dict] = field(default_factory=list)
    exported: list[str] = field(default_factory=list)
    export_error: str | None = None

    @property
    def preview(self) -> bool:
        return bool(self.discovered) and self.discovered["destination"]["version"].startswith(
            "9.1.1"
        )

    @property
    def stale(self) -> bool:
        return self.status is not None and self.status_error is not None

    @property
    def active_count(self) -> int:
        if self.plan_run:
            return len(self.plan_run["plan"]["active_skipped"])
        if self.report:
            return len(self.report["run"]["plan"]["active_skipped"])
        return 0

    def reset_connection(self) -> None:
        self.connected = False
        self.discovered = None
        self.comparison = None
        self.comparison_error = None
        self.config_result = None
        self.plan_run = None
        self.plan_error = None
        self.acknowledged = False
        self.run_id = None
        self.status = None
        self.status_error = None
        self.status_at = None
        self.start_error = None
        self.started_run_id = None
        self.verification = None
        self.verify_error = None
        self.report = None
        self.runs = []
        self.exported = []
        self.export_error = None
