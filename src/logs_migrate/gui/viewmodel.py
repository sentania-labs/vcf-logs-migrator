"""Pure presentation derivations from engine results. No Qt, no migration decisions.

Every value shown by the GUI comes from an engine or API observation; when an input is
missing the view says Unavailable instead of estimating.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..errors import Code
from ..models import DAY, Window

UNAVAILABLE = "Unavailable"
PRESETS = (
    ("90 days", 90),
    ("3 months", 91),
    ("6 months", 182),
    ("9 months", 274),
    ("12 months", 365),
    ("All history", None),
)
ERROR_TEXT = {
    Code.INVALID_INPUT: "The request was rejected as invalid input.",
    Code.UNSUPPORTED: (
        "The appliance does not support this operation on this version "
        "(for example counts on 9.1.1)."
    ),
    Code.INVENTORY_CHANGED: "The appliance inventory changed since the preview. Refresh it.",
    Code.CAPACITY: "A destination node cannot hold the selected history.",
    Code.OVERLAP: "The requested window selects history already received by the destination.",
    Code.TOPOLOGY: "The destination identity changed since the plan was previewed.",
    Code.COLLISION: "A selected bucket identity already exists on the destination.",
    Code.PARTITIONS: "Destination partitions do not match the source. Apply configuration first.",
    Code.FIELDS: "Required extracted fields are missing on the 9.1.1 destination.",
    Code.ACK: "The latest-data acknowledgment is required before starting.",
    Code.AUTH: "Authentication failed.",
    Code.CONNECTION: "The API did not respond.",
    Code.SCHEMA: "The API returned an unrecognized response.",
    Code.INCOMPLETE: "The evidence was incomplete.",
    Code.VERIFICATION_PENDING: "Verification is pending.",
    Code.UNCERTAIN: "The remote outcome is uncertain. Refresh the API status.",
    Code.BUSY: "The destination already has a running task or another client holds the lock.",
    Code.SERVICE: "A service state could not be confirmed.",
    Code.RECOVERY: "A service recovery is required.",
    Code.INTEGRITY: "The simulator state does not match the fixture.",
    Code.STATE: "That action is not valid for this run's current state.",
    Code.STORAGE: "The private journal is unavailable.",
    Code.NOT_RECEIVED: "A selected group was not received by the destination.",
    Code.UNCERTAIN_START: (
        "An earlier start of this window got no answer from the destination. The window is "
        "held for 15 minutes; it is released once the API shows no task for it."
    ),
}


def split_error(text: str) -> tuple[Code | None, str]:
    """A failure is a bare code, `gui:<message>`, or `<code>: <message>` (a coded diagnostic)."""
    if text.startswith("gui:pre_start:"):
        return None, text[len("gui:pre_start:") :]
    if text.startswith("gui:"):
        return None, text[4:]
    head, separator, message = text.partition(": ")
    try:
        code = Code(head if separator else text)
    except ValueError:
        return None, "The operation failed. Query the API status before retrying."
    return code, message if separator else ERROR_TEXT[code]


def describe_error(code: str) -> str:
    return split_error(code)[1]


# Formatting -------------------------------------------------------------------------------


def format_count(value) -> str:
    if value is None:
        return UNAVAILABLE
    return f"{int(value):,}"


def _unit(size: int) -> tuple[str, float]:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if size >= scale:
            return unit, scale
    return "B", 1.0


def format_bytes(size, unit: str | None = None) -> str:
    if size is None:
        return UNAVAILABLE
    name, scale = (
        _unit(int(size))
        if unit is None
        else (unit, {"GB": 1e9, "MB": 1e6, "kB": 1e3, "B": 1.0}[unit])
    )
    value = int(size) / scale
    if name == "B":
        return f"{int(size)} B"
    digits = 0 if value >= 100 else 1
    return f"{value:,.{digits}f} {name}"


def format_bytes_pair(done, total) -> str:
    """`553 / 712 MB`: both figures in the larger value's unit."""
    if done is None:
        return UNAVAILABLE
    if total is None:
        return format_bytes(done)
    unit, scale = _unit(int(max(done, total)))
    if unit == "B":
        return f"{int(done)} / {int(total)} B"
    digits = 0 if max(done, total) / scale >= 100 else 1
    return f"{int(done) / scale:,.{digits}f} / {int(total) / scale:,.{digits}f} {unit}"


def format_rate(bytes_per_second) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return UNAVAILABLE
    return "~" + format_bytes(round(bytes_per_second)) + "/s"


def format_elapsed(seconds) -> str:
    if seconds is None or seconds < 0:
        return UNAVAILABLE
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_eta(seconds) -> str:
    if seconds is None or seconds < 0:
        return UNAVAILABLE
    whole = int(round(seconds))
    if whole < 60:
        return f"~{whole} sec"
    if whole < 3600:
        return f"~{whole // 60} min"
    return f"~{whole // 3600} h {whole % 3600 // 60} min"


def format_percent(done, total) -> str:
    if done is None or not total:
        return UNAVAILABLE
    return f"{100.0 * done / total:.1f}% of planned bytes"


def format_version(version: str) -> str:
    """`9.0.2.0200` reads as `9.0.2`; a preview keeps its label."""
    parts = version.split(".")
    if len(parts) > 3 and all(p.isdigit() for p in parts[:3]):
        return ".".join(parts[:3])
    return version


def day_of(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, UTC).date()


def format_day(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}"


def format_day_range(start_ms, end_ms, *, inclusive_end=False, suffix=" UTC") -> str:
    """Half-open millisecond ranges render as inclusive day labels: `Jun 1–Nov 4 UTC`."""
    if start_ms is None or end_ms is None:
        return UNAVAILABLE
    first = day_of(start_ms)
    last_ms = end_ms if inclusive_end else end_ms - 1
    last = day_of(max(start_ms, last_ms))
    if first == last:
        text = format_day(first)
    elif first.year != last.year:
        text = f"{format_day(first)}, {first.year}–{format_day(last)}, {last.year}"
    else:
        text = f"{format_day(first)}–{format_day(last)}"
    return text + suffix


def format_day_label(iso_day: str) -> str:
    return format_day(date.fromisoformat(iso_day))


