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
                    reply = env.replies[(host, operation)]
                except KeyError:
                    raise MigrationError(Code.CONNECTION) from None
                return reply.pop(0) if isinstance(reply, list) else reply

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
            ("src-1.lab", ReadOperation.USAGE): (
                f"5000 /storage/core/loginsight/cidata/store/{A}\n"
                f"20000 /storage/core/loginsight/cidata/store/{B}\n"
            ),
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


def test_destination_member_failure_names_the_destination_host_not_a_source_member(
    rigged, monkeypatch
):
    env, _, _, ssh, store = rigged
    monkeypatch.setattr(LiveEnvironment, "_destination_ssh_enabled", lambda self: True)
    for name in ("src.lab:22", "src-1.lab:22", "src-2.lab:22", "dst.lab:22"):
        store.trust(Identity("ssh", name, "b" * 64, "ssh-ed25519"), "x ssh-ed25519 AAAA")
    store.trust(Identity("tls", "src.lab:9543", "a" * 64))
    store.trust(Identity("tls", "dst.lab:9543", "c" * 64))
    with pytest.raises(MigrationError):
        env.open()
    assert env.notes["stage"] == "ssh dst.lab:22 (members)"


def test_source_member_failure_names_the_source_host(rigged):
    env, _, _, ssh, store = rigged
    store.trust(Identity("tls", "src.lab:9543", "a" * 64))
    store.trust(Identity("tls", "dst.lab:9543", "c" * 64))
    store.trust(Identity("ssh", "src.lab:22", "b" * 64, "ssh-ed25519"), "x ssh-ed25519 AAAA")
    del ssh.replies[("src.lab", ReadOperation.MEMBERS)]
    with pytest.raises(MigrationError):
        env.open()
    assert env.notes["stage"] == "ssh src.lab:22 (members)"


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
    assert by_id[A].size == 479923834 and by_id[A].messages == 1893963 and by_id[A].bounds_proven
    assert by_id[B].size == 20971520 and by_id[C].status == "active" and not by_id[C].complete
    assert dst.version == "9.0.2.0200" and [n.id for n in dst.nodes] == ["dst.lab"]
    assert dst.nodes[0].repository_available is None and dst.buckets == ()
    assert env.notes["source_listing_keys"]


def test_selected_sizing_only_reads_overlapping_sealed_buckets(rigged):
    env, source, _, ssh = trusted(rigged)
    source.records += [
        session(),
        record("GET", "/api/v1/version", {"version": "8.18.7"}),
        record("GET", "/api/v1/partitioning", {"partitions": []}),
    ]
    calls = []
    old_client = ssh.client

    def client(host):
        result = old_client(host)
        original = result.active_usage

        def selected(ids):
            calls.append((host, ids))
            return original(ids)

        result.active_usage = selected
        return result

    ssh.client = client
    cluster = env.source_cluster(Window(1790000000000, 1790000000001))
    assert calls == [("src-1.lab", [A])]
    assert next(b for b in cluster.buckets if b.id == A).size == 5000
    assert not any(op == ReadOperation.USAGE for _, op in ssh.reads)


def test_only_source_listings_are_timed_and_held_for_saving(rigged):
    env, _, _, ssh = trusted(rigged)
    env.node_inventory("src-1.lab")
    assert set(env._listing_seconds) == {"src-1.lab"}
    assert set(env._new_listings) == {("src-1.lab", "source_ssh", None)}
    env.node_inventory("src-2.lab", "destination_ssh", "root")
    assert set(env._listing_seconds) == {"src-1.lab"}
    assert set(env._new_listings) == {("src-1.lab", "source_ssh", None)}


def test_failed_selected_sizing_clears_the_saved_listing_so_a_retry_refreshes(rigged, tmp_path):
    from logs_migrate.journal import Journal

    env, source, _, ssh = trusted(rigged)
    journal = Journal(tmp_path / "inventory.db")
    env.inventory_journal = journal
    source.records += [session()] + [
        entry
        for _ in range(3)
        for entry in (
            record("GET", "/api/v1/version", {"version": "8.18.7"}),
            record("GET", "/api/v1/partitioning", {"partitions": []}),
        )
    ]
    old_client = ssh.client
    broken = {"on": False}

    def client(host):
        result = old_client(host)
        original = result.active_usage

        def selected(ids):
            if broken["on"]:
                raise MigrationError(Code.CONNECTION)
            return original(ids)

        result.active_usage = selected
        return result

    ssh.client = client
    window = Window(1790000000000, 1790000000001)
    env.source_cluster()
    listings = len([op for _, op in ssh.reads if op == ReadOperation.INVENTORY])
    env._inventory_cache.clear()
    broken["on"] = True
    with pytest.raises(MigrationError) as failure:
        env.source_cluster(window)
    assert failure.value.code == Code.CONNECTION
    assert "src-1.lab" in failure.value.message and "retry" in failure.value.message
    assert len([op for _, op in ssh.reads if op == ReadOperation.INVENTORY]) == listings
    broken["on"] = False
    env.source_cluster(window)
    assert len([op for _, op in ssh.reads if op == ReadOperation.INVENTORY]) == 2 * listings
    journal.close()


def test_source_listing_journal_cache_survives_a_new_session(rigged, tmp_path):
    from logs_migrate.journal import Journal

    env, source, _, ssh = trusted(rigged)
    journal = Journal(tmp_path / "inventory.db")
    env.inventory_journal = journal
    source.records += [session()] + [
        entry
        for _ in range(3)
        for entry in (
            record("GET", "/api/v1/version", {"version": "8.18.7"}),
            record("GET", "/api/v1/partitioning", {"partitions": []}),
        )
    ]
    env.source_cluster()
    env._inventory_cache.clear()
    env.source_cluster()
    assert len([op for _, op in ssh.reads if op == ReadOperation.INVENTORY]) == 2
    env.invalidate_inventory()
    env.source_cluster()
    assert len([op for _, op in ssh.reads if op == ReadOperation.INVENTORY]) == 4
    journal.close()


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
    assert step.status == "fail" and "Source activity could not be measured" in step.detail


