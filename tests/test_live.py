"""Live adapter codecs against recorded shapes, the trust store, credential sources and the
self-test runner. No network: transports and SSH reads are recorded."""

from __future__ import annotations

import json
import os
from contextlib import nullcontext

import pytest

from logs_migrate.adapters import live
from logs_migrate.adapters.live import (
    Connection,
    LiveEnvironment,
    decode_task,
    observed_keys,
    parse_listing,
    parse_members,
    parse_partitions,
    parse_space,
    parse_usage,
    partition_models,
)
from logs_migrate.adapters.rest import Logs90, RecordedTransport, parse_version
from logs_migrate.adapters.ssh import ReadOperation
from logs_migrate.engine import Engine, day_window, partial_days
from logs_migrate.errors import Code, MigrationError
from logs_migrate.models import DAY, Window
from logs_migrate.security import Credentials, Secret
from logs_migrate.trust import Identity, TrustRequired, TrustStore

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"

LISTING = "\n".join(
    [
        "Buckets:",
        f"id={A}, status=archived, partition=, created=2026-07-12T19:44:57, size=104857600, "
        "messages=874099, startTime=2026-06-01T00:00:00, endTime=2026-06-14T23:59:59",
        f"id={B}, status=archived, partition=d82bb6e4-bcaf-305b-9b55-30924b7f9cb6, "
        "created=1783600000000, size=20971520, messages=65207, startTime=1780000000000, "
        "endTime=1781000000000",
        f"id={C}, status=active, partition=, created=2026-11-01T10:00:00, size=0, messages=0",
        "",
    ]
)


def test_listing_parser_reads_dates_in_both_shapes_and_keeps_key_names_only():
    buckets = parse_listing(LISTING)
    assert [b.id for b in buckets] == [A, B, C]
    first, second, third = buckets
    assert first.status == "archived" and first.messages == 874099 and first.size == 104857600
    from datetime import UTC, datetime

    def ms(text):
        return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)

    assert first.start == ms("2026-06-01T00:00:00") and first.end == ms("2026-06-14T23:59:59")
    assert first.created == ms("2026-07-12T19:44:57") and first.partition == ""
    assert second.start == 1780000000000 and second.end == 1781000000000
    assert second.partition == "d82bb6e4-bcaf-305b-9b55-30924b7f9cb6"
    assert third.status == "active" and third.start is None and third.end is None
    assert "starttime" in first.keys and all("874099" not in k for k in first.keys)


@pytest.mark.parametrize(
    "line",
    [
        f"Bucket{{id={A}, status=ARCHIVED, startTimeMillis=1, endTimeMillis=2, messageCount=3}}",
        f"id: {A}, state: archived, from: 1, to: 2, count: 3",
    ],
)
def test_listing_parser_accepts_alias_keys(line):
    (bucket,) = parse_listing(line)
    assert bucket.id == A and bucket.status == "archived"
    assert (bucket.start, bucket.end, bucket.messages) == (1000, 2000, 3)


def test_listing_without_status_or_uuid_is_ignored_not_guessed():
    assert parse_listing("id=not-a-uuid, status=archived\nstatus=archived\n") == []


def test_usage_space_and_members_parsers():
    store = "/storage/core/loginsight/cidata/store"
    assert parse_usage(f"123\t{store}/{A}\n456 {store}/{B}\nnonsense\n") == {A: 123, B: 456}
    assert parse_space("Size Used Avail\n100 40 60\n") == (100, 40, 60)
    assert parse_space("garbage") == (None, None, None)
    xml = (
        '<config><distributed enabled="true"><daemon host="Node-1.lab" port="16520" token="s"/>'
        '<daemon host="node-2.lab" port="16520" token="s"/><daemon host="node-1.lab"/>'
        '</distributed><other host="not-a-member"/></config>'
    )
    assert parse_members(xml, "typed") == ["node-1.lab", "node-2.lab"]
    assert parse_members("<config/>", "typed") == ["typed"]


def test_partition_models_normalise_default_and_keys():
    rules = parse_partitions(
        {
            "partitions": [
                {"id": "", "name": "", "retentionPeriodSeconds": 0},
                {
                    "id": "p",
                    "name": "secops",
                    "routingFilter": 'appname="Vpxa"',
                    "retentionPeriodSeconds": 9504000,
                },
            ]
        }
    )
    models = partition_models(rules)
    assert [p.name for p in models] == ["Default", "secops"]
    assert models[1].criteria == 'appname="Vpxa"' and models[1].retention_seconds == 9504000
    with pytest.raises(MigrationError, match="schema"):
        parse_partitions({"partitions": "no"})


def test_version_parser_accepts_build_suffixes():
    assert parse_version({"releaseName": "GA", "version": "8.18.7-25423541"}) == "8.18.7"
    assert parse_version({"productVersion": "9.0.2.0200.25430789"}) == "9.0.2.0200"
    with pytest.raises(MigrationError, match="schema"):
        parse_version({"releaseName": "GA"})


