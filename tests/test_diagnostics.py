import logging

from logs_migrate import diagnostics
from logs_migrate.security import Secret


def test_redaction_covers_file_stderr_and_tracebacks(tmp_path, capsys):
    path = tmp_path / "debug.log"
    diagnostics.configure(path, verbose=True)
    secret = "credential-known-to-session"
    _credential = Secret(secret)
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


def test_default_off(tmp_path, capsys):
    path = tmp_path / "debug.log"
    diagnostics.configure(path, verbose=True)
    diagnostics.configure(enabled=False)
    assert diagnostics.log_path() is None
    logging.getLogger("logs_migrate").debug("after-disable-marker")
    logging.getLogger("paramiko.transport").debug("after-disable-marker")
    assert "after-disable-marker" not in path.read_text()
    assert "after-disable-marker" not in capsys.readouterr().err


def test_rest_error_body_redacts_text():
    from logs_migrate.adapters.rest import _safe_error_body

    assert _safe_error_body(b'{"status": 403, "message": "secret", "token": "value"}') == {
        "status": 403,
        "message": "<redacted>",
        "token": "<redacted>",
    }


def test_diagnostic_flags_work_around_commands():
    from logs_migrate.cli import _diagnostic_args, parser

    cases = (
        (["--debug-log", "status"], "status"),
        (["status", "--debug-log"], "status"),
        (["config", "--debug-log", "compare"], "config"),
        (["-v", "status"], "status"),
    )
    for args, command in cases:
        parsed = parser().parse_args(_diagnostic_args(args))
        assert parsed.command == command
        assert parsed.debug_log is not None or parsed.verbose


def test_secret_with_whitespace_fully_redacted():
    from logs_migrate.diagnostics import redact

    _credential = Secret("abc def-unique")
    assert "def-unique" not in redact("password=abc def-unique trailing")


def test_transport_exceptions_redacted(tmp_path, capsys, monkeypatch):
    import sys
    from types import SimpleNamespace

    import pytest

    from logs_migrate.adapters.rest import HTTPS
    from logs_migrate.adapters.ssh import NodeSSH, ReadOperation
    from logs_migrate.errors import MigrationError

    rest_secret, ssh_secret = "rest-secret-unique", "ssh-secret-unique"
    _credential = Secret(rest_secret)
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


def test_credentials_clear_and_session_disconnect_release_secrets():
    from types import SimpleNamespace

    from logs_migrate.adapters.rest import Logs90, RecordedTransport
    from logs_migrate.gui.session import Session
    from logs_migrate.security import Credentials

    credentials = Credentials({"source_api": "shared-password"})
    password = credentials.get("source_api")
    token = Secret("session-value")
    client = Logs90(RecordedTransport([]), "admin", password)
    client.token = token
    session = Session()
    session.credentials = credentials
    session.env = SimpleNamespace(source_client=client, destination_client=None)
    assert diagnostics.redact("shared-password session-value") == "<redacted> <redacted>"
    session.disconnect()
    assert diagnostics.redact("shared-password session-value") == "shared-password session-value"


def test_token_refresh_releases_old_token():
    from logs_migrate.adapters.rest import Logs90, RecordedTransport
    from logs_migrate.errors import Code

    transport = RecordedTransport(
        [
            {"method": "GET", "path": "/api/v1/version", "error": Code.AUTH.value},
            {"method": "POST", "path": "/api/v1/sessions", "response": {"sessionId": "new-token"}},
            {"method": "GET", "path": "/api/v1/version", "response": {}},
        ]
    )
    client = Logs90(transport, "admin", Secret("password-value"))
    old = Secret("old-token")
    client.token = old
    client.call("GET", "/api/v1/version")
    assert diagnostics.redact("old-token new-token") == "old-token <redacted>"


def test_existing_and_rotated_logs_are_private(tmp_path):
    import os
    import stat

    import pytest

    if os.name != "posix":
        pytest.skip("POSIX mode bits are unavailable")
    path = tmp_path / "debug.log"
    path.write_text("previous")
    path.chmod(0o644)
    diagnostics.configure(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    handler = diagnostics._handlers[0]
    handler.doRollover()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_name("debug.log.1").stat().st_mode) == 0o600
    diagnostics.configure(enabled=False)


def test_live_secrets_can_be_logged_after_enabling_and_are_not_retained(tmp_path):
    import gc
    import weakref

    first = Secret("duplicate credential")
    second = Secret("duplicate credential")
    reference = weakref.ref(first)
    diagnostics.configure(tmp_path / "enabled-later.log")
    assert diagnostics.redact("duplicate credential") == "<redacted>"
    first.release()
    assert diagnostics.redact("duplicate credential") == "<redacted>"
    del first
    gc.collect()
    assert reference() is None
    second.release()
    assert diagnostics.redact("duplicate credential") == "duplicate credential"
    diagnostics.configure(enabled=False)


def test_default_logs_pruned_to_ten_newest_but_custom_path_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr("logs_migrate.gui.settings.config_dir", lambda: tmp_path / "config")
    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    for day in range(1, 13):
        stamp = f"20260101-0000{day:02d}-000000"
        for suffix in ("", ".1", ".2"):
            (logs / f"debug-{stamp}.log{suffix}").write_text("old")
    unrelated = logs / "notes.txt"
    unrelated.write_text("keep")
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    for day in range(1, 13):
        (custom_dir / f"debug-20250101-0000{day:02d}-000000.log").write_text("mine")

    diagnostics.configure(custom_dir / "chosen.log")
    assert len(list(custom_dir.iterdir())) == 13
    assert len(list(logs.glob("debug-*"))) == 36

    new = diagnostics.configure()
    diagnostics.configure(enabled=False)
    bases = sorted(p.name for p in logs.glob("debug-*.log"))
    assert len(bases) == 10
    assert new.name in bases
    assert "debug-20260101-000001-000000.log" not in bases
    assert "debug-20260101-000003-000000.log" not in bases
    assert "debug-20260101-000004-000000.log" in bases
    assert not list(logs.glob("debug-20260101-000003-000000.log.*"))
    assert len(list(logs.glob("debug-20260101-000012-000000.log.*"))) == 2
    assert unrelated.read_text() == "keep"
