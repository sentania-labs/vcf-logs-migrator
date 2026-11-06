import copy

import pytest
from conftest import execute, planned, verified
from test_engine import operations

from logs_migrate.errors import MigrationError
from logs_migrate.models import State, Window


def fixup(engine, run):
    return engine.fixup(
        run["id"],
        consent_nodes=("dst-1", "dst-2", "dst-3"),
        backup_ack=True,
        plan_hash=run["plan_hash"],
    )


def test_fixup_all_credentials_first_primary_last_and_one_node_at_a_time(rig):
    engine, env, journal = rig
    run = verified(engine)
    before_source = copy.deepcopy(env.data["source"])
    result = fixup(engine, run)
    assert result["fixup"]["outcome"] == "complete"
    assert result["fixup"]["counts_unchanged"]
    trace = env.data["trace"]
    first_stop = next(i for i, e in enumerate(trace) if e["operation"] == "stop_service")
    tested = {e["node"] for e in trace[:first_stop] if e["operation"] == "test_node_credentials"}
    stopped = [e["node"] for e in trace if e["operation"] == "stop_service"]
    assert tested == set(stopped)
    assert "dst-1" not in stopped or stopped[-1] == "dst-1"
    down = set()
    for event in trace:
        if event["operation"] == "stop_service":
            down.add(event["node"])
            assert len(down) == 1
        elif event["operation"] == "start_service":
            down.remove(event["node"])
    assert not down
    assert env.data["source"] == before_source
    assert not journal.load(run["id"])["service_obligations"]
    assert not env.data["recovery_markers"]


@pytest.mark.parametrize("failure", ["credentials", "artifact", "health", "consent"])
def test_preflight_failure_stops_no_nodes(rig, failure):
    engine, env, _ = rig
    run = verified(engine)
    nodes = {b["node"] for b in env.data["destination"]["buckets"]}
    if failure == "credentials":
        env.data["faults"] = {"credentials." + node: "auth" for node in nodes}
    elif failure == "artifact":
        env.data["artifact_verified"] = False
    elif failure == "health":
        env.data["query_unready"] = {node: True for node in nodes}
    with pytest.raises(MigrationError):
        if failure == "consent":
            engine.fixup(run["id"], consent_nodes=(), backup_ack=True, plan_hash=run["plan_hash"])
        else:
            fixup(engine, run)
    assert "stop_service" not in operations(env)


@pytest.mark.parametrize(
    "fault_point", ["stop.before", "stop.after", "fixup.before", "fixup.after"]
)
def test_fixup_exceptions_always_restore_safe_catalog(rig, fault_point):
    engine, env, journal = rig
    run = verified(engine)
    env.data["faults"] = {fault_point + "." + n: "disconnect" for n in ("dst-1", "dst-2", "dst-3")}
    with pytest.raises(MigrationError):
        fixup(engine, run)
    assert all(n["service"] == "running" for n in env.data["destination"]["nodes"])
    assert not journal.load(run["id"])["service_obligations"]
    assert operations(env).count("start_service") == 1
    assert operations(env).count("stop_service") == 1


def test_failed_restart_is_durable_and_recoverable(rig):
    engine, env, journal = rig
    run = verified(engine)
    env.data["faults"] = {"restart.before." + n: "disconnect" for n in ("dst-1", "dst-2", "dst-3")}
    with pytest.raises(MigrationError, match="recovery"):
        fixup(engine, run)
    assert journal.load(run["id"])["state"] == State.RECOVERY_REQUIRED
    assert len(journal.load(run["id"])["service_obligations"]) == 1
    env.data["faults"] = {}
    result = engine.recover_services(run["id"])
    assert result["state"] == State.STOPPED
    assert not result["service_obligations"]
    assert all(n["service"] == "running" for n in env.data["destination"]["nodes"])