def format_time(iso: str | None, zone: str) -> str:
    if not iso:
        return UNAVAILABLE
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        local = parsed.astimezone(ZoneInfo(zone))
    except Exception:
        local = parsed.astimezone(UTC)
    return f"{format_day(local)}, {local.strftime('%H:%M:%S %Z')}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Windows and presets ----------------------------------------------------------------------


def history_extent(cluster: dict) -> tuple[int, int] | None:
    """Earliest proven start to latest proven end (exclusive) over sealed buckets."""
    starts = [b["start"] for b in cluster["buckets"] if b.get("start") is not None]
    ends = [b["end"] for b in cluster["buckets"] if b.get("end") is not None]
    if not starts or not ends:
        return None
    return min(starts), max(ends) + 1


def window_from_days(start: date, end_inclusive: date) -> Window | None:
    if window_error(start, end_inclusive):
        return None
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1000)
    end_ms = (
        int(
            datetime(
                end_inclusive.year, end_inclusive.month, end_inclusive.day, tzinfo=UTC
            ).timestamp()
            * 1000
        )
        + DAY
    )
    return Window(start_ms, end_ms)


def preset_days(label: str, extent: tuple[int, int] | None) -> tuple[date, date] | None:
    """Inclusive UTC day pair for a preset, anchored on the end of the source history."""
    if extent is None:
        return None
    first, last = day_of(extent[0]), day_of(extent[1] - 1)
    days = dict(PRESETS).get(label, "missing")
    if days == "missing":
        return None
    if days is None:
        return first, last
    return max(first, last - timedelta(days=days - 1)), last


# Connections ------------------------------------------------------------------------------


def cluster_summary(cluster: dict) -> str:
    return f"{format_version(cluster['version'])} · {len(cluster['nodes'])} node" + (
        "s" if len(cluster["nodes"]) != 1 else ""
    )


def connection_checks(
    discovered: dict | None, compatible: str | None, mode: str = "simulation"
) -> list[tuple[str, str]]:
    if discovered is None:
        return [
            ("Source and destination API inventory", "Not tested"),
            ("Version compatibility", "Not tested"),
            ("Destination → every source node on SSH", "Checked when the transfer starts"),
            ("Credentials", "Session memory only; excluded from reports"),
        ]
    source, destination = discovered["source"], discovered["destination"]
    return [
        (
            "Source and destination API inventory",
            f"Passed · {cluster_summary(source)} → {cluster_summary(destination)}",
        ),
        (
            "Appliance addresses",
            "Contacted over TLS and SSH with enrolled identities"
            if mode == "live"
            else "Not contacted; this session reads the simulator fixture",
        ),
        (
            "Version compatibility",
            "Passed" if compatible is None else "Failed · " + describe_error(compatible),
        ),
        ("Destination → every source node on SSH", "Checked when the transfer starts"),
        ("Credentials", "Session memory only; excluded from reports"),
    ]


def selftest_rows(result: dict) -> list[tuple[str, str]]:
    """Self-test steps as check rows: `PASS · detail`."""
    rows = []
    for step in result.get("steps", []):
        label = {"pass": "Passed", "fail": "Failed", "skip": "Skipped"}.get(
            step["status"], step["status"]
        )
        detail = step.get("detail", "")
        if step["status"] == "fail" and detail and detail.partition(": ")[0] in set(Code):
            detail = describe_error(detail)
        rows.append((step["name"], label + (" · " + detail if detail else "")))
    summary = result.get("summary", {})
    rows.append(
        (
            "Self-test",
            f"{summary.get('pass', 0)} passed, {summary.get('fail', 0)} failed, "
            f"{summary.get('skip', 0)} skipped · read only",
        )
    )
    return rows


# Plan -------------------------------------------------------------------------------------


def axis_labels(extent: tuple[int, int] | None) -> tuple[str, str, str]:
    """Start, middle and end day labels for the timeline axis."""
    if extent is None:
        return ("", "", "")
    start, end = extent
    middle = start + (end - start) // 2
    return (format_day(day_of(start)), format_day(day_of(middle)), format_day(day_of(end - 1)))


def window_error(start: date, end_inclusive: date) -> str | None:
    if end_inclusive < start:
        return "End date must be on or after the start date."
    if start < date(1970, 1, 1):
        return "Start date must be 1970 or later."
    return None


def plan_error_text(code: str, discovered: dict | None, window: Window | None) -> str:
    """Say what is actually wrong with a refused preview."""
    if code.startswith(Code.COLLISION.value + ": Selected groups already exist") and discovered:
        conflicts = [
            b for b in discovered["source"]["buckets"] if f"{b['node']}/{b['id']} (" in code
        ]
        if conflicts:
            groups = [
                f"{b['node']} ("
                f"{format_day_range(b.get('start'), b.get('end'), inclusive_end=True)})"
                for b in conflicts
            ]
            return (
                f"{len(groups)} selected groups already exist on the destination: "
                + "; ".join(groups)
                + ". Choose dates that exclude these groups."
            )
    if code == Code.INCOMPLETE.value and discovered and window:
        sealed = [
            b
            for b in discovered["source"]["buckets"]
            if b["status"] != "active" and b.get("start") is not None and b.get("end") is not None
        ]
        overlapping = [b for b in sealed if b["start"] < window.end and b["end"] >= window.start]
        extent = history_extent(discovered["source"])
        if not overlapping:
            span = f" Source history spans {format_day_range(*extent)}." if extent else ""
            return "No sealed history in these dates." + span + " Choose another date range."
        if not discovered["destination"].get("history_complete", True):
            return (
                "The destination's transfer history is incomplete, so overlap cannot be ruled out."
            )
        return "Some selected groups have unproven date bounds, so the preview cannot be trusted."
    return describe_error(code)


