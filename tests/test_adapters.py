import hashlib
import json

import pytest

from logs_migrate.adapters.rest import HTTPS, Logs90, Logs911, RecordedTransport
from logs_migrate.adapters.ssh import ReadOperation, RecordedSSH, SourceSSH
from logs_migrate.errors import Code, MigrationError
from logs_migrate.models import Window
from logs_migrate.security import Credentials, Secret


def record(method, path, response=None, error=None):
    return dict(method=method, path=path, response=response, error=error)


def session():
    return record("POST", "/api/v1/sessions", {"sessionId": "fixture-bearer"})


def test_90_auth_and_mover_wire_shape_without_secret_recording():
    class Inspecting(RecordedTransport):
        def request(self, method, path, body=None, headers=None):
            if path == "/api/v2/data-migrations" and method == "POST":
                assert body == dict(
                    hostName="source.invalid",
                    username="root",
                    password="canary-root",
                    bucketStartTimeMillis=1,
                    bucketEndTimeMillis=100,
                )
                assert headers == {"Authorization": "Bearer fixture-bearer"}
            return super().request(method, path, body, headers)

    transport = Inspecting(
        [session(), record("POST", "/api/v2/data-migrations", {"fixture": True})]
    )
    client = Logs90(transport, "admin", Secret("canary-admin"))
    assert client.start_migration(
        "source.invalid", "root", Secret("canary-root"), Window(1, 100)
    ) == {"fixture": True}
    assert "canary" not in repr(transport.calls)
    assert "canary" not in repr(client.password)


def test_911_ops_exchange_and_header_are_separate():
    class Inspecting(RecordedTransport):
        def request(self, method, path, body=None, headers=None):
            if path.endswith("exchange"):
                assert body == {"serviceKeys": ["ops-li"]}
                assert headers == {"Authorization": "OpsToken fixture-ops"}
            else:
                assert headers == {"X-JWT-Token": "fixture-jwt"}
            return super().request(method, path, body, headers)

    ops = Inspecting(
        [record("POST", "/suite-api/api/auth/token/exchange", {"fixtureJWT": "fixture-jwt"})]
    )
    logs = Inspecting([record("GET", "/api/v2/data-migrations", {"fixture": "idle"})])
    client = Logs911(logs, ops, Secret("fixture-ops"), lambda body: body["fixtureJWT"])
    assert client.migration_status() == {"fixture": "idle"}
    with pytest.raises(MigrationError, match="unsupported"):
        client.daily_counts(Window(1, 2))


def test_get_auth_expiry_refreshes_once_but_post_never_replays():
    transport = RecordedTransport(
        [
            session(),
            record("GET", "/api/v2/data-migrations", error="authentication_failed"),
            session(),
            record("GET", "/api/v2/data-migrations", {}),
        ]
    )
    client = Logs90(transport, "admin", Secret("pw"))
    assert client.migration_status() == {}
    assert len(transport.calls) == 4
    transport = RecordedTransport(
        [session(), record("POST", "/api/v2/data-migrations", error="authentication_failed")]
    )
    client = Logs90(transport, "admin", Secret("pw"))
    with pytest.raises(MigrationError, match="authentication"):
        client.start_migration("source.invalid", "root", Secret("pw"), Window(1, 2))
    assert transport.calls.count(("POST", "/api/v2/data-migrations")) == 1


def test_all_non_fixture_mutations_are_release_gated():
    class Rejecting:
        def request(self, *args, **kwargs):
            pytest.fail("Release gate must run before any network request")

    client = Logs90(Rejecting(), "admin", Secret("canary"))
    with pytest.raises(MigrationError, match="unsupported"):
        client.start_migration("source.invalid", "root", Secret("canary"), Window(1, 2))
    with pytest.raises(MigrationError, match="unsupported"):
        client.test_migration("source.invalid", "root", Secret("canary"))
    with pytest.raises(MigrationError, match="unsupported"):
        client.cancel_migration()
    with pytest.raises(MigrationError, match="unsupported"):
        client.create_partition({})
    with pytest.raises(MigrationError, match="unsupported"):
        client.call("POST", "/api/v2/data-migrations", {})


