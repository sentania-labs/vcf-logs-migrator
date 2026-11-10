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

D = "44444444-4444-4444-4444-444444444444"
# Lines quoted from a real 8.18.7 `bucket-index show` (ids masked): comma separated head,
# space separated fields inside cmd=[...], and cmd=[NULL] for buckets sealed by a restart.
REAL_DATED = (
    f"{{partition=, id={A}, status=archived created=2026-10-21T08:08:59.600+0000 "
    "size=479923834 cmd=[startTime=1780543623050 endTime=1792570139600 messages=1893963 "
    "generation=271]}"
)
REAL_UNDATED = (
    f"{{partition=, id={D}, status=archived created=2026-11-07T00:01:32.162+0000 "
    "size=90158030 cmd=[NULL]}"
)
LISTING = "\n".join(
    [
        REAL_DATED,
        f"{{partition=d82bb6e4-bcaf-305b-9b55-30924b7f9cb6, id={B}, status=archived "
        "created=2026-10-22T08:08:59.600+0000 size=20971520 cmd=[startTime=1780000000000 "
        "endTime=1781000000000 messages=65207 generation=12]}",
        f"{{partition=, id={C}, status=active created=2026-11-08T00:00:00.000+0000 size=0 "
        "cmd=[NULL]}",
        REAL_UNDATED,
        "",
    ]
)


def test_listing_parser_reads_real_8187_lines_and_keeps_key_names_only():
    buckets = parse_listing(LISTING)
    assert [b.id for b in buckets] == [A, B, C, D]
    first, second, third, fourth = buckets
    assert first.status == "archived" and first.partition == ""
    assert (first.size, first.messages) == (479923834, 1893963)
    assert (first.start, first.end) == (1780543623050, 1792570139600)
    assert first.created == 1792570139600
    assert second.partition == "d82bb6e4-bcaf-305b-9b55-30924b7f9cb6"
    assert (second.start, second.end) == (1780000000000, 1781000000000)
    assert third.status == "active" and third.start is None and third.end is None
    assert fourth.status == "archived" and fourth.size == 90158030 and fourth.created
    assert fourth.start is None and fourth.end is None and fourth.messages is None
    assert "starttime" in first.keys and "generation" in first.keys
    assert all("1893963" not in k for k in first.keys)


def test_listing_parser_still_reads_comma_separated_dates_in_both_shapes():
    from datetime import UTC, datetime

    def ms(text):
        return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)

    line = (
        f"id={A}, status=archived, partition=, created=2026-07-12T19:44:57, size=104857600, "
        "messages=874099, startTime=2026-06-01T00:00:00, endTime=2026-06-14T23:59:59"
    )
    (bucket,) = parse_listing(line)
    assert bucket.start == ms("2026-06-01T00:00:00") and bucket.end == ms("2026-06-14T23:59:59")
    assert bucket.created == ms("2026-07-12T19:44:57") and bucket.messages == 874099


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
            def active_usage(self, ids):
                reply = env.replies[(host, ReadOperation.USAGE)]
                return reply.pop(0) if isinstance(reply, list) else reply

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
    assert by_id[A].size == 5000 and by_id[A].messages == 1893963 and by_id[A].bounds_proven
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
    assert task.id == "" and task.status == "running"  # No creation time to bind it safely.


def _run_selftest(rigged, window, growth=None):
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
    env.source_growth = lambda: growth or {"bytes": 0, "nodes": [], "window_seconds": 5}
    return env, destination, selftest.run(env, window)


def test_selftest_reports_every_read_and_sends_nothing(rigged):
    env, destination, result = _run_selftest(rigged, Window(1780000000000, 1782000000000))
    names = [s.name for s in result.steps]
    assert "Source API version" in names and "Plan preview (no request sent)" in names
    failed = [(s.name, s.detail) for s in result.steps if s.status == "fail"]
    assert failed == [], failed
    assert result.passed and "All checks passed" in result.text()
    assert not any(m != "GET" and p != "/api/v1/sessions" for m, p in destination.calls)
    data = result.data()
    assert data["summary"]["fail"] == 0 and "status_response_keys" in data["notes"]
    assert "canary" not in json.dumps(data)


