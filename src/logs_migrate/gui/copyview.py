"""Plain words for the plan, a refusal, a running copy, its result and past runs. No Qt."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape

from ..errors import Code
from . import copies
from . import viewmodel as vm
from .copies import day_of, day_ranges, days_between, group_days

# Plan -------------------------------------------------------------------------------------


def line(icon: str, kind: str, key: str, text: str, small: str = "") -> dict:
    return {"icon": icon, "kind": kind, "key": key, "text": text, "small": small}


def excluded_days(excluded: list[dict]) -> list[date]:
    return sorted(
        {
            day
            for item in excluded
            for day in days_between(
                date.fromisoformat(item["first"]), date.fromisoformat(item["last"])
            )
        }
    )


def room(copy: dict) -> dict:
    """Free space against this copy's need, per destination server, across every part."""
    need: dict[str, int] = {}
    free: dict[str, int | None] = {}
    for part in copy["parts"]:
        if part["state"] == "done":
            continue
        for node in part["root"]["plan"]["capacity"]["nodes"]:
            need[node["id"]] = need.get(node["id"], 0) + (node.get("estimated_bytes") or 0)
            known = [
                value
                for value in (node.get("available_bytes"), node.get("disk_headroom_bytes"))
                if value is not None
            ]
            free.setdefault(node["id"], min(known) if len(known) == 2 else None)
            if node.get("fits") is False and free[node["id"]] is None and known:
                free[node["id"]] = min(known)
    nodes = [
        {
            "id": name,
            "free": free[name],
            "need": need[name],
            "short": max(0, need[name] - free[name]) if free[name] is not None else None,
        }
        for name in need
    ]
    return {
        "nodes": nodes,
        "known": bool(nodes) and all(node["free"] is not None for node in nodes),
        "fits": all(not node["short"] for node in nodes),
    }


def estimate(copy: dict) -> tuple[float | None, float | None]:
    low = high = 0.0
    for part in copy["parts"]:
        if part["state"] == "done":
            continue
        figure = part["root"]["plan"].get("estimate")
        if not figure:
            return None, None
        low += figure["low_seconds"]
        high += figure["high_seconds"]
    return low, high


def plan_notes(copy: dict) -> list[dict]:
    """The engine's cautions that change what a person should expect, in plain words."""
    notes = []
    undated = hosts = duplicates = 0
    for part in copy["parts"]:
        plan = part["root"]["plan"]
        undated = max(undated, len(plan.get("undated_excluded", [])))
        for warning in plan.get("warnings", []):
            if "the bucket listing returned text but no recognizable groups" in warning:
                hosts += 1
            if warning.startswith("Multiple destination copies:"):
                duplicates += 1
    if hosts:
        notes.append(
            line(
                "!",
                "warn",
                "Check",
                "<b>The app could not read the stored logs on "
                f"{vm.plural(hosts, 'source server')}.</b>",
                "Their logs may be missing from this plan. Save a troubleshooting file and ask "
                "whoever supports the source before relying on it.",
            )
        )
    if undated:
        notes.append(
            line(
                "!",
                "warn",
                "Not included",
                f"<b>{vm.plural(undated, 'stored piece')} on the source "
                f"{'carries' if undated == 1 else 'carry'} no dates.</b>",
                "The app cannot tell whether they belong to these dates, so they are left out.",
            )
        )
    if duplicates:
        notes.append(
            line(
                "!",
                "warn",
                "Note",
                "The destination holds more than one copy of some days in these dates.",
                "Nothing is copied over them. The saved detailed list names them.",
            )
        )
    return notes


