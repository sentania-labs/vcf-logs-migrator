"""Two complete agreeing observations and per-day baseline/ingest accounting."""

from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime

from .errors import Code, MigrationError, require
from .models import Counts, Policy, Verification, Window
from .ports import Clock, Environment


def stable_counts(
    env: Environment,
    clock: Clock,
    policy: Policy,
    scope: str,
    window: Window,
    keys: tuple[str, ...],
) -> Counts:
    previous = None
    for attempt in range(policy.attempts):
        result = env.count(scope, window, keys)
        require(result.scope == scope, Code.SCHEMA)
        if result.complete:
            if previous == result:
                return result
            previous = result
        else:
            previous = None
        if attempt + 1 < policy.attempts:
            clock.sleep(policy.poll_seconds)
    try:
        start = datetime.fromtimestamp(window.start / 1000, UTC).date()
        end = datetime.fromtimestamp((window.end - 1) / 1000, UTC).date()
        dates = f"{start} to {end} UTC"
    except (OverflowError, OSError, ValueError):
        dates = "all dates"
    raise MigrationError(
        Code.INCOMPLETE,
        f"{scope.capitalize()} counts for {dates} did not complete twice with "
        "matching results. Check the appliance and retry.",
    )


def combine(parts: list[Counts], scope: str) -> Counts:
    daily: Counter = Counter()
    identities: Counter = Counter()
    for part in parts:
        daily.update(dict(part.daily))
        identities.update(dict(part.identities))
    return Counts(
        all(part.complete for part in parts),
        tuple(sorted(daily.items())),
        tuple(sorted(identities.items())),
        scope,
    )


def reconcile(source: Counts, baseline: Counts, actual: Counts, ingest: Counts) -> dict:
    require(
        all(c.complete for c in (source, baseline, actual, ingest)),
        Code.INCOMPLETE,
    )
    expected = Counter(dict(source.daily)) + Counter(dict(baseline.daily))
    expected += Counter(dict(ingest.daily))
    observed = dict(actual.daily)
    daily = [
        {
            "day": day,
            "source": dict(source.daily).get(day, 0),
            "preexisting": dict(baseline.daily).get(day, 0),
            "new_ingest": dict(ingest.daily).get(day, 0),
            "expected": expected.get(day, 0),
            "actual": observed.get(day, 0),
            "difference": observed.get(day, 0) - expected.get(day, 0),
        }
        for day in sorted(set(expected) | set(observed))
    ]
    success = all(d["difference"] == 0 for d in daily)
    months = {}
    for row in daily:
        month = months.setdefault(row["day"][:7], {"expected": 0, "actual": 0})
        month["expected"] += row["expected"]
        month["actual"] += row["actual"]
    return {
        "outcome": Verification.SUCCESS.value if success else Verification.DISCREPANCY.value,
        "complete": True,
        "source_count": source.total,
        "preexisting_count": baseline.total,
        "new_ingest_count": ingest.total,
        "expected_count": sum(expected.values()),
        "actual_count": actual.total,
        "daily": daily,
        "monthly": months,
        "method": (
            "Two agreeing complete observations; per-day counts including baseline and new ingest"
        ),
        "evidence": {
            "source": asdict(source),
            "baseline": asdict(baseline),
            "actual": asdict(actual),
            "ingest": asdict(ingest),
        },
    }
