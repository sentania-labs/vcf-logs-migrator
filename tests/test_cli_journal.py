import json
import os
import traceback

import pytest
from conftest import FIXTURES, execute, planned

from logs_migrate.cli import main
from logs_migrate.errors import MigrationError
from logs_migrate.journal import Journal
from logs_migrate.models import ACTIVE_ACK


@pytest.mark.parametrize("fixture", ["sealed-populated.fixture", "sealed-911.fixture"])
def test_cli_persistent_flow_and_offline_reports(tmp_path, capsys, fixture):
    base = ["--journal", str(tmp_path / "j.sqlite"), "--fixture", str(FIXTURES / fixture)]
    assert main([*base, "partitions"]) == 0
    assert json.loads(capsys.readouterr().out)["missing"] == []
    assert (
        main([*base, "plan", "--since", "2026-03-01T00:00:00Z", "--until", "2026-04-01T00:00:00Z"])
        == 0
    )
    run = json.loads(capsys.readouterr().out)
    assert main([*base, "copy", run["id"], "--ack-backup", "--ack-active", ACTIVE_ACK]) == 0
    assert json.loads(capsys.readouterr().out)["task"]["status"] == "running"
    api_only = [
        "--fixture",
        str(FIXTURES / fixture),
        "--remote-state",
        str(tmp_path / "j.simulated-state"),
    ]
    assert main([*api_only, "status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "idle"
    assert main([*base, "status", run["id"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "completed" and len(status["nodes"]) == 3
    assert main([*base, "verify", run["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "verified_success"
    offline = ["--journal", str(tmp_path / "j.sqlite"), "report", run["id"]]
    assert main([*offline, "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["run"]["backup_ack"] and report["run"]["backup_verified"] is False
    assert main(offline) == 0
    assert "verified_success" in capsys.readouterr().out


def test_journal_private_and_exclusive(rig):
    _, _, journal = rig
    if os.name == "posix":
        assert journal.path.stat().st_mode & 0o077 == 0
    other = Journal(journal.path)
    try:
        with journal.exclusive(), pytest.raises(MigrationError):
            with other.exclusive():
                pytest.fail("lock admitted a second writer")
    finally:
        other.close()


@pytest.mark.parametrize(
    "point",
    ["discover", "test_mover", "start.before", "start.after", "poll.before", "query.source"],
)
def test_adapter_exception_secrets_never_escape_errors_or_audit(rig, point):
    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {point: "secret"}
    with pytest.raises(MigrationError) as error:
        execute(engine, run)
    assert error.value.__context__ is None and error.value.__cause__ is None
    artifacts = [
        str(error.value),
        "".join(traceback.format_exception(error.value, chain=True)),
        json.dumps(journal.load(run["id"])),
        json.dumps(journal.events(run["id"])),
    ]
    assert all("DO-NOT-PRINT" not in a for a in artifacts)


@pytest.mark.parametrize("command", ["copy", "verify", "report"])
def test_cli_does_not_echo_pasted_secrets(tmp_path, capsys, command):
    with pytest.raises(SystemExit) as error:
        main(["--journal", str(tmp_path / "j.sqlite"), command, "Hunter2-pasted-by-mistake"])
    assert error.value.code == 2
    assert "Hunter2" not in "".join(capsys.readouterr())


@pytest.mark.parametrize("command", ["fixup", "recover", "close", "resume", "run", "start"])
def test_removed_controllers_are_unavailable(command, capsys):
    with pytest.raises(SystemExit) as error:
        main([command])
    assert error.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as help_:
        main(["--help"])
    assert help_.value.code == 0
    assert command not in capsys.readouterr().out


def test_neither_fixture_nor_hosts_is_invalid_input(tmp_path, capsys):
    assert main(["--journal", str(tmp_path / "j.sqlite"), "discover"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_input"


def test_connection_fault_exits_as_operation_failed_not_missing_prerequisite(tmp_path, capsys):
    data = json.loads((FIXTURES / "sealed-populated.fixture").read_text())
    data["faults"] = {"discover": "secret"}
    fixture = tmp_path / "faulty.fixture"
    fixture.write_text(json.dumps(data))
    code = main(["--journal", str(tmp_path / "j.sqlite"), "--fixture", str(fixture), "discover"])
    assert code == 7
    assert json.loads(capsys.readouterr().err)["error"] == "connection_failed"
