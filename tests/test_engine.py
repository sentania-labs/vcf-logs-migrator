import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from conftest import FIXTURES, execute, planned

from logs_migrate.adapters.simulated import SimulatedEnvironment
from logs_migrate.engine import Engine
from logs_migrate.errors import Code, MigrationError
from logs_migrate.models import ACTIVE_ACK, DAY, Counts, Policy, Window
from logs_migrate.ports import Transfer
from logs_migrate.reporting import markdown
from logs_migrate.verification import reconcile, stable_counts


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
    with pytest.raises(MigrationError) as error:
        engine.plan(Window(start, start + 3 * DAY), Policy(poll_seconds=0))
    assert error.value.code == Code.OVERLAP
    del env.data["cancel_after"]
    remainder = engine.plan(Window(start + DAY, start + 3 * DAY), Policy(poll_seconds=0))
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


@pytest.mark.parametrize("backup,active", [(False, ACTIVE_ACK), (True, "")])
def test_required_attestations_precede_api_post(rig, backup, active):
    engine, env, _ = rig
    run = planned(engine)
    with pytest.raises(MigrationError) as error:
        engine.run(run["id"], backup_ack=backup, active_ack=active)
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
    with pytest.raises(MigrationError) as error:
        planned(Engine(env, journal, engine.clock))
    assert error.value.code == Code.OVERLAP


def test_whole_bucket_cannot_be_sent_twice_in_adjacent_windows(rig):
    engine, env, _ = rig
    start = env.data["source"]["buckets"][0]["start"]
    env.data["destination"]["version"] = "9.1.1"
    first = engine.plan(Window(start, start + 500), Policy(poll_seconds=0))
    execute(engine, first)
    engine.status()
    with pytest.raises(MigrationError) as error:
        engine.plan(Window(start + 500, start + 1001))
    assert error.value.code == Code.OVERLAP


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


def test_destination_restart_or_inventory_drift_does_not_cancel_transfer(rig):
    engine, env, _ = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 3
    execute(engine, run)
    env.data["destination"]["nodes"][0]["used"] += 1
    assert engine.status()["status"] == "running"
    assert not any(t["operation"] == "cancel" for t in env.data["trace"])
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
    node["repository_available"] = required - 1
    short = planned(engine)
    capacity = short["plan"]["capacity"]
    assert capacity["fits"] is False
    assert [n["fits"] for n in capacity["nodes"]][0] is False
    with pytest.raises(MigrationError) as error:
        execute(engine, short)
    assert error.value.code == Code.CAPACITY
    assert not env.data["trace"]


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
    assert "Snapshot acknowledgment" in block
    assert text.count("Snapshot acknowledgment") == 1
    assert text.count("Last day covered only by sealed buckets") == 1
    assert "| Day | Source |" in text