def test_task_decoder_handles_empty_nested_list_and_node_shapes():
    assert decode_task({}, destination="d", now_ms=0).status == "idle"
    assert decode_task([], destination="d", now_ms=0).status == "idle"
    body = {
        "dataMigration": {
            "taskId": "t-1",
            "status": "IN_PROGRESS",
            "hostName": "src.lab",
            "bucketStartTimeMillis": 1000,
            "bucketEndTimeMillis": 2000,
            "startTime": 1_700_000_000_000,
            "totalBytes": 500,
            "nodes": [
                {
                    "host": "dst-1.lab",
                    "status": "COMPLETED",
                    "copiedBuckets": [A],
                    "bytesTransferred": 300,
                },
                {
                    "host": "dst-2.lab",
                    "status": "RUNNING",
                    "skippedBuckets": [{"id": B}],
                    "transferredBytes": 50,
                },
            ],
        }
    }
    task = decode_task(body, destination="dst.lab", now_ms=1_700_000_060_000)
    assert task.id == "t-1" and task.status == "running" and task.source == "src.lab"
    assert task.window == Window(1000, 2000)
    assert task.completed == (A,) and task.skipped == (B,)
    assert task.bytes_transferred == 350 and task.total_bytes == 500
    assert task.elapsed_seconds == 60.0
    assert [n.node for n in task.nodes] == ["dst-1.lab", "dst-2.lab"]
    assert task.nodes[1].status == "running"
    # Several parts: any part still running keeps the task running; all done is done.
    mixed = decode_task(
        [{"status": "RUNNING"}, {"status": "Completed", "id": 7}], destination="d", now_ms=0
    )
    assert mixed.status == "running" and mixed.id == "7"
    done = decode_task(
        [{"status": "COMPLETED"}, {"status": "COMPLETED"}], destination="d", now_ms=0
    )
    assert done.status == "completed"
    failed = decode_task([{"status": "FAILED"}, {"status": "COMPLETED"}], destination="d", now_ms=0)
    assert failed.status == "failed"
    nested = decode_task(
        {"nodes": [{"host": "a", "status": "COMPLETED"}, {"host": "b", "status": "RUNNING"}]},
        destination="d",
        now_ms=0,
    )
    assert nested.status == "running"
    walked = decode_task(
        {"details": {"x": {"status": "COMPLETED"}, "y": {"status": "RUNNING"}}},
        destination="d",
        now_ms=0,
    )
    assert walked.status == "running"
    top_done = decode_task(
        {"status": "COMPLETED", "nodes": [{"host": "a", "status": "RUNNING"}]},
        destination="d",
        now_ms=0,
    )
    assert top_done.status == "running"
    odd = decode_task({"id": "x", "phase": "wobbling"}, destination="d", now_ms=0)
    assert odd.status == "unknown"
    assert (
        decode_task({"inProgress": True, "status": None}, destination="d", now_ms=0).status
        == "unknown"
    )
    negative = decode_task(
        {"status": "COMPLETED", "notCopied": [B], "copied": [A]}, destination="d", now_ms=0
    )
    assert negative.completed == (A,) and negative.skipped == (B,)
    only_negative = decode_task(
        {"status": "COMPLETED", "notMigratedBuckets": [B]}, destination="d", now_ms=0
    )
    assert only_negative.completed == () and only_negative.skipped == (B,)
    for key in (
        "bucketsNotMigrated",
        "bucketsNotCopied",
        "bucketsNotTransferred",
        "bucketsUnmigrated",
        "bucketsUncopied",
        "bucketsUntransferred",
    ):
        camel = decode_task({"status": "COMPLETED", key: [B]}, destination="d", now_ms=0)
        assert camel.completed == () and camel.skipped == (B,)
    two = decode_task(
        {"status": "COMPLETED", "copied": [A, B, C], "skippedBuckets": [B], "failedBuckets": [C]},
        destination="d",
        now_ms=0,
    )
    assert two.completed == (A,) and set(two.skipped) == {B, C}
    assert observed_keys(body)[:2] == ["dataMigration", "dataMigration.taskId"]


def test_trust_store_enrolls_once_and_refuses_changes(tmp_path):
    store = TrustStore(tmp_path / "cfg")
    tls = Identity("tls", "logs.lab:9543", "a" * 64)
    assert store.check(tls) == "unknown"
    store.trust(tls)
    assert store.check(tls) == "trusted"
    assert store.check(Identity("tls", "logs.lab:9543", "b" * 64)) == "changed"
    with pytest.raises(MigrationError, match=Code.AUTH):
        store.trust(Identity("tls", "logs.lab:9543", "b" * 64))
    ssh = Identity("ssh", "node.lab:22", "c" * 64, "ssh-ed25519")
    store.trust(ssh, "node.lab ssh-ed25519 AAAA")
    assert (tmp_path / "cfg" / "known_hosts").read_text() == "node.lab ssh-ed25519 AAAA\n"
    if os.name == "posix":
        assert (tmp_path / "cfg" / "trusted-identities.json").stat().st_mode & 0o077 == 0
    again = TrustStore(tmp_path / "cfg")
    assert again.entries() == [("ssh", "node.lab:22", "c" * 64), ("tls", "logs.lab:9543", "a" * 64)]
    assert again.forget("ssh", "node.lab:22") and not again.forget("ssh", "node.lab:22")
    assert (tmp_path / "cfg" / "known_hosts").read_text() == ""
    assert "c" * 64 not in tls.display and "AA:AA" not in tls.display
    assert tls.display.startswith("TLS logs.lab:9543 SHA256 aa:aa:")


