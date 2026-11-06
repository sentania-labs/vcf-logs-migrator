import copy
from dataclasses import asdict, replace

import pytest
from conftest import execute, planned, verified

from logs_migrate.engine import Engine
from logs_migrate.errors import Code, MigrationError
from logs_migrate.models import ACTIVE_ACK, Bucket, Policy, State, Window, digest, parse_time
from logs_migrate.planning import build_plan, capacity
from logs_migrate.reporting import markdown, report_data


def detach_on(engine, kind):
    def hook(event):
        if event["kind"] == kind:
            engine.request_detach()

    engine.on_event = hook


def flaky_discover(env, failures):
    original = env.discover
    left = {"n": failures}

    def discover():
        task = env.data["task"]
        if task and task["status"] == "running" and env.data["poll"] >= 1 and left["n"]:
            left["n"] -= 1
            raise MigrationError(Code.CONNECTION)
        return original()

    env.discover = discover


def operations(env):
    return [entry["operation"] for entry in env.data["trace"]]


def test_full_workflow_populated_target_ingest_and_no_source_mutation(rig):
    engine, env, journal = rig
    before = copy.deepcopy(env.data["source"])
    run = verified(engine)
    assert run["state"] == State.VERIFIED
    assert run["verification"]["actual_count"] == 8
    assert run["verification"]["source_count"] == 6
    assert run["verification"]["preexisting_count"] == 1
    assert run["verification"]["new_ingest_count"] == 1
    assert env.data["source"] == before
    assert "stop_service" not in operations(env)
    assert len(run["plan"]["active_skipped"]) == 1
    assert run["plan"]["last_fully_sealed_day"] is None
    assert all(v == "complete" for v in run["bucket_outcomes"].values())
    kinds = [e["kind"] for e in journal.events(run["id"])]
    assert kinds.index("transfer_start_intent") < kinds.index("transfer_start_observed")


@pytest.mark.parametrize("version", ["9.0.2.0200", "9.1.1"])
def test_both_destination_engines_and_preview(rig, version):
    engine, env, _ = rig
    env.data["destination"]["version"] = version
    run = verified(engine)
    assert run["state"] == State.VERIFIED
    if version == "9.1.1":
        assert env.data["destination"]["buckets"] == []
        assert any("preview" in s for s in run["plan"]["warnings"])
        with pytest.raises(MigrationError, match="unsupported"):
            engine.plan(Window(**run["plan"]["window"]), tail_of=run["id"])


@pytest.mark.parametrize(
    "kwargs",
    [dict(backup_ack=False, active_ack=ACTIVE_ACK), dict(backup_ack=True, active_ack="yes")],
)
def test_acknowledgments_bind_to_plan_and_no_start(rig, kwargs):
    engine, env, _ = rig
    run = planned(engine)
    with pytest.raises(MigrationError, match="acknowledgment"):
        engine.run(run["id"], plan_hash=run["plan_hash"], **kwargs)
    assert "start" not in operations(env)


def test_changed_plan_hash_and_tampered_plan_block(rig):
    engine, env, journal = rig
    run = planned(engine)
    with pytest.raises(MigrationError, match="acknowledgment"):
        engine.run(run["id"], plan_hash="other", backup_ack=True, active_ack=ACTIVE_ACK)
    run = journal.load(run["id"])
    run["plan"]["window"]["end"] += 1000
    journal.save(run, "tampered")
    with pytest.raises(MigrationError, match="integrity"):
        execute(engine, run)
    assert "start" not in operations(env)


def test_release_gate_has_no_execution_override(rig):
    engine, env, _ = rig
    run = planned(engine)
    env.mode = "appliance"
    with pytest.raises(MigrationError, match="unsupported"):
        execute(engine, run)
    assert "start" not in operations(env)


