import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from conftest import FIXTURES, execute, planned

from logs_migrate.adapters.simulated import SimulatedEnvironment
from logs_migrate.engine import Engine
from logs_migrate.errors import Code, MigrationError
from logs_migrate.models import DAY, Counts, Policy, Window
from logs_migrate.ports import NodeTransfer, Transfer
from logs_migrate.reporting import markdown
from logs_migrate.verification import reconcile, stable_counts


def test_complete_duplicate_destination_copies_are_counted_once(rig):
    engine, env, _ = rig
    source = env.data["source"]["buckets"][0]
    for node in ("dst-1", "dst-2", "dst-3"):
        env.data["destination"]["buckets"].append({**source, "node": node})
    run = planned(engine)
    assert [b["id"] for b in run["plan"]["already_received"]] == [source["id"]]
    assert len(run["plan"]["selected"]) == 2
    assert any("dst-1" in warning and "dst-2" in warning for warning in run["plan"]["warnings"])
    execute(engine, run)
    assert engine.status()["status"] == "completed"
    copies = [b for b in env.data["destination"]["buckets"] if b["id"] == source["id"]]
    assert len(copies) == 3
    assert engine.verify(run["id"])["outcome"] == "verified_success"


@pytest.mark.parametrize("nodes", [("dst-1",), ("dst-1", "dst-2")])
def test_buckets_already_on_the_destination_never_block_whichever_node_holds_them(rig, nodes):
    engine, env, _ = rig
    source = env.data["source"]["buckets"][0]
    for node in nodes:
        env.data["destination"]["buckets"].append({**source, "node": node})
    plan = planned(engine)["plan"]
    assert [b["id"] for b in plan["already_received"]] == [source["id"]]
    assert source["id"] not in {b["id"] for b in plan["selected"]}
    assert len(plan["selected"]) == 2
    assert any("another node" in w and source["id"] in w for w in plan["warnings"])


def test_running_external_transfer_names_overlapping_buckets(rig):
    engine, env, _ = rig
    bucket = env.discover()[0].buckets[0]
    env.data["complete_after_polls"] = 100
    env.start(Window(bucket.start, bucket.start + DAY), ())
    with pytest.raises(MigrationError) as error:
        planned(engine)
    assert error.value.code == Code.OVERLAP
    assert "transfer is running" in error.value.message
    assert bucket.id in error.value.message and "2026-03-01" in error.value.message


def _earlier_task_listing(env, bucket):
    env.data["destination"]["inventory_available"] = True
    env.data["history"] = [
        {
            "source": env.data["source"]["id"],
            "destination": env.data["destination"]["id"],
            "window": {"start": bucket.start, "end": bucket.end + 1},
            "destination_nodes": [],
            "bucket_ids": [bucket.id],
            "complete": True,
            "task_id": "earlier-1",
        }
    ]


@pytest.mark.parametrize("crossing", [False, True])
def test_unfinished_earlier_task_holds_buckets_the_destination_lacks(rig, crossing):
    engine, env, _ = rig
    bucket = env.discover()[0].buckets[0]
    _earlier_task_listing(env, bucket)
    if crossing:
        env.data["history"][0]["bucket_ids"] = []
    env.transfer = lambda: Transfer("earlier-1", "unknown")
    with pytest.raises(MigrationError) as error:
        planned(engine)
    assert error.value.code == Code.OVERLAP
    assert bucket.id in error.value.affected_ids
    assert (
        bucket.id in error.value.message and "after that transfer finishes" in error.value.message
    )


@pytest.mark.parametrize("crossing", [False, True])
def test_finished_earlier_task_listing_absent_buckets_warns_and_proceeds(rig, crossing):
    engine, env, _ = rig
    bucket = env.discover()[0].buckets[0]
    _earlier_task_listing(env, bucket)
    if crossing:
        env.data["history"][0]["bucket_ids"] = []
    plan = planned(engine)["plan"]
    assert bucket.id in {b["id"] for b in plan["selected"]}
    note = next(w for w in plan["warnings"] if "not on the destination" in w)
    assert bucket.id in note


def test_repeated_destination_listing_on_one_node_is_incomplete_only_when_selected(rig):
    engine, env, _ = rig
    source = env.data["source"]["buckets"][0]
    env.data["destination"]["buckets"] += [{**source, "node": "dst-1"}] * 2
    with pytest.raises(MigrationError) as error:
        planned(engine)
    assert error.value.code == Code.INCOMPLETE
    assert source["id"] in error.value.message
    other = env.data["source"]["buckets"][1]
    plan = engine.plan(Window(other["start"], other["end"] + 1), Policy(poll_seconds=0))["plan"]
    assert source["id"] not in {b["id"] for b in plan["selected"]}


@pytest.mark.parametrize(
    "field,value", [("complete", False), ("registered", False), ("size", 1), ("messages", 1)]
)
def test_unsafe_existing_copy_names_bucket_and_destination(rig, field, value):
    engine, env, _ = rig
    source = env.data["source"]["buckets"][0]
    env.data["destination"]["buckets"].append({**source, "node": "dst-1", field: value})
    with pytest.raises(MigrationError) as error:
        planned(engine)
    assert source["id"] in error.value.message
    assert "dst-1" in error.value.message and "2026-03-01" in error.value.message
    assert "dealt with on the destination" in error.value.message