def plan_view(run: dict, discovered: dict | None) -> dict:
    plan = run["plan"]
    selected = plan["selected"]
    coverage = plan["coverage"]
    capacity = plan["capacity"]
    diff = plan["partition_diff"]
    active = plan["active_skipped"]
    events = sum(b["messages"] for b in selected)
    size = sum(b["size"] for b in selected)
    groups = [
        (
            f"Group {index} · {b['node']}",
            format_day_range(b["start"], b["end"], inclusive_end=True, suffix=""),
            f"{format_bytes(b['size'])} / {format_count(b['messages'])}",
            "Import",
        )
        for index, b in enumerate(selected, start=1)
    ]
    tips = [f"Bucket {b['id']} on {b['node']}" for b in selected]
    for index, key in enumerate(active, start=len(selected) + 1):
        node, _, ident = key.rpartition("/")
        groups.append((f"Group {index} · {node}", "Open; may advance", "Unknown", "Open · exclude"))
        tips.append(f"Open bucket {ident} on {node}")
    for b in plan.get("undated_excluded", []):
        groups.append(
            (
                f"Group {len(groups) + 1} · {b['node']}",
                "No dates; may hold data",
                f"{format_bytes(b['size'])} / unknown",
                "Unknown · exclude",
            )
        )
        tips.append(f"Sealed bucket {b['id']} on {b['node']} reports no time range")
    if plan["last_fully_sealed_day"]:
        sealed_text = (
            "Last day covered only by sealed buckets right now: "
            f"{format_day_label(plan['last_fully_sealed_day'])}."
        )
    else:
        sealed_text = (
            "Last day covered only by sealed buckets: not proven (open group bounds unknown); "
            f"sealed groups end {format_day(day_of(coverage['end'] - 1))}."
        )
    checks = [
        ("Destination overlap", "None in selected whole groups"),
        (
            "Partitions",
            "Match"
            if not diff["missing"] and not diff["different"]
            else f"{len(diff['missing'])} missing, {len(diff['different'])} differ",
        ),
    ]
    if plan["destination"]["version"].startswith("9.1.1"):
        checks.append(
            (
                "Extracted fields",
                "Present" if not diff["missing_fields"] else ", ".join(diff["missing_fields"]),
            )
        )
    unknown = capacity.get("unknown", [])
    checks.append(
        (
            "Storage",
            "A node cannot hold the selection"
            if not capacity["fits"]
            else "Budget not observed; the API stops at 95% disk"
            if unknown
            else "Every node fits",
        )
    )
    checks.append(
        (
            "Latest data",
            f"{len(active)} open group{'s' if len(active) != 1 else ''} excluded"
            if active
            else "No open groups",
        )
    )
    ingest = plan.get("source_ingest")
    if ingest is not None:
        growth_bytes = ingest.get("bytes")
        seconds = ingest.get("window_seconds")
        if growth_bytes is None:
            detail = UNAVAILABLE
        elif growth_bytes:
            detail = (
                f"Active buckets on {', '.join(ingest['nodes'])} grew by "
                f"{format_bytes(growth_bytes)} in {seconds} s"
            )
        else:
            detail = f"No active bucket growth in {seconds} s"
        checks.append(("Source ingest", detail))
    required = capacity["required_bytes"]
    storage = [
        (
            n["id"],
            format_bytes(n["available_bytes"]),
            format_bytes(required),
            "Unknown"
            if n["fits"] is None
            else format_bytes(n["available_bytes"] - required)
            if n["fits"]
            else "Short",
        )
        for n in capacity["nodes"]
    ]
    blockers = []
    if not capacity["fits"]:
        blockers.append("storage")
    if diff["missing"] or diff["different"]:
        blockers.append("partitions")
    if diff["missing_fields"]:
        blockers.append("fields")
    return {
        "run_id": run["id"],
        "events": events,
        "size": size,
        "subtitle": (
            f"{format_version(plan['source']['version'])} → "
            f"{format_version(plan['destination']['version'])} · "
            f"{format_bytes(size)} / {format_count(events)} selected events"
        ),
        "requested": format_day_range(plan["window"]["start"], plan["window"]["end"]),
        "included": format_day_range(coverage["start"], coverage["end"], suffix=""),
        "coverage": (coverage["start"], coverage["end"]),
        "window": (plan["window"]["start"], plan["window"]["end"]),
        "extent": history_extent(discovered["source"]) if discovered else None,
        "last_sealed_day": plan["last_fully_sealed_day"],
        "sealed_text": sealed_text,
        "axis": axis_labels(history_extent(discovered["source"]) if discovered else None),
        "active_count": len(active),
        "groups": groups,
        "group_tips": tips,
        "checks": checks,
        "storage": storage,
        "warnings": plan["warnings"],
        "blockers": blockers,
        "needs_ack": bool(active),
    }


# Refusals the engine raises before any POST; every other failure may have reached the API.
PRE_POST_CODES = {
    Code.INVALID_INPUT,
    Code.UNCERTAIN_START,
    Code.UNSUPPORTED,
    Code.INVENTORY_CHANGED,
    Code.CAPACITY,
    Code.OVERLAP,
    Code.TOPOLOGY,
    Code.COLLISION,
    Code.PARTITIONS,
    Code.FIELDS,
    Code.ACK,
    Code.AUTH,
    Code.BUSY,
    Code.SERVICE,
    Code.STATE,
    Code.SCHEMA,
}


def is_pre_post(code: str) -> bool:
    return code.startswith("gui:pre_start:") or split_error(code)[0] in PRE_POST_CODES


def start_note(code: str) -> str:
    """Footer text after a failed Start; only pre-POST refusals promise nothing was sent."""
    if is_pre_post(code):
        return describe_error(code) + " No API request was submitted."
    return (
        describe_error(code)
        + " The request may have reached the API; status is being refreshed. Do not submit again."
    )


def start_state(
    view: dict | None, acknowledged: bool, connected: bool, started: bool = False
) -> tuple[bool, str]:
    """Whether Start is enabled and the footer sentence explaining why."""
    if not connected:
        return False, "Connect both environments before planning."
    if view is None:
        return False, "Refresh the preview to select whole sealed groups by date."
    if started:
        return (
            False,
            "This plan was already submitted to the API. Watch Migration; preview a new window "
            "to migrate more history.",
        )
    if view["blockers"]:
        return False, "Start is unavailable until the checks pass. " + {
            "storage": "A destination node cannot hold the selection.",
            "partitions": "Destination partitions must match first.",
            "fields": "Required extracted fields are missing on the destination.",
        }[view["blockers"][0]]
    if view["needs_ack"] and not acknowledged:
        return (
            False,
            "Latest-data acknowledgment is required. "
            "Start submits this window to the destination API.",
        )
    return (
        True,
        "Start records the snapshot reminder as acknowledged and submits the displayed window; "
        "there is no undo.",
    )