def test_credentials_come_from_environment_or_private_file_never_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGS_SOURCE_API_PASSWORD", "env-canary")
    credentials = Credentials(interactive=False)
    assert credentials.has("source_api") and credentials.get("source_api").reveal() == "env-canary"
    assert not credentials.has("ops_token")
    with pytest.raises(MigrationError, match=Code.AUTH):
        credentials.get("ops_token")
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"source_ssh": "file-canary"}))
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(MigrationError, match=Code.AUTH):
            Credentials.from_file(path)
        path.chmod(0o600)
    loaded = Credentials.from_file(path)
    assert loaded.get("source_ssh").reveal() == "file-canary"
    assert "canary" not in repr(loaded.get("source_ssh"))
    path.write_text(json.dumps({"argv_password": "x"}))
    with pytest.raises(MigrationError, match=Code.AUTH):
        Credentials.from_file(path)


# A LiveEnvironment whose network is recorded: TLS/SSH probes, REST transports and SSH reads.


def session():
    return {"method": "POST", "path": "/api/v1/sessions", "response": {"sessionId": "s"}}


def record(method, path, response):
    return {"method": method, "path": path, "response": response}


class FakeSSH:
    """Replies keyed by (host, operation); every command is a fixed read."""

    def __init__(self, replies):
        self.replies = replies
        self.reads = []

    def client(self, host):
        env = self

        class Client:
            def read(self, operation):
                env.reads.append((host, operation))
                try:
                    return env.replies[(host, operation)]
                except KeyError:
                    raise MigrationError(Code.CONNECTION) from None

        return Client()


@pytest.fixture
def rigged(tmp_path, monkeypatch):
    """A live environment with recorded TLS, SSH and REST; returns env plus the transports."""
    store = TrustStore(tmp_path / "cfg")
    pins = {"src.lab": "a" * 64, "dst.lab": "c" * 64}
    monkeypatch.setattr(
        live, "probe_tls", lambda host, port: Identity("tls", f"{host}:{port}", pins[host])
    )
    monkeypatch.setattr(
        live,
        "probe_ssh",
        lambda host, port=22, timeout=15: (
            Identity("ssh", f"{host}:{port}", "b" * 64, "ssh-ed25519"),
            f"{host} ssh-ed25519 AAAA",
        ),
    )
    xml = '<distributed><daemon host="src-1.lab"/><daemon host="src-2.lab"/></distributed>'
    ssh = FakeSSH(
        {
            ("src.lab", ReadOperation.MEMBERS): xml,
            ("src-1.lab", ReadOperation.SERVICE): "active\n",
            ("src-1.lab", ReadOperation.SPACE): "Size Used Avail\n1000 100 900\n",
            ("src-1.lab", ReadOperation.INVENTORY): LISTING,
            ("src-1.lab", ReadOperation.USAGE): f"5000 /storage/core/loginsight/cidata/store/{A}\n",
            ("src-2.lab", ReadOperation.SERVICE): "active\n",
            ("src-2.lab", ReadOperation.SPACE): "Size Used Avail\n1000 100 900\n",
            ("src-2.lab", ReadOperation.INVENTORY): "",
            ("src-2.lab", ReadOperation.USAGE): "",
        }
    )
    source = RecordedTransport([])
    destination = RecordedTransport([])
    source.identity = "a" * 64
    destination.identity = "c" * 64

    def https(self, endpoint):
        return source if endpoint[0] == "src.lab" else destination

    monkeypatch.setattr(LiveEnvironment, "_https", https)
    monkeypatch.setattr(
        LiveEnvironment, "_ssh", lambda self, host, credential, username: ssh.client(host)
    )
    monkeypatch.setattr(LiveEnvironment, "exclusive", lambda self: nullcontext())
    credentials = Credentials(
        {"source_api": "x", "source_ssh": "y", "destination_api": "z"}, interactive=False
    )
    connection = Connection("src.lab", "dst.lab:9543")
    env = LiveEnvironment(connection, credentials, store)
    return env, source, destination, ssh, store


def test_open_requires_trust_for_every_identity_then_discovers_members(rigged):
    env, source, destination, ssh, store = rigged
    with pytest.raises(TrustRequired) as first:
        env.open()
    assert sorted(i.name for i in first.value.identities) == [
        "dst.lab:9543",
        "src.lab:22",
        "src.lab:9543",
    ]
    assert ssh.reads == []  # Nothing is read before the operator trusts the identities.
    for identity in first.value.identities:
        store.trust(identity, env.key_line(identity))
    with pytest.raises(TrustRequired) as second:
        env.open()
    assert [i.name for i in second.value.identities] == ["src-1.lab:22", "src-2.lab:22"]
    for identity in second.value.identities:
        store.trust(identity, env.key_line(identity))
    assert env.open() is env and env.members == ["src-1.lab", "src-2.lab"]


