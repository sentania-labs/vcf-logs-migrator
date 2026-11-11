"""Thin migration API wrapper. The destination owns task state and progress."""

import re
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .config import ConfigMigration
from .errors import Code, MigrationError, Rejection, require, safe_boundary
from .models import (
    ACTIVE_ACK,
    COUNT_BASIS,
    DAY,
    EVERYTHING_END,
    Counts,
    Policy,
    Verification,
    Window,
    digest,
    sealed_frontier,
    selected_message_count,
)
from .planning import build_plan, partition_diff
from .ports import SystemClock, Transfer
from .verification import combine, reconcile, stable_counts

TERMINAL = {"idle", "completed", "cancelled", "failed"}
TASK_REPORT = "task_report"
RATE_WINDOW_SECONDS = 60
EVERYTHING = Window(0, EVERYTHING_END)
# A start whose outcome is uncertain (no HTTP answer) holds its window this long; after that,
# an appliance history with no task covering the window is the evidence that nothing started.
UNCERTAIN_RELEASE_SECONDS = 900
START_CLOCK_SLACK_MS = 300_000


def _ids(keys):
    return {key.rsplit("/", 1)[-1] for key in keys}


def day_window(window: Window) -> Window:
    """The whole UTC days a window touches; count queries are compared per day."""
    return Window(window.start // DAY * DAY, ((window.end - 1) // DAY + 1) * DAY)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()


def _affected_error(code, message, ids):
    error = MigrationError(code, message)
    error.affected_ids = set(ids)
    return error


def _inspect_destination(inspect, destination, originals):
    try:
        return inspect(destination, set(originals))
    except MigrationError as error:
        dates = []
        for bucket_id in sorted(getattr(error, "affected_ids", set())):
            original = originals.get(bucket_id)
            if original is None:
                continue
            start = original.start if hasattr(original, "start") else original.get("start")
            end = original.end if hasattr(original, "end") else original.get("end")
            if start is not None and end is not None:
                dates.append(f"{bucket_id}: {_day(start)} to {_day(end)} UTC")
        if dates:
            error.message += " Affected dates: " + "; ".join(dates) + "."
        raise


def _check_destination_copies(existing, originals):
    bad = []
    incomplete = False
    for bucket in existing:
        original = originals[bucket.id]
        size = original.size if hasattr(original, "size") else original["size"]
        messages = original.messages if hasattr(original, "messages") else original.get("messages")
        start = original.start if hasattr(original, "start") else original.get("start")
        end = original.end if hasattr(original, "end") else original.get("end")
        problems = []
        if not bucket.complete or not bucket.registered:
            incomplete = True
            problems.append("incomplete or not indexed")
        if bucket.size != size:
            problems.append(f"source size {size} bytes, destination size {bucket.size} bytes")
        if messages is not None and bucket.messages is not None and messages != bucket.messages:
            problems.append(f"source {messages} messages, destination {bucket.messages} messages")
        if problems:
            dates = (
                f"{_day(start)} to {_day(end)} UTC"
                if start is not None and end is not None
                else "dates unknown"
            )
            bad.append(
                (
                    bucket.id,
                    f"{bucket.id} ({dates}; destination: {bucket.node}; "
                    + "; ".join(problems)
                    + ")",
                )
            )
    if bad:
        count = len({bucket_id for bucket_id, _ in bad})
        raise _affected_error(
            Code.INCOMPLETE if incomplete else Code.COLLISION,
            f"{count} destination bucket{'s need' if count != 1 else ' needs'} attention: "
            + "; ".join(detail for _, detail in bad)
            + ". The listed destination copies must be removed before this window can be copied. "
            "Inspect them and refresh; this tool does not remove them.",
            {bucket_id for bucket_id, _ in bad},
        )


def _check_duplicate_destination(existing, originals, selected_ids, placements):
    counts = Counter(bucket.id for bucket in existing)
    duplicate_ids = sorted(bucket_id for bucket_id, count in counts.items() if count > 1)
    details = []
    unsafe = []
    for bucket_id in duplicate_ids:
        original = originals[bucket_id]
        start = original.start if hasattr(original, "start") else original.get("start")
        end = original.end if hasattr(original, "end") else original.get("end")
        dates = (
            f"{_day(start)} to {_day(end)} UTC"
            if start is not None and end is not None
            else "dates unknown"
        )
        copies = [b for b in existing if b.id == bucket_id]
        nodes = {b.node for b in copies}
        description = f"{bucket_id} ({dates}; destination: {', '.join(sorted(nodes))})"
        details.append(description)
        if len(nodes) != len(copies):
            raise MigrationError(
                Code.INCOMPLETE,
                "A destination node listed the same bucket more than once; the locations "
                "could not be distinguished: "
                + description
                + ". Inspect the listing before retrying.",
            )
        manifest = original.manifest if hasattr(original, "manifest") else original.get("manifest")
        if manifest is not None and any(
            b.manifest is not None and b.manifest != manifest for b in copies
        ):
            unsafe.append(description)
    if unsafe:
        count = len(unsafe)
        raise _affected_error(
            Code.COLLISION,
            f"{count} selected bucket{'s have' if count != 1 else ' has'} "
            "multiple destination entries: "
            + "; ".join(unsafe)
            + ". The copies differ from the source. "
            "Inspect the listed destination copies before retrying.",
            [detail.split(" ", 1)[0] for detail in unsafe],
        )
    if selected_ids - {b.id for b in existing}:
        misplaced = []
        for bucket_id in sorted({b.id for b in existing}):
            nodes = {b.node for b in existing if b.id == bucket_id}
            assigned = placements.get(bucket_id)
            if assigned is not None and assigned not in nodes:
                original = originals[bucket_id]
                start = original.start if hasattr(original, "start") else original.get("start")
                end = original.end if hasattr(original, "end") else original.get("end")
                dates = (
                    f"{_day(start)} to {_day(end)} UTC"
                    if start is not None and end is not None
                    else "dates unknown"
                )
                misplaced.append(
                    f"{bucket_id} ({dates}; destination: {', '.join(sorted(nodes))}; "
                    f"new copy: {assigned})"
                )
        if misplaced:
            raise _affected_error(
                Code.TOPOLOGY,
                "These destination buckets are on a different node than a new copy would use. "
                "Submitting this window would add another copy: "
                + "; ".join(misplaced)
                + ". Inspect the copies before continuing.",
                [detail.split(" ", 1)[0] for detail in misplaced],
            )
    return details


def partial_days(source, plan: dict) -> list[str]:
    """Requested days whose source events are not all in the selected buckets: days shared
    with an unselected sealed bucket, and days at or after the active tail. The tail starts
    at the first active bucket's proven start; when active bounds are unknown (the 8.18
    listing shows none) it starts at the oldest node's newest sealed bucket end.
    That is the sealed frontier. Counts on partial days are reported, never judged; a day
    before the frontier that an open bucket secretly holds late arrivals for shows up as a
    discrepancy, not a pass."""
    coverage = day_window(Window(**plan.get("window", plan["coverage"])))
    selected = {b["node"] + "/" + b["id"] for b in plan["selected"]}
    days = []
    cursor = coverage.start
    while cursor < coverage.end:
        days.append(cursor)
        cursor += DAY
    partial = set()
    unproven_active = False
    for bucket in source.buckets:
        if bucket.key in selected or bucket.empty_proven:
            continue
        if bucket.status == "active" and not (bucket.bounds_proven and bucket.start is not None):
            unproven_active = True
            continue
        if bucket.start is None or bucket.end is None:
            unproven_active = True
            continue
        for start in days:
            if bucket.start < start + DAY and bucket.end >= start:
                partial.add(start)
    if unproven_active:
        sealed = sealed_frontier(source.buckets)
        if sealed is None:
            partial.update(days)
        else:
            partial.update(start for start in days if start >= sealed // DAY * DAY)
    return sorted(_day(start) for start in partial)


class Engine:
    def __init__(self, environment, journal, clock=None, on_event=None):
        self.env = environment
        self.journal = journal
        if hasattr(environment, "inventory_journal"):
            environment.inventory_journal = journal
        self.clock = clock or SystemClock()
        self.on_event = on_event
        self._samples = {}

    @property
    def attribution(self) -> bool:
        return bool(getattr(self.env, "attribution", True))

    def config_compare(self):
        """Compare forwarding, partitions, masking and filters by name."""
        return ConfigMigration(self.env).compare()

    def config_apply(self, selection):
        """Apply explicit type:name selectors; return per-rule observations."""
        return ConfigMigration(self.env).apply(selection)

    def _audit(self, run, event, details=None):
        self.journal.save(run, event, details)
        if self.on_event:
            try:
                self.on_event({"run_id": run["id"], "kind": event, "details": details or {}})
            except Exception:
                pass  # Presentation observers cannot interrupt an authorized API operation.

    @contextmanager
    def _exclusive(self):
        with ExitStack() as stack:
            if self.journal is not None:
                stack.enter_context(self.journal.exclusive())
            stack.enter_context(self.env.exclusive())
            yield

    def _releasable(self, row, destination) -> bool:
        """An uncertain start (no acknowledgment) may be released only when enough time has
        passed for the appliance to show a task it accepted, and its history shows none
        covering the window. One idle observation alone proves nothing."""
        attempted = row.get("attempted_at")
        if attempted:
            try:
                started = datetime.fromisoformat(attempted)
                now = datetime.fromisoformat(self.clock.now())
                if (now - started).total_seconds() < UNCERTAIN_RELEASE_SECONDS:
                    return False
            except ValueError:
                return False
        window = Window(**row["window"]) if row.get("window") else None
        for prior in self.env.history():
            if prior.destination != destination:
                continue
            if row.get("task_id") and prior.task_id == row["task_id"]:
                return False
            if window is not None and day_window(prior.window).overlaps(day_window(window)):
                return False
        return True

    def _record(self, task, destination):
        """Ledger what the API reports as transferred once a task has finished."""
        if self.journal is None or task.status not in TERMINAL:
            return
        if task.status == "idle" and not self.attribution:
            # A live API that shows no task: a selection the API accepted stays recorded; one
            # whose start was never acknowledged is released only once it is stale and the
            # appliance history shows no task covering it.
            for row in self.journal.transfers(destination):
                if (
                    not row["closed"]
                    and not row.get("acknowledged")
                    and self._releasable(row, destination)
                ):
                    self.journal.release_transfer(row["run"])
            return
        received = {*task.completed, *(k for n in task.nodes for k in n.completed)}
        received_ids = _ids(received)
        every = self.journal.transfers(destination)
        if task.id and task.id in {r["task_id"] for r in every if r["closed"] and r.get("task_id")}:
            return
        rows = [row for row in every if not row["closed"]]
        for row in rows:
            if task.destination != destination:
                matched = False
            elif row.get("task_id"):
                matched = bool(task.id) and task.id == row["task_id"]
            elif task.bucket_keys:
                matched = _ids(row["keys"]) == _ids(task.bucket_keys)
            elif task.window is not None and row.get("window"):
                # Compared by day: a server may echo the window on day boundaries.
                matched = day_window(Window(**row["window"])) == day_window(task.window)
            else:
                # The API reported neither the requested buckets nor the window; the only
                # open selection for this destination is the one it answered for.
                matched = len(rows) == 1
            if not matched:
                if self.attribution:
                    self.journal.settle_transfer(row["run"], [])
                elif not row.get("acknowledged") and self._releasable(row, destination):
                    # Live: a finished task that is not this selection's, the start was
                    # never acknowledged, and the uncertain start is stale.
                    self.journal.release_transfer(row["run"])
            elif self.attribution:
                self.journal.settle_transfer(
                    row["run"],
                    sorted(k for k in row["keys"] if k.rsplit("/", 1)[-1] in received_ids),
                )
            else:
                # The live API may report COMPLETED even when a node dropped work. Only
                # registered destination inventory or explicit per-bucket evidence proves
                # receipt. A still-scheduled bucket is not yet safe to count.
                if not row.get("acknowledged"):
                    self.journal.note_transfer_task(row["run"], task.id or "")
                cluster = self.env.destination_cluster(fresh=True)
                require(
                    all(
                        node.scheduled_ids is not None
                        or not any((node.scheduled_buckets or {}).values())
                        for node in task.nodes
                    ),
                    Code.INCOMPLETE,
                    "The destination reported remaining buckets without usable identities; "
                    "retry status.",
                )
                pending = {
                    bucket
                    for node in task.nodes
                    for group in (node.scheduled_ids or {}).values()
                    for bucket in group
                }
                if cluster.inventory_available:
                    seen = {b.id for b in cluster.buckets if b.registered}
                    arrived = (seen - pending) & _ids(row["keys"])
                    self.journal.settle_transfer(
                        row["run"],
                        sorted(k for k in row["keys"] if k.rsplit("/", 1)[-1] in arrived),
                    )
                elif received_ids & _ids(row["keys"]) and not cluster.preview:
                    skipped = _ids(
                        {*task.skipped, *(k for node in task.nodes for k in node.skipped)}
                    )
                    arrived = received_ids - skipped - pending
                    unaccounted = _ids(row["keys"]) - received_ids - skipped - pending
                    self.journal.settle_transfer(
                        row["run"],
                        sorted(k for k in row["keys"] if k.rsplit("/", 1)[-1] in arrived),
                        TASK_REPORT,
                        sorted(k for k in row["keys"] if k.rsplit("/", 1)[-1] in unaccounted),
                    )
                elif received and cluster.preview:
                    skipped = _ids(
                        {*task.skipped, *(k for node in task.nodes for k in node.skipped)}
                    )
                    accounted = received_ids | skipped | pending
                    if _ids(row["keys"]) <= accounted:
                        self.journal.settle_transfer(
                            row["run"],
                            sorted(k for k in row["keys"] if k.rsplit("/", 1)[-1] in received_ids),
                        )
                elif (
                    not cluster.preview
                    and task.nodes
                    and all(node.scheduled_ids is not None for node in task.nodes)
                ):
                    skipped = _ids(
                        {*task.skipped, *(k for node in task.nodes for k in node.skipped)}
                    )
                    arrived = _ids(row["keys"]) - pending - skipped
                    self.journal.settle_transfer(
                        row["run"],
                        sorted(k for k in row["keys"] if k.rsplit("/", 1)[-1] in arrived),
                        TASK_REPORT,
                    )
                # A cancelled or failed task that lists no remaining buckets leaves the row
                # open: treating the selection as received or absent would be a guess.

    def _settle(self, destination):
        task = self.env.transfer()
        self._record(task, destination)
        return task

    def _hold_unarrived(self, plan, task, ids, message):
        if task.status not in TERMINAL:
            raise _affected_error(
                Code.OVERLAP,
                "A transfer that includes these buckets is still running or its status cannot "
                "be read. Wait and refresh: " + message,
                ids,
            )
        if not plan["destination"].get("inventory_available"):
            raise _affected_error(
                Code.OVERLAP,
                message + " Destination inventory is unavailable; inspect it before retrying.",
                ids,
            )
        shown = ids & {
            b.id for b in self.env.destination_cluster(fresh=True).buckets if b.registered
        }
        if shown:
            raise _affected_error(
                Code.INVENTORY_CHANGED,
                "The destination inventory changed while planning. Refresh the preview: "
                + ", ".join(sorted(shown))
                + ".",
                shown,
            )
        plan["warnings"].append(
            message + " They are not on the destination, so they may be copied."
        )

    def _overlap(self, plan, task):
        destination = plan["destination"]["id"]
        selected = {b["id"] for b in plan["selected"]}
        for prior in self.journal.transfers(destination):
            held = prior["received"] if prior["closed"] else prior["keys"]
            affected = selected.intersection(_ids(held))
            if affected:
                # An open row whose start was never acknowledged is an uncertain start: the
                # window is held with that reason until it is stale and the appliance shows
                # no task for it.
                uncertain = not prior["closed"] and not prior.get("acknowledged")
                groups = [b for b in plan["selected"] if b["id"] in affected]
                details = "; ".join(
                    f"{b['id']} ({_day(b['start'])} to {_day(b['end'])} UTC; source: {b['node']})"
                    for b in groups
                )
                running = (
                    task is not None
                    and task.status == "running"
                    and (not prior.get("task_id") or task.id == prior["task_id"])
                )
                inspected = plan["destination"].get("inventory_available") or (
                    self.attribution and not plan["destination"]["version"].startswith("9.1.1")
                )
                reason = (
                    "A transfer is running for these buckets. Wait for it to finish and refresh: "
                    if running
                    else "An earlier record says these buckets arrived, but the destination "
                    "inventory does not show them. Inspect the destination before retrying: "
                    if prior["closed"] and inspected
                    else "An earlier task reported these buckets, but destination inventory "
                    "is unavailable. Inspect the destination before retrying: "
                    if prior["closed"]
                    else "An earlier transfer has not been reconciled for these buckets: "
                )
                if running and task.nodes:
                    details += ". Destination nodes: " + ", ".join(n.node for n in task.nodes)
                raise _affected_error(
                    Code.UNCERTAIN_START
                    if uncertain
                    else Code.INCOMPLETE
                    if prior["closed"] and inspected
                    else Code.OVERLAP,
                    reason + details + ".",
                    affected,
                )
        if task is not None and task.status == "running":
            task_ids = _ids(
                {
                    *task.bucket_keys,
                    *(
                        bucket
                        for node in task.nodes
                        for group in (node.scheduled_ids or {}).values()
                        for bucket in group
                    ),
                }
            )
            if task_ids:
                affected = selected & task_ids
            elif task.window is not None:
                affected = {
                    b["id"]
                    for b in plan["selected"]
                    if Window(b["start"], b["end"] + 1).overlaps(task.window)
                }
            else:
                affected = set()
            if affected:
                details = "; ".join(
                    f"{b['id']} ({_day(b['start'])} to {_day(b['end'])} UTC; source: {b['node']})"
                    for b in plan["selected"]
                    if b["id"] in affected
                )
                nodes = ", ".join(node.node for node in task.nodes) or "not reported"
                raise _affected_error(
                    Code.OVERLAP,
                    f"A transfer is running for {len(affected)} selected buckets: {details}. "
                    f"Destination task nodes: {nodes}. Wait for it to finish and refresh.",
                    affected,
                )
        # Include transfers started outside this journal, wherever the appliance exposes them.
        ours = {row["task_id"] for row in self.journal.transfers(destination) if row.get("task_id")}
        for prior in self.env.history():
            if prior.destination != destination or (prior.task_id and prior.task_id in ours):
                continue
            historical = selected.intersection(prior.bucket_ids)
            if historical:
                groups = [b for b in plan["selected"] if b["id"] in historical]
                details = "; ".join(
                    f"{b['id']} ({_day(b['start'])} to {_day(b['end'])} UTC; source: {b['node']})"
                    for b in groups
                )
                self._hold_unarrived(
                    plan,
                    task,
                    historical,
                    "An earlier transfer listed these buckets, but they are not on the "
                    "destination: " + details + ".",
                )
            if not prior.bucket_ids and (prior.complete or prior.partial):
                # A finished transfer with no received list (one started from the product UI,
                # say), or one that lists only what it skipped, covered whole buckets in its
                # window; anything of ours overlapping that window may already be there.
                crossing = [
                    b
                    for b in plan["selected"]
                    if Window(b["start"], b["end"] + 1).overlaps(prior.window)
                ]
                if crossing:
                    when = (
                        _day(prior.created_at_ms) if prior.created_at_ms is not None else "unknown"
                    )
                    groups = ", ".join(f"{b['node']}/{b['id']}" for b in crossing)
                    task_label = re.sub(r"[^A-Za-z0-9._:-]", "_", prior.task_id or "")[:64]
                    detail = (
                        f"Earlier task {task_label or 'unknown'} ran {when}; "
                        f"its window was {_day(prior.window.start)} to "
                        f"{_day(prior.window.end - 1)} UTC. Selected groups crossing "
                        f"that window: {groups}."
                    )
                    self._hold_unarrived(plan, task, {b["id"] for b in crossing}, detail)

    def _source_ingest(self) -> dict:
        try:
            return self.env.source_growth()
        except MigrationError:
            pass
        return {"bytes": None, "nodes": [], "window_seconds": None}

    @safe_boundary
    def discover(self):
        source, destination = self.env.discover()
        return {
            "source": asdict(source),
            "destination": asdict(destination),
            "partition_diff": partition_diff(source, destination),
        }

    def _known_placements(self, destination, ids):
        placement = getattr(self.env, "bucket_placement", None)
        if placement is not None:
            return {bucket_id: placement(bucket_id, destination) for bucket_id in ids}
        nodes = [node.id for node in destination.nodes]
        if len(nodes) == 1:
            return dict.fromkeys(ids, nodes[0])
        known = {}
        for row in self.journal.transfers(destination.id):
            previous = self.journal.load(row["run"])
            if [node["id"] for node in previous["plan"]["destination"]["nodes"]] != nodes:
                continue
            for node, assigned in previous.get("assignment", {}).items():
                for bucket_id in ids.intersection(assigned):
                    if bucket_id not in known:
                        known[bucket_id] = node
                    elif known[bucket_id] != node:
                        known[bucket_id] = None
        return known

    def _plan_with_existing(self, source, destination, window):
        selected = [b for b in source.buckets if b.selected(window)]
        ids = {b.id for b in selected}
        require(
            len(ids) == len(selected),
            Code.COLLISION,
            "Source groups have duplicate identities. "
            "Inspect the source inventory before retrying.",
        )
        existing = []
        for row in self.journal.transfers(destination.id):
            if row["source"] == source.id and row["closed"] and ids & _ids(row["received"]):
                earlier = self.journal.load(row["run"])
                if not self._nodes_comparable(destination, earlier["plan"]["destination"]):
                    continue
                if len(earlier["plan"]["destination"]["nodes"]) != len(destination.nodes):
                    held = ids & _ids(row["received"])
                    details = "; ".join(
                        f"{b.id} ({_day(b.start)} to {_day(b.end)} UTC; destination: "
                        + (
                            ", ".join(sorted({d.node for d in destination.buckets if d.id == b.id}))
                            or "not observed"
                        )
                        + ")"
                        for b in selected
                        if b.id in held
                    )
                    raise _affected_error(
                        Code.TOPOLOGY,
                        f"The destination node count changed since {len(held)} buckets "
                        "were received; "
                        "resubmitting them could create duplicates: " + details + ". "
                        "Restore the original node count before continuing.",
                        held,
                    )
        inspect = getattr(self.env, "inspect_destination_buckets", None)
        if not destination.preview and (destination.inventory_available or self.attribution):
            by_id = {b.id: b for b in selected}
            if inspect is not None:
                destination = _inspect_destination(inspect, destination, by_id)
            existing = [b for b in destination.buckets if b.id in ids]
            _check_destination_copies(existing, by_id)
            placements = self._known_placements(destination, ids)
            duplicates = _check_duplicate_destination(
                existing,
                by_id,
                ids,
                placements,
            )
            received = {b.id for b in existing}
        elif destination.preview:
            received = (
                _ids(
                    {
                        key
                        for row in self.journal.transfers(destination.id)
                        if row["closed"] and row["source"] == source.id
                        for key in row["received"]
                    }
                )
                & ids
            )
        else:
            received = set()
        filtered = replace(source, buckets=tuple(b for b in source.buckets if b.id not in received))
        if selected and len(received) == len(selected):
            from .planning import capacity

            empty_destination = replace(
                destination, buckets=tuple(b for b in destination.buckets if b.id not in received)
            )
            plan = build_plan(source, empty_destination, window)
            plan["selected"] = []
            plan["capacity"] = capacity(destination, ())
            plan["warnings"].append(
                "All selected buckets are already present; nothing remains to copy."
            )
        else:
            plan = build_plan(filtered, destination, window)
        plan["already_received"] = [asdict(b) for b in selected if b.id in received]
        if received:
            dates = sorted(
                {f"{_day(b.start)} to {_day(b.end)} UTC" for b in selected if b.id in received}
            )
            unknown_placement = (
                not destination.preview
                and (destination.inventory_available or self.attribution)
                and any(placements.get(b.id) is None for b in existing)
                and bool(plan["selected"])
            )
            suffix = (
                f"; {len(plan['selected'])} buckets remain to be copied."
                if unknown_placement
                else f" and will be left as they are; {len(plan['selected'])} will be copied."
            )
            plan["warnings"].append(
                f"{len(received)} buckets ({'; '.join(dates)}) are already on the destination"
                + suffix
            )
        if not destination.preview and (destination.inventory_available or self.attribution):
            if duplicates:
                plan["warnings"].append("Multiple destination copies: " + "; ".join(duplicates))
            unknown = sorted({b.id for b in existing if placements.get(b.id) is None})
            if unknown and plan["selected"]:
                plan["warnings"].append(
                    "The destination node for a new copy could not be checked for "
                    + ", ".join(unknown)
                    + ". A copy may be created on another node; inspect placement before starting."
                )
        return plan

    @staticmethod
    def _held_note(task, row, preview):
        received = _ids({*task.completed, *(k for n in task.nodes for k in n.completed)})
        skipped = _ids({*task.skipped, *(k for n in task.nodes for k in n.skipped)})
        pending = {
            bucket
            for node in task.nodes
            for group in (node.scheduled_ids or {}).values()
            for bucket in group
        }
        missing = _ids(row["keys"]) - received - skipped - pending
        if received:
            return (
                f"The task listed {len(received)} received bucket(s), but {len(missing)} "
                "selected bucket(s) are neither received, skipped nor still to do, so what "
                "arrived cannot be told. The selection stays held rather than guessed."
            )
        if preview:
            return (
                "The task finished without a list of the buckets it received, and 9.1.1 has "
                "no bucket store to read, so what arrived cannot be told. The selection "
                "stays held rather than guessed."
            )
        return (
            "The task finished but reports no list of buckets still to do, and the "
            "destination's bucket store was not read, so what arrived cannot be told. The "
            "selection stays held; connect destination SSH and check status again."
        )

    @staticmethod
    def _baseline(series, elapsed, value):
        """Record a poll and return the newest earlier sample at least a window old, if any."""
        if series and elapsed < series[-1][0]:
            series.clear()
        if not series or elapsed > series[-1][0]:
            series.append((elapsed, value))
        while len(series) > 1 and series[1][0] <= elapsed - RATE_WINDOW_SECONDS:
            series.pop(0)
        first = series[0]
        return first if elapsed - first[0] >= RATE_WINDOW_SECONDS else None

    def _nodes_observed(self, destination):
        return self.attribution or destination.inventory_available

    def _nodes_comparable(self, destination, earlier):
        return self._nodes_observed(destination) and bool(
            self.attribution or earlier.get("inventory_available")
        )

    def _estimate(self, plan, observed=True, listed_nodes=0):
        sizes = sum(b["size"] for b in plan["selected"])
        count = len(plan["selected"])
        assumed = not observed
        streams = len(plan["destination"]["nodes"])
        if assumed and listed_nodes:
            streams = listed_nodes
        observed = None
        for run in reversed(self.journal.runs()):
            if (
                run["plan"]["source"]["identity"] == plan["source"]["identity"]
                and run["plan"]["destination"]["identity"] == plan["destination"]["identity"]
                and len(run["plan"]["destination"]["nodes"]) == streams
                and run.get("measured_rate_bytes_per_second", 0) > 0
            ):
                observed = run["measured_rate_bytes_per_second"]
                break
        times = list(getattr(self.env, "_listing_seconds", {}).values())
        overhead = sum(times) / len(times) if times else 18.0
        if observed:
            seconds = sizes / observed
        else:
            seconds = count * overhead / streams + sizes / (streams * 4_000_000)
        return {
            "low_seconds": seconds / 2,
            "high_seconds": seconds * 2,
            "selected_bytes": sizes,
            "bucket_count": count,
            "destination_nodes": streams,
            "destination_nodes_assumed": assumed,
            "rate_bytes_per_second": observed if observed else 4_000_000,
            "rate_basis": "measured overall" if observed else "assumed per destination node",
            "listing_seconds": overhead if not observed else None,
            "listing_basis": "measured"
            if times and not observed
            else "assumed"
            if not observed
            else None,
        }

    @safe_boundary
    def plan(self, window, policy=None):
        with self._exclusive():
            discover = getattr(self.env, "discover_window", None)
            source, destination = discover(window) if discover else self.env.discover()
            task = self._settle(destination.id)
            if destination.inventory_available:
                destination = self.env.destination_cluster(fresh=True)
            plan = self._plan_with_existing(source, destination, window)
            plan["estimate"] = self._estimate(plan, self._nodes_observed(destination))
            plan["source_ingest"] = self._source_ingest()
            growth = plan["source_ingest"]
            if growth["bytes"] is None:
                plan["warnings"].append("Source growth could not be observed; confirm cutover.")
            elif growth["bytes"]:
                plan["warnings"].append(
                    f"Active buckets on {', '.join(growth['nodes'])} grew by "
                    f"{growth['bytes']:,} bytes in {growth['window_seconds']} seconds."
                )
            plan["warnings"] += [
                f"{host}: the bucket listing returned text but no recognizable groups. "
                "Check the node by hand before trusting this plan."
                for host in sorted(getattr(self.env, "listing_warnings", ()))
            ]
            plan["partial_days"] = partial_days(source, plan)
            if plan["already_received"]:
                days = day_window(window)
                plan["partial_days"] = [_day(day) for day in range(days.start, days.end, DAY)]
            requested = day_window(window)
            plan["open_bucket_days"] = sorted(
                {
                    _day(day)
                    for b in source.buckets
                    if b.status == "active"
                    and b.bounds_proven
                    and b.start is not None
                    and b.end is not None
                    for day in range(requested.start, requested.end, DAY)
                    if b.start < day + DAY and b.end >= day
                }
            )
            if plan["open_bucket_days"]:
                plan["warnings"].append(
                    "Requested days shared with open groups: "
                    + ", ".join(plan["open_bucket_days"])
                    + "."
                )
            self._overlap(plan, task)
            run = {
                "schema_version": 2,
                "id": str(uuid4()),
                "created_at": self.clock.now(),
                "plan": plan,
                "plan_hash": digest(plan),
                "active_ack": False,
                "query_policy": asdict(policy or Policy()),
                "verification": {"outcome": Verification.PENDING.value, "complete": False},
            }
            self._audit(run, "plan_previewed")
            return run

    def _resume_plan(self, root):
        original = root["plan"]
        window = Window(**original["window"])
        invalidate = getattr(self.env, "invalidate_inventory", None)
        if invalidate is not None:
            invalidate()
        discover = getattr(self.env, "discover_window", None)
        source, destination = discover(window) if discover else self.env.discover()
        require(
            source.id == original["source"]["id"]
            and source.identity == original["source"]["identity"]
            and destination.id == original["destination"]["id"]
            and destination.identity == original["destination"]["identity"],
            Code.TOPOLOGY,
        )
        comparable = self._nodes_comparable(destination, original["destination"])
        if comparable:
            require(
                len(destination.nodes) == len(original["destination"]["nodes"]),
                Code.TOPOLOGY,
                "The destination node count changed; placement may differ and a resume could "
                "duplicate buckets. Restore the original node count before continuing.",
            )
        task = self._settle(destination.id)
        require(task.status in TERMINAL, Code.BUSY)
        if destination.inventory_available:
            destination = self.env.destination_cluster(fresh=True)
            require(
                destination.id == original["destination"]["id"]
                and destination.identity == original["destination"]["identity"]
                and (
                    not comparable
                    or len(destination.nodes) == len(original["destination"]["nodes"])
                ),
                Code.TOPOLOGY,
                "The destination topology changed while checking the remainder.",
            )
        rows = self.journal.transfers(destination.id)
        root_row = next((r for r in rows if r["run"] == root["id"]), None)
        require(
            root_row is not None and root_row["closed"],
            Code.INCOMPLETE,
            "The earlier transfer has no settled receipt record; inspect it before resuming.",
        )
        previously_present = {b["id"] for b in original.get("already_received", [])}
        intended = {
            b["id"]: b for b in (*original["selected"], *original.get("already_received", []))
        }
        selected_now = [b for b in source.buckets if b.selected(window)]
        current = {b.id: b for b in selected_now}
        require(
            len(current) == len(selected_now),
            Code.COLLISION,
            "Source groups have duplicate identities. Inspect them before resuming.",
        )
        require(
            set(current) <= intended.keys(),
            Code.INVENTORY_CHANGED,
            "New source buckets now overlap these dates. Review a new plan before continuing.",
        )
        journal_only = destination.preview or (
            not destination.inventory_available and not self.attribution
        )
        if journal_only:
            if not destination.preview:
                require(
                    root_row.get("basis") == TASK_REPORT,
                    Code.INCOMPLETE,
                    "The destination could not be inspected and the earlier task report "
                    "does not account for what arrived.",
                )
            received = (
                _ids(
                    {
                        k
                        for row in rows
                        if row["closed"] and row["source"] == source.id
                        for k in row["received"]
                    }
                )
                & intended.keys()
            )
        else:
            inspect = getattr(self.env, "inspect_destination_buckets", None)
            if inspect is not None:
                destination = _inspect_destination(inspect, destination, intended)
            else:
                require(
                    self.attribution or destination.inventory_available,
                    Code.INCOMPLETE,
                    "Destination inventory is required to resume safely.",
                )
            existing = [b for b in destination.buckets if b.id in intended]
            _check_destination_copies(existing, intended)
            duplicates = _check_duplicate_destination(
                existing,
                intended,
                set(intended),
                self._known_placements(destination, set(intended)),
            )
            received = {b.id for b in existing}
        require(
            previously_present <= received,
            Code.INCOMPLETE,
            "Buckets present before this run are no longer accounted for; inspect the destination.",
        )
        missing = set(intended) - received
        require(missing, Code.STATE, "All selected buckets are already present; verify the run.")
        require(
            missing <= current.keys(),
            Code.INVENTORY_CHANGED,
            "A source bucket needed to finish this run has been retired.",
        )
        require(
            all(asdict(current[i]) == intended[i] for i in missing),
            Code.INVENTORY_CHANGED,
            "A source bucket needed for this run has changed.",
        )
        filtered = replace(source, buckets=tuple(b for b in source.buckets if b.id not in received))
        plan_destination = replace(destination, buckets=()) if journal_only else destination
        plan = build_plan(filtered, plan_destination, window)
        plan["already_received"] = [intended[i] for i in sorted(received)]
        if not journal_only and duplicates:
            plan["warnings"].append("Multiple destination copies: " + "; ".join(duplicates))
        plan["original_selected"] = original["selected"]
        plan["original_coverage"] = original["coverage"]
        if journal_only and not destination.preview:
            plan["re_requested_unconfirmed"] = True
            plan["warnings"].append(
                "The destination could not be inspected. Remaining buckets will be requested "
                "again; some may already be present."
            )
        if not comparable:
            plan["node_count_unchecked"] = True
            plan["warnings"].append(
                "The destination's node count could not be checked. If nodes were added or "
                "removed since the first run, history may be copied a second time. Confirm "
                "the node count is unchanged before continuing."
            )
        plan["estimate"] = self._estimate(plan, self._nodes_observed(destination), len(task.nodes))
        self._overlap(plan, task)
        return source, destination, plan

    @safe_boundary
    def resume(self, run_id):
        """Preview a linked remainder; run() starts it after the usual fresh checks."""
        with self._exclusive():
            previous = self.journal.load(run_id)
            root = self.journal.load(previous.get("resume_root", run_id))
            _, _, plan = self._resume_plan(root)
            run = {
                "schema_version": 2,
                "id": str(uuid4()),
                "created_at": self.clock.now(),
                "plan": plan,
                "plan_hash": digest(plan),
                "active_ack": bool(root.get("active_ack")),
                "query_policy": root["query_policy"],
                "resume_root": root["id"],
                "resume_parent": run_id,
                "verification": {"outcome": Verification.PENDING.value, "complete": False},
            }
            self._audit(run, "resume_previewed")
            return run

    def _counts(self, policy, scope, window, keys):
        return stable_counts(self.env, self.clock, policy, scope, window, keys)

    def _ingest(self, policy, baseline):
        # Only arrivals beyond the destination's pre-transfer baseline count as new ingest.
        counts = self._counts(policy, "ingest", EVERYTHING, ())
        require(
            counts.identity_complete and baseline.identity_complete,
            Code.INCOMPLETE,
            "Destination ingest counts could not identify every event, so new arrivals "
            "cannot be separated from the baseline. Retry once ingestion settles.",
        )
        identities = Counter(dict(counts.identities)) - Counter(dict(baseline.identities))
        daily = Counter()
        for identity, count in identities.items():
            daily[identity[:10]] += count
        return Counts(
            counts.complete,
            tuple(sorted(daily.items())),
            tuple(sorted(identities.items())),
            "ingest",
        )

    @safe_boundary
    def run(self, run_id, *, backup_ack=False, active_ack=""):
        # Retained as an ignored keyword for callers of older versions.
        with self._exclusive():
            run = self.journal.load(run_id)
            require(run.get("schema_version") == 2, Code.SCHEMA)
            require(not run["plan"]["active_skipped"] or active_ack == ACTIVE_ACK, Code.ACK)
            require(
                bool(run["plan"]["selected"]),
                Code.STATE,
                "All selected buckets are already present; there is nothing to copy.",
            )
            require(
                all(
                    t["run"] != run_id
                    for t in self.journal.transfers(run["plan"]["destination"]["id"])
                ),
                Code.STATE,
            )
            # Authorization always rechecks the appliances, even after a cached preview.
            cache = getattr(self.env, "_inventory_cache", None)
            if cache is not None:
                cache.clear()
            invalidate = getattr(self.env, "invalidate_inventory", None)
            if invalidate is not None:
                invalidate()
            window = Window(**run["plan"]["window"])
            if run.get("resume_root"):
                source, destination, fresh = self._resume_plan(
                    self.journal.load(run["resume_root"])
                )
            else:
                discover = getattr(self.env, "discover_window", None)
                source, destination = discover(window) if discover else self.env.discover()
                fresh = self._plan_with_existing(source, destination, window)
            require(
                fresh["source"] == run["plan"]["source"]
                and fresh["destination"] == run["plan"]["destination"]
                and fresh["selected"] == run["plan"]["selected"]
                and fresh.get("already_received", []) == run["plan"].get("already_received", []),
                Code.INVENTORY_CHANGED,
            )
            task = self._settle(destination.id)
            self._overlap(fresh, task)
            require(fresh["capacity"]["fits"], Code.CAPACITY)
            difference = fresh["partition_diff"]
            require(not difference["missing"] and not difference["different"], Code.PARTITIONS)
            require(not difference["missing_fields"], Code.FIELDS)
            require(task.status in TERMINAL, Code.BUSY)
            for node in source.nodes:
                self.env.test_source_credentials(node.id)
            self.env.test_mover()
            policy = Policy(**run["query_policy"])
            coverage = Window(**run["plan"]["coverage"])
            keys = tuple(b["node"] + "/" + b["id"] for b in run["plan"]["selected"])
            if self.attribution:
                run["source_by_bucket"] = {
                    key: asdict(self._counts(policy, "source", coverage, (key,))) for key in keys
                }
                run["baseline"] = asdict(self._counts(policy, "baseline", EVERYTHING, ()))
            else:
                # Live counts are per day for the whole cluster, so the evidence is the
                # requested UTC days: source and destination baseline over the same days.
                days = day_window(Window(**run["plan"]["window"]))
                run["count_window"] = asdict(days)
                run["source_by_bucket"] = {}
                run["source_counts"] = asdict(self._counts(policy, "source", days, keys))
                try:
                    run["baseline"] = asdict(self._counts(policy, "baseline", days, ()))
                except MigrationError as error:
                    # 9.1.1: no qualified count route. The transfer can still run; the
                    # verification will report incomplete evidence.
                    if error.code != Code.UNSUPPORTED:
                        raise
                    run["baseline"] = None
                run["partial_days"] = partial_days(source, fresh)
                if fresh.get("already_received") and not run.get("resume_root"):
                    days = day_window(window)
                    run["partial_days"] = [_day(day) for day in range(days.start, days.end, DAY)]
                if run.get("resume_root"):
                    root = self.journal.load(run["resume_root"])
                    for key in ("baseline", "source_counts", "count_window", "partial_days"):
                        if key in root:
                            run[key] = root[key]
            run["active_ack"] = active_ack == ACTIVE_ACK
            run["capacity_at_start"] = fresh["capacity"]
            self._audit(run, "transfer_authorized")
            # Protect against duplicate sends even if the POST response is lost. This is an
            # overlap record, not a locally maintained task state or a destination-wide lock.
            self.journal.record_transfer(run)
            self._audit(run, "api_start_requested")
            try:
                task = self.env.start(Window(**fresh["window"]), keys)
            except Rejection:
                self.journal.release_transfer(run_id)
                self._audit(run, "api_start_rejected")
                raise
            except Exception:
                self._audit(run, "api_start_response_unavailable")
                raise MigrationError(Code.UNCERTAIN) from None
            try:
                self.journal.note_transfer_task(run_id, task.id or "")
                self._observe_assignment(run, task)
                self._audit(run, "api_start_response", asdict(task))
            except MigrationError:
                raise MigrationError(Code.UNCERTAIN) from None
            warning = self._capacity_assignment(task, fresh, run.get("assignment"))
            progress = self._progress(task, run)
            progress["capacity_warning"] = warning
            return {
                "run_id": run_id,
                "task": progress,
                "capacity_warning": warning,
                "verification": run["verification"],
            }

    def _observe_assignment(self, run, task):
        if run.get("assignment") or not task.nodes:
            return
        assignment = {}
        for node in task.nodes:
            if node.scheduled_ids is None or node.total_buckets is None:
                return
            if set(node.scheduled_ids) != set(node.total_buckets) or any(
                len(ids) != node.total_buckets[source] for source, ids in node.scheduled_ids.items()
            ):
                return
            assignment[node.node] = [item for ids in node.scheduled_ids.values() for item in ids]
        ids = [item for items in assignment.values() for item in items]
        planned = {b["id"] for b in run["plan"]["selected"]}
        if len(ids) == len(set(ids)) and set(ids) == planned:
            run["assignment"] = assignment
            self._audit(run, "bucket_assignment_observed")

    def _capacity_assignment(self, task, plan, assignment=None):
        """Check the remaining assigned buckets when the API reports their identities."""
        sizes = {bucket["id"]: bucket["size"] for bucket in plan["selected"]}
        budgets = {node["id"]: node for node in plan["capacity"]["nodes"]}
        result = []
        for node in task.nodes:
            if node.node not in budgets:
                continue
            if assignment and node.node in assignment:
                ids = assignment[node.node]
            elif node.scheduled_ids is not None:
                ids = [bucket for group in node.scheduled_ids.values() for bucket in group]
            else:
                continue
            if len(set(ids)) != len(ids) or any(bucket not in sizes for bucket in ids):
                continue
            needed = sum(sizes[bucket] for bucket in ids)
            budget = budgets[node.node]
            limits = [budget.get("available_bytes"), budget.get("disk_headroom_bytes")]
            if any(limit is not None and needed > limit for limit in limits):
                result.append({"node": node.node, "scheduled_bytes": needed})
        return result

    def _progress(self, task, run=None):
        result = asdict(task)
        elapsed = task.elapsed_seconds
        rate = (
            task.bytes_transferred / elapsed
            if task.bytes_transferred is not None and elapsed and elapsed > 0
            else None
        )
        result["rate_bytes_per_second"] = rate
        result["eta_seconds"] = (
            max(0, task.total_bytes - task.bytes_transferred) / rate
            if rate and task.total_bytes is not None
            else None
        )
        if (
            result["eta_seconds"] is None
            and task.status == "running"
            and task.elapsed_seconds is not None
            and task.progress is not None
            and task.progress > 0
        ):
            result["eta_seconds"] = task.elapsed_seconds * (100 - task.progress) / task.progress
        if result["eta_seconds"] is None and task.status == "running" and task.nodes:
            # Every node must have an estimate before a task-wide estimate is meaningful.
            estimates = [
                n.elapsed_seconds * (100 - n.progress) / n.progress
                for n in task.nodes
                if n.elapsed_seconds is not None and n.progress is not None and n.progress > 0
            ]
            if len(estimates) == len(task.nodes):
                result["eta_seconds"] = max(estimates)
        if task.status == "completed":
            result["eta_seconds"] = 0
        elif task.status != "running":
            result["eta_seconds"] = None
        for node, output in zip(task.nodes, result["nodes"], strict=True):
            elapsed_node = node.elapsed_seconds or task.elapsed_seconds
            rate_node = (
                node.bytes_transferred / elapsed_node
                if node.bytes_transferred and elapsed_node and elapsed_node > 0
                else None
            )
            output["rate_bytes_per_second"] = rate_node
            output["eta_seconds"] = (
                elapsed_node * (100 - node.progress) / node.progress
                if node.status == "running" and elapsed_node and node.progress and node.progress > 0
                else None
            )
            if (
                node.status == "running"
                and node.scheduled_buckets is not None
                and node.total_buckets
            ):
                count = sum(node.total_buckets.values())
                pending_count = sum(node.scheduled_buckets.values())
                if pending_count <= count:
                    output["buckets_done"] = count - pending_count
                    output["buckets_total"] = count
        assignment = run.get("assignment") if run else None
        if (
            assignment
            and task.status == "running"
            and all(node.status == "running" for node in task.nodes)
        ):
            sizes = {b["id"]: b["size"] for b in run["plan"]["selected"]}
            node_results = []
            for node, output in zip(task.nodes, result["nodes"], strict=True):
                if node.node not in assignment or node.scheduled_ids is None:
                    break
                remaining = {item for ids in node.scheduled_ids.values() for item in ids}
                assigned = set(assignment[node.node])
                if not remaining <= assigned:
                    break
                done = assigned - remaining
                copied = sum(sizes[item] for item in done)
                left = sum(sizes[item] for item in remaining)
                elapsed = node.elapsed_seconds or task.elapsed_seconds
                node_rate = copied / elapsed if copied and elapsed and elapsed > 0 else None
                output.update(
                    {
                        "buckets_done": len(done),
                        "buckets_total": len(assigned),
                        "bytes_transferred": copied,
                        "total_bytes": copied + left,
                        "rate_bytes_per_second": node_rate,
                        "eta_seconds": left / node_rate
                        if node_rate and node.status == "running"
                        else None,
                    }
                )
                node_results.append(output)
            if len(node_results) == len(task.nodes):
                copied = sum(n["bytes_transferred"] for n in node_results)
                remaining = sum(n["total_bytes"] - n["bytes_transferred"] for n in node_results)
                elapsed = task.elapsed_seconds
                rate = copied / elapsed if copied and elapsed and elapsed > 0 else None
                result.update(
                    {
                        "bytes_transferred": copied,
                        "buckets_done": sum(n["buckets_done"] for n in node_results),
                        "buckets_total": sum(n["buckets_total"] for n in node_results),
                        "rate_bytes_per_second": rate,
                    }
                )
                if task.status == "running" and all(
                    n["eta_seconds"] is not None for n in node_results
                ):
                    result["eta_seconds"] = max(n["eta_seconds"] for n in node_results)
                result["total_bytes"] = copied + remaining
        result["observation"] = (
            "destination API; estimates derive from API counters and elapsed time"
        )
        return result

    def _untied_status(self, reason, hint):
        """No run was identified: say so, and list destination tasks as not tied to any run."""
        lister = getattr(self.env, "destination_tasks", None)
        tasks = lister() if lister is not None else [self.env.transfer()]
        result = self._progress(Transfer("", "idle"))
        result["run"] = None
        result["no_run"] = {"reason": reason, "hint": hint}
        result["destination_tasks"] = {
            "note": "Destination tasks, not tied to any run of this tool",
            "tasks": [
                {
                    "id": t.id,
                    "status": t.status,
                    "window": asdict(t.window) if t.window else None,
                    "progress": t.progress,
                }
                for t in tasks
                if t.id
            ],
        }
        return result

    def _remainder_summary(self, run, destination, task):
        root_id = run.get("resume_root", run["id"])
        root = self.journal.load(root_id)
        linked = {
            r["id"]
            for r in self.journal.runs()
            if r["id"] == root_id or r.get("resume_root") == root_id
        }
        rows = [r for r in self.journal.transfers(destination) if r["run"] in linked]
        if not rows or any(not row["closed"] for row in rows):
            return None
        received = _ids({key for row in rows for key in row["received"]})
        received.update(b["id"] for b in root["plan"].get("already_received", []))
        selected = [*root["plan"].get("already_received", []), *root["plan"]["selected"]]
        remaining = [b for b in selected if b["id"] not in received]
        plan = {**root["plan"], "selected": remaining}
        observed = self.attribution or bool(root["plan"]["destination"].get("inventory_available"))
        try:
            estimate = self._estimate(
                plan,
                observed,
                len(task.nodes) or len(root.get("assignment") or run.get("assignment") or {}),
            )
        except Exception:
            estimate = None  # A time estimate must never block status or a transfer.
        return {
            "already_received": [b for b in selected if b["id"] in received],
            "selected": remaining,
            "estimate": estimate,
            "re_requested_unconfirmed": any(row.get("basis") == TASK_REPORT for row in rows),
            "node_count_unchecked": not observed,
        }

    @safe_boundary
    def status(self, run_id=None):
        with self._exclusive():
            if self.journal is None:
                return self._untied_status(
                    "no_journal",
                    "No run was identified because no journal was given; pass --journal.",
                )
            runs = self.journal.runs()
            if run_id is None:
                run_id = runs[-1]["id"] if runs else None
            run = next((r for r in runs if r["id"] == run_id), None)
            if run is None:
                return self._untied_status(
                    "run_not_found", "No run was identified in this journal; check the run ID."
                )
            destination = run["plan"]["destination"]["id"]
            rows = self.journal.transfers(destination)
            row = next((r for r in rows if r["run"] == run_id), None)
            if row is None:
                return self._progress(Transfer("", "idle", destination=destination))
            task_id = row.get("task_id") or ""
            window = Window(**row["window"])
            finder = getattr(self.env, "transfer_for", None)
            if finder is not None:
                try:
                    attempted_ms = int(
                        datetime.fromisoformat(row["attempted_at"]).timestamp() * 1000
                    )
                except (KeyError, TypeError, ValueError):
                    attempted_ms = None
                if attempted_ms is not None:
                    attempted_ms -= START_CLOCK_SLACK_MS
                task = finder(task_id, window=window, attempted_at_ms=attempted_ms)
            else:
                task = self.env.transfer()
            others = {r["task_id"] for r in rows if r["run"] != run_id and r.get("task_id")}
            found = (
                bool(task.id)
                and task.destination == destination
                and task.id not in others
                and (task.id == task_id if task_id else finder is not None or task.window == window)
            )
            if not found:
                result = self._progress(Transfer("", "idle", destination=destination))
                if not row.get("acknowledged") and not row["closed"]:
                    result["start_outcome"] = "unknown"
                    try:
                        hold = datetime.fromisoformat(row["attempted_at"]) + timedelta(
                            seconds=UNCERTAIN_RELEASE_SECONDS
                        )
                    except (KeyError, TypeError, ValueError):
                        hold = None
                    if hold is not None:
                        result["hold_until"] = hold.isoformat()
                    when = f" until {hold.isoformat()}" if hold else " for 15 minutes"
                    result["start_hint"] = (
                        "The start's outcome is unknown and no task has appeared yet. Keep "
                        "checking status. If none appears, this window stays held"
                        f"{when} from the start attempt; after that, plan and copy again."
                    )
                return result
            if not task_id:
                self.journal.note_transfer_task(run_id, task.id)
            self._record(task, destination)
            self._observe_assignment(run, task)
            result = self._progress(task, run)
            settled = next(
                (r for r in self.journal.transfers(destination) if r["run"] == run_id), row
            )
            if task.status in TERMINAL - {"idle"}:
                if not settled["closed"]:
                    result["settlement"] = "held"
                    result["settlement_note"] = self._held_note(
                        task, settled, run["plan"]["destination"]["version"].startswith("9.1.1")
                    )
                elif settled.get("basis") == TASK_REPORT:
                    result["settlement"] = TASK_REPORT
                    result["settlement_note"] = (
                        "What arrived is taken from the destination's task report, not "
                        "confirmed against the destination's bucket store (no destination SSH)."
                    )
                    if settled.get("unaccounted"):
                        result["settlement_note"] += (
                            " Buckets not accounted for will be requested again because "
                            "the destination could not be inspected."
                        )
                if settled["closed"]:
                    result["remainder"] = self._remainder_summary(run, destination, task)
            elapsed = task.elapsed_seconds
            copied = result.get("bytes_transferred")
            sizes = {b["id"]: b["size"] for b in run["plan"]["selected"]}
            pending_now = {}
            for node in task.nodes:
                if node.status != "running" or node.scheduled_ids is None:
                    continue
                ids = {item for group in node.scheduled_ids.values() for item in group}
                if ids <= sizes.keys():
                    pending_now[node.node] = sorted(ids)
            samples = self._samples.setdefault(run_id, {"pending": [], "rate": []})
            if task.status == "running" and elapsed is not None and pending_now:
                prior = self._baseline(samples["pending"], elapsed, pending_now)
                if prior:
                    interval = elapsed - prior[0]
                    for output in result["nodes"]:
                        name = output["node"]
                        if name not in pending_now or name not in prior[1]:
                            continue
                        before = set(prior[1][name])
                        after = set(pending_now[name])
                        if not after <= before:
                            continue
                        rate = sum(sizes[item] for item in before - after) / interval
                        if rate > 0:
                            output["rate_bytes_per_second"] = rate
                            output["eta_seconds"] = sum(sizes[item] for item in after) / rate
                    active = [n for n in result["nodes"] if pending_now.get(n["node"])]
                    if (
                        active
                        and all(n.node in pending_now for n in task.nodes)
                        and all(n.get("rate_bytes_per_second") for n in active)
                    ):
                        result["rate_bytes_per_second"] = sum(
                            n["rate_bytes_per_second"] for n in active
                        )
                        if all(n.get("eta_seconds") is not None for n in active):
                            result["eta_seconds"] = max(n["eta_seconds"] for n in active)
            if task.status == "running" and elapsed is not None and copied is not None:
                prior = self._baseline(samples["rate"], elapsed, copied)
                if prior and copied > prior[1]:
                    current = (copied - prior[1]) / (elapsed - prior[0])
                    result["rate_bytes_per_second"] = current
                    if result.get("total_bytes") is not None:
                        result["eta_seconds"] = max(0, result["total_bytes"] - copied) / current
            measured = None
            if task.elapsed_seconds is not None and task.elapsed_seconds >= 180:
                if result.get("bytes_transferred"):
                    measured = result["bytes_transferred"] / task.elapsed_seconds
                elif result.get("rate_bytes_per_second"):
                    measured = result["rate_bytes_per_second"]
            stored = run.get("measured_rate_bytes_per_second")
            if measured and (not stored or abs(measured - stored) > stored * 0.1):
                run["measured_rate_bytes_per_second"] = measured
                self.journal.save(run, "rate_observed")
            capacity_plan = {
                **run["plan"],
                "capacity": run.get("capacity_at_start", run["plan"]["capacity"]),
            }
            result["capacity_warning"] = (
                self._capacity_assignment(task, capacity_plan, run.get("assignment"))
                if task.status == "running"
                else []
            )
            return result

    @safe_boundary
    def stop(self, task_id):
        """Explicit API cancellation, including tasks whose POST acknowledgment was lost."""
        with self._exclusive():
            task = self.env.transfer()
            require(task.id == task_id, Code.UNCERTAIN)
            if task.status == "running":
                self.env.cancel()
                task = self.env.transfer()
            self._record(task, task.destination)
            return self._progress(task)

    @safe_boundary
    def verify(self, run_id):
        """Cumulative per destination: everything the API has reported as received so far."""
        with self._exclusive():
            run = self.journal.load(run_id)
            require("baseline" in run and "source_by_bucket" in run, Code.STATE)
            try:
                planned = run["plan"]["destination"]
                require(
                    self.env.destination_topology() == (planned["id"], planned["identity"]),
                    Code.TOPOLOGY,
                )
                task = self._settle(planned["id"])
                require(task.status in TERMINAL | {"running"}, Code.UNCERTAIN)
                require(task.status != "running", Code.BUSY)
                # Re-read after the transfer is terminal; never compare a cached listing.
                destination = self.env.destination_cluster(fresh=True)
                require(
                    destination.id == planned["id"] and destination.identity == planned["identity"],
                    Code.TOPOLOGY,
                )
                if not destination.preview and (
                    destination.inventory_available or self.attribution
                ):
                    selected_for_inspection = {
                        b["id"]: b
                        for b in (
                            *run["plan"].get("original_selected", run["plan"]["selected"]),
                            *run["plan"].get("already_received", []),
                        )
                    }
                    inspect = getattr(self.env, "inspect_destination_buckets", None)
                    if inspect is not None:
                        destination = _inspect_destination(
                            inspect, destination, selected_for_inspection
                        )
                    _check_destination_copies(
                        [b for b in destination.buckets if b.id in selected_for_inspection],
                        selected_for_inspection,
                    )
                bucket_check = None
                if not self.attribution and destination.inventory_available:
                    selected = run["plan"].get("original_selected", run["plan"]["selected"])
                    by_id = {b.id: b for b in destination.buckets}
                    matched, unknown, unmatched = [], [], []
                    for b in selected:
                        seen = by_id.get(b["id"])
                        if seen is None:
                            unmatched.append(b)
                        elif b["messages"] is None or seen.messages is None:
                            unknown.append(b)
                        elif seen.messages == b["messages"]:
                            matched.append(b)
                        else:
                            unmatched.append(b)
                    bucket_check = {
                        "matched_groups": len(matched),
                        "total_groups": len(selected),
                        "matched_events": sum(b["messages"] for b in matched),
                        "total_events": selected_message_count(selected),
                        "unmatched": [b["node"] + "/" + b["id"] for b in unmatched],
                        "unknown": [b["node"] + "/" + b["id"] for b in unknown],
                    }
                try:
                    result = (
                        self._reconcile(run, destination.id)
                        if self.attribution
                        else self._reconcile_days(run, destination.id)
                    )
                except MigrationError as error:
                    result = {
                        "outcome": Verification.INCOMPLETE.value,
                        "complete": False,
                        "reason": error.code.value,
                        **({"detail": error.message} if hasattr(error, "message") else {}),
                    }
                if bucket_check is not None:
                    result["bucket_check"] = bucket_check
                    result["daily_outcome"] = result["outcome"]
                    not_received = set(result.get("buckets", {}).get("not_received", []))
                    if any(key not in not_received for key in bucket_check["unmatched"]):
                        result["outcome"] = Verification.DISCREPANCY.value
                    elif bucket_check["unknown"]:
                        if result["outcome"] != Verification.DISCREPANCY.value:
                            result["outcome"] = Verification.INCOMPLETE.value
                            result["complete"] = False
                    elif (
                        result["outcome"] == Verification.INCOMPLETE.value
                        and result.get("reason") == "no_fully_covered_days"
                        and not result.get("buckets", {}).get("not_received")
                    ):
                        result["outcome"] = Verification.SUCCESS.value
                elif (
                    not self.attribution
                    and result["outcome"] == Verification.SUCCESS.value
                    and result.get("unmeasured_day_count")
                ):
                    result["outcome"] = Verification.INCOMPLETE.value
                    result["complete"] = False
            except MigrationError as error:
                result = {
                    "outcome": Verification.INCOMPLETE.value,
                    "complete": False,
                    "reason": error.code.value,
                    **({"detail": error.message} if hasattr(error, "message") else {}),
                }
            if not self.attribution and not result.get("complete"):
                result.setdefault("expected_count", selected_message_count(run["plan"]["selected"]))
                result.setdefault("actual_count", None)
                if run.get("baseline") is None:
                    result["received_unavailable_reason"] = (
                        "No baseline was recorded, so arrivals cannot be calculated. "
                        "See the daily comparison when available."
                    )
            if result.get("expected_count", 0) is None:
                result["expected_unavailable_reason"] = (
                    "One or more selected groups did not report an event count. "
                    "Check the source inventory."
                )
            result["count_basis"] = COUNT_BASIS
            result["at"] = self.clock.now()
            run["verification"] = result
            self._audit(run, "daily_counts_verified", {"outcome": result["outcome"]})
            return result

    def _reconcile(self, run, destination):
        rows = self.journal.transfers(destination)
        received = {key for row in rows for key in row["received"]}
        require(
            bool(received),
            Code.INCOMPLETE,
            "No selected group has been received by the destination yet.",
        )
        selected = {key for row in rows for key in row["keys"]}
        runs = {row["run"]: self.journal.load(row["run"]) for row in rows}
        source = combine(
            [
                Counts.from_dict(runs[row["run"]]["source_by_bucket"][key])
                for row in rows
                for key in row["received"]
            ],
            "source",
        )
        baseline = Counts.from_dict(runs[rows[0]["run"]]["baseline"])
        policy = Policy(**run["query_policy"])
        actual = self._counts(policy, "destination", EVERYTHING, ())
        ingest = self._ingest(policy, baseline)
        require(
            actual == self._counts(policy, "destination", EVERYTHING, ())
            and ingest == self._ingest(policy, baseline),
            Code.INCOMPLETE,
            "Destination counts changed between two observations, so they are not stable. "
            "Retry once ingestion settles.",
        )
        result = reconcile(source, baseline, actual, ingest)
        result["expected_count"] = source.total
        result["actual_count"] = actual.total - baseline.total - ingest.total
        result["buckets"] = {
            "covered": sorted(received),
            "not_received": sorted(selected - received),
        }
        result["covered_days"] = [day for day, _ in source.daily]
        if result["buckets"]["not_received"]:
            # A selection that did not all land is never a success; a discrepancy stays one.
            if result["outcome"] == Verification.SUCCESS.value:
                result["outcome"] = Verification.INCOMPLETE.value
            result["reason"] = Code.NOT_RECEIVED.value
        return result

    def _reconcile_days(self, run, destination):
        """Live evidence: this run's covered days, source versus destination minus baseline.

        New ingest cannot be attributed from counts alone, so a day shared with an open or
        unselected bucket is marked partial and never decides the outcome; a difference on
        a fully covered day is a discrepancy.
        """
        row = next((r for r in self.journal.transfers(destination) if r["run"] == run["id"]), None)
        require(
            row is not None and row["closed"],
            Code.INCOMPLETE,
            "The destination has no closed transfer recorded for this run.",
        )
        root_id = run.get("resume_root", run["id"])
        linked = {
            r["id"]
            for r in self.journal.runs()
            if r["id"] == root_id or r.get("resume_root") == root_id
        }
        related = [r for r in self.journal.transfers(destination) if r["run"] in linked]
        require(
            all(r["closed"] for r in related),
            Code.INCOMPLETE,
            "A linked transfer has no settled receipt record.",
        )
        received = {key for r in related for key in r["received"]}
        require(
            bool(received),
            Code.INCOMPLETE,
            "No selected group has been received by the destination for this run.",
        )
        require(run.get("baseline") is not None, Code.UNSUPPORTED)
        source = Counts.from_dict(run["source_counts"])
        baseline = Counts.from_dict(run["baseline"])
        policy = Policy(**run["query_policy"])
        days = (
            Window(**run["count_window"])
            if "count_window" in run
            else day_window(Window(**run["plan"]["coverage"]))
        )
        actual = self._counts(policy, "destination", days, ())
        require(
            actual == self._counts(policy, "destination", days, ()),
            Code.INCOMPLETE,
            "Destination daily counts changed between two observations, so they are not "
            "stable. Retry once ingestion settles.",
        )
        ingest = Counts(True, (), (), "ingest")
        result = reconcile(source, baseline, actual, ingest)
        # Bucket metadata supplies the selected total; daily queries cover only the
        # recorded baseline window and cannot attribute arrivals to individual groups.
        result["expected_count"] = selected_message_count(
            run["plan"].get("original_selected", run["plan"]["selected"])
        )
        coverage_days = day_window(
            Window(**run["plan"].get("original_coverage", run["plan"]["coverage"]))
        )
        measured_start = max(days.start, coverage_days.start)
        measured_end = min(days.end, coverage_days.end)
        measured_days = max(0, measured_end - measured_start) // DAY
        unmeasured = (coverage_days.end - coverage_days.start) // DAY - measured_days
        other_ingest = any(d["actual"] > d["expected"] for d in result["daily"])
        delta = actual.total - baseline.total
        measured = None if other_ingest or delta < 0 else delta
        result["measured_arrival_count"] = measured
        result["measured_window"] = asdict(days)
        result["unmeasured_day_count"] = unmeasured
        result["actual_count"] = measured if not unmeasured else None
        result["received_unavailable_reason"] = (
            "Other events may have arrived during the run; see the daily comparison."
            if other_ingest
            else "The destination count fell below the baseline; see the daily comparison."
            if delta < 0
            else None
        )
        partial = set(run.get("partial_days", []))
        for entry in result["daily"]:
            entry["partial"] = entry["day"] in partial
        full = [d for d in result["daily"] if not d["partial"]]
        # Without a single fully covered day there is no evidence to judge; that is
        # incomplete, never success.
        result["outcome"] = (
            Verification.INCOMPLETE.value
            if not full
            else Verification.DISCREPANCY.value
            if any(d["difference"] != 0 for d in full)
            else Verification.SUCCESS.value
        )
        if not full:
            result["complete"] = False
            result["reason"] = "no_fully_covered_days"
        result["partial_days"] = sorted(partial)
        result["partial_differences"] = any(
            d["difference"] != 0 for d in result["daily"] if d["partial"]
        )
        result["method"] = (
            "Two agreeing complete observations; per-day source counts over the covered days "
            "against destination counts minus the baseline recorded at start. New ingest is "
            "not attributed; days shared with open or unselected buckets are partial."
        )
        result["buckets"] = {
            "covered": sorted(received),
            "not_received": sorted({key for r in related for key in r["keys"]} - received),
        }
        result["covered_days"] = [day for day, _ in source.daily]
        if any(r.get("basis") == TASK_REPORT for r in related):
            result["receipt_basis"] = TASK_REPORT
            result["receipt_note"] = (
                "What arrived is taken from the destination's task report, not confirmed "
                "against the destination's bucket store (no destination SSH)."
            )
            if any(r.get("unaccounted") for r in related):
                result["receipt_note"] += (
                    " Unaccounted buckets were requested again because the destination "
                    "could not be inspected."
                )
        if result["buckets"]["not_received"]:
            if result["outcome"] == Verification.SUCCESS.value:
                result["outcome"] = Verification.INCOMPLETE.value
            result["reason"] = Code.NOT_RECEIVED.value
        return result
