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
@pytest.mark.parametrize("compat_flags", [[], ["--ack-backup"]])
def test_cli_persistent_flow_and_offline_reports(tmp_path, capsys, fixture, compat_flags):
    base = ["--journal", str(tmp_path / "j.sqlite"), "--fixture", str(FIXTURES / fixture)]
    assert main([*base, "partitions"]) == 0
    assert json.loads(capsys.readouterr().out)["missing"] == []
    assert (
        main([*base, "plan", "--since", "2026-03-01T00:00:00Z", "--until", "2026-04-01T00:00:00Z"])
        == 0
    )
    run = json.loads(capsys.readouterr().out)
    assert main([*base, "copy", run["id"], *compat_flags, "--ack-active", ACTIVE_ACK]) == 0
    assert json.loads(capsys.readouterr().out)["task"]["status"] == "running"
    api_only = [
        "--fixture",
        str(FIXTURES / fixture),
        "--remote-state",
        str(tmp_path / "j.simulated-state"),
    ]
    assert main([*api_only, "status"]) == 0
    untied = json.loads(capsys.readouterr().out)
    assert untied["status"] == "idle" and untied["run"] is None
    assert untied["no_run"]["reason"] == "no_journal"
    assert [t["id"] for t in untied["destination_tasks"]["tasks"]] == ["sim-1"]
    assert main([*base, "status", run["id"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "completed" and len(status["nodes"]) == 3
    assert main([*base, "verify", run["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "verified_success"
    offline = ["--journal", str(tmp_path / "j.sqlite"), "report", run["id"]]
    assert main([*offline, "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["run"]["active_ack"] is True
    assert "backup" not in json.dumps(report).lower()
    assert "snapshot" not in json.dumps(report).lower()
    assert main(offline) == 0
    assert "verified_success" in capsys.readouterr().out


def test_cli_resume_survives_reopening_the_journal(tmp_path, capsys):
    fixture = tmp_path / "fixture.json"
    data = json.loads((FIXTURES / "sealed-populated.fixture").read_text())
    data["cancel_after"] = 1
    fixture.write_text(json.dumps(data))
    base = ["--journal", str(tmp_path / "j.sqlite"), "--fixture", str(fixture)]
    assert (
        main([*base, "plan", "--since", "2026-03-01T00:00:00Z", "--until", "2026-04-01T00:00:00Z"])
        == 0
    )
    first = json.loads(capsys.readouterr().out)
    assert main([*base, "copy", first["id"], "--ack-active", ACTIVE_ACK]) == 0
    capsys.readouterr()
    assert main([*base, "status", first["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
    state_file = tmp_path / "j.simulated-state"
    remote = json.loads(state_file.read_text())
    del remote["cancel_after"]
    state_file.write_text(json.dumps(remote))
    assert main([*base, "resume", first["id"], "--preview"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert len(resumed["plan"]["already_received"]) == 1
    assert len(resumed["plan"]["selected"]) == 2
    assert main([*base, "copy", resumed["id"], "--ack-active", ACTIVE_ACK]) == 0
    capsys.readouterr()
    assert main([*base, "status", resumed["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert main([*base, "verify", resumed["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "verified_success"


def test_inventory_cache_expires_and_does_not_use_future_timestamps(tmp_path):
    journal = Journal(tmp_path / "cache.sqlite")
    try:
        journal.inventory_put("cluster-node", 1000, "listing")
        assert journal.inventory_get("cluster-node", 1299, 300) == "listing"
        assert journal.inventory_get("cluster-node", 1300, 300) is None
        assert journal.inventory_get("cluster-node", 999, 300) is None
    finally:
        journal.close()


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


@pytest.mark.parametrize("command", ["fixup", "recover", "close", "run", "start"])
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


def test_copy_help_hides_legacy_backup_flag(capsys):
    with pytest.raises(SystemExit) as result:
        main(["copy", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "--ack-backup" not in help_text
    assert "--ack-active" in help_text