def test_selftest_names_the_exception_class_of_an_unexpected_failure(rigged, monkeypatch):
    def broken(self):
        raise KeyError("secret-looking remote text")

    monkeypatch.setattr(live.LiveEnvironment, "source_version", broken, raising=False)
    _, _, result = _run_selftest(rigged, Window(1780000000000, 1782000000000))
    (step,) = [s for s in result.steps if s.name == "Source API version"]
    assert step.status == "fail"
    assert "KeyError" in step.detail and "secret-looking" not in step.detail


def test_selftest_failure_detail_is_plain_for_the_cli_and_named_in_the_window(rigged, monkeypatch):
    from logs_migrate.gui import viewmodel as vm

    monkeypatch.setattr(live.LiveEnvironment, "source_version", lambda self: 1 / 0, raising=False)
    _, _, result = _run_selftest(rigged, Window(1780000000000, 1782000000000))
    (step,) = [s for s in result.steps if s.name == "Source API version"]
    assert not step.detail.startswith("gui:") and "ZeroDivisionError" in step.detail
    rows = dict(vm.selftest_rows(result.data()))
    assert "ZeroDivisionError" in rows["Source API version"]


def test_selftest_without_a_window_does_not_size_all_sealed_buckets(rigged):
    env, destination, result = _run_selftest(rigged, None)
    (plan,) = [s for s in result.steps if s.name == "Plan preview (no request sent)"]
    assert plan.status == "skip"
    assert "Choose dates" in plan.detail


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
    assert result["actual_count"] is None
    assert "Other events" in result["received_unavailable_reason"]
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
    assert result["expected_count"] == 6
    assert result["actual_count"] == result["measured_arrival_count"] == 6
    assert result["unmeasured_day_count"] == 0
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


