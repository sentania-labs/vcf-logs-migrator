import copy
import json
from dataclasses import replace

import pytest
from conftest import FIXTURES, execute, planned

from logs_migrate.adapters.simulated import SimulatedEnvironment
from logs_migrate.engine import Engine
from logs_migrate.errors import Code, MigrationError
from logs_migrate.models import ACTIVE_ACK, DAY, Counts, Policy, Window
from logs_migrate.ports import Transfer
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
    assert result["expected_count"] == result["actual_count"] == 8
    assert all(d["difference"] == 0 for d in result["daily"])
    assert env.data["source"] == before
    assert {t["operation"] for t in env.data["trace"]} == {
        "test_source_credentials",
        "test_mover",
        "start",
    }
    assert "state" not in journal.load(run["id"])


def test_two_disjoint_transfers_remain_independently_verifiable(rig):
    engine, env, _ = rig
    start = env.data["source"]["buckets"][0]["start"]
    first = engine.plan(Window(start, start + DAY), Policy(poll_seconds=0))
    execute(engine, first)
    assert engine.status()["status"] == "completed"
    second = engine.plan(Window(start + DAY, start + 3 * DAY), Policy(poll_seconds=0))
    execute(engine, second)
    assert engine.status()["id"] == "sim-2"
    for run, count in [(first, 2), (second, 4)]:
        result = engine.verify(run["id"])
        assert result["outcome"] == "verified_success"
        assert result["source_count"] == count


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
    assert result["actual_count"] == 7 + old_count + new_count


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


def test_status_is_api_truth_without_a_journal_and_supplies_estimates(rig):
    engine, env, _ = rig
    task = Transfer(
        "api-task", "running", bytes_transferred=200, elapsed_seconds=4, total_bytes=1000
    )
    env.transfer = lambda: task
    wrapper = Engine(env, None)
    assert wrapper.status()["rate_bytes_per_second"] == 50
    assert wrapper.status()["eta_seconds"] == 16
    task = replace(task, status="failed", elapsed_seconds=None)
    result = wrapper.status()
    assert result["status"] == "failed" and result["rate_bytes_per_second"] is None
    assert result["eta_seconds"] is None


def test_lost_post_ack_can_be_observed_and_explicitly_cancelled_without_replay(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    env.data["faults"] = {"start.after": "connection"}
    with pytest.raises(MigrationError) as error:
        execute(engine, run)
    assert error.value.code == Code.UNCERTAIN
    fresh = Engine(env, None)
    task = fresh.status()
    assert task["status"] == "running"
    with pytest.raises(MigrationError):
        fresh.stop("different-task")
    assert fresh.stop(task["id"])["status"] == "cancelled"
    assert journal.transfers(env.data["destination"]["id"])
    with pytest.raises(MigrationError):
        execute(engine, run)
    assert sum(t["operation"] == "start" for t in env.data["trace"]) == 1


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


def test_api_completed_with_skips_is_a_count_discrepancy(rig):
    engine, env, _ = rig
    run = planned(engine)
    env.data["skip_buckets"] = [env.data["source_events"][0]["bucket"]]
    execute(engine, run)
    status = engine.status()
    assert status["status"] == "completed" and status["skipped"]
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_discrepancy"
    assert result["expected_count"] - result["actual_count"] == 2


def test_destination_restart_or_inventory_drift_does_not_cancel_transfer(rig):
    engine, env, _ = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 3
    execute(engine, run)
    env.data["destination"]["nodes"][0]["service_epoch"] = "boot-2"
    env.data["destination"]["retention_fingerprint"] = "changed"
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
