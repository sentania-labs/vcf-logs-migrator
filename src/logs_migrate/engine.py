"""Reusable orchestration. Presentation layers do not implement migration behavior."""

from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from .errors import Code, MigrationError, require, safe_boundary
from .journal import Journal
from .models import ACTIVE_ACK, Bucket, Cluster, Counts, Policy, State, Verification, Window, digest
from .planning import build_plan, capacity, compatible, partition_diff, preparation_hash
from .ports import Environment, SystemClock
from .verification import reconcile, stable_counts

_CANCEL_ON = {Code.CAPACITY, Code.INVENTORY_CHANGED, Code.TOPOLOGY, Code.PARTITIONS, Code.FIELDS}
_TERMINAL = {"idle", "completed", "cancelled", "failed"}


class _Detach(Exception):
    pass


class Engine:
    def __init__(self, environment: Environment, journal: Journal, clock=None, on_event=None):
        self.env = environment
        self.journal = journal
        self.clock = clock or SystemClock()
        self.on_event = on_event
        self.stop_requested = False
        self.detach_requested = False

    def _execution_gate(self):
        # Deliberate release gate, not a user-overridable acknowledgment. Live lab qualification
        # of credentials, placement, cancellation and restoration must precede its removal.
        require(self.env.mode == "simulation", Code.UNSUPPORTED)

    def request_stop(self):
        """Signal-safe intent; the monitor journals cancellation at its next checkpoint."""
        self.stop_requested = True

    def request_detach(self):
        """Signal-safe intent to stop observing. The appliance task is left running."""
        self.detach_requested = True

    def _save(self, run, kind, details=None):
        self.journal.save(run, kind, details)
        if self.on_event:
            # Subscribers cannot interrupt a service restoration or a journal commit.
            try:
                self.on_event(
                    {
                        "run_id": run["id"],
                        "kind": kind,
                        "at": run["updated_at"],
                        "details": details or {},
                    }
                )
            except Exception:
                pass

    @safe_boundary
    def discover(self) -> dict:
        with self.env.exclusive():
            source, destination = self.env.discover()
        compatible(source, destination)
        return {
            "schema_version": 1,
            "source": asdict(source),
            "destination": asdict(destination),
            "partition_diff": partition_diff(source, destination),
            "partition_preparation_hash": preparation_hash(source, destination),
            "destination_capability": "Experimental",
            "qualification": "Fixture delivery; this client needs live lab qualification",
        }

    @safe_boundary
    def plan(
        self,
        window: Window,
        policy: Policy | None = None,
        *,
        tail_of: str | None = None,
        create_missing: bool = False,
        backup_ack: bool = False,
        fixup_existing: bool = False,
        partition_hash: str | None = None,
    ) -> dict:
        with self.journal.exclusive(), self.env.exclusive():
            source, destination = self.env.discover()
            compatible(source, destination)
            run = {
                "schema_version": 1,
                "id": str(uuid4()),
                "state": State.DRAFT.value,
                "created_at": self.clock.now(),
                "updated_at": self.clock.now(),
                "reason": None,
                "plan": None,
                "plan_hash": None,
                "transfer": None,
                "transfer_intent": False,
                "cancel_intent": False,
                "backup_ack": False,
                "backup_verified": False,
                "service_obligations": {},
                "closed": None,
                "drift": [],
                "bucket_outcomes": {},
                "tail_data": [],
                "verification": {"outcome": Verification.PENDING.value},
            }
            self._save(run, "created")
            self.journal.transition(run, State.DISCOVERED)
            try:
                if create_missing:
                    self._execution_gate()
                    require(not self.journal.unresolved(destination.id), Code.RECOVERY)
                    require(
                        self.env.transfer().status in _TERMINAL,
                        Code.BUSY,
                    )
                    require(backup_ack, Code.ACK)
                    difference = partition_diff(source, destination)
                    require(partition_hash == preparation_hash(source, destination), Code.ACK)
                    require(not difference["different"], Code.PARTITIONS)
                    run["preparation"] = {
                        "source": source.identity_fingerprint,
                        "destination": destination.identity_fingerprint,
                        "destination_id": destination.id,
                        "partitions": [asdict(p) for p in source.partitions],
                        "hash": partition_hash,
                        "pending": True,
                    }
                    for p in source.partitions:
                        if p.name not in {d.name for d in destination.partitions}:
                            self._save(run, "partition_create_intent", {"partition_id": p.id})
                            self.env.create_partition(p)
                            _, destination = self.env.discover()
                            require(p in destination.partitions, Code.PARTITIONS)
                            self._save(run, "partition_create_observed", {"partition_id": p.id})
                    run["preparation"]["pending"] = False
                    self._save(run, "partition_preparation_complete")
                old_ids = ()
                if tail_of:
                    prior = self.journal.load(tail_of)
                    require(prior["state"] == State.VERIFIED, Code.OVERLAP)
                    require(destination.version.startswith("9.0."), Code.UNSUPPORTED)
                    require(prior["plan"]["window"] == asdict(window), Code.OVERLAP)
                    previous_source = Cluster.from_dict(prior["plan"]["source"])
                    previous_dest = Cluster.from_dict(prior["plan"]["destination"])
                    require(
                        previous_source.identity_fingerprint == source.identity_fingerprint,
                        Code.TOPOLOGY,
                    )
                    require(
                        previous_dest.identity_fingerprint == destination.identity_fingerprint,
                        Code.TOPOLOGY,
                    )
                    old_ids = tuple(b["id"] for b in prior["plan"]["selected"])
                    old_ids += tuple(prior["plan"]["previously_copied"])
                    for b in prior["plan"]["selected"]:
                        current = next((s for s in source.buckets if s.id == b["id"]), None)
                        require(
                            current is not None and current.manifest == b["manifest"],
                            Code.COLLISION,
                        )
                require(not (fixup_existing and tail_of), Code.INVALID_INPUT)
                plan = build_plan(
                    source,
                    destination,
                    window,
                    policy or Policy(),
                    old_ids,
                    fixup_only=fixup_existing,
                )
                plan["tail_of"] = tail_of
                if not fixup_existing:
                    self._overlap(plan)
                run["plan"] = plan
                run["plan_hash"] = digest(plan)
                run["bucket_outcomes"] = {
                    b["node"] + "/" + b["id"]: "pending" for b in plan["selected"]
                }
                self.journal.transition(run, State.PLANNED)
                return run
            except MigrationError as error:
                uncertain = run.get("preparation", {}).get("pending", False)
                self.journal.transition(
                    run, State.RECOVERY_REQUIRED if uncertain else State.BLOCKED, error.code
                )
                error.run_id = run["id"]
                raise
            except Exception:
                self.journal.transition(run, State.RECOVERY_REQUIRED, Code.UNCERTAIN)
                error = MigrationError(Code.UNCERTAIN)
                error.run_id = run["id"]
                raise error from None

    def _overlap(self, plan):
        destination = Cluster.from_dict(plan["destination"])
        require(destination.history_complete, Code.INCOMPLETE)
        window = Window(**plan["window"])
        selected_ids = {b["id"] for b in plan["selected"]}
        ancestors = set()
        tail = plan["tail_of"]
        while tail:
            require(tail not in ancestors, Code.OVERLAP)
            ancestors.add(tail)
            tail = self.journal.load(tail)["plan"]["tail_of"]
        for h in self.journal.transfers(destination.id):
            require(not (selected_ids & set(h["bucket_ids"])), Code.OVERLAP)
            require(set(h["destination_nodes"]) == {n.id for n in destination.nodes}, Code.TOPOLOGY)
            if window.overlaps(Window(h["start"], h["end"])):
                require(h["run"] in ancestors, Code.OVERLAP)
        for h in self.env.history():
            if h.destination != destination.id:
                continue
            require(not (selected_ids & set(h.bucket_ids)), Code.OVERLAP)
            if window.overlaps(h.window):
                # External history may be reconciled only by a recorded run with the same IDs.
                permitted = (
                    bool(ancestors)
                    and h.complete
                    and set(h.bucket_ids) <= set(plan["previously_copied"])
                )
                require(permitted, Code.OVERLAP)
                require(
                    set(h.destination_nodes) == {n.id for n in destination.nodes}, Code.TOPOLOGY
                )

    def _tail(self, run, source, *, strict):
        """Sealed in-window buckets the plan does not cover are tail data for a later run."""
        plan = run["plan"]
        window = Window(**plan["window"])
        known = set(plan["api_selected"])
        found = []
        for b in source.buckets:
            if b.status == "active" or b.key in known:
                continue
            if not b.bounds_proven or b.start is None or b.end is None:
                require(not strict, Code.INVENTORY_CHANGED)
            elif b.selected(window):
                found.append(b.key)
        require(not (strict and found), Code.INVENTORY_CHANGED)
        if sorted(found) != run.get("tail_data", []):
            run["tail_data"] = sorted(found)
            self._save(run, "tail_data_observed", {"buckets": run["tail_data"]})

    def _live(self, run, *, during=False, verifying=False):
        """Revalidate. Only changes that affect the selected cohort or its targets can stop a copy.

        Routine destination events (retention retiring its own buckets, a restart, node growth)
        and post-copy source aging are recorded as drift facts, never as reasons to interrupt
        a healthy transfer or to make verification unreachable.
        """
        plan = run["plan"]
        require(digest(plan) == run["plan_hash"], Code.INTEGRITY)
        source, destination = self.env.discover()
        compatible(source, destination)
        previous = Cluster.from_dict(plan["destination"])
        strict = not (during or verifying)
        drift: list[str] = []

        def drifted(name, code):
            require(not strict, code)
            drift.append(name)

        if verifying:
            require(
                destination.id == previous.id
                and {n.id for n in previous.nodes} <= {n.id for n in destination.nodes},
                Code.TOPOLOGY,
            )
            if destination.identity_fingerprint != previous.identity_fingerprint:
                drift.append("destination_topology_changed")
            current = {b.key: b for b in source.buckets}
            planned = {f"{b['node']}/{b['id']}": b for b in plan["source"]["buckets"]}
            for key in plan["api_selected"]:
                if key not in current:
                    drift.append("source_bucket_aged_out")
                else:
                    require(asdict(current[key]) == planned[key], Code.INVENTORY_CHANGED)
        else:
            require(
                destination.identity_fingerprint == previous.identity_fingerprint, Code.TOPOLOGY
            )
            require(
                source.cohort_fingerprint(plan["api_selected"]) == plan["source_fingerprint"],
                Code.INVENTORY_CHANGED,
            )
        self._tail(run, source, strict=strict)
        if destination.retention_fingerprint != previous.retention_fingerprint:
            drifted("destination_retention_changed", Code.INVENTORY_CHANGED)
        if {n.id: n.service_epoch for n in destination.nodes} != {
            n.id: n.service_epoch for n in previous.nodes
        }:
            drifted("destination_service_restarted", Code.SERVICE)
        difference = partition_diff(source, destination)
        if verifying:
            if difference["missing"] or difference["different"]:
                drift.append("partitions_changed")
            if difference["missing_fields"]:
                drift.append("extracted_fields_changed")
        else:
            require(not difference["missing"] and not difference["different"], Code.PARTITIONS)
            require(not difference["missing_fields"], Code.FIELDS)
        present = {b.key: b for b in destination.buckets}
        for before in previous.buckets:
            if before.status != "active" and present.get(before.key) != before:
                drifted("destination_baseline_bucket_changed", Code.INVENTORY_CHANGED)
        selected = tuple(Bucket(**b) for b in plan["selected"])
        if strict:
            self._overlap(plan)
            for b in selected:
                require(not any(d.id == b.id for d in destination.buckets), Code.COLLISION)
        known = set(run.get("drift", []))
        if set(drift) - known:
            run["drift"] = sorted(known | set(drift))
            self._save(run, "destination_drift_observed", {"facts": sorted(set(drift) - known)})
        if verifying:
            return source, destination, None
        policy = Policy(**plan["policy"])
        remaining = tuple(b for b in selected if run["bucket_outcomes"].get(b.key) != "complete")
        return source, destination, capacity(destination, remaining, policy)

    def _counts(self, run, scope):
        plan = run["plan"]
        return stable_counts(
            self.env,
            self.clock,
            Policy(**plan["policy"]),
            scope,
            Window(**plan["coverage"]),
            tuple(run["bucket_outcomes"]),
        )

    @safe_boundary
    def run(self, run_id: str, *, plan_hash: str, backup_ack: bool, active_ack: str = "") -> dict:
        self._execution_gate()
        with self.journal.exclusive(), self.env.exclusive():
            run = self.journal.load(run_id)
            require(run["plan"]["kind"] == "migration", Code.STATE)
            require(not run["transfer_intent"], Code.UNCERTAIN)
            require(run["state"] in {State.PLANNED, State.BLOCKED}, Code.STATE)
            try:
                require(
                    not self.journal.unresolved(run["plan"]["destination"]["id"], run_id),
                    Code.RECOVERY,
                )
                require(
                    not self.journal.awaiting_verification(run["plan"]["destination"]["id"]),
                    Code.VERIFICATION_PENDING,
                )
                require(plan_hash == run["plan_hash"] and backup_ack, Code.ACK)
                require(not run["plan"]["active_skipped"] or active_ack == ACTIVE_ACK, Code.ACK)
                _, _, reservations = self._live(run)
                remote = self.env.transfer()
                require(remote.status in _TERMINAL, Code.BUSY)
                require(not run["service_obligations"], Code.RECOVERY)
                source = self._counts(run, "source")
                require(
                    source.total == sum(b["messages"] for b in run["plan"]["selected"]),
                    Code.INCOMPLETE,
                )
                baseline = self._counts(run, "baseline")
                require(
                    not (set(dict(source.identities)) & set(dict(baseline.identities))),
                    Code.OVERLAP,
                )
                for node in run["plan"]["source"]["nodes"]:
                    self.env.test_source_credentials(node["id"])
                self._save(run, "mover_test_intent")
                self.env.test_mover()
                self._save(run, "reserve_intent")
                self.env.reserve(reservations, run["plan"]["policy"]["ceiling_percent"])
                self._live(run)
                run["baseline"] = asdict(baseline)
                run["source_evidence"] = asdict(source)
                run["previous_task"] = asdict(remote)
                self.journal.transition(run, State.PREFLIGHT_PASSED)
                run["backup_ack"] = True
                run["active_ack"] = bool(active_ack == ACTIVE_ACK)
                self.journal.transition(run, State.AUTHORIZED)
                require(not self.detach_requested, Code.INTERRUPTED)
                # The permanent overlap ledger is intentionally conservative across crashes.
                self.journal.record_transfer(run)
                run["transfer_intent"] = True
                self._save(run, "transfer_start_intent", {"plan_hash": plan_hash})
                self.journal.transition(run, State.RUNNING)
                try:
                    transfer = self.env.start(
                        Window(**run["plan"]["window"]), tuple(run["bucket_outcomes"])
                    )
                except Exception:
                    self.journal.transition(run, State.RECOVERY_REQUIRED, Code.UNCERTAIN)
                    raise MigrationError(Code.UNCERTAIN) from None
                run["transfer"] = asdict(transfer)
                self._save(run, "transfer_start_observed")
                return self._monitor(run)
            except MigrationError as error:
                if run["state"] not in {State.RECOVERY_REQUIRED, State.PARTIAL, State.STOPPED}:
                    self.journal.transition(
                        run,
                        State.RECOVERY_REQUIRED if run["transfer_intent"] else State.BLOCKED,
                        error.code,
                    )
                raise
            except Exception:
                self.journal.transition(run, State.RECOVERY_REQUIRED, Code.UNCERTAIN)
                raise MigrationError(Code.UNCERTAIN) from None

    def _contact(self, run, call, *, detachable=True):
        policy = Policy(**run["plan"]["policy"])
        lost = None
        while True:
            try:
                return call()
            except MigrationError as error:
                if error.code != Code.CONNECTION:
                    raise
            if detachable and self.detach_requested:
                raise _Detach
            now = self.clock.monotonic()
            if lost is None:
                lost = now
                self._save(run, "monitor_contact_lost")
            require(now - lost < policy.contact_loss_seconds, Code.UNCERTAIN)
            self.clock.sleep(policy.monitor_poll_seconds)

    def _monitor(self, run):
        if run["state"] in {State.MONITOR_DETACHED, State.RECOVERY_REQUIRED}:
            self.journal.transition(run, State.RUNNING)
        try:
            return self._watch(run)
        except _Detach:
            self.journal.transition(run, State.MONITOR_DETACHED)
            return run

    def _watch(self, run):
        policy = Policy(**run["plan"]["policy"])
        while True:
            transfer = self._contact(run, self.env.transfer)
            if run["transfer"] is not None:
                require(transfer.id == run["transfer"]["id"], Code.UNCERTAIN)
                require(
                    transfer.bytes_transferred >= run["transfer"]["bytes_transferred"]
                    and set(run["transfer"]["completed"]) <= set(transfer.completed),
                    Code.UNCERTAIN,
                )
            require(
                transfer.status in {"running", "completed", "cancelled", "failed"}, Code.UNCERTAIN
            )
            if self.stop_requested and transfer.status == "running":
                return self._cancel(run)
            require(
                set(transfer.completed) | set(transfer.skipped) <= set(run["plan"]["api_selected"]),
                Code.INTEGRITY,
            )
            expected_nodes = {n["id"] for n in run["plan"]["destination"]["nodes"]}
            require(
                len(transfer.nodes) == len(expected_nodes)
                and {n.node for n in transfer.nodes} == expected_nodes,
                Code.INCOMPLETE,
            )
            require(
                all(
                    n.status in {"running", "completed", "cancelled", "failed"}
                    for n in transfer.nodes
                ),
                Code.UNCERTAIN,
            )
            require(
                {key for n in transfer.nodes for key in n.completed} == set(transfer.completed)
                and {key for n in transfer.nodes for key in n.skipped} == set(transfer.skipped),
                Code.INCOMPLETE,
            )
            require(
                sum(len(n.completed) for n in transfer.nodes) == len(set(transfer.completed)),
                Code.COLLISION,
            )
            self._account(run, transfer)
            self._save(run, "transfer_observed")
            try:
                self._contact(run, lambda: self._live(run, during=True))
            except MigrationError as error:
                if error.code not in _CANCEL_ON:
                    raise
                if transfer.status == "running":
                    self._cancel(run)
                self.journal.transition(run, State.PARTIAL, error.code)
                raise
            if self.stop_requested and transfer.status == "running":
                return self._cancel(run)
            if self.detach_requested and transfer.status == "running":
                raise _Detach
            if transfer.status != "running":
                complete = all(v == "complete" for v in run["bucket_outcomes"].values())
                if transfer.status == "cancelled":
                    self.journal.transition(run, State.STOPPED)
                elif (
                    transfer.status == "completed"
                    and complete
                    and not transfer.skipped
                    and all(n.status == "completed" for n in transfer.nodes)
                ):
                    self.journal.transition(run, State.VERIFYING)
                else:
                    self.journal.transition(run, State.PARTIAL, Code.INCOMPLETE)
                return run
            self.clock.sleep(policy.monitor_poll_seconds)

    def _account(self, run, transfer, *, detachable=True):
        """Record what landed, attesting every landed bucket before it counts as complete."""
        run["transfer"] = asdict(transfer)
        for b in (Bucket(**b) for b in run["plan"]["selected"]):
            if b.key in transfer.completed:
                require(
                    self._contact(
                        run, lambda b=b: self.env.attest_transfer(b), detachable=detachable
                    ),
                    Code.INTEGRITY,
                )
                run["bucket_outcomes"][b.key] = "complete"
            elif b.key in transfer.skipped:
                run["bucket_outcomes"][b.key] = "skipped"

    def _cancel(self, run):
        observed = self._contact(run, self.env.transfer, detachable=False)
        require(
            run["transfer"] is not None and observed.id == run["transfer"]["id"], Code.UNCERTAIN
        )
        if observed.status == "running":
            run["cancel_intent"] = True
            self._save(run, "cancel_intent")
            try:
                self.env.cancel()
                policy = Policy(**run["plan"]["policy"])
                for _ in range(policy.attempts):
                    observed = self.env.transfer()
                    require(observed.id == run["transfer"]["id"], Code.UNCERTAIN)
                    if observed.status in {"cancelled", "completed", "failed"}:
                        break
                    self.clock.sleep(policy.poll_seconds)
                else:
                    raise MigrationError(Code.UNCERTAIN)
            except Exception:
                self.journal.transition(run, State.RECOVERY_REQUIRED, Code.UNCERTAIN)
                raise MigrationError(Code.UNCERTAIN) from None
        require(observed.status in {"completed", "cancelled", "failed"}, Code.UNCERTAIN)
        self._account(run, observed, detachable=False)
        if observed.status == "failed":
            self.journal.transition(run, State.PARTIAL, Code.INCOMPLETE)
        else:
            self.journal.transition(run, State.STOPPED)
        return run

    @safe_boundary
    def resume(self, run_id: str) -> dict:
        """Reattach only. Never POST the time window again after a lost acknowledgment."""
        self._execution_gate()
        with self.journal.exclusive(), self.env.exclusive():
            run = self.journal.load(run_id)
            if run.get("preparation", {}).get("pending"):
                source, destination = self.env.discover()
                preparation = run["preparation"]
                require(
                    source.identity_fingerprint == preparation["source"]
                    and destination.identity_fingerprint == preparation["destination"],
                    Code.TOPOLOGY,
                )
                observed = {p.name: asdict(p) for p in destination.partitions}
                require(
                    all(observed.get(p["name"]) == p for p in preparation["partitions"]),
                    Code.PARTITIONS,
                )
                preparation["pending"] = False
                self._save(run, "partition_preparation_reconciled")
                self.journal.transition(run, State.BLOCKED, Code.PARTITIONS)
                return run
            require(run["transfer_intent"], Code.STATE)
            require(
                run["state"]
                in {
                    State.AUTHORIZED,
                    State.RUNNING,
                    State.MONITOR_DETACHED,
                    State.RECOVERY_REQUIRED,
                },
                Code.STATE,
            )
            require(not run["service_obligations"], Code.RECOVERY)
            try:
                try:
                    self._contact(run, lambda: self._live(run, during=True))
                except _Detach:
                    require(run["transfer"] is not None, Code.UNCERTAIN)
                    if run["state"] == State.RUNNING:
                        self.journal.transition(run, State.MONITOR_DETACHED)
                    return run
                except MigrationError as error:
                    if (
                        (self.stop_requested or error.code in _CANCEL_ON)
                        and error.code != Code.UNCERTAIN
                        and run["transfer"] is not None
                        and run["state"]
                        in {
                            State.RUNNING,
                            State.MONITOR_DETACHED,
                            State.RECOVERY_REQUIRED,
                        }
                    ):
                        self._cancel(run)
                        self.journal.transition(run, State.PARTIAL, error.code)
                    raise
                # An unacknowledged POST cannot be attributed by task state alone.
                require(run["transfer"] is not None, Code.UNCERTAIN)
                return self._monitor(run)
            except MigrationError as error:
                if run["state"] not in {State.RECOVERY_REQUIRED, State.PARTIAL, State.STOPPED}:
                    self.journal.transition(run, State.RECOVERY_REQUIRED, error.code)
                raise
            except Exception:
                self.journal.transition(run, State.RECOVERY_REQUIRED, Code.UNCERTAIN)
                raise MigrationError(Code.UNCERTAIN) from None

    @safe_boundary
    def verify(self, run_id: str) -> dict:
        with self.journal.exclusive(), self.env.exclusive():
            run = self.journal.load(run_id)
            if run["state"] == State.RECOVERY_REQUIRED or run["service_obligations"]:
                run["verification"] = {
                    "outcome": Verification.INCOMPLETE.value,
                    "complete": False,
                    "reason": Code.RECOVERY.value,
                }
                self._save(run, "verification_deferred_for_recovery")
                return run
            require(run["state"] not in {State.RUNNING, State.MONITOR_DETACHED}, Code.BUSY)
            require(run["transfer_intent"] and not run["service_obligations"], Code.STATE)
            try:
                require(
                    all(v == "complete" for v in run["bucket_outcomes"].values()), Code.INCOMPLETE
                )
                self._contact(run, lambda: self._live(run, verifying=True), detachable=False)
                # The appliance reports only its latest task; the terminal evidence persisted
                # at the moment it was observed is what this run's verification stands on.
                transfer = run["transfer"]
                require(
                    transfer is not None
                    and transfer["status"] == "completed"
                    and not transfer["skipped"],
                    Code.INCOMPLETE,
                )
                require(
                    len(transfer["nodes"]) == len(run["plan"]["destination"]["nodes"])
                    and all(n["status"] == "completed" for n in transfer["nodes"]),
                    Code.INCOMPLETE,
                )
                self.journal.transition(run, State.VERIFYING)
                source = Counts.from_dict(run["source_evidence"])
                actual = self._counts(run, "destination")
                ingest = self._counts(run, "ingest")
                # Query again as a set; independently stable queries may refer to different moments.
                require(
                    actual == self._counts(run, "destination")
                    and ingest == self._counts(run, "ingest"),
                    Code.INCOMPLETE,
                )
                result = reconcile(source, Counts.from_dict(run["baseline"]), actual, ingest)
                result["at"] = self.clock.now()
                result["window"] = run["plan"]["coverage"]
                run["verification"] = result
                self._contact(run, lambda: self._live(run, verifying=True), detachable=False)
                if result["outcome"] == Verification.SUCCESS:
                    run["closed"] = None
                self.journal.transition(
                    run,
                    State.VERIFIED if result["outcome"] == Verification.SUCCESS else State.PARTIAL,
                )
            except MigrationError as error:
                run["verification"] = {
                    "outcome": Verification.INCOMPLETE.value,
                    "complete": False,
                    "reason": error.code.value,
                }
                self.journal.transition(run, State.PARTIAL, error.code)
            except Exception:
                run["verification"] = {
                    "outcome": Verification.FAILURE.value,
                    "complete": False,
                    "reason": Code.CONNECTION.value,
                }
                self.journal.transition(run, State.FAILED, Code.CONNECTION)
            return run

    @safe_boundary
    def close(self, run_id: str) -> dict:
        """Operator close of a run that cannot complete. Never success, never a deletion.

        Re-reads the destination, confirms the run's task is not active, records exactly which
        planned buckets landed, and releases only the verification dependency. Overlap
        protection is kept for every bucket that landed or whose outcome is uncertain; it is
        released only for a run proven never to have reached the appliance.
        """
        self._execution_gate()
        with self.journal.exclusive(), self.env.exclusive():
            run = self.journal.load(run_id)
            require(run["transfer_intent"] and not run["closed"], Code.STATE)
            require(
                run["state"]
                in {State.STOPPED, State.PARTIAL, State.FAILED, State.RECOVERY_REQUIRED},
                Code.STATE,
            )
            require(not run["service_obligations"], Code.RECOVERY)
            plan = run["plan"]
            _, destination = self._contact(run, self.env.discover, detachable=False)
            remote = self._contact(run, self.env.transfer, detachable=False)
            require(remote.status != "running", Code.BUSY)
            selected = tuple(Bucket(**b) for b in plan["selected"])
            started = run["transfer"] is not None
            buckets = {}
            for b in selected:
                copies = [d for d in destination.buckets if d.id == b.id]
                if run["bucket_outcomes"].get(b.key) == "complete":
                    buckets[b.key] = "landed"
                elif copies and self._contact(
                    run, lambda b=b: self.env.attest_transfer(b), detachable=False
                ):
                    buckets[b.key] = "landed"
                elif copies:
                    buckets[b.key] = "unverified_destination_copy"
                elif (
                    destination.preview
                    and started
                    and remote.id == run["transfer"]["id"]
                    and self._contact(
                        run, lambda b=b: self.env.attest_transfer(b), detachable=False
                    )
                ):
                    buckets[b.key] = "landed"
                elif destination.preview:
                    buckets[b.key] = "unknown"
                else:
                    buckets[b.key] = "absent"
            if not started:
                previous = run["previous_task"]
                history = (
                    h
                    for h in self.env.history()
                    if h.destination == destination.id
                    and set(h.bucket_ids) & {b.id for b in selected}
                )
                # An unchanged appliance task and no trace of any selected bucket prove that
                # the lost POST never took effect; 9.1.1 inventory cannot prove absence.
                require(
                    not destination.preview
                    and (remote.id, remote.status) == (previous["id"], previous["status"])
                    and all(v == "absent" for v in buckets.values())
                    and next(history, None) is None,
                    Code.UNCERTAIN,
                )
            elif all(v == "landed" for v in buckets.values()):
                require(
                    run.get("fixup", {}).get("prior_verification") == Verification.SUCCESS.value,
                    Code.VERIFICATION_PENDING,
                )
            self._save(run, "close_intent")
            if not started:
                self.journal.release_transfer(run["id"])
            run["closed"] = {
                "at": self.clock.now(),
                "task_status": remote.status,
                "task_started": started,
                "buckets": buckets,
                "landed": sorted(k for k, v in buckets.items() if v == "landed"),
                "event_reconciliation": False,
                "verification_outcome": run["verification"]["outcome"],
                "overlap_protection": "retained" if started else "released_never_started",
            }
            if run["state"] == State.RECOVERY_REQUIRED:
                reason = Code(run["reason"]) if run["reason"] else None
                self.journal.transition(run, State.PARTIAL if started else State.FAILED, reason)
            else:
                self._save(run, "run_closed")
            return run

    @safe_boundary
    def fixup(
        self, run_id: str, *, consent_nodes: tuple[str, ...], backup_ack: bool, plan_hash: str
    ) -> dict:
        self._execution_gate()
        with self.journal.exclusive(), self.env.exclusive():
            run = self.journal.load(run_id)
            require(run["plan"] is not None and backup_ack, Code.ACK)
            require(plan_hash == run["plan_hash"] and digest(run["plan"]) == plan_hash, Code.ACK)
            require(not run["service_obligations"], Code.RECOVERY)
            require(run["state"] != State.RECOVERY_REQUIRED, Code.RECOVERY)
            require(
                run["plan"]["kind"] == "fixup"
                or run["verification"]["outcome"] == Verification.SUCCESS,
                Code.INCOMPLETE,
            )
            source, destination = self.env.discover()
            require(not self.journal.unresolved(destination.id, run_id), Code.RECOVERY)
            require(
                source.cohort_fingerprint(run["plan"]["api_selected"])
                == run["plan"]["source_fingerprint"],
                Code.INVENTORY_CHANGED,
            )
            require(destination.version.startswith("9.0."), Code.UNSUPPORTED)
            expected = Cluster.from_dict(run["plan"]["destination"])
            require(
                destination.identity_fingerprint == expected.identity_fingerprint, Code.TOPOLOGY
            )
            difference = partition_diff(source, destination)
            require(not difference["missing"] and not difference["different"], Code.PARTITIONS)
            require(
                self.env.transfer().status in _TERMINAL,
                Code.BUSY,
            )
            capacity(destination, (), Policy(**run["plan"]["policy"]))
            targets: dict[str, list[Bucket]] = {}
            for b in (Bucket(**b) for b in run["plan"]["selected"]):
                copies = [d for d in destination.buckets if d.id == b.id]
                require(
                    len(copies) == 1 and copies[0].complete and copies[0].manifest == b.manifest,
                    Code.INTEGRITY,
                )
                require(self.env.attest_transfer(b), Code.INTEGRITY)
                partition = next((p for p in destination.partitions if p.id == b.partition), None)
                require(partition is not None and b.end is not None, Code.PARTITIONS)
                from .models import parse_time

                margin = run["plan"]["policy"]["retention_margin_seconds"] * 1000
                require(
                    partition.retention_seconds == 0
                    or b.end + partition.retention_seconds * 1000
                    > parse_time(self.clock.now()) + margin,
                    Code.RETENTION,
                )
                node = copies[0].node
                already_fixed = run.get("fixup_nodes", {}).get(
                    node
                ) == "complete" and self.env.fixup_confirmed(node, b)
                if not already_fixed:
                    targets.setdefault(node, []).append(b)
            require(set(targets) <= set(consent_nodes), Code.ACK)
            # All credentials and initial states are tested before ANY node stops.
            for node in targets:
                self.env.test_node_credentials(node)
                require(all(self.env.readiness(node)), Code.SERVICE)
                require(self.env.fixup_artifact_verified(node), Code.INTEGRITY)
            before_counts = self._counts(run, "destination")
            run["backup_ack"] = True
            run["fixup_consent"] = {"plan_hash": plan_hash, "nodes": list(consent_nodes)}
            prior = run["verification"]["outcome"]
            run["verification"] = {"outcome": Verification.PENDING.value}
            run["fixup"] = {"outcome": "in_progress", "prior_verification": prior}
            self._save(run, "fixup_authorized")
            if run["plan"]["kind"] == "migration":
                self.journal.transition(run, State.PARTIAL)
            primary = destination.nodes[0].id
            for node in sorted(targets, key=lambda n: (n == primary, n)):
                for other in destination.nodes:
                    require(all(self.env.readiness(other.id)), Code.SERVICE)
                require(
                    self.env.transfer().status in _TERMINAL,
                    Code.BUSY,
                )
                buckets = targets[node]
                self._fixup_node(run, node, buckets)
                run.setdefault("fixup_nodes", {})[node] = "complete"
                self._save(run, "fixup_node_complete", {"node": node})
            after_counts = self._counts(run, "destination")
            require(after_counts == before_counts, Code.INTEGRITY)
            run["fixup"] = {
                "outcome": "complete",
                "counts_unchanged": True,
                "count": after_counts.total,
                "at": self.clock.now(),
                "prior_verification": prior,
            }
            self._save(run, "fixup_complete")
            run["verification"] = {"outcome": Verification.PENDING.value}
            if run["plan"]["kind"] == "migration":
                self.journal.transition(run, State.PARTIAL)
            return run

    def _fixup_node(self, run, node, buckets):
        run["service_obligations"][node] = {"initial": "running", "restart_required": True}
        self._save(run, "remote_recovery_arm_intent", {"node": node})
        self.env.arm_recovery(node, run["id"])
        require(self.env.owns_recovery(node, run["id"]), Code.RECOVERY)
        self._save(run, "service_stop_intent", {"node": node})
        failed = None
        catalog_safe = True
        before = None
        try:
            self.env.stop_service(node)
            require(self.env.service(node) == "stopped", Code.SERVICE)
            self._save(run, "service_stopped", {"node": node})
            self._save(run, "catalog_backup_intent", {"node": node})
            before = self.env.catalog_backup(node, run["id"])
            run["service_obligations"][node]["catalog_before"] = before
            self._save(run, "catalog_backup_observed", {"node": node})
            for b in buckets:
                require(
                    b.complete and b.status != "active" and self.env.attest_transfer(b),
                    Code.INTEGRITY,
                )
                self._save(
                    run,
                    "fixup_intent",
                    {
                        "node": node,
                        "bucket": b.id,
                        "partition": b.partition,
                        "creation_time": b.end,
                    },
                )
                self.env.fixup(node, b)
                require(self.env.fixup_confirmed(node, b), Code.INTEGRITY)
                self._save(run, "fixup_observed", {"node": node, "bucket": b.id})
        except Exception:
            failed = Code.SERVICE
        finally:
            try:
                if before is not None:
                    catalog_safe = self.env.catalog_safe(node, before, tuple(buckets))
                require(catalog_safe, Code.RECOVERY)
                journal_failure = False
                try:
                    self._save(run, "service_restart_intent", {"node": node})
                except MigrationError:
                    # Initial durable obligation requires restoration even if the disk fills.
                    journal_failure = True
                require(self.env.owns_recovery(node, run["id"]), Code.RECOVERY)
                self.env.start_service(node)
                require(all(self.env.readiness(node)), Code.RECOVERY)
                require(not journal_failure, Code.STORAGE)
                run["service_obligations"][node]["restored"] = True
                self._save(run, "service_restart_confirmed", {"node": node})
                self._save(run, "remote_recovery_clear_intent", {"node": node})
                self.env.clear_recovery(node, run["id"])
                del run["service_obligations"][node]
                self._save(run, "service_obligation_resolved", {"node": node})
            except Exception:
                self.journal.transition(run, State.RECOVERY_REQUIRED, Code.RECOVERY)
                raise MigrationError(Code.RECOVERY) from None
        if failed:
            self.journal.transition(run, State.PARTIAL, failed)
            raise MigrationError(failed)

    @safe_boundary
    def recover_services(self, run_id: str) -> dict:
        self._execution_gate()
        with self.journal.exclusive(), self.env.exclusive():
            run = self.journal.load(run_id)
            _, destination = self.env.discover()
            require(bool(run["service_obligations"]), Code.STATE)
            self.journal.transition(run, State.RECOVERY_REQUIRED, Code.RECOVERY)
            expected = Cluster.from_dict(run["plan"]["destination"])
            require(
                destination.identity_fingerprint == expected.identity_fingerprint, Code.TOPOLOGY
            )
            for node, obligation in list(run["service_obligations"].items()):
                require(obligation["initial"] == "running", Code.RECOVERY)
                owns = self.env.owns_recovery(node, run["id"])
                require(owns or obligation.get("restored", False), Code.RECOVERY)
                self.env.test_node_credentials(node)
                if "catalog_before" in obligation:
                    require(
                        self.env.catalog_safe(
                            node,
                            obligation["catalog_before"],
                            tuple(Bucket(**b) for b in run["plan"]["selected"]),
                        ),
                        Code.RECOVERY,
                    )
                self._save(run, "service_recovery_intent", {"node": node})
                try:
                    if owns:
                        require(
                            self.env.transfer().status in _TERMINAL,
                            Code.BUSY,
                        )
                        self.env.start_service(node)
                    require(all(self.env.readiness(node)), Code.RECOVERY)
                except Exception:
                    self.journal.transition(run, State.RECOVERY_REQUIRED, Code.RECOVERY)
                    raise MigrationError(Code.RECOVERY) from None
                obligation["restored"] = True
                self._save(run, "service_restart_confirmed", {"node": node})
                if owns:
                    self._save(run, "remote_recovery_clear_intent", {"node": node})
                    self.env.clear_recovery(node, run["id"])
                del run["service_obligations"][node]
                self._save(run, "service_obligation_resolved", {"node": node})
            self.journal.transition(run, State.STOPPED)
            return run
