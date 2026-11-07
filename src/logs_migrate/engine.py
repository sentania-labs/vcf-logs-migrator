"""Thin migration API wrapper. The destination owns task state and progress."""

from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from uuid import uuid4

from .config import ConfigMigration
from .errors import Code, MigrationError, require, safe_boundary
from .models import ACTIVE_ACK, EVERYTHING_END, Counts, Policy, Verification, Window, digest
from .planning import build_plan, partition_diff
from .ports import SystemClock
from .verification import combine, reconcile, stable_counts

TERMINAL = {"idle", "completed", "cancelled", "failed"}
EVERYTHING = Window(0, EVERYTHING_END)


def _ids(keys):
    return {key.rsplit("/", 1)[-1] for key in keys}


class Engine:
    def __init__(self, environment, journal, clock=None, on_event=None):
        self.env = environment
        self.journal = journal
        self.clock = clock or SystemClock()
        self.on_event = on_event

    def config_compare(self):
        """Compare forwarding, partitions, masking and filters by name."""
        return ConfigMigration(self.env).compare()

    def config_apply(self, selection):
        """Apply explicit type:name selectors; return per-rule observations."""
        return ConfigMigration(self.env).apply(selection)

    def _audit(self, run, event, details=None):
        self.journal.save(run, event, details)
        if self.on_event:
            try:
                self.on_event({"run_id": run["id"], "kind": event, "details": details or {}})
            except Exception:
                pass  # Presentation observers cannot interrupt an authorized API operation.

    @contextmanager
    def _exclusive(self):
        with ExitStack() as stack:
            if self.journal is not None:
                stack.enter_context(self.journal.exclusive())
            stack.enter_context(self.env.exclusive())
            yield

    def _record(self, task, destination):
        """Ledger what the API reports as transferred once a task has finished."""
        if self.journal is None or task.status not in TERMINAL:
            return
        received = {*task.completed, *(k for n in task.nodes for k in n.completed)}
        for row in self.journal.transfers(destination):
            if not row["closed"]:
                matched = task.destination == destination and set(row["keys"]) == set(
                    task.bucket_keys
                )
                self.journal.settle_transfer(row["run"], sorted(received) if matched else [])

    def _settle(self, destination):
        task = self.env.transfer()
        self._record(task, destination)
        return task

    def _overlap(self, plan):
        destination = plan["destination"]["id"]
        selected = {b["id"] for b in plan["selected"]}
        for prior in self.journal.transfers(destination):
            held = prior["received"] if prior["closed"] else prior["keys"]
            require(not selected.intersection(_ids(held)), Code.OVERLAP)
        # Include transfers started outside this journal, wherever the appliance exposes them.
        for prior in self.env.history():
            if prior.destination == destination:
                require(not selected.intersection(prior.bucket_ids), Code.OVERLAP)

    @safe_boundary
    def discover(self):
        source, destination = self.env.discover()
        return {
            "source": asdict(source),
            "destination": asdict(destination),
            "partition_diff": partition_diff(source, destination),
        }

    @safe_boundary
    def plan(self, window, policy=None):
        with self._exclusive():
            source, destination = self.env.discover()
            plan = build_plan(source, destination, window)
            self._settle(destination.id)
            self._overlap(plan)
            run = {
                "schema_version": 2,
                "id": str(uuid4()),
                "created_at": self.clock.now(),
                "plan": plan,
                "plan_hash": digest(plan),
                "backup_ack": False,
                "backup_verified": False,
                "active_ack": False,
                "query_policy": asdict(policy or Policy()),
                "verification": {"outcome": Verification.PENDING.value, "complete": False},
            }
            self._audit(run, "plan_previewed")
            return run

    def _counts(self, policy, scope, window, keys):
        return stable_counts(self.env, self.clock, policy, scope, window, keys)

    def _ingest(self, policy, baseline):
        # Only arrivals beyond the destination's pre-transfer baseline count as new ingest.
        counts = self._counts(policy, "ingest", EVERYTHING, ())
        require(counts.identity_complete and baseline.identity_complete, Code.INCOMPLETE)
        identities = Counter(dict(counts.identities)) - Counter(dict(baseline.identities))
        daily = Counter()
        for identity, count in identities.items():
            daily[identity[:10]] += count
        return Counts(
            counts.complete,
            tuple(sorted(daily.items())),
            tuple(sorted(identities.items())),
            "ingest",
        )

    @safe_boundary
    def run(self, run_id, *, backup_ack=False, active_ack=""):
        # The CLI has no live Environment yet. Transport qualification remains separate.
        require(self.env.mode == "simulation", Code.UNSUPPORTED)
        with self._exclusive():
            run = self.journal.load(run_id)
            require(run.get("schema_version") == 2, Code.SCHEMA)
            require(backup_ack, Code.ACK)
            require(not run["plan"]["active_skipped"] or active_ack == ACTIVE_ACK, Code.ACK)
            require(
                all(
                    t["run"] != run_id
                    for t in self.journal.transfers(run["plan"]["destination"]["id"])
                ),
                Code.STATE,
            )
            source, destination = self.env.discover()
            fresh = build_plan(source, destination, Window(**run["plan"]["window"]))
            require(
                fresh["source"] == run["plan"]["source"]
                and fresh["destination"] == run["plan"]["destination"]
                and fresh["selected"] == run["plan"]["selected"],
                Code.INVENTORY_CHANGED,
            )
            task = self._settle(destination.id)
            self._overlap(fresh)
            require(fresh["capacity"]["fits"], Code.CAPACITY)
            difference = fresh["partition_diff"]
            require(not difference["missing"] and not difference["different"], Code.PARTITIONS)
            require(not difference["missing_fields"], Code.FIELDS)
            require(task.status in TERMINAL, Code.BUSY)
            for node in source.nodes:
                self.env.test_source_credentials(node.id)
            self.env.test_mover()
            policy = Policy(**run["query_policy"])
            coverage = Window(**run["plan"]["coverage"])
            run["source_by_bucket"] = {
                key: asdict(self._counts(policy, "source", coverage, (key,)))
                for key in (b["node"] + "/" + b["id"] for b in run["plan"]["selected"])
            }
            run["baseline"] = asdict(self._counts(policy, "baseline", EVERYTHING, ()))
            run["backup_ack"] = True
            run["active_ack"] = active_ack == ACTIVE_ACK
            self._audit(run, "transfer_authorized", {"snapshot_verified": False})
            # Protect against duplicate sends even if the POST response is lost. This is an
            # overlap record, not a locally maintained task state or a destination-wide lock.
            self.journal.record_transfer(run)
            self._audit(run, "api_start_requested")
            try:
                task = self.env.start(
                    Window(**fresh["window"]),
                    tuple(b["node"] + "/" + b["id"] for b in fresh["selected"]),
                )
            except Exception:
                self._audit(run, "api_start_response_unavailable")
                raise MigrationError(Code.UNCERTAIN) from None
            self._audit(run, "api_start_response", asdict(task))
            return {
                "run_id": run_id,
                "task": self._progress(task),
                "verification": run["verification"],
            }

    def _progress(self, task):
        result = asdict(task)
        elapsed = task.elapsed_seconds
        rate = task.bytes_transferred / elapsed if elapsed and elapsed > 0 else None
        result["rate_bytes_per_second"] = rate
        result["eta_seconds"] = (
            max(0, task.total_bytes - task.bytes_transferred) / rate
            if rate and task.total_bytes is not None
            else None
        )
        if task.status == "completed":
            result["eta_seconds"] = 0
        elif task.status != "running":
            result["eta_seconds"] = None
        result["observation"] = (
            "destination API; estimates derive from API counters and elapsed time"
        )
        return result

    @safe_boundary
    def status(self):
        with self._exclusive():
            task = self.env.transfer()
            self._record(task, task.destination)
            return self._progress(task)

    @safe_boundary
    def stop(self, task_id):
        """Explicit API cancellation, including tasks whose POST acknowledgment was lost."""
        require(self.env.mode == "simulation", Code.UNSUPPORTED)
        with self._exclusive():
            task = self.env.transfer()
            require(task.id == task_id, Code.UNCERTAIN)
            if task.status == "running":
                self.env.cancel()
                task = self.env.transfer()
            self._record(task, task.destination)
            return self._progress(task)

    @safe_boundary
    def verify(self, run_id):
        """Cumulative per destination: everything the API has reported as received so far."""
        with self._exclusive():
            run = self.journal.load(run_id)
            require("baseline" in run and "source_by_bucket" in run, Code.STATE)
            try:
                _, destination = self.env.discover()
                planned = run["plan"]["destination"]
                require(
                    destination.id == planned["id"] and destination.identity == planned["identity"],
                    Code.TOPOLOGY,
                )
                task = self._settle(destination.id)
                require(task.status in TERMINAL | {"running"}, Code.UNCERTAIN)
                require(task.status != "running", Code.BUSY)
                result = self._reconcile(run, destination.id)
            except MigrationError as error:
                result = {
                    "outcome": Verification.INCOMPLETE.value,
                    "complete": False,
                    "reason": error.code.value,
                }
            result["at"] = self.clock.now()
            run["verification"] = result
            self._audit(run, "daily_counts_verified", {"outcome": result["outcome"]})
            return result

    def _reconcile(self, run, destination):
        rows = self.journal.transfers(destination)
        received = {key for row in rows for key in row["received"]}
        require(bool(received), Code.INCOMPLETE)
        selected = {key for row in rows for key in row["keys"]}
        runs = {row["run"]: self.journal.load(row["run"]) for row in rows}
        source = combine(
            [
                Counts.from_dict(runs[row["run"]]["source_by_bucket"][key])
                for row in rows
                for key in row["received"]
            ],
            "source",
        )
        baseline = Counts.from_dict(runs[rows[0]["run"]]["baseline"])
        policy = Policy(**run["query_policy"])
        actual = self._counts(policy, "destination", EVERYTHING, ())
        ingest = self._ingest(policy, baseline)
        require(
            actual == self._counts(policy, "destination", EVERYTHING, ())
            and ingest == self._ingest(policy, baseline),
            Code.INCOMPLETE,
        )
        result = reconcile(source, baseline, actual, ingest)
        result["buckets"] = {
            "covered": sorted(received),
            "not_received": sorted(selected - received),
        }
        result["covered_days"] = [day for day, _ in source.daily]
        if result["buckets"]["not_received"]:
            # Received groups reconcile, but the selection did not all land: never a success.
            result["outcome"] = Verification.INCOMPLETE.value
            result["reason"] = Code.NOT_RECEIVED.value
        return result
