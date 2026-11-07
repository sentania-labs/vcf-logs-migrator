"""Thin migration API wrapper. The destination owns task state and progress."""

from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

from .config import ConfigMigration
from .errors import Code, MigrationError, Rejection, require, safe_boundary
from .models import (
    ACTIVE_ACK,
    DAY,
    EVERYTHING_END,
    Counts,
    Policy,
    Verification,
    Window,
    digest,
)
from .planning import build_plan, partition_diff
from .ports import SystemClock
from .verification import combine, reconcile, stable_counts

TERMINAL = {"idle", "completed", "cancelled", "failed"}
EVERYTHING = Window(0, EVERYTHING_END)
INGEST_WINDOW_SECONDS = 86_400


def _ids(keys):
    return {key.rsplit("/", 1)[-1] for key in keys}


def day_window(window: Window) -> Window:
    """The whole UTC days a window touches; count queries are compared per day."""
    return Window(window.start // DAY * DAY, ((window.end - 1) // DAY + 1) * DAY)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()


def partial_days(source, plan: dict) -> list[str]:
    """Covered days whose source events are not all in the selected buckets: days shared
    with an unselected sealed bucket, and days at or after the active tail. The tail starts
    at the first active bucket's proven start; when active bounds are unknown (the 8.18
    listing shows none) it starts at the newest sealed bucket's end, the sealed frontier.
    Counts on partial days are reported, never judged; a day before the frontier that an
    open bucket secretly holds late arrivals for shows up as a discrepancy, not a pass."""
    coverage = day_window(Window(**plan["coverage"]))
    selected = {b["node"] + "/" + b["id"] for b in plan["selected"]}
    days = []
    cursor = coverage.start
    while cursor < coverage.end:
        days.append(cursor)
        cursor += DAY
    partial = set()
    unproven_active = False
    for bucket in source.buckets:
        if bucket.key in selected:
            continue
        if bucket.status == "active" and not (bucket.bounds_proven and bucket.start is not None):
            unproven_active = True
            continue
        if bucket.start is None or bucket.end is None:
            unproven_active = True
            continue
        for start in days:
            if bucket.start < start + DAY and bucket.end >= start:
                partial.add(start)
    if unproven_active:
        last_sealed = plan.get("last_fully_sealed_day")
        if last_sealed is not None:
            partial.update(start for start in days if _day(start) > last_sealed)
        else:
            sealed_ends = [
                b.end for b in source.buckets if b.status != "active" and b.end is not None
            ]
            if not sealed_ends:
                partial.update(days)
            else:
                frontier = max(sealed_ends) // DAY * DAY
                partial.update(start for start in days if start >= frontier)
    return sorted(_day(start) for start in partial)


class Engine:
    def __init__(self, environment, journal, clock=None, on_event=None):
        self.env = environment
        self.journal = journal
        self.clock = clock or SystemClock()
        self.on_event = on_event

    @property
    def attribution(self) -> bool:
        return bool(getattr(self.env, "attribution", True))

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
        if task.status == "idle" and not self.attribution:
            # A live API that shows no task moved nothing: a selection whose start never
            # produced a task id is released; one that was acknowledged stays recorded.
            for row in self.journal.transfers(destination):
                if not row["closed"] and not row.get("task_id"):
                    self.journal.release_transfer(row["run"])
            return
        received = {*task.completed, *(k for n in task.nodes for k in n.completed)}
        received_ids = _ids(received)
        skipped_ids = _ids({*task.skipped, *(k for n in task.nodes for k in n.skipped)})
        every = self.journal.transfers(destination)
        if task.id and task.id in {r["task_id"] for r in every if r["closed"] and r.get("task_id")}:
            return
        rows = [row for row in every if not row["closed"]]
        for row in rows:
            if task.destination != destination:
                matched = False
            elif task.bucket_keys:
                matched = _ids(row["keys"]) == _ids(task.bucket_keys)
            elif task.id and row.get("task_id"):
                matched = task.id == row["task_id"]
            elif task.window is not None and row.get("window"):
                # Compared by day: a server may echo the window on day boundaries.
                matched = day_window(Window(**row["window"])) == day_window(task.window)
            else:
                # The API reported neither the requested buckets nor the window; the only
                # open selection for this destination is the one it answered for.
                matched = len(rows) == 1
            if matched and task.id and not row.get("task_id"):
                self.journal.note_transfer_task(row["run"], task.id)
            if not matched:
                if self.attribution:
                    self.journal.settle_transfer(row["run"], [])
                elif not row.get("task_id"):
                    # Live: a finished task that is not this selection's, and the start was
                    # never acknowledged with an id: the appliance holds no task for it.
                    self.journal.release_transfer(row["run"])
            elif received or self.attribution:
                self.journal.settle_transfer(
                    row["run"],
                    sorted(k for k in row["keys"] if k.rsplit("/", 1)[-1] in received_ids),
                )
            else:
                # No per-bucket detail: by the API's own account a completed task moved
                # everything it did not list as skipped; a cancelled or failed one may have
                # moved any of it. The selection minus the skipped stays recorded so it is
                # never submitted twice, and the skipped can be planned again.
                self.journal.settle_transfer(
                    row["run"],
                    sorted(k for k in row["keys"] if k.rsplit("/", 1)[-1] not in skipped_ids),
                )

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
        coverage = Window(**plan["coverage"])
        ours = {row["task_id"] for row in self.journal.transfers(destination) if row.get("task_id")}
        for prior in self.env.history():
            if prior.destination != destination or (prior.task_id and prior.task_id in ours):
                continue
            require(not selected.intersection(prior.bucket_ids), Code.OVERLAP)
            if not prior.bucket_ids and prior.complete:
                # A finished transfer with no bucket list (one started from the product UI,
                # say) covered whole buckets in its window; anything of ours overlapping
                # that window may already be there.
                require(not coverage.overlaps(prior.window), Code.OVERLAP)

    def _source_ingest(self) -> dict:
        """Whether the source is still receiving events: a cutover check that warns."""
        reader = getattr(self.env, "recent_source_events", None)
        events = None
        if reader is not None:
            try:
                events = reader(INGEST_WINDOW_SECONDS)
            except MigrationError:
                events = None
        return {"window_seconds": INGEST_WINDOW_SECONDS, "events": events}

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
            plan["source_ingest"] = self._source_ingest()
            events = plan["source_ingest"]["events"]
            hours = INGEST_WINDOW_SECONDS // 3600
            if events is None:
                plan["warnings"].append(
                    f"Source ingest in the last {hours} hours could not be observed; "
                    "confirm cutover before relying on the last sealed day."
                )
            elif events:
                plan["warnings"].append(
                    f"The source received {events:,} events in the last {hours} hours. "
                    "Open buckets keep growing; a later run of the same dates collects the "
                    "tail once they seal."
                )
            plan["partial_days"] = partial_days(source, plan)
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
            keys = tuple(b["node"] + "/" + b["id"] for b in run["plan"]["selected"])
            if self.attribution:
                run["source_by_bucket"] = {
                    key: asdict(self._counts(policy, "source", coverage, (key,))) for key in keys
                }
                run["baseline"] = asdict(self._counts(policy, "baseline", EVERYTHING, ()))
            else:
                # Live counts are per day for the whole cluster, so the evidence is the
                # covered days: source and destination baseline over the same days.
                days = day_window(coverage)
                run["source_by_bucket"] = {}
                run["source_counts"] = asdict(self._counts(policy, "source", days, keys))
                try:
                    run["baseline"] = asdict(self._counts(policy, "baseline", days, ()))
                except MigrationError as error:
                    # 9.1.1: no qualified count route. The transfer can still run; the
                    # verification will report incomplete evidence.
                    require(error.code == Code.UNSUPPORTED, error.code)
                    run["baseline"] = None
                run["partial_days"] = partial_days(source, fresh)
            run["backup_ack"] = True
            run["active_ack"] = active_ack == ACTIVE_ACK
            self._audit(run, "transfer_authorized", {"snapshot_verified": False})
            # Protect against duplicate sends even if the POST response is lost. This is an
            # overlap record, not a locally maintained task state or a destination-wide lock.
            self.journal.record_transfer(run)
            self._audit(run, "api_start_requested")
            try:
                task = self.env.start(Window(**fresh["window"]), keys)
            except Rejection:
                self.journal.release_transfer(run_id)
                self._audit(run, "api_start_rejected")
                raise
            except Exception:
                self._audit(run, "api_start_response_unavailable")
                raise MigrationError(Code.UNCERTAIN) from None
            self._audit(run, "api_start_response", asdict(task))
            if task.id:
                self.journal.note_transfer_task(run_id, task.id)
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
                if self.attribution:
                    result = self._reconcile(run, destination.id)
                else:
                    result = self._reconcile_days(run, destination.id)
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
            # A selection that did not all land is never a success; a discrepancy stays one.
            if result["outcome"] == Verification.SUCCESS.value:
                result["outcome"] = Verification.INCOMPLETE.value
            result["reason"] = Code.NOT_RECEIVED.value
        return result

    def _reconcile_days(self, run, destination):
        """Live evidence: this run's covered days, source versus destination minus baseline.

        New ingest cannot be attributed from counts alone, so a day shared with an open or
        unselected bucket is marked partial and never decides the outcome; a difference on
        a fully covered day is a discrepancy.
        """
        row = next((r for r in self.journal.transfers(destination) if r["run"] == run["id"]), None)
        require(row is not None and row["closed"], Code.INCOMPLETE)
        received = set(row["received"])
        require(bool(received), Code.INCOMPLETE)
        require(run.get("baseline") is not None, Code.UNSUPPORTED)
        source = Counts.from_dict(run["source_counts"])
        baseline = Counts.from_dict(run["baseline"])
        policy = Policy(**run["query_policy"])
        days = day_window(Window(**run["plan"]["coverage"]))
        actual = self._counts(policy, "destination", days, ())
        require(actual == self._counts(policy, "destination", days, ()), Code.INCOMPLETE)
        ingest = Counts(True, (), (), "ingest")
        result = reconcile(source, baseline, actual, ingest)
        partial = set(run.get("partial_days", []))
        for entry in result["daily"]:
            entry["partial"] = entry["day"] in partial
        full = [d for d in result["daily"] if not d["partial"]]
        # Without a single fully covered day there is no evidence to judge; that is
        # incomplete, never success.
        require(bool(full), Code.INCOMPLETE)
        result["outcome"] = (
            Verification.DISCREPANCY.value
            if any(d["difference"] != 0 for d in full)
            else Verification.SUCCESS.value
        )
        result["partial_days"] = sorted(partial)
        result["partial_differences"] = any(
            d["difference"] != 0 for d in result["daily"] if d["partial"]
        )
        result["method"] = (
            "Two agreeing complete observations; per-day source counts over the covered days "
            "against destination counts minus the baseline recorded at start. New ingest is "
            "not attributed; days shared with open or unselected buckets are partial."
        )
        result["buckets"] = {
            "covered": sorted(received),
            "not_received": sorted(set(row["keys"]) - received),
        }
        result["covered_days"] = [day for day, _ in source.daily]
        if result["buckets"]["not_received"]:
            if result["outcome"] == Verification.SUCCESS.value:
                result["outcome"] = Verification.INCOMPLETE.value
            result["reason"] = Code.NOT_RECEIVED.value
        return result
