"""Bounded complete=true polling and explicit multiset accounting."""

from collections import Counter
from dataclasses import asdict

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
        if result.complete and result.identity_complete:
            if previous == result:
                return result
            previous = result
        else:
            previous = None
        if attempt + 1 < policy.attempts:
            clock.sleep(policy.poll_seconds)
    raise MigrationError(Code.INCOMPLETE)


def reconcile(source: Counts, baseline: Counts, actual: Counts, ingest: Counts) -> dict:
    require(
        all(c.complete and c.identity_complete for c in (source, baseline, actual, ingest)),
        Code.INCOMPLETE,
    )
    # Cohorts are evidence, not operator-supplied count adjustments. Source overlapping the
    # baseline is blocked before copying; ingest duplicating source is still unexpected data.
    require(not (set(dict(source.identities)) & set(dict(baseline.identities))), Code.OVERLAP)
    expected_ids = Counter(dict(source.identities)) + Counter(dict(baseline.identities))
    expected_ids += Counter(dict(ingest.identities))
    actual_ids = Counter(dict(actual.identities))
    missing = expected_ids - actual_ids
    unexpected = actual_ids - expected_ids
    duplicates = set(dict(ingest.identities)) & set(dict(source.identities))
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
    success = (
        not missing
        and not unexpected
        and not duplicates
        and all(d["difference"] == 0 for d in daily)
    )
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
        "missing_events": sum(missing.values()),
        "unexpected_events": sum(unexpected.values()),
        "duplicated_ingest_identities": len(duplicates),
        "daily": daily,
        "monthly": months,
        "method": "Two agreeing complete observations; exact event-and-field multiset accounting",
        "evidence": {
            "source": asdict(source),
            "baseline": asdict(baseline),
            "actual": asdict(actual),
            "ingest": asdict(ingest),
        },
    }