# Migration --------------------------------------------------------------------------------

STATE_LABEL = {
    "idle": "No history transfer running",
    "running": "RUNNING",
    "completed": "COMPLETED",
    "cancelled": "CANCELLED",
    "failed": "FAILED",
    "unknown": "UNKNOWN",
}


def uncertain_start_text(hold_until: str | None, zone: str) -> str:
    held = f"until {format_time(hold_until, zone)}" if hold_until else "for 15 minutes"
    return (
        "The start's outcome is unknown and no task has appeared yet. Keep refreshing status. "
        f"If none appears, this window stays held {held}, 15 minutes from the start attempt; "
        "after that, refresh the preview on Discovery & Plan and start again."
    )


def progress_view(
    status: dict | None,
    *,
    stale: bool,
    last_ok: str | None,
    zone: str,
    hosts: tuple[str, str] | None = None,
    start_uncertain: bool = False,
) -> dict:
    if status is None or (status["status"] == "idle" and start_uncertain):
        return {
            "state": "idle",
            "banner": (
                "warn",
                "Start outcome uncertain · no task observed",
                uncertain_start_text(status.get("hold_until") if status else None, zone),
            )
            if start_uncertain
            else (
                "info",
                "No API task observed",
                "Start a migration from Discovery & Plan, "
                "or refresh to read existing server tasks.",
            ),
            "tiles": [
                ("Transferred", UNAVAILABLE, "No API task"),
                ("Transfer rate", UNAVAILABLE, "Estimated from API counters"),
                ("Elapsed", UNAVAILABLE, "Since API task start"),
                ("ETA", UNAVAILABLE, "Estimate; verification time is separate"),
            ],
            "nodes": [],
            "bucket_rows": [],
            "details": [],
            "progress": None,
        }
    state = status["status"]
    label = STATE_LABEL.get(state, state.upper())
    if stale:
        banner = (
            "warn",
            "Status unavailable · server tasks may still run",
            f"Last response {format_time(last_ok, zone)}. "
            "Reconnect to read existing tasks; do not submit again.",
        )
    elif state == "running":
        banner = (
            "info",
            "API state: RUNNING",
            "Destination nodes pull directly from the source. "
            "API completion is followed by count verification.",
        )
    elif state == "completed":
        banner = (
            "ok",
            "API state: COMPLETED",
            "COMPLETED alone is not verified success. Verify the imported history next.",
        )
    elif state == "failed":
        banner = (
            "bad",
            "API state: FAILED",
            "Server-reported failure; imported data stays on the destination. "
            "No automatic resubmission.",
        )
    elif state == "cancelled":
        banner = (
            "warn",
            "API state: CANCELLED",
            "Cancellation was requested. Buckets already received stay on the destination.",
        )
    elif state == "idle":
        banner = ("info", "No history transfer running", "Start a migration from Discovery & Plan.")
    else:
        banner = (
            "warn",
            f"API state: {label}",
            "The API reported an unrecognized state. Refresh before deciding anything.",
        )
    done, total = status.get("bytes_transferred"), status.get("total_bytes")
    rate = status.get("rate_bytes_per_second")
    elapsed = status.get("elapsed_seconds")
    eta = status.get("eta_seconds")
    tiles = [
        (
            "Transferred",
            format_bytes_pair(done, total) if done is not None else UNAVAILABLE,
            "Last API sample"
            if stale
            else (
                f"API progress: {status['progress']:g}%"
                if status.get("progress") is not None
                else format_percent(done, total)
            ),
        ),
        (
            "Transfer rate",
            UNAVAILABLE if stale else format_rate(rate),
            "Estimated from API counters and elapsed time",
        ),
        (
            "Elapsed",
            format_elapsed(elapsed),
            "At last response" if stale else "Since API task start",
        ),
        (
            "ETA",
            "Done" if state == "completed" else UNAVAILABLE if stale else format_eta(eta),
            "Task finished" if state == "completed" else "Estimate; verification time is separate",
        ),
    ]
    nodes = []
    bucket_rows = []
    for node in status.get("nodes", []):
        node_state = "Unknown" if stale else STATE_LABEL.get(node["status"], node["status"].upper())
        scheduled = node.get("scheduled_buckets")
        totals = node.get("total_buckets")
        if scheduled is not None or totals is not None:
            sources = sorted(set(scheduled or {}) | set(totals or {}))
            for source in sources:
                bucket_rows.append(
                    (
                        node["node"],
                        source,
                        str((scheduled or {}).get(source, 0)),
                        str((totals or {}).get(source, UNAVAILABLE)),
                    )
                )
            buckets = "See source breakdown"
        else:
            buckets = f"{len(node['completed'])} received"
        if node.get("progress") is not None:
            node_state += f" · {node['progress']:g}%"
        if node["skipped"]:
            buckets += f" · {len(node['skipped'])} skipped"
        if stale and node["status"] == "running":
            error = "Last response stale"
        elif node["status"] == "failed":
            error = "Node reported failure"
        elif node["skipped"]:
            error = f"{len(node['skipped'])} skipped by the API"
        else:
            error = "—"
        nodes.append(
            (
                node["node"],
                node_state,
                format_bytes(node["bytes_transferred"])
                if node["bytes_transferred"] is not None
                else UNAVAILABLE,
                buckets,
                error,
            )
        )
    window = status.get("window")
    details = [
        ("API task", status.get("id") or UNAVAILABLE),
        ("Window", format_day_range(window["start"], window["end"]) if window else UNAVAILABLE),
        (
            "Source → destination",
            f"{hosts[0]} → {hosts[1]}"
            if hosts
            else f"{status.get('source') or '?'} → {status.get('destination') or '?'}",
        ),
        ("Last successful poll", format_time(last_ok, zone)),
        (
            "Buckets",
            f"{len(status.get('completed', ()))} received · "
            f"{len(status.get('skipped', ()))} skipped · "
            f"{len(status.get('bucket_keys', ()))} requested",
        ),
    ]
    if bucket_rows and not status.get("completed") and not status.get("skipped"):
        details = [(k, v) for k, v in details if k != "Buckets"]
    progress = None
    if done is not None and total:
        progress = max(0, min(100, int(100 * done / total)))
    elif status.get("progress") is not None:
        progress = int(status["progress"])
    return {
        "state": state,
        "banner": banner,
        "tiles": tiles,
        "nodes": nodes,
        "bucket_rows": bucket_rows,
        "details": details,
        "progress": progress,
    }


