"""Thin migration API wrapper. The destination owns task state and progress."""

from collections import Counter
from dataclasses import asdict
from uuid import uuid4

from .errors import Code, MigrationError, require, safe_boundary
from .models import ACTIVE_ACK, Counts, Policy, Verification, Window, digest
from .planning import build_plan, partition_diff
from .ports import SystemClock
from .verification import reconcile, stable_counts


class Engine:
    def __init__(self, environment, journal, clock=None, on_event=None):
        self.env = environment
        self.journal = journal
        self.clock = clock or SystemClock()
        self.on_event = on_event

    def _audit(self, run, event, details=None):
        self.journal.save(run, event, details)
        if self.on_event:
            try:
                self.on_event({"run_id": run["id"], "kind": event, "details": details or {}})
            except Exception:
                pass  # Presentation observers cannot interrupt an authorized API operation.

    def _overlap(self, plan):
        destination = plan["destination"]["id"]
        window = Window(**plan["window"])
        coverage = Window(**plan["coverage"])
        selected = {b["id"] for b in plan["selected"]}
        for prior in self.journal.transfers(destination):
            require(not window.overlaps(Window(prior["start"], prior["end"])), Code.OVERLAP)
            require(not selected.intersection(prior["bucket_ids"]), Code.OVERLAP)
            # Whole-bucket coverage is what verify counts; an unverified earlier run's totals
            # would absorb the new transfer's events.
            if coverage.overlaps(Window(**prior["coverage"])):
                outcome = self.journal.load(prior["run"])["verification"]["outcome"]
                require(outcome == Verification.SUCCESS, Code.VERIFICATION_PENDING)
        # Include transfers started outside this journal, wherever the appliance exposes them.
        for prior in self.env.history():
            if prior.destination == destination:
                require(not window.overlaps(prior.window), Code.OVERLAP)
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
        with self.journal.exclusive(), self.env.exclusive():
            source, destination = self.env.discover()
            plan = build_plan(source, destination, window)
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

    def _counts(self, run, scope):
        plan = run["plan"]
        counts = stable_counts(
            self.env,
            self.clock,
            Policy(**run["query_policy"]),
            scope,
            Window(**plan["coverage"]),
            tuple(b["node"] + "/" + b["id"] for b in plan["selected"]),
        )
        if scope != "ingest":
            return counts
        # Only arrivals beyond THIS run's baseline count as new ingest. The destination's
        # current task can already refer to another window; it never selects our baseline.
        require(counts.identity_complete, Code.INCOMPLETE)
        baseline = Counts.from_dict(run["baseline"])
        require(baseline.identity_complete, Code.INCOMPLETE)
        identities = Counter(dict(counts.identities)) - Counter(dict(baseline.identities))
        daily = Counter()
        for identity, count in identities.items():
            daily[identity[:10]] += count
        return Counts(
            counts.complete, tuple(sorted(daily.items())), tuple(sorted(identities.items())), scope
        )

    @safe_boundary
    def run(self, run_id, *, backup_ack=False, active_ack=""):
        # The CLI has no live Environment yet. Transport qualification remains separate.
        require(self.env.mode == "simulation", Code.UNSUPPORTED)
        with self.journal.exclusive(), self.env.exclusive():
            run = self.journal.load(run_id)
            require(run.get("schema_version") == 2, Code.SCHEMA)
            require(backup_ack, Code.ACK)
            require(not run["plan"]["active_skipped"] or active_ack == ACTIVE_ACK, Code.ACK)
            source, destination = self.env.discover()
            fresh = build_plan(source, destination, Window(**run["plan"]["window"]))
            require(
                fresh["source"] == run["plan"]["source"]
                and fresh["destination"] == run["plan"]["destination"]
                and fresh["selected"] == run["plan"]["selected"],
                Code.INVENTORY_CHANGED,
            )
            self._overlap(fresh)
            require(fresh["capacity"]["fits"], Code.CAPACITY)
            difference = fresh["partition_diff"]
            require(not difference["missing"] and not difference["different"], Code.PARTITIONS)
            require(not difference["missing_fields"], Code.FIELDS)
            require(
                self.env.transfer().status in {"idle", "completed", "cancelled", "failed"},
                Code.BUSY,
            )
            for node in source.nodes:
                self.env.test_source_credentials(node.id)
            self.env.test_mover()
            run["source_counts"] = asdict(self._counts(run, "source"))
            run["baseline"] = asdict(self._counts(run, "baseline"))
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
        with self.env.exclusive():
            return self._progress(self.env.transfer())

    @safe_boundary
    def stop(self, task_id):
        """Explicit API cancellation, including tasks whose POST acknowledgment was lost."""
        require(self.env.mode == "simulation", Code.UNSUPPORTED)
        with self.env.exclusive():
            task = self.env.transfer()
            require(task.id == task_id, Code.UNCERTAIN)
            if task.status == "running":
                self.env.cancel()
            return self._progress(self.env.transfer())

    @safe_boundary
    def verify(self, run_id):
        with self.journal.exclusive(), self.env.exclusive():
            run = self.journal.load(run_id)
            require("baseline" in run and "source_counts" in run, Code.STATE)
            try:
                _, destination = self.env.discover()
                planned = run["plan"]["destination"]
                require(
                    destination.id == planned["id"] and destination.identity == planned["identity"],
                    Code.TOPOLOGY,
                )
                task = self.env.transfer()
                window = Window(**run["plan"]["coverage"])
                # An overlapping active or unknown task can still change these counts.
                require(
                    task.status in {"idle", "running", "completed", "failed", "cancelled"},
                    Code.UNCERTAIN,
                )
                if task.status == "running":
                    require(task.window is not None and not task.window.overlaps(window), Code.BUSY)
                actual = self._counts(run, "destination")
                ingest = self._counts(run, "ingest")
                require(
                    actual == self._counts(run, "destination")
                    and ingest == self._counts(run, "ingest"),
                    Code.INCOMPLETE,
                )
                result = reconcile(
                    Counts.from_dict(run["source_counts"]),
                    Counts.from_dict(run["baseline"]),
                    actual,
                    ingest,
                )
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
