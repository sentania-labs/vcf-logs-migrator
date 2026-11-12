"""Plain words for what the engine reports. No Qt, no migration decisions.

Every figure shown comes from an engine or appliance observation; when one is missing the
screen says so instead of estimating. Identifiers and engine sentences never reach a
screen: they go to the troubleshooting file and the saved detail files.
"""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from ..errors import Code
from .copies import DAY_MS, day_of

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Formatting -------------------------------------------------------------------------------


def format_count(value) -> str:
    return "not known" if value is None else f"{int(value):,}"


def format_big(value) -> str:
    """`1.9 billion`, `104 million`, or the exact figure below a million."""
    if value is None:
        return "not known"
    if value >= 1_000_000_000:
        return f"{value / 1e9:.1f} billion"
    if value >= 1_000_000:
        return f"{round(value / 1e6):,} million"
    return f"{int(value):,}"


def format_bytes(size, unit: str | None = None) -> str:
    """`186 GB`, `57.6 GB`, `812 MB`. The CLI uses this too."""
    if size is None:
        return "not known"
    scales = (("GB", 1e9), ("MB", 1e6), ("kB", 1e3))
    name, scale = next(
        ((n, s) for n, s in scales if (unit == n if unit else size >= s)), ("B", 1.0)
    )
    if name == "B":
        return f"{int(size)} B"
    value = int(size) / scale
    return f"{value:,.0f} {name}" if value >= 100 else f"{value:,.1f} {name}"


def format_eta(seconds) -> str:
    """Compact form for the CLI: `~12 min`."""
    if seconds is None or seconds < 0:
        return "Unavailable"
    whole = int(round(seconds))
    if whole < 60:
        return f"~{whole} sec"
    if whole < 3600:
        return f"~{whole // 60} min"
    return f"~{whole // 3600} h {whole % 3600 // 60} min"


def format_duration(seconds) -> str:
    """`47 min`, `2 h 26 min`, `under a minute`."""
    if seconds is None or seconds < 0:
        return "not known"
    minutes = int(round(seconds / 60))
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def format_estimate(low, high) -> str:
    """A time range a person can plan around: `about 4 to 6 hours`."""
    if low is None or high is None:
        return "not known"
    if high < 3300:
        if high < 150:
            return "a few minutes"
        low_minutes = max(1, round(low / 60))
        high_minutes = max(low_minutes + 1, round(high / 60))
        if high_minutes >= 45:
            return "under an hour"
        return f"about {low_minutes} to {high_minutes} minutes"
    low_hours = max(1, round(low / 3600))
    high_hours = max(low_hours + 1, round(high / 3600))
    return f"about {low_hours} to {high_hours} hours"


def format_version(version: str) -> str:
    """`9.0.2.0200` reads as `9.0.2`."""
    parts = version.split(".")
    if len(parts) > 3 and all(p.isdigit() for p in parts[:3]):
        return ".".join(parts[:3])
    return version.removesuffix("-preview")


def format_day(value: date, year: bool = True) -> str:
    return f"{MONTHS[value.month - 1]} {value.day}" + (f", {value.year}" if year else "")


def format_range(first: date, last: date) -> str:
    """`Nov 10, 2026`, `May 11 to Nov 9, 2026` or `Nov 16, 2025 to Jan 31, 2026`."""
    if first == last:
        return format_day(first)
    if first.year == last.year:
        return f"{format_day(first, False)} to {format_day(last)}"
    return f"{format_day(first)} to {format_day(last)}"


def format_ranges(ranges: list[tuple[date, date]]) -> str:
    texts = [format_range(a, b) for a, b in ranges]
    if len(texts) <= 2:
        return " and ".join(texts)
    return ", ".join(texts[:-1]) + " and " + texts[-1]


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count:,} {word if count == 1 else many or word + 's'}"


def _local(iso: str | None, zone: str) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(ZoneInfo(zone_name(zone)))
    except Exception:
        return parsed.astimezone(UTC)


def zone_name(zone: str) -> str:
    return zone.removesuffix(" (detected)")