def plan_view(
    copy: dict,
    requested: tuple[date, date],
    extent: dict | None,
    *,
    server_sign_in: bool,
) -> dict:
    """The plan as the six short lines, or the reason there is nothing to start."""
    first_plan = copy["parts"][0]["root"]["plan"]
    already_days = sorted({day for group in copy["already"] for day in group_days(group)})
    skipped = excluded_days(copy["excluded"])
    newest = extent["newest"] if extent else None
    base = {
        "copy": copy,
        "skipped": skipped,
        "already_ranges": day_ranges(already_days),
        "newest": newest,
        "needs_ack": bool(copy["active_skipped"] or skipped),
    }
    if not copy["groups"]:
        return {**base, "phase": "already"}
    difference = first_plan["partition_diff"]
    if difference["missing"] or difference["different"]:
        return {**base, "phase": "rules"}
    if difference["missing_fields"]:
        return {**base, "phase": "fields", "fields": difference["missing_fields"]}
    space = room(copy)
    if space["known"] and not space["fits"]:
        return {**base, "phase": "space", "room": space}
    days = copy["days"]
    first, last = days[0], days[-1]
    start, end = requested
    to_copy = days
    extra_start = (start - first).days if first < start else 0
    extra_end = (last - end).days if last > end else 0
    small = ""
    if extra_start and not extra_end:
        small = (
            f"{vm.plural(extra_start, 'day')} more than you picked. The source stores "
            f"{vm.format_day(first, False)} to {vm.format_day(start, False)} together, so they "
            "are copied together."
        )
    elif extra_end and not extra_start:
        small = (
            f"{vm.plural(extra_end, 'day')} more than you picked. The source stores "
            f"{vm.format_day(end, False)} to {vm.format_day(last, False)} together, so they "
            "are copied together."
        )
    elif extra_start and extra_end:
        small = (
            f"{vm.plural(extra_start + extra_end, 'day')} more than you picked. The source "
            "stores several days together, so whole stored pieces are copied."
        )
    lines = [
        line(
            "✓",
            "ok",
            "Copies",
            vm.b(vm.format_range(first, last))
            + (f", except {vm.plural(len(skipped), 'day')}" if skipped else "")
            + f" ({vm.plural(len(to_copy), 'day')}{' to copy' if already_days else ''})",
            small,
        )
    ]
    only_there = [day for day in already_days if day not in set(days)]
    if already_days:
        count = len(only_there) or len(already_days)
        lines.append(
            line(
                "✓",
                "ok",
                "Already there",
                f"<b>{vm.plural(count, 'day')}</b> arrived in earlier runs and "
                f"{'is' if count == 1 else 'are'} skipped.",
                " · ".join(vm.format_range(a, z) for a, z in day_ranges(already_days)),
            )
        )
    messages = [group.get("messages") for group in copy["groups"]]
    entries = None if any(m is None for m in messages) else sum(messages)
    lines.append(
        line(
            "✓",
            "ok",
            "Size",
            vm.b(vm.format_bytes(copy["total_bytes"]))
            + (f" · about {vm.format_big(entries)} log entries" if entries is not None else ""),
        )
    )
    low, high = estimate(copy)
    lines.append(
        line(
            "✓",
            "ok",
            "Time",
            vm.b(vm.format_estimate(low, high)),
            "An estimate. Once the copy runs you see the real pace.",
        )
    )
    if space["known"] and server_sign_in:
        count = len(space["nodes"])
        lines.append(
            line(
                "✓",
                "ok",
                "Room",
                "Enough free space on "
                + (f"all {count} destination servers" if count > 1 else "the destination server"),
                '<a href="space">Show space per server</a>',
            )
        )
    else:
        lines.append(
            line(
                "!",
                "warn",
                "Room",
                "<b>Not checked.</b> "
                + (
                    "No destination server sign-in was given, so the app cannot see free space."
                    if not server_sign_in
                    else "The destination did not report its free space."
                ),
                "The destination stops a copy by itself before a disk fills."
                + (
                    " <b>Also: if this copy stops part-way, it cannot be continued from this "
                    'app.</b> To avoid both, <a href="add-sign-in">add the sign-in on '
                    "Connect</a> first."
                    if not server_sign_in
                    else ""
                ),
            )
        )
    if skipped:
        lines.append(
            line(
                "!",
                "warn",
                "Skipped",
                f"<b>{vm.format_ranges(day_ranges(skipped))}.</b> The destination holds broken "
                f"copies of {'this day' if len(skipped) == 1 else f'these {len(skipped)} days'}.",
                f"The copy runs as {len(copy['parts'])} parts back to back around them. Keep "
                "this window open until the last part has started."
                if len(copy["parts"]) > 1
                else "",
            )
        )
    if copy["active_skipped"]:
        onward = (
            f"Logs from {vm.format_day(newest)} onward."
            if newest and newest > last
            else f"The newest logs, from part-way through {vm.format_day(newest)}."
            if newest
            else "The newest logs."
        )
        lines.append(
            line(
                "!",
                "warn",
                "Left out",
                f"<b>{onward}</b> The source is still writing them, so they cannot be copied yet.",
                "It closes them every few days. Copy them in a later run.",
            )
        )
    risk = [
        item for part in copy["parts"] for item in part["root"]["plan"].get("retention_risk", [])
    ]
    if risk:
        risky = sorted({day for item in risk for day in group_days(item)})
        lines.append(
            line(
                "!",
                "warn",
                "Start soon",
                f"<b>{vm.format_range(risky[0], risky[-1])} "
                f"{'is' if len(risky) == 1 else 'are'} about to be deleted by the source.</b>",
                f"The source is set to delete logs after {risk[0]['retention_days']} days, and "
                "these days reach that age this week. If they are gone when you start, the app "
                "tells you and asks you to check the dates again.",
            )
        )
    if first_plan["destination"]["version"].startswith("9.1"):
        lines.append(
            line(
                "!",
                "warn",
                "Checking",
                "<b>The result cannot be counted on a version 9.1 destination.</b>",
                "The copy can run, but afterwards this app cannot confirm that everything "
                "arrived. Compare a few days by searching on both systems.",
            )
        )
    lines += plan_notes(copy)
    ack = None
    if base["needs_ack"]:
        newest_text = f"the newest logs ({vm.format_day(newest)} onward)" if newest else ""
        skipped_text = (
            f"the {vm.plural(len(skipped), 'skipped day')}".replace(
                " 1 skipped day", " skipped day"
            )
            if skipped
            else ""
        )
        if not copy["active_skipped"]:
            newest_text = ""
        elif not newest_text:
            newest_text = "the newest logs"
        ack = (
            "I understand "
            + " and ".join(text for text in (skipped_text, newest_text) if text)
            + " will not be copied this time."
        )
    return {
        **base,
        "phase": "ready",
        "lines": lines,
        "room": space,
        "ack": ack,
        "extra_start": extra_start,
        "first": first,
        "last": last,
    }


def months_view(view: dict) -> list[tuple[str, str, str, str]]:
    """Month by month: days in the dates, size to copy, and whether it is copied."""
    copy = view["copy"]
    to_copy = set(copy["days"])
    there = {day for group in copy["already"] for day in group_days(group)} - to_copy
    skipped = set(view["skipped"])
    span = sorted(to_copy | there | skipped)
    if not span:
        return []
    per_day: dict[date, float] = {}
    for group in copy["groups"]:
        days = group_days(group)
        for day in days:
            per_day[day] = per_day.get(day, 0) + group["size"] / len(days)
    months: dict[tuple[int, int], dict] = {}
    for day in days_between(span[0], span[-1]):
        slot = months.setdefault(
            (day.year, day.month), {"n": 0, "copy": 0, "there": 0, "skipped": 0, "size": 0.0}
        )
        slot["n"] += 1
        if day in skipped:
            slot["skipped"] += 1
        elif day in to_copy:
            slot["copy"] += 1
            slot["size"] += per_day.get(day, 0)
        elif day in there:
            slot["there"] += 1
    rows = []
    for (year, month), slot in months.items():
        if slot["there"] == slot["n"]:
            answer = "No. Already on the destination."
        elif slot["copy"] == slot["n"]:
            answer = "Yes, all of it"
        else:
            parts = []
            if slot["there"]:
                parts.append(f"{vm.plural(slot['there'], 'day')} already there")
            if slot["skipped"]:
                parts.append(f"{vm.plural(slot['skipped'], 'day')} skipped (broken copies)")
            none = slot["n"] - slot["copy"] - slot["there"] - slot["skipped"]
            if none:
                parts.append(f"{vm.plural(none, 'day')} with no logs")
            answer = ("Partly: " if slot["copy"] else "No: ") + ", ".join(parts)
        rows.append(
            (
                f"{vm.MONTHS[month - 1]} {year}",
                vm.plural(slot["n"], "day"),
                vm.format_bytes(slot["size"]) if slot["copy"] else "none",
                answer,
            )
        )
    if view["newest"] and copy["active_skipped"]:
        rows.append(
            (f"{vm.format_day(view['newest'])} onward", "", "not known", "No. Still being written.")
        )
    return rows


def space_rows(space: dict) -> list[tuple[str, str, str, str]]:
    return [
        (
            node["id"],
            vm.format_bytes(node["free"]),
            vm.format_bytes(node["need"]),
            vm.format_bytes(node["free"] - node["need"])
            if node["free"] is not None and node["free"] >= node["need"]
            else "Not enough"
            if node["free"] is not None
            else "not known",
        )
        for node in space["nodes"]
    ]


def plan_detail_csv(view: dict) -> str:
    """Every stored piece in the plan with its source server, for the saved detail file."""
    rows = ["status,source_server,first_day,last_day,size_bytes,log_entries,identifier"]
    for status, groups in (
        ("copy", view["copy"]["groups"]),
        ("already_there", view["copy"]["already"]),
    ):
        for group in sorted(groups, key=lambda g: (g["start"], g["node"])):
            rows.append(
                ",".join(
                    str(value)
                    for value in (
                        status,
                        group["node"],
                        day_of(group["start"]).isoformat(),
                        day_of(group["end"]).isoformat(),
                        group["size"],
                        group["messages"] if group["messages"] is not None else "",
                        group["id"],
                    )
                )
            )
    return "\n".join(rows) + "\n"


