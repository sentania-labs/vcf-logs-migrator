"""Real appliances behind the Environment and ConfigEnvironment ports.

Source: 8.18 Logs REST on 9543 (version, partitions, per-day counts) and root SSH to every
cluster member for the bucket listing, directory sizes, free space and service state.
Destination: 9.0 Logs REST (data-migrations test/start/status/cancel, partitions, counts,
the four config rule types) or 9.1.1 through the VCF Operations token exchange. Optional
root SSH to the 9.0 destination adds its node list, repository budget and bucket inventory.

Everything here is read-only except the three operator-gated writes the engine asks for:
start, cancel and config writes. Nothing stops a service or deletes a bucket.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..errors import Accepted, Code, MigrationError, QueryTimeout, require
from ..models import Bucket, Cluster, Counts, Node, Partition, Window
from ..ports import History, NodeTransfer, SystemClock, Transfer
from ..security import Credentials, Secret
from ..trust import Identity, TrustRequired, TrustStore, config_dir, probe_ssh, probe_tls
from .config import RestConfigEnvironment
from .rest import HTTPS, Logs90, Logs911, split_endpoint
from .ssh import NodeSSH, ReadOperation

UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
PAIR = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*?)(?=,\s*[A-Za-z_][A-Za-z0-9_]*\s*[=:]|\s*[\]})]*\s*$)"
)
# Real 8.18.7 lines: `{partition=, id=<uuid>, status=archived created=... size=...
# cmd=[startTime=... endTime=... messages=... generation=...]}`. Only the head is comma
# separated; the rest is space separated, and a bucket sealed by a restart carries
# `cmd=[NULL]` with no time metadata.
CMD_BLOCK = re.compile(r"\bcmd\s*=\s*\[([^\]]*)\]", re.IGNORECASE)
SPACE_BEFORE_KEY = re.compile(r"(?<=[^,\s])\s+(?=[A-Za-z_][A-Za-z0-9_]*=)")
HOST_ATTRIBUTE = re.compile(r'host\s*=\s*"([^"]+)"')
DISTRIBUTED = re.compile(r"<distributed\b.*?</distributed>", re.DOTALL | re.IGNORECASE)


def _date_label(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


LISTING_KEYS = {
    "id": ("id", "bucketid", "bucket"),
    "status": ("status", "state"),
    "partition": ("partition", "partitionid"),
    "created": ("created", "createtime", "creationtime", "createdtime"),
    "size": ("size", "sizebytes", "bytes"),
    "messages": ("messages", "messagecount", "count", "events"),
    "start": ("starttime", "start", "mintimestamp", "starttimemillis", "from"),
    "end": ("endtime", "end", "maxtimestamp", "endtimemillis", "to"),
}
STATUS_WORDS = {
    "running": "running",
    "in_progress": "running",
    "inprogress": "running",
    "started": "running",
    "starting": "running",
    "pending": "running",
    "scheduled": "running",
    "cancelling": "running",
    "canceling": "running",
    "completed": "completed",
    "complete": "completed",
    "success": "completed",
    "succeeded": "completed",
    "successful": "completed",
    "finished": "completed",
    "done": "completed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "failed": "failed",
    "error": "failed",
    "failure": "failed",
    "idle": "idle",
    "none": "idle",
    "not_started": "idle",
    "notstarted": "idle",
}


@dataclass(frozen=True)
class Connection:
    """Addresses and user names; never secrets. Secrets come from `Credentials`."""

    source_host: str
    destination: str
    source_api_user: str = "admin"
    source_ssh_user: str = "root"
    destination_api_user: str = "admin"
    destination_ssh_user: str = ""
    ops_url: str = ""
    source_api_port: int = 9543
    ssh_port: int = 22
    timeout: float = 60

    def __post_init__(self):
        # Host names are compared with the config file's member list, which is lower-cased.
        object.__setattr__(self, "source_host", self.source_host.strip().lower())
        object.__setattr__(self, "destination", self.destination.strip().lower())

    @property
    def preview(self) -> bool:
        return bool(self.ops_url.strip())

    @property
    def destination_endpoint(self) -> tuple[str, int]:
        return split_endpoint(self.destination, 9543)

    @property
    def ops_endpoint(self) -> tuple[str, int]:
        return split_endpoint(self.ops_url, 443)


@dataclass
class ListedBucket:
    id: str
    status: str
    partition: str = ""
    created: int | None = None
    size: int | None = None
    messages: int | None = None
    start: int | None = None
    end: int | None = None
    keys: tuple[str, ...] = ()


@dataclass
class NodeInventory:
    host: str
    service: str
    capacity: int | None
    used: int | None
    available: int | None
    buckets: list[ListedBucket]
    usage: dict[str, int] = field(default_factory=dict)
    listing_keys: tuple[str, ...] = ()


# Parsers --------------------------------------------------------------------------------


def parse_time(value: str) -> int | None:
    text = value.strip().strip('"')
    if not text or text.lower() in {"null", "none", "-", "n/a"}:
        return None
    if re.fullmatch(r"-?\d+", text):
        number = int(text)
        if number <= 0:
            return None
        return number if number >= 10**11 else number * 1000
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def parse_int(value: str) -> int | None:
    text = value.strip().strip('"').replace(",", "")
    if re.fullmatch(r"\d+", text):
        return int(text)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?", text, re.IGNORECASE)
    if match:
        scale = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[match.group(2).upper()]
        return int(float(match.group(1)) * scale)
    return None


def normalise_listing_line(line: str) -> str:
    """Make a real space-separated `bucket-index` line look like the comma-separated shape."""

    def unwrap(match: re.Match) -> str:
        inner = match.group(1).strip()
        return "" if inner.upper() in {"", "NULL"} else inner

    return SPACE_BEFORE_KEY.sub(", ", CMD_BLOCK.sub(unwrap, line)).rstrip(", ")


def parse_listing(text: str) -> list[ListedBucket]:
    """`bucket-index show` lines to buckets. Key names vary by build; values are matched by
    meaning and unknown keys are kept by name (never value) for the self-test report."""
    buckets: list[ListedBucket] = []
    for line in text.splitlines():
        pairs = {k.lower(): v.strip() for k, v in PAIR.findall(normalise_listing_line(line))}
        if not pairs:
            continue
        found = {}
        for name, aliases in LISTING_KEYS.items():
            for alias in aliases:
                if alias in pairs:
                    found[name] = pairs[alias]
                    break
        ident = found.get("id", "")
        match = UUID.search(ident.lower()) or UUID.search(line.lower())
        if match is None or "status" not in found:
            continue
        status = found["status"].strip().strip('"').lower()
        if status not in {"active", "archived", "inactive"}:
            status = "active" if "active" in status else "archived"
        bucket = ListedBucket(
            id=match.group(0),
            status=status,
            partition=found.get("partition", "").strip().strip('"'),
            created=parse_time(found.get("created", "")),
            size=parse_int(found.get("size", "")),
            messages=parse_int(found.get("messages", "")),
            start=parse_time(found.get("start", "")),
            end=parse_time(found.get("end", "")),
            keys=tuple(sorted(pairs)),
        )
        buckets.append(bucket)
    return buckets


def parse_usage(text: str) -> dict[str, int]:
    """`du -sB1 <store>/<id>` lines to bytes per bucket id."""
    usage = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        match = UUID.search(parts[-1].lower())
        if match:
            usage[match.group(0)] = int(parts[0])
    return usage


def parse_space(text: str) -> tuple[int | None, int | None, int | None]:
    """`df -B1 --output=size,used,avail` to (size, used, avail)."""
    for line in reversed(text.splitlines()):
        parts = line.split()
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
            return int(parts[0]), int(parts[1]), int(parts[2])
    return None, None, None


def parse_members(text: str, fallback: str) -> list[str]:
    """Hosts named in the <distributed> section of loginsight-config.xml."""
    match = DISTRIBUTED.search(text)
    hosts: list[str] = []
    if match:
        for host in HOST_ATTRIBUTE.findall(match.group(0)):
            host = host.strip().lower().rstrip(".")
            if host and host not in hosts and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", host):
                hosts.append(host)
    return hosts or [fallback]


def parse_partitions(response) -> list[dict]:
    if isinstance(response, dict):
        for key in ("partitions", "items", "results"):
            if isinstance(response.get(key), list):
                response = response[key]
                break
    require(isinstance(response, list), Code.SCHEMA)
    require(all(isinstance(p, dict) for p in response), Code.SCHEMA)
    return response


def partition_models(rules: list[dict]) -> tuple[Partition, ...]:
    models = []
    for index, rule in enumerate(rules):
        name = str(rule.get("name") or "").strip() or "Default"
        criteria = ""
        for key in ("criteria", "filter", "routingFilter", "query"):
            if isinstance(rule.get(key), str):
                criteria = rule[key]
                break
        retention = 0
        for key in ("retentionPeriodSeconds", "retention_seconds", "retentionSeconds"):
            if type(rule.get(key)) is int:
                retention = rule[key]
                break
        order = 0
        for key in ("order", "priority"):
            if type(rule.get(key)) is int:
                order = rule[key]
                break
        ident = rule.get("id")
        models.append(
            Partition(
                re.sub(r"[^A-Za-z0-9._:-]", "_", name)[:128],
                ident if isinstance(ident, str) else "",
                criteria,
                max(0, retention),
                max(0, order if order else index),
            )
        )
    return tuple(models)


def _walk(value, depth=0):
    if depth > 6:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, depth + 1)


def _status_word(value) -> str | None:
    if not isinstance(value, str):
        return None
    word = value.strip().lower().replace("-", "_").replace(" ", "_")
    return STATUS_WORDS.get(word)


def _uuids(value) -> tuple[str, ...]:
    found = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                match = UUID.search(item.lower())
                if match:
                    found.append(match.group(0))
            elif isinstance(item, dict):
                for key in ("id", "bucketId", "bucket", "name"):
                    if isinstance(item.get(key), str):
                        match = UUID.search(item[key].lower())
                        if match:
                            found.append(match.group(0))
                            break
    return tuple(dict.fromkeys(found))


def _first(mapping: dict, pattern: str, kind=str, exclude: str | None = None):
    regex = re.compile(pattern, re.IGNORECASE)
    excluded = re.compile(exclude, re.IGNORECASE) if exclude else None
    for key, value in mapping.items():
        if excluded is not None and excluded.search(key):
            continue
        if regex.search(key) and (
            (kind is int and type(value) is int)
            or (kind is str and isinstance(value, str))
            or (kind is list and isinstance(value, list))
        ):
            return value
    return None


POSITIVE_BUCKETS = r"(copied|completed|migrated|transferred|succeeded|done)"
NEGATIVE_BUCKETS = (
    r"(not|un(copied|migrated|transferred)|^un|skipped|failed|errored|missing|pending|remaining)"
)
SKIPPED_BUCKETS = r"(skipped|failed|errored|(not|un)[_-]?(copied|migrated|transferred)|missing)"


def _bucket_lists(entry: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(completed, skipped) bucket ids; a key naming the negative wins over the positive."""
    completed = _uuids(_first(entry, POSITIVE_BUCKETS, list, exclude=NEGATIVE_BUCKETS) or [])
    skipped_re = re.compile(SKIPPED_BUCKETS, re.IGNORECASE)
    skipped = _uuids(
        [
            item
            for key, value in entry.items()
            if skipped_re.search(key) and isinstance(value, list)
            for item in value
        ]
    )
    return tuple(b for b in completed if b not in skipped), skipped