def test_overlap_and_changed_nodes_block(rig):
    engine, env, _ = rig
    run = verified(engine)
    with pytest.raises(MigrationError):
        planned(engine)
    env.data["destination"]["nodes"].append({**env.data["destination"]["nodes"][0], "id": "dst-4"})
    with pytest.raises(MigrationError, match="topology"):
        engine.plan(Window(**run["plan"]["window"]), tail_of=run["id"])
    assert operations(env).count("start") == 1


def test_sealed_tail_90_reconciles_existing_ids(rig):
    engine, env, _ = rig
    first = verified(engine)
    active = env.data["source"]["buckets"][-1]
    active.update(
        status="archived",
        complete=True,
        size=1000,
        messages=1,
        start=parse_time("2026-03-04T00:00:00Z"),
        end=parse_time("2026-03-04T00:00:00Z"),
        manifest=digest("tail"),
        bounds_proven=True,
    )
    env.data["source_events"].append(
        {"id": "tail", "time": active["start"], "bucket": active["node"] + "/" + active["id"]}
    )
    env.data["ingest_during_run"] = []
    second = engine.plan(Window(**first["plan"]["window"]), tail_of=first["id"])
    assert len(second["plan"]["selected"]) == 1
    execute(engine, second)
    assert engine.verify(second["id"])["state"] == State.VERIFIED
    assert len(env.data["destination"]["buckets"]) == 4


def test_wide_bucket_is_not_reimported_in_adjacent_911_window(rig):
    engine, env, _ = rig
    env.data["destination"]["version"] = "9.1.1"
    env.data["source"]["buckets"][0]["end"] = parse_time("2026-05-01T00:00:00Z")
    first = verified(engine)
    assert first["state"] == State.VERIFIED
    with pytest.raises(MigrationError, match="overlapping"):
        engine.plan(Window(parse_time("2026-04-01T00:00:00Z"), parse_time("2026-05-02T00:00:00Z")))
    assert operations(env).count("start") == 1


@pytest.mark.parametrize("mutation", ["retention", "removed", "sealed", "manifest", "identity"])
def test_preflight_revalidates_live_inventory(rig, mutation):
    engine, env, _ = rig
    run = planned(engine)
    source = env.data["source"]
    if mutation == "retention":
        source["retention_fingerprint"] = "changed"
    elif mutation == "removed":
        source["buckets"].pop(0)
    elif mutation == "sealed":
        source["buckets"][-1]["status"] = "archived"
    elif mutation == "manifest":
        source["buckets"][0]["manifest"] = digest("new-content")
    else:
        source["identity"] = "new-key"
    with pytest.raises(MigrationError, match="inventory_changed"):
        execute(engine, run)
    assert "start" not in operations(env)


@pytest.mark.parametrize("collision", ["different", "unregistered", "partial", "duplicate"])
def test_conflicting_destination_is_preserved_and_blocks(rig, collision):
    engine, env, _ = rig
    b = {**env.data["source"]["buckets"][0], "node": "dst-1"}
    if collision == "different":
        b["manifest"] = digest("different")
    elif collision == "unregistered":
        b["registered"] = False
    elif collision == "partial":
        b["complete"] = False
    env.data["destination"]["buckets"].append(b)
    if collision == "duplicate":
        env.data["destination"]["buckets"].append({**b, "node": "dst-2"})
    before = copy.deepcopy(env.data["destination"])
    with pytest.raises(MigrationError, match="conflict"):
        planned(engine)
    assert env.data["destination"] == before
    assert "start" not in operations(env)


def test_different_bucket_ids_same_events_blocks(rig):
    engine, env, _ = rig
    env.data["destination_events"].append({**env.data["source_events"][0], "bucket": "other-id"})
    run = planned(engine)
    with pytest.raises(MigrationError, match="overlapping"):
        execute(engine, run)
    assert "start" not in operations(env)