# Refusals ---------------------------------------------------------------------------------

BROKEN_REASON = {
    "incomplete": "Only part of it arrived",
    "differs": "Size differs from the source",
}


def refused_days(refusal: dict) -> list[date]:
    return sorted({day for group in refusal.get("groups", []) for day in group_days(group)})


def free_ranges(requested: tuple[date, date], taken: list[date]) -> list[tuple[date, date]]:
    """The requested days that touch nothing refused, as unbroken ranges."""
    blocked = set(taken)
    return day_ranges(day for day in days_between(*requested) if day not in blocked)


def exclusions(refusal: dict) -> list[dict]:
    """The refused days as ranges a plan can leave out."""
    return [
        {"first": first.isoformat(), "last": last.isoformat()}
        for first, last in day_ranges(refused_days(refusal))
    ]


def refusal_view(
    code: str | None,
    refusal: dict | None,
    requested: tuple[date, date],
    extent: dict | None,
    *,
    zone: str,
    saved: str | None = None,
    server_sign_in: bool = True,
) -> dict:
    """Why these dates cannot be copied as picked: a heading, the days affected and the fix."""
    kind = (refusal or {}).get("kind")
    days = refused_days(refusal or {})
    count = len(days)
    free = free_ranges(requested, days) if days else []
    those = "this day" if count == 1 else f"these {count} days"
    without = [(f"Plan without {those}", "exclude", "", "primary")] if free and count else []
    nothing = "Nothing was copied or changed."
    view = {
        "tone": "bad",
        "card": "Can’t start yet",
        "table": None,
        "more": "",
        "days": days,
        "broken": kind in {"broken", "vanished", "servers_changed"},
        "running": kind == "running",
    }
    if kind == "running":
        task = refusal.get("task") or {}
        done = ""
        if task.get("bytes_transferred") is not None and task.get("total_bytes"):
            done = f" ({int(100 * task['bytes_transferred'] / task['total_bytes'])}% done)"
        elif task.get("progress") is not None:
            done = f" ({int(task['progress'])}% done)"
        started = (
            f"that started {vm.format_clock(task.get('started_at'), zone)}"
            if task.get("started_at")
            else "that was started outside this app"
        )
        covered = vm.format_ranges(day_ranges(days))
        actions = []
        if refusal.get("run"):
            actions.append(("Watch the running copy", "watch", refusal["run"], ""))
        for index, (first, last) in enumerate(sorted(free, key=lambda r: r[0] - r[1])[:2]):
            actions.append(
                (
                    f"Copy only {vm.format_range(first, last)}",
                    "use-dates",
                    f"{first.isoformat()}|{last.isoformat()}",
                    "primary" if index == 0 else "",
                )
            )
        if actions and not any(a[3] == "primary" for a in actions):
            actions[0] = (*actions[0][:3], "primary")
        return {
            **view,
            "tone": "warn",
            "title": "Part of these dates is already being copied",
            "text": f"A copy {started} is still running and covers <b>{covered}</b>. Starting "
            "those dates again would copy them twice, so the app will not.",
            "table": (
                ["Dates", "Status"],
                [(covered, f"In the copy running now{done}")]
                + [(vm.format_range(a, z), "Free to copy") for a, z in free],
            ),
            "hint": nothing
            + (
                " Watch the copy that is running, or copy only the dates that are free."
                if free
                else " Watch the copy that is running."
            ),
            "actions": actions,
        }
    if kind == "broken":
        grouped: dict[tuple[str, str], set[date]] = {}
        for group in refusal["groups"]:
            key = (group.get("destination_node") or "not known", group.get("reason") or "differs")
            grouped.setdefault(key, set()).update(group_days(group))
        rows = sorted(
            (first, vm.format_range(first, last), node, BROKEN_REASON.get(reason, reason))
            for (node, reason), node_days in grouped.items()
            for first, last in day_ranges(node_days)
        )
        saved_text = (
            f"<b>✓ Saved “{escape(saved.rsplit('/', 1)[-1])}” in "
            f"{escape(saved.rsplit('/', 1)[0])}.</b> "
            '<a href="open-folder">Open the folder</a>'
            if saved
            else '<a href="save-details">Save the details file</a>'
        )
        return {
            **view,
            "title": f"{vm.plural(count, 'day')} cannot be copied: the destination already "
            f"holds a broken copy of {'it' if count == 1 else 'them'}",
            "text": f"An earlier copy left {those} on the destination incomplete or different "
            "from the source. This app never deletes or overwrites anything on the "
            "destination, so it will not copy over them. "
            + (
                "<b>Everything else in your dates is fine and can be copied now.</b> "
                f"{those.capitalize()} {'is' if count == 1 else 'are'} then skipped whole, so "
                "nothing is left half-copied."
                if free
                else "Every day in your dates is affected, so there is nothing to copy now."
            ),
            "table": (
                ["Days", "Destination server", "What is wrong"],
                [row[1:] for row in rows],
            ),
            "more": f"To copy {those} later, the broken copies must first be removed from the "
            "destination’s storage. This app cannot do that. The people who support the "
            "destination system itself (its vendor’s support, not this tool) can; give them "
            f"the details file, which lists each one. {saved_text}",
            "hint": nothing
            + (
                " Make a plan without these days, or change the dates on the left."
                if free
                else " Change the dates on the left."
            ),
            "actions": without,
        }
    if kind == "held":
        until = vm.format_time(refusal.get("held_until"), zone)
        return {
            **view,
            "tone": "warn",
            "title": f"These dates are locked until {until}",
            "text": "An earlier start of these dates got no answer from the destination. To "
            f"avoid copying them twice, they stay locked for 15 minutes, until <b>{until}</b>. "
            "If no copy has appeared by then, check the dates again.",
            "table": (["Dates", "Status"], [(vm.format_ranges(day_ranges(days)), "Locked")]),
            "hint": nothing + " Please do not start another copy of these dates.",
            "actions": [("Check again", "check", "", "primary")],
        }
    if kind == "vanished":
        return {
            **view,
            "title": f"An earlier run recorded {those} as arrived, but the destination no "
            "longer shows them",
            "text": "The app will not copy over something it cannot account for. Ask whoever "
            "runs the destination what happened to these days. Everything else in your "
            "dates can be copied now.",
            "table": (["Days"], [(vm.format_range(a, z),) for a, z in day_ranges(days)]),
            "hint": nothing,
            "actions": without,
        }
    if kind == "unseen":
        return {
            **view,
            "tone": "warn",
            "title": "The app cannot see whether these days are already on the destination",
            "text": "An earlier copy reported them, and without the destination’s server "
            "sign-in the app cannot look. Add that sign-in on Connect, then check the dates "
            "again.",
            "table": (["Days"], [(vm.format_range(a, z),) for a, z in day_ranges(days)]),
            "hint": nothing,
            "actions": [("Add the destination server sign-in", "add-sign-in", "", "primary")]
            if not server_sign_in
            else [("Check again", "check", "", "primary")],
        }
    if kind == "unsettled":
        return {
            **view,
            "tone": "warn",
            "title": "An earlier copy of these dates has not been accounted for yet",
            "text": "The app has not yet seen how that copy ended. Open it, let the app read "
            "its result from the destination, then check the dates again.",
            "table": (["Days"], [(vm.format_range(a, z),) for a, z in day_ranges(days)]),
            "hint": nothing,
            "actions": [("Go to that copy", "watch", refusal.get("run") or "", "primary")],
        }
    if kind == "servers_changed":
        return {
            **view,
            "title": "The destination has a different number of servers than when part of "
            "these dates was copied",
            "text": "Copying those days again now could store them twice. Put the destination "
            "back to the number of servers it had, or leave these days out.",
            "table": (["Days"], [(vm.format_range(a, z),) for a, z in day_ranges(days)]),
            "hint": nothing,
            "actions": without,
        }
    if kind == "empty":
        action = []
        text = "Pick other dates."
        if extent:
            text = (
                f"Its logs run from <b>{vm.format_day(extent['first'])}</b> to "
                f"<b>{vm.format_day(extent['last'])}</b>. Pick dates inside that range."
            )
            action = [
                (
                    f"Use {vm.format_range(extent['first'], extent['last'])}",
                    "preset",
                    "Everything",
                    "primary",
                )
            ]
        return {
            **view,
            "tone": "info",
            "card": "Nothing to do",
            "title": "The source has no finished logs in these dates",
            "text": text,
            "hint": nothing,
            "actions": action,
        }
    if kind == "duplicates":
        what, todo = (
            "The source lists some of its stored logs twice, so the app cannot tell them apart.",
            "Ask whoever supports the source system to look at its stored logs. A "
            "troubleshooting file from Settings names them.",
        )
    else:
        what, todo = vm.plain_error(code)
    return {
        **view,
        "title": "These dates could not be checked",
        "text": f"<b>What happened:</b> {escape(what)}<br><b>What to do:</b> {escape(todo)}",
        "hint": nothing,
        "actions": [("Check again", "check", "", "primary")],
    }


