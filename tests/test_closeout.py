"""Regressions for the independent code-safety review: dead ends, benign drift, stop and verify."""

import copy

import pytest
from conftest import execute, planned
from test_engine import detach_on, operations
from test_fixup import fixup

from logs_migrate.engine import Engine
from logs_migrate.errors import Code, MigrationError
from logs_migrate.models import Policy, State, Verification, Window, digest, parse_time
from logs_migrate.reporting import markdown, report_data


def window(first, last):
    return Window(parse_time(first), parse_time(last))


def plan_day(engine, day):
    return engine.plan(
        window(f"2026-03-0{day}T00:00:00Z", f"2026-03-0{day + 1}T00:00:00Z"),
        Policy(poll_seconds=0),
    )


def mutate_on(engine, kind, change):
    def hook(event):
        if event["kind"] == kind:
            change()

    engine.on_event = hook


def stopped_first_window(engine, env):
    env.data["complete_after_polls"] = 100
    first = plan_day(engine, 1)
    engine.request_stop()
    assert execute(engine, first)["state"] == State.STOPPED
    engine.stop_requested = False
    env.data["complete_after_polls"] = 1
    return first


def test_stopped_run_blocks_a_disjoint_window_until_it_is_closed(rig):
    engine, env, journal = rig
    first = stopped_first_window(engine, env)
    assert engine.verify(first["id"])["verification"]["outcome"] == Verification.INCOMPLETE
    second = plan_day(engine, 2)
    restarted = Engine(env, journal, engine.clock)
    with pytest.raises(MigrationError, match="verification_pending"):
        execute(restarted, second)
    assert operations(env).count("start") == 1

    closed = restarted.close(first["id"])
    assert closed["state"] == State.PARTIAL
    assert closed["closed"]["task_started"] is True
    assert closed["closed"]["landed"] == []
    assert set(closed["closed"]["buckets"].values()) == {"absent"}
    assert closed["closed"]["event_reconciliation"] is False
    assert closed["verification"]["outcome"] != Verification.SUCCESS

    execute(restarted, second)
    assert restarted.verify(second["id"])["state"] == State.VERIFIED
    stays = journal.load(first["id"])
    assert stays["state"] == State.PARTIAL and stays["closed"]
    assert any(t["run"] == first["id"] for t in journal.transfers(env.data["destination"]["id"]))
    with pytest.raises(MigrationError, match="overlapping_transfer"):
        plan_day(restarted, 1)


def test_closed_run_is_reported_as_incomplete_never_success(rig):
    engine, env, journal = rig
    first = stopped_first_window(engine, env)
    closed = engine.close(first["id"])
    actions = report_data(closed, journal.events(first["id"]))["next_actions"]
    assert "never a successful migration" in actions[0]
    text = markdown(closed, journal.events(first["id"]))
    assert "## Closure" in text and "never a successful migration" in text
    assert "VERIFIED" not in text.replace("verification_pending", "")
    kinds = [e["kind"] for e in journal.events(first["id"])]
    assert kinds.index("close_intent") < kinds.index("run_closed")
    with pytest.raises(MigrationError, match="invalid_state_transition"):
        engine.close(first["id"])