def test_selftest_names_active_bucket_growth_not_event_counts(rigged):
    grew = {"bytes": 2048, "nodes": ["src-1.lab"], "window_seconds": 7}
    _, _, result = _run_selftest(rigged, Window(1780000000000, 1782000000000), grew)
    (step,) = [s for s in result.steps if s.name == "Source ingest (cutover check)"]
    assert step.status == "pass"
    assert step.detail == "active buckets on src-1.lab grew by 2,048 bytes in 7 seconds"
    _, _, quiet = _run_selftest(rigged, Window(1780000000000, 1782000000000))
    (step,) = [s for s in quiet.steps if s.name == "Source ingest (cutover check)"]
    assert step.detail == "no active bucket growth in 5 seconds"


def test_selftest_fails_the_cutover_check_when_growth_cannot_be_observed(rigged):
    unknown = {"bytes": None, "nodes": [], "window_seconds": None}
    _, _, result = _run_selftest(rigged, Window(1780000000000, 1782000000000), unknown)
    (step,) = [s for s in result.steps if s.name == "Source ingest (cutover check)"]
    assert step.status == "fail" and step.detail == Code.INCOMPLETE.value


def test_selftest_plans_without_a_window_despite_the_open_bucket(rigged):
    env, destination, result = _run_selftest(rigged, None)
    (plan,) = [s for s in result.steps if s.name == "Plan preview (no request sent)"]
    assert plan.status == "pass", plan.detail
    assert "active skipped" in plan.detail


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
    assert run["plan"]["partial_days"][0] == "2026-03-03"
    assert "2026-03-02" not in run["plan"]["partial_days"]
    started = engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    assert started["task"]["status"] == "running"
    assert engine.status()["status"] == "completed"
    result = engine.verify(run["id"])
    # New ingest landed on Mar 1, a fully covered day; counts cannot attribute it, so the
    # honest answer is a discrepancy on that day, never a pass.
    assert result["outcome"] == "verified_discrepancy"
    assert [d["difference"] for d in result["daily"]][:3] == [1, 0, 0]
    assert [d["partial"] for d in result["daily"]][:3] == [False, False, True]
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
    assert result["outcome"] == "verified_success" and "2026-03-03" in result["partial_days"]
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
    # The live client names the destination by its TLS pin; the simulated engine side names
    # it dst-cluster. One appliance, one name, as in a real session.
    from dataclasses import replace as _replace

    def _as_simulated(call):
        return lambda *a, **k: _replace(call(*a, **k), destination="dst-cluster")

    env.start, env.transfer = _as_simulated(live_env.start), _as_simulated(live_env.transfer)
    return env, engine


MARCH = Window(1772323200000, 1775001600000)
SESSION = ("POST", "/api/v1/sessions", (200, {"sessionId": "s"}))
IDLE = ("GET", MIGRATIONS, (200, {}))


@pytest.mark.parametrize("status", [400, 404, 409, 500, 501])
def test_a_start_the_appliance_refused_is_not_held_and_the_plan_can_be_retried(
    tmp_path, rigged, monkeypatch, status
):
    from logs_migrate.models import Policy

    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [SESSION, IDLE, IDLE, IDLE, ("POST", MIGRATIONS, (status, None)), IDLE],
    )
    run, start = _ready_run(engine, MARCH)
    with pytest.raises(MigrationError) as raised:
        start()
    assert raised.value.code == Code.CONNECTION
    assert engine.journal.transfers(run["plan"]["destination"]["id"]) == []
    engine.plan(MARCH, Policy(poll_seconds=0))


def test_a_start_with_no_answer_holds_the_window_until_stale_and_absent_from_history(
    tmp_path, rigged, monkeypatch
):
    """One idle observation is not proof the start was lost: the window stays held with the
    uncertain-start reason until 15 minutes have passed and the appliance history shows no
    task covering it."""
    from logs_migrate.models import Policy

    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [SESSION, IDLE, IDLE, IDLE, ("POST", MIGRATIONS, TimeoutError())] + [IDLE] * 12,
    )
    run, start = _ready_run(engine, MARCH)
    destination = run["plan"]["destination"]["id"]
    with pytest.raises(MigrationError) as raised:
        start()
    assert raised.value.code == Code.UNCERTAIN
    (row,) = engine.journal.transfers(destination)
    assert not row["closed"] and not row.get("acknowledged") and row["attempted_at"]
    with pytest.raises(MigrationError, match=Code.UNCERTAIN_START):
        engine.plan(MARCH, Policy(poll_seconds=0))
    (row,) = engine.journal.transfers(destination)
    assert not row["closed"], "an idle status alone must not release an uncertain start"
    engine.clock.sleep(899)
    with pytest.raises(MigrationError, match=Code.UNCERTAIN_START):
        engine.plan(MARCH, Policy(poll_seconds=0))
    engine.clock.sleep(1)
    engine.plan(MARCH, Policy(poll_seconds=0))
    assert [r for r in engine.journal.transfers(destination) if not r["closed"]] == []


