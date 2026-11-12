"""Exercise the real adapter path with deterministic post-connect failures."""

import hashlib
import sys
from types import SimpleNamespace

import pytest

from logs_migrate.adapters.ssh import NodeSSH, ReadOperation
from logs_migrate.errors import Code, MigrationError
from logs_migrate.security import Secret


@pytest.fixture
def ssh_client(monkeypatch):
    class SSHException(Exception):
        pass

    class AuthenticationException(SSHException):
        pass

    class BadHostKeyException(SSHException):
        pass

    class IncompatiblePeer(SSHException):
        pass

    class ChannelException(SSHException):
        pass

    key = SimpleNamespace(asbytes=lambda: b"key")
    client = SimpleNamespace(
        load_host_keys=lambda path: None,
        set_missing_host_key_policy=lambda policy: None,
        get_host_keys=lambda: SimpleNamespace(lookup=lambda name: {"ssh-ed25519": key}),
        connect=lambda *a, **kw: None,
        close=lambda: None,
    )
    module = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: None,
        SSHException=SSHException,
        AuthenticationException=AuthenticationException,
        BadHostKeyException=BadHostKeyException,
        ssh_exception=SimpleNamespace(IncompatiblePeer=IncompatiblePeer),
    )
    monkeypatch.setitem(sys.modules, "paramiko", module)
    node = NodeSSH(
        "local.invalid", "root", Secret("canary"), hashlib.sha256(b"key").hexdigest(), "/unused"
    )
    return node, client, module, ChannelException


@pytest.mark.parametrize(
    "kind,code,words",
    [
        ("tcp", Code.CONNECTION, "SSH TCP connection"),
        ("timeout", Code.CONNECTION, "SSH TCP connection"),
        ("reset", Code.CONNECTION, "SSH handshake"),
        ("host", Code.AUTH, "SSH host key"),
        ("negotiation", Code.CONNECTION, "SSH negotiation"),
        ("auth", Code.AUTH, "SSH authentication"),
        ("banner", Code.CONNECTION, "SSH handshake"),
        ("command", Code.CONNECTION, "SSH remote command"),
    ],
)
def test_failure_stages(ssh_client, kind, code, words):
    node, client, module, channel_error = ssh_client
    failures = {
        "tcp": ConnectionRefusedError("canary"),
        "timeout": TimeoutError("canary"),
        "reset": ConnectionResetError("canary"),
        "host": module.BadHostKeyException("canary"),
        "negotiation": module.ssh_exception.IncompatiblePeer(
            "Incompatible ssh peer (no acceptable ciphers)"
        ),
        "auth": module.AuthenticationException("Authentication failed."),
        "banner": module.SSHException("Error reading SSH protocol banner canary"),
        "command": channel_error("canary"),
    }

    def fail(*a, **kw):
        raise failures[kind]

    if kind == "command":
        client.exec_command = fail
    else:
        client.connect = fail
    with pytest.raises(MigrationError) as caught:
        node.read(ReadOperation.MEMBERS)
    assert caught.value.code == code
    assert words in caught.value.message
    assert "API" not in caught.value.message
    assert "canary" not in caught.value.message
    if kind == "negotiation":
        assert "Incompatible ssh peer (no acceptable ciphers)" in caught.value.message
    if kind == "auth":
        assert "Authentication failed." in caught.value.message


@pytest.mark.parametrize("missing", [True, False])
def test_unknown_and_mismatched_enrollment(ssh_client, missing):
    node, client, _, _ = ssh_client
    if missing:
        client.get_host_keys = lambda: SimpleNamespace(lookup=lambda name: None)
    else:
        node.host_key_sha256 = "0" * 64
    with pytest.raises(MigrationError) as caught:
        node.read(ReadOperation.MEMBERS)
    assert "SSH host key" in caught.value.message


def test_command_nonzero_never_exposes_stderr(ssh_client):
    node, client, _, _ = ssh_client
    output = SimpleNamespace(
        read=lambda size: b"", channel=SimpleNamespace(recv_exit_status=lambda: 127)
    )
    client.exec_command = lambda *a, **kw: (None, output, "canary")
    with pytest.raises(MigrationError) as caught:
        node.read(ReadOperation.MEMBERS)
    assert "SSH remote command" in caught.value.message
    assert "127" in caught.value.message
    assert "canary" not in caught.value.message


