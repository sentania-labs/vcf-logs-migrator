"""Deterministic stateful appliance model, explicitly not a captured vendor response schema."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from collections import Counter
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from ..config import RULE_TYPES, indexed, write_body
from ..errors import Code, MigrationError, require
from ..journal import file_lock
from ..models import Cluster, Counts, Window, canonical, digest
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
    attribution = True

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
        if kind == "invalid_input":
            raise MigrationError(Code.INVALID_INPUT)
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

    # Stand-in sign-in. A fixture that declares none of these accepts anything, as before.

    def check_address(self, side, typed):
        """A fixture may name the address each system answers on; any other is unreachable."""
        expected = self.data.get("addresses", {}).get(side)
        host = typed.strip().lower().removeprefix("https://").split(":")[0].rstrip("/")
        if expected and host != expected.lower():
            raise MigrationError(Code.CONNECTION)

    def check_sign_in(self, kind, secret, server=None):
        """A fixture may declare the secret a sign-in expects (any other is refused), or a
        list of secrets it refuses (any other is accepted). `server` names the one server
        a server sign-in is handed to."""
        expected = self.data.get("sign_in", {}).get(kind)
        refused = secret in expected if isinstance(expected, list) else secret != expected
        if expected is not None and refused:
            raise MigrationError(Code.AUTH)

    def tls_identity(self, side):
        """A stable stand-in for the certificate digest of one address."""
        return hashlib.sha256(self.data[side]["identity"].encode()).hexdigest()

    def server_digests(self, side):
        """A stable stand-in for each server's host key digest. A fixture may give a server
        another seed, to stand for one that was rebuilt."""
        seeds = self.data.get("server_seeds", {})
        return [
            (
                node["id"],
                hashlib.sha256(str(seeds.get(node["id"], node["id"])).encode()).hexdigest(),
            )
            for node in self.data[side]["nodes"]
        ]

    def server_keys(self, side):
        """Stand-in host keys, in the form an administrator sees them (`SHA256:...`)."""
        return [
            (name, "SHA256:" + base64.b64encode(bytes.fromhex(digest)).decode().rstrip("="))
            for name, digest in self.server_digests(side)
        ]

    @property
    def stage_delay(self):
        """Seconds a client may pause per stage so a person can watch a stand-in run."""
        return float(self.data.get("stage_delay", 0))

    def version_check(self):
        from ..versions import evaluate

        source = self.data["source"]
        destination = self.data["destination"]
        source_versions = source.get("node_versions") or {
            n["id"]: source["version"] for n in source["nodes"]
        }
        destination_versions = destination.get("node_versions") or {
            n["id"]: destination["version"].removesuffix("-preview") for n in destination["nodes"]
        }
        return evaluate(
            source_versions,
            destination_versions,
            destination.get("migration_api", "present"),
            "Simulator node versions",
        )

    def destination_topology(self):
        self._fault("discover")
        destination = self.data["destination"]
        return destination["id"], destination["identity"]

    def destination_cluster(self, *, fresh: bool = False):
        return self.discover()[1]

    def discover(self):
        self._fault("discover")

        def cluster(scope):
            data = dict(self.data[scope])
            data.pop("node_versions", None)
            data.pop("migration_api", None)
            data.pop("config", None)
            data.pop("hosts", None)
            if (
                scope == "destination"
                and self.data.get("placeholder_nodes")
                and not self.data["task"]
            ):
                data["nodes"] = data["nodes"][:1]
                data["buckets"] = [
                    b for b in data["buckets"] if b["node"] == data["nodes"][0]["id"]
                ]
            return Cluster.from_dict(data)

        return cluster("source"), cluster("destination")

    def config_rules(self, scope, kind):
        require(scope in {"source", "destination"} and kind in RULE_TYPES, Code.INVALID_INPUT)
        self._fault("config.read." + scope + "." + kind)
        cluster = self.data[scope]
        # Partition inventory is shared with data-migration preflight.
        rules = (
            cluster.get("partitions", [])
            if kind == "partitions"
            else cluster.get("config", {}).get(kind, [])
        )
        return deepcopy(rules)

    def config_write(self, kind, rule, existing):
        require(kind in RULE_TYPES, Code.INVALID_INPUT)
        destination = self.data["destination"]
        version = destination["version"]
        require(version.startswith(("9.0.", "9.1.1")), Code.UNSUPPORTED)
        body = write_body(kind, rule, existing)
        if kind == "forwarding" and version.startswith("9.0."):
            hosts = [
                destination["id"],
                *destination.get("hosts", []),
                *(n["id"] for n in destination["nodes"]),
            ]
            require(
                str(body.get("host", "")).lower().rstrip(".")
                not in {h.lower().rstrip(".") for h in hosts},
                Code.INVALID_INPUT,
            )
        self._fault("config.write." + kind + ".before")
        if kind == "partitions":
            rules = destination.setdefault("partitions", [])
        else:
            rules = destination.setdefault("config", {}).setdefault(kind, [])
        current = indexed(rules).get(rule["name"])
        require(current == existing, Code.INVENTORY_CHANGED)
        if kind == "forwarding":
            if body.pop("acceptCert", False):
                certificates = self.data.setdefault("trusted_forwarder_hosts", [])
                if body["host"] not in certificates:
                    certificates.append(body["host"])
            body.pop("testConnection", None)
            if "transportProtocol" in body:
                body["transportProtocol"] = body["transportProtocol"].lower()
        if existing is None:
            if kind != "forwarding":
                raw = bytearray(hashlib.md5(rule["name"].encode(), usedforsecurity=False).digest())
                raw[6] = raw[6] & 0x0F | 0x30
                raw[8] = raw[8] & 0x3F | 0x80
                body["id"] = str(uuid.UUID(bytes=bytes(raw)))
            rules.append(body)
        else:
            rules[rules.index(current)] = body
        self._trace(
            "config.write",
            type=kind,
            name=rule["name"],
            action="create" if existing is None else "update",
        )
        self._fault("config.write." + kind + ".after")

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

    def count(self, scope, window, bucket_keys):
        self._fault("query." + scope)
        if scope == "source":
            events = [e for e in self.data["source_events"] if e["bucket"] in bucket_keys]
        elif scope == "baseline":
            events = self.data["destination_events"]
        elif scope == "destination":
            events = self.data["destination_events"]
        elif scope == "ingest":
            # The engine subtracts the requested run's baseline. There is no global
            # "current run" cohort: the API's latest task may belong to a later window.
            events = [e for e in self.data["destination_events"] if e.get("cohort") == "ingest"]
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

    def source_growth(self):
        """Growth of the source's active buckets: quiet unless the fixture's `source_growth`
        says otherwise; a `null` byte figure means it could not be observed."""
        growth = self.data.get("source_growth", {"bytes": 0, "nodes": [], "window_seconds": 5})
        size = growth["bytes"]
        require(size is None or type(size) is int and size >= 0, Code.SCHEMA)
        return dict(growth)

    def test_mover(self):
        self._trace("test_mover")
        self._fault("test_mover")

    def test_source_credentials(self, node):
        self._trace("test_source_credentials", node=node)
        self._fault("source_credentials." + node)

    def _assigned_node(self, bucket_id, destination):
        """The fixture's deterministic assignment, not the appliance's hash."""
        return destination.nodes[int(digest(bucket_id)[:8], 16) % len(destination.nodes)].id

    def start(self, window, bucket_keys):
        self._trace("start", window=asdict(window))
        self._fault("start.before")
        require(self.data["task"] is None or self.data["task"]["status"] != "running", Code.BUSY)
        source, dest = self.discover()
        # The real API accepts dates, not a list of bucket IDs.
        if not dest.preview:
            bucket_keys = tuple(b.key for b in source.buckets if b.selected(window))
        self.data["task"] = {
            "id": f"sim-{len(self.data['history']) + 1}",
            "status": "running",
            "completed": [],
            "skipped": [],
            "bytes_transferred": 0,
            "keys": list(bucket_keys),
            "window": asdict(window),
            "source": self.data["source"]["id"],
            "destination": self.data["destination"]["id"],
        }
        self.data["poll"] = 0
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
            failed = set(self.data.get("failed_nodes", []))
            if task.get("failed_node"):
                failed = {task["failed_node"]}
            default_status = (
                "failed"
                if node["id"] in failed and task["status"] != "running"
                else "completed"
                if failed and task["status"] == "failed"
                else task["status"]
            )
            status = self.data.get("node_statuses", {}).get(node["id"], default_status)
            if "paced_done" in task:
                # A paced task reports what each server still has to fetch, as 9.0 does.
                mine = [
                    k
                    for k in task["keys"]
                    if nodes[int(digest(k.split("/")[-1])[:8], 16) % len(nodes)]["id"] == node["id"]
                ]
                waiting = [k for k in task["keys"][task["paced_done"] :] if k in mine]
                scheduled: dict[str, list[str]] = {}
                totals: dict[str, int] = {}
                for k in mine:
                    totals[k.split("/")[0]] = totals.get(k.split("/")[0], 0) + 1
                    scheduled.setdefault(k.split("/")[0], [])
                for k in waiting:
                    scheduled[k.split("/")[0]].append(k.split("/")[-1])
                outcomes.append(
                    NodeTransfer(
                        node["id"],
                        status,
                        completed,
                        skipped,
                        size,
                        None,
                        {name: len(ids) for name, ids in scheduled.items()},
                        totals,
                        task.get("paced_elapsed"),
                        {name: tuple(ids) for name, ids in scheduled.items()},
                    )
                )
                continue
            outcomes.append(NodeTransfer(node["id"], status, completed, skipped, size))
        return Transfer(
            task["id"],
            task["status"],
            tuple(task["completed"]),
            tuple(task["skipped"]),
            task["bytes_transferred"],
            tuple(outcomes),
            task.get("source", ""),
            task.get("destination", ""),
            Window(**task["window"]) if task.get("window") else None,
            tuple(task.get("keys", ())),
            task["paced_elapsed"]
            if "paced_elapsed" in task
            else self.data.get("poll", 0) * self.data.get("api_poll_seconds", 1),
            sum(
                b["size"]
                for b in self.data["source"]["buckets"]
                if b["node"] + "/" + b["id"] in task.get("keys", ())
            ),
        )

    def transfer(self):
        self._fault("poll.before")
        task = self.data["task"]
        if task and task.get("offline_until"):
            if time.time() < task["offline_until"]:
                raise MigrationError(Code.CONNECTION)
            task["offline_until"] = None
        if task and task["status"] == "running":
            self.data["poll"] += 1
            if self.data.get("pace"):
                self._advance_paced(task, self.data["pace"])
            elif self.data["poll"] >= self.data.get("complete_after_polls", 1):
                source, dest = self.discover()
                limit = self.data.get("cancel_after")
                copied = 0
                for key in task["keys"]:
                    if limit is not None and copied >= limit:
                        break
                    outcome = self._deliver(task, key, source, dest)
                    if outcome == "failed":
                        self._close_history()
                        self._persist()
                        return self._transfer()
                    if outcome == "copied":
                        copied += 1
                self._finish(task, limit is not None and copied >= limit)
        return self._transfer()

    def _finish(self, task, cancelled=False):
        task["status"] = (
            "cancelled"
            if cancelled
            else "failed"
            if self.data.get("failed_nodes")
            else self.data.get("terminal_status", "completed")
        )
        self._close_history()
        self.data["destination_events"].extend(self.data.get("ingest_during_run", []))
        self._persist()
        self._fault("poll.after_complete")

    def _deliver(self, task, key, source, dest):
        """Settle one bucket of a running task: `copied`, `present`, `skipped` or `failed`."""
        b = next(b for b in source.buckets if b.key == key)
        once = self.data.get("skip_buckets_once", [])
        if key in once:
            once.remove(key)  # Skipped this time only, so a later copy of it arrives.
            task["skipped"].append(key)
            return "skipped"
        if key in self.data.get("skip_buckets", []):
            task["skipped"].append(key)
            return "skipped"
        node = self._assigned_node(b.id, dest)
        if not dest.preview:
            existing = [item for item in dest.buckets if item.id == b.id and item.node == node]
            if existing:
                if all(item.registered and item.complete for item in existing):
                    task["completed"].append(key)
                    return "present"
                task["skipped"].append(key)
                return "skipped"
        if node in self.data.get("failed_nodes", []):
            task["skipped"].append(key)
            return "skipped"
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
            return "failed"
        if not dest.preview:
            self.data["destination"]["buckets"].append(asdict(copy))
        short = self.data.get("short_events", {}).get(key, 0)
        for e in self.data["source_events"]:
            if e["bucket"] == key:
                arrived = {**e, "cohort": "migration"}
                if short:
                    # A day that arrives short: some entries of this bucket never land.
                    lost = min(short, arrived.get("multiplicity", 1) - 1)
                    arrived["multiplicity"] = arrived.get("multiplicity", 1) - lost
                    short -= lost
                self.data["destination_events"].append(arrived)
        task["completed"].append(key)
        task["bytes_transferred"] += b.size
        for n in self.data["destination"]["nodes"]:
            if n["id"] == node:
                n["used"] += b.size
                n["repository_available"] -= b.size
        return "copied"

    def _advance_paced(self, task, pace):
        """Progress by the wall clock instead of by poll count, so a person can watch it.

        `pace` is `{"bytes_per_second": n, "time_scale": n}`: the task's own clock runs
        `time_scale` times faster than real time. Optional fixture knobs: `fail_at`
        (`{"fraction", "node", "once"}`: the task fails by itself), `stall`
        (`{"at_fraction", "seconds"}`: nothing arrives for that long on the task's clock)
        and `offline` (`{"at_fraction", "seconds"}`: status reads fail for real seconds).
        """
        now = time.time()
        started = task.setdefault("paced_started", now)
        elapsed = (now - started) * float(pace.get("time_scale", 1))
        task["paced_elapsed"] = elapsed
        source, dest = self.discover()
        sizes = {b.key: b.size for b in source.buckets}
        total = sum(sizes[key] for key in task["keys"]) or 1
        rate = float(pace["bytes_per_second"])
        work = elapsed
        stall = self.data.get("stall")
        if stall:
            begins = stall["at_fraction"] * total / rate
            if elapsed > begins:
                work = max(begins, elapsed - stall["seconds"])
        budget = work * rate
        done = task.setdefault("paced_done", 0)
        spent = task.setdefault("paced_spent", 0)
        fail = self.data.get("fail_at")
        offline = self.data.get("offline")
        while done < len(task["keys"]):
            if fail and spent >= fail["fraction"] * total:
                task["status"] = "failed"
                task["failed_node"] = fail.get("node")
                if fail.get("once", True):
                    del self.data["fail_at"]
                break
            if offline and spent >= offline["at_fraction"] * total:
                task["offline_until"] = now + offline["seconds"]
                del self.data["offline"]
                offline = None
            key = task["keys"][done]
            if spent + sizes[key] > budget:
                break
            before = task["bytes_transferred"]
            outcome = self._deliver(task, key, source, dest)
            task["bytes_transferred"] = before  # Recomputed below from what was fetched.
            done += 1
            task["paced_done"] = done
            if outcome == "failed":
                break
            if outcome == "copied":
                spent += sizes[key]
        task["paced_spent"] = spent
        if task["status"] != "running":
            task["bytes_transferred"] = spent
            self._close_history()
            self._persist()
            return
        if done >= len(task["keys"]):
            task["bytes_transferred"] = spent
            self._finish(task)
            return
        current = sizes[task["keys"][done]]
        task["bytes_transferred"] = int(spent + max(0, min(current, budget - spent)))
        self._persist()
        if task.get("offline_until"):
            raise MigrationError(Code.CONNECTION)

    def _close_history(self):
        """Once finished, history names only the buckets the task actually transferred."""
        entry = self.data["history"][-1]
        entry["complete"] = not self.data["task"]["skipped"]
        entry["bucket_ids"] = [k.split("/")[-1] for k in self.data["task"]["completed"]]

    def cancel(self):
        self._trace("cancel")
        self._fault("cancel.before")
        if self.data["task"]:
            self.data["task"]["status"] = "cancelled"
            self._close_history()
        self._persist()
        self._fault("cancel.after")

    def attest_transfer(self, bucket):
        self._fault("attest")
        task = self.data["task"]
        if self.data["destination"]["version"].startswith("9.1.1"):
            return task is not None and bucket.key in task["completed"]
        copies = [b for b in self.data["destination"]["buckets"] if b["id"] == bucket.id]
        return (
            bool(copies)
            and bucket.manifest is not None
            and all(
                copy["complete"]
                and copy.get("registered", True)
                and copy["manifest"] == bucket.manifest
                for copy in copies
            )
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
        target = next(n for n in self.data["destination"]["nodes"] if n["id"] == node)
        target["service"] = "running"
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