def test_an_uncertain_start_whose_task_appears_in_history_settles_instead_of_releasing(
    tmp_path, rigged, monkeypatch
):
    from logs_migrate.models import Policy

    running = (
        "GET",
        MIGRATIONS,
        (
            200,
            {
                "status": "RUNNING",
                "bucketStartTimeMillis": MARCH.start,
                "bucketEndTimeMillis": MARCH.end,
            },
        ),
    )
    done = (
        "GET",
        MIGRATIONS,
        (
            200,
            {
                "status": "COMPLETED",
                "bucketStartTimeMillis": MARCH.start,
                "bucketEndTimeMillis": MARCH.end,
            },
        ),
    )
    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [SESSION, IDLE, IDLE, IDLE, ("POST", MIGRATIONS, TimeoutError())]
        + [running]  # The held plan refuses before reading history: one status read.
        + [done] * 8,
    )
    run, start = _ready_run(engine, MARCH)
    destination = run["plan"]["destination"]["id"]
    with pytest.raises(MigrationError, match=Code.UNCERTAIN):
        start()
    # The lost answer was an accepted start: the status shows it running on our window.
    with pytest.raises(MigrationError, match=Code.UNCERTAIN_START):
        engine.plan(MARCH, Policy(poll_seconds=0))
    engine.clock.sleep(900)
    # Finished on our window: the row settles as usual and the window is overlap-protected.
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(MARCH, Policy(poll_seconds=0))
    (row,) = engine.journal.transfers(destination)
    assert row["closed"] and row["received"]


@pytest.mark.parametrize("status", [408, 502, 503, 504])
def test_a_gateway_answer_after_the_start_holds_the_window_until_the_task_settles_it(
    tmp_path, rigged, monkeypatch, status
):
    from logs_migrate.models import Policy

    window = {
        "bucketStartTimeMillis": MARCH.start,
        "bucketEndTimeMillis": MARCH.end,
    }
    running = ("GET", MIGRATIONS, (200, {"status": "RUNNING", **window}))
    done = ("GET", MIGRATIONS, (200, {"status": "COMPLETED", **window}))
    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [SESSION, IDLE, IDLE, IDLE, ("POST", MIGRATIONS, (status, None))] + [running] + [done] * 8,
    )
    run, start = _ready_run(engine, MARCH)
    destination = run["plan"]["destination"]["id"]
    with pytest.raises(MigrationError) as raised:
        start()
    assert raised.value.code == Code.UNCERTAIN
    (row,) = engine.journal.transfers(destination)
    assert not row["closed"]
    with pytest.raises(MigrationError, match=Code.UNCERTAIN_START):
        engine.plan(MARCH, Policy(poll_seconds=0))
    engine.clock.sleep(900)
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(MARCH, Policy(poll_seconds=0))
    (row,) = engine.journal.transfers(destination)
    assert row["closed"] and row["received"]


def test_an_uncertain_start_is_not_released_by_an_unrelated_finished_task_until_stale(
    tmp_path, rigged, monkeypatch
):
    from logs_migrate.models import Policy

    other = (
        "GET",
        MIGRATIONS,
        (
            200,
            {
                "status": "COMPLETED",
                "id": "other",
                "bucketStartTimeMillis": 1,
                "bucketEndTimeMillis": 2,
                "copied": [A],
            },
        ),
    )
    env, engine = _wired(
        tmp_path,
        rigged,
        monkeypatch,
        [SESSION, IDLE, IDLE, IDLE, ("POST", MIGRATIONS, TimeoutError())] + [other] * 12,
    )
    run, start = _ready_run(engine, MARCH)
    destination = run["plan"]["destination"]["id"]
    with pytest.raises(MigrationError, match=Code.UNCERTAIN):
        start()
    with pytest.raises(MigrationError, match=Code.UNCERTAIN_START):
        engine.plan(MARCH, Policy(poll_seconds=0))
    (row,) = engine.journal.transfers(destination)
    assert not row["closed"]
    engine.clock.sleep(900)
    engine.plan(MARCH, Policy(poll_seconds=0))
    assert [r for r in engine.journal.transfers(destination) if not r["closed"]] == []