def trusted(rigged):
    env, source, destination, ssh, store = rigged
    store.trust(Identity("tls", "src.lab:9543", "a" * 64))
    store.trust(Identity("tls", "dst.lab:9543", "c" * 64))
    for name in ("src.lab:22", "src-1.lab:22", "src-2.lab:22"):
        store.trust(Identity("ssh", name, "b" * 64, "ssh-ed25519"), "x ssh-ed25519 AAAA")
    env.open()
    return env, source, destination, ssh


def test_discover_builds_clusters_from_rest_and_ssh_reads(rigged):
    env, source, destination, ssh = trusted(rigged)
    source.records += [
        session(),
        record("GET", "/api/v1/version", {"version": "8.18.7-25423541"}),
        record("GET", "/api/v1/partitioning", {"partitions": [{"id": "", "name": "Default"}]}),
    ]
    destination.records += [
        session(),
        record("GET", "/api/v1/version", {"version": "9.0.2.0200.25430789"}),
        record("GET", "/api/v1/partitioning", {"partitions": [{"id": "", "name": "Default"}]}),
        record("GET", "/api/v2/data-migrations", {}),
    ]
    src, dst = env.discover()
    assert src.version == "8.18.7" and [n.id for n in src.nodes] == ["src-1.lab", "src-2.lab"]
    assert src.nodes[0].repository_available == 900 and src.identity == "a" * 64
    by_id = {b.id: b for b in src.buckets}
    assert by_id[A].size == 5000 and by_id[A].messages == 874099 and by_id[A].bounds_proven
    assert by_id[B].size == 20971520 and by_id[C].status == "active" and not by_id[C].complete
    assert dst.version == "9.0.2.0200" and [n.id for n in dst.nodes] == ["dst.lab"]
    assert dst.nodes[0].repository_available is None and dst.buckets == ()
    assert env.notes["source_listing_keys"]


def test_counts_recent_ingest_and_history_use_the_documented_routes(rigged):
    env, source, destination, ssh = trusted(rigged)
    path = (
        f"/api/v1/aggregated-events/timestamp/%3E%3D0/timestamp/%3C{DAY}"
        f"?bin-width={DAY}&timeout=580000"
    )
    source.records += [
        session(),
        record("GET", path, {"complete": True, "bins": [{"minTimestamp": 0, "value": 4}]}),
    ]
    counts = env.count("source", Window(0, DAY), ())
    assert counts.daily == (("1970-01-01", 4),) and counts.complete and counts.identities == ()
    assert env.count("ingest", Window(0, DAY), ()).daily == ()
    destination.records += [
        session(),
        record(
            "GET",
            "/api/v2/data-migrations",
            {
                "status": "COMPLETED",
                "bucketStartTimeMillis": 0,
                "bucketEndTimeMillis": 5,
                "copied": [A],
            },
        ),
    ]
    (history,) = env.history()
    assert history.bucket_ids == (A,) and history.window == Window(0, 5) and history.complete


def test_start_falls_back_to_a_running_task_when_status_lags(rigged):
    env, source, destination, ssh = trusted(rigged)
    destination.records += [
        session(),
        record("GET", "/api/v2/data-migrations", {}),
        record("POST", "/api/v2/data-migrations", {"accepted": True}),
        record("GET", "/api/v2/data-migrations", {}),
    ]
    task = env.start(Window(1, 2), ("src-1.lab/" + A,))
    assert task.status == "running" and task.window == Window(1, 2) and task.id == ""


def test_start_does_not_adopt_the_previous_task_with_the_same_window(rigged):
    env, source, destination, ssh = trusted(rigged)
    previous = {
        "id": "task-1",
        "status": "COMPLETED",
        "bucketStartTimeMillis": 1,
        "bucketEndTimeMillis": 2,
        "copied": [A],
    }
    destination.records += [
        session(),
        record("GET", "/api/v2/data-migrations", previous),
        record("POST", "/api/v2/data-migrations", {"accepted": True}),
        record("GET", "/api/v2/data-migrations", previous),
    ]
    task = env.start(Window(1, 2), ("src-1.lab/" + A,))
    assert task.status == "running" and task.id == "" and task.completed == ()
    destination.records += [
        record("GET", "/api/v2/data-migrations", previous),
        record("POST", "/api/v2/data-migrations", {"accepted": True}),
        record("GET", "/api/v2/data-migrations", {**previous, "id": "task-2", "status": "RUNNING"}),
    ]
    task = env.start(Window(1, 2), ("src-1.lab/" + A,))
    assert task.id == "task-2" and task.status == "running"


