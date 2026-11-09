"""Read-only checks against real hosts with a pass/fail summary a human or an agent can read.

Every step is one of the reads the plan needs. Nothing is started, written or stopped.
Details name counts, versions, hosts and response key names; never values from a listing,
never credentials, never raw remote text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from .adapters.ssh_errors import SSHFailure
from .errors import Code, MigrationError
from .models import DAY, Cluster, Window
from .planning import build_plan, compatible, partition_diff

DEFAULT_COUNT_DAYS = 30


@dataclass
class Step:
    name: str
    status: str  # pass, fail, skip
    detail: str = ""


@dataclass
class SelfTest:
    steps: list[Step] = field(default_factory=list)
    notes: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not any(s.status == "fail" for s in self.steps)

    def counts(self) -> dict[str, int]:
        return {
            status: sum(1 for s in self.steps if s.status == status)
            for status in ("pass", "fail", "skip")
        }

    def text(self) -> str:
        width = max((len(s.name) for s in self.steps), default=20)
        lines = [
            f"{s.status.upper():<5} {s.name:<{width}}  {s.detail}".rstrip() for s in self.steps
        ]
        totals = self.counts()
        lines.append("")
        lines.append(
            f"Summary: {totals['pass']} passed, {totals['fail']} failed, {totals['skip']} skipped. "
            + ("All checks passed." if self.passed else "Fix the failed checks before planning.")
        )
        lines.append("Read-only: nothing was started, written or stopped.")
        return "\n".join(lines) + "\n"

    def data(self) -> dict:
        return {
            "steps": [asdict(s) for s in self.steps],
            "summary": self.counts(),
            "passed": self.passed,
            "notes": self.notes,
        }


def _code(error: Exception) -> str:
    if isinstance(error, SSHFailure):
        return f"{error.code.value}: {error.message}"
    return error.code.value if isinstance(error, MigrationError) else "operation_failed"


def _fmt_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()


def run(env, window: Window | None = None, *, count_days: int = DEFAULT_COUNT_DAYS) -> SelfTest:
    """`env` is an opened LiveEnvironment (identities already trusted)."""
    report = SelfTest()
    results: dict[str, object] = {}

    def step(name, action, *, skip: str | None = None):
        if skip:
            report.steps.append(Step(name, "skip", skip))
            return None
        try:
            value, detail = action()
        except Exception as error:  # Only codes and safe diagnostics cross.
            report.steps.append(Step(name, "fail", _code(error)))
            return None
        report.steps.append(Step(name, "pass", detail))
        results[name] = value
        return value

    connection = env.connection

    def source_version():
        version = env.source_version()
        return version, version

    step("Source API version", source_version)

    def source_partitions():
        from .adapters.live import parse_partitions, partition_models

        rules = partition_models(parse_partitions(env.source_client.partitions()))
        return rules, f"{len(rules)} partition" + ("s" if len(rules) != 1 else "")

    step("Source partitions", source_partitions)

    def members():
        hosts = env.members or env.source_members()
        env.members = hosts
        detail = f"{len(hosts)} member" + ("s" if len(hosts) != 1 else "") + ": " + ", ".join(hosts)
        return hosts, detail

    hosts = step("Source cluster members (SSH)", members) or []
    inventories = []
    for host in hosts:

        def node(host=host):
            inventory = env.node_inventory(host)
            inventories.append(inventory)
            sealed = [b for b in inventory.buckets if b.status != "active"]
            active = [b for b in inventory.buckets if b.status == "active"]
            bounded = [b for b in sealed if b.start is not None and b.end is not None]
            sized = sum(1 for b in inventory.buckets if b.id in inventory.usage)
            space = (
                f"{inventory.available / 1e9:.1f} GB free"
                if inventory.available is not None
                else "free space unknown"
            )
            detail = (
                f"service {inventory.service} · {len(sealed)} sealed ({len(bounded)} with dates), "
                f"{len(active)} active · {sized} sized by du · {space}"
            )
            if inventory.listing_keys:
                detail += " · listing keys: " + ", ".join(inventory.listing_keys)
            if sealed and len(bounded) != len(sealed):
                raise MigrationError(Code.INCOMPLETE)
            return inventory, detail

        step(f"Node {host} (SSH listing, du, df, service)", node)

    def source_cluster():
        cluster = env.source_cluster()
        sealed = [b for b in cluster.buckets if b.status != "active"]
        if sealed:
            first = min(b.start for b in sealed)
            last = max(b.end for b in sealed)
            span = f"{_fmt_day(first)} to {_fmt_day(last)}"
        else:
            span = "no sealed buckets"
        return cluster, (
            f"{cluster.version} · {len(cluster.nodes)} node(s) · "
            f"{len(sealed)} sealed buckets · {span}"
        )

    source = step("Source inventory model", source_cluster, skip=None if hosts else "no members")

    def destination_version():
        version = env.destination_version()
        assumed = env.notes.get("destination_version_assumed")
        return version, version + (" (assumed; the API returned no version)" if assumed else "")

    step("Destination API version", destination_version)

    def destination_partitions():
        from .adapters.live import parse_partitions, partition_models

        rules = partition_models(parse_partitions(env.destination_client.partitions()))
        return rules, f"{len(rules)} partition" + ("s" if len(rules) != 1 else "")

    step(
        "Destination partitions",
        destination_partitions,
        skip="9.1.1 partitions are read during planning" if connection.preview else None,
    )

    def task():
        transfer = env.transfer()
        keys = env.notes.get("status_response_keys", [])
        detail = f"task {transfer.status}" + (f" · id {transfer.id}" if transfer.id else "")
        detail += f" · {len(transfer.nodes)} node entries"
        if keys:
            detail += " · keys: " + ", ".join(keys[:12]) + (" …" if len(keys) > 12 else "")
        return transfer, detail

    step("Destination data-migrations status", task)

    def destination_cluster():
        cluster = env.destination_cluster()
        known = [n.id for n in cluster.nodes if n.storage_known]
        return cluster, (
            f"{cluster.version} · {len(cluster.nodes)} node(s) · "
            f"{len(cluster.buckets)} buckets listed · budget known for {len(known)}"
        )

    destination = step("Destination inventory model", destination_cluster)

    def compatibility():
        compatible(source, destination)
        return True, f"{source.version} → {destination.version}"

    step(
        "Version compatibility",
        compatibility,
        skip=None
        if isinstance(source, Cluster) and isinstance(destination, Cluster)
        else "inventory missing",
    )

    def partitions():
        diff = partition_diff(source, destination)
        detail = f"{len(diff['missing'])} missing, {len(diff['different'])} differ"
        if diff["missing"]:
            detail += ": " + ", ".join(p["name"] for p in diff["missing"])
        return diff, detail

    step(
        "Partition comparison",
        partitions,
        skip=None
        if isinstance(source, Cluster) and isinstance(destination, Cluster)
        else "inventory missing",
    )

    now = int(datetime.now(UTC).timestamp() * 1000)
    count_window = window or Window((now - count_days * DAY) // DAY * DAY, now // DAY * DAY + DAY)
    label = f"{_fmt_day(count_window.start)} to {_fmt_day(count_window.end - 1)}"

    def source_counts():
        daily, complete = env.source_client.daily_counts(count_window)
        total = sum(daily.values())
        days = sum(1 for v in daily.values() if v)
        if not complete:
            raise MigrationError(Code.INCOMPLETE)
        return daily, f"{total:,} events on {days} days ({label}) · complete"

    step("Source daily counts", source_counts)

    def destination_counts():
        daily, complete = env.destination_client.daily_counts(count_window)
        total = sum(daily.values())
        days = sum(1 for v in daily.values() if v)
        if not complete:
            raise MigrationError(Code.INCOMPLETE)
        return daily, f"{total:,} events on {days} days ({label}) · complete"

    step(
        "Destination daily counts",
        destination_counts,
        skip="9.1.1 query route not qualified; counts unavailable" if connection.preview else None,
    )

    def count_probe():
        from .adapters.live import observed_keys

        probe = env.destination_client.probe_counts(
            Window(count_window.end - DAY, count_window.end)
        )
        keys = observed_keys(probe)
        env.notes["destination_count_probe_keys"] = keys
        shown = ", ".join(keys[:16]) + (" …" if len(keys) > 16 else "")
        return probe, "response keys (names only, never used as evidence): " + (shown or "none")

    if connection.preview:
        step("Destination 9.1 count query probe", count_probe)

    def recent():
        events = env.recent_source_events()
        if events is None:
            raise MigrationError(Code.INCOMPLETE)
        return events, (
            f"{events:,} events in the last 24 hours"
            + (" · source is still receiving events" if events else " · source looks quiet")
        )

    step("Source ingest (cutover check)", recent)

    def plan():
        sealed = [b for b in source.buckets if b.status != "active" and b.start is not None]
        if window is not None:
            requested = window
        elif sealed:
            requested = Window(min(b.start for b in sealed), max(b.end for b in sealed) + 1)
        else:
            raise MigrationError(Code.INCOMPLETE)
        preview = build_plan(source, destination, requested)
        detail = (
            f"{len(preview['selected'])} buckets, "
            f"{preview['capacity']['required_bytes'] / 1e9:.2f} GB, "
            f"covers {_fmt_day(preview['coverage']['start'])} to "
            f"{_fmt_day(preview['coverage']['end'] - 1)} · "
            f"{len(preview['active_skipped'])} active skipped · "
            f"last sealed day {preview['last_fully_sealed_day'] or 'unknown'}"
        )
        if preview["capacity"]["unknown"]:
            detail += " · destination budget unknown"
        return preview, detail

    step(
        "Plan preview (no request sent)",
        plan,
        skip=None
        if isinstance(source, Cluster) and isinstance(destination, Cluster)
        else "inventory missing",
    )
    report.notes = {k: v for k, v in env.notes.items() if k != "identity_changed"}
    return report