def test_all_bad_copies_are_listed_together(rig):
    engine, env, _ = rig
    first, second = env.data["source"]["buckets"][:2]
    env.data["destination"]["buckets"].extend(
        [{**first, "node": "dst-1", "size": 1}, {**second, "node": "dst-2", "messages": 1}]
    )
    with pytest.raises(MigrationError) as error:
        planned(engine)
    assert first["id"] in error.value.message and second["id"] in error.value.message
    assert "source size 1000 bytes, destination size 1 bytes" in error.value.message
    assert "source 2 messages, destination 1 messages" in error.value.message


def test_verification_reports_a_copy_that_became_incomplete(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    assert engine.status()["status"] == "completed"
    copy = env.data["destination"]["buckets"][0]
    copy["complete"] = False
    result = engine.verify(run["id"])
    assert result["incomplete_destination_copies"] == [f"{copy['node']}/{copy['id']}"]


@pytest.mark.parametrize("fixture_name", ["sealed-populated.fixture", "sealed-911.fixture"])
def test_api_flow_preserves_source_and_verifies_daily_counts(rig, fixture_name):
    engine, _, journal = rig
    env = SimulatedEnvironment(json.loads((FIXTURES / fixture_name).read_text()))
    engine = Engine(env, journal, engine.clock)
    before = copy.deepcopy(env.data["source"])
    run = planned(engine)
    assert len(run["plan"]["selected"]) == 3
    assert len(run["plan"]["active_skipped"]) == 1
    assert execute(engine, run)["task"]["status"] == "running"
    assert engine.status()["status"] == "completed"
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_success"
    assert (result["source_count"], result["preexisting_count"], result["new_ingest_count"]) == (
        6,
        1,
        1,
    )
    assert result["expected_count"] == result["actual_count"] == result["source_count"] == 6
    assert all(d["difference"] == 0 for d in result["daily"])
    assert env.data["source"] == before
    assert {t["operation"] for t in env.data["trace"]} == {
        "test_source_credentials",
        "test_mover",
        "start",
    }
    assert "state" not in journal.load(run["id"])


def test_verification_is_cumulative_and_a_later_transfer_never_invalidates_an_earlier_success(rig):
    engine, env, _ = rig
    start = env.data["source"]["buckets"][0]["start"]
    first = engine.plan(Window(start, start + DAY), Policy(poll_seconds=0))
    execute(engine, first)
    assert engine.status()["status"] == "completed"
    before = engine.verify(first["id"])
    assert before["outcome"] == "verified_success" and before["source_count"] == 2
    second = engine.plan(Window(start + DAY, start + 3 * DAY), Policy(poll_seconds=0))
    execute(engine, second)
    assert engine.status()["id"] == "sim-2"
    for run in (first, second):
        result = engine.verify(run["id"])
        assert result["outcome"] == "verified_success"
        assert result["source_count"] == 6 and result["actual_count"] == result["expected_count"]
        assert len(result["buckets"]["covered"]) == 3 and not result["buckets"]["not_received"]
        assert len(result["covered_days"]) == 3


def test_transfers_with_intersecting_coverage_verify_without_double_counting(rig):
    engine, env, _ = rig
    hour = 3_600_000
    buckets = env.data["source"]["buckets"]
    origin = buckets[0]["start"]
    buckets[0]["end"] = origin + 10 * hour
    buckets[1]["start"], buckets[1]["end"] = origin + 5 * hour, origin + 20 * hour
    for event in env.data["source_events"]:
        if event["bucket"].endswith("2"):
            event["time"] = origin + 17 * hour + event["time"] % 1000
    first = engine.plan(Window(origin, origin + hour), Policy(poll_seconds=0))
    second = engine.plan(Window(origin + 15 * hour, origin + 16 * hour), Policy(poll_seconds=0))
    assert {b["id"] for b in first["plan"]["selected"]}.isdisjoint(
        b["id"] for b in second["plan"]["selected"]
    )
    execute(engine, first)
    assert engine.status(first["id"])["status"] == "completed"
    execute(engine, second)
    assert engine.status()["status"] == "completed"
    for run in (first, second):
        result = engine.verify(run["id"])
        assert result["outcome"] == "verified_success"
        assert result["preexisting_count"] == 1 and result["new_ingest_count"] == 2


def test_stopped_transfer_records_only_what_landed_and_the_remainder_can_be_replanned(rig):
    engine, env, _ = rig
    start = env.data["source"]["buckets"][0]["start"]
    env.data["destination"]["version"] = "9.1.1"
    env.data["cancel_after"] = 1
    first = engine.plan(Window(start, start + 3 * DAY), Policy(poll_seconds=0))
    assert len(first["plan"]["selected"]) == 3
    execute(engine, first)
    stopped = engine.status()
    assert stopped["status"] == "cancelled" and len(stopped["completed"]) == 1
    landed = stopped["completed"][0].rsplit("/", 1)[-1]
    remainder = engine.plan(Window(start, start + 3 * DAY), Policy(poll_seconds=0))
    assert [b["id"] for b in remainder["plan"]["already_received"]] == [landed]
    del env.data["cancel_after"]
    remaining = {b["id"] for b in remainder["plan"]["selected"]}
    assert landed not in remaining and len(remaining) == 2
    execute(engine, remainder)
    assert engine.status()["status"] == "completed"
    result = engine.verify(first["id"])
    assert result["outcome"] == "verified_success"
    assert len(result["buckets"]["covered"]) == 3 and result["source_count"] == 6


def test_in_flight_selection_blocks_overlap_until_the_api_reports_what_landed(rig):
    engine, env, _ = rig
    start = env.data["source"]["buckets"][0]["start"]
    env.data["complete_after_polls"] = 100
    execute(engine, engine.plan(Window(start, start + DAY), Policy(poll_seconds=0)))
    with pytest.raises(MigrationError) as error:
        engine.plan(Window(start, start + 3 * DAY), Policy(poll_seconds=0))
    assert error.value.code == Code.OVERLAP
    assert (
        engine.verify(next(iter(engine.journal.db.execute("SELECT run FROM transfers")))[0])[
            "reason"
        ]
        == Code.BUSY
    )


@pytest.mark.parametrize("old_count", [1, 3])
@pytest.mark.parametrize("new_count", [0, 2])
def test_baseline_ingest_is_not_counted_again_even_with_identical_new_arrivals(
    rig, old_count, new_count
):
    engine, env, _ = rig
    old = {**env.data["ingest_during_run"][0], "multiplicity": old_count}
    env.data["destination_events"].append(old)
    env.data["ingest_during_run"] = [{**old, "multiplicity": new_count}] if new_count else []
    run = planned(engine)
    execute(engine, run)
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_success"
    assert result["preexisting_count"] == 1 + old_count
    assert result["new_ingest_count"] == new_count
    assert result["actual_count"] == result["expected_count"] == 6


def test_baseline_loss_is_a_discrepancy(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    engine.status()
    env.data["destination_events"] = [
        e for e in env.data["destination_events"] if e.get("cohort") != "baseline"
    ]
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_discrepancy"
    assert result["actual_count"] == result["expected_count"] - 1


@pytest.mark.parametrize("scope", ["destination", "ingest"])
def test_incomplete_counts_cannot_replace_prior_success_with_success(rig, scope):
    engine, env, journal = rig
    run = planned(engine)
    execute(engine, run)
    assert engine.verify(run["id"])["outcome"] == "verified_success"
    env.data["query_completeness"] = {scope: [False] * 6}
    result = engine.verify(run["id"])
    assert result["outcome"] == "incomplete_verification" and result["complete"] is False
    assert journal.load(run["id"])["verification"] == result


def test_stable_counts_requires_two_consecutive_complete_observations(rig):
    engine, env, _ = rig
    values = iter(
        [
            Counts(True, (("2026-03-01", 1),), (), "destination"),
            Counts(False, (("2026-03-01", 1),), (), "destination"),
            Counts(True, (("2026-03-01", 2),), (), "destination"),
            Counts(True, (("2026-03-01", 2),), (), "destination"),
        ]
    )
    env.count = lambda *args: next(values)
    result = stable_counts(
        env, engine.clock, Policy(poll_seconds=1), "destination", Window(1, 2), ()
    )
    assert result.total == 2 and engine.clock.seconds == 3


def test_equal_totals_on_different_days_are_not_verified():
    source = Counts(True, (("2026-03-01", 1),), (), "source")
    baseline = Counts(True, (), (), "baseline")
    actual = Counts(True, (("2026-03-02", 1),), (), "destination")
    ingest = Counts(True, (), (), "ingest")
    assert reconcile(source, baseline, actual, ingest)["outcome"] == "verified_discrepancy"


def test_source_retention_after_transfer_does_not_erase_count_evidence(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    engine.status()
    env.data["source_events"] = []
    env.data["source"]["buckets"] = []
    assert engine.verify(run["id"])["outcome"] == "verified_success"


@pytest.mark.parametrize("status", ["running", "unknown", "unrecognized"])
def test_api_task_uncertainty_never_reports_verification_success(rig, status):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    env.data["complete_after_polls"] = 100
    env.data["task"]["status"] = status
    assert engine.verify(run["id"])["outcome"] == "incomplete_verification"


def test_status_requires_run_identity_and_supplies_estimates(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    task = Transfer(
        "sim-1",
        "running",
        bytes_transferred=200,
        elapsed_seconds=4,
        total_bytes=1000,
        destination=run["plan"]["destination"]["id"],
    )
    env.transfer = lambda: task
    assert engine.status(run["id"])["eta_seconds"] == 16
    untied = Engine(env, None).status()
    assert untied["status"] == "idle" and untied["run"] is None
    assert "--journal" in untied["no_run"]["hint"]
    assert untied["destination_tasks"]["tasks"] == [
        {"id": "sim-1", "status": "running", "window": None, "progress": None}
    ]
    assert "not tied to any run" in untied["destination_tasks"]["note"]
    task = replace(task, id="old-task", status="completed")
    assert engine.status(run["id"])["status"] == "idle"


@pytest.mark.parametrize("status", ["failed", "cancelled", "idle", "unknown"])
def test_only_a_running_task_reports_an_eta(rig, status):
    _, env, _ = rig
    task = Transfer("api-task", status, bytes_transferred=200, elapsed_seconds=4, total_bytes=1000)
    env.transfer = lambda: task
    assert Engine(env, None).status()["eta_seconds"] is None


def test_lost_post_ack_can_be_observed_and_explicitly_cancelled_without_replay(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    env.data["faults"] = {"start.after": "connection"}
    with pytest.raises(MigrationError) as error:
        execute(engine, run)
    assert error.value.code == Code.UNCERTAIN
    fresh = Engine(env, journal, engine.clock)
    task = fresh.status(run["id"])
    assert task["status"] == "running" and task["id"] == "sim-1"
    assert "start_outcome" not in task
    assert journal.transfers(env.data["destination"]["id"])[0]["task_id"] == "sim-1"
    with pytest.raises(MigrationError):
        fresh.stop("different-task")
    assert fresh.stop(task["id"])["status"] == "cancelled"
    assert journal.transfers(env.data["destination"]["id"])
    with pytest.raises(MigrationError):
        execute(engine, run)
    assert sum(t["operation"] == "start" for t in env.data["trace"]) == 1


def test_lost_post_ack_with_no_task_found_says_the_start_is_unknown(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {"start.before": "connection"}
    with pytest.raises(MigrationError) as error:
        execute(engine, run)
    assert error.value.code == Code.UNCERTAIN
    result = Engine(env, journal, engine.clock).status(run["id"])
    assert result["status"] == "idle" and result["id"] == ""
    assert result["start_outcome"] == "unknown"
    attempted = datetime.fromisoformat(
        journal.transfers(env.data["destination"]["id"])[0]["attempted_at"]
    )
    assert datetime.fromisoformat(result["hold_until"]) == attempted + timedelta(minutes=15)
    assert (
        result["hold_until"] in result["start_hint"]
        and "plan and copy again" in result["start_hint"]
    )


@pytest.mark.parametrize("skew_seconds,found", [(120, True), (1200, False)])
def test_lost_post_lookup_tolerates_a_few_minutes_of_appliance_clock_skew(rig, skew_seconds, found):
    from datetime import datetime

    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {"start.after": "connection"}
    with pytest.raises(MigrationError):
        execute(engine, run)
    destination = env.data["destination"]["id"]
    row = journal.transfers(destination)[0]
    created = int(datetime.fromisoformat(row["attempted_at"]).timestamp() * 1000)
    created -= skew_seconds * 1000

    def finder(task_id="", *, window=None, attempted_at_ms=None):
        if window == Window(**row["window"]) and created >= attempted_at_ms:
            return Transfer("late-task", "running", destination=destination, window=window)
        return Transfer("", "idle", destination=destination)

    env.transfer_for = finder
    result = Engine(env, journal, engine.clock).status(run["id"])
    assert (result["id"] == "late-task") is found
    assert (result.get("start_outcome") == "unknown") is (not found)


def test_stop_does_not_relabel_a_completed_task(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    assert engine.stop("sim-1")["status"] == "completed"
    assert not any(t["operation"] == "cancel" for t in env.data["trace"])


def test_active_acknowledgment_precedes_api_post(rig):
    engine, env, _ = rig
    run = planned(engine)
    with pytest.raises(MigrationError) as error:
        engine.run(run["id"])
    assert error.value.code == Code.ACK
    assert not env.data["trace"]


def test_api_busy_blocks_new_start(rig):
    engine, env, _ = rig
    start = env.data["source"]["buckets"][0]["start"]
    first = engine.plan(Window(start, start + DAY), Policy(poll_seconds=0))
    env.data["complete_after_polls"] = 100
    execute(engine, first)
    second = engine.plan(Window(start + DAY, start + 3 * DAY), Policy(poll_seconds=0))
    with pytest.raises(MigrationError) as error:
        execute(engine, second)
    assert error.value.code == Code.BUSY
    assert sum(t["operation"] == "start" for t in env.data["trace"]) == 1


@pytest.mark.parametrize("preview", [False, True])
def test_overlap_guard_survives_current_task_replacement_and_missing_destination_ids(rig, preview):
    engine, env, journal = rig
    if preview:
        env.data["destination"]["version"] = "9.1.1"
    run = planned(engine)
    execute(engine, run)
    engine.status()
    env.data["task"] = None
    env.data["history"] = []
    env.data["destination"]["buckets"] = []
    if preview:
        plan = planned(Engine(env, journal, engine.clock))["plan"]
        assert not plan["selected"] and plan["already_received"]
    else:
        with pytest.raises(MigrationError) as error:
            planned(Engine(env, journal, engine.clock))
        assert error.value.code == Code.INCOMPLETE
        assert "inventory does not show them" in error.value.message


def test_whole_bucket_cannot_be_sent_twice_in_adjacent_windows(rig):
    engine, env, _ = rig
    start = env.data["source"]["buckets"][0]["start"]
    env.data["destination"]["version"] = "9.1.1"
    first = engine.plan(Window(start, start + 500), Policy(poll_seconds=0))
    execute(engine, first)
    engine.status()
    plan = engine.plan(Window(start + 500, start + 1001))
    assert not plan["plan"]["selected"] and plan["plan"]["already_received"]
    with pytest.raises(MigrationError, match=Code.STATE):
        execute(engine, plan)


@pytest.mark.parametrize("change", ["partition", "fields", "history", "inventory"])
def test_preflight_refusals_do_not_mutate_appliances(rig, change):
    engine, env, _ = rig
    env.data["destination"]["version"] = "9.1.1"
    run = planned(engine)
    if change == "partition":
        env.data["destination"]["partitions"] = []
    elif change == "fields":
        env.data["destination"]["fields"] = []
    elif change == "history":
        env.data["destination"]["history_complete"] = False
    else:
        env.data["source"]["buckets"][0]["size"] += 1
    with pytest.raises(MigrationError):
        execute(engine, run)
    assert not env.data["trace"]


def test_presentation_callback_failure_cannot_abort_an_api_start(rig):
    engine, env, _ = rig
    run = planned(engine)

    def broken(event):
        raise RuntimeError("observer unavailable")

    engine.on_event = broken
    assert execute(engine, run)["task"]["status"] == "running"
    assert engine.status()["status"] == "completed"


def test_api_skipped_bucket_is_reported_as_not_received_in_result_and_report(rig):
    engine, env, journal = rig
    run = planned(engine)
    skipped = env.data["source_events"][0]["bucket"]
    env.data["skip_buckets"] = [skipped]
    execute(engine, run)
    status = engine.status()
    assert status["status"] == "completed" and list(status["skipped"]) == [skipped]
    result = engine.verify(run["id"])
    # Regression: the received groups reconcile, but a skipped selection is never a success.
    assert result["outcome"] == "incomplete_verification"
    assert result["reason"] == Code.NOT_RECEIVED and result["complete"] is True
    assert result["buckets"]["not_received"] == [skipped]
    assert skipped not in result["buckets"]["covered"] and result["source_count"] == 4
    assert all(d["difference"] == 0 for d in result["daily"])
    text = markdown(journal.load(run["id"]), journal.events(run["id"]))
    assert "verified_success" not in text and "incomplete_verification" in text
    assert f"selected but not received: {skipped}" in text


def test_skipped_bucket_with_count_loss_stays_a_discrepancy(rig):
    engine, env, _ = rig
    run = planned(engine)
    skipped = env.data["source_events"][0]["bucket"]
    env.data["skip_buckets"] = [skipped]
    execute(engine, run)
    engine.status()
    env.data["destination_events"] = [
        e for e in env.data["destination_events"] if e.get("cohort") != "baseline"
    ]
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_discrepancy"
    assert result["reason"] == Code.NOT_RECEIVED and result["buckets"]["not_received"] == [skipped]


def test_cancel_resume_and_verify_all_received_buckets(rig):
    engine, env, _ = rig
    env.data["cancel_after"] = 1
    first = planned(engine)
    execute(engine, first)
    assert engine.status(first["id"])["status"] == "cancelled"
    resumed = engine.resume(first["id"])
    assert len(resumed["plan"]["already_received"]) == 1
    assert len(resumed["plan"]["selected"]) == 2
    del env.data["cancel_after"]
    execute(engine, resumed)
    assert engine.status(resumed["id"])["status"] == "completed"
    result = engine.verify(resumed["id"])
    assert result["outcome"] == "verified_success"
    assert len(result["buckets"]["covered"]) == 3


@pytest.mark.parametrize("failure", ["node", "disk"])
def test_resume_after_one_node_fails_or_hits_disk_limit(rig, failure):
    from logs_migrate.models import digest

    engine, env, _ = rig
    first = planned(engine)
    bucket = first["plan"]["selected"][0]
    key = bucket["node"] + "/" + bucket["id"]
    nodes = env.data["destination"]["nodes"]
    node = nodes[int(digest(bucket["id"])[:8], 16) % len(nodes)]["id"]
    if failure == "node":
        env.data["failed_nodes"] = [node]
    else:
        env.data["skip_buckets"] = [key]
    execute(engine, first)
    status = engine.status(first["id"])
    assert status["status"] == ("failed" if failure == "node" else "completed")
    if failure == "node":
        assert any(n["status"] == "failed" for n in status["nodes"])
        assert any(n["status"] == "completed" for n in status["nodes"])
    env.data.pop("failed_nodes", None)
    env.data.pop("skip_buckets", None)
    resumed = engine.resume(first["id"])
    assert any(b["id"] == bucket["id"] for b in resumed["plan"]["selected"])
    execute(engine, resumed)
    engine.status(resumed["id"])
    assert engine.verify(resumed["id"])["outcome"] == "verified_success"


def test_resume_without_original_journal_refuses(rig, tmp_path):
    from logs_migrate.journal import Journal

    engine, env, _ = rig
    first = planned(engine)
    execute(engine, first)
    engine.status(first["id"])
    lost = Journal(tmp_path / "replacement.sqlite")
    try:
        with pytest.raises(MigrationError, match=Code.INVALID_INPUT):
            Engine(env, lost, engine.clock).resume(first["id"])
    finally:
        lost.close()


def test_resume_911_uses_journal_receipts(rig):
    _, _, journal = rig
    env = SimulatedEnvironment(json.loads((FIXTURES / "sealed-911.fixture").read_text()))
    engine = Engine(env, journal)
    env.data["cancel_after"] = 1
    first = planned(engine)
    execute(engine, first)
    assert engine.status(first["id"])["status"] == "cancelled"
    resumed = engine.resume(first["id"])
    assert len(resumed["plan"]["already_received"]) == 1
    del env.data["cancel_after"]
    execute(engine, resumed)
    engine.status(resumed["id"])
    assert engine.verify(resumed["id"])["outcome"] == "verified_success"


def test_resume_a_plan_that_already_included_previously_received_buckets(rig):
    engine, env, _ = rig
    start = env.data["source"]["buckets"][0]["start"]
    earlier = engine.plan(Window(start, start + DAY), Policy(poll_seconds=0))
    execute(engine, earlier)
    engine.status(earlier["id"])
    env.data["cancel_after"] = 1
    first = engine.plan(Window(start, start + 3 * DAY), Policy(poll_seconds=0))
    assert len(first["plan"]["already_received"]) == 1
    execute(engine, first)
    engine.status(first["id"])
    resumed = engine.resume(first["id"])
    assert len(resumed["plan"]["already_received"]) == 2
    assert len(resumed["plan"]["selected"]) == 1
    del env.data["cancel_after"]
    execute(engine, resumed)
    engine.status(resumed["id"])
    assert engine.verify(resumed["id"])["outcome"] == "verified_success"


def test_resume_refuses_retired_or_unregistered_remaining_bucket(rig):
    engine, env, _ = rig
    env.data["cancel_after"] = 1
    first = planned(engine)
    execute(engine, first)
    engine.status(first["id"])
    remaining = first["plan"]["selected"][1]["id"]
    env.data["source"]["buckets"] = [
        b for b in env.data["source"]["buckets"] if b["id"] != remaining
    ]
    with pytest.raises(MigrationError, match=Code.INVENTORY_CHANGED):
        engine.resume(first["id"])


def test_resume_refuses_a_present_unregistered_bucket(rig):
    engine, env, _ = rig
    first = planned(engine)
    env.data["partial_at_bucket"] = (
        first["plan"]["selected"][0]["node"] + "/" + first["plan"]["selected"][0]["id"]
    )
    execute(engine, first)
    assert engine.status(first["id"])["status"] == "failed"
    with pytest.raises(MigrationError, match=Code.INCOMPLETE):
        engine.resume(first["id"])


def test_replanning_also_refuses_changed_destination_node_count(rig):
    engine, env, _ = rig
    env.data["cancel_after"] = 1
    first = planned(engine)
    execute(engine, first)
    engine.status(first["id"])
    env.data["destination"]["nodes"].append(
        dict(env.data["destination"]["nodes"][0], id="dst-extra")
    )
    with pytest.raises(MigrationError, match=Code.TOPOLOGY):
        planned(engine)


def test_resume_refuses_changed_node_count(rig):
    engine, env, _ = rig
    env.data["cancel_after"] = 1
    first = planned(engine)
    execute(engine, first)
    engine.status(first["id"])
    extra = dict(env.data["destination"]["nodes"][0], id="dst-extra")
    env.data["destination"]["nodes"].append(extra)
    with pytest.raises(MigrationError, match=Code.TOPOLOGY):
        engine.resume(first["id"])


def test_destination_restart_or_inventory_drift_does_not_cancel_transfer(rig):
    engine, env, _ = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 3
    execute(engine, run)
    env.data["destination"]["nodes"][0]["used"] += 1
    restarting = env.data["destination"]["nodes"][0]["id"]
    env.data["node_statuses"] = {restarting: "unknown"}
    status = engine.status()
    assert status["status"] == "running"
    assert any(n["node"] == restarting and n["status"] == "unknown" for n in status["nodes"])
    assert not any(t["operation"] == "cancel" for t in env.data["trace"])
    del env.data["node_statuses"]
    engine.status()
    assert engine.verify(run["id"])["outcome"] == "verified_success"


def test_source_credential_failure_stops_at_first_node(rig):
    engine, env, _ = rig
    run = planned(engine)
    env.data["faults"] = {"source_credentials.src-1": "auth"}
    with pytest.raises(MigrationError) as error:
        execute(engine, run)
    assert error.value.code == Code.AUTH
    assert env.data["trace"] == [{"operation": "test_source_credentials", "node": "src-1"}]


def test_plan_shows_space_and_copy_refuses_when_a_node_cannot_hold_the_selection(rig):
    engine, env, _ = rig
    fits = planned(engine)["plan"]
    required = sum(b["size"] for b in fits["selected"])
    assert fits["capacity"]["fits"] and fits["capacity"]["required_bytes"] == required
    assert any("root password" in w for w in fits["warnings"])
    assert any("retention" in w for w in fits["warnings"])
    node = env.data["destination"]["nodes"][0]
    node["repository_available"] = fits["capacity"]["estimated_bytes_per_node"] - 1
    short = planned(engine)
    capacity = short["plan"]["capacity"]
    assert capacity["fits"] is False
    assert [n["fits"] for n in capacity["nodes"]][0] is False
    with pytest.raises(MigrationError) as error:
        execute(engine, short)
    assert error.value.code == Code.CAPACITY
    assert not env.data["trace"]


def test_status_uses_rate_over_a_window_not_between_two_polls(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    destination = run["plan"]["destination"]["id"]
    polls = [(0, 0), (30, 0), (60, 600), (65, 600), (70, 600)]
    seen = []
    for elapsed, copied in polls:
        env.transfer = lambda e=elapsed, c=copied: Transfer(
            "sim-1",
            "running",
            bytes_transferred=c,
            destination=destination,
            elapsed_seconds=e,
            total_bytes=1200,
        )
        seen.append(engine.status(run["id"]))
    assert seen[2]["rate_bytes_per_second"] == 10
    assert seen[2]["eta_seconds"] == 60
    for later in seen[3:]:
        assert later["rate_bytes_per_second"] > 0
        assert later["eta_seconds"] is not None


def test_node_rate_uses_a_window_of_pending_samples(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    selected = run["plan"]["selected"]
    ids = [b["id"] for b in selected]
    destination = run["plan"]["destination"]["id"]

    def poll(elapsed, remaining):
        node = NodeTransfer(
            "dst-1",
            "running",
            bytes_transferred=None,
            total_buckets={"source": len(ids)},
            scheduled_ids={"source": tuple(remaining)},
            elapsed_seconds=elapsed,
        )
        env.transfer = lambda: Transfer(
            "sim-1",
            "running",
            bytes_transferred=None,
            nodes=(node,),
            destination=destination,
            elapsed_seconds=elapsed,
        )
        return engine.status(run["id"])

    poll(10, ids[1:])
    short = poll(20, ids[2:])
    assert short["nodes"][0].get("rate_bytes_per_second") != selected[1]["size"] / 10
    progress = poll(70, ids[2:])
    assert progress["nodes"][0]["rate_bytes_per_second"] == selected[1]["size"] / 60


def test_observed_assignment_provides_progress_and_measured_preview(rig):
    engine, _, journal = rig
    run = planned(engine)
    selected = run["plan"]["selected"]
    ids = tuple(b["id"] for b in selected)
    node = NodeTransfer(
        "dst-1",
        "running",
        scheduled_ids={"source": ids},
        total_buckets={"source": len(ids)},
        elapsed_seconds=300,
    )
    task = Transfer("test", "running", nodes=(node,), elapsed_seconds=300)
    engine._observe_assignment(run, task)
    later = replace(task, nodes=(replace(node, scheduled_ids={"source": ids[1:]}),))
    progress = engine._progress(later, run)
    assert progress["buckets_done"] == 1
    assert progress["bytes_transferred"] == selected[0]["size"]
    assert progress["nodes"][0]["eta_seconds"] is not None
    run["measured_rate_bytes_per_second"] = 12_000_000
    journal.save(run, "rate_observed")
    estimate = engine._estimate(run["plan"])
    assert estimate["rate_basis"] == "measured overall"
    assert estimate["rate_bytes_per_second"] == 12_000_000
    terminal = engine._progress(replace(later, status="completed"), run)
    assert "buckets_done" not in terminal


def test_capacity_warns_when_api_assignment_exceeds_node_budget(rig):
    engine, _, _ = rig
    plan = planned(engine)["plan"]
    bucket = plan["selected"][0]
    node = plan["capacity"]["nodes"][0]
    node["available_bytes"] = bucket["size"] - 1
    task = Transfer(
        "task",
        "running",
        nodes=(NodeTransfer(node["id"], "running", scheduled_ids={"source": (bucket["id"],)}),),
    )
    assert engine._capacity_assignment(task, plan) == [
        {"node": node["id"], "scheduled_bytes": bucket["size"]}
    ]
    unknown = replace(task, nodes=(replace(task.nodes[0], scheduled_ids=None),))
    assert engine._capacity_assignment(unknown, plan) == []


def test_capacity_estimates_each_node_and_checks_95_percent_disk_limit(rig):
    from logs_migrate.models import Node
    from logs_migrate.planning import capacity

    _, env, _ = rig
    source, destination = env.discover()
    buckets = tuple(b for b in source.buckets if b.status != "active")
    total = sum(b.size for b in buckets)
    share = (total * 5 + 11) // 12
    destination = replace(
        destination,
        nodes=tuple(Node(f"dst-{i}", total * 10, 0, share) for i in range(3)),
    )
    result = capacity(destination, buckets)
    assert result["fits"] and result["estimated_bytes_per_node"] == share < total
    assert all(n["estimated_bytes"] == share for n in result["nodes"])
    full = replace(destination.nodes[0], used=total * 10 * 95 // 100 - share)
    result = capacity(replace(destination, nodes=(full, *destination.nodes[1:])), buckets)
    assert result["fits"] is False and result["nodes"][0]["fits"] is False


@pytest.mark.parametrize("field,value", [("id", "other-cluster"), ("identity", "re-enrolled")])
def test_changed_destination_endpoint_is_refused_before_post(rig, field, value):
    engine, env, _ = rig
    run = planned(engine)
    env.data["destination"][field] = value
    with pytest.raises(MigrationError) as error:
        execute(engine, run)
    assert error.value.code == Code.INVENTORY_CHANGED
    assert not env.data["trace"]


@pytest.mark.parametrize("field,value", [("id", "other-cluster"), ("identity", "re-enrolled")])
def test_changed_destination_endpoint_never_verifies(rig, field, value):
    engine, env, journal = rig
    run = planned(engine)
    execute(engine, run)
    engine.status()
    env.data["destination"][field] = value
    result = engine.verify(run["id"])
    assert result["outcome"] == "incomplete_verification" and result["complete"] is False
    assert result["reason"] == Code.TOPOLOGY
    assert journal.load(run["id"])["verification"] == result


def test_a_9_1_1_plan_says_before_the_run_that_counts_cannot_be_verified(rig):
    engine, _, journal = rig
    env = SimulatedEnvironment(json.loads((FIXTURES / "sealed-911.fixture").read_text()))
    run = planned(Engine(env, journal, engine.clock))
    warnings = [w for w in run["plan"]["warnings"] if "Count verification is not available" in w]
    assert len(warnings) == 1 and "9.1.1" in warnings[0] and "query route" in warnings[0]
    other = planned(engine)
    assert not [w for w in other["plan"]["warnings"] if "Count verification" in w]


def test_undated_sealed_bucket_is_listed_not_fatal_and_never_copied(rig, fixture):
    undated = "00000000-0000-0000-0000-0000000000a5"
    fixture["source"]["buckets"].append(
        {
            "id": undated,
            "node": "src-2",
            "partition": "",
            "status": "archived",
            "size": 90158030,
            "messages": 0,
            "start": None,
            "end": None,
            "manifest": None,
            "bounds_proven": False,
            "complete": True,
        }
    )
    base, _, journal = rig
    engine = Engine(SimulatedEnvironment(fixture), journal, base.clock)
    run = planned(engine)
    plan = run["plan"]
    assert undated not in {b["id"] for b in plan["selected"]}
    assert plan["undated_excluded"] == [
        {"id": undated, "node": "src-2", "partition": "", "size": 90158030, "created": None}
    ]
    assert any("src-2/" + undated in w for w in plan["warnings"])
    # A window the undated bucket cannot be shown to touch still plans, and with no dated
    # bucket in range the refusal is the ordinary "nothing selected", not a bucket-level error.
    with pytest.raises(MigrationError, match=Code.INCOMPLETE):
        engine.plan(Window(DAY, 2 * DAY), Policy(poll_seconds=0))


def test_signoff_report_formats_utc_dates_and_ranges(rig):
    engine, _, journal = rig
    run = planned(engine)
    execute(engine, run)
    engine.status()
    engine.verify(run["id"])
    result = journal.load(run["id"])
    text = markdown(result, journal.events(run["id"]))
    assert "selected groups;" in text
    assert "UTC" in text and "Unix ms" not in text
    assert "2026-03-01 to 2026-03-03" in text
    block = text.split("\n\n")[2]
    assert "Last day covered only by sealed buckets right now" in block
    assert "snapshot" not in text.lower()
    assert "backup" not in text.lower()
    assert text.count("Last day covered only by sealed buckets") == 1
    assert "| Day | Source |" in text


def test_plan_warning_for_an_unrecognized_listing_names_the_host_only(rig):
    engine, env, _ = rig
    env.listing_warnings = {"src-1.lab"}
    start = env.data["source"]["buckets"][0]["start"]
    run = engine.plan(Window(start, start + DAY), Policy(poll_seconds=0))
    (warning,) = [w for w in run["plan"]["warnings"] if "src-1.lab" in w]
    assert "no recognizable groups" in warning and "by hand" in warning
    assert "first line" not in warning


def test_status_polling_does_not_write_the_journal(rig):
    engine, env, journal = rig
    run = planned(engine)
    execute(engine, run)
    destination = run["plan"]["destination"]["id"]
    before = len(journal.events(run["id"]))
    for elapsed, copied in ((10, 100), (20, 400), (30, 700)):
        env.transfer = lambda e=elapsed, c=copied: Transfer(
            "sim-1",
            "running",
            bytes_transferred=c,
            destination=destination,
            elapsed_seconds=e,
            total_bytes=1000,
        )
        engine.status(run["id"])
    assert len(journal.events(run["id"])) == before
