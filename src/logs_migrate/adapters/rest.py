"""Logs REST clients for 8.18, 9.0 and 9.1.1 with an explicit route allowlist.

Request bodies follow docs/validation-2026-11-06-official-api.md and docs/config-migration.md.
Response decoding is lenient where no raw capture exists (task status, token exchange); the
`selftest` command prints the key names it saw so a decoder can be tightened from evidence.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import ssl
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from ..errors import Code, MigrationError, Rejection, require, safe_boundary
from ..models import DAY, Window
from ..security import Secret

MAX_BODY = 16 * 1024 * 1024
COUNT_CHUNK_DAYS = 30  # One aggregated-events query returns at most 100 bins (newest first).
COUNT_TIMEOUT_SECONDS = 600.0
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")
JWT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict | list: ...


def peer_certificate_sha256(host: str, port: int, timeout: float = 15) -> str:
    """Read the presented leaf certificate's digest without trusting it. No request is sent."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
    except Exception:
        raise MigrationError(Code.CONNECTION) from None


def split_endpoint(value: str, default_port: int) -> tuple[str, int]:
    """`host`, `host:port` or `https://host:port` to a host and port pair."""
    text = value.strip()
    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        require(parsed.scheme == "https" and parsed.hostname is not None, Code.INVALID_INPUT)
        require(parsed.username is None and parsed.password is None, Code.INVALID_INPUT)
        return parsed.hostname, parsed.port or default_port
    parsed = urllib.parse.urlsplit("https://" + text)
    require(
        parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query,
        Code.INVALID_INPUT,
    )
    return parsed.hostname, parsed.port or default_port