# Verification -----------------------------------------------------------------------------


def outcome_label(verification: dict | None) -> str:
    """Plain words for a run's verification state; never 'success' with groups missing."""
    if not verification:
        return "verification pending"
    outcome = verification.get("outcome")
    missing = verification.get("buckets", {}).get("not_received", [])
    if missing:
        state = "counts differ" if outcome == "verified_discrepancy" else "incomplete"
        return f"{state} · {len(missing)} group{'s' if len(missing) != 1 else ''} not received"
    return {
        "verified_success": "verified",
        "verified_discrepancy": "counts differ",
        "incomplete_verification": "incomplete",
    }.get(outcome, "verification pending")


def received_counts_match(result: dict) -> bool:
    return all(d["difference"] == 0 for d in result.get("daily", []))


def group_names(run: dict | None) -> dict[str, str]:
    """Bucket key to the group name the plan page shows, with its dates."""
    if not run:
        return {}
    names = {}
    for index, b in enumerate(run["plan"]["selected"], start=1):
        dates = format_day_range(b["start"], b["end"], inclusive_end=True, suffix="")
        names[b["node"] + "/" + b["id"]] = f"Group {index} · {b['node']} ({dates})"
    return names


def missing_groups(result: dict, run: dict | None) -> list[str]:
    names = group_names(run)
    return [names.get(key, key) for key in result.get("buckets", {}).get("not_received", [])]


def missing_days(result: dict, run: dict | None) -> list[tuple[str, int]]:
    """Per-day source counts of selected groups the API never delivered."""
    if not run or "source_by_bucket" not in run:
        return []
    days: dict[str, int] = {}
    for key in result.get("buckets", {}).get("not_received", []):
        for day, count in run["source_by_bucket"].get(key, {}).get("daily", []):
            days[day] = days.get(day, 0) + count
    return sorted(days.items())


def daily_rows(result: dict, run: dict | None) -> list[tuple[str, str, str, str]]:
    """Reconciliation rows in date order, including days the API never delivered."""
    keyed: list[tuple[str, tuple[str, str, str, str]]] = []
    current: list[dict] = []

    def flush():
        if not current:
            return
        first, last = current[0]["day"], current[-1]["day"]
        label = (
            format_day_label(first)
            if first == last
            else f"{format_day_label(first)}–{format_day_label(last)}"
        )
        keyed.append(
            (
                first,
                (
                    label,
                    format_count(sum(r["expected"] for r in current)),
                    format_count(sum(r["actual"] for r in current)),
                    "Match",
                ),
            )
        )
        current.clear()

    for row in result.get("daily", []):
        if row["difference"] == 0:
            if current and date.fromisoformat(row["day"]) - date.fromisoformat(
                current[-1]["day"]
            ) != timedelta(days=1):
                flush()
            current.append(row)
            continue
        flush()
        difference = f"{row['difference']:+,}".replace("-", "−")
        if row.get("partial"):
            difference += " · partial day"
        keyed.append(
            (
                row["day"],
                (
                    format_day_label(row["day"]),
                    format_count(row["expected"]),
                    format_count(row["actual"]),
                    difference,
                ),
            )
        )
    flush()
    for day, count in missing_days(result, run):
        keyed.append(
            (day, (format_day_label(day), format_count(count), "Not received", "Not received"))
        )
    return [row for _, row in sorted(keyed, key=lambda item: item[0])]


def report_headline_rows(verification: dict) -> list[tuple[str, str]]:
    if not verification.get("complete"):
        return [("Sent from selected groups", "Pending")]
    sent = format_count(verification.get("expected_count"))
    if verification.get("unmeasured_day_count") and verification.get("measured_window"):
        return [
            ("Sent from selected groups", sent),
            ("Arrivals", unmeasured_arrival_sentence(verification)),
        ]
    return [
        ("Expected from selected groups", sent),
        (
            "Arrived at destination since this run started",
            format_count(verification.get("actual_count")),
        ),
    ]


def unmeasured_arrival_sentence(result: dict) -> str:
    window = result["measured_window"]
    start = datetime.fromtimestamp(window["start"] / 1000, UTC).date()
    end = datetime.fromtimestamp((window["end"] - 1) / 1000, UTC).date()
    arrived = result.get("measured_arrival_count")
    counted = (
        f"{format_count(arrived)} events arrived"
        if arrived is not None
        else result.get("received_unavailable_reason") or "Arrivals could not be attributed"
    )
    return (
        f"Arrivals were counted for the requested dates only ({start} to {end} UTC: "
        f"{counted}); {result['unmeasured_day_count']} other days these groups touch "
        "were not counted."
    )


