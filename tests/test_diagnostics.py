import logging

from logs_migrate import diagnostics
from logs_migrate.security import Secret


def test_redaction_covers_file_stderr_and_tracebacks(tmp_path, capsys):
    path = tmp_path / "debug.log"
    diagnostics.configure(path, verbose=True)
    secret = "credential-known-to-session"
    Secret(secret)
    try:
        raise ValueError(f"password={secret} Authorization: Bearer {secret}")
    except ValueError:
        logging.getLogger("logs_migrate.adapters.rest").exception("REST failure %s", secret)
    logging.getLogger("paramiko.transport").debug("SSH password=%s", secret)
    diagnostics.configure(enabled=False)
    for output in (path.read_text(), capsys.readouterr().err):
        assert secret not in output
        assert "Traceback" in output
        assert "SSH" in output
        assert "<redacted>" in output


def test_default_off(tmp_path):
    diagnostics.configure(enabled=False)
    assert diagnostics.log_path() is None
    assert list(tmp_path.iterdir()) == []


def test_rest_error_body_redacts_text():
    from logs_migrate.adapters.rest import _safe_error_body

    assert _safe_error_body(b'{"status": 403, "message": "secret", "token": "value"}') == {
        "status": 403,
        "message": "<redacted>",
        "token": "<redacted>",
    }


def test_diagnostic_flags_work_around_commands():
    from logs_migrate.cli import _diagnostic_args, parser

    for args in (["--debug-log", "status"], ["status", "--debug-log"], ["-v", "status"]):
        parsed = parser().parse_args(_diagnostic_args(args))
        assert parsed.command == "status"
        assert parsed.debug_log is not None or parsed.verbose


def test_transport_exceptions_redacted(tmp_path, capsys, monkeypatch):
    import sys
    from types import SimpleNamespace

    import pytest

    from logs_migrate.adapters.rest import HTTPS
    from logs_migrate.adapters.ssh import NodeSSH, ReadOperation
    from logs_migrate.errors import MigrationError

    rest_secret, ssh_secret = "rest-secret-unique", "ssh-secret-unique"
    Secret(rest_secret)
    path = tmp_path / "transports.log"
    diagnostics.configure(path, verbose=True)

    class BrokenHTTPS:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            raise RuntimeError(f"Authorization: Bearer {rest_secret}")

        def close(self):
            pass

    class BrokenSSH:
        def load_host_keys(self, path):
            raise RuntimeError(f"password={ssh_secret}")

        def close(self):
            pass

    monkeypatch.setattr("http.client.HTTPSConnection", BrokenHTTPS)
    monkeypatch.setitem(sys.modules, "paramiko", SimpleNamespace(SSHClient=BrokenSSH))
    with pytest.raises(MigrationError):
        HTTPS("https://example.invalid", certificate_sha256="a" * 64).request("GET", "/api")
    with pytest.raises(MigrationError):
        NodeSSH("example.invalid", "root", Secret(ssh_secret), "a" * 64, "hosts").read(
            ReadOperation.INVENTORY
        )
    diagnostics.configure(enabled=False)
    for output in (path.read_text(), capsys.readouterr().err):
        assert rest_secret not in output
        assert ssh_secret not in output
        assert "REST request failed" in output
        assert "SSH operation failed" in output