def _clock(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def format_clock(iso: str | None, zone: str, now: str | None = None) -> str:
    """A time of day relative to now: `today at 2:10 PM`, `on Nov 3 at 9:02 AM`."""
    moment = _local(iso, zone)
    if moment is None:
        return "at a time that was not recorded"
    today = (_local(now, zone) or datetime.now(moment.tzinfo)).date()
    if moment.date() == today:
        return f"today at {_clock(moment)}"
    if moment.date() == today + timedelta(days=1):
        return f"tomorrow at {_clock(moment)}"
    if moment.date() == today - timedelta(days=1):
        return f"yesterday at {_clock(moment)}"
    return f"on {format_day(moment.date(), moment.year != today.year)} at {_clock(moment)}"


def format_time(iso: str | None, zone: str) -> str:
    """Just the time of day: `2:10 PM`."""
    moment = _local(iso, zone)
    return _clock(moment) if moment else "not known"


def format_when(iso: str | None, zone: str) -> str:
    """A full timestamp for lists and reports: `Nov 12, 2026, 2:10 PM`."""
    moment = _local(iso, zone)
    return f"{format_day(moment.date())}, {_clock(moment)}" if moment else "not recorded"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def b(text) -> str:
    return f"<b>{escape(str(text))}</b>"


# Errors in plain words --------------------------------------------------------------------

# What happened and what to do, for every code the engine can return. The engine's own
# sentence (which may name stored groups by id) stays in the troubleshooting file.
PLAIN = {
    Code.INVALID_INPUT: (
        "One of the systems rejected the request as not valid.",
        "Try again. If it keeps happening, save a troubleshooting file.",
    ),
    Code.UNSUPPORTED: (
        "One of the systems does not support this on its version.",
        "Check the versions on Connect.",
    ),
    Code.INVENTORY_CHANGED: (
        "The logs stored on the source changed after the plan was made.",
        "Check the dates again to make a fresh plan.",
    ),
    Code.CAPACITY: (
        "A destination server does not have room for its share of the copy.",
        "Free up space on the destination, or copy fewer months, then check the dates again.",
    ),
    Code.OVERLAP: (
        "Part of these dates is already being copied or was copied before.",
        "Check the dates again to see which days are affected.",
    ),
    Code.TOPOLOGY: (
        "The destination is not the same system the plan was made for, "
        "or its number of servers changed.",
        "Test the connections again, then check the dates again.",
    ),
    Code.COLLISION: (
        "The destination already holds some of these logs in a form that differs from the source.",
        "Check the dates again to see which days are affected.",
    ),
    Code.PARTITIONS: (
        "A rule the copy depends on is missing or different on the destination.",
        "Go to Copy rules and copy the rule marked Needed.",
    ),
    Code.FIELDS: (
        "The destination does not have some fields these logs use.",
        "Add them in the destination's own settings, then check the dates again.",
    ),
    Code.ACK: (
        "The tick box about the newest logs was not ticked.",
        "Tick it next to the Start button.",
    ),
    Code.AUTH: (
        "A sign-in was not accepted.",
        "Test the connections again on Connect.",
    ),
    Code.CONNECTION: (
        "One of the systems did not answer.",
        "Check that it is running and that this computer can reach it, then try again.",
    ),
    Code.SCHEMA: (
        "One of the systems answered in a form this app does not recognise.",
        "Try again. If it keeps happening, save a troubleshooting file.",
    ),
    Code.INCOMPLETE: (
        "The app could not get a complete answer from one of the systems.",
        "Wait a few minutes and try again.",
    ),
    Code.VERIFICATION_PENDING: ("The result has not been checked yet.", "Check what arrived."),
    Code.UNCERTAIN: (
        "The request was sent but the destination did not answer.",
        "Do not send it again. The app keeps asking the destination what happened.",
    ),
    Code.BUSY: (
        "The destination is busy with another copy, or another window of this app is "
        "using the same record of past runs.",
        "Wait for the other copy to finish, or close the other window, then try again.",
    ),
    Code.SERVICE: (
        "A server on one of the systems is not running normally.",
        "Check that every server on both systems is up, then try again.",
    ),
    Code.RECOVERY: (
        "A server on the destination needs attention before anything else is done.",
        "Ask whoever runs the destination to check it.",
    ),
    Code.INTEGRITY: (
        "The stand-in systems do not match their recorded state.",
        "Start the stand-in again with a fresh record.",
    ),
    Code.STATE: (
        "That step is not available for this copy right now.",
        "Go back one step and try from there.",
    ),
    Code.STORAGE: (
        "The record of past runs on this computer could not be read or written.",
        "Check Settings, Fine tuning, for where past runs are stored.",
    ),
    Code.NOT_RECEIVED: (
        "Part of what was sent did not arrive.",
        "Copy what is missing, then check again.",
    ),
    Code.UNCERTAIN_START: (
        "An earlier start of these dates got no answer from the destination, so the dates "
        "are locked for 15 minutes to avoid copying them twice.",
        "Wait until the lock ends, then check the dates again.",
    ),
}
UNKNOWN_ERROR = (
    "Something went wrong that this app does not have words for.",
    "Try again. If it keeps happening, save a troubleshooting file.",
)


def error_code(text: str | None) -> Code | None:
    """A failure arrives as a bare code or `<code>: <engine sentence>`."""
    if not text:
        return None
    try:
        return Code(text.partition(": ")[0])
    except ValueError:
        return None


def plain_error(text: str | None) -> tuple[str, str]:
    """(what happened, what to do). A `gui:` failure was already worded for a person."""
    if text and text.startswith("gui:"):
        return text[4:], ""
    return PLAIN.get(error_code(text), UNKNOWN_ERROR)


def what_happened(text: str | None) -> str:
    """One paragraph of rich text: what happened and what to do."""
    what, todo = plain_error(text)
    return f"<b>What happened:</b> {escape(what)}" + (
        f"<br><b>What to do:</b> {escape(todo)}" if todo else ""
    )


# Frame ------------------------------------------------------------------------------------

STEPS = (
    ("connect", "Connect", "Sign in to both"),
    ("rules", "Copy rules", "For new logs"),
    ("dates", "Choose dates", "What to copy"),
    ("copy", "Copy logs", "Watch it run"),
    ("check", "Check result", "Did it all arrive?"),
)
STEP_NAMES = {**{key: name for key, name, _ in STEPS}, "runs": "Past runs", "settings": "Settings"}
LOCK_REASON = {
    "rules": "After connecting",
    "dates": "After connecting",
    "copy": "After choosing dates",
    "check": "After a copy",
}


def chip(kind: str, text: str) -> dict:
    return {"kind": kind, "text": text}


def header_chips(frame: dict) -> list[dict]:
    """The connection summary shown on every screen."""
    source, destination = frame.get("source"), frame.get("destination")
    if frame.get("connected"):
        chips = [chip("ok", f"✓ Source: {source['host']} · {source['version']}")]
        if frame.get("out_of_touch"):
            chips.append(
                chip(
                    "warn",
                    f"! Destination: {destination['host']} · out of touch since "
                    f"{frame['out_of_touch']}",
                )
            )
        else:
            chips.append(
                chip("ok", f"✓ Destination: {destination['host']} · {destination['version']}")
            )
        return chips
    if frame.get("refused"):
        return [
            chip("", f"Source: {source['host']} · {source['version']}"),
            chip("", f"Destination: {destination['host']} · {destination['version']}"),
            chip("bad", "Cannot migrate"),
        ]
    failure = frame.get("failure")
    if failure and failure.get("kind") in {"auth", "reach", "identity"}:
        side, other = failure["side"], "destination" if failure["side"] == "source" else "source"
        bad = {
            "auth": f"✕ {side.capitalize()} not signed in",
            "reach": f"✕ {side.capitalize()} not found",
            "identity": f"✕ {side.capitalize()} identity changed",
        }[failure["kind"]]
        good = (
            (f"✓ {other.capitalize()} signed in", "ok")
            if failure.get("other_signed_in")
            else (f"{other.capitalize()} found", "")
            if failure.get("other_found")
            else None
        )
        chips = [chip("bad", bad)]
        if good:
            chips.insert(0 if other == "source" else 1, chip(good[1], good[0]))
        return chips
    return [chip("", "Not connected")]


# Connect ----------------------------------------------------------------------------------


def need_list(values: dict[str, str], token: bool, server_needed: bool = False) -> list[str]:
    """What is still missing before the connections can be tested, in the order it is typed."""

    def empty(key):
        return not (values.get(key) or "").strip()

    need = []
    if empty("source_host"):
        need.append("source address")
    if empty("source_password"):
        need.append("source password")
    if empty("ssh_password"):
        need.append("source server password")
    if empty("destination_host"):
        need.append("destination address")
    if token:
        if empty("ops_url"):
            need.append("VCF Operations address")
        if empty("ops_token"):
            need.append("access token")
    else:
        if empty("destination_password"):
            need.append("destination password")
        if server_needed and empty("destination_ssh_user"):
            need.append("destination server sign-in")
        if not empty("destination_ssh_user") and empty("destination_ssh_password"):
            need.append("destination server password (or clear the server user)")
    return need


def fingerprint_pairs(digest: str) -> str:
    """A SHA-256 digest as a browser shows it: `3A:1F:9C:...`."""
    text = digest.upper()
    return ":".join(text[i : i + 2] for i in range(0, len(text), 2))


def server_key(digest: str) -> str:
    """A server key digest as that server's administrator sees it: `SHA256:b1Wc...`."""
    if digest.startswith("SHA256:"):
        return digest
    try:
        return "SHA256:" + base64.b64encode(bytes.fromhex(digest)).decode().rstrip("=")
    except ValueError:
        return digest


def _versions(versions: dict[str, str]) -> str:
    distinct = sorted({format_version(v) for v in versions.values()})
    return " and ".join(distinct) if distinct else "not known"


def _servers(versions: dict[str, str]) -> int | None:
    return None if set(versions) == {"cluster"} or not versions else len(versions)


def _disagreement(versions: dict[str, str]) -> str:
    groups: dict[str, list[str]] = {}
    for node, version in versions.items():
        groups.setdefault(format_version(version), []).append(node)
    ordered = sorted(groups.items(), key=lambda item: len(item[1]))
    parts = [
        f"{' and '.join(nodes)} {'is' if len(nodes) == 1 else 'are'} on {version}"
        for version, nodes in ordered
    ]
    return " while ".join(parts)


def verdict(check: dict) -> dict:
    """The version decision as a headline, an explanation and which system has to change."""
    source, destination = check.get("source") or {}, check.get("destination") or {}
    sv, dv = _versions(source), _versions(destination)
    result = {
        "passed": bool(check.get("passed")),
        "source_version": sv,
        "destination_version": dv,
        "source_servers": _servers(source),
        "destination_servers": _servers(destination),
        "source_badge": None,
        "destination_badge": None,
        "caution": None,
    }
    if check.get("passed"):
        result["title"] = "✓ Connected. These two systems can migrate."
        result["text"] = f"Logs can be copied from version {sv} to version {dv}."
        if any("has not been tested" in w for w in check.get("warnings", [])):
            result["caution"] = (
                f"Version {dv} has not been tried with this app.",
                "It answered the way a version that works does, so you can continue. Check "
                "the result carefully after the first copy.",
            )
        return result
    reason = check.get("reason", "")
    if reason == "source_newer":
        result["destination_badge"] = "Needs upgrading"
        result["title"] = (
            "✕ These two systems cannot migrate: the source is newer than the destination."
        )
        result["text"] = (
            f"The source is version {sv} and the destination is {dv}. Logs can only move to "
            f"the same version or a newer one. <b>What to do:</b> upgrade the destination to "
            f"{sv} or later, then test again."
        )
    elif reason == "destination_old":
        result["destination_badge"] = "Too old"
        result["title"] = "✕ These two systems cannot migrate: the destination is too old."
        result["text"] = (
            f"The destination is version {dv}. Copying logs in needs version 9.0 or later. "
            "<b>What to do:</b> check that the destination address really is the new system. "
            "If it is, upgrade it, then test again."
        )
    elif reason in {"source_mixed", "destination_mixed"}:
        side = reason.split("_")[0]
        versions = source if side == "source" else destination
        result[f"{side}_badge"] = "Servers disagree"
        count = len(versions)
        result["title"] = (
            f"✕ These two systems cannot migrate yet: the {side} servers are on different versions."
        )
        result["text"] = (
            f"{escape(_disagreement(versions))}, which usually means an upgrade is half done. "
            f"<b>What to do:</b> finish upgrading the {side} so all {count} servers match, "
            "then test again."
        )
    else:
        side = "source" if reason.startswith("source") else "destination"
        result[f"{side}_badge"] = "Not known"
        result["title"] = "✕ Could not tell whether these two systems can migrate."
        result["text"] = (
            f"The {side} did not report "
            + (
                "whether it can take logs in"
                if reason == "destination_unchecked"
                else "its version in a form the app can read"
            )
            + ". <b>What to do:</b> test again. If it keeps happening, save a troubleshooting "
            "file from Settings and send it to whoever is helping you."
        )
    return result


def failure_banner(failure: dict) -> tuple[str, str, str]:
    """(kind, title, text) for a test that stopped at one field."""
    side = failure["side"]
    other = "destination" if side == "source" else "source"
    field, kind = failure.get("field"), failure["kind"]
    if failure.get("edited"):
        return (
            "info",
            {
                "address": "Address changed. Test again to check it.",
                "token": "Access token changed. Test again to check it.",
            }.get(field, "Password changed. Test again to check it."),
            f"The {other} is still signed in." if failure.get("other_signed_in") else "",
        )
    if kind == "reach":
        found = f"The {other} was found. " if failure.get("other_found") else ""
        if field == "ops_address":
            return (
                "bad",
                "Could not find VCF Operations.",
                "No access token was sent. Check the VCF Operations address below (marked in "
                "red), then test again.",
            )
        return (
            "bad",
            f"Could not find the {side} system.",
            f"{found}No password was sent to either. Check the {side} address below (marked "
            "in red), then test again.",
        )
    if kind == "auth":
        what = {
            "password": f"{side} password",
            "server_password": f"{side} server password",
            "token": "access token",
        }.get(field, f"{side} sign-in")
        if field == "password" and side == "destination":
            title = "The source signed in. The destination did not."
        elif field == "server_password":
            title = f"The {side}'s servers did not accept the server sign-in."
        elif field == "token":
            title = "The access token was not accepted."
        else:
            title = f"The {side} did not sign in."
        return (
            "bad",
            title,
            f"Fix the {what} below (marked in red), then test again. Nothing else needs retyping.",
        )
    if kind == "identity":
        return (
            "bad",
            f"The identity of the {side} has changed since you first connected.",
            "The app recorded this system's identity the first time and it now presents a "
            "different one. That is expected if its certificate was replaced or the system was "
            "rebuilt. If neither happened, stop and ask whoever runs it. No password was sent.",
        )
    what, todo = plain_error(failure.get("code"))
    return ("bad", "The test could not finish.", f"{what} {todo}".strip())


def field_hint(failure: dict | None, side: str, field: str) -> str | None:
    """The red sentence under the box a failed test points at."""
    if (
        not failure
        or failure.get("edited")
        or failure["side"] != side
        or failure.get("field") != field
    ):
        return None
    if failure["kind"] == "reach":
        return (
            "Could not reach this address. Check the spelling, and that this computer can "
            "reach it on the network."
        )
    if field == "server_password":
        return f"The {side}'s servers did not accept this user and password."
    if field == "token":
        return "VCF Operations did not accept this access token."
    return f"The {side} did not accept this user and password. Type the password again."


# Copy rules -------------------------------------------------------------------------------

KIND_NAME = {
    "partitions": "Storage group",
    "forwarding": "Forwarding",
    "masking": "Masking",
    "filters": "Filter",
}


def rule_name(kind: str, name: str) -> str:
    return f"{KIND_NAME.get(kind, 'Rule')} “{name}”"


def _days(seconds) -> str:
    return f"for {int(seconds) // 86400} days" if seconds else "with no time limit"


def rule_does(kind: str, rule: dict | None) -> str:
    """What a rule does, in a sentence, from the fields the source reports."""
    if not rule:
        return {
            "partitions": "Decides how long a group of logs is kept.",
            "forwarding": "Sends a copy of new logs to another system.",
            "masking": "Hides part of the text of new logs.",
            "filters": "Drops new logs that match a condition.",
        }.get(kind, "Changes what happens to new logs.")
    if kind == "partitions":
        seconds = rule.get("retention_seconds") or rule.get("retentionSeconds") or 0
        return f"Keeps the logs it selects {_days(seconds)}."
    if kind == "forwarding":
        return f"Sends a copy of new logs to {rule.get('host', 'another system')}" + (
            ", only those that match a condition." if rule.get("filter") else "."
        )
    if kind == "masking":
        state = "" if rule.get("enabled", True) else " Switched off on the source."
        return "Hides text that matches a pattern in new logs." + state
    if kind == "filters":
        state = "on" if rule.get("enabled") else "off"
        return f"Drops new logs that match a condition. Switched {state} on the source."
    return "Changes what happens to new logs."


def rule_rows(comparison: dict | None) -> list[dict]:
    """One row per rule that is missing or different on the destination."""
    if comparison is None:
        return []
    details = comparison.get("details", {})
    rows = []
    for rule in comparison["rules"]:
        if rule["status"] == "identical":
            continue
        kind, changed = rule["type"], rule.get("changed_fields", [])
        detail = details.get(rule["selector"], {})
        source, destination = detail.get("source"), detail.get("destination")
        needed = kind == "partitions"
        trusts = any("acceptCert" in w for w in rule.get("warnings", []))
        switch = kind in {"filters", "masking"} and "enabled" in changed
        if rule["status"] == "missing":
            now = "Missing"
            off = kind in {"filters", "masking"} and source and not source.get("enabled", True)
            will = "Add it, switched off" if off else "Add it"
        elif switch and destination is not None and len(changed) == 1:
            on = bool(destination.get("enabled"))
            now = "Switched on" if on else "Switched off"
            will = (
                "Switch it off here too, so those logs are kept"
                if on and kind == "filters"
                else f"Switch it {'off' if on else 'on'} here too"
            )
        else:
            now = "Different"
            will = "Change it to match the source"
        note = ""
        if needed:
            note = (
                "Logs cannot be copied until this exists. No restart is needed for the copy; "
                "new logs use it after the destination’s next restart."
                if rule["status"] == "missing"
                else "Logs cannot be copied until this matches the source."
            )
        elif trusts:
            note = "The destination will be told to trust that system’s certificate."
        rows.append(
            {
                "selector": rule["selector"],
                "kind": kind,
                "name": rule_name(kind, rule["name"]),
                "short": rule["name"],
                "does": rule_does(kind, source),
                "now": now,
                "will": will,
                "note": note,
                "needed": needed,
                "changes": rule["status"] == "differs",
                "wordy": needed or trusts or rule["status"] == "differs",
            }
        )
    rows.sort(key=lambda row: (not row["needed"], not row["wordy"]))
    return rows


def rules_summary(comparison: dict | None) -> dict:
    rules = comparison["rules"] if comparison else []
    return {
        "total": len(rules),
        "same": sum(rule["status"] == "identical" for rule in rules),
        "destination_only": len(comparison.get("destination_only", [])) if comparison else 0,
    }


def rules_banner(rows: list[dict]) -> tuple[str, str]:
    count = len(rows)
    needed = sum(row["needed"] for row in rows)
    title = (
        f"{plural(count, 'rule')} on the source {'is' if count == 1 else 'are'} missing or "
        "different on the destination."
    )
    rest = count - needed
    if needed and rest:
        lead = (
            "One of them, a storage group marked Needed,"
            if needed == 1
            else f"{needed} of them, storage groups marked Needed,"
        )
        text = (
            f"{lead} must be copied before logs can be moved. The other {rest} "
            f"{'is' if rest == 1 else 'are'} your choice. "
        )
    elif needed:
        lead = "It is a storage group" if count == 1 else "They are storage groups"
        text = f"{lead} marked Needed, and must be copied before logs can be moved. "
    else:
        text = "None of them is needed to copy logs; they are your choice. "
    return (
        title,
        text + "Copying rules moves no logs, deletes nothing and does not change the source.",
    )


def rules_tail(summary: dict) -> str:
    parts = []
    if summary["same"]:
        parts.append(
            f"{plural(summary['same'], 'more rule')} "
            f"{'is' if summary['same'] == 1 else 'are'} already the same on both sides."
        )
    if summary["destination_only"]:
        only = summary["destination_only"]
        parts.append(
            f"{plural(only, 'rule')} {'exists' if only == 1 else 'exist'} only on the "
            f"destination and {'is' if only == 1 else 'are'} left alone."
        )
    return " ".join(parts)


RULE_FAILURE = {
    Code.INVALID_INPUT.value: "The destination refused it as not valid",
    Code.UNSUPPORTED.value: "The destination’s version does not support it",
    Code.INVENTORY_CHANGED.value: "It changed on the destination while it was being copied",
    Code.AUTH.value: "The destination did not accept the sign-in",
    Code.CONNECTION.value: "The destination did not answer, or refused it",
    Code.INCOMPLETE.value: "It could not be read back from the destination to confirm it",
    Code.UNCERTAIN.value: "The destination did not answer, so it may or may not be there",
}


def rule_results(result: dict | None) -> list[dict]:
    """Per rule: Copied, Left out or Not copied, each with a plain reason."""
    rows = []
    for rule in (result or {}).get("rules", []):
        status = rule["status"]
        if status == "unchanged" or (status == "skipped" and rule["comparison"] == "identical"):
            continue
        name = rule_name(rule["type"], rule["name"])
        if status in {"created", "updated"}:
            rows.append(
                {"name": name, "badge": ("ok", "Copied"), "text": "Confirmed on the destination"}
            )
        elif status == "skipped":
            rows.append(
                {"name": name, "badge": ("", "Left out"), "text": "You chose not to copy it"}
            )
        else:
            reason = RULE_FAILURE.get(rule.get("reason", ""), "The destination did not take it")
            rows.append(
                {
                    "name": name,
                    "badge": ("bad", "Not copied" if status == "failed" else "Not sure"),
                    "text": reason,
                    "selector": rule["selector"],
                    "failed": True,
                }
            )
    return rows


def rules_state(comparison: dict | None, result: dict | None) -> str:
    """`unknown`, `none`, `diff`, `done` or `partial`: where the Copy rules step stands."""
    if result is not None:
        return "done" if result["complete"] else "partial"
    if comparison is None:
        return "unknown"
    return "diff" if rule_rows(comparison) else "none"


def needed_outstanding(comparison: dict | None, result: dict | None) -> bool:
    """Whether a rule the copy depends on is still missing on the destination."""
    needed = {row["selector"] for row in rule_rows(comparison) if row["needed"]}
    if result is not None:
        copied = {r["selector"] for r in result["rules"] if r["status"] in {"created", "updated"}}
        needed -= copied
    return bool(needed)


# Settings ---------------------------------------------------------------------------------

REFRESH_RANGE = (2, 60)
GIVE_UP_RANGE = (1, 60)


def number_problem(text: str, low: int, high: int, current: int) -> str | None:
    """Why a typed number cannot be used; the old value stays in force."""
    try:
        value = int(text.strip())
    except ValueError:
        value = None
    if value is None or not low <= value <= high or str(value) != text.strip():
        return f"Enter a whole number from {low} to {high}. Still using {current}."
    return None


def folder_problem(path: str) -> str | None:
    from pathlib import Path

    target = Path(path).expanduser()
    if target.exists() and not target.is_dir():
        return "That is a file, not a folder. Choose a folder."
    return None


# History of the source --------------------------------------------------------------------


def history(source: dict | None) -> dict | None:
    """First day with finished logs, the last day that can be picked, and where the newest
    logs (still being written) begin. None until the source's stored logs have been listed."""
    if not source:
        return None
    sealed = [
        g
        for g in source.get("buckets", [])
        if g["status"] != "active" and g.get("start") is not None and g.get("end") is not None
    ]
    if not sealed:
        return None
    first = day_of(min(g["start"] for g in sealed))
    last_sealed = day_of(max(g["end"] for g in sealed))
    open_groups = [g for g in source["buckets"] if g["status"] == "active"]
    starts = [g["start"] for g in open_groups if g.get("bounds_proven") and g.get("start")]
    newest = None
    if open_groups:
        if starts and len(starts) == len(open_groups):
            newest = day_of(min(starts))
        else:
            ends: dict[str, int] = {}
            for g in sealed:
                ends[g["node"]] = max(ends.get(g["node"], 0), g["end"])
            newest = day_of(min(ends.values())) if ends else last_sealed
    last = min(last_sealed, newest - timedelta(days=1)) if newest else last_sealed
    return {"first": first, "last": max(first, last), "newest": newest, "last_sealed": last_sealed}


PRESETS = (("Last 3 months", 3), ("Last 6 months", 6), ("Last 12 months", 12), ("Everything", None))


def preset_days(months: int | None, extent: dict) -> tuple[date, date]:
    """Inclusive day pair for a preset, counted back from the last day that can be picked."""
    last = extent["last"]
    if months is None:
        return extent["first"], last
    year, month = last.year, last.month - months
    while month <= 0:
        year, month = year - 1, month + 12
    for day in (last.day, 28):
        try:
            start = date(year, month, day) + timedelta(days=1)
            break
        except ValueError:
            continue
    return max(extent["first"], start), last


def dates_problem(start: date, end: date, extent: dict | None) -> str | None:
    if end < start:
        return "The end date is before the start date."
    if extent and end > extent["last"] and extent["newest"]:
        return (
            f"Logs after {format_day(extent['last'])} are still being written and cannot be "
            "copied yet."
        )
    return None


def window_ms(start: date, end: date) -> tuple[int, int]:
    first = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1000)
    last = int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp() * 1000)
    return first, last + DAY_MS