def test_partition_check_create_and_911_fields(rig):
    engine, env, _ = rig
    env.data["source"]["partitions"].append(
        {
            "name": "secops",
            "id": "secops-id",
            "criteria": 'appname="Vpxa"',
            "retention_seconds": 0,
            "order": 1,
        }
    )
    run = planned(engine)
    assert len(run["plan"]["partition_diff"]["missing"]) == 1
    with pytest.raises(MigrationError, match="partition"):
        execute(engine, run)
    new = engine.plan(
        Window(**run["plan"]["window"]),
        create_missing=True,
        backup_ack=True,
        partition_hash=engine.discover()["partition_preparation_hash"],
    )
    assert new["plan"]["partition_diff"]["missing"] == []
    assert operations(env).count("create_partition") == 1
    env.data["destination"]["version"] = "9.1.1"
    env.data["destination"]["fields"] = []
    new = planned(engine)
    with pytest.raises(MigrationError, match="fields"):
        execute(engine, new)


def test_capacity_uses_each_node_and_service_headroom(rig):
    engine, env, _ = rig
    source, dest = env.discover()
    dest = replace(dest, nodes=(replace(dest.nodes[0], repository_available=100), *dest.nodes[1:]))
    with pytest.raises(MigrationError, match="capacity"):
        capacity(dest, source.buckets, Policy())
    run = planned(engine)
    env.data["destination"]["nodes"][1]["repository_available"] = 1
    with pytest.raises(MigrationError, match="capacity"):
        execute(engine, run)
    assert "start" not in operations(env)


def test_api_completed_with_skips_is_partial(rig):
    engine, env, _ = rig
    run = planned(engine)
    env.data["skip_buckets"] = [next(iter(run["bucket_outcomes"]))]
    result = execute(engine, run)
    assert result["state"] == State.PARTIAL
    assert engine.verify(run["id"])["verification"]["outcome"] == "incomplete_verification"