def test_selftest_reports_every_read_and_sends_nothing(rigged):
    from logs_migrate import selftest

    env, source, destination, ssh = trusted(rigged)
    count = lambda: {"complete": True, "bins": []}  # noqa: E731
    source.records += [
        session(),
        record("GET", "/api/v1/version", {"version": "8.18.7-1"}),
        record("GET", "/api/v1/partitioning", {"partitions": []}),
        record("GET", "/api/v1/version", {"version": "8.18.7-1"}),
        record("GET", "/api/v1/partitioning", {"partitions": []}),
    ]
    destination.records += [
        session(),
        record("GET", "/api/v1/version", {"version": "9.0.2.0200"}),
        record("GET", "/api/v1/partitioning", {"partitions": []}),
        record("GET", "/api/v2/data-migrations", {}),
        record("GET", "/api/v1/version", {"version": "9.0.2.0200"}),
        record("GET", "/api/v1/partitioning", {"partitions": []}),
        record("GET", "/api/v2/data-migrations", {}),
    ]

    def any_counts(method, path, body=None, headers=None, timeout=None):
        if "aggregated-events" in path:
            assert timeout == 600.0
            return count()
        return RecordedTransport.request(source, method, path, body, headers)

    source.request = any_counts
    destination_request = destination.request

    def destination_counts(method, path, body=None, headers=None, timeout=None):
        if "aggregated-events" in path:
            return count()
        return destination_request(method, path, body, headers)

    destination.request = destination_counts
    result = selftest.run(env, Window(1780000000000, 1782000000000))
    names = [s.name for s in result.steps]
    assert "Source API version" in names and "Plan preview (no request sent)" in names
    failed = [(s.name, s.detail) for s in result.steps if s.status == "fail"]
    assert failed == [], failed
    assert result.passed and "All checks passed" in result.text()
    assert not any(m != "GET" and p != "/api/v1/sessions" for m, p in destination.calls)
    data = result.data()
    assert data["summary"]["fail"] == 0 and "status_response_keys" in data["notes"]
    assert "canary" not in json.dumps(data)


def test_engine_live_path_verifies_covered_days_without_attribution(tmp_path):
    """The simulator posing as a live environment: no identities, per-day counts only."""
    from conftest import FIXTURES

    from logs_migrate.adapters.simulated import SimulatedClock, SimulatedEnvironment
    from logs_migrate.journal import Journal
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    env = SimulatedEnvironment(json.loads((FIXTURES / "sealed-populated.fixture").read_text()))
    env.mode = "live"
    env.attribution = False
    clock = SimulatedClock()
    engine = Engine(env, Journal(tmp_path / "j.sqlite", clock), clock)
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    # The open bucket has no bounds, so the sealed frontier (newest sealed end, Mar 3) starts
    # the partial tail; Mar 1 and Mar 2 are fully covered days.
    assert run["plan"]["partial_days"] == ["2026-03-03"]
    started = engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    assert started["task"]["status"] == "running"
    assert engine.status()["status"] == "completed"
    result = engine.verify(run["id"])
    # New ingest landed on Mar 1, a fully covered day; counts cannot attribute it, so the
    # honest answer is a discrepancy on that day, never a pass.
    assert result["outcome"] == "verified_discrepancy"
    assert [d["difference"] for d in result["daily"]] == [1, 0, 0]
    assert [d["partial"] for d in result["daily"]] == [False, False, True]
    assert result["buckets"]["covered"] and not result["buckets"]["not_received"]
    assert "not attributed" in result["method"]


def test_engine_live_path_passes_only_on_fully_covered_days_that_match(tmp_path):
    from conftest import FIXTURES

    from logs_migrate.adapters.simulated import SimulatedClock, SimulatedEnvironment
    from logs_migrate.journal import Journal
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    data = json.loads((FIXTURES / "sealed-populated.fixture").read_text())
    data["ingest_during_run"] = []
    env = SimulatedEnvironment(data)
    env.mode = "live"
    env.attribution = False
    clock = SimulatedClock()
    engine = Engine(env, Journal(tmp_path / "j.sqlite", clock), clock)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    run = engine.plan(window, Policy(poll_seconds=0))
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    assert engine.status()["status"] == "completed"
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_success" and result["partial_days"] == ["2026-03-03"]
    assert not result["partial_differences"]
    # Nothing landed (the API said COMPLETED anyway): a discrepancy, never a pass.
    env.data["destination_events"] = [
        e for e in env.data["destination_events"] if e.get("cohort") == "baseline"
    ]
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_discrepancy"


def _live_rig(tmp_path, data_changes=None):
    from conftest import FIXTURES

    from logs_migrate.adapters.simulated import SimulatedClock, SimulatedEnvironment
    from logs_migrate.journal import Journal

    data = json.loads((FIXTURES / "sealed-populated.fixture").read_text())
    data["ingest_during_run"] = []
    data.update(data_changes or {})
    env = SimulatedEnvironment(data)
    env.mode = "live"
    env.attribution = False
    clock = SimulatedClock()
    return env, Engine(env, Journal(tmp_path / "j.sqlite", clock), clock)