def verification_view(
    result: dict | None,
    status: dict | None,
    active_count: int,
    preview: bool,
    run: dict | None = None,
) -> dict:
    api = status["status"].upper() if status else "Not observed"
    checks = [
        ("Destination API task now", f"{api}; checked separately from counts"),
        ("Query method", "9.1 query API · Preview" if preview else "Logs REST daily counts"),
    ]
    if result is None:
        return {
            "outcome": "pending",
            "banner": (
                "info",
                "Count verification pending",
                "Run verification after the API reports a finished task.",
            ),
            "tiles": [
                (
                    "Expected from selected groups",
                    "Pending",
                    "Sum of the selected groups' event counts",
                ),
                ("Arrived at destination since this run started", "Pending", "Not yet queried"),
                (
                    "Excluded latest data",
                    f"{active_count} open group{'s' if active_count != 1 else ''}",
                    "Not imported",
                ),
            ],
            "rows": [],
            "checks": checks
            + [
                (
                    "Partitions",
                    "9.1 routing check pending" if preview else "9.0 API imports into Default",
                )
            ],
        }
    outcome = result["outcome"]
    missing = result.get("buckets", {}).get("not_received", [])
    missing_labels = [format_day_label(day) for day, _ in missing_days(result, run)]
    if missing:
        outcome = "partial"
        banner = (
            "bad",
            f"{len(missing)} selected group{'s' if len(missing) != 1 else ''} not received",
            (
                f"{', '.join(missing_labels)} {'is' if len(missing_labels) == 1 else 'are'} not on "
                "the destination; the API skipped the group. "
                if missing_labels
                else "The API skipped a selected group. "
            )
            + (
                "Received groups match their counts. "
                if received_counts_match(result)
                else "Daily counts also differ in the received groups; inspect the comparison. "
            )
            + "Plan a window that covers the missing history again, or export the evidence "
            "for the appliance administrator.",
        )
    elif outcome == "verified_success":
        partial = result.get("partial_days", [])
        banner = (
            "warn" if active_count or partial else "ok",
            "Included history verified",
            "Complete repeated daily counts match on every fully covered day. "
            + (
                f"{len(partial)} partial day{'s' if len(partial) != 1 else ''} shared with open "
                "or unselected groups "
                + ("differ as expected. " if result.get("partial_differences") else "match too. ")
                if partial
                else ""
            )
            + (
                "Open groups were excluded; requested coverage is not complete."
                if active_count
                else ""
            ),
        )
    elif outcome == "verified_discrepancy":
        differing = [d for d in result["daily"] if d["difference"] != 0 and not d.get("partial")]
        differing = differing or [d for d in result["daily"] if d["difference"] != 0]
        first = differing[0]
        banner = (
            "bad",
            "Daily counts differ",
            f"{abs(first['difference']):,} {'fewer' if first['difference'] < 0 else 'more'} "
            f"events on {format_day_label(first['day'])}. Imported data is unchanged; "
            "inspect the daily comparison before any new plan.",
        )
    else:
        reason = result.get("reason")
        task = status["status"] if status else None
        why = {
            "incomplete_evidence": "No selected group has been received yet, or the count queries "
            "did not complete and repeat consistently.",
            "no_fully_covered_days": (
                "No requested day is fully covered; open groups may contain "
                "late or backfilled data."
            ),
            "destination_busy": "The destination task is still running.",
            "topology_changed": "The destination identity changed since the plan.",
        }.get(reason or "", describe_error(reason) if reason else "")
        if result.get("detail"):
            why += f" {result['detail']}"
        if task in {"cancelled", "failed"}:
            why += (
                f" The API task {task}; plan the remainder with a window that excludes "
                "received groups."
            )
        banner = (
            "warn",
            "Verification incomplete",
            (why + " " if why else "")
            + "Received partial counts are not a destination total; no success is claimed.",
        )
    complete = result.get("complete", False)
    has_counts = "expected_count" in result
    unmeasured = result.get("unmeasured_day_count", 0)
    measured_window = result.get("measured_window")
    excluded_tile = (
        "Excluded latest data",
        f"{active_count} open group{'s' if active_count != 1 else ''}",
        "Not imported",
    )
    if unmeasured and measured_window:
        tiles = [
            (
                "Sent from selected groups",
                format_count(result.get("expected_count")) if has_counts else "Pending",
                unmeasured_arrival_sentence(result) if has_counts else "Not yet comparable",
            ),
            excluded_tile,
        ]
    else:
        tiles = [
            (
                "Expected from selected groups",
                format_count(result.get("expected_count")) if has_counts else "Pending",
                "Sum of the selected groups' event counts" if has_counts else "Not yet comparable",
            ),
            (
                "Arrived at destination since this run started",
                format_count(result.get("actual_count")) if has_counts else "Pending",
                (
                    result.get("received_unavailable_reason")
                    or (
                        "Destination count minus the recorded baseline"
                        if result.get("actual_count") is not None
                        else "Not available: the counts were incomplete"
                    )
                )
                if has_counts
                else "Not yet comparable",
            ),
            excluded_tile,
        ]
    rows = daily_rows(result, run) if result.get("daily") else []
    buckets = result.get("buckets", {})
    if buckets:
        received = buckets.get("covered", [])
        checks.append(
            (
                "Groups",
                f"{len(received)} received"
                + (
                    f" · {len(missing)} not received"
                    + (f" ({', '.join(missing_labels)})" if missing_labels else "")
                    if missing
                    else ""
                ),
            )
        )
    checks.append(
        ("Partitions", "9.1 routing check pending" if preview else "9.0 API imports into Default")
    )
    checks.append(("Evidence", result.get("method", "Pending") if complete else "Incomplete"))
    checks.append(
        ("Scope", "Cumulative: every group this destination has received so far, across runs")
    )
    return {
        "outcome": outcome,
        "banner": banner,
        "tiles": tiles,
        "rows": rows,
        "checks": checks,
        "missing_groups": missing_groups(result, run),
    }


# Configuration ----------------------------------------------------------------------------

KIND_LABEL = {
    "forwarding": "Forwarding",
    "partitions": "Partition",
    "masking": "Masking",
    "filters": "Filtering",
}


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "unset"
    return str(value)


