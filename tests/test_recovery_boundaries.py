import pytest
from conftest import execute, planned, verified
from test_engine import operations
from test_fixup import fixup

from logs_migrate.errors import Code, MigrationError
from logs_migrate.models import State, Window


def test_create_partition_lost_response_reconciles_without_duplicate_post(rig):
    engine, env, journal = rig
    env.data["source"]["partitions"].append(
        {
            "name": "secops",
            "id": "secops",
            "criteria": 'appname="vpxa"',
            "retention_seconds": 0,
            "order": 1,
        }
    )
    preview = engine.discover()
    run = planned(engine)
    env.data["faults"] = {"create_partition.after": ["disconnect"]}
    with pytest.raises(MigrationError) as error:
        engine.plan(
            Window(**run["plan"]["window"]),
            create_missing=True,
            backup_ack=True,
            partition_hash=preview["partition_preparation_hash"],
        )
    preparation_id = error.value.run_id
    assert journal.load(preparation_id)["state"] == State.RECOVERY_REQUIRED
    assert journal.unresolved(env.data["destination"]["id"]) == [preparation_id]
    recovered = engine.resume(preparation_id)
    assert recovered["state"] == State.BLOCKED  # A new immutable plan is required.
    assert not journal.unresolved(env.data["destination"]["id"])
    assert operations(env).count("create_partition") == 1
    assert planned(engine)["state"] == State.PLANNED


def test_partition_hash_rejects_changed_routing_before_mutation(rig):
    engine, env, _ = rig
    preview = engine.discover()
    run = planned(engine)
    env.data["source"]["partitions"].append(
        {
            "name": "secops",
            "id": "secops",
            "criteria": 'appname="vpxa"',
            "retention_seconds": 0,
            "order": 1,
        }
    )
    with pytest.raises(MigrationError, match="acknowledgment"):
        engine.plan(
            Window(**run["plan"]["window"]),
            create_missing=True,
            backup_ack=True,
            partition_hash=preview["partition_preparation_hash"],
        )
    assert "create_partition" not in operations(env)


def test_full_journal_does_not_prevent_service_restore(rig, monkeypatch):
    engine, env, journal = rig
    run = verified(engine)
    original = engine._save

    def fail_restart_record(current, kind, details=None):
        if kind == "service_restart_intent":
            raise MigrationError(Code.STORAGE)
        return original(current, kind, details)

    monkeypatch.setattr(engine, "_save", fail_restart_record)
    with pytest.raises(MigrationError, match="recovery"):
        fixup(engine, run)
    assert all(n["service"] == "running" for n in env.data["destination"]["nodes"])
    assert journal.load(run["id"])["service_obligations"]
    assert env.data["recovery_markers"]
    monkeypatch.setattr(engine, "_save", original)
    assert engine.recover_services(run["id"])["service_obligations"] == {}


def test_crash_after_marker_clear_reconciles_without_starting_unowned_node(rig, monkeypatch):
    engine, env, journal = rig
    run = verified(engine)
    original = env.clear_recovery

    def clear_then_crash(node, run_id):
        original(node, run_id)
        raise SystemExit(97)

    monkeypatch.setattr(env, "clear_recovery", clear_then_crash)
    with pytest.raises(SystemExit):
        fixup(engine, run)
    saved = journal.load(run["id"])
    assert next(iter(saved["service_obligations"].values()))["restored"] is True
    assert not env.data["recovery_markers"]
    before = operations(env).count("start_service")
    engine.recover_services(run["id"])
    assert operations(env).count("start_service") == before


def test_verified_fixup_nodes_are_not_stopped_again(rig):
    engine, env, _ = rig
    run = verified(engine)
    fixup(engine, run)
    before = operations(env).count("stop_service")
    assert engine.verify(run["id"])["state"] == State.VERIFIED
    assert fixup(engine, run)["fixup"]["outcome"] == "complete"
    assert operations(env).count("stop_service") == before


def test_capacity_drop_during_transfer_cancels_within_poll(rig, monkeypatch):
    engine, env, journal = rig
    run = planned(engine)
    env.data["complete_after_polls"] = 100
    original = env.transfer

    def poll_then_exhaust():
        result = original()
        if result.status == "running":
            env.data["destination"]["nodes"][0]["repository_available"] = 1
        return result

    monkeypatch.setattr(env, "transfer", poll_then_exhaust)
    with pytest.raises(MigrationError, match="capacity"):
        execute(engine, run)
    assert operations(env).count("cancel") == 1
    saved = journal.load(run["id"])
    assert saved["state"] == State.PARTIAL
    assert saved["reason"] == Code.CAPACITY
    assert saved["transfer"]["status"] == "cancelled"


def test_ambiguous_node_state_is_recovery_required_not_stopped(rig):
    engine, env, journal = rig
    run = planned(engine)
    env.data["terminal_status"] = "new-vendor-state"
    with pytest.raises(MigrationError, match="uncertain"):
        execute(engine, run)
    assert journal.load(run["id"])["state"] == State.RECOVERY_REQUIRED


@pytest.mark.parametrize("status", ["failed", "running", "unrecognized"])
def test_global_completed_does_not_hide_per_node_outcome(rig, status):
    engine, env, journal = rig
    run = planned(engine)
    env.data["node_statuses"] = {"dst-2": status}
    if status == "unrecognized":
        with pytest.raises(MigrationError, match="uncertain"):
            execute(engine, run)
        assert journal.load(run["id"])["state"] == State.RECOVERY_REQUIRED
        assert engine.verify(run["id"])["state"] == State.RECOVERY_REQUIRED
    else:
        assert execute(engine, run)["state"] == State.PARTIAL
    assert engine.verify(run["id"])["state"] != State.VERIFIED


def test_partial_api_copy_is_preserved_unregistered_and_blocks_replanning(rig):
    engine, env, _ = rig
    run = planned(engine)
    env.data["partial_at_bucket"] = next(iter(run["bucket_outcomes"]))
    assert execute(engine, run)["state"] == State.PARTIAL
    partial = env.data["destination"]["buckets"][0]
    assert partial["complete"] is False and partial["registered"] is False
    assert partial["manifest"] is None
    with pytest.raises(MigrationError, match="conflict"):
        planned(engine)
    assert env.data["destination"]["buckets"][0] == partial
    assert engine.verify(run["id"])["state"] != State.VERIFIED