def test_a_partial_external_transfer_blocks_overlapping_windows(tmp_path):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.ports import History, Transfer

    env, engine = _live_rig(tmp_path)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    run = engine.plan(window, Policy(poll_seconds=0))
    destination = run["plan"]["destination"]["id"]
    env.transfer = lambda: Transfer("", "idle", destination=destination)
    # Not ours (no journaled task id), finished, lists only what it skipped.
    env.history = lambda: (
        History(
            "src",
            destination,
            Window(window.start, window.start + 1),
            ("dst-1",),
            (),
            False,
            "ext-1",
            True,
        ),
    )
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(window, Policy(poll_seconds=0))
    # An exhaustive received list is matched by bucket id instead; disjoint ids pass.
    env.history = lambda: (
        History(
            "src",
            destination,
            Window(window.start, window.start + 1),
            ("dst-1",),
            (A,),
            False,
            "ext-2",
            False,
        ),
    )
    engine.plan(window, Policy(poll_seconds=0))
    # Our own journaled task with the same shape stays allowed (tail rerun).
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    engine.journal.note_transfer_task(run["id"], "ours")
    env.history = lambda: (
        History("src", destination, window, ("dst-1",), (), False, "ours", True),
    )
    with pytest.raises(MigrationError, match=Code.OVERLAP):
        engine.plan(window, Policy(poll_seconds=0))  # Blocked by the journal row, not history.


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
    early = Bucket(A, "n", "", "archived", 1, 1, DAY, DAY + 3 * 3600 * 1000, None, True, True)
    assert last_sealed_day((early, active)) == "1969-12-31"
    assert last_sealed_day((active,)) is None
    assert last_sealed_day((sealed,)) is None


def test_the_sealed_frontier_is_the_oldest_node_when_a_source_has_several_nodes():
    D4 = "44444444-4444-4444-4444-444444444444"
    from logs_migrate.models import Bucket, Cluster, Node, last_sealed_day

    def sealed(ident, node, end):
        return Bucket(ident, node, "", "archived", 1, 1, 0, end, None, True, True)

    def active(ident, node):
        return Bucket(ident, node, "", "active", 0, 0, None, None, None, False, False)

    fresh = sealed(A, "a", 6 * DAY + 3600 * 1000)
    stale = sealed(B, "b", 3 * DAY + 12 * 3600 * 1000)
    buckets = (fresh, stale, active(C, "a"), active(D4, "b"))
    # Node b's open bucket began on day 4 (Chicago day 3), so day 2 is the last sealed day.
    assert last_sealed_day(buckets) == "1970-01-03"
    nodes = (Node("a", 10, 1, 9), Node("b", 10, 1, 9))
    cluster = Cluster("c", "8.18.7", nodes, (), (), buckets, "i")
    plan = {
        "coverage": {"start": 0, "end": 7 * DAY},
        "selected": [{"node": "a", "id": A}, {"node": "b", "id": B}],
        "last_fully_sealed_day": None,
    }
    assert partial_days(cluster, plan) == ["1970-01-04", "1970-01-05", "1970-01-06", "1970-01-07"]
    # A node with an open bucket and nothing sealed cannot be bounded: everything is partial.
    unbounded = Cluster("c", "8.18.7", nodes, (), (), (fresh, active(C, "a"), active(D4, "b")), "i")
    assert last_sealed_day(unbounded.buckets) is None
    assert len(partial_days(unbounded, {**plan, "selected": [{"node": "a", "id": A}]})) == 7


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

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
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
    # With --trust-new the identity is enrolled and the run stops: open is never retried,
    # so no credential reaches the identity that was only just seen.
    calls = []

    def open_then_fail(self):
        calls.append(1)
        if len(calls) == 1:
            raise TrustRequired([identity])
        raise MigrationError(Code.CONNECTION)

    monkeypatch.setattr(LiveEnvironment, "open", open_then_fail)
    code = cli.main(["--source", "src.lab", "--destination", "dst.lab", "--trust-new", "discover"])
    out, err = capsys.readouterr()
    assert code == 0 and len(calls) == 1
    enrolled = json.loads(out.splitlines()[-1])
    assert enrolled["credentials_sent"] is False and len(enrolled["enrolled"]) == 1
    assert TrustStore(tmp_path / "cfg" / "vcf-logs-migrator").check(identity) == "trusted"
    assert "canary" not in out + err
    # The second run talks to the pinned identity (the stub then fails as connection).
    calls.clear()
    monkeypatch.setattr(
        LiveEnvironment, "open", lambda self: (_ for _ in ()).throw(MigrationError(Code.CONNECTION))
    )
    assert cli.main(["--source", "src.lab", "--destination", "dst.lab", "discover"]) == 7