def test_destination_directory_inspection_detects_unregistered_bucket(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    env, _ = _live_rig(tmp_path)
    source, destination = env.discover()
    bucket = replace(source.buckets[0], node=destination.nodes[0].id)
    destination = replace(destination, buckets=(bucket,), inventory_available=True)

    class SSH:
        def __init__(self, host):
            self.host = host

        def existing_bucket_usage(self, ids):
            if self.host == destination.nodes[0].id:
                return f"{bucket.size} /store/{bucket.id}\n"
            return ""

    adapter = SimpleNamespace(
        connection=SimpleNamespace(destination_ssh_user="root"),
        _ssh=lambda host, credential, user: SSH(host),
    )
    inspected = LiveEnvironment.inspect_destination_buckets(adapter, destination, {bucket.id})
    assert inspected.buckets[0].size == bucket.size
    orphan = replace(destination, buckets=())
    with pytest.raises(MigrationError, match=Code.INCOMPLETE):
        LiveEnvironment.inspect_destination_buckets(adapter, orphan, {bucket.id})


def test_destination_inspection_retains_copies_from_each_node(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    env, _ = _live_rig(tmp_path)
    source, destination = env.discover()
    bucket = source.buckets[0]
    nodes = destination.nodes[:2]
    destination = replace(
        destination,
        buckets=tuple(replace(bucket, node=node.id) for node in nodes),
        inventory_available=True,
    )

    class SSH:
        def __init__(self, host):
            self.host = host

        def existing_bucket_usage(self, ids):
            return (
                f"{bucket.size} /store/{bucket.id}\n" if self.host in {n.id for n in nodes} else ""
            )

    adapter = SimpleNamespace(
        connection=SimpleNamespace(destination_ssh_user="root"),
        _ssh=lambda host, credential, user: SSH(host),
    )
    inspected = LiveEnvironment.inspect_destination_buckets(adapter, destination, {bucket.id})
    assert {b.node for b in inspected.buckets} == {node.id for node in nodes}


def test_destination_inspection_names_an_indexed_bucket_without_a_directory(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    env, _ = _live_rig(tmp_path)
    source, destination = env.discover()
    bucket = source.buckets[0]
    node = destination.nodes[0]
    destination = replace(
        destination,
        buckets=(replace(bucket, node=node.id),),
        inventory_available=True,
    )

    class SSH:
        def existing_bucket_usage(self, ids):
            return ""

    adapter = SimpleNamespace(
        connection=SimpleNamespace(destination_ssh_user="root"),
        _ssh=lambda host, credential, user: SSH(),
    )
    with pytest.raises(MigrationError) as error:
        LiveEnvironment.inspect_destination_buckets(adapter, destination, {bucket.id})
    assert error.value.code == Code.INCOMPLETE
    assert error.value.affected_ids == {bucket.id}
    assert f"{node.id}: {bucket.id}" in error.value.message


def test_live_settlement_uses_registered_inventory_and_excludes_pending(tmp_path):
    from dataclasses import replace

    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.ports import NodeTransfer, Transfer

    env, engine = _live_rig(tmp_path)
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    engine.run(run["id"], active_ack=ACTIVE_ACK)
    destination_id = run["plan"]["destination"]["id"]
    source, destination = env.discover()
    selected_ids = [b["id"] for b in run["plan"]["selected"]]
    registered = tuple(
        replace(b, node=destination.nodes[0].id) for b in source.buckets if b.id in selected_ids[:2]
    )
    env.destination_cluster = lambda **kw: replace(
        destination, buckets=registered, inventory_available=True
    )
    task = Transfer(
        "sim-1",
        "completed",
        destination=destination_id,
        nodes=(NodeTransfer("dst-1", "completed", scheduled_ids={"src-1": (selected_ids[1],)}),),
    )
    env.transfer = lambda: task
    engine.status(run["id"])
    (row,) = engine.journal.transfers(destination_id)
    assert row["closed"]
    assert {key.rsplit("/", 1)[-1] for key in row["received"]} == {selected_ids[0]}


def test_live_ledger_records_a_skipped_bucket_as_not_received_without_a_copied_list(tmp_path):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.ports import NodeTransfer, Transfer

    env, engine = _live_rig(tmp_path)
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    destination = run["plan"]["destination"]["id"]
    # The API's finished task names only what it skipped (the documented disk-limit trap).
    first = "00000000-0000-0000-0000-000000000001"  # A fixture bucket, listed as skipped.
    node = NodeTransfer("dst-1", "completed", scheduled_ids={})
    env.transfer = lambda: Transfer(
        "sim-1", "completed", (), (first,), nodes=(node,), destination=destination
    )
    engine.status()
    (row,) = engine.journal.transfers(destination)
    assert row["closed"] and first not in {k.rsplit("/", 1)[-1] for k in row["received"]}
    assert len(row["received"]) == 2
    result = engine.verify(run["id"])
    assert result["buckets"]["not_received"] == ["src-1/" + first]
    assert result["receipt_basis"] == "task_report"
    assert (
        result["outcome"] != "verified_success"
        and result["reason"] == "selected_buckets_not_received"
    )


@pytest.mark.parametrize("state", ["cancelled", "failed"])
def test_live_ledger_without_inventory_settles_a_stopped_task_from_its_remaining_list(
    tmp_path, state
):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.ports import NodeTransfer, Transfer

    env, engine = _live_rig(tmp_path)
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    destination = run["plan"]["destination"]["id"]
    ids = [b["id"] for b in run["plan"]["selected"]]
    left = ids[-1]
    node = NodeTransfer(state, state, scheduled_ids={"src-1": (left,)})
    env.transfer = lambda: Transfer("sim-1", state, destination=destination, nodes=(node,))
    status = engine.status()
    (row,) = engine.journal.transfers(destination)
    assert row["closed"] and {k.rsplit("/", 1)[-1] for k in row["received"]} == set(ids[:-1])
    assert status["settlement"] == "task_report"
    assert "not confirmed" in status["settlement_note"]


@pytest.mark.parametrize("state", ["cancelled", "failed"])
def test_live_ledger_without_inventory_holds_a_stopped_task_with_no_remaining_list(tmp_path, state):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.ports import Transfer

    env, engine = _live_rig(tmp_path)
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    destination = run["plan"]["destination"]["id"]
    env.transfer = lambda: Transfer("sim-1", state, destination=destination)
    status = engine.status()
    (row,) = engine.journal.transfers(destination)
    assert not row["closed"] and status["settlement"] == "held"
    assert "stays held" in status["settlement_note"]


def _preview_run(tmp_path):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    env, engine = _live_rig(tmp_path)
    env.data["destination"]["version"] = "9.1.1"
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    return env, engine, run


def test_no_ssh_partial_received_list_can_resume_and_verify(tmp_path):
    from dataclasses import asdict, replace

    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    env, engine = _live_rig(tmp_path, {"cancel_after": 1})
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    first = engine.plan(window, Policy(poll_seconds=0))
    engine.run(first["id"], active_ack=ACTIVE_ACK)
    status = engine.status(first["id"])
    assert status["status"] == "cancelled"
    assert len(status["completed"]) == 1 and not status["skipped"]
    row = engine.journal.transfers(first["plan"]["destination"]["id"])[0]
    assert row["closed"] and row["basis"] == "task_report"
    assert len(row["received"]) == 1 and len(row["unaccounted"]) == 2
    assert "requested again" in status["settlement_note"]
    assert len(status["remainder"]["selected"]) == 2
    # One unaccounted bucket actually landed, but the task report omitted it. Re-requesting
    # must skip its existing directory instead of copying its events a second time.
    source, destination = env.discover()
    unreported = next(b for b in source.buckets if b.id == first["plan"]["selected"][1]["id"])
    env.data["destination"]["buckets"].append(
        asdict(replace(unreported, node=destination.nodes[0].id, partition=""))
    )
    env.data["destination_events"].extend(
        {**event, "cohort": "migration"}
        for event in env.data["source_events"]
        if event["bucket"] == unreported.key
    )
    del env.data["cancel_after"]
    resumed = engine.resume(first["id"])
    assert len(resumed["plan"]["already_received"]) == 1
    assert len(resumed["plan"]["selected"]) == 2
    assert resumed["plan"]["re_requested_unconfirmed"]
    engine.run(resumed["id"], active_ack=ACTIVE_ACK)
    engine.status(resumed["id"])
    result = engine.verify(resumed["id"])
    assert result["outcome"] == "verified_success"
    assert result["receipt_basis"] == "task_report"
    assert "requested again" in result["receipt_note"]


def test_no_ssh_resume_does_not_refuse_on_a_placeholder_node_count(tmp_path):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    env, engine = _live_rig(tmp_path, {"cancel_after": 1, "placeholder_nodes": True})
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    first = engine.plan(window, Policy(poll_seconds=0))
    assert len(first["plan"]["destination"]["nodes"]) == 1
    assert first["plan"]["estimate"]["destination_nodes"] == 1
    assert first["plan"]["estimate"]["destination_nodes_assumed"]
    engine.run(first["id"], active_ack=ACTIVE_ACK)
    status = engine.status(first["id"])
    assert status["status"] == "cancelled"
    assert status["remainder"]["node_count_unchecked"]
    assert status["remainder"]["estimate"]["destination_nodes_assumed"]
    del env.data["cancel_after"]
    resumed = engine.resume(first["id"])
    plan = resumed["plan"]
    assert plan["node_count_unchecked"]
    assert any("node count could not be checked" in w for w in plan["warnings"])
    assert plan["estimate"]["destination_nodes"] == len(env.data["destination"]["nodes"]) == 3
    assert plan["estimate"]["destination_nodes_assumed"]
    assert status["remainder"]["estimate"] == plan["estimate"]
    engine.run(resumed["id"], active_ack=ACTIVE_ACK)
    engine.status(resumed["id"])
    assert engine.verify(resumed["id"])["outcome"] == "verified_success"


@pytest.mark.parametrize("ingest", [False, True])
def test_no_ssh_resume_verification_accounts_for_unattributed_ingest(tmp_path, ingest):
    from conftest import FIXTURES

    from logs_migrate.adapters.simulated import SimulatedClock, SimulatedEnvironment
    from logs_migrate.journal import Journal
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    data = json.loads((FIXTURES / "sealed-populated.fixture").read_text())
    if not ingest:
        data["ingest_during_run"] = []
    data.update({"cancel_after": 1, "placeholder_nodes": True})
    data["destination"]["inventory_available"] = False
    env = SimulatedEnvironment(data)
    env.attribution = False
    env.mode = "live"
    clock = SimulatedClock()
    engine = Engine(env, Journal(tmp_path / "j.sqlite", clock), clock)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    first = engine.plan(window, Policy(poll_seconds=0))
    engine.run(first["id"], active_ack=ACTIVE_ACK)
    assert engine.status(first["id"])["status"] == "cancelled"
    stopped = engine.verify(first["id"])
    assert stopped["outcome"] != "verified_success"
    del env.data["cancel_after"]
    resumed = engine.resume(first["id"])
    engine.run(resumed["id"], active_ack=ACTIVE_ACK)
    assert engine.status(resumed["id"])["status"] == "completed"
    result = engine.verify(resumed["id"])
    migrated = [e["id"] for e in env.data["destination_events"] if e.get("cohort") == "migration"]
    assert len(migrated) == len(set(migrated)) == 6
    if ingest:
        assert result["outcome"] == "verified_discrepancy"
        assert (result["daily"][0]["expected"], result["daily"][0]["actual"]) == (3, 5)
        assert sum(e.get("cohort") == "ingest" for e in env.data["destination_events"]) == 2
    else:
        assert result["outcome"] == "verified_success"


def _placeholder_then_observed(tmp_path):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    env, engine = _live_rig(tmp_path, {"cancel_after": 1, "placeholder_nodes": True})
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    first = engine.plan(window, Policy(poll_seconds=0))
    assert len(first["plan"]["destination"]["nodes"]) == 1
    engine.run(first["id"], active_ack=ACTIVE_ACK)
    assert engine.status(first["id"])["status"] == "cancelled"
    del env.data["cancel_after"]
    env.data["placeholder_nodes"] = False
    env.data["destination"]["inventory_available"] = True
    return env, engine, window, first


def test_resume_after_destination_ssh_is_connected_does_not_compare_a_placeholder_count(tmp_path):
    from logs_migrate.models import ACTIVE_ACK

    env, engine, _, first = _placeholder_then_observed(tmp_path)
    resumed = engine.resume(first["id"])
    plan = resumed["plan"]
    assert len(plan["destination"]["nodes"]) == len(env.data["destination"]["nodes"]) == 3
    assert plan["node_count_unchecked"]
    assert any("node count could not be checked" in w for w in plan["warnings"])
    engine.run(resumed["id"], active_ack=ACTIVE_ACK)
    engine.status(resumed["id"])
    assert engine.verify(resumed["id"])["outcome"] == "verified_success"


def test_replanning_after_destination_ssh_is_connected_does_not_compare_a_placeholder_count(
    tmp_path,
):
    from logs_migrate.models import Policy

    _, engine, window, _ = _placeholder_then_observed(tmp_path)
    replanned = engine.plan(window, Policy(poll_seconds=0))
    assert len(replanned["plan"]["already_received"]) == 1
    assert len(replanned["plan"]["selected"]) == 2


def test_no_ssh_unrelated_receipt_does_not_release_the_selection(tmp_path):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.ports import Transfer

    env, engine = _live_rig(tmp_path)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    first = engine.plan(window, Policy(poll_seconds=0))
    engine.run(first["id"], active_ack=ACTIVE_ACK)
    destination = first["plan"]["destination"]["id"]
    env.transfer = lambda: Transfer("sim-1", "cancelled", (A,), destination=destination)
    result = engine.status(first["id"])
    row = engine.journal.transfers(destination)[0]
    assert not row["closed"] and result["settlement"] == "held"


def test_911_completed_task_without_receipt_evidence_stays_held(tmp_path):
    from logs_migrate.ports import NodeTransfer, Transfer

    env, engine, run = _preview_run(tmp_path)
    destination = run["plan"]["destination"]["id"]
    node = NodeTransfer("dst-1", "completed", scheduled_ids={})
    env.transfer = lambda: Transfer("sim-1", "completed", nodes=(node,), destination=destination)
    status = engine.status()
    (row,) = engine.journal.transfers(destination)
    assert not row["closed"] and status["settlement"] == "held"
    assert "9.1.1" in status["settlement_note"]
    assert "SSH" not in status["settlement_note"]


def test_a_partial_received_list_holds_and_names_the_unaccounted_buckets(tmp_path):
    from logs_migrate.ports import Transfer

    env, engine, run = _preview_run(tmp_path)
    destination = run["plan"]["destination"]["id"]
    keys = [f"src-1/{b['id']}" for b in run["plan"]["selected"]]
    env.transfer = lambda: Transfer("sim-1", "completed", (keys[0],), destination=destination)
    status = engine.status()
    (row,) = engine.journal.transfers(destination)
    assert not row["closed"] and status["settlement"] == "held"
    assert f"{len(keys) - 1} selected bucket(s) are neither received" in status["settlement_note"]
    assert "reports no list" not in status["settlement_note"]


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
                "taskDetails": {"dst": {"status": "COMPLETED", "scheduledBuckets": {}}},
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
    done = (
        "GET",
        MIGRATIONS,
        (
            200,
            {
                "status": "COMPLETED",
                **window,
                "taskDetails": {"dst": {"status": "COMPLETED", "scheduledBuckets": {}}},
            },
        ),
    )
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


def test_cli_trust_new_notes_shared_ssh_keys_and_still_enrolls(monkeypatch, capsys, tmp_path):
    from logs_migrate import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("LOGS_SOURCE_SSH_PASSWORD", "canary-ssh")
    monkeypatch.setenv("LOGS_SOURCE_API_PASSWORD", "canary-api")
    monkeypatch.setenv("LOGS_DESTINATION_API_PASSWORD", "canary-dst")
    shared = [Identity("ssh", f"10.0.0.{n}:22", "a" * 64) for n in (20, 21, 22)]

    def open_(self):
        raise TrustRequired(shared)

    monkeypatch.setattr(LiveEnvironment, "open", open_)
    monkeypatch.setattr(LiveEnvironment, "key_line", lambda self, identity: "key-line")
    code = cli.main(["--source", "src.lab", "--destination", "dst.lab", "--trust-new", "discover"])
    out, err = capsys.readouterr()
    assert code == 0
    assert "SSH host 10.0.0.21:22 presents the same key as 10.0.0.20:22." in err
    assert "SSH host 10.0.0.22:22 presents the same key as 10.0.0.20:22." in err
    assert "cloned" in err
    assert len(json.loads(out.splitlines()[-1])["enrolled"]) == 3


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


def test_task_status_stays_running_when_a_node_is_restarting():
    task = decode_task(
        {
            "taskId": "one",
            "status": "RUNNING",
            "taskDetails": {
                "dst-1": {"status": "RUNNING"},
                "dst-2": {"status": "STARTING"},
            },
        },
        destination="d",
        now_ms=1700000060000,
    )
    assert task.status == "running"
    assert all(node.status == "running" for node in task.nodes)


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


STORE = "/storage/core/loginsight/cidata/store/"


def test_source_growth_compares_active_directory_sizes_only(rigged):
    env, source, destination, ssh = trusted(rigged)
    env.members = ["src-1.lab"]
    env.clock.sleep = lambda seconds: None
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = [f"1000 {STORE}{C}\n", f"1300 {STORE}{C}\n"]
    result = env.source_growth()
    assert result["bytes"] == 300 and result["nodes"] == ["src-1.lab"]
    assert result["window_seconds"] >= 1


def test_source_growth_counts_a_node_without_active_buckets_as_idle(rigged):
    env, source, destination, ssh = trusted(rigged)
    env.members = ["src-1.lab", "src-2.lab"]
    env.clock.sleep = lambda seconds: None
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = [f"1000 {STORE}{C}\n", f"1300 {STORE}{C}\n"]
    ssh.replies[("src-2.lab", ReadOperation.INVENTORY)] = LISTING.replace(
        "status=active", "status=archived"
    )
    ssh.replies[("src-2.lab", ReadOperation.USAGE)] = ""
    result = env.source_growth()
    assert result["bytes"] == 300 and result["nodes"] == ["src-1.lab"]


def test_source_growth_never_runs_the_full_per_node_inventory(rigged):
    env, source, destination, ssh = trusted(rigged)
    env.members = ["src-1.lab", "src-2.lab"]
    env.clock.sleep = lambda seconds: None
    for host in env.members:
        ssh.replies[(host, ReadOperation.USAGE)] = f"1000 {STORE}{C}\n"
    ssh.reads.clear()
    result = env.source_growth()
    assert result["bytes"] == 0 and result["nodes"] == []
    assert {host for host, _ in ssh.reads} == set(env.members)
    assert {op for _, op in ssh.reads} == {ReadOperation.INVENTORY}


def test_source_growth_ignores_a_stale_cached_inventory(rigged, monkeypatch):
    env, source, destination, ssh = trusted(rigged)
    env.members = ["src-1.lab"]
    now = [1000.0]
    monkeypatch.setattr(live.time, "monotonic", lambda: now[0])
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = f"1000 {STORE}{C}\n"
    env.node_inventory("src-1.lab")
    now[0] += 250
    env.clock.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    rolled = "44444444-4444-4444-4444-444444444444"
    ssh.replies[("src-1.lab", ReadOperation.INVENTORY)] = LISTING.replace(C, rolled)
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = [
        f"4000 {STORE}{rolled}\n",
        f"4100 {STORE}{rolled}\n",
    ]
    result = env.source_growth()
    assert result["bytes"] == 100 and result["window_seconds"] == 5


def test_active_buckets_keep_recorded_dates(rigged):
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
        1780000000000,
        1781000000000,
        7,
        True,
    )


def test_repeated_listing_on_one_node_does_not_fail_discovery(rigged):
    env, _, _, _ = trusted(rigged)
    inventory = env.node_inventory("src-1.lab")
    inventory.buckets.append(inventory.buckets[0])
    cluster = env._cluster_from_nodes("c", "8.18.7", [inventory], (), "i")
    assert len(cluster.buckets) == len(inventory.buckets)


def test_empty_active_bucket_sentinel_has_no_dates():
    line = (
        f"id={C}, status=active cmd=[startTime=9223372036854775807 "
        "endTime=-9223372036854775808 messages=0]"
    )
    (bucket,) = parse_listing(line)
    assert bucket.start is None and bucket.end is None and bucket.messages == 0
    assert bucket.empty_proven


def test_source_growth_refuses_bucket_rollover(rigged):
    env, source, destination, ssh = trusted(rigged)
    env.members = ["src-1.lab"]
    env.clock.sleep = lambda seconds: None
    ssh.replies[("src-1.lab", ReadOperation.INVENTORY)] = [
        LISTING,
        LISTING.replace("status=active", "status=archived"),
    ]
    ssh.replies[("src-1.lab", ReadOperation.USAGE)] = [f"1000 {STORE}{C}\n", ""]
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
    assert not [op for _, op in ssh.reads if op == ReadOperation.USAGE]
    assert len([op for _, op in ssh.reads if op == ReadOperation.INVENTORY]) == 2
    assert not messages


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


def test_live_verification_separates_days_not_measured(tmp_path):
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    env, engine = _live_rig(tmp_path)
    # The selected bucket spans three days but the request and baseline cover one.
    env.data["source"]["buckets"][0]["end"] = parse_time("2026-03-03T12:00:00Z")
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-02T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    engine.status()
    result = engine.verify(run["id"])
    assert result["expected_count"] == sum(b["messages"] for b in run["plan"]["selected"])
    assert result["unmeasured_day_count"] == 2
    assert result["actual_count"] is None
    assert result["measured_arrival_count"] == 2


def test_unmeasured_days_headline_is_one_figure_in_the_window_and_the_report(tmp_path):
    from logs_migrate.gui import viewmodel as vm
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.reporting import markdown

    env, engine = _live_rig(tmp_path)
    env.data["source"]["buckets"][0]["end"] = parse_time("2026-03-03T12:00:00Z")
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-02T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    engine.status()
    result = engine.verify(run["id"])
    sent = sum(b["messages"] for b in run["plan"]["selected"])
    view = vm.verification_view(result, engine.status(), 0, False)
    assert [t[0] for t in view["tiles"]] == ["Sent from selected groups", "Excluded latest data"]
    assert view["tiles"][0][1] == f"{sent:,}"
    assert "requested dates only" in view["tiles"][0][2]
    assert "2 other days these groups touch were not counted" in view["tiles"][0][2]
    report = markdown(engine.journal.load(run["id"]), [])
    summary = report.splitlines()[2]
    assert f"{sent:,} events sent from selected groups" in summary
    assert "requested dates only" in summary and "2 other days" in summary
    assert "events expected" not in summary


def test_unrecognized_listing_names_the_host_and_never_the_remote_text(rigged):
    env, _, _, ssh, _ = rigged
    banner = "Welcome token=hunter2 AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 to the appliance"
    ssh.replies[("src-1.lab", ReadOperation.INVENTORY)] = banner + "\nsecond line\n"
    inventory = env.node_inventory("src-1.lab")
    assert inventory.buckets == [] and inventory.unrecognized is True
    assert env.listing_warnings == {"src-1.lab"}


def test_selftest_names_the_node_with_an_unrecognized_listing(rigged):
    env, _, _, ssh, _ = rigged
    ssh.replies[("src-1.lab", ReadOperation.INVENTORY)] = "Welcome to the appliance 10.0.0.9\n"
    _, _, result = _run_selftest(rigged, Window(1780000000000, 1782000000000))
    (step,) = [s for s in result.steps if s.name.startswith("Node src-1.lab")]
    assert step.status == "pass"
    assert "no recognizable groups" in step.detail and "check this node by hand" in step.detail
    assert "Welcome" not in step.detail and "10.0.0.9" not in step.detail


def test_shared_ssh_keys_include_previously_enrolled_hosts():
    from logs_migrate.trust import shared_ssh_keys

    enrolled = {"kind": "ssh", "name": "node-a:22", "fingerprint": "a" * 64}
    identities = [
        Identity("ssh", "node-b:22", "a" * 64),
        Identity("ssh", "node-c:22", "b" * 64),
        Identity("tls", "node-d:443", "a" * 64),
    ]
    assert shared_ssh_keys([enrolled, *identities]) == [("node-b:22", "node-a:22")]


@pytest.mark.parametrize("count_text", ["", " messages=not-a-number"])
def test_missing_sealed_message_count_stays_unknown(rigged, count_text):
    env, _, _, ssh, _ = rigged
    ssh.replies[("src-1.lab", ReadOperation.INVENTORY)] = (
        f"id={A}, status=archived, startTime=1780543623050, endTime=1792570139600,{count_text}"
    )
    inventory = env.node_inventory("src-1.lab")
    assert inventory.buckets[0].messages is None
    cluster = env._cluster_from_nodes("c", "8.18.7", [inventory], (), "i")
    assert cluster.buckets[0].messages is None


def test_live_unknown_selected_count_does_not_become_zero(tmp_path):
    from logs_migrate.gui import viewmodel as vm
    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.reporting import markdown

    env, engine = _live_rig(tmp_path)
    env.data["source"]["buckets"][0]["messages"] = None
    run = engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
        Policy(poll_seconds=0),
    )
    assert run["plan"]["selected"][0]["messages"] is None
    assert "Unavailable" in str(vm.plan_view(run, None))
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    engine.status()
    result = engine.verify(run["id"])
    assert result["expected_count"] is None
    assert result["actual_count"] == 6
    assert "Unavailable" in str(vm.verification_view(result, engine.status(), 0, False, run))
    assert "unavailable" in markdown(engine.journal.load(run["id"]), []).lower()


def test_external_window_overlap_warns_only_with_destination_inventory(tmp_path):
    from dataclasses import replace

    from logs_migrate.models import Policy, parse_time
    from logs_migrate.ports import History, Transfer

    env, engine = _live_rig(tmp_path)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    source, destination = env.discover()
    env.transfer = lambda: Transfer("", "idle", destination=destination.id)
    env.history = lambda: (History("src", destination.id, window, (), (), True, "old-task"),)
    with pytest.raises(MigrationError) as error:
        engine.plan(window, Policy(poll_seconds=0))
    assert error.value.code == Code.OVERLAP
    assert "old-task" in error.value.message and "Selected groups" in error.value.message
    env.discover = lambda: (source, replace(destination, inventory_available=True))
    run = engine.plan(window, Policy(poll_seconds=0))
    assert any(
        "old-task" in warning and "not on the destination" in warning
        for warning in run["plan"]["warnings"]
    )


def test_overlap_detail_never_carries_raw_remote_task_text(tmp_path):
    from logs_migrate.models import Policy, parse_time
    from logs_migrate.ports import History, Transfer

    env, engine = _live_rig(tmp_path)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    _, destination = env.discover()
    env.transfer = lambda: Transfer("", "idle", destination=destination.id)
    hostile = "t\x1b[31m\nforged " + "x" * 200
    env.history = lambda: (History("src", destination.id, window, (), (), True, hostile),)
    with pytest.raises(MigrationError) as error:
        engine.plan(window, Policy(poll_seconds=0))
    message = error.value.message
    assert "\x1b" not in message and "\nforged" not in message and "x" * 65 not in message


def test_live_bucket_verification_checks_destination_counts(tmp_path):
    from dataclasses import replace

    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    env, engine = _live_rig(tmp_path)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    run = engine.plan(window, Policy(poll_seconds=0))
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    engine.status()
    source, destination = env.discover()
    selected_ids = {b["id"] for b in run["plan"]["selected"]}
    matching = tuple(
        replace(b, node=destination.nodes[0].id) for b in source.buckets if b.id in selected_ids
    )
    env.destination_cluster = lambda fresh=False: replace(
        destination, buckets=matching, inventory_available=True
    )
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_success", str(result)
    assert result["bucket_check"]["matched_groups"] == len(matching)
    assert result["bucket_check"]["matched_events"] == result["bucket_check"]["total_events"]
    env.destination_cluster = lambda fresh=False: replace(
        destination, buckets=matching[:-1], inventory_available=True
    )
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_discrepancy"
    assert len(result["bucket_check"]["unmatched"]) == 1


@pytest.mark.parametrize("matching_last", [True, False])
def test_verification_rejects_conflicting_repeated_destination_entries(tmp_path, matching_last):
    from dataclasses import replace

    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    env, engine = _live_rig(tmp_path)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    run = engine.plan(window, Policy(poll_seconds=0))
    engine.run(run["id"], active_ack=ACTIVE_ACK)
    engine.status()
    source, destination = env.discover()
    ids = {b["id"] for b in run["plan"]["selected"]}
    matching = tuple(
        replace(b, node=destination.nodes[0].id) for b in source.buckets if b.id in ids
    )
    differing = replace(matching[0], messages=matching[0].messages - 1)
    copies = (differing, matching[0]) if matching_last else (matching[0], differing)
    env.destination_cluster = lambda fresh=False: replace(
        destination, buckets=(*copies, *matching[1:]), inventory_available=True
    )
    result = engine.verify(run["id"])
    assert result["outcome"] != "verified_success"
    assert any(matching[0].id in key for key in result["bucket_check"]["unmatched"])
    env.destination_cluster = lambda fresh=False: replace(
        destination,
        buckets=(replace(matching[0], messages=None), differing, matching[0], *matching[1:]),
        inventory_available=True,
    )
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_discrepancy"
    env.destination_cluster = lambda fresh=False: replace(
        destination, buckets=(matching[0], matching[0], *matching[1:]), inventory_available=True
    )
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_success"
    assert result["bucket_check"]["matched_groups"] == len(matching)


def test_live_verification_reports_count_loss_and_incomplete_copy_separately(tmp_path):
    from dataclasses import replace

    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    env, engine = _live_rig(tmp_path)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    run = engine.plan(window, Policy(poll_seconds=0))
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    engine.status()
    source, destination = env.discover()
    ids = {b["id"] for b in run["plan"]["selected"]}
    matching = tuple(
        replace(b, node=destination.nodes[0].id) for b in source.buckets if b.id in ids
    )

    def verify(buckets):
        env.destination_cluster = lambda fresh=False: replace(
            destination, buckets=buckets, inventory_available=True
        )
        return engine.verify(run["id"])

    lost = verify((replace(matching[0], messages=matching[0].messages - 1), *matching[1:]))
    assert lost["outcome"] == "verified_discrepancy"
    assert lost["bucket_check"]["unmatched"] == [
        next(f"{b.node}/{b.id}" for b in source.buckets if b.id == matching[0].id)
    ]
    assert "incomplete_destination_copies" not in lost
    partial = verify((replace(matching[0], complete=False), *matching[1:]))
    assert partial["incomplete_destination_copies"] == [f"{matching[0].node}/{matching[0].id}"]
    assert partial["outcome"] == "verified_discrepancy"


def test_live_bucket_verification_does_not_claim_difference_for_unknown_count(tmp_path):
    from dataclasses import replace

    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time

    env, engine = _live_rig(tmp_path)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    run = engine.plan(window, Policy(poll_seconds=0))
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    engine.status()
    source, destination = env.discover()
    ids = {b["id"] for b in run["plan"]["selected"]}
    matching = tuple(
        replace(b, node=destination.nodes[0].id) for b in source.buckets if b.id in ids
    )
    matching = (replace(matching[0], messages=None), *matching[1:])
    env.destination_cluster = lambda fresh=False: replace(
        destination, buckets=matching, inventory_available=True
    )
    result = engine.verify(run["id"])
    assert result["outcome"] == "incomplete_verification"
    assert result["bucket_check"]["unknown"] and not result["bucket_check"]["unmatched"]


def test_empty_active_sentinel_does_not_hide_dated_open_groups():
    from logs_migrate.models import Bucket, last_sealed_day

    dated = Bucket(A, "a", "", "active", 0, 5, 5 * DAY, 6 * DAY, None, True, False)
    empty = Bucket(B, "a", "", "active", 0, 0, None, None, None, False, False, empty_proven=True)
    assert last_sealed_day((dated, empty), zone="UTC") == "1970-01-05"


def test_plan_lists_the_requested_days_an_open_bucket_with_recorded_dates_shares(tmp_path):
    from conftest import FIXTURES

    from logs_migrate.models import Policy, parse_time

    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-04T00:00:00Z"))
    data = json.loads((FIXTURES / "sealed-populated.fixture").read_text())

    def days(label, start, end):
        source = json.loads(json.dumps(data["source"]))
        source["buckets"][3].update(
            start=parse_time(start), end=parse_time(end), bounds_proven=True
        )
        _, engine = _live_rig(tmp_path / label, {"source": source})
        return engine.plan(window, Policy(poll_seconds=0))["plan"]["open_bucket_days"]

    assert days("before", "2026-02-20T00:00:00Z", "2026-06-08T00:00:00Z") == [
        "2026-03-01",
        "2026-03-02",
        "2026-03-03",
    ]
    assert days("inside", "2026-03-02T12:00:00Z", "2026-06-08T00:00:00Z") == [
        "2026-03-02",
        "2026-03-03",
    ]
    assert days("after", "2026-03-04T00:00:00Z", "2026-06-08T00:00:00Z") == []
    assert days("ended", "2026-01-01T00:00:00Z", "2026-02-28T23:59:59Z") == []


def test_a_fresh_destination_read_rereads_only_the_destination_listing(rigged, monkeypatch):
    env, source, destination, ssh = trusted(rigged)
    monkeypatch.setattr(LiveEnvironment, "_destination_ssh_enabled", lambda self: True)
    xml = '<distributed><daemon host="dst.lab"/></distributed>'
    ssh.replies.update(
        {
            ("dst.lab", ReadOperation.MEMBERS): xml,
            ("dst.lab", ReadOperation.SERVICE): "active\n",
            ("dst.lab", ReadOperation.SPACE): "Size Used Avail\n1000 100 900\n",
            ("dst.lab", ReadOperation.INVENTORY): "",
            ("dst.lab", ReadOperation.USAGE): "",
        }
    )
    destination.records += [session()] + [
        record(*item)
        for _ in range(3)
        for item in (
            ("GET", "/api/v1/version", {"version": "9.0.2.0200.25430789"}),
            ("GET", "/api/v1/partitioning", {"partitions": []}),
        )
    ]

    def reads(host):
        return [op for h, op in ssh.reads if h == host and op == ReadOperation.INVENTORY]

    env.destination_cluster()
    env.destination_cluster()
    assert len(reads("dst.lab")) == 1
    env.destination_cluster(fresh=True)
    assert len(reads("dst.lab")) == 2
    assert reads("src-1.lab") == [] and reads("src-2.lab") == []


def _verified_rig(tmp_path, *, skip=None, drop_events=False, unknown_first=False):
    from dataclasses import replace

    from logs_migrate.models import ACTIVE_ACK, Policy, parse_time
    from logs_migrate.ports import Transfer

    env, engine = _live_rig(tmp_path)
    window = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    run = engine.plan(window, Policy(poll_seconds=0))
    engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)
    destination_id = run["plan"]["destination"]["id"]
    if skip:
        env.transfer = lambda: Transfer(
            "sim-1", "completed", (), (skip,), destination=destination_id
        )
    engine.status()
    if drop_events:
        env.data["destination_events"] = [
            e for e in env.data["destination_events"] if e.get("cohort") == "baseline"
        ]
    source, destination = env.discover()
    ids = {b["id"] for b in run["plan"]["selected"]} - {skip}
    landed = tuple(replace(b, node=destination.nodes[0].id) for b in source.buckets if b.id in ids)
    if unknown_first:
        landed = (replace(landed[0], messages=None), *landed[1:])
    env.destination_cluster = lambda fresh=False: replace(
        destination, buckets=landed, inventory_available=True
    )
    discoveries = []
    original = env.discover
    env.discover = lambda: discoveries.append(1) or original()
    return env, engine, run, discoveries


