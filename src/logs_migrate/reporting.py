"""Plain count evidence and audit reports, never an alternate source of task status."""

import json


def report_data(run, events):
    return {
        "schema_version": 2,
        "run": run,
        "events": events,
        "qualification": (
            "Count evidence from the appliances' own query APIs; counts do not prove "
            "identical event content."
        ),
        "task_status": (
            "Query status for the current destination API task; this is an audit report."
        ),
    }


def markdown(run, events):
    plan = run["plan"]
    verification = run["verification"]
    lines = [
        "# Migration count report",
        "",
        f"Run: `{run['id']}`",
        "",
        "Count evidence only. Query `status` for current API task progress.",
        "",
        f"Verification: **{verification['outcome']}**. "
        f"Complete query evidence: **{verification.get('complete', False)}**.",
        "",
        "Snapshot acknowledgment: "
        + ("operator attested" if run["backup_ack"] else "not given")
        + "; snapshots were not independently verified.",
        "",
        f"Requested dates (Unix ms): {json.dumps(plan['window'], sort_keys=True)}.",
        f"Whole-bucket coverage (Unix ms): {json.dumps(plan['coverage'], sort_keys=True)}.",
        f"Active buckets skipped: {len(plan['active_skipped'])}.",
        f"Last fully sealed day: {plan['last_fully_sealed_day'] or 'unknown'}.",
        "",
    ]
    if "buckets" in verification:
        buckets = verification["buckets"]
        lines += [
            f"Covered buckets: {len(buckets['covered'])}; "
            f"selected but not received: {', '.join(buckets['not_received']) or 'none'}.",
            f"Covered days: {', '.join(verification['covered_days']) or 'none'}.",
            "",
        ]
    if verification.get("reason"):
        lines += [f"Reason: {verification['reason']}.", ""]
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