def test_forbidden_operations_are_structurally_absent():
    source = SourceSSH("source.invalid", "root", Secret("pw"), "0" * 64, "/missing")
    assert not hasattr(source, "stop_service")
    assert not hasattr(source, "write")
    client = Logs90(RecordedTransport([]), "admin", Secret("pw"))
    for method, path in [
        ("DELETE", "/api/v1/partitioning"),
        ("POST", "/api/v1/config"),
        ("GET", "//other.invalid/token"),
    ]:
        with pytest.raises(MigrationError, match="unsupported"):
            client.call(method, path)
    ssh = RecordedSSH([(ReadOperation.INVENTORY, "fixture inventory")])
    assert ssh.read(ReadOperation.INVENTORY) == "fixture inventory"
    assert ssh.commands == ["/usr/lib/loginsight/application/sbin/bucket-index show"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"complete": False, "bins": []},
        {"complete": True},
        {"complete": True, "bins": [{"value": -1}]},
    ],
)
def test_query_completeness_and_schema_fail_closed(body):
    path = (
        "/api/v1/aggregated-events/timestamp/%3E%3D1/timestamp/%3C2"
        "?bin-width=86400000&timeout=580000"
    )
    client = Logs90(
        RecordedTransport([session(), record("GET", path, body)]), "admin", Secret("pw")
    )
    with pytest.raises(MigrationError):
        client.daily_counts(Window(1, 2))


def test_query_empty_complete_is_zero_but_duplicate_bins_are_rejected():
    path = (
        "/api/v1/aggregated-events/timestamp/%3E%3D1/timestamp/%3C2"
        "?bin-width=86400000&timeout=580000"
    )
    client = Logs90(
        RecordedTransport([session(), record("GET", path, {"complete": True, "bins": []})]),
        "admin",
        Secret("pw"),
    )
    assert client.daily_counts(Window(1, 2)) == {"1970-01-01": 0}
    bins = [{"minTimestamp": 1, "value": 1}] * 2
    client = Logs90(
        RecordedTransport([session(), record("GET", path, {"complete": True, "bins": bins})]),
        "admin",
        Secret("pw"),
    )
    with pytest.raises(MigrationError):
        client.daily_counts(Window(1, 2))


def test_certificate_pin_checked_before_any_credential_is_sent(monkeypatch):
    sent = []

    class Socket:
        def getpeercert(self, binary_form):
            return b"unexpected-certificate"

    class Connection:
        sock = Socket()

        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            pass

        def request(self, *args, **kwargs):
            sent.append((args, kwargs))

        def close(self):
            pass

    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    transport = HTTPS(
        "https://logs.invalid:9543", certificate_sha256=hashlib.sha256(b"enrolled").hexdigest()
    )
    with pytest.raises(MigrationError, match="authentication"):
        transport.request("POST", "/api/v1/sessions", {"password": "canary"})
    assert not sent


@pytest.mark.parametrize(
    "url", ["http://logs.invalid", "https://u:canary@logs.invalid", "https://logs.invalid/path"]
)
def test_insecure_urls_fail_without_echoing_credentials(url):
    with pytest.raises(MigrationError) as error:
        HTTPS(url, certificate_sha256="0" * 64)
    assert "canary" not in str(error.value)


def test_credentials_same_password_nodes_and_stream_not_argv():
    from io import StringIO

    credentials = Credentials.from_stream(StringIO(json.dumps({"destination_ssh": "canary"})))
    nodes = credentials.destination_nodes(("n1", "n2", "n3"), same_password=True)
    assert all(secret.reveal() == "canary" for secret in nodes.values())
    assert "canary" not in repr(nodes)
    credentials.clear()
    with pytest.raises(MigrationError, match=Code.AUTH):
        credentials.get("destination_ssh")
