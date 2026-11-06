import json
import os

import pytest
from conftest import FIXTURES, execute, planned

from logs_migrate.cli import main
from logs_migrate.errors import MigrationError
from logs_migrate.journal import Journal
from logs_migrate.models import ACTIVE_ACK


def test_cli_persistent_workflow_and_reports(tmp_path, capsys):
    base = [
        "--journal",
        str(tmp_path / "journal.sqlite"),
        "--fixture",
        str(FIXTURES / "sealed-populated.fixture"),
    ]
    assert main([*base, "discover"]) == 0
    assert json.loads(capsys.readouterr().out)["source"]["version"] == "8.18.7"
    assert (
        main([*base, "plan", "--since", "2026-03-01T00:00:00Z", "--until", "2026-04-01T00:00:00Z"])
        == 0
    )
    run = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                *base,
                "copy",
                run["id"],
                "--plan-hash",
                run["plan_hash"],
                "--ack-backup",
                "--ack-active",
                ACTIVE_ACK,
            ]
        )
        == 0
    )
    copied = json.loads(capsys.readouterr().out)
    assert copied["state"] == "VERIFYING"
    assert copied["verification"]["outcome"] == "verification_pending"
    assert main([*base, "verify", run["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "VERIFIED"
    assert main([*base, "status", run["id"]]) == 0
    assert "journal_only" in json.loads(capsys.readouterr().out)["observation"]
    assert main([*base, "report", run["id"], "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["run"]["verification"]["actual_count"] == 8
    assert main([*base, "report", run["id"]]) == 0
    assert "verified_success" in capsys.readouterr().out


def test_cli_never_echoes_accidental_password_argument(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["--journal", str(tmp_path / "j"), "--password=canary-secret", "discover"])
    output = capsys.readouterr()
    assert "canary-secret" not in output.out + output.err


def test_cli_live_execution_is_explicitly_blocked(tmp_path, capsys):
    assert main(["--journal", str(tmp_path / "j"), "discover"]) == 3
    assert json.loads(capsys.readouterr().err)["error"] == "unsupported_capability"


def test_journal_private_mode_and_second_writer_rejected(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    second = Journal(tmp_path / "j.sqlite")
    try:
        if os.name == "posix":
            assert journal.path.stat().st_mode & 0o777 == 0o600
        with journal.exclusive():
            with pytest.raises(MigrationError, match="busy"):
                with second.exclusive():
                    pytest.fail("Second writer acquired a held lease")
    finally:
        journal.close()
        second.close()


def test_symlink_journal_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_text("unchanged")
    link = tmp_path / "journal"
    link.symlink_to(target)
    with pytest.raises(MigrationError):
        Journal(link)
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize("point", ["start.before", "start.after", "poll.after_complete"])
def test_crash_preserves_durable_intent_and_never_blindly_reposts(rig, point):
    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {point: "crash"}
    with pytest.raises(SystemExit):
        execute(engine, run)
    saved = journal.load(run["id"])
    assert saved["transfer_intent"]
    assert journal.transfers(env.data["destination"]["id"])
    env.data["faults"] = {}
    try:
        engine.resume(run["id"])
    except MigrationError:
        pass
    assert sum(t["operation"] == "start" for t in env.data["trace"]) == 1


@pytest.mark.parametrize(
    "point", ["discover", "test_mover", "start.before", "start.after", "poll.before"]
)
def test_error_text_and_journal_never_contain_remote_secrets(rig, point):
    engine, env, journal = rig
    run = planned(engine)
    env.data["faults"] = {point: "secret"}
    with pytest.raises(MigrationError) as error:
        execute(engine, run)
    assert "DO-NOT-PRINT" not in str(error.value)
    assert "DO-NOT-PRINT" not in json.dumps(journal.load(run["id"]))
    assert "DO-NOT-PRINT" not in json.dumps(journal.events(run["id"]))


def test_sigint_handler_is_installed_only_for_monitoring_commands(tmp_path, monkeypatch, capsys):
    import signal

    installed = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.append(sig))
    base = [
        "--journal",
        str(tmp_path / "journal.sqlite"),
        "--fixture",
        str(FIXTURES / "sealed-populated.fixture"),
    ]
    assert main([*base, "discover"]) == 0
    assert (
        main([*base, "plan", "--since", "2026-03-01T00:00:00Z", "--until", "2026-04-01T00:00:00Z"])
        == 0
    )
    run = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert not installed
    args = [*base, "copy", run["id"], "--plan-hash", run["plan_hash"], "--ack-backup"]
    assert main([*args, "--ack-active", ACTIVE_ACK]) == 0
    assert signal.SIGINT in installed


def test_cli_workflow_on_the_911_preview_fixture(tmp_path, capsys):
    base = [
        "--journal",
        str(tmp_path / "journal.sqlite"),
        "--fixture",
        str(FIXTURES / "sealed-911.fixture"),
    ]
    assert main([*base, "discover"]) == 0
    assert json.loads(capsys.readouterr().out)["destination"]["version"] == "9.1.1"
    assert (
        main([*base, "plan", "--since", "2026-03-01T00:00:00Z", "--until", "2026-04-01T00:00:00Z"])
        == 0
    )
    run = json.loads(capsys.readouterr().out)
    assert any("preview" in warning for warning in run["plan"]["warnings"])
    args = [*base, "copy", run["id"], "--plan-hash", run["plan_hash"], "--ack-backup"]
    assert main([*args, "--ack-active", ACTIVE_ACK]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "VERIFYING"
    assert main([*base, "verify", run["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "VERIFIED"


def _long_running_fixture(tmp_path):
    data = json.loads((FIXTURES / "sealed-populated.fixture").read_text())
    data["complete_after_polls"] = 100
    path = tmp_path / "long.fixture"
    path.write_text(json.dumps(data))
    return path


def _plan(base, capsys):
    since, until = "2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z"
    assert main([*base, "plan", "--since", since, "--until", until]) == 0
    return json.loads(capsys.readouterr().out)


def _copy(base, run):
    return main(
        [
            *base,
            "copy",
            run["id"],
            "--plan-hash",
            run["plan_hash"],
            "--ack-backup",
            "--ack-active",
            ACTIVE_ACK,
        ]
    )


def test_exit_code_follows_persisted_state_when_capacity_drops_mid_copy(
    tmp_path, capsys, monkeypatch
):
    from logs_migrate.adapters.simulated import SimulatedEnvironment

    base = [
        "--journal",
        str(tmp_path / "j.sqlite"),
        "--fixture",
        str(_long_running_fixture(tmp_path)),
    ]
    run = _plan(base, capsys)
    original = SimulatedEnvironment.transfer

    def transfer(self):
        result = original(self)
        if result.status == "running":
            self.data["destination"]["nodes"][0]["repository_available"] = 1
        return result

    monkeypatch.setattr(SimulatedEnvironment, "transfer", transfer)
    assert _copy(base, run) == 5
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert error == {"error": "insufficient_capacity", "run_id": run["id"]}
    assert main([*base, "status", run["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["run"]["state"] == "PARTIAL"


def test_exit_code_after_stop_with_source_drift_is_partial(tmp_path, capsys):
    from logs_migrate.adapters.simulated import SimulatedClock, SimulatedEnvironment
    from logs_migrate.engine import Engine

    fixture = _long_running_fixture(tmp_path)
    base = ["--journal", str(tmp_path / "j.sqlite"), "--fixture", str(fixture)]
    run = _plan(base, capsys)
    state = tmp_path / "j.simulated-state"
    env = SimulatedEnvironment.from_file(fixture, state)
    journal = Journal(tmp_path / "j.sqlite")
    engine = Engine(env, journal, SimulatedClock())
    engine.on_event = lambda e: (
        engine.request_detach() if e["kind"] == "transfer_start_observed" else None
    )
    assert execute(engine, run)["state"] == "MONITOR_DETACHED"
    journal.close()
    env.data["source"]["retention_fingerprint"] = "changed"
    env._persist()
    assert main([*base, "stop", run["id"]]) == 5
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])["run_id"] == run["id"]


def test_exit_code_is_recovery_required_when_post_start_integrity_fails(
    tmp_path, capsys, monkeypatch
):
    from logs_migrate.adapters.simulated import SimulatedEnvironment

    base = [
        "--journal",
        str(tmp_path / "j.sqlite"),
        "--fixture",
        str(FIXTURES / "sealed-populated.fixture"),
    ]
    run = _plan(base, capsys)
    monkeypatch.setattr(SimulatedEnvironment, "attest_transfer", lambda self, bucket: False)
    assert _copy(base, run) == 8
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])["error"] == (
        "integrity_mismatch"
    )
    assert main([*base, "status", run["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["run"]["state"] == "RECOVERY_REQUIRED"