def test_close_lists_exactly_which_buckets_landed(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    detach_on(engine, "transfer_start_observed")
    assert execute(engine, run)["state"] == State.MONITOR_DETACHED
    landed, pending = list(run["bucket_outcomes"])[0], list(run["bucket_outcomes"])[1:]
    bucket = next(b for b in env.data["source"]["buckets"] if landed.endswith(b["id"]))
    env.data["destination"]["buckets"].append({**bucket, "node": "dst-1", "registered": True})
    env.data["task"]["completed"].append(landed)
    stopper = Engine(env, journal, engine.clock)
    stopper.request_stop()
    stopper.resume(run["id"])
    closed = stopper.close(run["id"])
    assert closed["closed"]["buckets"][landed] == "landed"
    assert all(closed["closed"]["buckets"][key] == "absent" for key in pending)
    assert closed["closed"]["landed"] == [landed]
    assert closed["bucket_outcomes"][landed] == "complete"


def test_close_refuses_a_run_that_still_needs_real_verification(rig):
    engine, env, journal = rig
    first = plan_day(engine, 1)
    execute(engine, first)
    env.data["faults"] = {"query.destination": "secret"}
    failed = engine.verify(first["id"])
    assert failed["state"] == State.FAILED
    assert failed["verification"]["outcome"] == Verification.FAILURE
    second = plan_day(engine, 2)
    with pytest.raises(MigrationError, match="verification_pending"):
        execute(engine, second)
    before = journal.load(first["id"])
    with pytest.raises(MigrationError, match="verification_pending"):
        engine.close(first["id"])
    assert journal.load(first["id"]) == before
    env.data["faults"] = {}
    assert engine.verify(first["id"])["state"] == State.VERIFIED
    execute(engine, second)
    assert engine.verify(second["id"])["state"] == State.VERIFIED


def test_fixup_reset_run_can_be_closed_or_reverified(rig):
    engine, env, journal = rig
    first = plan_day(engine, 1)
    execute(engine, first)
    assert engine.verify(first["id"])["state"] == State.VERIFIED
    after = fixup(engine, journal.load(first["id"]))
    assert after["state"] == State.PARTIAL
    assert after["verification"]["outcome"] == Verification.PENDING
    second = plan_day(engine, 2)
    with pytest.raises(MigrationError, match="verification_pending"):
        execute(engine, second)
    closed = engine.close(first["id"])
    assert closed["state"] == State.PARTIAL and set(closed["closed"]["buckets"].values()) == {
        "landed"
    }
    execute(engine, second)
    assert engine.verify(second["id"])["state"] == State.VERIFIED
    # Closing never forecloses real verification, which supersedes the closure.
    final = engine.verify(first["id"])
    assert final["state"] == State.VERIFIED and final["closed"] is None


def test_close_of_a_run_that_never_started_releases_the_window(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {"start.before": "crash"}
    with pytest.raises(SystemExit):
        execute(engine, run)
    env.data["faults"] = {}
    restarted = Engine(env, journal, engine.clock)
    with pytest.raises(MigrationError, match="uncertain"):
        restarted.resume(run["id"])
    destination = env.data["destination"]["id"]
    assert journal.unresolved(destination) == [run["id"]]
    assert env.data["task"] is None

    closed = restarted.close(run["id"])
    assert closed["state"] == State.FAILED
    assert closed["closed"]["task_started"] is False
    assert closed["closed"]["overlap_protection"] == "released_never_started"
    assert not journal.unresolved(destination)
    assert not journal.transfers(destination)
    again = planned(restarted)
    execute(restarted, again)
    assert restarted.verify(again["id"])["state"] == State.VERIFIED


@pytest.mark.parametrize("effect", ["accepted", "running"])
def test_close_never_clears_a_start_that_may_have_taken_effect(rig, effect):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100 if effect == "running" else 1
    env.data["faults"] = {"start.after": ["disconnect"]}
    with pytest.raises(MigrationError, match="uncertain"):
        execute(engine, run)
    env.data["faults"] = {}
    before = journal.load(run["id"])
    with pytest.raises(MigrationError, match="uncertain|busy"):
        engine.close(run["id"])
    assert journal.load(run["id"]) == before
    assert journal.load(run["id"])["state"] == State.RECOVERY_REQUIRED
    assert journal.transfers(env.data["destination"]["id"])
    assert operations(env).count("cancel") == 0


def test_close_refuses_an_unacknowledged_start_when_the_bucket_is_present(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {"start.before": "crash"}
    with pytest.raises(SystemExit):
        execute(engine, run)
    env.data["faults"] = {}
    key = next(iter(run["bucket_outcomes"]))
    bucket = next(b for b in env.data["source"]["buckets"] if key.endswith(b["id"]))
    env.data["destination"]["buckets"].append({**bucket, "node": "dst-1"})
    restarted = Engine(env, journal, engine.clock)
    with pytest.raises(MigrationError):
        restarted.resume(run["id"])
    with pytest.raises(MigrationError, match="uncertain"):
        restarted.close(run["id"])
    assert journal.transfers(env.data["destination"]["id"])


@pytest.mark.parametrize("state", ["detached", "verified"])
def test_close_rejects_runs_that_are_not_terminal_incomplete(rig, state):
    engine, env, journal = rig
    run = planned(engine)
    if state == "detached":
        env.data["complete_after_polls"] = 100
        detach_on(engine, "transfer_start_observed")
        execute(engine, run)
    else:
        execute(engine, run)
        engine.verify(run["id"])
    before = journal.load(run["id"])
    with pytest.raises(MigrationError, match="invalid_state_transition"):
        engine.close(run["id"])
    assert journal.load(run["id"]) == before


@pytest.mark.parametrize(
    "mutation", ["baseline_bucket_retired", "node_restarted", "destination_retention"]
)
def test_routine_destination_events_never_cancel_a_healthy_transfer(rig, mutation):
    engine, env, journal = rig
    baseline = {
        "id": "00000000-0000-0000-0000-0000000000aa",
        "node": "dst-1",
        "partition": "",
        "status": "archived",
        "size": 100,
        "messages": 1,
        "start": parse_time("2025-01-01T00:00:00Z"),
        "end": parse_time("2025-01-01T00:00:01Z"),
        "manifest": digest("old"),
        "bounds_proven": True,
    }
    env.data["destination"]["buckets"].append(baseline)
    run = planned(engine)
    env.data["complete_after_polls"] = 3

    def change():
        destination = env.data["destination"]
        if mutation == "baseline_bucket_retired":
            destination["buckets"] = [
                b for b in destination["buckets"] if b["id"] != baseline["id"]
            ]
        elif mutation == "node_restarted":
            destination["nodes"][1]["service_epoch"] = "boot-2"
        else:
            destination["retention_fingerprint"] = "changed-by-destination"

    mutate_on(engine, "transfer_start_observed", change)
    result = execute(engine, run)
    assert result["state"] == State.VERIFYING
    assert "cancel" not in operations(env)
    assert env.data["task"]["status"] == "completed"
    assert result["drift"]
    verified = engine.verify(run["id"])
    assert verified["state"] == State.VERIFIED
    assert verified["drift"] == result["drift"]


def test_destination_baseline_loss_is_still_detected_as_a_discrepancy(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    env.data["destination_events"] = env.data["destination_events"][1:]
    result = engine.verify(run["id"])
    assert result["verification"]["outcome"] == Verification.DISCREPANCY
    assert result["state"] == State.PARTIAL


def test_source_retention_aging_out_a_migrated_bucket_does_not_block_verification(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    aged = next(iter(run["bucket_outcomes"]))
    env.data["source"]["buckets"] = [
        b for b in env.data["source"]["buckets"] if not aged.endswith(b["id"])
    ]
    env.data["source"]["retention_fingerprint"] = "source-retention-ran"
    result = engine.verify(run["id"])
    assert result["state"] == State.VERIFIED
    assert result["verification"]["source_count"] == 6
    assert "source_bucket_aged_out" in result["drift"]


def test_destination_growth_after_copy_does_not_block_verification(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    extra = copy.deepcopy(env.data["destination"]["nodes"][0])
    env.data["destination"]["nodes"].append({**extra, "id": "dst-4"})
    result = engine.verify(run["id"])
    assert result["state"] == State.VERIFIED
    assert "destination_topology_changed" in result["drift"]


def test_destination_losing_a_node_after_copy_is_incomplete_not_success(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    gone = env.data["destination"]["nodes"].pop()["id"]
    env.data["destination"]["buckets"] = [
        b for b in env.data["destination"]["buckets"] if b["node"] != gone
    ]
    result = engine.verify(run["id"])
    assert result["state"] == State.PARTIAL
    assert result["verification"]["reason"] == Code.TOPOLOGY


def test_engine_owned_fixup_restart_does_not_make_verification_unreachable(rig):
    engine, env, journal = rig
    run = planned(engine)
    execute(engine, run)
    assert engine.verify(run["id"])["state"] == State.VERIFIED
    epochs = {n["id"]: n["service_epoch"] for n in env.data["destination"]["nodes"]}
    after = fixup(engine, journal.load(run["id"]))
    assert {n["id"]: n["service_epoch"] for n in env.data["destination"]["nodes"]} != epochs
    assert after["state"] == State.PARTIAL
    final = engine.verify(run["id"])
    assert final["state"] == State.VERIFIED
    assert "destination_service_restarted" in final["drift"]


def test_stop_racing_task_completion_still_accounts_the_landed_buckets(rig):
    engine, env, _ = rig
    env.data["complete_after_polls"] = 2
    run = planned(engine)
    engine.request_stop()
    result = execute(engine, run)
    assert result["state"] == State.STOPPED
    assert result["transfer"]["status"] == "completed"
    assert all(v == "complete" for v in result["bucket_outcomes"].values())
    assert "cancel" not in operations(env)
    assert engine.verify(run["id"])["state"] == State.VERIFIED


def test_stop_cancels_a_running_task_even_when_a_landed_bucket_fails_attestation(rig):
    engine, env, journal = rig
    env.data["complete_after_polls"] = 100
    run = planned(engine)
    detach_on(engine, "transfer_start_observed")
    assert execute(engine, run)["state"] == State.MONITOR_DETACHED
    env.data["task"]["completed"].append(next(iter(run["bucket_outcomes"])))
    stopper = Engine(env, journal, engine.clock)
    stopper.request_stop()
    with pytest.raises(MigrationError, match="integrity_mismatch"):
        stopper.resume(run["id"])
    assert operations(env).count("cancel") == 1
    assert env.data["task"]["status"] == "cancelled"
    saved = journal.load(run["id"])
    assert saved["state"] == State.RECOVERY_REQUIRED
    closed = stopper.close(run["id"])
    assert closed["state"] == State.PARTIAL
    assert "unverified_destination_copy" not in closed["closed"]["buckets"].values()
    assert not journal.unresolved(env.data["destination"]["id"])


def test_task_that_failed_by_itself_is_partial_not_stopped(rig):
    engine, env, _ = rig
    env.data["complete_after_polls"] = 2
    env.data["terminal_status"] = "failed"
    run = planned(engine)
    engine.request_stop()
    result = execute(engine, run)
    assert result["state"] == State.PARTIAL
    assert result["reason"] == Code.INCOMPLETE


def test_reserve_intent_is_journaled_before_the_reservation(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {"reserve": "crash"}
    with pytest.raises(SystemExit):
        execute(engine, run)
    assert "reserve_intent" in [e["kind"] for e in journal.events(run["id"])]


@pytest.mark.parametrize("version", ["9.0.2.0200", "9.1.1"])
def test_close_trusts_persisted_attestation_when_inventory_cannot_show_the_bucket(rig, version):
    engine, env, journal = rig
    env.data["destination"]["version"] = version
    first = plan_day(engine, 1)
    execute(engine, first)
    env.data["query_completeness"] = {"destination": [False] * 10}
    assert engine.verify(first["id"])["verification"]["complete"] is False
    env.data["query_completeness"] = {}
    env.data["destination"]["buckets"] = []
    second = plan_day(engine, 2)
    with pytest.raises(MigrationError, match="verification_pending"):
        execute(engine, second)
    before = journal.load(first["id"])
    with pytest.raises(MigrationError, match="verification_pending"):
        engine.close(first["id"])
    assert journal.load(first["id"]) == before
    assert engine.verify(first["id"])["state"] == State.VERIFIED


def test_close_reports_unknown_not_absent_for_unproven_911_buckets(rig):
    engine, env, _ = rig
    env.data["destination"]["version"] = "9.1.1"
    first = stopped_first_window(engine, env)
    closed = engine.close(first["id"])
    assert set(closed["closed"]["buckets"].values()) == {"unknown"}
    assert closed["closed"]["landed"] == []


@pytest.mark.parametrize("version", ["9.0.2.0200", "9.1.1"])
def test_close_cannot_bypass_verification_for_a_task_that_completed_unobserved(rig, version):
    engine, env, journal = rig
    env.data["destination"]["version"] = version
    env.data["complete_after_polls"] = 100
    first = plan_day(engine, 1)
    env.data["faults"] = {"poll.before": [""] + ["disconnect"] * 1000}
    with pytest.raises(MigrationError, match="uncertain"):
        execute(engine, first)
    assert journal.load(first["id"])["state"] == State.RECOVERY_REQUIRED
    env.data["faults"] = {}
    env.data["complete_after_polls"] = 1
    assert env.transfer().status == "completed"
    before = journal.load(first["id"])
    with pytest.raises(MigrationError, match="verification_pending"):
        engine.close(first["id"])
    assert journal.load(first["id"]) == before
    second = plan_day(engine, 2)
    with pytest.raises(MigrationError, match="service_recovery_required"):
        execute(engine, second)
    assert engine.resume(first["id"])["state"] == State.VERIFYING
    assert engine.verify(first["id"])["state"] == State.VERIFIED