def test_missing_run_marker_never_starts_someone_elses_node(rig):
    engine, env, journal = rig
    run = verified(engine)
    node = "dst-1"
    run["service_obligations"][node] = {"initial": "running", "restart_required": True}
    journal.save(run, "crash_fixture")
    env.data["destination"]["nodes"][0]["service"] = "stopped"
    with pytest.raises(MigrationError, match="recovery"):
        engine.recover_services(run["id"])
    assert "start_service" not in operations(env)


def test_catalog_loss_holds_restart_and_keeps_backup(rig, monkeypatch):
    engine, env, journal = rig
    run = verified(engine)
    original = env.fixup

    def corrupt(node, bucket):
        original(node, bucket)
        env.data["destination"]["buckets"] = [
            b for b in env.data["destination"]["buckets"] if b["id"] != bucket.id
        ]

    monkeypatch.setattr(env, "fixup", corrupt)
    with pytest.raises(MigrationError, match="recovery"):
        fixup(engine, run)
    state = journal.load(run["id"])
    assert state["state"] == State.RECOVERY_REQUIRED
    assert next(iter(state["service_obligations"].values()))["catalog_before"]
    assert "start_service" not in operations(env)


def test_retention_that_would_delete_history_blocks_before_stop(rig):
    engine, env, _ = rig
    for cluster in ("source", "destination"):
        env.data[cluster]["partitions"][0]["retention_seconds"] = 86400
    run = verified(engine)
    with pytest.raises(MigrationError, match="retention"):
        fixup(engine, run)
    assert "stop_service" not in operations(env)


def test_fixup_before_new_migration_with_existing_equal_data(rig):
    engine, env, _ = rig
    for bucket in env.data["source"]["buckets"]:
        if bucket["status"] != "active":
            env.data["destination"]["buckets"].append({**bucket, "node": "dst-1"})
    env.data["destination_events"].extend(copy.deepcopy(env.data["source_events"]))
    source, _ = env.discover()
    sealed = [b for b in source.buckets if b.status != "active"]
    run = engine.plan(
        Window(min(b.start for b in sealed), max(b.end for b in sealed) + 1), fixup_existing=True
    )
    assert fixup(engine, run)["fixup"]["outcome"] == "complete"
    with pytest.raises(MigrationError, match="transition"):
        execute(engine, run)
    assert "start" not in operations(env)


def test_fixup_never_interrupts_foreign_task(rig):
    engine, env, _ = rig
    run = verified(engine)
    env.data["task"]["status"] = "running"
    env.data["complete_after_polls"] = 1000
    with pytest.raises(MigrationError, match="busy"):
        fixup(engine, run)
    assert "stop_service" not in operations(env)


def test_remote_recovery_model_survives_controller_loss(rig):
    engine, env, journal = rig
    run = planned(engine)
    node = "dst-1"
    run["service_obligations"][node] = {"initial": "running", "restart_required": True}
    journal.save(run, "service_stop_intent")
    env.arm_recovery(node, run["id"])
    env.stop_service(node)
    env.controller_lost()  # No Engine finally block runs in this test.
    assert all(env.readiness(node))
    assert engine.recover_services(run["id"])["service_obligations"] == {}


def test_named_partition_fixup_preserves_existing_catalog_and_sets_creation_time(rig):
    engine, env, _ = rig
    partition = {
        "name": "secops",
        "id": "secops",
        "criteria": 'appname="vpxa"',
        "retention_seconds": 0,
        "order": 1,
    }
    for cluster in ("source", "destination"):
        env.data[cluster]["partitions"].append(dict(partition))
    for b in env.data["source"]["buckets"]:
        b["partition"] = "secops"
    run = verified(engine)
    assert all(b["partition"] == "" for b in env.data["destination"]["buckets"])
    fixup(engine, run)
    assert all(
        b["partition"] == "secops" and b["created"] == b["end"]
        for b in env.data["destination"]["buckets"]
    )
    assert engine.verify(run["id"])["state"] == State.VERIFIED