def test_lost_post_response_never_reposts(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {"start.after": ["disconnect"]}
    with pytest.raises(MigrationError, match="uncertain"):
        execute(engine, run)
    assert journal.load(run["id"])["state"] == State.RECOVERY_REQUIRED
    with pytest.raises(MigrationError, match="uncertain"):
        engine.resume(run["id"])
    assert operations(env).count("start") == 1


def test_long_running_task_is_monitored_not_uncertain(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    result = execute(engine, run)
    assert result["state"] == State.VERIFYING
    assert env.data["poll"] == 100 > Policy().attempts
    assert not journal.unresolved(run["plan"]["destination"]["id"])
    assert engine.verify(run["id"])["state"] == State.VERIFIED


def test_detach_leaves_task_running_and_resume_reattaches_without_repost(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    detach_on(engine, "transfer_start_observed")
    result = execute(engine, run)
    assert result["state"] == State.MONITOR_DETACHED
    assert env.data["task"]["status"] == "running"
    assert "cancel" not in operations(env)
    assert not journal.unresolved(run["plan"]["destination"]["id"])
    with pytest.raises(MigrationError, match="destination_busy"):
        engine.verify(run["id"])
    assert journal.load(run["id"])["state"] == State.MONITOR_DETACHED
    env.data["complete_after_polls"] = 1
    reattached = Engine(env, journal, engine.clock)
    assert reattached.resume(run["id"])["state"] == State.VERIFYING
    assert reattached.verify(run["id"])["state"] == State.VERIFIED
    assert operations(env).count("start") == 1
    assert journal.load(run["id"])["verification"]["actual_count"] == 8


def test_stop_cancels_a_detached_task(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    detach_on(engine, "transfer_start_observed")
    execute(engine, run)
    stopper = Engine(env, journal, engine.clock)
    stopper.request_stop()
    assert stopper.resume(run["id"])["state"] == State.STOPPED
    assert env.data["task"]["status"] == "cancelled"


def test_transient_contact_loss_keeps_monitoring(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 3
    env.data["faults"] = {"poll.before": ["", "disconnect", "disconnect"]}
    assert execute(engine, run)["state"] == State.VERIFYING
    kinds = [e["kind"] for e in journal.events(run["id"])]
    assert kinds.count("monitor_contact_lost") == 1


def test_contact_loss_beyond_window_is_uncertain_and_never_cancels(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {"poll.before": [""] + ["disconnect"] * 1000}
    with pytest.raises(MigrationError, match="uncertain"):
        execute(engine, run)
    assert journal.load(run["id"])["state"] == State.RECOVERY_REQUIRED
    assert "cancel" not in operations(env)
    assert engine.clock.seconds >= Policy().contact_loss_seconds


def test_source_rotation_after_copy_still_verifies_and_reports_tail(rig):
    engine, env, journal = rig
    run = planned(engine)
    execute(engine, run)
    buckets = env.data["source"]["buckets"]
    sealed = dict(buckets[-1], status="archived", bounds_proven=True)
    sealed.update(
        start=parse_time("2026-03-31T00:00:00Z"),
        end=parse_time("2026-03-31T12:00:00Z"),
        manifest=digest("late"),
        messages=1,
        size=10,
    )
    buckets[-1] = sealed
    buckets.append({**sealed, "id": "00000000-0000-0000-0000-000000000005", "status": "active"})
    result = engine.verify(run["id"])
    assert result["state"] == State.VERIFIED
    assert result["tail_data"] == ["src-1/00000000-0000-0000-0000-000000000004"]
    assert "--tail-of" in report_data(result, journal.events(run["id"]))["next_actions"][-1]


def test_selected_bucket_change_after_copy_still_stops_verification(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    env.data["source"]["buckets"][0]["manifest"] = digest("changed")
    result = engine.verify(run["id"])
    assert result["state"] == State.PARTIAL
    assert result["verification"]["reason"] == "inventory_changed"


def test_new_in_window_sealed_bucket_blocks_before_start(rig):
    engine, env, _ = rig
    run = planned(engine)
    active = env.data["source"]["buckets"][-1]
    active.update(
        status="archived",
        bounds_proven=True,
        start=parse_time("2026-03-31T00:00:00Z"),
        end=parse_time("2026-03-31T12:00:00Z"),
        manifest=digest("late"),
    )
    with pytest.raises(MigrationError, match="inventory_changed"):
        execute(engine, run)
    assert "start" not in operations(env)


def test_cooperative_stop_waits_for_server_and_preserves_outcome(rig):
    engine, env, journal = rig
    env.data["complete_after_polls"] = 100
    run = planned(engine)
    engine.request_stop()
    result = execute(engine, run)
    assert result["state"] == State.STOPPED
    assert result["transfer"]["status"] == "cancelled"
    assert operations(env).count("cancel") == 1
    kinds = [e["kind"] for e in journal.events(run["id"])]
    assert "cancel_intent" in kinds


def test_resume_precheck_failure_still_cancels_running_task(rig):
    engine, env, journal = rig
    env.data["complete_after_polls"] = 100
    run = planned(engine)
    detach_on(engine, "transfer_start_observed")
    assert execute(engine, run)["state"] == State.MONITOR_DETACHED
    assert env.data["task"]["status"] == "running"
    env.data["source"]["retention_fingerprint"] = "changed"
    stopper = Engine(env, journal, engine.clock)
    stopper.request_stop()
    with pytest.raises(MigrationError, match="inventory_changed"):
        stopper.resume(run["id"])
    assert operations(env).count("cancel") == 1
    assert env.data["task"]["status"] == "cancelled"
    saved = journal.load(run["id"])
    assert saved["state"] == State.PARTIAL
    assert saved["reason"] == "inventory_changed"
    with pytest.raises(MigrationError, match="invalid_state_transition"):
        stopper.resume(run["id"])
    assert journal.load(run["id"])["state"] == State.PARTIAL
    assert operations(env).count("cancel") == 1
    assert not journal.unresolved(run["plan"]["destination"]["id"])


def test_cancel_failure_is_not_stopped(rig):
    engine, env, journal = rig
    env.data["complete_after_polls"] = 100
    env.data["faults"] = {"cancel.before": "disconnect"}
    run = planned(engine)
    engine.request_stop()
    with pytest.raises(MigrationError, match="uncertain"):
        execute(engine, run)
    assert journal.load(run["id"])["state"] == State.RECOVERY_REQUIRED
    assert env.data["task"]["status"] == "running"


def test_foreign_running_task_blocks(rig):
    engine, env, _ = rig
    run = planned(engine)
    env.data["task"] = {
        "id": "foreign",
        "status": "running",
        "keys": [],
        "completed": [],
        "skipped": [],
        "bytes_transferred": 0,
    }
    env.data["complete_after_polls"] = 100
    with pytest.raises(MigrationError, match="busy"):
        execute(engine, run)
    assert "cancel" not in operations(env)


@pytest.mark.parametrize("complete", [False, None])
def test_incomplete_or_missing_query_completeness_never_passes(rig, complete):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    env.data["query_completeness"] = {"destination": [complete] * 20}
    result = engine.verify(run["id"])
    assert result["state"] == State.PARTIAL
    assert result["verification"]["outcome"] == "incomplete_verification"


def test_incomplete_then_two_complete_reads_pass(rig):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    env.data["query_completeness"] = {"destination": [False, True, True]}
    assert engine.verify(run["id"])["state"] == State.VERIFIED


@pytest.mark.parametrize("fault", ["missing", "duplicate", "compensated", "lost_baseline"])
def test_exact_identity_accounting_catches_discrepancies(rig, fault):
    engine, env, _ = rig
    run = planned(engine)
    execute(engine, run)
    rows = env.data["destination_events"]
    if fault == "missing":
        rows.pop(1)
    elif fault == "duplicate":
        rows.append(dict(rows[1]))
    elif fault == "compensated":
        rows.pop(1)
        rows.append(dict(rows[1]))
    else:
        rows.pop(0)
    result = engine.verify(run["id"])
    assert result["state"] == State.PARTIAL
    assert result["verification"]["outcome"] == "verified_discrepancy"
    if fault == "compensated":
        assert result["verification"]["actual_count"] == result["verification"]["expected_count"]
        assert result["verification"]["missing_events"] == 1
        assert result["verification"]["unexpected_events"] == 1


def test_reports_include_truthful_state_and_reconciliation(rig):
    engine, _, journal = rig
    run = verified(engine)
    text = markdown(run, journal.events(run["id"]))
    assert "Fixture evidence only" in text and "verified_success" in text
    assert "Pre-existing" in text and "CST" in text
    assert report_data(run, [])["run"]["backup_verified"] is False


def test_transition_cannot_jump_to_verified(rig):
    engine, _, journal = rig
    run = planned(engine)
    with pytest.raises(MigrationError, match="transition"):
        journal.transition(run, State.VERIFIED)


def test_half_open_selection_inclusive_bucket_end_and_late_backfill(rig):
    _, env, _ = rig
    source, dest = env.discover()
    b = source.buckets[0]
    assert b.selected(Window(b.end, b.end + 1))
    assert not b.selected(Window(b.end + 1, b.end + 2))
    assert not b.selected(Window(b.start - 1, b.start))
    outlier = replace(b, start=b.start - 100 * 86400000)
    plan = build_plan(
        replace(source, buckets=(outlier,)), dest, Window(b.start, b.end + 1), Policy()
    )
    assert plan["coverage"]["start"] == outlier.start
    unknown = replace(b, bounds_proven=False)
    with pytest.raises(MigrationError, match="incomplete"):
        unknown.selected(Window(b.start, b.end + 1))


def test_timezones_leap_day_and_invalid_naive_times():
    assert parse_time("2024-02-29T12:00:00Z") == parse_time("2024-02-29T06:00:00-06:00")
    assert (
        parse_time("2026-03-08T03:00:00-05:00") - parse_time("2026-03-08T01:00:00-06:00") == 3600000
    )
    with pytest.raises(MigrationError):
        parse_time("2026-03-01")


def test_unknown_bucket_status_and_traversal_fail(rig):
    _, env, _ = rig
    b = asdict(env.discover()[0].buckets[0])
    with pytest.raises(MigrationError):
        Bucket(**{**b, "id": "../../store"})
    with pytest.raises(MigrationError):
        Bucket(**{**b, "status": "maybe-sealed"})


def test_external_history_and_unknown_history_block(rig):
    engine, env, _ = rig
    env.data["destination"]["history_complete"] = False
    with pytest.raises(MigrationError, match="incomplete"):
        planned(engine)
    env.data["destination"]["history_complete"] = True
    env.data["history"] = [
        dict(
            source="source-cluster",
            destination="dst-cluster",
            window=dict(
                start=parse_time("2026-03-01T00:00:00Z"), end=parse_time("2026-04-01T00:00:00Z")
            ),
            destination_nodes=["dst-1", "dst-2", "dst-3"],
            bucket_ids=["other"],
            complete=True,
        )
    ]
    with pytest.raises(MigrationError, match="overlapping"):
        planned(engine)


def test_source_auth_failure_stops_at_first_node_without_api_test(rig):
    engine, env, _ = rig
    run = planned(engine)
    env.data["faults"] = {"source_credentials.src-1": "auth"}
    with pytest.raises(MigrationError, match="authentication"):
        execute(engine, run)
    assert operations(env).count("test_source_credentials") == 1
    assert "test_mover" not in operations(env)


def test_resume_monitor_failure_after_confirmed_cancel_stays_partial(rig, monkeypatch):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    detach_on(engine, "transfer_start_observed")
    execute(engine, run)
    original = env.transfer

    def poll_then_exhaust():
        result = original()
        if result.status == "running":
            env.data["destination"]["nodes"][0]["repository_available"] = 1
        return result

    monkeypatch.setattr(env, "transfer", poll_then_exhaust)
    with pytest.raises(MigrationError, match="capacity"):
        Engine(env, journal, engine.clock).resume(run["id"])
    saved = journal.load(run["id"])
    assert saved["state"] == State.PARTIAL
    assert saved["reason"] == "insufficient_capacity"
    assert saved["transfer"]["status"] == "cancelled"


def test_transient_inventory_blip_during_monitoring_does_not_cancel(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 3
    flaky_discover(env, 2)
    assert execute(engine, run)["state"] == State.VERIFYING
    assert "cancel" not in operations(env)
    assert engine.verify(run["id"])["state"] == State.VERIFIED


def test_persistent_inventory_outage_is_uncertain_and_leaves_task_running(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    flaky_discover(env, 10**6)
    with pytest.raises(MigrationError, match="uncertain"):
        execute(engine, run)
    assert journal.load(run["id"])["state"] == State.RECOVERY_REQUIRED
    assert env.data["task"]["status"] == "running"
    assert "cancel" not in operations(env)


def test_resume_precheck_blip_does_not_cancel_running_task(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    detach_on(engine, "transfer_start_observed")
    assert execute(engine, run)["state"] == State.MONITOR_DETACHED
    env.data["complete_after_polls"] = 3
    flaky_discover(env, 2)
    assert Engine(env, journal, engine.clock).resume(run["id"])["state"] == State.VERIFYING
    assert "cancel" not in operations(env)


def test_attest_connection_blip_does_not_strand_the_run(rig):
    engine, env, _ = rig
    run = planned(engine)
    original = env.attest_transfer
    left = {"n": 2}

    def attest(bucket):
        if env.data["task"] and env.data["task"]["status"] == "running" and left["n"]:
            left["n"] -= 1
            raise MigrationError(Code.CONNECTION)
        return original(bucket)

    env.attest_transfer = attest
    assert execute(engine, run)["state"] == State.VERIFYING


def test_interrupt_before_start_aborts_without_starting_and_can_retry(rig):
    engine, env, journal = rig
    run = planned(engine)
    engine.request_detach()
    with pytest.raises(MigrationError, match="interrupted_before_start"):
        execute(engine, run)
    assert "start" not in operations(env)
    saved = journal.load(run["id"])
    assert saved["state"] == State.BLOCKED and not saved["transfer_intent"]
    assert not journal.transfers(run["plan"]["destination"]["id"])
    assert execute(Engine(env, journal, engine.clock), run)["state"] == State.VERIFYING


def test_detach_interrupts_contact_loss_wait(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    env.data["faults"] = {"poll.before": [""] + ["disconnect"] * 1000}
    detach_on(engine, "monitor_contact_lost")
    result = execute(engine, run)
    assert result["state"] == State.MONITOR_DETACHED
    assert engine.clock.seconds < Policy().contact_loss_seconds
    assert env.data["task"]["status"] == "running"
    assert "cancel" not in operations(env)


def test_partition_creation_blocked_while_detached_task_runs(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    detach_on(engine, "transfer_start_observed")
    assert execute(engine, run)["state"] == State.MONITOR_DETACHED
    env.data["source"]["partitions"].append(
        {
            "name": "secops",
            "id": "secops-id",
            "criteria": 'appname="Vpxa"',
            "retention_seconds": 0,
            "order": 1,
        }
    )
    other = Engine(env, journal, engine.clock)
    with pytest.raises(MigrationError, match="destination_busy"):
        other.plan(
            Window(**run["plan"]["window"]),
            create_missing=True,
            backup_ack=True,
            partition_hash=other.discover()["partition_preparation_hash"],
        )
    assert "create_partition" not in operations(env)


@pytest.mark.parametrize("terminal", ["verified", "partial", "stopped"])
def test_resume_and_stop_reject_terminal_runs_without_changing_them(rig, terminal):
    engine, env, journal = rig
    run = planned(engine)
    if terminal == "verified":
        execute(engine, run)
        engine.verify(run["id"])
    elif terminal == "partial":
        env.data["complete_after_polls"] = 100
        detach_on(engine, "transfer_start_observed")
        execute(engine, run)
        env.data["source"]["retention_fingerprint"] = "changed"
        stopper = Engine(env, journal, engine.clock)
        stopper.request_stop()
        with pytest.raises(MigrationError):
            stopper.resume(run["id"])
    else:
        env.data["complete_after_polls"] = 100
        detach_on(engine, "transfer_start_observed")
        execute(engine, run)
        stopper = Engine(env, journal, engine.clock)
        stopper.request_stop()
        assert stopper.resume(run["id"])["state"] == State.STOPPED
    before = journal.load(run["id"])
    cancels = operations(env).count("cancel")
    for stop in (False, True):
        actor = Engine(env, journal, engine.clock)
        if stop:
            actor.request_stop()
        with pytest.raises(MigrationError, match="invalid_state_transition"):
            actor.resume(run["id"])
    assert journal.load(run["id"]) == before
    assert operations(env).count("cancel") == cancels


def test_verify_refuses_a_run_whose_monitor_died_and_resume_reattaches(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {"poll.after_complete": "crash"}
    with pytest.raises(SystemExit):
        execute(engine, run)
    env.data["faults"] = {}
    assert journal.load(run["id"])["state"] == State.RUNNING
    with pytest.raises(MigrationError, match="destination_busy"):
        engine.verify(run["id"])
    assert journal.load(run["id"])["state"] == State.RUNNING
    assert engine.resume(run["id"])["state"] == State.VERIFYING
    assert engine.verify(run["id"])["state"] == State.VERIFIED