def refusal_details(refusal: dict, labels: dict) -> str:
    """The saved details file: every affected stored piece with its identifier and servers."""
    lines = [
        "VCF Logs Migrator: days that could not be copied",
        f"Source: {labels.get('source', 'not recorded')}",
        f"Destination: {labels.get('destination', 'not recorded')}",
        "",
        "Each line is one stored piece of logs on the destination that is incomplete or "
        "differs from the source. The identifier is the name the log system itself uses for it.",
        "",
    ]
    for group in sorted(refusal.get("groups", []), key=lambda g: (g["start"] or 0, g["id"])):
        days = group_days(group)
        lines.append(
            f"{vm.format_range(days[0], days[-1]) if days else 'dates not known'}"
            f" | destination server: {group.get('destination_node') or 'not known'}"
            f" | source server: {group.get('source_node') or 'not known'}"
            f" | {BROKEN_REASON.get(group.get('reason', ''), group.get('reason') or 'affected')}"
            f" | identifier: {group['id']}"
        )
    return "\n".join(lines) + "\n"


# Copy logs --------------------------------------------------------------------------------

NODE_BADGE = {
    "running": ("accent", "Copying"),
    "completed": ("ok", "Finished"),
    "failed": ("bad", "Failed"),
    "cancelled": ("", "Stopped"),
    "idle": ("", "Waiting"),
}
QUIET_MINUTES = 5


def format_finish(moment: datetime, now: datetime) -> str:
    """`6:40 PM today`, `1:05 AM tomorrow`, `Nov 14 at 3:00 PM`."""
    clock = vm._clock(moment)
    if moment.date() == now.date():
        return f"{clock} today"
    if moment.date() == now.date() + timedelta(days=1):
        return f"{clock} tomorrow"
    return f"{vm.format_day(moment.date(), False)} at {clock}"


def progress_view(
    copy: dict,
    status: dict | None,
    *,
    zone: str,
    now: str,
    lost_at: str | None = None,
    quiet_minutes: float = 0,
    latest: dict | None = None,
    continued: bool = False,
) -> dict:
    """The one progress card: percent, size, three facts, a row per destination server."""
    total = copy["total_bytes"] or 0
    running = bool(status) and status["status"] == "running"
    current = (status or {}).get("bytes_transferred") or 0
    done = min(total, copy["done_bytes"] + (current if running else 0)) if total else 0
    fraction = done / total if total else 0.0
    mode = "lost" if lost_at else "quiet" if quiet_minutes >= QUIET_MINUTES and running else "live"
    eta = (status or {}).get("eta_seconds") if running else None
    later = 0.0
    for part in copy["parts"]:
        if part["state"] == "planned" and part["root"]["plan"].get("estimate"):
            later += part["root"]["plan"]["estimate"]["low_seconds"] * 2
    left = eta + later if eta is not None else None
    elapsed = (status or {}).get("elapsed_seconds")
    local_now = vm._local(now, zone)
    if mode == "lost":
        facts = [
            (
                "Started",
                ("again " if continued else "")
                + vm.format_clock(
                    (status or {}).get("started_at") or copy["started_at"], zone, now
                ),
            ),
            ("Time left", "not known right now"),
            ("Should finish around", "not known right now"),
        ]
    else:
        facts = [
            ("Running again for" if continued else "Running for", vm.format_duration(elapsed)),
            (
                "Time left",
                "about " + vm.format_duration(left) if left is not None else "working it out…",
            ),
            (
                "Should finish around",
                format_finish(local_now + timedelta(seconds=left), local_now)
                if left is not None and local_now
                else "known after a few minutes",
            ),
        ]
    servers = []
    for node in (status or {}).get("nodes", []):
        node_done, node_total = node.get("bytes_transferred"), node.get("total_bytes")
        if node_total:
            part = min(1.0, (node_done or 0) / node_total)
            text = f"{vm.format_bytes(node_done or 0)} of {vm.format_bytes(node_total)}"
        elif node.get("progress") is not None:
            part = node["progress"] / 100
            text = f"{int(node['progress'])}%"
        elif node.get("buckets_total"):
            part = node["buckets_done"] / node["buckets_total"]
            text = vm.format_bytes(node_done) if node_done else f"{int(100 * part)}%"
        else:
            part = None
            text = vm.format_bytes(node_done) if node_done is not None else "not reported"
        if mode == "lost":
            badge = ("", "Not known")
        elif mode == "quiet" and node["status"] == "running":
            badge = ("warn", f"Nothing new for {int(quiet_minutes)} min")
        else:
            badge = NODE_BADGE.get(node["status"], ("", "Not known"))
        servers.append({"name": node["node"], "fraction": part, "text": text, "badge": badge})
    if latest:
        where = f", on {latest['node']}" if latest.get("node") else ""
        day = vm.format_day(latest["day"], False)
        if mode == "live":
            arrived = f"Latest to arrive: logs up to {day}{where}."
        elif mode == "quiet":
            arrived = (
                f"Last to arrive: logs up to {day}, at {vm.format_time(latest.get('at'), zone)}."
            )
        else:
            arrived = f"Last seen: logs up to {day}."
    else:
        arrived = "" if mode == "lost" else "Waiting for the first logs to arrive."
    return {
        "mode": mode,
        "percent": int(fraction * 100),
        "fraction": fraction,
        "size": f"<b>{vm.format_bytes(done)}</b> of {vm.format_bytes(total)} copied",
        "facts": facts,
        "servers": servers,
        "latest": arrived,
        "lost_at": vm.format_time(lost_at, zone) if lost_at else "",
    }


