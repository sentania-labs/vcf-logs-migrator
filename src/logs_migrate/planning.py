"""Pure planning and capacity checks. No date restriction or 90-day cap."""

from dataclasses import asdict

from .errors import Code, require
from .models import Bucket, Cluster, Policy, Window, last_sealed_day


def compatible(source: Cluster, destination: Cluster):
    require(source.version.startswith("8.18."), Code.UNSUPPORTED)
    require(destination.version.startswith("9.0.") or destination.preview, Code.UNSUPPORTED)
    require(source.id != destination.id and source.identity != destination.identity, Code.TOPOLOGY)
    require(not ({n.id for n in source.nodes} & {n.id for n in destination.nodes}), Code.TOPOLOGY)
    require(all(n.service == "running" for n in (*source.nodes, *destination.nodes)), Code.SERVICE)


def partition_diff(source: Cluster, destination: Cluster) -> dict:
    target = {p.name: p for p in destination.partitions}
    return {
        "missing": [asdict(p) for p in source.partitions if p.name not in target],
        "different": [
            p.name for p in source.partitions if p.name in target and p != target[p.name]
        ],
        "missing_fields": sorted(set(source.fields) - set(destination.fields))
        if destination.preview
        else [],
    }


def capacity(destination: Cluster, buckets: tuple[Bucket, ...], policy: Policy) -> dict[str, int]:
    # Conservative bound: the API's node assignment is not operator-controlled.
    # Every node must be able to hold the entire batch, overhead, and bounded concurrent growth.
    transfer = sum(b.size for b in buckets)
    required = (transfer * (100 + policy.overhead_percent) + 99) // 100
    required += policy.growth_bytes
    allocations = {}
    for node in destination.nodes:
        headroom = min(
            node.capacity * policy.ceiling_percent // 100 - node.used,
            node.repository_available - policy.reserve_bytes,
        )
        require(headroom >= required, Code.CAPACITY)
        allocations[node.id] = required
    return allocations


BACKUP_ACK = (
    "No undo: migrated data stays on the destination. Snapshot the destination before "
    "continuing (snapshotting the source too is optional)."
)


def build_plan(source: Cluster, destination: Cluster, window: Window) -> dict:
    compatible(source, destination)
    require(destination.history_complete, Code.INCOMPLETE)
    selected = tuple(b for b in source.buckets if b.selected(window))
    require(bool(selected), Code.INCOMPLETE)
    require(len({b.id for b in selected}) == len(selected), Code.COLLISION)
    require(
        all(b.complete and b.start is not None and b.end is not None for b in selected),
        Code.INCOMPLETE,
    )
    require(not ({b.id for b in selected} & {b.id for b in destination.buckets}), Code.COLLISION)
    coverage = Window(min(b.start for b in selected), max(b.end for b in selected) + 1)
    return {
        "source": {"id": source.id, "version": source.version},
        "destination": {
            "id": destination.id,
            "version": destination.version,
            "nodes": [{"id": n.id} for n in destination.nodes],
        },
        "window": asdict(window),
        "coverage": asdict(coverage),
        "selected": [asdict(b) for b in selected],
        "active_skipped": [b.key for b in source.buckets if b.status == "active"],
        "last_fully_sealed_day": last_sealed_day(source.buckets),
        "partition_diff": partition_diff(source, destination),
        "warnings": [
            BACKUP_ACK,
            "Active buckets are skipped; newest data is not copied.",
            "Whole buckets can extend beyond the requested date range.",
        ]
        + (
            ["9.1.1 preview; live adapter qualification is pending."] if destination.preview else []
        ),
    }