@pytest.mark.parametrize("stage", ["tcp", "handshake", "negotiation", "auth", "command", "host"])
def test_gui_sign_in_points_an_ssh_failure_at_a_box_and_keeps_the_diagnostic(stage):
    from types import SimpleNamespace

    from logs_migrate.adapters.ssh_errors import SSHFailure
    from logs_migrate.gui.session import Failure, Session

    error = SSHFailure(stage)

    class Environment:
        notes = {"stage": "ssh local.invalid:22 (members)"}
        connection = SimpleNamespace(source_host="local.invalid")

        def destination_version_hint(self):
            return None

        def open(self):
            raise error

    session = Session()
    with pytest.raises(Failure) as caught:
        session._sign_in_live(Environment(), False)
    data = caught.value.data
    assert data["side"] == "source"
    assert data["kind"] == {"host": "identity", "auth": "auth"}.get(stage, "reach")
    assert data["field"] == ("server_password" if stage == "auth" else "address")
    # The engine's own sentence is kept for the troubleshooting file, not for the screen.
    assert error.message in caught.value.code and error.message in session.errors[-1]


@pytest.mark.parametrize(
    ("role", "stage", "typed_source", "expected"),
    [
        ("destination", "ssh 10.0.0.50:22", "10.0.0.5", "destination"),
        ("source", "ssh logs-02.example:22", "10.0.0.5", "source"),
    ],
)
def test_gui_sign_in_marks_the_box_of_the_probe_in_flight_not_a_host_name_match(
    role, stage, typed_source, expected
):
    from logs_migrate.adapters.ssh_errors import SSHFailure
    from logs_migrate.gui.session import Failure, Session

    class Environment:
        notes = {"stage": stage, "stage_role": role}
        connection = SimpleNamespace(source_host=typed_source)

        def destination_version_hint(self):
            return None

        def open(self):
            raise SSHFailure("tcp")

    with pytest.raises(Failure) as caught:
        Session()._sign_in_live(Environment(), False)
    assert caught.value.data["side"] == expected
    assert caught.value.data["servers"] is True


def test_trust_probe_reports_a_closed_port_as_tcp():
    pytest.importorskip("paramiko")
    import socket

    from logs_migrate.trust import probe_ssh

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    with pytest.raises(MigrationError) as caught:
        probe_ssh("127.0.0.1", port, timeout=3)
    assert caught.value.code == Code.CONNECTION
    assert "SSH TCP connection" in caught.value.message
    assert "handshake" not in caught.value.message


@pytest.mark.parametrize("kind", ["negotiation", "banner", "dependency"])
def test_trust_probe_mapping(ssh_client, monkeypatch, kind):
    import socket

    from logs_migrate.trust import probe_ssh

    _, _, module, _ = ssh_client
    error, expected = {
        "negotiation": (module.ssh_exception.IncompatiblePeer("canary"), "SSH negotiation"),
        "banner": (module.SSHException("canary"), "SSH handshake"),
        "dependency": (ModuleNotFoundError("canary"), "SSH support is unavailable"),
    }[kind]
    closed = []

    def fail(*a, **kw):
        raise error

    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **kw: SimpleNamespace(close=lambda: None)
    )
    if kind == "dependency":
        module.Transport = fail
    else:
        module.Transport = lambda address: SimpleNamespace(
            start_client=fail, close=lambda: closed.append(True)
        )
    with pytest.raises(MigrationError) as caught:
        probe_ssh("local.invalid")
    assert expected in caught.value.message
    assert "canary" not in caught.value.message
    if kind in {"negotiation", "banner"}:
        assert closed == [True]


@pytest.mark.parametrize("stage", ["tcp", "host", "auth", "negotiation", "command"])
def test_cli_ssh_message_and_exit_contract(monkeypatch, capsys, stage):
    import json

    from logs_migrate import cli
    from logs_migrate.adapters.ssh_errors import SSHFailure

    def fail(args):
        raise SSHFailure(stage)

    monkeypatch.setattr(cli, "open_live", fail)
    monkeypatch.setattr(cli, "harden_process", lambda: None)
    assert cli.main(["--source", "local.invalid", "--destination", "dest.invalid", "status"]) == 7
    result = json.loads(capsys.readouterr().err)
    assert "SSH" in result["message"]
    assert result["error"] == (Code.AUTH if stage in {"host", "auth"} else Code.CONNECTION)


def test_selftest_keeps_safe_ssh_diagnostic():
    from logs_migrate.adapters.ssh_errors import SSHFailure
    from logs_migrate.selftest import _code

    result = _code(SSHFailure("auth", TimeoutError("canary")))
    assert "authentication_failed" in result
    assert "SSH authentication" in result
    assert "canary" not in result
