"""One engine session owned by the worker thread. Thin calls; the engine decides everything."""

from __future__ import annotations

import json
from pathlib import Path

from ..adapters.simulated import SimulatedClock, SimulatedEnvironment
from ..adapters.ssh_errors import SSHFailure
from ..engine import Engine
from ..errors import Code, MigrationError, require
from ..journal import Journal
from ..models import ACTIVE_ACK, Policy, Window
from ..planning import compatible
from ..ports import Transfer
from ..reporting import report_data
from ..security import Credentials
from ..trust import TrustRequired, TrustStore
from . import viewmodel as vm


class LocalError(Exception):
    """A local problem (such as an unwritable folder) worded for the operator."""


class Session:
    """Holds the environment, engine, journal and in-memory credentials for one connection."""

    def __init__(self):
        self.env = None
        self.on_progress = None
        self.engine = None
        self.journal = None
        self.credentials = None
        self.connection: dict[str, str] = {}
        self.pending_trust: dict[str, tuple[object, str | None]] = {}
        self.store = None

    @property
    def mode(self) -> str:
        return getattr(self.env, "mode", "simulation") if self.env is not None else "simulation"

    def connect(
        self,
        *,
        fixture: str,
        journal: str,
        credentials: dict[str, str],
        connection: dict[str, str] | None = None,
    ) -> dict:
        """Open the simulator (fixture given) or the real appliances (addresses given).

        Unknown appliance identities raise TrustRequired before anything is sent; the window
        shows them and calls `trust`, then connects again.
        """
        self.disconnect()
        journal_path = Path(journal).expanduser()
        self.credentials = Credentials(
            {key: value for key, value in credentials.items() if value}, interactive=False
        )
        self.connection = dict(connection or {})
        if fixture:
            fixture_path = Path(fixture).expanduser()
            try:
                env = SimulatedEnvironment.from_file(
                    fixture_path, journal_path.with_suffix(".simulated-state")
                )
            except MigrationError:
                raise
            except Exception:
                raise MigrationError(Code.SCHEMA) from None
            clock = SimulatedClock()
        else:
            env = self._open_live()
            clock = None
        self.journal = Journal(journal_path)
        self.env = env
        self.engine = Engine(env, self.journal, clock)
        result = self.discover()
        result["mode"] = self.mode
        result["checks"] = self._checks(result)
        return result

    def probe_endpoints(self, *, connection: dict[str, str]) -> None:
        self.disconnect()
        self.connection = dict(connection)
        self.credentials = Credentials({}, interactive=False)
        self._open_live(probe_only=True)

    def _open_live(self, *, probe_only=False):
        from ..adapters.live import Connection, LiveEnvironment

        values = self.connection
        require(
            bool(values.get("source_host")) and bool(values.get("destination")), Code.INVALID_INPUT
        )
        connection = Connection(
            source_host=values["source_host"].strip(),
            destination=values["destination"].strip(),
            source_api_user=values.get("source_api_user") or "admin",
            source_ssh_user=values.get("source_ssh_user") or "root",
            destination_api_user=values.get("destination_api_user") or "admin",
            destination_ssh_user=values.get("destination_ssh_user") or "",
            ops_url=values.get("ops_url") or "",
        )
        self.store = self.store or TrustStore()
        env = LiveEnvironment(connection, self.credentials, self.store)
        env.on_progress = self.on_progress
        try:
            if probe_only:
                env.probe_endpoints()
                return env
            return env.open()
        except TrustRequired as pending:
            pending.existing = [
                {"kind": kind, "name": name, "fingerprint": fingerprint}
                for kind, name, fingerprint in self.store.entries()
            ]
            self.pending_trust = {
                identity.name: (identity, env.key_line(identity)) for identity in pending.identities
            }
            raise
        except MigrationError as error:
            stage = env.notes.get("stage")
            if stage:
                message = (
                    error.message
                    if isinstance(error, SSHFailure)
                    else vm.describe_error(error.code.value)
                )
                raise LocalError(f"{message} While contacting {stage}.") from None
            raise

    def trust(self, *, identities: list[dict]) -> list[str]:
        """Enroll the identities the operator accepted in the dialog, by the fingerprint
        the dialog showed; a pending entry with another fingerprint is not enrolled."""
        require(bool(self.pending_trust), Code.STATE)
        self.store = self.store or TrustStore()
        enrolled = []
        for shown in identities:
            pending = self.pending_trust.get(shown["name"])
            if pending is None or pending[0].fingerprint != shown["fingerprint"]:
                continue
            identity, line = self.pending_trust.pop(shown["name"])
            self.store.trust(identity, line)
            enrolled.append(shown["name"])
        return enrolled

    def _checks(self, discovered: dict) -> list[tuple[str, str]]:
        if self.mode != "live":
            return []
        env = self.env
        notes = getattr(env, "notes", {})
        source, destination = discovered["source"], discovered["destination"]
        rows = [
            ("Source Logs API", f"Passed · {vm.format_version(source['version'])}"),
            (
                "Source cluster members (SSH)",
                f"Passed · {len(source['nodes'])} · " + ", ".join(n["id"] for n in source["nodes"]),
            ),
            (
                "Destination Logs API",
                f"Passed · {vm.format_version(destination['version'])}"
                + (" (version assumed)" if notes.get("destination_version_assumed") else ""),
            ),
        ]
        try:
            task = env.transfer()
            rows.append(("Destination data-migrations status", f"Passed · task {task.status}"))
        except MigrationError as error:
            rows.append(("Destination data-migrations status", "Failed · " + error.code.value))
        budget = [
            n["id"] for n in destination["nodes"] if n.get("repository_available") is not None
        ]
        rows.append(
            (
                "Destination repository budget",
                f"Observed on {len(budget)} node(s)"
                if budget
                else "Not observed (no destination SSH)",
            )
        )
        return rows

    def selftest(self) -> dict:
        from .. import selftest

        require(self.env is not None and self.mode == "live", Code.STATE)
        return selftest.run(self.env).data()

    def reconnect(self, *, fixture: str, journal: str) -> dict:
        """Re-open the same environment after a lost connection; credentials stay in memory."""
        credentials = self.credentials.snapshot() if self.credentials is not None else {}
        return self.connect(
            fixture=fixture, journal=journal, credentials=credentials, connection=self.connection
        )

    def disconnect(self) -> None:
        if self.journal is not None:
            self.journal.close()
        if self.env is not None:
            for name in ("source_client", "destination_client"):
                client = getattr(self.env, name, None)
                if client is not None:
                    client.clear_token()
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
        before = len(self.journal.events(run_id))
        try:
            return self._engine().run(
                run_id, backup_ack=True, active_ack=ACTIVE_ACK if acknowledged else ""
            )
        except MigrationError as error:
            events = self.journal.events(run_id)[before:]
            kinds = {event["kind"] for event in events}
            if "api_start_requested" not in kinds or "api_start_rejected" in kinds:
                reason = getattr(error, "message", None) or vm.describe_error(error.code.value)
                raise LocalError("pre_start:" + reason) from None
            raise

    def status(self, *, run_id: str | None = None) -> dict:
        result = (
            self._engine().status(run_id)
            if run_id
            else self._engine()._progress(Transfer("", "idle"))
        )
        result["run_id"] = run_id
        return result

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

    def export(
        self,
        *,
        run_id: str,
        folder: str,
        formats: list[str],
        zone: str = "UTC",
        hosts: tuple[str, str] | None = None,
        index: int | None = None,
        config_result: dict | None = None,
    ) -> list[str]:
        """Markdown in the pages' own words; JSON stays the engine's machine record."""
        require(self.journal is not None, Code.STATE)
        require(bool(formats), Code.INVALID_INPUT)
        run, events = self.journal.load(run_id), self.journal.events(run_id)
        target = Path(folder).expanduser()
        written = []
        try:
            target.mkdir(parents=True, exist_ok=True)
            stem = f"migration-{run_id[:8]}"
            if "markdown" in formats:
                status = None
                try:
                    status = self._engine().status(run_id)
                except Exception:
                    status = None  # The report still renders; the task row says Not observed.
                path = target / (stem + ".md")
                path.write_text(
                    vm.report_markdown(run, events, status, zone, hosts, index, config_result),
                    encoding="utf-8",
                )
                written.append(str(path))
            if "json" in formats:
                path = target / (stem + ".json")
                path.write_text(
                    json.dumps(report_data(run, events), sort_keys=True, indent=2), encoding="utf-8"
                )
                written.append(str(path))
        except OSError as error:
            raise LocalError(f"Could not write to {target}: {error.strerror or error}") from error
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
