"""Known wire operations only; unknown vendor response shapes require a qualified codec.

The repository documents request bodies, but does not include raw task/9.1 query
responses. Do not mistake the normalized simulator schema for those wire schemas.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import ssl
import urllib.parse
from collections.abc import Callable
from typing import Protocol

from ..errors import Code, MigrationError, require, safe_boundary
from ..models import Window
from ..security import Secret


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict | list: ...


class HTTPS:
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
            self.context = ssl.create_default_context(cafile=ca_file)
        except Exception:
            raise MigrationError(Code.CONNECTION) from None

    def request(self, method, path, body=None, headers=None):
        require(
            path.startswith("/")
            and not path.startswith("//")
            and "\r" not in path
            and "\n" not in path,
            Code.INVALID_INPUT,
        )
        connection = http.client.HTTPSConnection(
            self.host, self.port, timeout=self.timeout, context=self.context
        )
        try:
            # Handshake and BOTH CA/hostname and enrolled certificate checks before credentials.
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
            require(response.status not in {401, 403}, Code.AUTH)
            require(200 <= response.status < 300, Code.CONNECTION)  # No credential redirects.
            raw = response.read(16 * 1024 * 1024 + 1)
            require(len(raw) <= 16 * 1024 * 1024, Code.SCHEMA)
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


class Logs90:
    def __init__(self, transport: Transport, username: str, password: Secret):
        self.transport = transport
        self.username = username
        self.password = password
        self.token: Secret | None = None

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

    def call(self, method, path, body=None):
        allowed = {
            ("POST", "/api/v2/data-migrations/test"),
            ("POST", "/api/v2/data-migrations"),
            ("GET", "/api/v2/data-migrations"),
            ("DELETE", "/api/v2/data-migrations"),
            ("GET", "/api/v1/partitioning"),
            ("POST", "/api/v1/partitioning"),
        }
        require(
            (method, path) in allowed
            or (method == "GET" and path.startswith("/api/v1/aggregated-events/timestamp/")),
            Code.UNSUPPORTED,
        )
        if method != "GET":
            require(isinstance(self.transport, RecordedTransport), Code.UNSUPPORTED)
        try:
            return self.transport.request(method, path, body, self.headers())
        except MigrationError as error:
            if error.code == Code.AUTH and method == "GET":
                self.token = None
                return self.transport.request(method, path, body, self.headers())
            # Never replay a POST or DELETE after an uncertain response.
            raise
        except Exception:
            raise MigrationError(Code.CONNECTION) from None

    def test_migration(self, host: str, username: str, password: Secret):
        require(isinstance(self.transport, RecordedTransport), Code.UNSUPPORTED)
        return self.call(
            "POST",
            "/api/v2/data-migrations/test",
            {"hostName": host, "username": username, "password": password.reveal()},
        )

    def start_migration(self, host: str, username: str, password: Secret, window: Window):
        require(isinstance(self.transport, RecordedTransport), Code.UNSUPPORTED)
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
        require(isinstance(self.transport, RecordedTransport), Code.UNSUPPORTED)
        return self.call("DELETE", "/api/v2/data-migrations")

    def partitions(self):
        return self.call("GET", "/api/v1/partitioning")

    def daily_counts(self, window: Window) -> dict[str, int]:
        """One frozen daily interval per request: avoids the legacy 100-bin response cap.

        These are count evidence only. Exact cohort/multiset verification requires a
        separate qualified query adapter; counts alone cannot detect compensated loss.
        """
        from datetime import UTC, datetime

        from ..models import DAY

        daily = {}
        cursor = window.start
        while cursor < window.end:
            end = min((cursor // DAY + 1) * DAY, window.end)
            path = (
                f"/api/v1/aggregated-events/timestamp/%3E%3D{cursor}/timestamp/%3C{end}"
                f"?bin-width={DAY}&timeout=580000"
            )
            response = self.call("GET", path)
            require(
                isinstance(response, dict) and response.get("complete") is True, Code.INCOMPLETE
            )
            bins = response.get("bins")
            require(isinstance(bins, list) and len(bins) <= 1, Code.SCHEMA)
            count = 0
            for row in bins:
                require(
                    isinstance(row, dict)
                    and type(row.get("value")) is int
                    and row["value"] >= 0
                    and type(row.get("minTimestamp")) is int,
                    Code.SCHEMA,
                )
                require(cursor // DAY * DAY <= row["minTimestamp"] < end, Code.SCHEMA)
                count += row["value"]
            day = datetime.fromtimestamp(cursor / 1000, UTC).date().isoformat()
            daily[day] = count
            cursor = end
        return daily


class Logs911(Logs90):
    """OpsToken exchange plus X-JWT-Token. Response decoding is evidence-versioned."""

    def __init__(
        self,
        logs_transport: Transport,
        ops_transport: Transport,
        ops_token: Secret,
        decode_exchange: Callable[[dict | list], str],
    ):
        self.transport = logs_transport
        self.ops_transport = ops_transport
        self.ops_token = ops_token
        self.decode_exchange = decode_exchange
        self.token = None

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

    def daily_counts(self, window: Window) -> dict[str, int]:
        # 9.1 is an OpenSearch-backed engine. The 9.0 query route must not be reused.
        raise MigrationError(Code.UNSUPPORTED)


class RecordedTransport:
    """Replay sanitized wire captures. Never records live request bodies or authentication."""

    def __init__(self, records: list[dict]):
        self.records = list(records)
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, body=None, headers=None):
        require(bool(self.records), Code.SCHEMA)
        record = self.records.pop(0)
        require((method, path) == (record["method"], record["path"]), Code.SCHEMA)
        self.calls.append((method, path))
        if record.get("error"):
            raise MigrationError(Code(record["error"]))
        return record["response"]