class HTTPS:
    """JSON over TLS. The enrolled certificate digest is checked before any request is sent."""

    def __init__(
        self,
        base_url: str,
        *,
        certificate_sha256: str,
        ca_file: str | None = None,
        timeout: float = 60,
    ):
        parsed = urllib.parse.urlsplit(base_url)
        require(
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment,
            Code.INVALID_INPUT,
        )
        require(bool(re.fullmatch(r"[0-9a-f]{64}", certificate_sha256)), Code.INVALID_INPUT)
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.pin = certificate_sha256
        self.timeout = timeout
        try:
            if ca_file is not None:
                self.context = ssl.create_default_context(cafile=ca_file)
            else:
                # Appliances ship self-signed certificates; the enrolled digest is the identity.
                self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                self.context.check_hostname = False
                self.context.verify_mode = ssl.CERT_NONE
        except Exception:
            raise MigrationError(Code.CONNECTION) from None

    @property
    def identity(self) -> str:
        return self.pin

    def request(self, method, path, body=None, headers=None, timeout=None):
        require(
            path.startswith("/")
            and not path.startswith("//")
            and "\r" not in path
            and "\n" not in path,
            Code.INVALID_INPUT,
        )
        connection = http.client.HTTPSConnection(
            self.host, self.port, timeout=timeout or self.timeout, context=self.context
        )
        try:
            # Handshake and enrolled certificate check before any credential leaves the client.
            connection.connect()
            require(
                hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
                == self.pin,
                Code.AUTH,
            )
            connection.request(
                method,
                path,
                body=None if body is None else json.dumps(body).encode(),
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            response = connection.getresponse()
            if response.status in {401, 403}:
                raise Rejection(Code.AUTH)
            if not 200 <= response.status < 300:  # No credential redirects.
                raise Rejection(Code.CONNECTION)
            raw = response.read(MAX_BODY + 1)
            require(len(raw) <= MAX_BODY, Code.SCHEMA)
            if not raw:
                return {}
            parsed = json.loads(raw)
            require(isinstance(parsed, (dict, list)), Code.SCHEMA)
            return parsed
        except MigrationError:
            raise
        except Exception:
            raise MigrationError(Code.CONNECTION) from None
        finally:
            connection.close()


CONFIG_PATHS = {
    "/api/v1/" + name for name in ("partitioning", "log-filter", "log-mask", "log-forwarder")
}
# Every route the wrapper may call. Nothing else reaches the wire, whatever the caller asks.
ROUTES = {
    ("GET", "/api/v1/version"),
    ("POST", "/api/v1/sessions"),
    ("POST", "/api/v2/data-migrations/test"),
    ("POST", "/api/v2/data-migrations"),
    ("GET", "/api/v2/data-migrations"),
    ("DELETE", "/api/v2/data-migrations"),
    ("POST", "/api/v1/log-forwarder/batch"),
} | {(method, path) for path in CONFIG_PATHS for method in ("GET", "POST")}


def allowed(method: str, path: str) -> bool:
    if (method, path) in ROUTES:
        return True
    if method == "GET" and path.startswith("/api/v1/aggregated-events/timestamp/"):
        return True
    if method == "PUT":
        for base in CONFIG_PATHS:
            rest = path[len(base) + 1 :] if path.startswith(base + "/") else ""
            if rest and "/" not in rest and "?" not in rest and "#" not in rest:
                return True
    return False


def parse_version(response) -> str:
    """`{"version": "8.18.7-25423541", ...}` to `8.18.7`; any string field holding a version."""
    require(isinstance(response, dict), Code.SCHEMA)
    for key in ("version", "releaseVersion", "productVersion"):
        value = response.get(key)
        if isinstance(value, str):
            match = VERSION_PATTERN.search(value)
            if match:
                return match.group(0)
    for value in response.values():
        if isinstance(value, str):
            match = VERSION_PATTERN.search(value)
            if match:
                return match.group(0)
    raise MigrationError(Code.SCHEMA)


def decode_token_exchange(response) -> str:
    """The Ops exchange body is not captured in the repository; accept a `token` field or the
    single JWT-shaped string in the body. Anything else is an unrecognized schema."""
    require(isinstance(response, dict), Code.SCHEMA)
    for key in ("token", "jwt", "accessToken", "access_token"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    candidates = [
        v for v in response.values() if isinstance(v, str) and JWT_PATTERN.fullmatch(v.strip())
    ]
    require(len(candidates) == 1, Code.SCHEMA)
    return candidates[0].strip()


class Logs90:
    """Logs session (Bearer) client; serves 8.18 sources and 9.0 destinations."""

    def __init__(self, transport: Transport, username: str, password: Secret):
        self.transport = transport
        self.username = username
        self.password = password
        self.token: Secret | None = None

    @property
    def identity(self) -> str:
        return getattr(self.transport, "identity", "")

    @safe_boundary
    def authenticate(self):
        response = self.transport.request(
            "POST",
            "/api/v1/sessions",
            {"username": self.username, "password": self.password.reveal(), "provider": "Local"},
        )
        require(
            isinstance(response, dict) and isinstance(response.get("sessionId"), str), Code.SCHEMA
        )
        self.token = Secret(response["sessionId"])

    def headers(self):
        if self.token is None:
            self.authenticate()
        return {"Authorization": "Bearer " + self.token.reveal()}

    def call(self, method, path, body=None, timeout=None):
        require(allowed(method, path), Code.UNSUPPORTED)
        extra = {"timeout": timeout} if timeout else {}
        try:
            return self.transport.request(method, path, body, self.headers(), **extra)
        except MigrationError as error:
            if error.code == Code.AUTH and method == "GET":
                self.token = None
                return self.transport.request(method, path, body, self.headers(), **extra)
            # Never replay a POST, PUT or DELETE after an uncertain response.
            raise
        except Exception:
            raise MigrationError(Code.CONNECTION) from None

    def version(self) -> str:
        return parse_version(self.call("GET", "/api/v1/version"))

    def test_migration(self, host: str, username: str, password: Secret):
        return self.call(
            "POST",
            "/api/v2/data-migrations/test",
            {"hostName": host, "username": username, "password": password.reveal()},
        )

    def start_migration(self, host: str, username: str, password: Secret, window: Window):
        return self.call(
            "POST",
            "/api/v2/data-migrations",
            {
                "hostName": host,
                "username": username,
                "password": password.reveal(),
                "bucketStartTimeMillis": window.start,
                "bucketEndTimeMillis": window.end,
            },
        )

    def migration_status(self):
        return self.call("GET", "/api/v2/data-migrations")

    def cancel_migration(self):
        return self.call("DELETE", "/api/v2/data-migrations")

    def partitions(self):
        return self.call("GET", "/api/v1/partitioning")

    def daily_counts(self, window: Window) -> tuple[dict[str, int], bool]:
        """Per-UTC-day counts over the window, in 30-day queries with 1-day bins.

        Returns the counts and whether every response was `complete`. Partial responses
        (right after a service start, or on a busy node) are evidence of nothing.
        """
        daily: dict[str, int] = {}
        complete = True
        cursor = window.start
        while cursor < window.end:
            end = min(cursor + COUNT_CHUNK_DAYS * DAY, window.end)
            path = (
                f"/api/v1/aggregated-events/timestamp/%3E%3D{cursor}/timestamp/%3C{end}"
                f"?bin-width={DAY}&timeout=580000"
            )
            # The server is asked for up to 580 s; the socket waits a little longer.
            response = self.call("GET", path, timeout=COUNT_TIMEOUT_SECONDS)
            require(isinstance(response, dict), Code.SCHEMA)
            if response.get("complete") is not True:
                complete = False
            bins = response.get("bins", [])
            require(isinstance(bins, list) and len(bins) <= 100, Code.SCHEMA)
            seen: set[int] = set()
            for row in bins:
                require(
                    isinstance(row, dict)
                    and type(row.get("value")) is int
                    and row["value"] >= 0
                    and type(row.get("minTimestamp")) is int,
                    Code.SCHEMA,
                )
                require(cursor // DAY * DAY <= row["minTimestamp"] < end, Code.SCHEMA)
                require(row["minTimestamp"] not in seen, Code.SCHEMA)
                seen.add(row["minTimestamp"])
                day = datetime.fromtimestamp(row["minTimestamp"] / 1000, UTC).date().isoformat()
                daily[day] = daily.get(day, 0) + row["value"]
            day_cursor = cursor
            while day_cursor < end:
                day = datetime.fromtimestamp(day_cursor / 1000, UTC).date().isoformat()
                daily.setdefault(day, 0)
                day_cursor = (day_cursor // DAY + 1) * DAY
            cursor = end
        return dict(sorted(daily.items())), complete


class Logs911(Logs90):
    """OpsToken exchange plus X-JWT-Token to the Logs FQDN on 9543 (9.1.1 preview)."""

    def __init__(
        self,
        logs_transport: Transport,
        ops_transport: Transport,
        ops_token: Secret,
        decode_exchange: Callable[[dict | list], str] | None = None,
    ):
        self.transport = logs_transport
        self.ops_transport = ops_transport
        self.ops_token = ops_token
        self.decode_exchange = decode_exchange or decode_token_exchange
        self.token = None
        self.username = ""
        self.password = None

    def authenticate(self):
        try:
            response = self.ops_transport.request(
                "POST",
                "/suite-api/api/auth/token/exchange",
                {"serviceKeys": ["ops-li"]},
                {"Authorization": "OpsToken " + self.ops_token.reveal()},
            )
            self.token = Secret(self.decode_exchange(response))
        except MigrationError:
            raise
        except Exception:
            raise MigrationError(Code.SCHEMA) from None

    def headers(self):
        if self.token is None:
            self.authenticate()
        return {"X-JWT-Token": self.token.reveal()}

    def daily_counts(self, window: Window) -> tuple[dict[str, int], bool]:
        # 9.1 is an OpenSearch-backed engine; its query route is not qualified in this
        # repository, so counts there are reported as unavailable rather than guessed.
        raise MigrationError(Code.UNSUPPORTED)


class RecordedTransport:
    """Replay sanitized wire captures. Never records live request bodies or authentication."""

    identity = "recorded"

    def __init__(self, records: list[dict]):
        self.records = list(records)
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, body=None, headers=None, timeout=None):
        require(bool(self.records), Code.SCHEMA)
        record = self.records.pop(0)
        require((method, path) == (record["method"], record["path"]), Code.SCHEMA)
        self.calls.append((method, path))
        if record.get("error"):
            raise MigrationError(Code(record["error"]))
        return record["response"]