def rule_summary(kind: str, rule: dict | None) -> str:
    """The substance of a rule in one line: target, criteria, mask or filter."""
    if not rule:
        return "Not readable"
    if kind == "forwarding":
        target = f"{rule.get('host', '?')}:{rule.get('port', '?')}"
        protocol = str(rule.get("protocol", "")).lower() or "?"
        transport = str(rule.get("transportProtocol", "")).lower()
        tls = "TLS" if rule.get("sslEnabled") else "plain"
        parts = [target, protocol + (f"/{transport}" if transport else ""), tls]
        if rule.get("filter"):
            parts.append(f"filter {rule['filter']}")
        return " · ".join(parts)
    if kind == "partitions":
        criteria = rule.get("criteria") or "all events"
        seconds = rule.get("retention_seconds") or rule.get("retentionSeconds") or 0
        retention = f"{int(seconds) // 86400} days" if seconds else "no retention limit"
        return f"{criteria} · {retention}"
    if kind == "masking":
        selector = rule.get("selector", "?")
        mask = rule.get("maskValue", "****")
        field_name = rule.get("fieldName", "text")
        state = "enabled" if rule.get("enabled", True) else "disabled"
        return f"{selector} → {mask} ({field_name}) · {state}"
    if kind == "filters":
        state = "enabled" if rule.get("enabled") else "disabled"
        return f"{rule.get('filter', '?')} · {state}"
    return ", ".join(f"{k}={_value(v)}" for k, v in rule.items() if k not in {"id", "name"})


def config_rows(comparison: dict | None) -> list[dict]:
    if comparison is None:
        return []
    details = comparison.get("details", {})
    rows = []
    for rule in comparison["rules"]:
        kind, status = rule["type"], rule["status"]
        changed = rule.get("changed_fields", [])
        warnings = rule.get("warnings", [])
        tls = any("acceptCert" in w for w in warnings)
        detail = details.get(rule["selector"], {})
        source_rule, destination_rule = detail.get("source"), detail.get("destination")
        if detail:
            source = rule_summary(kind, source_rule)
        elif status == "differs" and changed:
            source = "Differs in " + ", ".join(changed)
        else:
            source = "Source rule"
        if status == "missing":
            destination = "Missing"
            action = "Create"
            if kind == "partitions":
                action += " · one destination restart"
            elif kind == "forwarding" and tls:
                action += " · trust target certificate"
            else:
                action += " unchanged"
        elif status == "differs":
            if destination_rule is not None and changed:
                destination = "Same name · " + ", ".join(
                    f"{field}: {_value(destination_rule.get(field))}" for field in changed[:3]
                )
                action = "Update " + ", ".join(
                    f"{field}: {_value(destination_rule.get(field))} → "
                    f"{_value((source_rule or {}).get(field))}"
                    for field in changed[:3]
                )
            else:
                destination = "Same name · differs"
                action = "Update " + (", ".join(changed) if changed else "definition")
        else:
            destination = "Identical"
            action = "Skip"
        rows.append(
            {
                "selector": rule["selector"],
                "rule": f"{KIND_LABEL.get(kind, kind)} · {rule['name']}",
                "source": source,
                "destination": destination,
                "action": action,
                "selectable": status != "identical",
                "selected": status != "identical",
                "kind": kind,
                "status": status,
                "changed": changed,
                "warnings": warnings,
            }
        )
    return rows


RESULT_LABEL = {
    "created": "Created",
    "updated": "Updated",
    "unchanged": "Identical; skipped",
    "skipped": "Not selected",
    "failed": "Failed",
    "uncertain": "Uncertain",
}


def config_result_rows(result: dict | None) -> list[tuple[str, str, str]]:
    if result is None:
        return []
    rows = []
    for rule in result["rules"]:
        status = rule["status"]
        label = RESULT_LABEL.get(status, status)
        if status in {"failed", "uncertain"} and rule.get("reason"):
            label += " · " + rule["reason"].replace("_", " ")
        if status == "created" and rule["type"] == "forwarding":
            label = (
                "Created; TLS target trusted"
                if any("acceptCert" in w for w in rule.get("warnings", []))
                else "Created"
            )
        if rule.get("restart_required"):
            step = "Restart required before routing"
        elif status in {"created", "updated"}:
            step = "Ready; read-back matched"
        elif status == "failed":
            step = "Review the rule, compare again, then retry this rule only"
        elif status == "uncertain":
            step = "Compare again before selecting a retry"
        else:
            step = "—"
        rows.append((f"{KIND_LABEL.get(rule['type'], rule['type'])} · {rule['name']}", label, step))
    return rows


def config_banner(result: dict | None) -> tuple[str, str, str] | None:
    if result is None:
        return None
    applied = sum(1 for r in result["rules"] if r["status"] in {"created", "updated"})
    failed = [r for r in result["rules"] if r["status"] in {"failed", "uncertain"}]
    if result["complete"]:
        return (
            "ok",
            "Selected rules applied",
            f"{applied} rule{'s' if applied != 1 else ''} created or updated on the "
            "destination. Nothing was deleted; source rules are unchanged.",
        )
    names = ", ".join(r["name"] for r in failed)
    return (
        "warn",
        f"{applied} rule{'s' if applied != 1 else ''} applied; {len(failed)} need attention",
        f"{names}: successful changes remain. Compare again, then retry only the failed rule.",
    )


# Reports ----------------------------------------------------------------------------------


