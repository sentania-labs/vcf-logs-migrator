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


def test_911_default_exchange_decoder_accepts_token_field_or_single_jwt():
    from logs_migrate.adapters.rest import decode_token_exchange

    assert decode_token_exchange({"token": "abc"}) == "abc"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig-part"
    assert decode_token_exchange({"id": 1, "value": jwt, "note": "x"}) == jwt
    for body in ({}, {"a": "x", "b": "y"}, [], {"value": "not a jwt"}):
        with pytest.raises(MigrationError, match="schema"):
            decode_token_exchange(body)


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


def test_mutations_reach_only_the_documented_routes():
    sent = []

    class Live:
        identity = "x"

        def request(self, method, path, body=None, headers=None):
            sent.append((method, path))
            return {"sessionId": "s"} if path.endswith("sessions") else {}

    client = Logs90(Live(), "admin", Secret("canary"))
    client.test_migration("source.invalid", "root", Secret("canary"))
    client.cancel_migration()
    with pytest.raises(MigrationError, match="unsupported"):
        client.call("POST", "/api/v2/data-migrations/other", {})
    with pytest.raises(MigrationError, match="unsupported"):
        client.call("DELETE", "/api/v1/partitioning/x")
    assert sent == [
        ("POST", "/api/v1/sessions"),
        ("POST", "/api/v2/data-migrations/test"),
        ("DELETE", "/api/v2/data-migrations"),
    ]


def test_forbidden_operations_are_structurally_absent():
    source = SourceSSH("source.invalid", "root", Secret("pw"), "0" * 64, "/missing")
    assert not hasattr(source, "stop_service")
    assert not hasattr(source, "write")
    assert not hasattr(source, "exec")
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


COUNT_PATH = (
    "/api/v1/aggregated-events/timestamp/%3E%3D1/timestamp/%3C2?bin-width=86400000&timeout=580000"
)


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"complete": True, "bins": [{"value": -1}]},
        {"complete": True, "bins": [{"minTimestamp": 1, "value": "1"}]},
        {"complete": True, "bins": [{"minTimestamp": 5, "value": 1}]},
    ],
)
def test_query_schema_failures_fail_closed(body):
    client = Logs90(
        RecordedTransport([session(), record("GET", COUNT_PATH, body)]), "admin", Secret("pw")
    )
    with pytest.raises(MigrationError):
        client.daily_counts(Window(1, 2))


@pytest.mark.parametrize("body", [{}, {"complete": False, "bins": []}, {"bins": []}])
def test_query_partial_answers_are_reported_incomplete_not_as_counts(body):
    client = Logs90(
        RecordedTransport([session(), record("GET", COUNT_PATH, body)]), "admin", Secret("pw")
    )
    assert client.daily_counts(Window(1, 2)) == ({"1970-01-01": 0}, False)


def test_query_empty_complete_is_zero_but_duplicate_bins_are_rejected():
    client = Logs90(
        RecordedTransport([session(), record("GET", COUNT_PATH, {"complete": True, "bins": []})]),
        "admin",
        Secret("pw"),
    )
    assert client.daily_counts(Window(1, 2)) == ({"1970-01-01": 0}, True)
    bins = [{"minTimestamp": 1, "value": 1}] * 2
    client = Logs90(
        RecordedTransport([session(), record("GET", COUNT_PATH, {"complete": True, "bins": bins})]),
        "admin",
        Secret("pw"),
    )
    with pytest.raises(MigrationError):
        client.daily_counts(Window(1, 2))


def test_query_chunks_thirty_days_and_fills_missing_days_with_zero():
    from logs_migrate.models import DAY

    first = (
        "/api/v1/aggregated-events/timestamp/%3E%3D0/timestamp/%3C"
        + str(30 * DAY)
        + "?bin-width=86400000&timeout=580000"
    )
    second = (
        f"/api/v1/aggregated-events/timestamp/%3E%3D{30 * DAY}/timestamp/%3C{31 * DAY}"
        "?bin-width=86400000&timeout=580000"
    )
    bins = [{"minTimestamp": 3 * DAY, "value": 7}, {"minTimestamp": 29 * DAY, "value": 2}]
    client = Logs90(
        RecordedTransport(
            [
                session(),
                record("GET", first, {"complete": True, "bins": bins}),
                record("GET", second, {"complete": True, "bins": []}),
            ]
        ),
        "admin",
        Secret("pw"),
    )
    daily, complete = client.daily_counts(Window(0, 31 * DAY))
    assert complete and len(daily) == 31
    assert daily["1970-01-04"] == 7 and daily["1970-01-30"] == 2 and daily["1970-01-31"] == 0
    assert sum(daily.values()) == 9


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
