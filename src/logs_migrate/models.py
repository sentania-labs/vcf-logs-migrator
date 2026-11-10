"""Immutable, secret-free evidence. Times are integer Unix milliseconds, ranges half-open."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from .errors import Code, require

ACTIVE_ACK = "i understand you aren't going to copy the latest data"
DAY = 86_400_000
EVERYTHING_END = 2**53


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def parse_time(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, Code.INVALID_INPUT)
        return int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError):
        from .errors import MigrationError

        raise MigrationError(Code.INVALID_INPUT) from None


def selected_message_count(buckets: list[dict]) -> int | None:
    """A total is evidence only when every selected bucket has a count."""
    counts = [bucket.get("messages") for bucket in buckets]
    return None if any(count is None for count in counts) else sum(counts)


class Verification(StrEnum):
    PENDING = "verification_pending"
    SUCCESS = "verified_success"
    DISCREPANCY = "verified_discrepancy"
    INCOMPLETE = "incomplete_verification"


COUNT_BASIS = "selected_groups"
LEGACY_RESULT_NOTE = (
    "This result was recorded by an earlier version; run Verify again to see "
    "selected-group figures."
)


def is_legacy_result(verification: dict) -> bool:
    return "expected_count" in verification and verification.get("count_basis") != COUNT_BASIS


_LABEL = re.compile(r"[A-Za-z0-9._:-]{0,128}")


def _label(value: object, *, empty: bool = False) -> bool:
    """Inventory identifiers can become host names, unit names or arguments; keep them inert."""
    return isinstance(value, str) and bool(_LABEL.fullmatch(value)) and (empty or bool(value))


@dataclass(frozen=True)
class Window:
    start: int
    end: int

    def __post_init__(self):
        require(type(self.start) is int and type(self.end) is int, Code.INVALID_INPUT)
        require(0 <= self.start < self.end, Code.INVALID_INPUT)

    def overlaps(self, other: Window) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Bucket:
    id: str
    node: str
    partition: str
    status: str
    size: int
    messages: int | None
    start: int | None
    end: int | None  # Inclusive appliance max timestamp, not window end.
    manifest: str | None
    bounds_proven: bool = False
    complete: bool = True
    registered: bool = True
    created: int | None = None

    def __post_init__(self):
        require(
            all(type(v) is bool for v in (self.bounds_proven, self.complete, self.registered)),
            Code.SCHEMA,
        )
        require(
            all(
                v is None or type(v) is int and v >= 0 for v in (self.start, self.end, self.created)
            ),
            Code.SCHEMA,
        )
        require(
            bool(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", self.id)), Code.SCHEMA
        )
        require(self.status in {"active", "archived", "inactive"}, Code.SCHEMA)
        require(_label(self.node) and _label(self.partition, empty=True), Code.SCHEMA)
        require(type(self.size) is int and self.size >= 0, Code.SCHEMA)
        require(
            self.messages is None or type(self.messages) is int and self.messages >= 0, Code.SCHEMA
        )
        if self.manifest is not None:
            require(bool(re.fullmatch(r"[0-9a-f]{64}", self.manifest)), Code.SCHEMA)
        if self.start is not None and self.end is not None:
            require(0 <= self.start <= self.end, Code.SCHEMA)

    @property
    def key(self) -> str:
        return f"{self.node}/{self.id}"

    @property
    def undated(self) -> bool:
        """A sealed bucket whose time range the appliance did not report."""
        return self.status != "active" and not (
            self.bounds_proven and self.start is not None and self.end is not None
        )

    def selected(self, window: Window) -> bool:
        # An undated sealed bucket cannot be shown to touch the window, so it is never
        # selected; the plan lists it for the operator instead of failing.
        if self.status == "active" or self.undated:
            return False
        return self.start < window.end and self.end >= window.start


@dataclass(frozen=True)
class Partition:
    name: str
    id: str
    criteria: str
    retention_seconds: int
    order: int = 0

    def __post_init__(self):
        require(_label(self.name) and _label(self.id, empty=True), Code.SCHEMA)
        require(
            isinstance(self.criteria, str)
            and len(self.criteria) <= 1024
            and (self.criteria == "" or self.criteria.isprintable()),
            Code.SCHEMA,
        )
        require(type(self.retention_seconds) is int and self.retention_seconds >= 0, Code.SCHEMA)
        require(type(self.order) is int and self.order >= 0, Code.SCHEMA)


@dataclass(frozen=True)
class Node:
    """Storage figures are None when the appliance did not report them (no SSH, or 9.1.1)."""

    id: str
    capacity: int | None
    used: int | None
    repository_available: int | None
    service: str = "running"

    def __post_init__(self):
        require(_label(self.id), Code.SCHEMA)
        figures = (self.capacity, self.used, self.repository_available)
        require(all(n is None or type(n) is int for n in figures), Code.SCHEMA)
        if self.capacity is not None:
            require(self.capacity > 0, Code.SCHEMA)
            require(self.used is None or 0 <= self.used <= self.capacity, Code.SCHEMA)
            require(
                self.repository_available is None
                or 0 <= self.repository_available <= self.capacity,
                Code.SCHEMA,
            )
        else:
            require(self.used is None or self.used >= 0, Code.SCHEMA)
            require(
                self.repository_available is None or self.repository_available >= 0, Code.SCHEMA
            )
        require(self.service in {"running", "stopped", "unknown"}, Code.SCHEMA)

    @property
    def storage_known(self) -> bool:
        return self.repository_available is not None


@dataclass(frozen=True)
class Cluster:
    id: str
    version: str
    nodes: tuple[Node, ...]
    partitions: tuple[Partition, ...]
    fields: tuple[str, ...]
    buckets: tuple[Bucket, ...]
    identity: str
    history_complete: bool = False

    def __post_init__(self):
        require(_label(self.id) and _label(self.version), Code.SCHEMA)
        require(type(self.history_complete) is bool, Code.SCHEMA)
        require(
            bool(self.nodes) and len({n.id for n in self.nodes}) == len(self.nodes), Code.SCHEMA
        )
        require(len({b.key for b in self.buckets}) == len(self.buckets), Code.SCHEMA)
        require(all(b.node in {n.id for n in self.nodes} for b in self.buckets), Code.SCHEMA)
        require(len({p.name for p in self.partitions}) == len(self.partitions), Code.SCHEMA)

    @property
    def preview(self) -> bool:
        return self.version.startswith("9.1.1")

    @classmethod
    def from_dict(cls, data: dict) -> Cluster:
        return cls(
            **{
                **data,
                "nodes": tuple(Node(**n) for n in data["nodes"]),
                "partitions": tuple(Partition(**p) for p in data["partitions"]),
                "fields": tuple(data["fields"]),
                "buckets": tuple(Bucket(**b) for b in data["buckets"]),
            }
        )


@dataclass(frozen=True)
class Counts:
    complete: bool
    daily: tuple[tuple[str, int], ...]
    # day/SHA256(canonical event + fields) -> multiplicity. Never raw log text.
    identities: tuple[tuple[str, int], ...]
    scope: str

    def __post_init__(self):
        require(type(self.complete) is bool, Code.SCHEMA)
        require(len(dict(self.daily)) == len(self.daily), Code.SCHEMA)
        require(all(type(n) is int and n >= 0 for _, n in self.daily), Code.SCHEMA)
        require(all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) for day, _ in self.daily), Code.SCHEMA)
        require(len(dict(self.identities)) == len(self.identities), Code.SCHEMA)
        require(
            all(
                re.fullmatch(r"\d{4}-\d{2}-\d{2}/[0-9a-f]{64}", key) and type(n) is int and n > 0
                for key, n in self.identities
            ),
            Code.SCHEMA,
        )

    @property
    def identity_complete(self) -> bool:
        totals: dict[str, int] = {}
        for key, n in self.identities:
            totals[key[:10]] = totals.get(key[:10], 0) + n
        return totals == {day: n for day, n in self.daily if n}

    @property
    def total(self) -> int:
        return sum(n for _, n in self.daily)

    @classmethod
    def from_dict(cls, data: dict) -> Counts:
        return cls(
            data["complete"],
            tuple(tuple(v) for v in data["daily"]),
            tuple(tuple(v) for v in data["identities"]),
            data["scope"],
        )


@dataclass(frozen=True)
class Policy:
    attempts: int = 6
    poll_seconds: float = 30.0

    def __post_init__(self):
        require(2 <= self.attempts <= 10000 and self.poll_seconds >= 0, Code.INVALID_INPUT)


def sealed_frontier(buckets: tuple[Bucket, ...]) -> int | None:
    """Where unproven open buckets may begin: the oldest per-node newest sealed end, else None."""
    newest: dict[str, int] = {}
    for b in buckets:
        if b.status != "active" and b.end is not None:
            newest[b.node] = max(newest.get(b.node, 0), b.end)
    ends = []
    for node in {b.node for b in buckets if b.status == "active"}:
        if node not in newest:
            return None
        ends.append(newest[node])
    return min(ends) if ends else None


def last_sealed_day(buckets: tuple[Bucket, ...], zone: str = "America/Chicago") -> str | None:
    """The day before the first active data: its proven start, else the sealed frontier."""
    from datetime import timedelta

    active = [b for b in buckets if b.status == "active"]
    if not active:
        return None
    if any(not b.bounds_proven or b.start is None for b in active):
        frontier = sealed_frontier(buckets)
        if frontier is None:
            return None
        day = datetime.fromtimestamp(frontier / 1000, ZoneInfo(zone)).date()
    else:
        first_active = min(b.start for b in active)
        day = datetime.fromtimestamp(first_active / 1000, ZoneInfo(zone)).date()
    return (day - timedelta(days=1)).isoformat()
