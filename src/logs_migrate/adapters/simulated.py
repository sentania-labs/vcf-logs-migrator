"""Deterministic stateful appliance model, explicitly not a captured vendor response schema."""

from __future__ import annotations

import json
import os
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from ..errors import Code, MigrationError, require
from ..journal import file_lock
from ..models import Cluster, Counts, Partition, Window, canonical, digest
from ..ports import History, NodeTransfer, Transfer


def event_identity(event):
    return digest([event["id"], event["time"], event.get("fields", {})])


class SimulatedClock:
    def __init__(self):
        self.seconds = 0

    def now(self):
        return datetime.fromtimestamp(1_793_923_200 + self.seconds, UTC).isoformat()

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


class SimulatedEnvironment:
    """Faults are injected at named boundaries and persisted with the remote model.

    Events contain only deterministic synthetic IDs/field digests and counts. The
    simulator has independent source, destination, registration, and service state.
    """

    mode = "simulation"

    def __init__(self, fixture: dict, state_path: Path | None = None):
        require(fixture.get("fixture_schema") == "logs-migrate.simulator.v1", Code.SCHEMA)
        self.state_path = state_path
        if state_path is not None:
            state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fixture = json.loads(canonical(fixture))
        if state_path is not None and state_path.exists():
            require(not state_path.is_symlink(), Code.STORAGE)
            self.data = json.loads(state_path.read_text())
            require(self.data["fixture_id"] == fixture["fixture_id"], Code.INTEGRITY)
        else:
            self.data = self.fixture
            self.data.update(
                {
                    "trace": [],
                    "task": None,
                    "history": [],
                    "fixups": {},
                    "baseline_events": list(fixture["destination_events"]),
                    "poll": 0,
                    "recovery_markers": {},
                    "catalog_backups": {},
                }
            )
        self.locked = False

    @classmethod
    def from_file(cls, fixture: Path, state_path: Path | None = None):
        return cls(json.loads(fixture.read_text()), state_path)

    def _persist(self):
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not self.state_path.is_symlink(), Code.STORAGE)
        temp = self.state_path.with_suffix(".tmp")
        fd = os.open(
            temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(fd, "w") as f:
            f.write(canonical(self.data))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.state_path)

    def _trace(self, operation, **safe):
        self.data["trace"].append({"operation": operation, **safe})
        self._persist()

    def _fault(self, point):
        failures = self.data.get("faults", {})
        kind = failures.get(point)
        if isinstance(kind, list):
            kind = kind.pop(0) if kind else None
        if not kind:
            return
        self._persist()
        if kind == "crash":
            raise SystemExit(97)
        if kind == "auth":
            raise MigrationError(Code.AUTH)
        if kind == "secret":
            raise RuntimeError("fixture-password-DO-NOT-PRINT")
        raise MigrationError(Code.CONNECTION)

    @contextmanager
    def exclusive(self):
        require(not self.locked, Code.BUSY)
        lease = (
            file_lock(self.state_path.with_suffix(".lock")) if self.state_path else nullcontext()
        )
        with lease:
            if self.state_path is not None and self.state_path.exists():
                require(not self.state_path.is_symlink(), Code.STORAGE)
                self.data = json.loads(self.state_path.read_text())
                require(self.data["fixture_id"] == self.fixture["fixture_id"], Code.INTEGRITY)
            self.locked = True
            try:
                yield
            finally:
                self._persist()
                self.locked = False

    def discover(self):
        self._fault("discover")
        return Cluster.from_dict(self.data["source"]), Cluster.from_dict(self.data["destination"])

    def history(self):
        return tuple(
            History(
                **{
                    **h,
                    "window": Window(**h["window"]),
                    "destination_nodes": tuple(h["destination_nodes"]),
                    "bucket_ids": tuple(h["bucket_ids"]),
                }
            )
            for h in self.data["history"]
        )

    def create_partition(self, partition: Partition):
        self._trace("create_partition", partition=partition.id)
        self._fault("create_partition.before")
        require(
            partition.name not in {p["name"] for p in self.data["destination"]["partitions"]},
            Code.PARTITIONS,
        )
        self.data["destination"]["partitions"].append(asdict(partition))
        self._persist()
        self._fault("create_partition.after")

    def count(self, scope, window, bucket_keys):
        self._fault("query." + scope)
        if scope == "source":
            events = [e for e in self.data["source_events"] if e["bucket"] in bucket_keys]
        elif scope == "baseline":
            events = self.data["destination_events"]
        elif scope == "destination":
            events = self.data["destination_events"]
        elif scope == "ingest":
            baseline = Counter()
            for event in self.data["baseline_events"]:
                baseline[event_identity(event)] += event.get("multiplicity", 1)
            events = []
            for event in self.data["destination_events"]:
                if event.get("cohort") != "ingest":
                    continue
                identity = event_identity(event)
                count = event.get("multiplicity", 1)
                retained = min(baseline[identity], count)
                baseline[identity] -= retained
                if count > retained:
                    events.append({**event, "multiplicity": count - retained})
        else:
            raise MigrationError(Code.SCHEMA)
        daily = Counter()
        identities = Counter()
        for e in events:
            if window.start <= e["time"] < window.end:
                day = datetime.fromtimestamp(e["time"] / 1000, UTC).date().isoformat()
                daily[day] += e.get("multiplicity", 1)
                identity = event_identity(e)
                identities[day + "/" + identity] += e.get("multiplicity", 1)
        complete = True
        replies = self.data.get("query_completeness", {}).get(scope, [])
        if replies:
            complete = replies.pop(0)
            self._persist()
        require(type(complete) is bool, Code.SCHEMA)
        return Counts(
            complete, tuple(sorted(daily.items())), tuple(sorted(identities.items())), scope
        )

    def test_mover(self):
        self._trace("test_mover")
        self._fault("test_mover")

    def test_source_credentials(self, node):
        self._trace("test_source_credentials", node=node)
        self._fault("source_credentials." + node)

    def reserve(self, per_node, ceiling_percent):
        self._trace("reserve", per_node=per_node, ceiling=ceiling_percent)
        self._fault("reserve")
        self.data["reservation"] = {"bytes": per_node, "ceiling": ceiling_percent}
        self._persist()

    def start(self, window, bucket_keys):
        self._trace("start", window=asdict(window))
        self._fault("start.before")
        require(self.data["task"] is None or self.data["task"]["status"] != "running", Code.BUSY)
        self.data["task"] = {
            "id": f"sim-{len(self.data['history']) + 1}",
            "status": "running",
            "completed": [],
            "skipped": [],
            "bytes_transferred": 0,
            "keys": list(bucket_keys),
            "window": asdict(window),
        }
        self.data["baseline_events"] = json.loads(canonical(self.data["destination_events"]))
        self.data["poll"] = 0
        source, dest = self.discover()
        self.data["history"].append(
            asdict(
                History(
                    source.id,
                    dest.id,
                    window,
                    tuple(n.id for n in dest.nodes),
                    tuple(k.split("/")[-1] for k in bucket_keys),
                    False,
                )
            )
        )
        self._persist()
        self._fault("start.after")
        return self._transfer()

    def _transfer(self):
        task = self.data["task"]
        if task is None:
            return Transfer("", "idle")
        nodes = self.data["destination"]["nodes"]
        outcomes = []
        for node in nodes:
            # Fixture placement only. This is deliberately not called vendor MurmurHash placement.
            completed = tuple(
                k
                for k in task["completed"]
                if nodes[int(digest(k.split("/")[-1])[:8], 16) % len(nodes)]["id"] == node["id"]
            )
            skipped = tuple(
                k
                for k in task["skipped"]
                if nodes[int(digest(k.split("/")[-1])[:8], 16) % len(nodes)]["id"] == node["id"]
            )
            size = sum(
                b["size"]
                for b in self.data["source"]["buckets"]
                if b["node"] + "/" + b["id"] in completed
            )
            status = self.data.get("node_statuses", {}).get(node["id"], task["status"])
            outcomes.append(NodeTransfer(node["id"], status, completed, skipped, size))
        return Transfer(
            task["id"],
            task["status"],
            tuple(task["completed"]),
            tuple(task["skipped"]),
            task["bytes_transferred"],
            tuple(outcomes),
        )

    def transfer(self):
        self._fault("poll.before")
        task = self.data["task"]
        if task and task["status"] == "running":
            self.data["poll"] += 1
            if self.data["poll"] >= self.data.get("complete_after_polls", 1):
                source, dest = self.discover()
                skip = set(self.data.get("skip_buckets", []))
                for key in task["keys"]:
                    b = next(b for b in source.buckets if b.key == key)
                    if key in skip:
                        task["skipped"].append(key)
                        continue
                    node = dest.nodes[int(digest(b.id)[:8], 16) % len(dest.nodes)].id
                    copy = replace(b, node=node, partition="" if not dest.preview else b.partition)
                    if self.data.get("partial_at_bucket") == key:
                        partial = replace(
                            copy, complete=False, registered=False, size=b.size // 2, manifest=None
                        )
                        if not dest.preview:
                            self.data["destination"]["buckets"].append(asdict(partial))
                        task["skipped"].append(key)
                        task["bytes_transferred"] += partial.size
                        task["status"] = "failed"
                        self._persist()
                        return self._transfer()
                    if not dest.preview:
                        self.data["destination"]["buckets"].append(asdict(copy))
                    for e in self.data["source_events"]:
                        if e["bucket"] == key:
                            self.data["destination_events"].append({**e, "cohort": "migration"})
                    task["completed"].append(key)
                    task["bytes_transferred"] += b.size
                    for n in self.data["destination"]["nodes"]:
                        if n["id"] == node:
                            n["used"] += b.size
                            n["repository_available"] -= b.size
                task["status"] = self.data.get("terminal_status", "completed")
                self.data["history"][-1]["complete"] = not task["skipped"]
                self.data["destination_events"].extend(self.data.get("ingest_during_run", []))
                self._persist()
                self._fault("poll.after_complete")
        return self._transfer()

    def cancel(self):
        self._trace("cancel")
        self._fault("cancel.before")
        if self.data["task"]:
            self.data["task"]["status"] = "cancelled"
        self._persist()
        self._fault("cancel.after")

    def attest_transfer(self, bucket):
        self._fault("attest")
        task = self.data["task"]
        if self.data["destination"]["version"].startswith("9.1.1"):
            return task is not None and bucket.key in task["completed"]
        copies = [b for b in self.data["destination"]["buckets"] if b["id"] == bucket.id]
        return (
            len(copies) == 1
            and copies[0]["complete"]
            and copies[0].get("registered", True)
            and copies[0]["manifest"] == bucket.manifest
        )

    def test_node_credentials(self, node):
        self._trace("test_node_credentials", node=node)
        self._fault("credentials." + node)

    def service(self, node):
        self._fault("service." + node)
        return next(n["service"] for n in self.data["destination"]["nodes"] if n["id"] == node)

    def readiness(self, node):
        return (
            self.service(node) == "running",
            not self.data.get("rest_unhealthy", {}).get(node, False),
            not self.data.get("query_unready", {}).get(node, False),
        )

    def arm_recovery(self, node, run):
        self._trace("arm_recovery", node=node)
        self._fault("arm_recovery." + node)
        require(node not in self.data["recovery_markers"], Code.RECOVERY)
        self.data["recovery_markers"][node] = run
        self._persist()

    def owns_recovery(self, node, run):
        return self.data["recovery_markers"].get(node) == run

    def clear_recovery(self, node, run):
        require(self.owns_recovery(node, run), Code.RECOVERY)
        self._trace("clear_recovery", node=node)
        del self.data["recovery_markers"][node]
        self._persist()

    def catalog_backup(self, node, run):
        require(self.service(node) == "stopped", Code.SERVICE)
        self._trace("catalog_backup", node=node)
        self._fault("catalog_backup." + node)
        before = {
            b["id"]: dict(b) for b in self.data["destination"]["buckets"] if b["node"] == node
        }
        self.data["catalog_backups"][node] = before
        self._persist()
        return before

    def catalog_safe(self, node, before, intended):
        after = {b["id"]: b for b in self.data["destination"]["buckets"] if b["node"] == node}
        if not set(before) <= set(after):
            return False
        allowed = {b.id: b for b in intended}
        for key, previous in before.items():
            current = after[key]
            if key not in allowed and current != previous:
                return False
            if key in allowed:
                original = {k: v for k, v in previous.items() if k not in {"partition", "created"}}
                observed = {k: v for k, v in current.items() if k not in {"partition", "created"}}
                if original != observed:
                    return False
        return True

    def fixup_artifact_verified(self, node):
        return self.data.get("artifact_verified", False)

    def controller_lost(self):
        """Model the remote watchdog, independent from Python's finally block."""
        for node in self.data["recovery_markers"]:
            before = self.data["catalog_backups"].get(node)
            source, _ = self.discover()
            if before is None or self.catalog_safe(node, before, source.buckets):
                self.start_service(node)

    def stop_service(self, node):
        self._trace("stop_service", node=node)
        self._fault("stop.before." + node)
        next(n for n in self.data["destination"]["nodes"] if n["id"] == node)["service"] = "stopped"
        self._persist()
        self._fault("stop.after." + node)

    def start_service(self, node):
        self._trace("start_service", node=node)
        self._fault("restart.before." + node)
        next(n for n in self.data["destination"]["nodes"] if n["id"] == node)["service"] = "running"
        self._persist()
        self._fault("restart.after." + node)

    def fixup(self, node, bucket):
        self._trace("fixup", node=node, bucket=bucket.id)
        require(self.service(node) == "stopped" and self.attest_transfer(bucket), Code.SERVICE)
        self._fault("fixup.before." + node)
        target = next(
            b
            for b in self.data["destination"]["buckets"]
            if b["id"] == bucket.id and b["node"] == node
        )
        target["partition"] = bucket.partition
        target["created"] = bucket.end
        self.data["fixups"][node + "/" + bucket.id] = {
            "partition": bucket.partition,
            "creation_time": bucket.end,
        }
        self._persist()
        self._fault("fixup.after." + node)

    def fixup_confirmed(self, node, bucket):
        return self.data["fixups"].get(node + "/" + bucket.id) == {
            "partition": bucket.partition,
            "creation_time": bucket.end,
        }