def remainder_facts(
    copy: dict, *, server_sign_in: bool, failed: bool, blocked: bool = False
) -> list:
    """What arrived, what is left and whether there is room, for a copy that stopped."""
    total = copy["total_bytes"] or 1
    done = copy["done_bytes"]
    rest = max(0, total - done)
    percent = int(100 * done / total)
    rate = None
    for part in copy["parts"]:
        for run in part["started"]:
            rate = run.get("measured_rate_bytes_per_second") or rate
    took = f" · about {vm.format_duration(rest / rate)}" if rate else ""
    if blocked or not copy["settled"]:
        return [
            ("Arrived, as the destination last reported", f"{vm.format_bytes(done)} ({percent}%)"),
            ("Still to copy", f"about {vm.format_bytes(rest)}"),
            ("Room for the rest", "Not checked yet"),
        ]
    return [
        ("Arrived and safe on the destination", f"{vm.format_bytes(done)} ({percent}%)"),
        ("Still to copy", f"{vm.format_bytes(rest)}{took}"),
        (
            "Room for the rest",
            "Not checked" if not server_sign_in else "Checked again when you continue",
        ),
    ]


START_FAILURE = {
    "mover": (
        "the destination’s servers could not reach the source’s servers over the network, "
        "or did not accept the source server sign-in. This computer can reach both, but during "
        "a copy the destination fetches the files from the source directly, so its servers must "
        "be able to reach the source’s servers too.",
        "ask whoever runs your network to let the destination’s servers reach the source’s "
        "servers (secure shell, port 22), and check the source server password on Connect. "
        "Then press Start again. Your dates and plan are kept.",
    ),
    "source_sign_in": (
        "a source server did not accept the server sign-in when it was tried just before the copy.",
        "check the source server user and password on Connect, test again, then press Start "
        "again. Your dates and plan are kept.",
    ),
    "rejected": (
        "the destination refused the request to start. It gave no reason this app can show.",
        "press Start again. If it is refused again, save a troubleshooting file and send it "
        "to whoever supports the destination.",
    ),
    "cancelled": (
        "you cancelled before the copy was requested.",
        "press Start again when you are ready. Your dates and plan are kept.",
    ),
}


def start_failure(failure: dict) -> tuple[str, str, bool]:
    """(what happened, what to do, whether the plan itself must be remade)."""
    stage = failure.get("stage")
    if stage in START_FAILURE:
        return (*START_FAILURE[stage], False)
    code = vm.error_code(failure.get("code"))
    what, todo = vm.plain_error(failure.get("code"))
    replan = code in {
        Code.INVENTORY_CHANGED,
        Code.CAPACITY,
        Code.OVERLAP,
        Code.COLLISION,
        Code.TOPOLOGY,
        Code.PARTITIONS,
        Code.FIELDS,
        Code.UNCERTAIN_START,
        Code.INCOMPLETE,
    }
    return what[0].lower() + what[1:], todo[0].lower() + todo[1:], replan


def continue_failure(failure: dict, *, server_sign_in: bool) -> dict:
    """Why a stopped copy cannot be continued yet, and the fix."""
    code = vm.error_code(failure.get("code"))
    refusal = failure.get("refusal") or {}
    if not server_sign_in and code in {Code.INCOMPLETE, Code.OVERLAP, None}:
        return {
            "sign_in": True,
            "text": "<b>What happened:</b> to continue safely the app must see which logs "
            "already arrived. It can only look if it has the destination’s server sign-in, and "
            "none was given on Connect.<br><b>What to do:</b> add the destination’s server "
            "sign-in on Connect and test. Then use the button there to come back here and "
            "continue. Without it the same logs could be copied twice, so the app will not "
            "guess.<br><b>If you cannot get that sign-in:</b> this copy stays as it is. What "
            "arrived is kept and you can check it, but the rest cannot be sent from this app.",
        }
    if refusal.get("kind") == "broken":
        days = refused_days(refusal)
        return {
            "sign_in": False,
            "text": "<b>What happened:</b> the destination holds a broken copy of "
            f"<b>{vm.format_ranges(day_ranges(days))}</b>, left when the copy stopped. This app "
            "never deletes or overwrites anything on the destination, so it will not copy over "
            "it.<br><b>What to do:</b> the people who support the destination system can remove "
            "the broken copy. Until then, choose dates again to copy the days around it: the "
            "app offers a plan without these days.",
            "replan": True,
        }
    if code == Code.STATE:
        return {
            "sign_in": False,
            "text": "<b>What happened:</b> everything in this copy is already on the "
            "destination, so there is nothing left to send.<br><b>What to do:</b> check what "
            "arrived.",
            "nothing_left": True,
        }
    return {"sign_in": False, "text": vm.what_happened(failure.get("code"))}


# Check result -----------------------------------------------------------------------------