def _aggregate(statuses: list[str]) -> str | None:
    """One status for several parts: any part still running means running."""
    statuses = [s for s in statuses if s]
    if not statuses:
        return None
    if "running" in statuses or "unknown" in statuses:
        return "running" if "running" in statuses else "unknown"
    if "failed" in statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    if all(s == "completed" for s in statuses):
        return "completed"
    if all(s == "idle" for s in statuses):
        return "idle"
    return "unknown"


def _node_name(entry: dict) -> str | None:
    for key in ("node", "nodeId", "nodeName", "host", "hostName", "hostname", "name", "ip"):
        value = entry.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value.strip()):
            return value.strip().lower()
    return None


def _node_entries(task: dict) -> list[dict]:
    for key in (
        "nodes",
        "nodeStatuses",
        "nodeStatus",
        "perNode",
        "nodeDetails",
        "taskDetails",
        "tasks",
        "details",
        "hosts",
    ):
        value = task.get(key)
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            return value
        if isinstance(value, dict) and value and all(isinstance(v, dict) for v in value.values()):
            return [{"node": name, **body} for name, body in value.items()]
    return []


def decode_task(response, *, destination: str, now_ms: int) -> Transfer:
    """Lenient decoding of the data-migrations status body. Unknown shapes decode to an
    `unknown` task rather than to idle, so nothing is started on top of them."""
    if response in ({}, [], None):
        return Transfer("", "idle", destination=destination)
    task = response
    if isinstance(task, dict) and isinstance(task.get("tasks"), list):
        tasks = task["tasks"]
        return decode_task(tasks[0] if tasks else {}, destination=destination, now_ms=now_ms)
    if isinstance(task, list):
        # A list is one entry per node or per task; a part still running means running.
        require(all(isinstance(t, dict) for t in task), Code.SCHEMA)
        parts = [decode_task(t, destination=destination, now_ms=now_ms) for t in task]
        status = _aggregate([p.status for p in parts]) or "unknown"
        latest = parts[-1]
        return Transfer(
            latest.id,
            status,
            tuple(dict.fromkeys(k for p in parts for k in p.completed)),
            tuple(dict.fromkeys(k for p in parts for k in p.skipped)),
            (
                sum(p.bytes_transferred for p in parts)
                if all(p.bytes_transferred is not None for p in parts)
                else None
            ),
            tuple(n for p in parts for n in p.nodes),
            latest.source,
            destination,
            latest.window,
            (),
            latest.elapsed_seconds,
            latest.total_bytes,
        )
    require(isinstance(task, dict), Code.SCHEMA)
    for key in ("dataMigration", "migration", "task", "result", "data"):
        if isinstance(task.get(key), dict):
            task = task[key]
    status = None
    for key in ("status", "state", "migrationStatus", "taskStatus", "phase"):
        status = _status_word(task.get(key))
        if status:
            break
    if status is None:
        # No top-level status: every nested one counts, and any running part wins.
        status = _aggregate(
            [_status_word(value) for key, value in _walk(task) if key in {"status", "state"}]
        )
    if status is None:
        content = [v for v in task.values() if v not in (None, "", [], {})]
        if not content:
            return Transfer("", "idle", destination=destination)
        status = "unknown"
    ident = ""
    for key in ("id", "taskId", "migrationId", "requestId", "jobId"):
        if isinstance(task.get(key), (str, int)) and task.get(key) != "":
            ident = str(task[key])
            break
    completed, skipped = _bucket_lists(task)
    nodes = []
    for entry in _node_entries(task):
        name = _node_name(entry)
        if name is None:
            continue
        node_status = None
        for key in ("status", "state"):
            node_status = _status_word(entry.get(key))
            if node_status:
                break
        node_completed, node_skipped = _bucket_lists(entry)
        node_bytes = _first(
            entry,
            r"(transferred|copied|migrated).*bytes|bytes.*(transferred|copied|migrated)",
            int,
        )
        if node_bytes is None:
            node_bytes = _first(entry, r"bytes", int)
        progress = entry.get("progress")
        if type(progress) not in (int, float) or not 0 <= progress <= 100:
            progress = None
        scheduled = entry.get("scheduledBuckets")
        scheduled = (
            {str(k): len(v) for k, v in scheduled.items() if isinstance(v, list)}
            if isinstance(scheduled, dict)
            else None
        )
        totals = entry.get("totalBucketsCount")
        totals = (
            {str(k): v for k, v in totals.items() if type(v) is int and v >= 0}
            if isinstance(totals, dict)
            else None
        )
        start = parse_time(str(entry.get("startTime"))) if entry.get("startTime") else None
        end = parse_time(str(entry.get("endTime"))) if entry.get("endTime") else None
        reference = end if end and node_status != "running" else now_ms
        node_elapsed = (reference - start) / 1000 if start and reference >= start else None
        nodes.append(
            NodeTransfer(
                name,
                node_status or status,
                node_completed,
                node_skipped,
                node_bytes,
                progress,
                scheduled,
                totals,
                node_elapsed,
            )
        )
    completed = tuple(dict.fromkeys(completed + tuple(k for n in nodes for k in n.completed)))
    skipped = tuple(dict.fromkeys(skipped + tuple(k for n in nodes for k in n.skipped)))
    if nodes:
        # The task is finished only when every node is; a running node keeps it running.
        status = _aggregate([status, *(n.status for n in nodes)]) or status
    transferred = _first(
        task, r"(transferred|copied|migrated).*bytes|bytes.*(transferred|copied|migrated)", int
    )
    if transferred is None and nodes and all(n.bytes_transferred is not None for n in nodes):
        transferred = sum(n.bytes_transferred for n in nodes)
    total = _first(task, r"(total|planned|expected).*bytes|bytes.*(total|planned|expected)", int)
    window = None
    start_ms = task.get("bucketStartTimeMillis", task.get("bucketStartTime"))
    end_ms = task.get("bucketEndTimeMillis", task.get("bucketEndTime"))
    if type(start_ms) is int and type(end_ms) is int and 0 <= start_ms < end_ms:
        window = Window(start_ms, end_ms)
    elapsed = None
    started = None
    for key, value in task.items():
        if re.search(
            r"^(taskCreateTime|start|started|created|began|submitted)", key, re.IGNORECASE
        ) and not key.startswith("bucket"):
            started = parse_time(str(value)) if isinstance(value, (str, int)) else None
            if started:
                break
    if started:
        finished = None
        for key, value in task.items():
            if re.search(
                r"^(taskEndTime|end|ended|finished|completed|updated|last)", key, re.IGNORECASE
            ) and not key.startswith("bucket"):
                finished = parse_time(str(value)) if isinstance(value, (str, int)) else None
                if finished:
                    break
        reference = finished if finished and status != "running" else now_ms
        if reference >= started:
            elapsed = (reference - started) / 1000
    if elapsed is None:
        elapsed = max(
            (n.elapsed_seconds for n in nodes if n.elapsed_seconds is not None), default=None
        )
    source = task.get("hostName") if isinstance(task.get("hostName"), str) else ""
    return Transfer(
        ident,
        status,
        completed,
        skipped,
        transferred,
        tuple(nodes),
        source,
        destination,
        window,
        (),
        elapsed,
        total,
        parse_time(str(task.get("taskCreateTime"))) if task.get("taskCreateTime") else None,
        (
            task["progress"]
            if type(task.get("progress")) in (int, float) and 0 <= task["progress"] <= 100
            else None
        ),
    )