def test_cli_expect_fingerprint_enrolls_and_proceeds_only_on_a_match(monkeypatch, capsys, tmp_path):
    from logs_migrate import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("LOGS_SOURCE_SSH_PASSWORD", "canary-ssh")
    monkeypatch.setenv("LOGS_SOURCE_API_PASSWORD", "canary-api")
    monkeypatch.setenv("LOGS_DESTINATION_API_PASSWORD", "canary-dst")
    monkeypatch.setattr("sys.stdin", type("Stdin", (), {"isatty": staticmethod(lambda: False)})())
    identity = Identity("ssh", "src.lab:22", "e" * 64, "ssh-ed25519")
    calls = []

    def open_then_fail(self):
        calls.append(1)
        if len(calls) == 1:
            self._pending[identity.name] = "src.lab ssh-ed25519 AAAA"
            raise TrustRequired([identity])
        raise MigrationError(Code.CONNECTION)

    monkeypatch.setattr(LiveEnvironment, "open", open_then_fail)
    wrong = cli.main(
        [
            "--source",
            "src.lab",
            "--destination",
            "dst.lab",
            "--expect-fingerprint",
            "f" * 64,
            "discover",
        ]
    )
    assert wrong == 7 and len(calls) == 1
    assert TrustStore(tmp_path / "cfg" / "vcf-logs-migrator").check(identity) == "unknown"
    colons = ":".join("ee" for _ in range(32)).upper()
    calls.clear()  # The stub asks for trust once per invocation.
    right = cli.main(
        [
            "--source",
            "src.lab",
            "--destination",
            "dst.lab",
            "--expect-fingerprint",
            colons,
            "discover",
        ]
    )
    assert right == 7 and len(calls) == 2, "a verified fingerprint enrolls and the run proceeds"
    store = TrustStore(tmp_path / "cfg" / "vcf-logs-migrator")
    assert store.check(identity) == "trusted"
    assert "src.lab ssh-ed25519 AAAA" in store.known_hosts.read_text()
    assert "canary" not in "".join(capsys.readouterr())


def test_logs90_identity_is_the_transport_pin():
    transport = RecordedTransport([])
    assert Logs90(transport, "a", Secret("b")).identity == "recorded"


def test_task_details_from_90_status():
    task = decode_task(
        {
            "taskId": "task-1",
            "status": "RUNNING",
            "taskDetails": {
                "dest-1": {
                    "status": "RUNNING",
                    "progress": 25,
                    "startTime": 1700000000000,
                    "endTime": None,
                    "scheduledBuckets": {"src-1": ["a", "b"]},
                    "totalBucketsCount": {"src-1": 4},
                    "bytesTransferred": 100,
                }
            },
        },
        destination="d",
        now_ms=1700000060000,
    )
    assert task.id == "task-1" and task.bytes_transferred == 100
    assert task.elapsed_seconds == 60
    assert task.nodes[0].progress == 25
    assert task.nodes[0].scheduled_buckets == {"src-1": 2}
    assert task.nodes[0].total_buckets == {"src-1": 4}