def test_live_ledger_records_a_skipped_bucket_as_not_received_without_a_copied_list(tmp_path):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.ports import Transfer

    env, engine = _live_rig(tmp_path)
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    destination = run["plan"]["destination"]["id"]
    # The API's finished task names only what it skipped (the documented disk-limit trap).
    first = "00000000-0000-0000-0000-000000000001"  # A fixture bucket, listed as skipped.
    env.transfer = lambda: Transfer("sim-1", "completed", (), (first,), destination=destination)
    engine.status()
    (row,) = engine.journal.transfers(destination)
    assert row["closed"] and first not in {k.rsplit("/", 1)[-1] for k in row["received"]}
    assert len(row["received"]) == 2
    result = engine.verify(run["id"])
    assert result["buckets"]["not_received"] == ["src-1/" + first]
    assert (
        result["outcome"] != "verified_success"
        and result["reason"] == "selected_buckets_not_received"
    )


def test_live_ledger_stays_open_while_status_still_shows_the_previous_task(tmp_path):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.ports import Transfer

    env, engine = _live_rig(tmp_path)
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    destination = run["plan"]["destination"]["id"]
    stale = Transfer("old", "completed", (), (), destination=destination, window=Window(1, 2))
    env.transfer = lambda: stale
    engine.status()
    (row,) = engine.journal.transfers(destination)
    assert not row["closed"], "a status for another window must not settle this selection"
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(
            Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
            Policy(poll_seconds=0),
        )
    # A finished transfer with a window but no bucket list (started from the product UI)
    # blocks anything overlapping its window.
    from logs_migrate.ports import History

    env.transfer = lambda: Transfer("", "idle", destination=destination)
    env.history = lambda: (
        History(
            "src",
            destination,
            Window(parse_time("2026-03-02T00:00:00Z"), parse_time("2026-03-02T12:00:00Z")),
            ("dst-1",),
            (),
            True,
        ),
    )
    engine.journal.settle_transfer(run["id"], [])
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(
            Window(parse_time("2026-03-02T00:00:00Z"), parse_time("2026-03-03T00:00:00Z")),
            Policy(poll_seconds=0),
        )


def _ready_run(engine, window):
    from logs_migrate.models import ACTIVE_ACK, Policy

    run = engine.plan(window, Policy(poll_seconds=0))
    return run, lambda: engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)


class Wire:
    """Stands in for the TLS socket: answers each HTTPS request from a script."""

    def __init__(self, script):
        self.script = list(script)

    def __call__(self, host, port, timeout=None, context=None):
        wire = self

        class Response:
            def __init__(self, status, body):
                self.status = status
                self.raw = (
                    body
                    if isinstance(body, bytes)
                    else b""
                    if body is None
                    else json.dumps(body).encode()
                )

            def read(self, limit):
                return self.raw

        class Connection:
            sock = type("Socket", (), {"getpeercert": staticmethod(lambda binary_form: b"cert")})()

            def connect(self):
                pass

            def request(self, method, path, body=None, headers=None):
                self.sent = (method, path)

            def getresponse(self):
                method, path, outcome = wire.script.pop(0)
                assert (method, path) == self.sent
                if isinstance(outcome, Exception):
                    raise outcome
                return Response(*outcome)

            def close(self):
                pass

        return Connection()


MIGRATIONS = "/api/v2/data-migrations"


def _wired(tmp_path, rigged, monkeypatch, script):
    """The engine over the real HTTPS client and live start/status, answering from `script`."""
    import hashlib

    from logs_migrate.adapters.rest import HTTPS

    live_env = trusted(rigged)[0]
    env, engine = _live_rig(tmp_path)
    monkeypatch.setattr("http.client.HTTPSConnection", Wire(script))
    live_env.destination_client.transport = HTTPS(
        "https://dst.lab:9543", certificate_sha256=hashlib.sha256(b"cert").hexdigest()
    )
    env.start, env.transfer = live_env.start, live_env.transfer
    return env, engine


MARCH = Window(1772323200000, 1775001600000)
SESSION = ("POST", "/api/v1/sessions", (200, {"sessionId": "s"}))
IDLE = ("GET", MIGRATIONS, (200, {}))


def test_a_start_the_appliance_answered_with_500_is_not_held_and_the_plan_can_be_retried(
    tmp_path, rigged, monkeypatch
):
    from logs_migrate.models import Policy

    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [SESSION, IDLE, IDLE, IDLE, ("POST", MIGRATIONS, (500, None)), IDLE],
    )
    run, start = _ready_run(engine, MARCH)
    with pytest.raises(MigrationError) as raised:
        start()
    assert raised.value.code == Code.CONNECTION
    assert engine.journal.transfers(run["plan"]["destination"]["id"]) == []
    engine.plan(MARCH, Policy(poll_seconds=0))


