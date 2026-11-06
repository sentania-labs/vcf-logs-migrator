"""Reports derive from durable evidence; never turn transfer completion into success."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import State


def report_data(run: dict, events: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "run": run,
        "events": events,
        "execution_backend": "simulation",
        "qualification": "Fixture evidence only. No real appliance execution is enabled.",
        "next_actions": next_actions(run),
    }


def next_actions(run: dict) -> list[str]:
    if run["service_obligations"]:
        return [
            "Reconcile the run-owned recovery marker and catalog backup before recovery.",
            "A damaged catalog requires manual restoration from its backup; do not delete buckets.",
        ]
    if run["reason"] == "bucket_identity_conflict":
        return [
            "Preserve the conflicting bucket and its catalog entry.",
            "Compare source and destination manifests on every node and record the mismatch.",
            "Use a clean destination or obtain a separately reviewed manual recovery procedure.",
            "The tool cannot repair this by deleting or overwriting destination data.",
        ]
    if run["state"] == State.VERIFIED:
        return ["Retain this evidence and the journal. Active buckets remain excluded."]
    if run["state"] == State.VERIFYING:
        return ["Run verify; transfer completion is not verified migration success."]
    if run["state"] == State.RECOVERY_REQUIRED:
        return ["Reattach to the existing task and inspect live state. Do not repeat the POST."]
    return ["Resolve the recorded blocker, rediscover, and review a new immutable plan."]


def markdown(run: dict, events: list[dict], timezone: str = "America/Chicago") -> str:
    def local(value):
        return (
            datetime.fromisoformat(value)
            .astimezone(ZoneInfo(timezone))
            .strftime("%Y-%m-%d %H:%M:%S %Z")
        )

    plan = run.get("plan") or {}
    verify = run["verification"]
    lines = [
        "# Log migration evidence",
        "",
        f"Run: `{run['id']}`",
        "",
        f"State: **{run['state']}**. Verification: **{verify['outcome']}**.",
        "",
        "Fixture evidence only. Real appliance execution remains release-gated.",
        "",
        f"Created: {local(run['created_at'])}. Updated: {local(run['updated_at'])}.",
        "",
        f"Plan fingerprint: `{run.get('plan_hash')}`.",
        "",
        f"Backup: {'operator attested' if run['backup_ack'] else 'not acknowledged'}; "
        "not independently verified.",
        "",
    ]
    if plan:
        lines += [
            f"Source: {plan['source']['id']} ({plan['source']['version']}); "
            f"{len(plan['source']['nodes'])} nodes.",
            "",
            f"Destination: {plan['destination']['id']} ({plan['destination']['version']}); "
            f"{len(plan['destination']['nodes'])} nodes.",
            "",
            f"Requested range (Unix ms, half-open): {plan['window']}.",
            "",
            f"Whole-bucket coverage: {plan['coverage']}.",
            "",
            f"Active buckets skipped: {len(plan['active_skipped'])}. Last fully sealed day: "
            f"{plan['last_fully_sealed_day'] or 'unknown because active bounds are unproven'}.",
            "",
            "## Bucket outcomes",
            "",
            "| Bucket | Outcome |",
            "|---|---|",
        ]
        lines += [f"| `{key}` | {value} |" for key, value in run["bucket_outcomes"].items()]
        lines += ["", "## Warnings", ""] + [f"- {warning}" for warning in plan["warnings"]]
    if "daily" in verify:
        lines += [
            "",
            "## Daily reconciliation",
            "",
            "| Day | Source | Pre-existing | New ingest | Expected | Actual | Difference |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in verify["daily"]:
            lines.append(
                "| "
                + " | ".join(
                    str(row[key])
                    for key in (
                        "day",
                        "source",
                        "preexisting",
                        "new_ingest",
                        "expected",
                        "actual",
                        "difference",
                    )
                )
                + " |"
            )
    lines += ["", "## Service interventions", ""]
    service_events = [e for e in events if e["kind"].startswith(("service_", "catalog_", "fixup_"))]
    lines += [
        f"- {local(e['at'])}: {e['kind']} ({json.dumps(e['details'], sort_keys=True)})."
        for e in service_events
    ] or ["None."]
    lines += ["", "## Next actions", ""] + [f"- {a}" for a in next_actions(run)]
    return "\n".join(lines) + "\n"