def test_recorded_90_task_list_selects_this_task_and_falls_back_only_for_unique_match():
    from pathlib import Path

    response = json.loads(
        (Path(__file__).parent / "fixtures/real-90-task-status.fixture").read_text()
    )

    class Client:
        identity = "test"

        def migration_status(self):
            return response

    env = LiveEnvironment(Connection(source_host="src", destination="dst"), Credentials({}))
    env.destination_client = Client()
    first, second = response["tasks"][:2]
    task = env.transfer_for(second["taskId"])
    assert task.id == second["taskId"] and len(task.nodes) == 3
    assert task.bytes_transferred is None and task.nodes[0].bytes_transferred is None
    assert task.window == Window(second["bucketStartTime"], second["bucketEndTime"])
    assert task.elapsed_seconds == (second["taskEndTime"] - second["taskCreateTime"]) / 1000
    assert (
        task.nodes[0].total_buckets
        == second["taskDetails"][task.nodes[0].node]["totalBucketsCount"]
    )
    window = Window(first["bucketStartTime"], first["bucketEndTime"])
    assert (
        env.transfer_for(window=window, attempted_at_ms=first["taskCreateTime"]).id
        == first["taskId"]
    )
    assert (
        env.transfer_for(window=window, attempted_at_ms=first["taskCreateTime"] + 1).status
        == "idle"
    )
    day = first["bucketStartTime"] // DAY * DAY
    same_day = Window(day + 3_600_000, day + 7_200_000)
    assert (
        env.transfer_for(window=same_day, attempted_at_ms=first["taskCreateTime"]).status == "idle"
    )
    listed = env.destination_tasks()
    assert [t.id for t in listed] == [t["taskId"] for t in response["tasks"]]
    response["tasks"].append(dict(first, taskId="duplicate"))
    assert env.transfer_for(window=window, attempted_at_ms=first["taskCreateTime"]).status == "idle"
    assert env.transfer_for("missing").status == "idle"


def test_source_growth_compares_active_directory_sizes_only(rigged):
    env, source, destination, ssh = trusted(rigged)
    env.members = ["src-1.lab"]
    path = "/storage/core/loginsight/cidata/store/"
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = f"5000 {path}{A}\n1000 {path}{C}\n"
    env.node_inventory("src-1.lab")
    env.clock.sleep = lambda seconds: None
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = [
        f"1000 {path}{C}\n",
        f"1300 {path}{C}\n",
    ]
    result = env.source_growth()
    assert result["bytes"] == 300 and result["nodes"] == ["src-1.lab"]
    assert result["window_seconds"] >= 1


def test_source_growth_counts_a_node_without_active_buckets_as_idle(rigged):
    env, source, destination, ssh = trusted(rigged)
    env.members = ["src-1.lab", "src-2.lab"]
    path = "/storage/core/loginsight/cidata/store/"
    idle = LISTING.replace("status=active", "status=archived")
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = f"1000 {path}{C}\n"
    ssh.replies[("src-2.lab", ReadOperation.INVENTORY)] = idle
    env.node_inventory("src-1.lab")
    env.node_inventory("src-2.lab")
    env.clock.sleep = lambda seconds: None
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = [
        f"1000 {path}{C}\n",
        f"1300 {path}{C}\n",
    ]
    result = env.source_growth()
    assert result["bytes"] == 300 and result["nodes"] == ["src-1.lab"]


def test_source_growth_inventories_every_node_when_nothing_is_cached(rigged):
    env, source, destination, ssh = trusted(rigged)
    env.members = ["src-1.lab", "src-2.lab"]
    path = "/storage/core/loginsight/cidata/store/"
    for host in env.members:
        ssh.replies[(host, ReadOperation.USAGE)] = f"1000 {path}{C}\n"
    env.clock.sleep = lambda seconds: None
    result = env.source_growth()
    assert result["bytes"] == 0 and result["nodes"] == []
    assert {host for host, _ in ssh.reads} >= set(env.members)


def test_source_growth_measures_from_a_fresh_baseline_not_the_cached_inventory(rigged, monkeypatch):
    env, source, destination, ssh = trusted(rigged)
    env.members = ["src-1.lab"]
    path = "/storage/core/loginsight/cidata/store/"
    now = [1000.0]
    monkeypatch.setattr(live.time, "monotonic", lambda: now[0])
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = f"1000 {path}{C}\n"
    env.node_inventory("src-1.lab")
    now[0] += 250
    env.clock.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = [
        f"4000 {path}{C}\n",
        f"4100 {path}{C}\n",
    ]
    result = env.source_growth()
    assert result["bytes"] == 100 and result["window_seconds"] == 5