def test_verify_reads_only_the_destination_after_the_task(tmp_path):
    _, engine, run, discoveries = _verified_rig(tmp_path)
    assert engine.verify(run["id"])["outcome"] == "verified_success"
    assert discoveries == []


def test_a_group_the_destination_skipped_reaches_the_not_received_guidance(tmp_path):
    from logs_migrate.gui import viewmodel as vm

    skipped = "00000000-0000-0000-0000-000000000001"
    _, engine, run, _ = _verified_rig(tmp_path, skip=skipped)
    result = engine.verify(run["id"])
    assert result["outcome"] == result["daily_outcome"]
    assert result["reason"] == "selected_buckets_not_received"
    assert result["bucket_check"]["unmatched"] == ["src-1/" + skipped]
    view = vm.verification_view(result, engine.status(), 0, False, run)
    assert view["outcome"] == "partial"
    tone, title, text = view["banner"]
    assert "not received" in title
    assert "covers the missing history again" in text
    assert "2 of 3 groups registered" in text


def test_an_unknown_group_count_never_hides_a_daily_discrepancy(tmp_path):
    from logs_migrate.gui import viewmodel as vm

    _, engine, run, _ = _verified_rig(tmp_path, drop_events=True, unknown_first=True)
    result = engine.verify(run["id"])
    assert result["bucket_check"]["unknown"] and not result["bucket_check"]["unmatched"]
    assert result["outcome"] == "verified_discrepancy"
    tone, title, text = vm.verification_view(result, engine.status(), 0, False, run)["banner"]
    assert title == "Group counts unavailable" and "Daily counts differ" in text


