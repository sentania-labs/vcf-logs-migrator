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


def preparation_hash(source: Cluster, destination: Cluster) -> str:
    from .models import digest

    return digest(
        {
            "source": source.identity_fingerprint,
            "destination": destination.identity_fingerprint,
            "difference": partition_diff(source, destination),
        }
    )


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


def build_plan(
    source: Cluster,
    destination: Cluster,
    window: Window,
    policy: Policy,
    previously_copied: tuple[str, ...] = (),
    *,
    fixup_only: bool = False,
) -> dict:
    compatible(source, destination)
    selected = tuple(b for b in source.buckets if b.selected(window))
    require(bool(selected), Code.INCOMPLETE)
    require(len({b.id for b in selected}) == len(selected), Code.COLLISION)
    require(
        all(b.complete and b.manifest is not None and b.size > 0 for b in selected), Code.INCOMPLETE
    )
    old = set(previously_copied)
    fresh = selected if fixup_only else tuple(b for b in selected if b.id not in old)
    require(bool(fresh), Code.INCOMPLETE)
    for b in selected:
        copies = [d for d in destination.buckets if d.id == b.id]
        if copies:
            require(
                (b.id in old or fixup_only)
                and len(copies) == 1
                and copies[0].complete
                and copies[0].registered
                and copies[0].manifest == b.manifest,
                Code.COLLISION,
            )
        elif b.id in old or fixup_only:
            require(False, Code.COLLISION)
    bounds = Window(min(b.start for b in fresh), max(b.end for b in fresh) + 1)
    warnings = [
        "Whole buckets may include events outside the requested window.",
        "Backup acknowledgment is an operator attestation, not snapshot verification.",
    ]
    active = tuple(b for b in source.buckets if b.status == "active")
    if active:
        warnings.append("Active buckets are skipped entirely; newest coverage is incomplete.")
    if destination.preview:
        warnings.append(
            "9.1.1 preview: per-event partition routing still needs live qualification."
        )
        warnings.append("9.1.1 may bypass ingest-time masking; this is not yet live-validated.")
        warnings.append("9.1.1 tail windows must select no previously imported bucket.")
    warnings.append(
        "The destination receives source root credentials. Use a temporary password "
        "and rotate it afterwards; the tool does not change source passwords."
    )
    if any(p.retention_seconds > 0 for p in source.partitions):
        warnings.append("Source retention can remove planned buckets; drift blocks execution.")
    if destination.version.startswith("9.0."):
        warnings.append("9.0 API registers in Default with registration-time creation timestamps.")
    return {
        "schema_version": 1,
        "kind": "fixup" if fixup_only else "migration",
        "source": asdict(source),
        "destination": asdict(destination),
        "source_fingerprint": source.source_fingerprint,
        "window": asdict(window),
        "coverage": asdict(bounds),
        "policy": asdict(policy),
        "selected": [asdict(b) for b in fresh],
        "api_selected": [b.key for b in selected],
        "previously_copied": list(previously_copied),
        "active_skipped": [b.key for b in active],
        "last_fully_sealed_day": last_sealed_day(source.buckets),
        "last_fully_sealed_day_basis": "unknown"
        if last_sealed_day(source.buckets) is None
        else "earliest proven active event bound; America/Chicago",
        "partition_diff": partition_diff(source, destination),
        "capacity_reservations": capacity(destination, () if fixup_only else fresh, policy),
        "warnings": warnings,
    }