def test_active_buckets_keep_no_dates_whatever_the_listing_reports(rigged):
    env, source, destination, ssh = trusted(rigged)
    stamped = LISTING.replace(
        f"id={C}, status=active created=2026-11-08T00:00:00.000+0000 size=0 cmd=[NULL]",
        f"id={C}, status=active created=2026-11-08T00:00:00.000+0000 size=0 "
        "cmd=[startTime=1780000000000 endTime=1781000000000 messages=7 generation=1]",
    )
    assert stamped != LISTING
    ssh.replies[("src-1.lab", ReadOperation.INVENTORY)] = stamped
    inventory = env.node_inventory("src-1.lab")
    cluster = env._cluster_from_nodes("c", "8.18.7", [inventory], (), "i")
    (bucket,) = [b for b in cluster.buckets if b.id == C]
    assert bucket.status == "active"
    assert (bucket.start, bucket.end, bucket.messages, bucket.bounds_proven) == (
        None,
        None,
        0,
        False,
    )


def test_source_growth_refuses_bucket_rollover(rigged):
    env, source, destination, ssh = trusted(rigged)
    env.members = ["src-1.lab"]
    path = "/storage/core/loginsight/cidata/store/"
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = f"1000 {path}{C}\n"
    env.node_inventory("src-1.lab")
    env.clock.sleep = lambda seconds: None
    ssh.replies[("src-1.lab", ReadOperation.INVENTORY)] = LISTING.replace(
        "status=active", "status=archived"
    )
    with pytest.raises(MigrationError, match=Code.INCOMPLETE):
        env.source_growth()


def test_live_copy_counts_requested_days_not_full_bucket_coverage(tmp_path):
    from conftest import FIXTURES

    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    data = json.loads((FIXTURES / "sealed-populated.fixture").read_text())
    source = data["source"]
    # The masked r1 request was Mar 1-3, 2024. Extend one selected bucket to
    # reproduce the roughly 25-month coverage in the later large-source run.
    source["buckets"][0]["start"] = parse_time("2023-03-01T00:00:00Z")
    source["buckets"][0]["end"] = parse_time("2025-04-01T00:00:00Z")
    source["buckets"][1]["start"] = parse_time("2024-03-01T00:00:11.738Z")
    source["buckets"][1]["end"] = parse_time("2024-03-02T00:00:00Z")
    env, engine = _live_rig(tmp_path, {"source": source})
    seen = []
    original = env.count

    def count(scope, window, keys):
        seen.append((scope, window))
        return original(scope, window, keys)

    env.count = count
    requested = Window(parse_time("2024-03-01T00:00:00Z"), parse_time("2024-03-03T00:00:00Z"))
    run = engine.plan(requested, Policy(poll_seconds=0))
    assert run["plan"]["coverage"]["start"] < requested.start
    assert run["plan"]["coverage"]["end"] > requested.end
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    days = requested
    assert len(seen) == 4 and all(window == days for _, window in seen)
    engine.status()
    before = len(seen)
    engine.verify(run["id"])
    assert len(seen) > before and all(window == days for _, window in seen)


def test_count_progress_reports_every_query(rigged, capsys):
    import sys

    env, source, _, _ = trusted(rigged)
    window = Window(0, 2 * DAY)
    path = (
        f"/api/v1/aggregated-events/timestamp/%3E%3D0/timestamp/%3C{2 * DAY}"
        "?bin-width=86400000&timeout=580000"
    )
    source.records += [
        session(),
        record("GET", path, {"complete": True, "bins": []}),
        record("GET", path, {"complete": True, "bins": []}),
    ]
    env.on_progress = lambda message: print(message, file=sys.stderr)
    env._counts(env.source_client, "source", window)
    env._counts(env.source_client, "source", window)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert all("Counting source: 1970-01-01 to 1970-01-03" in line for line in lines)


def test_count_timeout_identifies_scope_and_query_interval(rigged):
    from logs_migrate.errors import QueryTimeout

    env, source, _, _ = trusted(rigged)

    def request(*args, **kwargs):
        raise QueryTimeout()

    source.request = request
    with pytest.raises(QueryTimeout) as failure:
        env._counts(env.source_client, "source", Window(0, DAY))
    assert "source count query from 1970-01-01" in failure.value.message
    assert "timed out" in failure.value.message


