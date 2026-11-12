"""UI-thread snapshot of the latest engine observations. Screens render from this only."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import copies
from .settings import Settings


@dataclass
class AppState:
    settings: Settings
    # Connect
    connected: bool = False
    discovered: dict | None = None  # Both systems as last tested, including a refusal.
    failure: dict | None = None  # A test that stopped at one box.
    token_notice: bool = False  # The destination turned out to need an access token.
    server_needed: bool = False  # Sent here to add the destination server sign-in.
    resume: dict | None = None  # {"kind": "unfinished" | "missing" | "back", "copy": id}
    identities: list[dict] = field(default_factory=list)
    # Copy rules
    comparison: dict | None = None
    comparison_error: str | None = None
    config_result: dict | None = None
    rules_seen: bool = False
    # Choose dates
    inventory: dict | None = None
    inventory_error: str | None = None
    plan: dict | None = None  # {"parts": [runs], "excluded": [...]}, not started.
    plan_error: str | None = None
    plan_refusal: dict | None = None
    excluded: list[dict] = field(default_factory=list)
    acknowledged: bool = False
    details_saved: str | None = None
    # Copy logs
    record: dict | None = None
    copies: list[dict] = field(default_factory=list)
    copy_id: str | None = None
    run_id: str | None = None
    status: dict | None = None
    status_error: str | None = None
    status_at: str | None = None
    lost_at: str | None = None
    start_failure: dict | None = None
    continue_failure: dict | None = None
    unsure: bool = False
    continued_note: str = ""
    latest: dict | None = None
    arrived_seen: set = field(default_factory=set)
    quiet_since: float | None = None
    bytes_seen: int | None = None
    # Check result
    verify_error: str | None = None
    saved: dict = field(default_factory=dict)  # copy id to the files written for it.
    save_error: str | None = None
    # Past runs
    open_run: str | None = None
    record_error: str | None = None
    trouble_saved: str | None = None

    @property
    def live(self) -> bool:
        return bool(self.discovered) and self.discovered.get("mode") == "live"

    @property
    def server_sign_in(self) -> bool:
        return bool(self.discovered) and bool(self.discovered.get("server_sign_in"))

    @property
    def refused(self) -> bool:
        return bool(self.discovered) and not self.discovered["passed"]

    def copy(self, copy_id: str | None = None) -> dict | None:
        wanted = copy_id or self.copy_id
        return next((c for c in self.copies if c["id"] == wanted), None)

    def set_record(self, record: dict) -> None:
        self.record = record
        self.copies = copies.build(record["runs"], record["rows"])

    def clear_copy(self) -> None:
        self.copy_id = self.run_id = None
        self.status = self.status_error = self.status_at = self.lost_at = None
        self.start_failure = self.continue_failure = None
        self.unsure = False
        self.continued_note = ""
        self.latest = None
        self.arrived_seen = set()
        self.quiet_since = self.bytes_seen = None
        self.verify_error = None

    def clear_plan(self) -> None:
        self.plan = self.plan_error = self.plan_refusal = None
        self.excluded = []
        self.acknowledged = False
        self.details_saved = None

    def reset_connection(self) -> None:
        """Signed out, or about to test again: nothing learned from the systems is kept."""
        self.connected = False
        self.discovered = None
        self.failure = None
        self.token_notice = False
        self.identities = []
        self.comparison = self.comparison_error = self.config_result = None
        self.rules_seen = False
        self.inventory = self.inventory_error = None
        self.clear_plan()
        self.clear_copy()
