"""One engine session owned by the worker thread. Thin calls; the engine decides everything.

The record of past runs opens at launch and stays open, so results can be read without
signing in. Signing in adds the two systems and the engine on top of it.
"""

from __future__ import annotations

import json
import platform
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..adapters.simulated import SimulatedEnvironment
from ..adapters.ssh_errors import SSHFailure
from ..engine import Engine
from ..errors import Code, MigrationError, Rejection, require
from ..journal import Journal
from ..models import ACTIVE_ACK, Policy, Window
from ..ports import SystemClock
from ..reporting import display_events, display_run, report_data
from ..security import Credentials
from ..trust import Identity, TrustRequired, TrustStore
from . import copies, copyview
from . import viewmodel as vm


class Failure(Exception):
    """A failure with what the window needs to show it: which box, which days, which stage."""

    def __init__(self, code: str, **data):
        super().__init__(code)
        self.code = code
        self.data = data


# Signing in met a server nobody has confirmed and stopped before sending it anything. The
# window asks about it; anywhere else (a reconnect) this sentence is what the person reads.
UNCONFIRMED = (
    "gui:A server whose identity code has not been confirmed was found. "
    "Test the connections on Connect to see it."
)


class StandInClock(SystemClock):
    """Real time for the record, but no waiting: the stand-in systems answer at once."""

    def sleep(self, seconds: float) -> None:
        return None


# Named engine steps and appliance progress lines, mapped to the stage a screen shows.
ENGINE_STAGES = {
    "start": {"recheck": 0, "count_source": 1, "count_destination": 2, "start": 3},
    "continue": {
        "resume": 0,
        "arrived": 1,
        "recheck": 2,
        "count_source": 2,
        "count_destination": 2,
        "start": 3,
    },
    "stop": {"stop_ask": 0, "stop_confirm": 1, "stop_record": 2},
    "verify": {"verify_ended": 0, "verify_count": 1, "verify_recount": 2, "verify_compare": 3},
}
APPLIANCE_STAGES = {
    "plan": (
        ("Listing buckets", 0),
        ("Sizing buckets", 1),
        ("Checking space", 2),
        ("Planning", 3),
    ),
    "discover": (("Listing buckets", 0), ("Sizing buckets", 1)),
}


def _side(role: str | None) -> str:
    return "source" if role == "source" else "destination"