def test_node_inventories_run_in_parallel_and_are_reused(rigged):
    import threading

    env, source, _, ssh = trusted(rigged)
    barrier = threading.Barrier(2, timeout=2)
    original = env.node_inventory
    first = set()

    def inventory(host, *args):
        if host not in first:
            first.add(host)
            barrier.wait()
        return original(host, *args)

    env.node_inventory = inventory
    source.records += [
        session(),
        record("GET", "/api/v1/version", {"version": "8.18.7"}),
        record("GET", "/api/v1/partitioning", {"partitions": []}),
        record("GET", "/api/v1/version", {"version": "8.18.7"}),
        record("GET", "/api/v1/partitioning", {"partitions": []}),
    ]
    messages = []
    env.on_progress = messages.append
    env.source_cluster()
    env.source_cluster()
    assert sorted(host for host, op in ssh.reads if op == ReadOperation.USAGE) == [
        "src-1.lab",
        "src-2.lab",
    ]
    assert len(messages) == 2 and all("sizing" in message for message in messages)


def test_plan_warns_that_undated_open_buckets_may_hold_backfill_without_dropping_the_frontier(
    tmp_path,
):
    from conftest import FIXTURES

    from logs_migrate.models import Policy, parse_time

    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-03T00:00:00Z"))
    _, engine = _live_rig(tmp_path)
    plan = engine.plan(window, Policy(poll_seconds=0))["plan"]
    (warning,) = [w for w in plan["warnings"] if "backfill" in w]
    assert "older-dated data" in warning and "not a guarantee" in warning
    assert plan["last_fully_sealed_day"] == "2026-03-01"

    data = json.loads((FIXTURES / "sealed-populated.fixture").read_text())
    active = data["source"]["buckets"][3]
    active.update(start=parse_time("2026-03-10T00:00:00Z"), end=None, bounds_proven=True)
    _, engine = _live_rig(tmp_path / "dated", {"source": data["source"]})
    dated = engine.plan(window, Policy(poll_seconds=0))["plan"]
    assert not [w for w in dated["warnings"] if "backfill" in w]


def test_backfilled_open_bucket_data_before_the_frontier_is_a_discrepancy_not_a_pass(tmp_path):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-03T00:00:00Z"))
    env, engine = _live_rig(tmp_path)
    env.data["source_events"].append(
        {
            "id": "backfill-1",
            "time": parse_time("2026-03-01T12:00:00Z"),
            "bucket": "src-1/00000000-0000-0000-0000-000000000001",
            "fields": {},
        }
    )
    run = engine.plan(window, Policy(poll_seconds=0))
    assert "2026-03-01" not in run["plan"]["partial_days"]
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    engine.status()
    env.data["destination_events"] = [
        e for e in env.data["destination_events"] if e["id"] != "backfill-1"
    ]
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_discrepancy"


def test_baseline_count_timeout_keeps_the_scope_and_interval_in_the_error(tmp_path):
    from logs_migrate.errors import QueryTimeout
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-03T00:00:00Z"))
    env, engine = _live_rig(tmp_path)
    run = engine.plan(window, Policy(poll_seconds=0))
    original = env.count

    def count(scope, requested, keys):
        if scope == "baseline":
            raise QueryTimeout("destination count query from 2026-03-01 timed out")
        return original(scope, requested, keys)

    env.count = count
    with pytest.raises(QueryTimeout) as failure:
        engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    assert "destination count query from 2026-03-01 timed out" in failure.value.message


def test_verify_keeps_the_timeout_message_for_the_operator(tmp_path):
    from logs_migrate.errors import QueryTimeout
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-03T00:00:00Z"))
    env, engine = _live_rig(tmp_path)
    run = engine.plan(window, Policy(poll_seconds=0))
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    engine.status()
    original = env.count

    def count(scope, requested, keys):
        if scope == "destination":
            raise QueryTimeout("destination count query from 2026-03-01 timed out")
        return original(scope, requested, keys)

    env.count = count
    result = engine.verify(run["id"])
    assert result["outcome"] == "incomplete_verification"
    assert result["reason"] == Code.CONNECTION.value
    assert result["detail"] == "destination count query from 2026-03-01 timed out"