def test_a_start_with_no_answer_is_released_once_the_appliance_shows_no_task(
    tmp_path, rigged, monkeypatch
):
    from logs_migrate.models import Policy

    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [SESSION, IDLE, IDLE, IDLE, ("POST", MIGRATIONS, TimeoutError()), IDLE],
    )
    run, start = _ready_run(engine, MARCH)
    destination = run["plan"]["destination"]["id"]
    with pytest.raises(MigrationError) as raised:
        start()
    assert raised.value.code == Code.UNCERTAIN
    (row,) = engine.journal.transfers(destination)
    assert not row["closed"]
    engine.plan(MARCH, Policy(poll_seconds=0))
    assert engine.journal.transfers(destination) == []


@pytest.mark.parametrize("status_read", [(503, None), (200, {})])
def test_an_accepted_start_stays_held_while_the_status_shows_no_task(
    tmp_path, rigged, monkeypatch, status_read
):
    from logs_migrate.models import Policy

    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [
            SESSION,
            IDLE,
            IDLE,
            IDLE,
            ("POST", MIGRATIONS, (200, {"accepted": True})),
            ("GET", MIGRATIONS, status_read),
            IDLE,
        ],
    )
    run, start = _ready_run(engine, MARCH)
    destination = run["plan"]["destination"]["id"]
    started = start()
    assert started["task"]["status"] == "running"
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(MARCH, Policy(poll_seconds=0))
    (row,) = engine.journal.transfers(destination)
    assert not row["closed"]


def test_an_accepted_start_with_an_undecodable_body_stays_held(tmp_path, rigged, monkeypatch):
    from logs_migrate.models import Policy

    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [SESSION, IDLE, IDLE, IDLE, ("POST", MIGRATIONS, (200, [1])), IDLE, IDLE],
    )
    run, start = _ready_run(engine, MARCH)
    assert start()["task"]["status"] == "running"
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(MARCH, Policy(poll_seconds=0))
    (row,) = engine.journal.transfers(run["plan"]["destination"]["id"])
    assert not row["closed"]


def test_an_accepted_start_with_a_non_json_body_stays_held(tmp_path, rigged, monkeypatch):
    from logs_migrate.models import Policy

    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [SESSION, IDLE, IDLE, IDLE, ("POST", MIGRATIONS, (202, b"Accepted")), IDLE, IDLE],
    )
    run, start = _ready_run(engine, MARCH)
    assert start()["task"]["status"] == "running"
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(MARCH, Policy(poll_seconds=0))
    (row,) = engine.journal.transfers(run["plan"]["destination"]["id"])
    assert not row["closed"]


def test_an_accepted_start_stays_held_when_the_journal_fails_after_it(
    tmp_path, rigged, monkeypatch
):
    from logs_migrate.models import Policy

    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [
            SESSION,
            IDLE,
            IDLE,
            IDLE,
            ("POST", MIGRATIONS, (200, {"accepted": True})),
            IDLE,
            IDLE,
        ],
    )
    run, start = _ready_run(engine, MARCH)
    audit = engine._audit

    def failing(run, event, details=None):
        if event == "api_start_response":
            raise MigrationError(Code.STORAGE)
        return audit(run, event, details)

    engine._audit = failing
    with pytest.raises(MigrationError) as raised:
        start()
    assert raised.value.code == Code.UNCERTAIN
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(MARCH, Policy(poll_seconds=0))
    (row,) = engine.journal.transfers(run["plan"]["destination"]["id"])
    assert not row["closed"]


def test_a_failed_acknowledgment_write_still_reports_the_start_as_uncertain(
    tmp_path, rigged, monkeypatch
):
    import sqlite3

    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [SESSION, IDLE, IDLE, IDLE, ("POST", MIGRATIONS, (200, {"accepted": True})), IDLE],
    )
    run, start = _ready_run(engine, MARCH)
    database = engine.journal.db

    class Failing:
        def execute(self, sql, *args):
            if sql.startswith("UPDATE transfers"):
                raise sqlite3.OperationalError("disk I/O error")
            return database.execute(sql, *args)

    engine.journal.db = Failing()
    with pytest.raises(MigrationError) as raised:
        start()
    assert raised.value.code == Code.UNCERTAIN
    engine.journal.db = database
    (row,) = engine.journal.transfers(run["plan"]["destination"]["id"])
    assert not row["closed"]


def test_a_same_window_rerun_is_not_blocked_by_this_tools_own_finished_task(tmp_path):
    from logs_migrate.models import Policy, parse_time
    from logs_migrate.ports import History, Transfer

    env, engine = _live_rig(tmp_path)
    first = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-02T00:00:00Z"))
    run, start = _ready_run(engine, first)
    start()
    destination = run["plan"]["destination"]["id"]
    (row,) = engine.journal.transfers(destination)
    assert row["task_id"]
    wide = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-03T00:00:00Z"))
    env.transfer = lambda: Transfer(
        row["task_id"], "completed", (), (), destination=destination, window=wide
    )

    def history(task_id):
        return lambda: (History("src", destination, wide, ("dst-1",), (), True, task_id),)

    later = Window(parse_time("2026-03-02T00:00:00Z"), parse_time("2026-03-03T00:00:00Z"))
    env.history = history("someone-else")
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(later, Policy(poll_seconds=0))
    env.history = history(row["task_id"])
    engine.plan(later, Policy(poll_seconds=0))


