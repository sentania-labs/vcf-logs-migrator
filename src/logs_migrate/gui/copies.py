"""What the run record says about each copy, as plain data. No Qt, no appliance calls.

The engine records runs: one per previewed window, plus one for every continuation. A person
thinks in copies: "the copy of May to November". A copy is one or more parts (several when
days were left out in the middle, because the destination takes one unbroken date range per
request), and each part is its first run plus the runs that continued it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

DAY_MS = 86_400_000


def day_of(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, UTC).date()


def day_start_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def days_between(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def group_days(group: dict) -> list[date]:
    """Every UTC day a stored group holds logs for."""
    if group.get("start") is None or group.get("end") is None:
        return []
    return days_between(day_of(group["start"]), day_of(group["end"]))


def day_ranges(days) -> list[tuple[date, date]]:
    """Sorted days folded into runs of consecutive days."""
    ranges: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        if ranges and day == ranges[-1][1] + timedelta(days=1):
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def _id(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def build(runs: list[dict], rows: dict[str, dict]) -> list[dict]:
    """Group recorded runs into copies, oldest first. Previews never started are left out."""
    by_id = {run["id"]: run for run in runs}
    chains: dict[str, list[dict]] = {}
    for run in runs:
        root = run.get("resume_root", run["id"])
        if root in by_id:
            chains.setdefault(root, []).append(run)
    groups: dict[str, dict[int, dict]] = {}
    order: list[str] = []
    for root_id, chain in chains.items():
        root = by_id[root_id]
        chain = [root] + [run for run in chain if run["id"] != root_id]
        started = [run for run in chain if run["id"] in rows]
        series = (root.get("client") or {}).get("series")
        key = series["id"] if series else root_id
        index = series["index"] if series else 0
        part = {"index": index, "root": root, "runs": chain, "started": started}
        if key not in groups:
            groups[key] = {}
            order.append(key)
        held = groups[key].get(index)
        # A part previewed again before its turn replaces the earlier preview; a started
        # part is never replaced by a later preview.
        if held is None or (not held["started"]) or started:
            groups[key][index] = part
    copies = []
    for key in order:
        parts = [groups[key][index] for index in sorted(groups[key])]
        if not any(part["started"] for part in parts):
            continue
        copies.append(summarize(key, parts, rows))
    copies.sort(key=lambda copy: copy["started_at"] or "")
    return copies


def preview(parts_runs: list[dict], excluded: list[dict] | None = None) -> dict:
    """A copy that has only been planned, in the same shape as a recorded one."""
    parts = [
        {"index": index, "root": run, "runs": [run], "started": []}
        for index, run in enumerate(parts_runs)
    ]
    series = (parts_runs[0].get("client") or {}).get("series") if parts_runs else None
    copy = summarize(series["id"] if series else parts_runs[0]["id"], parts, {})
    if excluded is not None:
        copy["excluded"] = excluded
    return copy


def summarize(key: str, parts: list[dict], rows: dict[str, dict]) -> dict:
    seen: dict[str, dict] = {}
    already: dict[str, dict] = {}
    for part in parts:
        plan = part["root"]["plan"]
        for group in plan["selected"]:
            seen.setdefault(group["id"], group)
        for group in plan.get("already_received", []):
            already.setdefault(group["id"], group)
        ids = {group["id"] for group in plan["selected"]}
        received: set[str] = set()
        open_row = None
        for run in part["started"]:
            row = rows[run["id"]]
            if row["closed"]:
                received |= {_id(k) for k in row["received"]} & ids
            else:
                open_row = row
        last = part["started"][-1] if part["started"] else None
        finished = (last or {}).get("finished")
        if not part["started"]:
            state = "planned"
        elif open_row is not None and not finished:
            state = "running" if open_row.get("acknowledged") else "unsure"
        elif ids <= received:
            state = "done"
        else:
            state = {"cancelled": "stopped", "failed": "failed", "completed": "missing"}.get(
                (finished or {}).get("status"), "stopped"
            )
        part.update(
            {
                "ids": ids,
                "received": received,
                "state": state,
                "last": last,
                "finished": finished,
                # Open with a finished task: the destination could not say what arrived.
                "settled": open_row is None,
            }
        )
    for group_id in already:
        seen.pop(group_id, None)
    groups = list(seen.values())
    sizes = {group["id"]: group["size"] for group in groups}
    received_all = set().union(*(part["received"] for part in parts)) if parts else set()
    states = [part["state"] for part in parts]
    current = next((n for n, part in enumerate(parts) if part["state"] != "done"), None)
    if "running" in states or "unsure" in states:
        state = "running" if "running" in states else "unsure"
        current = states.index(state)
    elif current is None:
        state = "finished"
    elif parts[current]["state"] == "planned":
        # A multi-part copy whose next part has not been asked for yet.
        state = "between" if current else "planned"
    else:
        state = parts[current]["state"]
    done = 0
    known = True
    for part in parts:
        if part["settled"]:
            done += sum(sizes.get(i, 0) for i in part["received"])
        elif part["finished"] and part["finished"].get("bytes_transferred") is not None:
            done += part["finished"]["bytes_transferred"]
            known = False
    first = parts[0]["root"]
    client = first.get("client") or {}
    series = client.get("series")
    started_at = None
    for part in parts:
        for run in part["started"]:
            started_at = started_at or rows[run["id"]].get("attempted_at") or run["created_at"]
    elapsed = sum(
        (run.get("finished") or {}).get("elapsed_seconds") or 0
        for part in parts
        for run in part["started"]
    )
    days = sorted({day for group in groups for day in group_days(group)})
    return {
        "id": key,
        "parts": parts,
        "current": current,
        "state": state,
        "groups": groups,
        "already": list(already.values()),
        "days": days,
        "total_bytes": sum(sizes.values()),
        "done_bytes": done,
        "done_known": known,
        "received": received_all,
        "labels": client.get("labels") or {},
        "requested": (series or {}).get("requested") or first["plan"]["window"],
        "excluded": (series or {}).get("excluded") or [],
        "series": series,
        "started_at": started_at,
        "elapsed_seconds": elapsed,
        "segments": sum(len(part["started"]) for part in parts),
        "settled": all(part["settled"] for part in parts),
        "source_version": first["plan"]["source"]["version"],
        "destination_version": first["plan"]["destination"]["version"],
        "active_skipped": bool(first["plan"]["active_skipped"]),
        "acknowledged": any(run.get("active_ack") for part in parts for run in part["started"]),
    }


def remaining_groups(copy: dict) -> list[dict]:
    return [group for group in copy["groups"] if group["id"] not in copy["received"]]


def current_run(copy: dict) -> dict | None:
    """The run whose task the destination is, or was last, working on."""
    for part in reversed(copy["parts"]):
        if part["last"] is not None and part["state"] in {"running", "unsure"}:
            return part["last"]
    if copy["current"] is not None and copy["parts"][copy["current"]]["last"] is not None:
        return copy["parts"][copy["current"]]["last"]
    for part in reversed(copy["parts"]):
        if part["last"] is not None:
            return part["last"]
    return None


def next_part(copy: dict) -> dict | None:
    """The first part of a multi-part copy that has not been started."""
    return next((part for part in copy["parts"] if part["state"] == "planned"), None)


# Checking -------------------------------------------------------------------------------


def _latest_check(part: dict) -> dict | None:
    """The count taken on the part's newest run. A count taken before the copy was
    continued no longer speaks for it, so it is not used."""
    if not part["started"]:
        return None
    result = part["started"][-1]["verification"]
    return result if result.get("outcome") != "verification_pending" else None


def check(copy: dict) -> dict:
    """What the day-by-day counts say about a copy, across all its parts.

    `phase` is `unchecked`, `incomplete` (counting could not finish), `ok`, `diff`, or
    `partial` (the copy is unfinished and what arrived matches). `issues` lists only the
    days that differ: `missing` (a source server's logs for those days never arrived) and
    `short`/`extra` (a day's count is lower or higher on the destination).
    """
    unfinished = copy["state"] in {"stopped", "failed", "between", "running", "unsure"}
    days_all = set(copy["days"])
    issues: list[dict] = []
    missing_days: set[date] = set()
    source: dict[date, int] = {}
    arrived: dict[date, int] = {}
    judged: set[date] = set()
    unjudged: set[date] = set()
    reasons: list[dict] = []
    unsettled: list[str] = []  # Parts the engine did not call verified, for whatever reason.
    checked_any = False
    checked_at = ""
    for part in copy["parts"]:
        if not part["started"]:
            continue
        result = _latest_check(part)
        if result is None:
            continue
        checked_any = True
        checked_at = max(checked_at, result.get("at", ""))
        selected = {g["node"] + "/" + g["id"]: g for g in part["root"]["plan"]["selected"]}
        part_days = {day for group in selected.values() for day in group_days(group)}
        not_received = [
            key for key in result.get("buckets", {}).get("not_received", []) if key in selected
        ]
        by_bucket = {}
        for run in part["started"]:
            by_bucket.update(run.get("source_by_bucket") or {})
        for key in not_received:
            group = selected[key]
            span = group_days(group)
            missing_days.update(span)
            for day, count in (by_bucket.get(key) or {}).get("daily", []):
                parsed = date.fromisoformat(day)
                source[parsed] = source.get(parsed, 0) + count
        by_node: dict[str, set[date]] = {}
        for key in not_received:
            by_node.setdefault(selected[key]["node"], set()).update(group_days(selected[key]))
        for node, node_days in sorted(by_node.items()):
            for first, last in day_ranges(node_days):
                issues.append({"kind": "missing", "first": first, "last": last, "node": node})
        # What the destination's own list of stored logs says, where it could be read:
        # a piece whose count differs, one that is incomplete, one listed two ways.
        every = {
            g["node"] + "/" + g["id"]: g
            for g in (
                *part["root"]["plan"]["selected"],
                *part["root"]["plan"].get("already_received", []),
            )
        }
        by_id = {group["id"]: group for group in every.values()}
        exact = result.get("bucket_check") or {}
        flagged = set(not_received)
        for kind, keys in (
            ("broken", result.get("incomplete_destination_copies", [])),
            ("conflict", result.get("conflicting_destination_copies", [])),
            ("differs", [key for key in exact.get("unmatched", []) if key in every]),
        ):
            for key in keys:
                group = every.get(key) or by_id.get(_id(key))
                if group is None or key in flagged or group["id"] in flagged:
                    continue
                flagged.update({key, group["id"]})
                span = group_days(group)
                if span:
                    issues.append(
                        {
                            "kind": kind,
                            "first": span[0],
                            "last": span[-1],
                            "node": key.rsplit("/", 1)[0] if kind != "differs" else group["node"],
                        }
                    )
        if exact.get("unknown") and result.get("outcome") != "verified_success":
            reasons.append({"reason": "counts_unknown", "detail": ""})
        if result.get("outcome") != "verified_success":
            unsettled.append(result.get("reason") or result.get("outcome") or "")
        if "daily" not in result:
            reasons.append(
                {"reason": result.get("reason") or "", "detail": result.get("detail") or ""}
            )
            continue
        for row in result["daily"]:
            day = date.fromisoformat(row["day"])
            if day not in part_days:
                continue
            source[day] = source.get(day, 0) + row["source"]
            arrived[day] = (
                arrived.get(day, 0) + row["actual"] - row["preexisting"] - row["new_ingest"]
            )
            if row.get("partial"):
                unjudged.add(day)
                continue
            judged.add(day)
            if row["difference"] and day not in missing_days:
                issues.append(
                    {
                        "kind": "short" if row["difference"] < 0 else "extra",
                        "first": day,
                        "last": day,
                        "difference": abs(row["difference"]),
                    }
                )
    for issue in issues:
        span = days_between(issue["first"], issue["last"])
        issue["source"] = sum(source.get(day, 0) for day in span)
        issue["arrived"] = sum(arrived.get(day, 0) for day in span)
    issues.sort(key=lambda issue: (issue["first"], issue["kind"]))
    differing = {
        day for issue in issues for day in days_between(issue["first"], issue["last"])
    } & days_all
    remaining = {day for group in remaining_groups(copy) for day in group_days(group)}
    got = days_all - remaining if copy["settled"] else set(arrived)
    short = [issue for issue in issues if issue["kind"] != "missing"]
    if not checked_any:
        phase = "unchecked"
    elif unfinished:
        phase = "diff" if short else "partial"
    elif reasons and not issues:
        phase = "incomplete"
    elif issues:
        phase = "diff"
    elif copy["state"] == "missing":
        phase = "diff"
    elif unsettled:
        # Nothing this screen can point at differs, yet the engine did not call the copy
        # verified. Never say "everything arrived" on its behalf.
        phase = "incomplete"
        reasons.append({"reason": unsettled[0], "detail": ""})
    else:
        phase = "ok"
    return {
        "phase": phase,
        "unfinished": unfinished,
        "issues": issues,
        "days": len(days_all),
        "differing_days": len(differing),
        "arrived_days": len(got & days_all),
        "source": source,
        "arrived": arrived,
        "source_total": sum(source.get(day, 0) for day in days_all),
        "arrived_total": sum(arrived.get(day, 0) for day in days_all),
        "judged_days": len(judged & days_all),
        "unjudged_days": sorted(unjudged & days_all),
        "reasons": reasons,
        "checked_at": checked_at,
        "can_copy_missing": any(issue["kind"] == "missing" for issue in issues) and not unfinished,
    }
