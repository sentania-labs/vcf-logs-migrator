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
        "Live appliance connections are not qualified in this build. "
        "Choose a simulator fixture in Settings to run the migration workflow."
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
}


def describe_error(code: str) -> str:
    try:
        return ERROR_TEXT[Code(code)]
    except ValueError:
        return "The operation failed. Query the API status before retrying."


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
    if end_inclusive < start:
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


def connection_checks(discovered: dict | None, compatible: str | None) -> list[tuple[str, str]]:
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
        ("Appliance addresses", "Not contacted; this build reads the simulator fixture"),
        (
            "Version compatibility",
            "Passed" if compatible is None else "Failed · " + describe_error(compatible),
        ),
        ("Destination → every source node on SSH", "Checked when the transfer starts"),
        ("Credentials", "Session memory only; excluded from reports"),
    ]


# Plan -------------------------------------------------------------------------------------


def _bucket_label(bucket: dict) -> str:
    return f"{bucket['node']} · {bucket['id'][:8]}"


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
            _bucket_label(b),
            format_day_range(b["start"], b["end"], inclusive_end=True, suffix=""),
            f"{format_bytes(b['size'])} / {format_count(b['messages'])}",
            "Import",
        )
        for b in selected
    ]
    for key in active:
        node, _, ident = key.rpartition("/")
        groups.append((f"{node} · {ident[:8]}", "Open; may advance", "Unknown", "Open · exclude"))
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
    checks.append(
        (
            "Storage",
            "Every node fits" if capacity["fits"] else "A node cannot hold the selection",
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
    required = capacity["required_bytes"]
    storage = [
        (
            n["id"],
            format_bytes(n["available_bytes"]),
            format_bytes(required),
            format_bytes(n["available_bytes"] - required) if n["fits"] else "Short",
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
        "active_count": len(active),
        "groups": groups,
        "checks": checks,
        "storage": storage,
        "warnings": plan["warnings"],
        "blockers": blockers,
        "needs_ack": bool(active),
    }


# Refusals the engine raises before any POST; every other failure may have reached the API.
PRE_POST_CODES = {
    Code.INVALID_INPUT,
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


def start_note(code: str) -> str:
    """Footer text after a failed Start; only pre-POST refusals promise nothing was sent."""
    try:
        before_post = Code(code) in PRE_POST_CODES
    except ValueError:
        before_post = False
    if before_post:
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


def progress_view(status: dict | None, *, stale: bool, last_ok: str | None, zone: str) -> dict:
    if status is None:
        return {
            "state": "idle",
            "banner": (
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
            format_bytes_pair(done, total),
            "Last API sample" if stale else format_percent(done, total),
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
    for node in status.get("nodes", []):
        node_state = "Unknown" if stale else STATE_LABEL.get(node["status"], node["status"].upper())
        buckets = f"{len(node['completed'])} received"
        if node["skipped"]:
            buckets += f" · {len(node['skipped'])} skipped"
        error = (
            "Last response stale"
            if stale and node["status"] == "running"
            else (f"{len(node['skipped'])} skipped by the API" if node["skipped"] else "—")
        )
        nodes.append(
            (node["node"], node_state, format_bytes(node["bytes_transferred"]), buckets, error)
        )
    window = status.get("window")
    details = [
        ("API task", status.get("id") or UNAVAILABLE),
        ("Window", format_day_range(window["start"], window["end"]) if window else UNAVAILABLE),
        (
            "Source → destination",
            f"{status.get('source') or '?'} → {status.get('destination') or '?'}",
        ),
        ("Last successful poll", format_time(last_ok, zone)),
        (
            "Buckets",
            f"{len(status.get('completed', ()))} received · "
            f"{len(status.get('skipped', ()))} skipped · "
            f"{len(status.get('bucket_keys', ()))} requested",
        ),
    ]
    progress = None
    if done is not None and total:
        progress = max(0, min(100, int(100 * done / total)))
    return {
        "state": state,
        "banner": banner,
        "tiles": tiles,
        "nodes": nodes,
        "details": details,
        "progress": progress,
    }


# Verification -----------------------------------------------------------------------------


def compress_daily(daily: list[dict]) -> list[tuple[str, str, str, str]]:
    """Consecutive matching days collapse into one period row; differences stand alone."""
    rows = []
    run: list[dict] = []

    def flush():
        if not run:
            return
        first, last = run[0]["day"], run[-1]["day"]
        label = (
            format_day_label(first)
            if first == last
            else f"{format_day_label(first)}–{format_day_label(last)}"
        )
        rows.append(
            (
                label,
                format_count(sum(r["expected"] for r in run)),
                format_count(sum(r["actual"] for r in run)),
                "Match",
            )
        )
        run.clear()

    for row in daily:
        if row["difference"] == 0:
            if run and date.fromisoformat(row["day"]) - date.fromisoformat(
                run[-1]["day"]
            ) != timedelta(days=1):
                flush()
            run.append(row)
            continue
        flush()
        rows.append(
            (
                format_day_label(row["day"]),
                format_count(row["expected"]),
                format_count(row["actual"]),
                f"{row['difference']:+,}".replace("-", "−"),
            )
        )
    flush()
    return rows


def verification_view(
    result: dict | None, status: dict | None, active_count: int, preview: bool
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
                    "Expected in included dates",
                    "Pending",
                    "Selected sealed history plus destination baseline and new ingest",
                ),
                ("Destination in included dates", "Pending", "Not yet queried"),
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
    if outcome == "verified_success":
        banner = (
            "ok",
            "Included history verified",
            "Complete repeated daily counts match. "
            "Open groups were excluded; requested coverage is not complete."
            if active_count
            else "Complete repeated daily counts match.",
        )
    elif outcome == "verified_discrepancy":
        differing = [d for d in result["daily"] if d["difference"] != 0]
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
        banner = (
            "warn",
            "Verification incomplete",
            (
                describe_error(reason)
                if reason
                else "Count queries did not complete or repeat consistently."
            )
            + " Received partial counts are not a destination total.",
        )
    complete = result.get("complete", False)
    tiles = [
        (
            "Expected in included dates",
            format_count(result.get("expected_count")) if complete else "Pending",
            f"Selected history {format_count(result.get('source_count'))} + pre-existing "
            f"{format_count(result.get('preexisting_count'))} + new ingest "
            f"{format_count(result.get('new_ingest_count'))}"
            if complete
            else "Not yet comparable",
        ),
        (
            "Destination in included dates",
            format_count(result.get("actual_count")) if complete else "Pending",
            "Complete repeated queries" if complete else "Not yet comparable",
        ),
        (
            "Excluded latest data",
            f"{active_count} open group{'s' if active_count != 1 else ''}",
            "Not imported",
        ),
    ]
    rows = compress_daily(result["daily"]) if complete else []
    buckets = result.get("buckets", {})
    if buckets:
        received = buckets.get("covered", [])
        missing = buckets.get("not_received", [])
        checks.append(
            (
                "Buckets",
                f"{len(received)} received"
                + (f"; not received: {', '.join(missing)}" if missing else ""),
            )
        )
    checks.append(
        ("Partitions", "9.1 routing check pending" if preview else "9.0 API imports into Default")
    )
    checks.append(("Evidence", result.get("method", "Pending") if complete else "Incomplete"))
    return {"outcome": outcome, "banner": banner, "tiles": tiles, "rows": rows, "checks": checks}


# Configuration ----------------------------------------------------------------------------

KIND_LABEL = {
    "forwarding": "Forwarding",
    "partitions": "Partition",
    "masking": "Masking",
    "filters": "Filtering",
}


def config_rows(comparison: dict | None) -> list[dict]:
    if comparison is None:
        return []
    rows = []
    for rule in comparison["rules"]:
        kind, status = rule["type"], rule["status"]
        changed = rule.get("changed_fields", [])
        warnings = rule.get("warnings", [])
        tls = any("acceptCert" in w for w in warnings)
        if status == "missing":
            destination = "Missing"
            action = "Create"
            if kind == "partitions":
                action += " · one destination restart"
            elif kind == "forwarding" and tls:
                action += " · TLS certificate accepted"
            else:
                action += " unchanged"
        elif status == "differs":
            destination = "Same name · differs"
            action = "Update " + (", ".join(changed) if changed else "definition")
        else:
            destination = "Identical"
            action = "Skip"
        rows.append(
            {
                "selector": rule["selector"],
                "rule": f"{KIND_LABEL.get(kind, kind)} · {rule['name']}",
                "source": "Source rule"
                if status != "differs"
                else "Differs in " + ", ".join(changed),
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


def report_view(run: dict, events: list[dict], status: dict | None, zone: str) -> dict:
    plan = run["plan"]
    verification = run["verification"]
    outcome = verification["outcome"]
    authorized = next((e["at"] for e in events if e["kind"] == "transfer_authorized"), None)
    task_state = status["status"].upper() if status else "Not observed"
    return {
        "subtitle": f"{run['id'][:8]} · {outcome.replace('_', ' ')}",
        "banner": {
            "verified_success": (
                "ok",
                "Included history verified",
                "Complete repeated daily counts matched. "
                "Open groups were excluded as acknowledged.",
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
            ("info", "Verification pending", "Run verification after the API task finishes."),
        ),
        "rows": [
            (
                "Source → destination",
                f"{format_version(plan['source']['version'])} → "
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
            (
                "Expected / actual in dates",
                f"{format_count(verification.get('expected_count'))} / "
                f"{format_count(verification.get('actual_count'))}"
                if verification.get("complete")
                else "Pending",
            ),
            (
                "Existing outside dates",
                format_count(verification.get("preexisting_count")) + "; counted separately"
                if verification.get("complete")
                else "Pending",
            ),
            ("Verification", verification.get("method", outcome.replace("_", " "))),
        ],
        "ack_text": "“I understand you aren't going to copy the latest data.”",
        "ack_recorded": (
            f"Recorded before start · {format_time(authorized, zone)}"
            if run.get("active_ack")
            else ("Not required; no open groups" if not plan["active_skipped"] else "Not recorded")
        ),
        "events": len(events),
    }


def run_label(run: dict) -> str:
    plan = run["plan"]
    return (
        f"{run['id'][:8]} · "
        f"{format_day_range(plan['window']['start'], plan['window']['end'])} · "
        f"{run['verification']['outcome'].replace('_', ' ')}"
    )