INCOMPLETE_REASON = {
    "counts_unknown": (
        "the destination could not say how many entries some of the stored logs hold, so "
        "those could not be compared.",
        "check again. If it keeps happening, save a troubleshooting file and ask whoever "
        "supports the destination.",
    ),
    "verified_discrepancy": (
        "the counts on the two systems differ in a way this screen cannot pin to a day.",
        "save the report, which holds the full figures, and keep the source until it has "
        "been looked into.",
    ),
    "selected_buckets_not_received": (
        "part of what was sent is not on the destination.",
        "go back to Copy logs and copy what is missing, then check again.",
    ),
    "destination_busy": (
        "a copy is still running on the destination, and counting waits until it has finished.",
        "wait for the copy to finish, then check again.",
    ),
    "unsupported_capability": (
        "this app cannot count the logs on a version 9.1 destination, so it cannot confirm "
        "what arrived.",
        "compare a few days by running the same search on both systems.",
    ),
    "no_fully_covered_days": (
        "every day in this copy is shared with logs the source was still writing, so no day "
        "could be judged.",
        "copy more dates once the source has closed those days, then check again.",
    ),
    "topology_changed": (
        "the destination is not the same system this copy went to.",
        "test the connections on Connect, then check again.",
    ),
    "connection_failed": (
        "one of the systems did not answer while its logs were being counted.",
        "check that both systems are up, then check again.",
    ),
    "authentication_failed": (
        "a sign-in was no longer accepted while counting.",
        "test the connections on Connect, then check again.",
    ),
}
UNSTABLE = (
    "the destination was counted twice and the two counts did not agree. That usually means it "
    "is still sorting the logs that just arrived. Nothing is known to be wrong.",
    "wait a few minutes and check again.",
)


def incomplete_reason(reasons: list[dict], error: str | None = None) -> tuple[str, str]:
    reason = reasons[0]["reason"] if reasons else (vm.error_code(error) or Code.INCOMPLETE).value
    if reason in INCOMPLETE_REASON:
        return INCOMPLETE_REASON[reason]
    if reason == Code.INCOMPLETE.value:
        return UNSTABLE
    what, todo = vm.plain_error(reason)
    return what[0].lower() + what[1:], todo[0].lower() + todo[1:]


def dates_text(copy: dict) -> str:
    if not copy["days"]:
        return "none"
    skipped = excluded_days(copy["excluded"])
    return vm.format_range(copy["days"][0], copy["days"][-1]) + (
        f", except {vm.plural(len(skipped), 'day')}" if skipped else ""
    )


def left_out(copy: dict) -> str:
    """What was deliberately not part of this copy, for the result and the report."""
    parts = []
    client = copy["parts"][0]["root"].get("client") or {}
    if copy["active_skipped"]:
        newest = client.get("newest")
        onward = (
            f"Logs from {vm.format_day(date.fromisoformat(newest))} onward"
            if newest
            else "The newest logs"
        )
        parts.append(
            f"{onward} were left out, "
            + ("as you agreed, " if copy["acknowledged"] else "")
            + "because the source was still writing them. Copy them in a later run."
        )
    skipped = excluded_days(copy["excluded"])
    if skipped:
        parts.append(
            f"{vm.format_ranges(day_ranges(skipped))} "
            f"{'was' if len(skipped) == 1 else 'were'} skipped, because the destination holds "
            f"broken copies of {'that day' if len(skipped) == 1 else 'those days'}."
            + (
                f" The copy ran as {len(copy['parts'])} parts back to back around them."
                if len(copy["parts"]) > 1
                else ""
            )
        )
    return " ".join(parts)


def issue_rows(result: dict, *, past: bool) -> list[tuple]:
    rows = []
    for issue in result["issues"]:
        days = vm.format_range(issue["first"], issue["last"])
        if issue["kind"] == "missing":
            rows.append(
                (
                    days,
                    vm.format_count(issue["source"]),
                    vm.format_count(issue["arrived"]),
                    ("bad", "Part never arrived"),
                    f"The logs that source server {issue['node']} holds for "
                    f"{'this day' if issue['first'] == issue['last'] else 'these days'} are not "
                    f"on the destination. They can be copied{'' if past else ' now'}: the "
                    "button below sends only what is missing.",
                )
            )
        elif issue["kind"] in {"broken", "conflict", "differs"}:
            where = {
                "broken": f"The destination server {issue['node']} holds only part of the logs "
                "for these days.",
                "conflict": f"The destination server {issue['node']} lists the logs for these "
                "days in two different ways.",
                "differs": f"The destination’s copy of the logs that source server "
                f"{issue['node']} holds for these days has a different number of entries.",
            }[issue["kind"]]
            rows.append(
                (
                    days,
                    vm.format_count(issue["source"]),
                    vm.format_count(issue["arrived"]),
                    (
                        "bad",
                        {
                            "broken": "Arrived incomplete",
                            "conflict": "Listed two ways",
                            "differs": "Count differs",
                        }[issue["kind"]],
                    ),
                    where + " This app never deletes or overwrites anything on the destination, "
                    "so it cannot repair this. Keep the source, save the report, and ask "
                    "whoever supports the destination.",
                )
            )
        elif issue["kind"] == "short":
            rows.append(
                (
                    days,
                    vm.format_count(issue["source"]),
                    vm.format_count(issue["arrived"]),
                    ("warn", f"{issue['difference']:,} fewer"),
                    f"{issue['difference']:,} of {vm.format_big(issue['source'])} entries for "
                    "this day are not on the destination. The app cannot add entries to a day "
                    "that has arrived.",
                )
            )
        else:
            rows.append(
                (
                    days,
                    vm.format_count(issue["source"]),
                    vm.format_count(issue["arrived"]),
                    ("warn", f"{issue['difference']:,} more"),
                    f"The destination holds {issue['difference']:,} more entries for this day "
                    "than the source. Something else also wrote logs for this day to the "
                    "destination.",
                )
            )
    return rows


def all_days_rows(copy: dict, result: dict) -> list[tuple[str, str, str, str]]:
    """Every day of the copy; runs of matching days are folded into one row."""
    labels: dict[date, str] = {}
    for issue in result["issues"]:
        label = {
            "missing": "Part never arrived",
            "broken": "Arrived incomplete",
            "conflict": "Listed two ways",
            "differs": "Count differs",
        }.get(issue["kind"]) or (
            f"{issue['difference']:,} {'fewer' if issue['kind'] == 'short' else 'more'}"
        )
        for day in days_between(issue["first"], issue["last"]):
            labels[day] = label
    unjudged = set(result["unjudged_days"])
    counted = set(result["source"]) | set(result["arrived"])
    rows = []
    span: list[date] = []
    tag = None

    def flush():
        if span:
            rows.append(
                (
                    vm.format_range(span[0], span[-1]),
                    vm.format_count(sum(result["source"].get(day, 0) for day in span)),
                    vm.format_count(sum(result["arrived"].get(day, 0) for day in span)),
                    tag,
                )
            )
            span.clear()

    for day in copy["days"]:
        this = labels.get(
            day,
            "Not judged: still being written"
            if day in unjudged
            else "Match"
            if day in counted
            else "Not copied yet"
            if result["unfinished"]
            else "No logs counted",
        )
        if this != tag or (span and day != span[-1] + timedelta(days=1)) or day in labels:
            flush()
        tag = this
        span.append(day)
        if day in labels and labels.get(day + timedelta(days=1)) != this:
            flush()
    flush()
    return rows