def report_view(
    run: dict,
    events: list[dict],
    status: dict | None,
    zone: str,
    hosts: tuple[str, str] | None = None,
    index: int | None = None,
) -> dict:
    plan = run["plan"]
    verification = run["verification"]
    outcome = verification["outcome"]
    missing = verification.get("buckets", {}).get("not_received", [])
    authorized = next((e["at"] for e in events if e["kind"] == "transfer_authorized"), None)
    task_state = status["status"].upper() if status else "Not observed"
    name = f"Run {index}" if index is not None else f"Run {run['id'][:8]}"
    not_started = "source_by_bucket" not in run
    return {
        "subtitle": f"{name} · {'not started' if not_started else outcome_label(verification)}",
        "banner": (
            "bad",
            f"{len(missing)} selected group{'s' if len(missing) != 1 else ''} not received",
            "The API skipped part of the selection; "
            + (
                "received groups match their counts. "
                if received_counts_match(verification)
                else "daily counts also differ in the received groups. "
            )
            + "This is not a complete migration of the requested dates.",
        )
        if missing
        else {
            "verified_success": (
                "warn" if plan["active_skipped"] else "ok",
                "Included history verified",
                "Complete repeated daily counts matched. "
                + (
                    "Open groups were excluded as acknowledged."
                    if plan["active_skipped"]
                    else "Every selected group was received."
                ),
            ),
            "verified_discrepancy": (
                "bad",
                "Daily counts differ",
                "Verification found a difference. Imported data is unchanged.",
            ),
            "incomplete_verification": (
                "warn",
                "Verification incomplete",
                "Count evidence is incomplete; no success is claimed.",
            ),
        }.get(
            outcome,
            ("info", "Not started", "This window was previewed but never submitted.")
            if not_started
            else ("info", "Verification pending", "Run verification after the API task finishes."),
        ),
        "rows": [
            (
                "Source → destination",
                (f"{hosts[0]} → {hosts[1]} · " if hosts else "")
                + f"{format_version(plan['source']['version'])} → "
                f"{format_version(plan['destination']['version'])}",
            ),
            (
                "Requested / included",
                f"{format_day_range(plan['window']['start'], plan['window']['end'], suffix='')}"
                f" / {format_day_range(plan['coverage']['start'], plan['coverage']['end'])}",
            ),
            (
                "Destination API task now",
                f"{status.get('id') if status else UNAVAILABLE} / {task_state}",
            ),
            (
                "Selected groups",
                f"{len(plan['selected'])} sealed · {len(plan['active_skipped'])} open excluded",
            ),
            *report_headline_rows(verification),
            (
                "Existing outside dates",
                format_count(verification.get("preexisting_count")) + "; counted separately"
                if verification.get("complete")
                else "Pending",
            ),
            ("Verification", outcome_label(verification).capitalize()),
        ],
        "ack_text": "“I understand you aren't going to copy the latest data.”",
        "ack_recorded": (
            f"Recorded before start · {format_time(authorized, zone)}"
            if run.get("active_ack")
            else ("Not required; no open groups" if not plan["active_skipped"] else "Not recorded")
        ),
        "events": len(events),
    }


def run_label(run: dict, index: int | None = None) -> str:
    plan = run["plan"]
    name = f"Run {index}" if index is not None else f"Run {run['id'][:8]}"
    state = "not started" if "source_by_bucket" not in run else outcome_label(run["verification"])
    return f"{name} · {format_day_range(plan['window']['start'], plan['window']['end'])} · {state}"


def plan_error_kind(code: str, discovered: dict | None, window: Window | None) -> str:
    """An empty window is an answer, not a failure."""
    text = plan_error_text(code, discovered, window)
    return "info" if text.startswith("No sealed history") else "bad"


def report_markdown(
    run: dict,
    events: list[dict],
    status: dict | None,
    zone: str,
    hosts: tuple[str, str] | None = None,
    index: int | None = None,
    config_result: dict | None = None,
) -> str:
    """The plain report: the same words the Reports and Verification pages show."""
    report = report_view(run, events, status, zone, hosts, index)
    verification = run["verification"]
    started = "source_by_bucket" in run
    view = (
        verification_view(
            verification if verification.get("outcome") != "verification_pending" else None,
            status,
            len(run["plan"]["active_skipped"]),
            run["plan"]["destination"]["version"].startswith("9.1.1"),
            run,
        )
        if started
        else None
    )
    status_kind, title, text = report["banner"]
    lines = [
        f"# Migration report · {report['subtitle']}",
        "",
        f"**{title}.** {text}",
        "",
        "## Migration",
        "",
    ]
    lines += [f"- {key}: {value}" for key, value in report["rows"]]
    if view is not None:
        lines += ["", "## Daily reconciliation (UTC)", ""]
        if view["rows"]:
            lines += [
                "| Day / period | Expected | Destination | Result |",
                "|---|---:|---:|---|",
            ]
            lines += [f"| {d} | {e} | {a} | {r} |" for d, e, a, r in view["rows"]]
        else:
            lines.append("No complete counts yet.")
        if view["missing_groups"]:
            lines += ["", "## Not received", ""]
            lines += [f"- {name}" for name in view["missing_groups"]]
        lines += ["", "## Checks", ""]
        lines += [f"- {key}: {value}" for key, value in view["checks"]]
    if config_result is not None:
        lines += ["", "## Configuration", ""]
        lines += [f"- {rule}: {result}" for rule, result, _ in config_result_rows(config_result)]
    lines += [
        "",
        "## Latest-data acknowledgment",
        "",
        report["ack_text"],
        "",
        report["ack_recorded"],
        "",
        "Counts are evidence, not proof of identical event content. The source was never "
        "changed and no destination data was deleted. "
        f"Audit events recorded: {report['events']}. Exported {format_time(now_iso(), zone)}.",
    ]
    return "\n".join(lines) + "\n"


# Input validation (pure) -----------------------------------------------------------------


def missing_fields(values: dict[str, str], preview: bool) -> list[str]:
    """Labels of the connection fields that must be filled before a test."""
    required = [
        ("source_host", "Cluster member"),
        ("ssh_user", "SSH user"),
        ("ssh_password", "SSH password"),
        ("source_user", "Logs API user"),
        ("source_password", "Logs API password"),
        ("destination_host", "Logs host and port"),
    ]
    if preview:
        required += [("ops_url", "VCF Operations URL"), ("ops_token", "VCF Operations token")]
    else:
        required += [
            ("destination_user", "Destination Logs API user"),
            ("destination_password", "Destination Logs API password"),
        ]
        if (values.get("destination_ssh_user") or "").strip():
            required.append(("destination_ssh_password", "Destination SSH password"))
    return [label for key, label in required if not (values.get(key) or "").strip()]


def validate_settings(fixture: str, journal: str, timezone: str) -> dict[str, str]:
    """Field name to reason for every setting that cannot be saved as typed."""
    from pathlib import Path
    from zoneinfo import available_timezones

    problems: dict[str, str] = {}
    if fixture and not Path(fixture).expanduser().is_file():
        problems["fixture"] = f"File not found: {fixture}"
    if not journal:
        problems["journal"] = "A journal file path is required."
    else:
        parent = Path(journal).expanduser().parent
        if parent.exists() and not parent.is_dir():
            problems["journal"] = f"Not a folder: {parent}"
        elif Path(journal).expanduser().is_dir():
            problems["journal"] = "The journal path must name a file, not a folder."
    if timezone not in available_timezones() and timezone != "UTC":
        problems["timezone"] = f"Unknown time zone: {timezone}"
    return problems
