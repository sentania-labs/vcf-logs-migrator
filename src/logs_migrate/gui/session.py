"""One engine session owned by the worker thread. Thin calls; the engine decides everything."""

from __future__ import annotations

import json
from pathlib import Path

from ..adapters.simulated import SimulatedClock, SimulatedEnvironment
from ..engine import Engine
from ..errors import Code, MigrationError, require
from ..journal import Journal
from ..models import ACTIVE_ACK, Policy, Window
from ..planning import compatible
from ..reporting import markdown, report_data
from ..security import Credentials


class Session:
    """Holds the environment, engine, journal and in-memory credentials for one connection."""

    def __init__(self):
        self.env = None
        self.engine = None
        self.journal = None
        self.credentials = None

    def connect(self, *, fixture: str, journal: str, credentials: dict[str, str]) -> dict:
        """Open the environment and read both inventories. Live appliances are not qualified."""
        self.disconnect()
        require(bool(fixture), Code.UNSUPPORTED)
        fixture_path, journal_path = Path(fixture), Path(journal)
        try:
            env = SimulatedEnvironment.from_file(
                fixture_path, journal_path.with_suffix(".simulated-state")
            )
        except MigrationError:
            raise
        except Exception:
            raise MigrationError(Code.SCHEMA) from None
        self.journal = Journal(journal_path)
        self.env = env
        self.engine = Engine(env, self.journal, SimulatedClock())
        self.credentials = Credentials(
            {key: value for key, value in credentials.items() if value}, interactive=False
        )
        return self.discover()

    def reconnect(self, *, fixture: str, journal: str) -> dict:
        """Re-open the same environment after a lost connection; credentials stay in memory."""
        credentials = self.credentials.snapshot() if self.credentials is not None else {}
        return self.connect(fixture=fixture, journal=journal, credentials=credentials)

    def disconnect(self) -> None:
        if self.journal is not None:
            self.journal.close()
        if self.credentials is not None:
            self.credentials.clear()
        self.env = self.engine = self.journal = self.credentials = None

    def _engine(self) -> Engine:
        require(self.engine is not None, Code.STATE)
        return self.engine

    def discover(self) -> dict:
        result = self._engine().discover()
        try:
            from ..models import Cluster

            compatible(
                Cluster.from_dict(result["source"]), Cluster.from_dict(result["destination"])
            )
            result["compatible"] = None
        except MigrationError as error:
            result["compatible"] = error.code.value
        return result

    def plan(self, *, start: int, end: int, attempts: int, poll_seconds: float) -> dict:
        return self._engine().plan(Window(start, end), Policy(attempts, poll_seconds))

    def start(self, *, run_id: str, acknowledged: bool) -> dict:
        return self._engine().run(
            run_id, backup_ack=True, active_ack=ACTIVE_ACK if acknowledged else ""
        )

    def status(self) -> dict:
        return self._engine().status()

    def stop(self, *, task_id: str) -> dict:
        return self._engine().stop(task_id)

    def verify(self, *, run_id: str) -> dict:
        return self._engine().verify(run_id)

    def runs(self) -> list[dict]:
        require(self.journal is not None, Code.STATE)
        return self.journal.runs()

    def report(self, *, run_id: str) -> dict:
        require(self.journal is not None, Code.STATE)
        run, events = self.journal.load(run_id), self.journal.events(run_id)
        return {"run": run, "events": events}

    def export(self, *, run_id: str, folder: str, formats: list[str]) -> list[str]:
        require(self.journal is not None, Code.STATE)
        require(bool(formats), Code.INVALID_INPUT)
        run, events = self.journal.load(run_id), self.journal.events(run_id)
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        written = []
        stem = f"migration-{run_id[:8]}"
        if "markdown" in formats:
            path = target / (stem + ".md")
            path.write_text(markdown(run, events), encoding="utf-8")
            written.append(str(path))
        if "json" in formats:
            path = target / (stem + ".json")
            path.write_text(
                json.dumps(report_data(run, events), sort_keys=True, indent=2), encoding="utf-8"
            )
            written.append(str(path))
        return written

    def config_compare(self) -> dict:
        """The engine's comparison plus the rule bodies it compared, for display only."""
        comparison = self._engine().config_compare()
        details: dict[str, dict] = {}
        reader = getattr(self.env, "config_rules", None)
        if reader is not None:
            kinds = {row["type"] for row in comparison["rules"]}
            inventory = {}
            for kind in kinds:
                try:
                    inventory[kind] = (
                        {r["name"]: r for r in reader("source", kind)},
                        {r["name"]: r for r in reader("destination", kind)},
                    )
                except Exception:
                    inventory[kind] = ({}, {})
            for row in comparison["rules"]:
                source, destination = inventory.get(row["type"], ({}, {}))
                details[row["selector"]] = {
                    "source": source.get(row["name"]),
                    "destination": destination.get(row["name"]),
                }
        comparison["details"] = details
        return comparison

    def config_apply(self, *, selection: list[str]) -> dict:
        return self._engine().config_apply(list(selection))