def check_view(
    copy: dict,
    result: dict,
    *,
    past: bool = False,
    saved: list[str] | None = None,
    error: str | None = None,
) -> dict:
    """One verdict in large type, then only the days that differ."""
    total_days = result["days"]
    phase = (
        "incomplete"
        if error and result["phase"] in {"unchecked", "incomplete"}
        else result["phase"]
    )
    unfinished = result["unfinished"]
    labels = copy["labels"]
    facts = [
        ("Dates in this copy" if unfinished else "Dates copied", dates_text(copy)),
        (
            "From → to",
            f"{labels.get('source', 'source')} → {labels.get('destination', 'destination')}",
        ),
        (
            "Copy ran for" if unfinished else "Copy took",
            vm.format_duration(copy["elapsed_seconds"])
            + (f" (in {copy['segments']} parts)" if copy["segments"] > 1 else ""),
        ),
    ]
    save = ("Save report again" if saved else "Save report", "save", "")
    more = ("Copy more dates", "more-dates", "")
    view = {
        "phase": phase,
        "facts": facts,
        "table": None,
        "note": "",
        "left_out": "",
        "saved": saved or [],
        "all_days": False,
    }
    percent = int(100 * copy["done_bytes"] / copy["total_bytes"]) if copy["total_bytes"] else 0

    def finish_actions():
        if past:
            return [save]
        return [save, (*more, "primary")] if saved else [more, (*save[:3], "primary")]

    if phase == "ok":
        unjudged = len(result["unjudged_days"])
        view.update(
            verdict=(
                "ok",
                "✓",
                "Everything arrived",
                (
                    f"All {total_days:,} days match."
                    if total_days != 1
                    else "The one day in this copy matches."
                )
                + " The source and the destination both hold "
                f"<b>{vm.format_count(result['source_total'])}</b> log entries for these "
                "dates."
                + (
                    f" {vm.plural(unjudged, 'of those days is', 'of those days are')} shared "
                    "with logs the source was still writing, so "
                    f"{'its count is' if unjudged == 1 else 'their counts are'} shown but not "
                    "judged."
                    if unjudged
                    else ""
                ),
            ),
            left_out=left_out(copy),
            all_days=True,
            hint="This is the result as it was recorded."
            if past
            else "This run is complete. It stays listed under Past runs."
            if saved
            else "This run is complete. Save the report if you want a record outside the app.",
            actions=finish_actions(),
        )
    elif phase == "diff":
        missing = [i for i in result["issues"] if i["kind"] == "missing"]
        short = [i for i in result["issues"] if i["kind"] == "short"]
        grave = [i for i in result["issues"] if i["kind"] not in {"short", "extra"}]
        differing = result["differing_days"]
        rest = max(0, total_days - differing)
        text = f"The other {vm.plural(rest, 'day')} match exactly."
        if unfinished:
            not_yet = total_days - result["arrived_days"]
            text = (
                f"This copy is not finished: <b>{vm.plural(not_yet, 'day')} "
                f"{'has' if not_yet == 1 else 'have'} not been copied yet.</b> Of the days that "
                "arrived, the ones below differ."
            )
        elif not result["issues"]:
            text = "Part of this copy did not arrive, and the days could not be counted."
        view.update(
            verdict=(
                "bad" if grave else "warn",
                "✕" if grave else "!",
                f"{differing:,} of {vm.plural(total_days, 'day')} "
                f"{'needs' if differing == 1 else 'need'} attention",
                text,
            ),
            table=(
                ["Days", "On the source", "On the destination", "What happened", "What it means"],
                issue_rows(result, past=past),
            ),
            left_out="",
            all_days=True,
        )
        if short:
            view["note"] = (
                "<b>About a day that arrived short:</b> the app can tell that entries are "
                "missing but not which ones or why. If every entry matters to you, keep the "
                "source until you have looked into it (run the same search for that day on "
                f"both systems and compare); otherwise the gap is {short[0]['difference']:,} "
                f"entries in {vm.format_big(result['source_total'])}. The saved report records "
                "the exact numbers."
            )
        if unfinished:
            view.update(
                hint="Continue the copy to send the rest."
                if not past
                else "This is the result as it was recorded.",
                actions=[
                    save,
                    ("Connect and continue this copy", "fix-past", "", "primary")
                    if past
                    else ("Continue copying", "continue", "", "primary"),
                ],
            )
        elif missing or not result["issues"]:
            view.update(
                hint="This is the result as it was recorded."
                if past
                else "Nothing on either system was changed by the check.",
                actions=[
                    save,
                    ("Connect to copy what is missing", "fix-past", "", "primary")
                    if past
                    else ("Copy what is missing", "copy-missing", "", "primary"),
                ],
            )
        else:
            view.update(
                hint="This is the result as it was recorded."
                if past
                else "Nothing more can be copied for these dates. Save the report for your "
                "records.",
                actions=finish_actions(),
            )
    elif phase == "partial":
        got = result["arrived_days"]
        view.update(
            verdict=(
                "info",
                "■",
                f"So far so good: all {vm.plural(got, 'day')} that arrived match"
                if got
                else "Nothing has arrived yet",
                f"This copy is not finished. <b>{got:,} of {vm.plural(total_days, 'day')}</b> "
                f"{'has' if got == 1 else 'have'} arrived and every one of them matches the "
                f"source. The other <b>{vm.plural(total_days - got, 'day')} "
                f"{'has' if total_days - got == 1 else 'have'} not been copied yet.</b>",
            ),
            hint="This is the result as it was recorded."
            if past
            else "The check found no problems in what has arrived. Continue the copy to send "
            "the rest.",
            actions=[
                save,
                ("Connect and continue this copy", "fix-past", "", "primary")
                if past
                else ("Continue copying", "continue", "", "primary"),
            ],
        )
    elif phase == "incomplete":
        what, todo = incomplete_reason(result["reasons"], error)
        view.update(
            verdict=(
                "warn",
                "?",
                "Could not finish checking",
                f"<b>What happened:</b> {escape(what)}<br><b>What to do:</b> {escape(todo)}",
            ),
            facts=[
                ("Days compared so far", f"{result['judged_days']:,} of {total_days:,}"),
                (
                    "Of those, matching",
                    f"{max(0, result['judged_days'] - result['differing_days']):,}",
                ),
                ("Dates in this copy", dates_text(copy)),
            ],
            hint="This is the result as it was recorded."
            if past
            else "No result yet. Checking again is safe and changes nothing.",
            actions=[save] if past else [("Check again", "check", "", "primary")],
        )
    else:
        done, total = copy["done_bytes"], copy["total_bytes"]
        if copy["state"] in {"stopped", "failed", "between"}:
            verb = "failed" if copy["state"] == "failed" else "was stopped"
            verdict = (
                "info",
                "■",
                f"This copy {verb} at {percent}% and never finished",
                f"{vm.format_bytes(done)} of {vm.format_bytes(total)} arrived and is safe on "
                f"the destination. {vm.format_bytes(max(0, total - done))} was not sent. What "
                "arrived has not been checked.",
            )
            hint = "To finish this copy, connect to both systems first."
            action = ("Connect and continue this copy", "fix-past", "", "primary")
        elif copy["state"] in {"running", "unsure"}:
            verdict = (
                "info",
                "■",
                "This copy was still running when it was last seen",
                "Connect to both systems to see how far it has got.",
            )
            hint = "To see this copy, connect to both systems first."
            action = ("Connect and go to this copy", "fix-past", "", "primary")
        else:
            verdict = (
                "info",
                "■",
                "This copy finished but has not been checked",
                f"{vm.format_bytes(total)} was sent to the destination. Checking counts the "
                "logs on both systems, day by day, to see that everything arrived.",
            )
            hint = "To check this copy, connect to both systems first."
            action = ("Connect and check this copy", "fix-past", "", "primary")
        if not past:
            action = ("Check what arrived", "check", "", "primary")
            hint = "Checking counts the logs on both systems. It changes nothing."
        view.update(verdict=verdict, hint=hint, actions=[save, action])
    return view


