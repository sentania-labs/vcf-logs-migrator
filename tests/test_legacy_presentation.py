"""Saved runs retain their evidence while retired prompts disappear from presentation."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import execute, planned

from logs_migrate.cli import main
from logs_migrate.gui import viewmodel as vm
from logs_migrate.gui.session import Session
from logs_migrate.journal import Journal
from logs_migrate.models import canonical, digest
from logs_migrate.reporting import report_data

# Exact warning and persisted fields from the release before snapshot prompts were removed.
OLD_WARNING = (
    "No undo: migrated data stays on the destination. Snapshot the destination before "
    "continuing (snapshotting the source too is optional)."
)


@pytest.fixture
def legacy_journal(rig):
    engine, _, journal = rig
    run = planned(engine)
    execute(engine, run)
    engine.status()
    engine.verify(run["id"])
    run = journal.load(run["id"])
    # Recreate the record written by e14c1d6b, including the old plan hash and audit detail.
    run.update(backup_ack=True, backup_verified=False)
    run["plan"]["warnings"].insert(0, OLD_WARNING)
    run["plan_hash"] = digest(run["plan"])
    journal.db.execute("UPDATE runs SET body=? WHERE id=?", (canonical(run), run["id"]))
    journal.db.execute(
        "UPDATE events SET body=? WHERE run=? AND kind='transfer_authorized'",
        (canonical({"snapshot_verified": False}), run["id"]),
    )
    events = journal.events(run["id"])
    # Reopen through the real journal reader, as after an upgrade and restart.
    reopened = Journal(journal.path)
    try:
        yield reopened, run, events
    finally:
        reopened.close()


def expected_presentation(run, events):
    shown = deepcopy(run)
    del shown["backup_ack"], shown["backup_verified"]
    shown["plan"]["warnings"].remove(OLD_WARNING)
    shown_events = deepcopy(events)
    for event in shown_events:
        if event["kind"] == "transfer_authorized":
            del event["details"]["snapshot_verified"]
    return shown, shown_events


def assert_retired_text_absent(text):
    assert OLD_WARNING not in text
    assert "Snapshot acknowledgment" not in text
    assert '"backup_ack"' not in text
    assert '"backup_verified"' not in text
    assert '"snapshot_verified"' not in text


@pytest.mark.parametrize("format_", ["json", "markdown"])
def test_cli_reports_hide_retired_prompts_without_changing_evidence(
    legacy_journal, capsys, format_
):
    journal, run, events = legacy_journal
    assert main(["--journal", str(journal.path), "report", run["id"], "--format", format_]) == 0
    output = capsys.readouterr().out
    assert_retired_text_absent(output)
    if format_ == "json":
        shown, shown_events = expected_presentation(run, events)
        report = json.loads(output)
        assert report["run"] == shown
        assert report["events"] == shown_events
    else:
        assert run["id"] in output
        assert "| Day | Source |" in output
    assert journal.load(run["id"]) == run
    assert journal.events(run["id"]) == events


def test_window_plan_and_both_exports_hide_retired_prompts(legacy_journal, tmp_path):
    journal, run, events = legacy_journal
    shown, shown_events = expected_presentation(run, events)
    view = vm.plan_view(run, None)
    assert view["warnings"] == shown["plan"]["warnings"]
    session = Session()
    session.journal = journal
    assert session.runs() == [shown]
    assert session.report(run_id=run["id"]) == {"run": shown, "events": shown_events}
    paths = session.export(run_id=run["id"], folder=str(tmp_path), formats=["markdown", "json"])
    assert {Path(path).suffix for path in paths} == {".md", ".json"}
    for path in paths:
        output = Path(path).read_text()
        assert_retired_text_absent(output)
        if path.endswith(".json"):
            report = json.loads(output)
            assert report["run"] == shown
            assert report["events"] == shown_events
        else:
            assert "Latest-data acknowledgment" in output
    assert journal.load(run["id"]) == run
    assert journal.events(run["id"]) == events


def test_unrelated_warning_and_audit_evidence_are_preserved(legacy_journal):
    _, run, events = legacy_journal
    run = deepcopy(run)
    events = deepcopy(events)
    run["plan"]["warnings"].append("Partition backup-snapshots differs on the destination.")
    events.append(
        {"kind": "appliance_observation", "details": {"snapshot_verified": "appliance evidence"}}
    )
    shown, shown_events = expected_presentation(run, events)
    report = report_data(run, events)
    assert report["run"] == shown
    assert report["events"] == shown_events
    assert OLD_WARNING in run["plan"]["warnings"]
    assert run["backup_ack"] is True
    assert any(e["details"].get("snapshot_verified") is False for e in events)