def test_the_sealed_frontier_names_the_last_fully_sealed_day_when_active_bounds_are_unknown():
    from logs_migrate.models import Bucket, last_sealed_day

    sealed = Bucket(A, "n", "", "archived", 1, 1, DAY, DAY + DAY // 2, None, True, True)
    active = Bucket(C, "n", "", "active", 0, 0, None, None, None, False, False)
    assert last_sealed_day((sealed, active)) == "1970-01-01"
    assert last_sealed_day((active,)) is None
    assert last_sealed_day((sealed,)) is None


def test_partial_days_and_day_window_helpers():
    from logs_migrate.models import Bucket, Cluster, Node

    def bucket(ident, node, start, end, status="archived"):
        return Bucket(
            ident, node, "", status, 1, 1, start, end, None, status != "active", status != "active"
        )

    selected = bucket(A, "n", 0, DAY + 5)
    other = bucket(B, "n", DAY, 3 * DAY)
    cluster = Cluster("c", "8.18.7", (Node("n", 10, 1, 9),), (), (), (selected, other), "i")
    plan = {
        "coverage": {"start": 0, "end": DAY + 6},
        "selected": [{"node": "n", "id": A}],
        "last_fully_sealed_day": None,
    }
    assert partial_days(cluster, plan) == ["1970-01-02"]
    assert day_window(Window(5, DAY + 6)) == Window(0, 2 * DAY)
    active = Bucket(C, "n", "", "active", 0, 0, None, None, None, False, False)
    # Active bounds unknown: the tail starts at the sealed frontier (the selected bucket's
    # end, day 2), so day 1 stays fully covered.
    cluster = Cluster("c", "8.18.7", (Node("n", 10, 1, 9),), (), (), (selected, active), "i")
    assert partial_days(cluster, plan) == ["1970-01-02"]
    assert partial_days(cluster, {**plan, "last_fully_sealed_day": "1970-01-01"}) == ["1970-01-02"]
    # No sealed bucket at all to bound the tail: every day is partial.
    cluster = Cluster("c", "8.18.7", (Node("n", 10, 1, 9),), (), (), (active,), "i")
    assert partial_days(cluster, {**plan, "selected": []}) == ["1970-01-01", "1970-01-02"]


def test_cli_selftest_uses_open_live_and_prints_a_summary(monkeypatch, capsys):
    from logs_migrate import cli, selftest

    class Env:
        pass

    monkeypatch.setattr(cli, "open_live", lambda args: Env())
    monkeypatch.setattr(
        selftest,
        "run",
        lambda env, window, **kw: selftest.SelfTest(
            [selftest.Step("Source API version", "pass", "8.18.7")]
        ),
    )
    assert cli.main(["--source", "src.lab", "--destination", "dst.lab", "selftest"]) == 0
    out = capsys.readouterr().out
    assert "PASS  Source API version" in out and "Read-only" in out
    monkeypatch.setattr(
        selftest,
        "run",
        lambda env, window, **kw: selftest.SelfTest(
            [selftest.Step("Destination API version", "fail", "authentication_failed")]
        ),
    )
    assert cli.main(["--source", "src.lab", "--destination", "dst.lab", "selftest", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_cli_refuses_untrusted_identities_without_a_terminal(monkeypatch, capsys, tmp_path):
    from logs_migrate import cli

    monkeypatch.setenv("LOGS_MIGRATE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("LOGS_SOURCE_SSH_PASSWORD", "canary-ssh")
    monkeypatch.setenv("LOGS_SOURCE_API_PASSWORD", "canary-api")
    monkeypatch.setenv("LOGS_DESTINATION_API_PASSWORD", "canary-dst")
    identity = Identity("tls", "src.lab:9543", "f" * 64)

    def open_(self):
        raise TrustRequired([identity])

    monkeypatch.setattr(LiveEnvironment, "open", open_)
    monkeypatch.setattr("sys.stdin", type("Stdin", (), {"isatty": staticmethod(lambda: False)})())
    code = cli.main(["--source", "src.lab", "--destination", "dst.lab", "discover"])
    assert code == 7
    err = capsys.readouterr().err
    assert "Unknown appliance identity" in err and "canary" not in err
    assert json.loads(err.splitlines()[-1])["error"] == "authentication_failed"
    # With --trust-new the identity is enrolled and the (stubbed) open runs again.
    calls = []

    def open_then_fail(self):
        calls.append(1)
        if len(calls) == 1:
            raise TrustRequired([identity])
        raise MigrationError(Code.CONNECTION)

    monkeypatch.setattr(LiveEnvironment, "open", open_then_fail)
    code = cli.main(["--source", "src.lab", "--destination", "dst.lab", "--trust-new", "discover"])
    assert code == 7 and len(calls) == 2
    assert TrustStore(tmp_path / "cfg").check(identity) == "trusted"
    assert "canary" not in capsys.readouterr().err


def test_logs90_identity_is_the_transport_pin():
    transport = RecordedTransport([])
    assert Logs90(transport, "a", Secret("b")).identity == "recorded"
