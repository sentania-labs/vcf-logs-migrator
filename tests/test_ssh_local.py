"""Optional wire-level tests against a loopback Paramiko SSH server, never the lab."""

import hashlib
import socket
import threading

import pytest

from logs_migrate.adapters.ssh import NodeSSH, ReadOperation
from logs_migrate.errors import Code, MigrationError
from logs_migrate.security import Secret


@pytest.mark.parametrize("failure", ["auth", "negotiation", "host", "command"])
def test_local_ssh_post_connect(tmp_path, monkeypatch, failure):
    paramiko = pytest.importorskip("paramiko")
    if failure == "negotiation":
        monkeypatch.setattr(paramiko.Transport, "_preferred_ciphers", ("aes256-ctr",))
    key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    done = threading.Event()
    errors = []

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return paramiko.AUTH_FAILED if failure == "auth" else paramiko.AUTH_SUCCESSFUL

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED

        def check_channel_exec_request(self, channel, command):
            channel.send_stderr(b"untrusted-secret-canary")
            channel.send_exit_status(127)
            channel.shutdown_write()
            return True

    def serve():
        transport = None
        try:
            conn, _ = listener.accept()
            transport = paramiko.Transport(conn)
            transport.add_server_key(key)
            if failure == "negotiation":
                transport.get_security_options().ciphers = ("aes128-cbc",)
            transport.start_server(server=Server())
            channel = transport.accept(3)
            done.wait(3)
            if channel is not None:
                channel.close()
        except (paramiko.SSHException, EOFError, ConnectionError):
            pass  # Expected peer disconnect (or reset) after client-side rejection.
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            if transport is not None:
                transport.close()
            listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    enrolled = paramiko.RSAKey.generate(2048) if failure == "host" else key
    hosts = paramiko.HostKeys()
    hosts.add(f"[127.0.0.1]:{port}", enrolled.get_name(), enrolled)
    path = tmp_path / "known_hosts"
    hosts.save(str(path))
    node = NodeSSH(
        "127.0.0.1",
        "root",
        Secret("local-test-password"),
        hashlib.sha256(enrolled.asbytes()).hexdigest(),
        str(path),
        port=port,
        timeout=3,
    )
    try:
        with pytest.raises(MigrationError) as caught:
            node.read(ReadOperation.MEMBERS)
        assert caught.value.code == (Code.AUTH if failure in {"auth", "host"} else Code.CONNECTION)
        expected = {
            "auth": "authentication",
            "host": "host key",
            "command": "remote command",
            "negotiation": "negotiation",
        }[failure]
        assert f"SSH {expected}" in caught.value.message
        assert "untrusted-secret-canary" not in caught.value.message
    finally:
        done.set()
        thread.join(6)
    assert not thread.is_alive()
    assert errors == []