def observed_keys(response, depth: int = 0) -> list[str]:
    """Key names of a response, for the self-test's qualification notes. No values."""
    names: list[str] = []
    if isinstance(response, dict):
        for key, value in response.items():
            names.append(str(key))
            names += (
                [f"{key}.{inner}" for inner in observed_keys(value, depth + 1)] if depth < 2 else []
            )
    elif isinstance(response, list) and response:
        names += (
            [f"[].{inner}" for inner in observed_keys(response[0], depth + 1)] if depth < 2 else []
        )
    return names


def cluster_id(identity: str) -> str:
    """The ledger key for an appliance: its TLS identity, not the spelling of its address,
    so a VIP, a node name and an IP of the same endpoint share one record."""
    return "logs-" + identity[:16]


# Environment -----------------------------------------------------------------------------


class LiveEnvironment:
    """Implements `ports.Environment` and `config.ConfigEnvironment` against real hosts."""

    mode = "live"
    attribution = False  # Live counts carry no per-event identities; ingest is not attributed.

    def __init__(
        self,
        connection: Connection,
        credentials: Credentials,
        trust: TrustStore | None = None,
        clock=None,
    ):
        self.connection = connection
        self.credentials = credentials
        self.trust = trust or TrustStore()
        self.clock = clock or SystemClock()
        self.notes: dict[str, object] = {}
        self.members: list[str] = []
        self.destination_members: list[str] = []
        self.source_client: Logs90 | None = None
        self.destination_client: Logs90 | None = None
        self.config_env: RestConfigEnvironment | None = None
        self._pending: dict[str, str] = {}
        self._inventory_cache = {}
        self.on_progress = None

    # Identity trust ----------------------------------------------------------------------

    def _check(self, identity: Identity, key_line: str | None, unknown: list[Identity]):
        state = self.trust.check(identity)
        if state == "trusted":
            return
        if state == "changed":
            self.notes["identity_changed"] = identity.name
            if identity.kind == "ssh":
                from .ssh_errors import SSHFailure

                raise SSHFailure("host")
            raise MigrationError(Code.AUTH)
        unknown.append(identity)
        if key_line:
            self._pending[identity.name] = key_line

    def key_line(self, identity: Identity) -> str | None:
        return self._pending.get(identity.name)

    def open(self) -> LiveEnvironment:
        """Probe every identity, then build the clients. Raises TrustRequired first."""
        connection = self.connection
        unknown: list[Identity] = []
        tls_source = (connection.source_host, connection.source_api_port)
        tls_destination = connection.destination_endpoint
        # `stage` names the probe in flight so a failure can say which host, without any
        # remote text.
        self.notes["stage"] = f"tls {tls_source[0]}:{tls_source[1]}"
        self._check(probe_tls(*tls_source), None, unknown)
        self.notes["stage"] = f"tls {tls_destination[0]}:{tls_destination[1]}"
        self._check(probe_tls(*tls_destination), None, unknown)
        if connection.preview:
            ops = connection.ops_endpoint
            self.notes["stage"] = f"tls {ops[0]}:{ops[1]}"
            self._check(probe_tls(*ops), None, unknown)
        self.notes["stage"] = f"ssh {connection.source_host}:{connection.ssh_port}"
        identity, line = probe_ssh(connection.source_host, connection.ssh_port)
        self._check(identity, line, unknown)
        if self._destination_ssh_enabled():
            self.notes["stage"] = f"ssh {tls_destination[0]}:{connection.ssh_port}"
            identity, line = probe_ssh(tls_destination[0], connection.ssh_port)
            self._check(identity, line, unknown)
        if unknown:
            raise TrustRequired(unknown)
        self.notes["stage"] = "source members"
        self.source_client = self._logs_client(tls_source, "source_api", connection.source_api_user)
        if connection.preview:
            logs = self._https(tls_destination)
            ops = self._https(connection.ops_endpoint)
            self.destination_client = Logs911(logs, ops, self.credentials.get("ops_token"))
        else:
            self.destination_client = self._logs_client(
                tls_destination, "destination_api", connection.destination_api_user
            )
        # Cluster members come from the typed member's config; each needs its own SSH trust.
        self.members = self.source_members()
        for host in self.members:
            if host != connection.source_host.lower():
                identity, line = probe_ssh(host, connection.ssh_port)
                self._check(identity, line, unknown)
        if self._destination_ssh_enabled():
            self.destination_members = self._members(
                tls_destination[0], "destination_ssh", connection.destination_ssh_user
            )
            for host in self.destination_members:
                if host != tls_destination[0].lower():
                    identity, line = probe_ssh(host, connection.ssh_port)
                    self._check(identity, line, unknown)
        if unknown:
            raise TrustRequired(unknown)
        self.notes.pop("stage", None)
        return self

    def _https(self, endpoint: tuple[str, int]) -> HTTPS:
        host, port = endpoint
        pin = self.trust.known(Identity("tls", f"{host}:{port}", ""))
        require(pin is not None, Code.AUTH)
        return HTTPS(
            f"https://{host}:{port}", certificate_sha256=pin, timeout=self.connection.timeout
        )

    def _logs_client(self, endpoint, credential: str, username: str) -> Logs90:
        return Logs90(self._https(endpoint), username, self.credentials.get(credential))

    def _ssh(self, host: str, credential: str, username: str) -> NodeSSH:
        name = f"{host}:{self.connection.ssh_port}"
        pin = self.trust.known(Identity("ssh", name, ""))
        require(pin is not None, Code.AUTH)
        return NodeSSH(
            host,
            username,
            self.credentials.get(credential),
            pin,
            str(self.trust.known_hosts),
            port=self.connection.ssh_port,
            timeout=self.connection.timeout,
        )

    def _destination_ssh_enabled(self) -> bool:
        return (
            not self.connection.preview
            and bool(self.connection.destination_ssh_user.strip())
            and self.credentials.has("destination_ssh")
        )

    # Reads -------------------------------------------------------------------------------

    def source_version(self) -> str:
        require(self.source_client is not None, Code.STATE)
        return self.source_client.version()

    def destination_version(self) -> str:
        require(self.destination_client is not None, Code.STATE)
        if self.connection.preview:
            try:
                return self.destination_client.version()
            except MigrationError as error:
                if error.code == Code.AUTH:
                    raise
                # The 9.1.1 log-processor does not necessarily serve /api/v1/version; the
                # operator chose the token path, which is 9.1.1's only route in.
                self.notes["destination_version_assumed"] = True
                return "9.1.1"
        return self.destination_client.version()

    def _members(self, host: str, credential: str, username: str) -> list[str]:
        # An unreadable member list fails closed: the API copies from every member, so a
        # plan and ledger built from one node would be short.
        text = self._ssh(host, credential, username).read(ReadOperation.MEMBERS)
        return parse_members(text, host.lower())

    def source_members(self) -> list[str]:
        return self._members(
            self.connection.source_host, "source_ssh", self.connection.source_ssh_user
        )

    def node_inventory(
        self, host: str, credential: str = "source_ssh", username: str | None = None
    ) -> NodeInventory:
        cache_key = (host, credential, username)
        cached = self._inventory_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        ssh = self._ssh(host, credential, username or self.connection.source_ssh_user)
        service = ssh.read(ReadOperation.SERVICE).strip().lower()
        capacity, used, available = parse_space(ssh.read(ReadOperation.SPACE))
        listing = ssh.read(ReadOperation.INVENTORY)
        buckets = parse_listing(listing)
        if self.on_progress:
            members = self.members if credential == "source_ssh" else self.destination_members
            position = members.index(host) + 1 if host in members else 1
            self.on_progress(
                f"node {position}/{len(members) or 1} ({host}): sizing {len(buckets):,} buckets"
            )
        usage = parse_usage(ssh.read(ReadOperation.USAGE))
        keys = tuple(sorted({k for b in buckets for k in b.keys}))
        result = NodeInventory(
            host,
            "running"
            if service == "active"
            else "stopped"
            if service in {"inactive", "failed"}
            else "unknown",
            capacity,
            used,
            available,
            buckets,
            usage,
            keys,
        )
        self._inventory_cache[cache_key] = (time.monotonic(), result)
        return result

    def _cluster_from_nodes(
        self, ident: str, version: str, inventories: list[NodeInventory], partitions, identity: str
    ) -> Cluster:
        names = {p.id: p.name for p in partitions}
        buckets = []
        for inventory in inventories:
            for listed in inventory.buckets:
                active = listed.status == "active"
                size = inventory.usage.get(listed.id, listed.size or 0) or 0
                partition = listed.partition
                if partition in names:
                    partition = names[partition]
                partition = re.sub(r"[^A-Za-z0-9._:-]", "_", partition)[:128]
                if partition == "Default":
                    partition = ""
                proven = not active and listed.start is not None and listed.end is not None
                buckets.append(
                    Bucket(
                        listed.id,
                        inventory.host,
                        partition,
                        "active" if active else "archived",
                        size,
                        0 if active else (listed.messages or 0),
                        None if active else listed.start,
                        None if active else listed.end,
                        None,
                        proven,
                        not active,
                        True,
                        listed.created,
                    )
                )
        nodes = tuple(Node(i.host, i.capacity, i.used, i.available, i.service) for i in inventories)
        return Cluster(ident, version, nodes, partitions, (), tuple(buckets), identity, True)

    def source_cluster(self) -> Cluster:
        require(self.source_client is not None, Code.STATE)
        version = self.source_version()
        partitions = partition_models(parse_partitions(self.source_client.partitions()))
        members = self.members or self.source_members()
        with ThreadPoolExecutor(max_workers=max(1, len(members))) as pool:
            inventories = list(pool.map(self.node_inventory, members))
        self.notes["source_listing_keys"] = sorted({k for i in inventories for k in i.listing_keys})
        return self._cluster_from_nodes(
            cluster_id(self.source_client.identity),
            version,
            inventories,
            partitions,
            self.source_client.identity,
        )

    def destination_cluster(self) -> Cluster:
        require(self.destination_client is not None, Code.STATE)
        host, _ = self.connection.destination_endpoint
        version = self.destination_version()
        try:
            partitions = partition_models(parse_partitions(self.destination_client.partitions()))
        except MigrationError:
            if not self.connection.preview:
                raise
            self.notes["destination_partitions_unavailable"] = True
            partitions = ()
        if self._destination_ssh_enabled():
            members = self.destination_members or self._members(
                host, "destination_ssh", self.connection.destination_ssh_user
            )
            with ThreadPoolExecutor(max_workers=max(1, len(members))) as pool:
                inventories = list(
                    pool.map(
                        lambda member: self.node_inventory(
                            member, "destination_ssh", self.connection.destination_ssh_user
                        ),
                        members,
                    )
                )
            return self._cluster_from_nodes(
                cluster_id(self.destination_client.identity),
                version,
                inventories,
                partitions,
                self.destination_client.identity,
            )
        task = self.transfer()
        names = [n.node for n in task.nodes] or [host.lower()]
        nodes = tuple(Node(name, None, None, None, "running") for name in names)
        return Cluster(
            cluster_id(self.destination_client.identity),
            version,
            nodes,
            partitions,
            (),
            (),
            self.destination_client.identity,
            True,
        )

    # ports.Environment -------------------------------------------------------------------

    @contextmanager
    def exclusive(self):
        from ..journal import file_lock

        host, port = self.connection.destination_endpoint
        directory = config_dir() / "locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_lock(Path(directory) / f"{host}-{port}.lock"):
            yield

    def discover(self) -> tuple[Cluster, Cluster]:
        return self.source_cluster(), self.destination_cluster()

    def history(self) -> tuple[History, ...]:
        task = self.transfer()
        if task.status in {"idle", "running", "unknown"} or task.window is None:
            return ()
        return (
            History(
                task.source or self.connection.source_host,
                cluster_id(self.destination_client.identity),
                task.window,
                tuple(n.node for n in task.nodes),
                task.completed,
                not task.skipped,
                task.id or "",
                bool(task.skipped) and not task.completed,
            ),
        )

    def _counts(self, client: Logs90, scope: str, window: Window) -> Counts:
        def progress(start, end):
            if self.on_progress:
                self.on_progress(
                    f"Counting {scope}: {_date_label(start)} to {_date_label(end)} (UTC)"
                )

        client.count_progress = progress
        try:
            daily, complete = client.daily_counts(window)
        except QueryTimeout as error:
            raise QueryTimeout(f"{scope} {error.message}") from None
        finally:
            client.count_progress = None
        return Counts(complete, tuple(sorted(daily.items())), (), scope)

    def count(self, scope: str, window: Window, bucket_keys: tuple[str, ...]) -> Counts:
        require(self.source_client is not None and self.destination_client is not None, Code.STATE)
        if scope == "source":
            return self._counts(self.source_client, scope, window)
        if scope in {"destination", "baseline"}:
            return self._counts(self.destination_client, scope, window)
        if scope == "ingest":
            return Counts(True, (), (), "ingest")
        raise MigrationError(Code.SCHEMA)

    def source_growth(self) -> dict:
        """Compare active directory sizes, never event timestamps or a full second inventory."""
        members = self.members or self.source_members()
        with ThreadPoolExecutor(max_workers=max(1, len(members))) as pool:
            inventories = dict(zip(members, pool.map(self.node_inventory, members), strict=True))
        active = {
            host: [b.id for b in inventory.buckets if b.status == "active"]
            for host, inventory in inventories.items()
        }

        def measure(host):
            ssh = self._ssh(host, "source_ssh", self.connection.source_ssh_user)
            return parse_usage(ssh.active_usage(active[host]))

        with ThreadPoolExecutor(max_workers=max(1, len(members))) as pool:
            before = dict(zip(members, pool.map(measure, members), strict=True))
        sampled = time.monotonic()
        self.clock.sleep(5)

        def observe(host):
            ssh = self._ssh(host, "source_ssh", self.connection.source_ssh_user)
            listing = parse_listing(ssh.read(ReadOperation.INVENTORY))
            if set(active[host]) != {b.id for b in listing if b.status == "active"}:
                raise MigrationError(Code.INCOMPLETE)
            usage = measure(host)
            ids = active[host]
            if any(
                i not in before[host] or i not in usage or usage[i] < before[host][i] for i in ids
            ):
                raise MigrationError(Code.INCOMPLETE)
            return sum(usage[i] - before[host][i] for i in ids)

        with ThreadPoolExecutor(max_workers=max(1, len(members))) as pool:
            changes = list(pool.map(observe, members))
        elapsed = max(1, round(time.monotonic() - sampled))
        return {
            "bytes": sum(changes),
            "nodes": [h for h, n in zip(members, changes, strict=False) if n],
            "window_seconds": elapsed,
        }

    def test_mover(self) -> None:
        require(self.destination_client is not None, Code.STATE)
        self.destination_client.test_migration(
            self.connection.source_host,
            self.connection.source_ssh_user,
            self.credentials.get("source_ssh"),
        )

    def test_source_credentials(self, node: str) -> None:
        self._ssh(node, "source_ssh", self.connection.source_ssh_user).read(ReadOperation.SERVICE)

    def _now_ms(self) -> int:
        return int(datetime.now(UTC).timestamp() * 1000)

    def start(self, window: Window, bucket_keys: tuple[str, ...]) -> Transfer:
        require(self.destination_client is not None, Code.STATE)
        destination = cluster_id(self.destination_client.identity)
        before = self.transfer()
        attempted_at_ms = self._now_ms()
        try:
            response = self.destination_client.start_migration(
                self.connection.source_host,
                self.connection.source_ssh_user,
                self.credentials.get("source_ssh"),
                window,
            )
        except Accepted:
            response = {}
        self.notes["start_response_keys"] = observed_keys(response)
        try:
            decoded = decode_task(response, destination=destination, now_ms=self._now_ms())
        except MigrationError:
            decoded = Transfer("", "unknown", destination=destination)
        if decoded.id and decoded.id != before.id:
            if decoded.status in {"idle", "unknown"}:
                return Transfer(decoded.id, "running", destination=destination, window=window)
            return decoded
        try:
            task = self.transfer_for(window=window, attempted_at_ms=attempted_at_ms)
        except MigrationError:
            task = None
        if task is not None and task.id and task.id != before.id:
            return task
        # The status route shows nothing yet, or still the previous task. Report the new
        # one as running with the requested window so the operator polls rather than
        # resubmits; the ledger row stays open until a task with this window is observed.
        return Transfer(decoded.id, "running", destination=destination, window=window)

    def transfer(self) -> Transfer:
        require(self.destination_client is not None, Code.STATE)
        destination = cluster_id(self.destination_client.identity)
        response = self.destination_client.migration_status()
        self.notes["status_response_keys"] = observed_keys(response)
        return decode_task(response, destination=destination, now_ms=self._now_ms())

    def destination_tasks(self) -> list[Transfer]:
        require(self.destination_client is not None, Code.STATE)
        destination = cluster_id(self.destination_client.identity)
        response = self.destination_client.migration_status()
        self.notes["status_response_keys"] = observed_keys(response)
        if isinstance(response, dict) and isinstance(response.get("tasks"), list):
            return [
                decode_task(item, destination=destination, now_ms=self._now_ms())
                for item in response["tasks"]
            ]
        return [decode_task(response, destination=destination, now_ms=self._now_ms())]

    def transfer_for(self, task_id: str = "", *, window=None, attempted_at_ms=None) -> Transfer:
        """Find exactly one run's task in the 9.0 task list."""
        require(self.destination_client is not None, Code.STATE)
        destination = cluster_id(self.destination_client.identity)
        tasks = self.destination_tasks()
        matches = [
            task
            for task in tasks
            if task.id
            and (
                task.id == task_id
                if task_id
                else window is not None
                and task.window == window
                and attempted_at_ms is not None
                and task.created_at_ms is not None
                and task.created_at_ms >= attempted_at_ms
            )
        ]
        return matches[0] if len(matches) == 1 else Transfer("", "idle", destination=destination)

    def cancel(self) -> None:
        require(self.destination_client is not None, Code.STATE)
        self.destination_client.cancel_migration()

    # config.ConfigEnvironment ------------------------------------------------------------

    def _config(self) -> RestConfigEnvironment:
        require(self.source_client is not None and self.destination_client is not None, Code.STATE)
        if self.config_env is None:
            host, _ = self.connection.destination_endpoint
            self.config_env = RestConfigEnvironment(
                self.source_client,
                self.destination_client,
                self.destination_version(),
                (host, *self.destination_members),
            )
        return self.config_env

    def config_rules(self, scope: str, kind: str) -> list[dict]:
        return self._config().config_rules(scope, kind)

    def config_write(self, kind: str, rule: dict, existing: dict | None) -> None:
        self._config().config_write(kind, rule, existing)


def secret_free(connection: Connection) -> dict:
    """Connection settings for display and reports; the dataclass never holds secrets."""
    return {
        "source_host": connection.source_host,
        "destination": connection.destination,
        "ops_url": connection.ops_url,
        "source_api_user": connection.source_api_user,
        "source_ssh_user": connection.source_ssh_user,
        "destination_api_user": connection.destination_api_user,
        "destination_ssh_user": connection.destination_ssh_user,
    }


__all__ = [
    "Connection",
    "LiveEnvironment",
    "ListedBucket",
    "NodeInventory",
    "Secret",
    "decode_task",
    "observed_keys",
    "parse_listing",
    "parse_members",
    "parse_partitions",
    "parse_space",
    "parse_usage",
    "partition_models",
    "secret_free",
]
