"""Adapters must provide observed evidence, not infer success from a process exit."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from .models import Bucket, Cluster, Counts, Window


@dataclass(frozen=True)
class NodeTransfer:
    node: str
    status: str
    completed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    bytes_transferred: int | None = 0
    progress: float | None = None
    scheduled_buckets: dict[str, int] | None = None
    total_buckets: dict[str, int] | None = None
    elapsed_seconds: float | None = None


@dataclass(frozen=True)
class Transfer:
    id: str
    status: str  # idle, running, completed, cancelled, failed, unknown
    completed: tuple[str, ...] = ()  # Source bucket keys, independently reconciled.
    skipped: tuple[str, ...] = ()
    bytes_transferred: int | None = 0
    nodes: tuple[NodeTransfer, ...] = ()
    # Observed request identity from the destination, never invented from local task state.
    source: str = ""
    destination: str = ""
    window: Window | None = None
    bucket_keys: tuple[str, ...] = ()
    elapsed_seconds: float | None = None
    total_bytes: int | None = None
    created_at_ms: int | None = None
    progress: float | None = None


@dataclass(frozen=True)
class History:
    source: str
    destination: str
    window: Window
    destination_nodes: tuple[str, ...]
    bucket_ids: tuple[str, ...]
    complete: bool
    task_id: str = ""
    # Finished with skipped buckets but no exhaustive received list: part of its window
    # may have landed, and nothing says which part.
    partial: bool = False
    created_at_ms: int | None = None


class Environment(Protocol):
    """No source write or stop operation exists on this boundary.

    An adapter serializes local start calls and includes external API transfers in history.
    Task state belongs to the destination; the wrapper never maintains a second controller.
    Observations are fresh on every call. Counts use the immutable query scope.
    """

    mode: str  # "simulation" (fixture) or "live" (real appliances).
    # Whether count evidence carries per-event identities (the simulator) so new ingest can
    # be attributed; live REST counts cannot, and the engine verifies by covered days instead.
    attribution: bool

    def exclusive(self) -> AbstractContextManager[None]: ...
    def discover(self) -> tuple[Cluster, Cluster]: ...
    def destination_topology(self) -> tuple[str, str]: ...
    def destination_cluster(self, *, fresh: bool = False) -> Cluster: ...
    def history(self) -> tuple[History, ...]: ...
    def count(self, scope: str, window: Window, bucket_keys: tuple[str, ...]) -> Counts: ...
    def source_growth(self) -> dict: ...
    def test_mover(self) -> None: ...
    def test_source_credentials(self, node: str) -> None: ...
    def start(self, window: Window, bucket_keys: tuple[str, ...]) -> Transfer: ...
    def transfer(self) -> Transfer: ...
    def cancel(self) -> None: ...


class SetAsideFixupEnvironment(Environment, Protocol):
    """Deferred service/registration capabilities; not consumed by the shipped Engine."""

    def attest_transfer(self, bucket: Bucket) -> bool: ...
    def test_node_credentials(self, node: str) -> None: ...
    def service(self, node: str) -> str: ...
    def readiness(self, node: str) -> tuple[bool, bool, bool]: ...
    def arm_recovery(self, node: str, run: str) -> None: ...
    def owns_recovery(self, node: str, run: str) -> bool: ...
    def catalog_backup(self, node: str, run: str) -> dict: ...
    def catalog_safe(self, node: str, before: dict, intended: tuple[Bucket, ...]) -> bool: ...
    def fixup_artifact_verified(self, node: str) -> bool: ...
    def clear_recovery(self, node: str, run: str) -> None: ...
    def stop_service(self, node: str) -> None: ...
    def start_service(self, node: str) -> None: ...
    def fixup(self, node: str, bucket: Bucket) -> None: ...
    def fixup_confirmed(self, node: str, bucket: Bucket) -> bool: ...


class Clock(Protocol):
    def now(self) -> str: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat()

    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)
