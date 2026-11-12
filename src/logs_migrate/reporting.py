"""Plain count evidence and audit reports, never an alternate source of task status."""

from datetime import UTC, date, datetime, timedelta

from .models import LEGACY_RESULT_NOTE, is_legacy_result

# Older journals contain this product prompt. Match it exactly so appliance evidence
# mentioning a snapshot or backup for some unrelated reason is not discarded.
_RETIRED_WARNING = (
    "No undo: migrated data stays on the destination. Snapshot the destination before "
    "continuing (snapshotting the source too is optional)."
)


def display_run(run):
    """Project a saved run for display without changing its original evidence or hash."""
    return {
        **{
            key: value for key, value in run.items() if key not in {"backup_ack", "backup_verified"}
        },
        "plan": {
            **run["plan"],
            "warnings": [w for w in run["plan"]["warnings"] if w != _RETIRED_WARNING],
        },
    }


def display_events(events):
    """Omit only the retired acknowledgment detail; keep the audit event itself."""
    return [
        {
            **event,
            "details": {
                key: value for key, value in event["details"].items() if key != "snapshot_verified"
            },
        }
        if event["kind"] == "transfer_authorized"
        else event
        for event in events
    ]


def report_data(run, events):
    return {
        "schema_version": 2,
        "run": display_run(run),
        "events": display_events(events),
        "qualification": (
            "Count evidence from the appliances' own query APIs; counts do not prove "
            "identical event content."
        ),
        "task_status": (
            "Query status for the current destination API task; this is an audit report."
        ),
    }


def _timestamp(ms):
    return (
        datetime.fromtimestamp(ms / 1000, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", " UTC")
    )


def _day_ranges(days):
    dates = sorted({date.fromisoformat(day) for day in days})
    ranges = []
    for day in dates:
        if ranges and day == ranges[-1][1] + timedelta(days=1):
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ", ".join(str(a) if a == b else f"{a} to {b}" for a, b in ranges) or "none"


def markdown(run, events):
    run = display_run(run)
    plan = run["plan"]
    verification = run["verification"]
    daily = verification.get("daily", [])
    matched = sum(not row.get("partial") and row["difference"] == 0 for row in daily)
    partial = sum(bool(row.get("partial")) for row in daily)
    expected = verification.get("expected_count")
    unmeasured = verification.get("unmeasured_day_count", 0)
    arrived = (
        verification.get("measured_arrival_count")
        if unmeasured
        else verification.get("actual_count")
    )
    measured_window = verification.get("measured_window")

    def show(value):
        return f"{value:,}" if value is not None else "unavailable"

    if is_legacy_result(verification):
        sent_text = "figures not shown"
        arrival_text = LEGACY_RESULT_NOTE.rstrip(".")
    elif unmeasured and measured_window:
        sent_text = f"{show(expected)} events sent from selected groups"
        arrival_text = (
            "arrivals were counted for the requested dates only "
            f"({_timestamp(measured_window['start'])} to {_timestamp(measured_window['end'])}, "
            f"end exclusive: {show(arrived)} arrived); {unmeasured} other days these groups "
            "touch were not counted; see the daily table"
        )
    else:
        sent_text = f"{show(expected)} events expected"
        arrival_text = f"{show(arrived)} arrived at destination since this run started"
    exact = verification.get("bucket_check")
    if exact:
        headline = (
            f"{exact['matched_groups']} of {exact['total_groups']} groups registered "
            f"on the destination with matching counts; {show(exact['matched_events'])} "
            f"of {show(exact['total_events'])} events. Search-level check: {arrival_text}."
        )
    else:
        share = (
            f" ({min(100, arrived * 100 / expected):.1f}% of selected events counted)"
            if expected and arrived is not None
            else " (verified share unavailable)"
        )
        headline = (
            f"{len(plan['selected'])} selected groups; {sent_text}; "
            f"{arrival_text}{share}; {matched} fully matched days; {partial} partial days."
        )
    lines = [
        "# Migration count report",
        "",
        headline,
        "",
        "- Last day covered only by sealed buckets right now: "
        f"{plan['last_fully_sealed_day'] or 'unknown'}.",
        "",
        f"Run: `{run['id']}`",
        "",
        "Count evidence only. Query `status` for current API task progress.",
        verification.get("received_unavailable_reason") or "",
        verification.get("expected_unavailable_reason") or "",
        "",
        f"Verification: **{verification['outcome']}**. "
        f"Complete query evidence: **{verification.get('complete', False)}**.",
        "",
        f"Requested dates: {_timestamp(plan['window']['start'])} to "
        f"{_timestamp(plan['window']['end'])} (end exclusive).",
        f"Whole-bucket coverage: {_timestamp(plan['coverage']['start'])} to "
        f"{_timestamp(plan['coverage']['end'])} (end exclusive).",
        f"Active buckets skipped: {len(plan['active_skipped'])}.",
        *plan.get("warnings", []),
        *(
            [
                "Sealed buckets excluded for having no time range: "
                + ", ".join(f"{b['node']}/{b['id']}" for b in plan["undated_excluded"])
                + "."
            ]
            if plan.get("undated_excluded")
            else []
        ),
        "",
    ]
    if "buckets" in verification:
        buckets = verification["buckets"]
        lines += [
            f"Covered buckets: {len(buckets['covered'])}; "
            f"selected but not received: {', '.join(buckets['not_received']) or 'none'}.",
            f"Covered days: {_day_ranges(verification['covered_days'])}.",
            "",
        ]
    if verification.get("incomplete_destination_copies"):
        lines += [
            "Incomplete or unregistered destination groups: "
            + ", ".join(verification["incomplete_destination_copies"])
            + ".",
            "",
        ]
    if verification.get("conflicting_destination_copies"):
        lines += [
            "Destination groups with conflicting message counts: "
            + ", ".join(verification["conflicting_destination_copies"])
            + ".",
            "",
        ]
    if verification.get("reason"):
        lines += [f"Reason: {verification['reason']}.", ""]
        if verification.get("detail"):
            lines += [f"Detail: {verification['detail']}", ""]
    if "daily" in verification:
        lines += [
            "| Day | Source | Pre-existing | New ingest | Expected | Actual | Difference |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in verification["daily"]:
            lines.append(
                "| "
                + " | ".join(
                    str(row[k])
                    for k in (
                        "day",
                        "source",
                        "preexisting",
                        "new_ingest",
                        "expected",
                        "actual",
                        "difference",
                    )
                )
                + (" (partial day) |" if row.get("partial") else " |")
            )
        if verification.get("partial_days"):
            lines += [
                "",
                "Partial days share events with open or unselected buckets; their differences "
                "are expected and do not decide the outcome.",
            ]
    lines += [
        "",
        "Counts do not establish full event-content equality.",
        "",
        f"Audit events: {len(events)}. No source writes or destination bucket deletions.",
    ]
    return "\n".join(lines) + "\n"