class Session:
    """Holds the record, the environment, the engine and in-memory credentials."""

    def __init__(self):
        self.env = None
        self.on_progress = None
        self.cancelled = lambda: False
        self.engine = None
        self.journal = None
        self.journal_path: Path | None = None
        self.credentials = None
        self.connection: dict[str, str] = {}
        self.pending: dict[str, Identity] = {}
        self.key_lines: dict[str, str | None] = {}
        self.store = None
        self.want_server = False
        self.job = ""
        self.delay = 0.0
        self.errors: list[str] = []
        self.server_sign_in = False

    @property
    def mode(self) -> str:
        return getattr(self.env, "mode", "simulation") if self.env is not None else "simulation"

    # Stages ------------------------------------------------------------------------------

    def stage(self, index: int) -> None:
        if self.on_progress:
            self.on_progress(f"stage:{index}")
        if self.delay:
            time.sleep(self.delay)  # A stand-in system pauses so a person can watch it.

    def _engine_step(self, name: str) -> None:
        index = ENGINE_STAGES.get(self.job, {}).get(name)
        if index is not None:
            self.stage(index)

    def _appliance_progress(self, message: str) -> None:
        for prefix, index in APPLIANCE_STAGES.get(self.job, ()):
            if message.startswith(prefix):
                self.stage(index)
        if self.on_progress:
            self.on_progress(message)

    def _fail(self, error: MigrationError, **data) -> Failure:
        """Keep the engine's sentence for the troubleshooting file; pass the data along."""
        message = getattr(error, "message", None)
        text = f"{error.code.value}: {message}" if message else error.code.value
        self.errors.append(f"{datetime.now(UTC).isoformat()} {self.job}: {text}")
        del self.errors[:-50]
        refusal = getattr(error, "refusal", None)
        if refusal is None and message:
            if message.startswith("No dated sealed groups overlap"):
                refusal = {"kind": "empty"}
            elif message.startswith("Source groups have duplicate identities"):
                refusal = {"kind": "duplicates"}
        if refusal is not None:
            data.setdefault("refusal", refusal)
        return Failure(text, **data)

    # The record --------------------------------------------------------------------------

    def open_record(self, *, journal: str) -> dict:
        """Open the record of past runs. No system is contacted."""
        path = Path(journal).expanduser()
        if self.journal is not None and self.journal_path != path:
            self.disconnect()
            self.journal.close()
            self.journal = None
        if self.journal is None:
            try:
                self.journal = Journal(path)
            except MigrationError as error:
                raise self._fail(error) from None
            self.journal_path = path
        return self.record()

    def move_record(self, *, folder: str) -> dict:
        """Move the record of past runs to another folder and carry on from there."""
        import shutil

        require(self.journal is not None and self.journal_path is not None, Code.STATE)
        old = self.journal_path
        target = Path(folder).expanduser() / old.name
        if target.exists():
            raise Failure(
                "gui:That folder already holds a record of past runs. Choose another folder."
            )
        self.disconnect()
        self.journal.close()
        self.journal = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                piece = old.with_name(old.name + suffix)
                if piece.exists():
                    shutil.move(str(piece), str(target.with_name(target.name + suffix)))
            state = old.with_suffix(".simulated-state")
            if state.exists():
                shutil.move(str(state), str(target.with_suffix(".simulated-state")))
        except OSError as error:
            self.journal = Journal(old)
            raise Failure(f"gui:Could not move past runs: {error.strerror or error}") from None
        return {"journal": str(target), "record": self.open_record(journal=str(target))}

    def record(self) -> dict:
        require(self.journal is not None, Code.STATE)
        return {
            "runs": [display_run(run) for run in self.journal.runs()],
            "rows": self.journal.transfer_rows(),
        }

    def _copies(self) -> list[dict]:
        record = self.record()
        return copies.build(record["runs"], record["rows"])

    # Connect -----------------------------------------------------------------------------

    def _stand_in(self, fixture: str):
        path = Path(fixture).expanduser()
        if not path.is_file():
            raise Failure("gui:The stand-in systems file was not found: " + str(path))
        try:
            env = SimulatedEnvironment.from_file(
                path, self.journal_path.with_suffix(".simulated-state")
            )
        except MigrationError as error:
            raise self._fail(error) from None
        except Exception:
            raise self._fail(MigrationError(Code.SCHEMA)) from None
        self.delay = env.stage_delay
        return env

    def _live(self):
        from ..adapters.live import Connection, LiveEnvironment

        values = self.connection
        connection = Connection(
            source_host=values["source_host"].strip(),
            destination=values["destination"].strip(),
            source_api_user=values.get("source_api_user") or "admin",
            source_ssh_user=values.get("source_ssh_user") or "root",
            destination_api_user=values.get("destination_api_user") or "admin",
            # Without its password the destination's servers are never contacted.
            destination_ssh_user=(values.get("destination_ssh_user") or "")
            if self.want_server
            else "",
            ops_url=values.get("ops_url") or "",
        )
        env = LiveEnvironment(connection, self.credentials, self.store)
        env.on_progress = self._appliance_progress
        self.delay = 0.0
        return env

    def _store(self, fixture: str) -> TrustStore:
        """Stand-in systems keep their made-up identities apart from real ones."""
        from .settings import config_dir

        wanted = config_dir() / "stand-in" if fixture else config_dir()
        if self.store is None or self.store.directory != wanted:
            self.store = TrustStore(wanted)
        return self.store

    def _roles(self) -> dict[str, str]:
        """Each role's `host:port` as the trust store names it."""
        from ..adapters.rest import split_endpoint

        values = self.connection
        source = values["source_host"].strip().lower()
        names = {"source": f"{source}:9543"}
        host, port = split_endpoint(values["destination"].strip().lower(), 9543)
        names["destination"] = f"{host}:{port}"
        if values.get("ops_url"):
            host, port = split_endpoint(values["ops_url"].strip().lower(), 443)
            names["ops"] = f"{host}:{port}"
        return names

    def _shown(self, role: str, identity: Identity, state: str) -> dict:
        """One identity as the window shows it. What was shown is what can be confirmed."""
        self.pending[identity.name] = identity
        return {
            "kind": "address" if identity.kind == "tls" else "server",
            "role": role,
            "name": identity.name,
            "fingerprint": identity.fingerprint,
            "detail": identity.detail,
            "state": state,
        }

    def _stand_in_servers(self, env, side: str) -> list[Identity]:
        return [
            Identity("ssh", f"{name}:22", digest, "ssh-ed25519")
            for name, digest in env.server_digests(side)
        ]

    def probe(
        self, *, fixture: str, connection: dict[str, str], destination_server: bool = False
    ) -> dict:
        """Find the typed addresses and read the certificate each one presents, and the
        identity code of the server at each. Nothing else is sent, and no password leaves
        this computer. `destination_server` says a destination server sign-in was typed."""
        self.disconnect()
        self.job = "connect"
        self.connection = dict(connection)
        self.credentials = Credentials({}, interactive=False)
        self.store = self._store(fixture)
        self.key_lines = {}
        self.want_server = bool(
            destination_server
            and connection.get("destination_ssh_user")
            and not connection.get("ops_url")
        )
        self.stage(0)
        found: list[tuple[str, Identity, str]] = []
        try:
            if fixture:
                env = self._stand_in(fixture)
                names = self._roles()
                for role in names:
                    side = "ops" if role == "ops" else role
                    typed = connection["ops_url" if role == "ops" else "destination"]
                    if role == "source":
                        typed = connection["source_host"]
                    try:
                        env.check_address(side, typed)
                    except MigrationError as error:
                        other = "destination" if role == "source" else "source"
                        other_found = True
                        if role == "source":
                            try:
                                env.check_address("destination", connection["destination"])
                            except MigrationError:
                                other_found = False
                        raise self._fail(
                            error,
                            side=_side(role),
                            field="ops_address" if role == "ops" else "address",
                            kind="reach",
                            other_found=other_found and other != role,
                        ) from None
                    digest = (
                        env.tls_identity("destination" if role == "ops" else role)
                        if role != "ops"
                        else env.tls_identity("destination")[::-1]
                    )
                    identity = Identity("tls", names[role], digest)
                    found.append((role, identity, self.store.check(identity)))
                for side in ("source", "destination") if self.want_server else ("source",):
                    identity = self._stand_in_servers(env, side)[0]
                    found.append((side, identity, self.store.check(identity)))
            else:
                env = self._live()
                try:
                    found = env.address_identities()
                    try:
                        servers = env.server_identities()
                    except MigrationError as error:
                        role = env.notes.get("stage_role")
                        raise self._fail(
                            error,
                            side=_side(role),
                            field="address",
                            kind="reach",
                            servers=True,
                            other_found=True,
                        ) from None
                    self.key_lines = {i.name: env.key_line(i) for _role, i, _state in servers}
                    found += servers
                except MigrationError as error:
                    role = env.notes.get("stage_role")
                    other_found = role != "source"
                    if role == "source":
                        try:
                            from ..trust import probe_tls

                            probe_tls(*env.connection.destination_endpoint)
                            other_found = True
                        except MigrationError:
                            other_found = False
                    raise self._fail(
                        error,
                        side=_side(role),
                        field="ops_address" if role == "ops" else "address",
                        kind="reach",
                        other_found=other_found,
                    ) from None
        except Failure:
            self.disconnect()
            raise
        self.pending = {}
        return {
            "identities": [self._shown(role, identity, state) for role, identity, state in found]
        }

    def trust(self, *, identities: list[dict], replace: bool = False) -> list[str]:
        """Record the identities the person confirmed, by the fingerprint they were shown.
        A changed address is replaced only when the person said so explicitly. A changed
        server code is never replaced from here: it stops the test."""
        require(self.store is not None, Code.STATE)
        enrolled = []
        for shown in identities:
            identity = self.pending.get(shown["name"])
            if identity is None or identity.fingerprint != shown["fingerprint"]:
                continue
            state = self.store.check(identity)
            if state == "changed":
                if not replace or identity.kind != "tls":
                    continue
                self.store.forget(identity.kind, identity.name)
            if state != "trusted":
                self.store.trust(identity, self.key_lines.get(identity.name))
            enrolled.append(identity.name)
        return enrolled

    def connect(
        self,
        *,
        fixture: str,
        journal: str,
        credentials: dict[str, str],
        connection: dict[str, str],
    ) -> dict:
        """Sign in to both systems and read their versions. Nothing on either is changed."""
        self.disconnect()
        self.open_record(journal=journal)
        self.job = "connect"
        self.connection = dict(connection)
        secrets = {key: value for key, value in credentials.items() if value}
        self.credentials = Credentials(secrets, interactive=False)
        self.store = self._store(fixture)
        token = bool(connection.get("ops_url"))
        self.server_sign_in = bool(
            not token and connection.get("destination_ssh_user") and secrets.get("destination_ssh")
        )
        self.want_server = self.server_sign_in
        self.stage(0)
        try:
            if fixture:
                env = self._stand_in(fixture)
                check, clock = self._sign_in_stand_in(env, secrets, token), StandInClock()
            else:
                env = self._live()
                check, clock = self._sign_in_live(env, token), None
        except Failure:
            self.disconnect()
            raise
        self.env = env
        self.engine = Engine(env, self.journal, clock)
        self.engine.on_step = self._engine_step
        if not check["passed"]:
            # Signed in, but nothing further is offered for a pairing that cannot migrate.
            self.errors.append(f"{datetime.now(UTC).isoformat()} connect: {check['message']}")
        return self._discovered(check)

    def _sign_in_stand_in(self, env, secrets: dict[str, str], token: bool) -> dict:
        def attempt(side, field, kind, check, **extra):
            try:
                check()
            except MigrationError as error:
                raise self._fail(error, side=side, field=field, kind=kind, **extra) from None

        more: list[dict] = []
        later: list[tuple] = []

        def servers(side, secret, **extra):
            """The server password goes to the server at the typed address first. Only that
            server can say which others stand behind the address, and none of them is sent
            the password before the person has confirmed its code."""
            for index, identity in enumerate(self._stand_in_servers(env, side)):
                state = self.store.check(identity)
                name = identity.name.rsplit(":", 1)[0]
                if state == "changed":
                    raise self._fail(
                        MigrationError(Code.AUTH, f"The identity of {name} changed."),
                        side=side,
                        field="address",
                        kind="identity",
                        server=name,
                    )
                if state == "unknown":
                    more.append(self._shown(side, identity, state))
                    if index == 0:
                        return  # Not even the typed server is confirmed: nothing is sent.
                    continue

                def send(name=name):
                    return attempt(
                        side,
                        "server_password",
                        "auth",
                        lambda: env.check_sign_in(side + "_ssh", secret, server=name),
                        **extra,
                    )

                if index == 0:
                    send()
                else:
                    later.append(send)

        self.stage(1)
        attempt(
            "source",
            "password",
            "auth",
            lambda: env.check_sign_in("source_api", secrets.get("source_api", "")),
            other_found=True,
        )
        servers("source", secrets.get("source_ssh", ""), other_found=True)
        self.stage(2)
        nine_one = env.data["destination"]["version"].startswith("9.1")
        if nine_one and not token:
            raise Failure("needs_token", side="destination", kind="token_needed")
        if token:
            attempt(
                "destination",
                "token",
                "auth",
                lambda: env.check_sign_in("ops_token", secrets.get("ops_token", "")),
                other_signed_in=True,
            )
        else:
            attempt(
                "destination",
                "password",
                "auth",
                lambda: env.check_sign_in("destination_api", secrets.get("destination_api", "")),
                other_signed_in=True,
            )
            if self.server_sign_in:
                servers("destination", secrets.get("destination_ssh", ""), other_signed_in=True)
        if more:
            raise Failure(UNCONFIRMED, kind="servers_found", identities=more)
        for send in later:
            send()
        self.stage(3)
        return env.version_check()

    def _same_server(self, env, identities: list[Identity]) -> list[Identity]:
        """A server found under another name that presents exactly the code the person
        confirmed for a typed address is that same server (only the holder of its key can
        present it), so it is recorded without a second question. Returns the rest."""
        port = env.connection.ssh_port
        typed = (env.connection.source_host, env.connection.destination_endpoint[0])
        confirmed = {
            self.store.known(Identity("ssh", f"{host}:{port}", ""))
            for name in typed
            for host in (name, name.lower())
        } - {None}
        rest = []
        for identity in identities:
            if identity.kind == "ssh" and identity.fingerprint in confirmed:
                self.store.trust(identity, env.key_line(identity))
            else:
                rest.append(identity)
        return rest

    def _servers_found(self, env, identities: list[Identity]) -> Failure:
        """Identities met after sign-in began that nobody has confirmed. Nothing has been
        sent to them; the window asks, and the test starts again once they are confirmed."""
        self.key_lines.update({i.name: env.key_line(i) for i in identities})
        behind = {host: "destination" for host in env.destination_members}
        names = {name: role for role, name in self._roles().items()}
        shown = []
        for identity in identities:
            host = identity.name.rsplit(":", 1)[0]
            role = names.get(identity.name) or behind.get(host, "source")
            if identity.kind == "ssh" and host == env.connection.destination_endpoint[0].lower():
                role = "destination"
            shown.append(self._shown(role, identity, "unknown"))
        return Failure(UNCONFIRMED, kind="servers_found", identities=shown)

    def _sign_in_live(self, env, token: bool) -> dict:
        def where(error: MigrationError) -> dict:
            """Which box a failure during `open()` belongs to, from the probe in flight."""
            stage = str(env.notes.get("stage") or "")
            side = "destination" if env.notes.get("stage_role") == "destination" else "source"
            if env.notes.get("identity_changed") or (
                isinstance(error, SSHFailure) and "host key" in error.message
            ):
                data = {"side": side, "field": "address", "kind": "identity"}
                changed = str(env.notes.get("identity_changed") or "")
                if isinstance(error, SSHFailure) and changed:
                    data["server"] = changed.rsplit(":", 1)[0]
                return data
            if isinstance(error, SSHFailure) and error.code == Code.AUTH:
                return {"side": side, "field": "server_password", "kind": "auth"}
            if stage.startswith("ssh"):
                return {"side": side, "field": "address", "kind": "reach", "servers": True}
            return {"side": side, "field": "address", "kind": "reach"}

        if not token:
            hint = None
            try:
                hint = env.destination_version_hint()
            except Exception:
                hint = None
            if hint and hint.startswith("9.1"):
                raise Failure("needs_token", side="destination", kind="token_needed")
        for attempt in range(3):
            try:
                env.open()
                break
            except TrustRequired as asked:
                # `open()` stops before anything is sent to an identity nobody confirmed.
                rest = self._same_server(env, asked.identities)
                if rest or attempt == 2:
                    raise self._servers_found(env, rest or asked.identities) from None
            except MigrationError as error:
                data = where(error)
                data["other_found"] = True
                raise self._fail(error, **data) from None
        self.stage(1)
        try:
            env.source_version()
        except MigrationError as error:
            kind = "auth" if error.code == Code.AUTH else "other"
            raise self._fail(
                error, side="source", field="password", kind=kind, other_found=True
            ) from None
        for host in env.members:
            try:
                env.test_source_credentials(host)
            except MigrationError as error:
                kind = "auth" if error.code == Code.AUTH else "other"
                raise self._fail(
                    error,
                    side="source",
                    field="server_password",
                    kind=kind,
                    server=host,
                    other_found=True,
                ) from None
        self.stage(2)
        try:
            env.destination_version()
        except MigrationError as error:
            kind = "auth" if error.code == Code.AUTH else "other"
            raise self._fail(
                error,
                side="destination",
                field="token" if token else "password",
                kind=kind,
                other_signed_in=True,
                maybe_token=not token,
            ) from None
        self.stage(3)
        try:
            return env.version_check()
        except MigrationError as error:
            raise self._fail(
                error, side="destination", field=None, kind="other", other_signed_in=True
            ) from None

    def _discovered(self, check: dict) -> dict:
        view = vm.verdict(check)
        connection = self.connection
        servers = 0
        for key in ("source", "destination"):
            versions = check.get(key) or {}
            if set(versions) != {"cluster"}:
                servers += len(versions)
        return {
            "mode": self.mode,
            "passed": bool(check["passed"]),
            "version_check": check,
            "source": {
                "host": connection["source_host"].strip(),
                "version": view["source_version"],
                "servers": view["source_servers"],
            },
            "destination": {
                "host": connection["destination"].strip().split(":")[0],
                "version": view["destination_version"],
                "servers": view["destination_servers"],
            },
            "servers_found": servers,
            "server_sign_in": self.server_sign_in,
            "tested_at": datetime.now(UTC).isoformat(),
        }

    def reconnect(self, *, fixture: str, journal: str) -> dict:
        """Sign in again after losing contact; the passwords are still in memory."""
        credentials = self.credentials.snapshot() if self.credentials is not None else {}
        return self.connect(
            fixture=fixture, journal=journal, credentials=credentials, connection=self.connection
        )

    def disconnect(self) -> None:
        """Forget the passwords and both systems. The record of past runs stays open."""
        if self.env is not None:
            for name in ("source_client", "destination_client"):
                client = getattr(self.env, name, None)
                if client is not None:
                    client.clear_token()
        if self.credentials is not None:
            self.credentials.clear()
        self.env = self.engine = self.credentials = None
        self.server_sign_in = False

    def close(self) -> None:
        self.disconnect()
        if self.journal is not None:
            self.journal.close()
            self.journal = None

    def identity_codes(self) -> dict:
        """The recorded identity of each address and of each server behind them."""
        require(self.env is not None and self.store is not None, Code.STATE)
        names = self._roles()
        known = {(kind, name): digest for kind, name, digest in self.store.entries()}
        addresses = [
            (role, names[role].rsplit(":", 1)[0], known.get(("tls", names[role]), ""))
            for role in names
        ]
        if self.mode != "live":
            servers = self.env.server_keys("source")
            servers += self.env.server_keys("destination") if self.server_sign_in else []
            contacted = self.server_sign_in
            pending = [n["id"] for n in self.env.data["destination"]["nodes"]]
        else:
            hosts = list(self.env.members) + list(self.env.destination_members)
            port = self.env.connection.ssh_port
            servers = [
                (host, vm.server_key(known.get(("ssh", f"{host}:{port}"), ""))) for host in hosts
            ]
            contacted = bool(self.env.destination_members)
            pending = []
        return {
            "addresses": addresses,
            "servers": servers,
            "destination_contacted": contacted,
            "destination_servers": pending,
        }

    def _engine(self) -> Engine:
        require(self.engine is not None, Code.STATE)
        return self.engine

    def _call(self, job: str, call, **data):
        self.job = job
        try:
            return call()
        except MigrationError as error:
            raise self._fail(error, **data) from None

    # Rules -------------------------------------------------------------------------------

    def config_compare(self) -> dict:
        """The engine's comparison plus the rule bodies it compared, for display only."""
        self.job = "compare"
        self.stage(0)
        comparison = self._call("compare", self._engine().config_compare)
        self.stage(1)
        details: dict[str, dict] = {}
        reader = getattr(self.env, "config_rules", None)
        if reader is not None:
            inventory = {}
            for kind in {row["type"] for row in comparison["rules"]}:
                try:
                    inventory[kind] = (
                        {r["name"]: r for r in reader("source", kind)},
                        {r["name"]: r for r in reader("destination", kind)},
                    )
                except Exception:
                    inventory[kind] = ({}, {})
            for row in comparison["rules"]:
                source, destination = inventory.get(row["type"], ({}, {}))
                details[row["selector"]] = {
                    "source": source.get(row["name"]),
                    "destination": destination.get(row["name"]),
                }
        comparison["details"] = details
        self.stage(2)
        return comparison

    def config_apply(self, *, selection: list[str]) -> dict:
        self.job = "apply"
        self.stage(0)
        result = self._call("apply", lambda: self._engine().config_apply(list(selection)))
        self.stage(1)
        return result

    # Dates -------------------------------------------------------------------------------

    def discover(self) -> dict:
        """List what the source holds and how much room the destination has."""
        self.job = "discover"
        if self.mode != "live":
            self.stage(0)
        result = self._call("discover", self._engine().discover, refusal={"kind": "listfail"})
        self.stage(1)
        return result

    def plan(
        self,
        *,
        start: int,
        end: int,
        attempts: int,
        poll_seconds: float,
        exclude: list[dict] | None = None,
        labels: dict | None = None,
        newest: str | None = None,
    ) -> dict:
        """Measure the dates and build the plan. With days to leave out, the plan is one
        part per unbroken range around them, because the destination takes one range of
        dates per request."""
        self.job = "plan"
        policy = Policy(attempts, poll_seconds)
        client = {"labels": labels or {}, "newest": newest}
        windows = [(start, end)]
        if exclude:
            first, last = copies.day_of(start), copies.day_of(end - 1)
            taken = copyview.excluded_days(exclude)
            windows = [vm.window_ms(a, z) for a, z in copyview.free_ranges((first, last), taken)]
            require(bool(windows), Code.INVALID_INPUT)
        series = None
        if len(windows) > 1 or exclude:
            series = {
                "id": str(uuid4()),
                "count": len(windows),
                "requested": {"start": start, "end": end},
                "excluded": exclude or [],
                "windows": [{"start": a, "end": z} for a, z in windows],
            }
        if self.mode != "live":
            for index in range(4):
                self.stage(index)
        runs = []
        empty = None
        for index, (a, z) in enumerate(windows):
            note = {**client, "series": {**series, "index": index}} if series else client
            try:
                runs.append(self._engine().plan(Window(a, z), policy, note))
            except MigrationError as error:
                message = getattr(error, "message", "")
                if series and message.startswith("No dated sealed groups overlap"):
                    empty = error  # A range between two left-out stretches holds nothing.
                    continue
                failed = self._fail(error)
                if "refusal" not in failed.data and error.code == Code.CONNECTION:
                    failed.data["refusal"] = {"kind": "listfail"}
                raise failed from None
        if not runs:
            raise self._fail(empty or MigrationError(Code.INCOMPLETE))
        self.stage(4)
        return {"parts": [display_run(run) for run in runs], "excluded": exclude or []}

    # Copy --------------------------------------------------------------------------------

    def _start(self, run_id: str, acknowledged: bool) -> dict:
        before = len(self.journal.events(run_id))
        try:
            result = self._engine().run(
                run_id,
                active_ack=ACTIVE_ACK if acknowledged else "",
                cancelled=self.cancelled,
            )
        except MigrationError as error:
            kinds = {event["kind"] for event in self.journal.events(run_id)[before:]}
            sent = "api_start_requested" in kinds and "api_start_rejected" not in kinds
            message = getattr(error, "message", "") or ""
            stage = None
            if not sent:
                if "api_start_rejected" in kinds:
                    stage = "rejected"
                elif message.startswith("Cancelled before"):
                    stage = "cancelled"
                elif "version_checked" in kinds and error.code in {Code.CONNECTION, Code.AUTH}:
                    # After the version check, before anything is counted or requested:
                    # the source server sign-ins and the destination's own reach test.
                    trace = getattr(self.env, "data", {}).get("trace", [])
                    last = trace[-1]["operation"] if trace else ""
                    stage = (
                        "source_sign_in"
                        if isinstance(error, SSHFailure) or last == "test_source_credentials"
                        else "mover"
                    )
            raise self._fail(
                error,
                sent=sent,
                stage=stage,
                run_id=run_id,
                rejected=isinstance(error, Rejection),
            ) from None
        result["run"] = display_run(self.journal.load(run_id))
        return result

    def start(self, *, run_id: str, acknowledged: bool) -> dict:
        """Count both sides, then ask the destination to start fetching."""
        self.job = "start"
        return self._start(run_id, acknowledged)

    def start_part(
        self,
        *,
        series: dict,
        index: int,
        attempts: int,
        poll_seconds: float,
        client: dict,
        acknowledged: bool,
        preview: str,
    ) -> dict:
        """Plan the next part of a multi-part copy afresh and start it. A part with nothing
        left to send is skipped, and the record says so, so it is never asked for again."""
        self.job = "continue"
        window = series["windows"][index]
        note = {**client, "series": {**series, "index": index}}
        self.stage(0)
        earlier = self.journal.load(preview)
        self._same_systems(earlier)
        try:
            run = self._engine().plan(
                Window(window["start"], window["end"]), Policy(attempts, poll_seconds), note
            )
        except MigrationError as error:
            if getattr(error, "message", "").startswith("No dated sealed groups overlap"):
                self._skip_part(preview, set())
                return {"skipped": True, "index": index}
            raise self._fail(error, sent=False) from None
        if not run["plan"]["selected"]:
            self._skip_part(preview, {g["id"] for g in run["plan"].get("already_received", [])})
            return {"skipped": True, "index": index}
        # What the plan the person agreed to held and this one does not is no longer on
        # the source. The record keeps it, so the result can say it was not copied.
        found = {
            g["id"] for g in (*run["plan"]["selected"], *run["plan"].get("already_received", []))
        }
        lost = [g for g in earlier["plan"]["selected"] if g["id"] not in found]
        if lost:
            stored = self.journal.load(run["id"])
            stored["client"]["series"]["gone_groups"] = lost
            self.journal.save(stored, "part_narrowed")
        self.stage(1)
        return self._start(run["id"], acknowledged)

    def _same_systems(self, earlier: dict) -> None:
        """A later part goes only between the two systems its copy was planned for.
        Checked before anything is planned, so a different system's dates are never
        recorded against this copy."""
        try:
            with self.env.exclusive():
                now = dict(zip(("source", "destination"), self.env.discover(), strict=True))
        except MigrationError as error:
            raise self._fail(error, sent=False) from None
        planned = earlier["plan"]
        changed = [
            side
            for side, cluster in now.items()
            if (cluster.id, cluster.identity) != (planned[side]["id"], planned[side]["identity"])
        ]
        if changed:
            raise self._fail(
                MigrationError(
                    Code.TOPOLOGY,
                    "The next part was planned for a different " + " and ".join(changed) + ".",
                ),
                sent=False,
                systems=changed,
                labels=(earlier.get("client") or {}).get("labels") or {},
            )

    def _skip_part(self, preview: str, there: set[str]) -> None:
        """Record that a planned part has nothing left to send: what it would have copied
        is on the destination already, or is no longer on the source."""
        earlier = self.journal.load(preview)
        series = earlier.setdefault("client", {}).setdefault("series", {})
        series["gone_ids"] = [g["id"] for g in earlier["plan"]["selected"] if g["id"] not in there]
        series["skipped"] = "gone" if series["gone_ids"] else "there"
        self.journal.save(earlier, "part_skipped")

    def continue_copy(self, *, run_id: str, acknowledged: bool) -> dict:
        """Work out what is left of a stopped copy and send only that."""
        self.job = "continue"
        if not self.server_sign_in:
            # Decided with the design: without the destination's server sign-in the app
            # cannot see what arrived, and it does not guess.
            raise Failure(Code.INCOMPLETE.value, sent=False, sign_in=True)
        try:
            run = self._engine().resume(run_id)
        except MigrationError as error:
            raise self._fail(error, sent=False) from None
        return self._start(run["id"], acknowledged or bool(run.get("active_ack")))

    def status(self, *, run_id: str) -> dict:
        self.job = "status"
        result = self._call("status", lambda: self._engine().status(run_id))
        result["run_id"] = run_id
        return result

    def stop(self, *, task_id: str, run_id: str) -> dict:
        self.job = "stop"
        result = self._call("stop", lambda: self._engine().stop(task_id))
        result["run_id"] = run_id
        return result

    # Check -------------------------------------------------------------------------------

    def verify(self, *, run_ids: list[str]) -> dict:
        """Count the destination day by day for every part and compare with the source."""
        self.job = "verify"
        if self.mode != "live":
            self.stage(0)
        for run_id in run_ids:
            self._call("verify", lambda run_id=run_id: self._engine().verify(run_id))
        self.stage(3)
        return self.record()

    # Files -------------------------------------------------------------------------------

    def _write(self, folder: str, name: str, text: str) -> str:
        target = Path(folder).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
            path = target / name
            path.write_text(text, encoding="utf-8")
        except OSError as error:
            raise Failure(f"gui:Could not write to {target}: {error.strerror or error}") from None
        return str(path)

    def save_text(self, *, folder: str, name: str, text: str) -> str:
        return self._write(folder, name, text)

    def save_report(
        self, *, copy_id: str, folder: str, zone: str, rules: list | None = None
    ) -> list[str]:
        """One Save writes the readable report and the data file beside it."""
        require(self.journal is not None, Code.STATE)
        copy = next((c for c in self._copies() if c["id"] == copy_id), None)
        require(copy is not None, Code.INVALID_INPUT)
        result = copies.check(copy)
        started = vm._local(copy["started_at"], "UTC")
        stem = (
            "logs-copy-report-"
            + (started.strftime("%Y-%m-%d-%H%M-") if started else "")
            + copy_id[:8]
        )
        runs = [run for part in copy["parts"] for run in part["runs"] if part["started"]]
        data = {
            "schema_version": 3,
            "copy": {
                "dates": copyview.dates_text(copy),
                "source": copy["labels"].get("source"),
                "destination": copy["labels"].get("destination"),
                "parts": len(copy["parts"]),
                "state": copy["state"],
                "result": result["phase"],
            },
            "runs": [
                report_data(run, display_events(self.journal.events(run["id"]))) for run in runs
            ],
        }
        return [
            self._write(
                folder, stem + ".md", copyview.report_markdown(copy, result, zone=zone, rules=rules)
            ),
            self._write(folder, stem + ".json", json.dumps(data, sort_keys=True, indent=2)),
        ]

    def save_troubleshooting(self, *, folder: str, settings: dict) -> str:
        """One file for whoever is helping: what went wrong, the record and the detailed log.
        It names servers and stored pieces of logs; it never holds a password."""
        from .. import __version__
        from ..diagnostics import log_path, redact

        target = Path(folder).expanduser()
        name = "troubleshooting-" + datetime.now().strftime("%Y-%m-%d-%H%M") + ".zip"
        summary = [
            f"VCF Logs Migrator {__version__}",
            f"Written {datetime.now(UTC).isoformat()}",
            f"Computer: {platform.platform()}",
            f"Signed in: {'yes' if self.env is not None else 'no'} ({self.mode})",
            "",
            "Settings (never passwords):",
            *(f"  {key} = {value}" for key, value in sorted(settings.items())),
            "",
            "What went wrong most recently, newest last, in the engine's own words:",
            *(self.errors or ["  nothing recorded in this session"]),
        ]
        try:
            target.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(target / name, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("summary.txt", redact("\n".join(summary) + "\n"))
                if self.journal is not None:
                    record = self.record()
                    record["events"] = {
                        run["id"]: display_events(self.journal.events(run["id"]))
                        for run in record["runs"]
                    }
                    archive.writestr(
                        "record.json", redact(json.dumps(record, sort_keys=True, indent=1))
                    )
                path = log_path()
                if path and path.exists():
                    archive.write(path, "detailed-log.txt")
        except OSError as error:
            raise Failure(f"gui:Could not write to {target}: {error.strerror or error}") from None
        return str(target / name)