def test_an_unknown_group_count_alone_is_incomplete_and_the_banner_shows_both_checks(tmp_path):
    from logs_migrate.gui import viewmodel as vm

    _, engine, run, _ = _verified_rig(tmp_path, unknown_first=True)
    result = engine.verify(run["id"])
    assert result["outcome"] == "incomplete_verification" and not result["complete"]
    tone, title, text = vm.verification_view(result, engine.status(), 0, False, run)["banner"]
    assert title == "Group counts unavailable"
    assert text.index("Group 1") < text.index("Daily counts")
    assert result["bucket_check"]["unknown"][0] not in text


def test_matching_groups_with_a_daily_difference_show_the_difference_in_the_banner(tmp_path):
    from logs_migrate.gui import viewmodel as vm

    _, engine, run, _ = _verified_rig(tmp_path, drop_events=True)
    result = engine.verify(run["id"])
    assert result["outcome"] == "verified_discrepancy" and not result["bucket_check"]["unmatched"]
    tone, title, text = vm.verification_view(result, engine.status(), 0, False, run)["banner"]
    assert title == "Selected groups checked" and tone == "bad"
    assert text.startswith("3 of 3 groups registered") and "Daily counts differ" in text


def test_verify_reads_the_destination_inventory_once(tmp_path):
    env, engine, run, _ = _verified_rig(tmp_path)
    original = env.destination_cluster
    reads = []
    env.destination_cluster = lambda fresh=False: reads.append(fresh) or original(fresh=fresh)
    assert engine.verify(run["id"])["outcome"] == "verified_success"
    assert reads == [True]


def test_verify_refuses_a_changed_destination_before_settling_or_listing(tmp_path):
    env, engine, run, _ = _verified_rig(tmp_path)
    destination_id = run["plan"]["destination"]["id"]
    before = engine.journal.transfers(destination_id)
    listed = []
    env.destination_topology = lambda: (destination_id, "another-identity")
    env.destination_cluster = lambda fresh=False: listed.append(fresh)
    result = engine.verify(run["id"])
    assert result["outcome"] == "incomplete_verification" and result["reason"] == "topology_changed"
    assert listed == [] and engine.journal.transfers(destination_id) == before


def test_probe_asks_about_the_destination_ssh_identity_before_any_credential(rigged):
    from dataclasses import replace

    env, _, _, _, _ = rigged
    env.connection = replace(env.connection, destination_ssh_user="root")
    with pytest.raises(TrustRequired) as asked:
        env.probe_endpoints()
    assert "dst.lab:22" in {i.name for i in asked.value.identities}