# Past runs --------------------------------------------------------------------------------


def run_result(copy: dict, result: dict) -> tuple[str, str]:
    """The result of a run as one coloured word or two."""
    percent = int(100 * copy["done_bytes"] / copy["total_bytes"]) if copy["total_bytes"] else 0
    if copy["state"] == "running":
        return ("accent", "In progress")
    if copy["state"] == "unsure":
        return ("warn", "Not sure it started")
    if copy["state"] in {"stopped", "between"}:
        return ("warn", f"Stopped at {percent}%, not finished")
    if copy["state"] == "failed":
        return ("warn", f"Failed at {percent}%, not finished")
    phase = result["phase"]
    if phase == "ok":
        return ("ok", "Everything arrived")
    if phase == "diff":
        if not result["issues"]:
            return ("bad", "Part did not arrive")
        days = result["differing_days"]
        return ("bad", f"{vm.plural(days, 'day')} {'needs' if days == 1 else 'need'} attention")
    if phase == "incomplete":
        return ("warn", "Could not finish checking")
    if copy["state"] == "missing":
        return ("bad", "Part did not arrive")
    return ("", "Finished, not checked")


def run_rows(found: list[dict], zone: str) -> list[dict]:
    """Every copy that was started, newest first."""
    rows = []
    for copy in reversed(found):
        labels = copy["labels"]
        rows.append(
            {
                "id": copy["id"],
                "when": vm.format_when(copy["started_at"], zone),
                "dates": dates_text(copy),
                "systems": f"{labels.get('source', 'source')} → "
                f"{labels.get('destination', 'destination')}",
                "size": vm.format_bytes(copy["total_bytes"]),
                "result": run_result(copy, copies.check(copy)),
                "live": copy["state"] in {"running", "unsure"},
            }
        )
    return rows


def unfinished(found: list[dict]) -> dict | None:
    """The newest copy that still has something to do, for the welcome-back hand-off."""
    for copy in reversed(found):
        if copy["state"] in {"running", "unsure", "stopped", "failed", "between"}:
            return copy
    return None


# Saved report -----------------------------------------------------------------------------


def report_markdown(
    copy: dict,
    result: dict,
    *,
    zone: str,
    rules: list[dict] | None = None,
    error: str | None = None,
) -> str:
    """The readable report: the same verdict and words as the Check result screen."""
    view = check_view(copy, result, past=True, error=error)
    _kind, _symbol, title, text = view["verdict"]

    def plain(rich: str) -> str:
        import re

        return re.sub(r"<[^>]+>", "", rich.replace("<br>", " ")).replace("&amp;", "&")

    labels = copy["labels"]
    lines = [
        "# Log copy report",
        "",
        f"**{title}.** {plain(text)}",
        "",
        "## This copy",
        "",
        f"- Started: {vm.format_when(copy['started_at'], zone)}",
        f"- Dates: {dates_text(copy)}",
        f"- From: {labels.get('source', 'not recorded')} (version "
        f"{vm.format_version(copy['source_version'])})",
        f"- To: {labels.get('destination', 'not recorded')} (version "
        f"{vm.format_version(copy['destination_version'])})",
        f"- Size: {vm.format_bytes(copy['total_bytes'])}",
        f"- Arrived: {vm.format_bytes(copy['done_bytes'])}",
        f"- Time spent copying: {vm.format_duration(copy['elapsed_seconds'])}"
        + (f", in {copy['segments']} parts" if copy["segments"] > 1 else ""),
    ]
    if result["checked_at"]:
        lines.append(f"- Checked: {vm.format_when(result['checked_at'], zone)}")
    if len(copy["parts"]) > 1:
        lines += [
            "",
            f"This copy ran as {len(copy['parts'])} separate copies back to back, because days "
            "in the middle were left out and the destination takes one unbroken range of "
            "dates at a time.",
        ]
    outside = left_out(copy)
    if outside:
        lines += ["", "## Not part of this copy", "", outside]
    if view["table"]:
        lines += ["", "## Days that need attention", ""]
        lines += [
            "| Days | On the source | On the destination | What happened | What it means |",
            "|---|---:|---:|---|---|",
        ]
        for days, source, arrived, badge, meaning in view["table"][1]:
            lines.append(f"| {days} | {source} | {arrived} | {badge[1]} | {meaning} |")
        if view["note"]:
            lines += ["", plain(view["note"])]
    if result["phase"] != "unchecked":
        lines += ["", "## All days", ""]
        lines += ["| Days | On the source | On the destination | Result |", "|---|---:|---:|---|"]
        lines += [
            f"| {days} | {source} | {arrived} | {tag} |"
            for days, source, arrived, tag in all_days_rows(copy, result)
        ]
        lines += [
            "",
            f"Days counted and judged: {result['judged_days']:,} of {result['days']:,}. Each "
            "system was counted twice and the two counts had to agree. Days run midnight to "
            "midnight UTC.",
        ]
    if rules:
        lines += ["", "## Rules copied in this session", ""]
        lines += [f"- {row['name']}: {row['badge'][1]}. {row['text']}." for row in rules]
    lines += [
        "",
        "## About this report",
        "",
        "The counts say how many log entries each system holds for each day. They do not "
        "compare the text of each entry. The source was never changed, and nothing on the "
        "destination was deleted or overwritten. The data file saved beside this report holds "
        "the full record, including the name the log systems use for each stored piece.",
        f"Report written {vm.format_when(vm.now_iso(), zone)}.",
    ]
    return "\n".join(lines) + "\n"
