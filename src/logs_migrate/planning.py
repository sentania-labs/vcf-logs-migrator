"""Pure planning and capacity checks. No date restriction or 90-day cap."""

from dataclasses import asdict, replace

from .errors import Code, require
from .models import Bucket, Cluster, Window, last_sealed_day


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
            p.name
            for p in source.partitions
            # Match by name and all configuration settings, as config compare does.
            # Destination-assigned IDs need not equal the source fixture/API ID.
            if p.name in target and replace(p, id=target[p.name].id) != target[p.name]
        ],
        "missing_fields": sorted(set(source.fields) - set(destination.fields))
        if destination.preview
        else [],
    }


def capacity(destination: Cluster, buckets: tuple[Bucket, ...]) -> dict:
    # The API, not the operator, assigns each bucket to a destination node, so every node's own
    # available repository budget must be able to hold the whole selection. A node whose budget
    # was not observed cannot refuse the plan; it is listed as unknown and warned about.
    required = sum(b.size for b in buckets)
    nodes = [
        {
            "id": n.id,
            "available_bytes": n.repository_available,
            "fits": None if not n.storage_known else n.repository_available >= required,
        }
        for n in destination.nodes
    ]
    return {
        "required_bytes": required,
        "nodes": nodes,
        "fits": all(n["fits"] is not False for n in nodes),
        "unknown": [n["id"] for n in nodes if n["fits"] is None],
    }


BACKUP_ACK = (
    "No undo: migrated data stays on the destination. Snapshot the destination before "
    "continuing (snapshotting the source too is optional)."
)


def build_plan(source: Cluster, destination: Cluster, window: Window) -> dict:
    compatible(source, destination)
    require(destination.history_complete, Code.INCOMPLETE)
    selected = tuple(b for b in source.buckets if b.selected(window))
    undated = tuple(b for b in source.buckets if b.undated)
    require(bool(selected), Code.INCOMPLETE)
    require(len({b.id for b in selected}) == len(selected), Code.COLLISION)
    require(
        all(b.complete and b.start is not None and b.end is not None for b in selected),
        Code.INCOMPLETE,
    )
    require(not ({b.id for b in selected} & {b.id for b in destination.buckets}), Code.COLLISION)
    coverage = Window(min(b.start for b in selected), max(b.end for b in selected) + 1)
    space = capacity(destination, selected)
    return {
        "source": {"id": source.id, "version": source.version, "identity": source.identity},
        "destination": {
            "id": destination.id,
            "version": destination.version,
            "identity": destination.identity,
            "nodes": [{"id": n.id} for n in destination.nodes],
        },
        "window": asdict(window),
        "coverage": asdict(coverage),
        "selected": [asdict(b) for b in selected],
        "active_skipped": [b.key for b in source.buckets if b.status == "active"],
        "undated_excluded": [
            {
                "id": b.id,
                "node": b.node,
                "partition": b.partition,
                "size": b.size,
                "created": b.created,
            }
            for b in source.buckets
            if b.undated
        ],
        "last_fully_sealed_day": last_sealed_day(source.buckets),
        "partition_diff": partition_diff(source, destination),
        "capacity": space,
        "warnings": [
            BACKUP_ACK,
            "Active buckets are skipped; newest data is not copied.",
            "Open buckets may contain late or backfilled data for earlier days; "
            "verification marks days with unknown active bounds as partial.",
            "Whole buckets can extend beyond the requested date range.",
            "The source root password is sent to the destination for the transfer.",
            "Source retention can remove planned buckets before the run.",
        ]
        + (
            [
                f"{len(undated)} sealed bucket(s) have no time range and were excluded; "
                "the plan cannot show whether they hold data in this window: "
                + ", ".join(f"{b.node}/{b.id}" for b in undated)
            ]
            if undated
            else []
        )
        + (
            []
            if space["fits"]
            else ["Selected buckets exceed a destination node's available repository budget."]
        )
        + (
            [
                "Destination repository budget was not observed for: "
                + ", ".join(space["unknown"])
                + ". The API stops copying at 95% disk and reports the skipped buckets; "
                "verification shows what landed."
            ]
            if space["unknown"]
            else []
        )
        + (
            [
                "9.1.1 preview; a 9.0 to 9.1.1 lab run has not been recorded.",
                "Count verification is not available for 9.1.1: no 9.1 query route has been "
                "captured, and counts from a guessed route would be evidence of nothing. The "
                "transfer can run; verification will report incomplete evidence.",
            ]
            if destination.preview
            else []
        ),
    }
