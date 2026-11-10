"""View-model and settings logic needs no Qt."""

import json
from datetime import date
from pathlib import Path

import pytest
from conftest import FIXTURES, execute, planned

from logs_migrate.gui import viewmodel as vm
from logs_migrate.gui.settings import Settings
from logs_migrate.models import DAY, Window


def test_formatting_never_invents_precision():
    assert vm.format_bytes(None) == "Unavailable"
    assert vm.format_bytes(712_000_000) == "712 MB"
    assert vm.format_bytes(1_000) == "1.0 kB"
    assert vm.format_bytes_pair(553_000_000, 712_000_000) == "553 / 712 MB"
    assert vm.format_bytes_pair(500, None) == "500 B"
    assert vm.format_rate(None) == vm.format_rate(0) == "Unavailable"
    assert vm.format_rate(12_800_000) == "~12.8 MB/s"
    assert vm.format_elapsed(38) == "00:38" and vm.format_elapsed(3725) == "01:02:05"
    assert vm.format_eta(13) == "~13 sec" and vm.format_eta(600) == "~10 min"
    assert vm.format_eta(None) == "Unavailable"
    assert vm.format_percent(553, 712) == "77.7% of planned bytes"
    assert vm.format_version("9.0.2.0200") == "9.0.2" and vm.format_version("9.1.1") == "9.1.1"
    assert vm.format_count(1_280_000) == "1,280,000"


def test_day_ranges_render_inclusive_labels():
    start = 1_780_272_000_000  # 2026-06-01T00:00:00Z
    assert vm.format_day_range(start, start + 2 * DAY) == "Jun 1–Jun 2 UTC"
    assert vm.format_day_range(start, start + DAY) == "Jun 1 UTC"
    assert vm.format_day_range(start, start, inclusive_end=True, suffix="") == "Jun 1"
    assert vm.format_day_range(None, 1) == "Unavailable"


def test_presets_anchor_on_the_end_of_source_history():
    extent = (vm.window_from_days(date(2026, 3, 9), date(2026, 3, 9)).start, 0)
    end = vm.window_from_days(date(2026, 11, 4), date(2026, 11, 4)).end
    extent = (extent[0], end)
    assert vm.preset_days("All history", extent) == (date(2026, 3, 9), date(2026, 11, 4))
    assert vm.preset_days("90 days", extent) == (date(2026, 8, 7), date(2026, 11, 4))
    assert vm.preset_days("12 months", extent) == (date(2026, 3, 9), date(2026, 11, 4))
    assert vm.preset_days("nonsense", extent) is None
    assert vm.preset_days("90 days", None) is None
    assert vm.window_from_days(date(2026, 2, 2), date(2026, 2, 1)) is None
    window = vm.window_from_days(date(2026, 2, 1), date(2026, 2, 1))
    assert isinstance(window, Window) and window.end - window.start == DAY


def test_dates_before_the_epoch_are_refused_not_raised():
    assert vm.window_error(date(1969, 12, 31), date(1970, 1, 2)) is not None
    assert vm.window_from_days(date(1969, 12, 31), date(1970, 1, 2)) is None
    assert vm.window_error(date(1970, 1, 1), date(1970, 1, 2)) is None
    assert vm.window_from_days(date(1970, 1, 1), date(1970, 1, 2)) is not None


def test_session_expands_home_in_typed_paths(tmp_path, monkeypatch):
    from logs_migrate.gui.session import Session

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path.parent)
    (tmp_path / "fx.fixture").write_bytes((FIXTURES / "sealed-populated.fixture").read_bytes())
    session = Session()
    session.connect(fixture="~/fx.fixture", journal="~/journal.sqlite", credentials={})
    assert (tmp_path / "journal.sqlite").exists()
    assert not (tmp_path.parent / "~").exists()
    session.disconnect()


def test_plan_view_reports_groups_checks_and_start_gate(rig):
    engine, env, _ = rig
    run = planned(engine)
    discovered = engine.discover()
    view = vm.plan_view(run, discovered)
    assert view["events"] == 6 and view["size"] == 3000
    assert len(view["groups"]) == 4 and view["groups"][-1][3] == "Open · exclude"
    assert dict(view["checks"])["Latest data"] == "1 open group excluded"
    assert dict(view["checks"])["Storage"] == "Every node fits"
    assert [row[0] for row in view["storage"]] == ["dst-1", "dst-2", "dst-3"]
    assert view["blockers"] == [] and view["needs_ack"]
    assert vm.start_state(view, False, True) == (
        False,
        "Latest-data acknowledgment is required. Start submits this window to the destination API.",
    )
    assert vm.start_state(view, True, True)[0] is True
    assert vm.start_state(None, True, True)[0] is False
    assert vm.start_state(view, True, False)[0] is False
    env.data["destination"]["nodes"][0]["repository_available"] = 1
    short = vm.plan_view(planned(engine), discovered)
    assert short["blockers"] == ["storage"]
    assert "cannot hold" in vm.start_state(short, True, True)[1]


@pytest.mark.parametrize(
    ("growth", "shown", "warned"),
    [
        (
            {"bytes": 3_000_000, "nodes": ["a", "b"], "window_seconds": 9},
            "Active buckets on a, b grew by 3.0 MB in 9 s",
            True,
        ),
        ({"bytes": 0, "nodes": [], "window_seconds": 5}, "No active bucket growth in 5 s", False),
        ({"bytes": None, "nodes": [], "window_seconds": None}, "Unavailable", True),
    ],
)
def test_plan_view_reports_source_growth_from_the_engine(rig, growth, shown, warned):
    engine, env, _ = rig
    env.data["source_growth"] = growth
    run = planned(engine)
    assert run["plan"]["source_ingest"] == growth
    assert dict(vm.plan_view(run, engine.discover())["checks"])["Source ingest"] == shown
    warnings = [w for w in run["plan"]["warnings"] if "Source growth" in w or "grew by" in w]
    assert bool(warnings) is warned


def test_progress_view_marks_stale_estimates_unavailable():
    status = {
        "id": "sim-1",
        "status": "running",
        "bytes_transferred": 553,
        "total_bytes": 712,
        "rate_bytes_per_second": 12.8,
        "elapsed_seconds": 38,
        "eta_seconds": 13,
        "nodes": [
            {
                "node": "dst-1",
                "status": "completed",
                "completed": ["a"],
                "skipped": [],
                "bytes_transferred": 313,
            },
            {
                "node": "dst-2",
                "status": "running",
                "completed": [],
                "skipped": ["b"],
                "bytes_transferred": 0,
            },
        ],
        "window": {"start": 1_780_272_000_000, "end": 1_780_272_000_000 + DAY},
        "source": "src",
        "destination": "dst",
        "completed": ["a"],
        "skipped": ["b"],
        "bucket_keys": ["a", "b", "c"],
    }
    live = vm.progress_view(status, stale=False, last_ok="2026-11-06T15:22:38+00:00", zone="UTC")
    assert live["banner"][0] == "info" and live["progress"] == 77
    assert [t[1] for t in live["tiles"]] == ["553 / 712 B", "~13 B/s", "00:38", "~13 sec"]
    assert live["nodes"][1][4] == "1 skipped by the API"
    stale = vm.progress_view(status, stale=True, last_ok=None, zone="UTC")
    assert stale["banner"][0] == "warn"
    assert [t[1] for t in stale["tiles"]] == ["553 / 712 B", "Unavailable", "00:38", "Unavailable"]
    assert stale["nodes"][1][1] == "Unknown" and stale["nodes"][1][4] == "Last response stale"
    idle = vm.progress_view(None, stale=False, last_ok=None, zone="UTC")
    assert idle["state"] == "idle" and idle["progress"] is None
    failed = vm.progress_view({**status, "status": "failed"}, stale=False, last_ok=None, zone="UTC")
    assert failed["banner"][0] == "bad"


def test_daily_rows_compress_matching_periods_and_isolate_differences():
    daily = [
        {"day": "2026-05-29", "expected": 10, "actual": 10, "difference": 0},
        {"day": "2026-05-30", "expected": 10, "actual": 10, "difference": 0},
        {"day": "2026-05-31", "expected": 4, "actual": 3, "difference": -1},
        {"day": "2026-06-01", "expected": 5, "actual": 5, "difference": 0},
        {"day": "2026-06-03", "expected": 5, "actual": 5, "difference": 0},
    ]
    assert vm.daily_rows({"daily": daily}, None) == [
        ("May 29–May 30", "20", "20", "Match"),
        ("May 31", "4", "3", "−1"),
        ("Jun 1", "5", "5", "Match"),
        ("Jun 3", "5", "5", "Match"),
    ]


def test_verification_view_states(rig):
    engine, _, _ = rig
    run = planned(engine)
    execute(engine, run)
    status = engine.status()
    result = engine.verify(run["id"])
    view = vm.verification_view(result, status, 1, False)
    assert view["banner"][0] == "warn" and view["tiles"][0][1] == "8"  # open group excluded
    assert vm.verification_view(result, status, 0, False)["banner"][0] == "ok"
    assert view["rows"] and all(r[3] == "Match" for r in view["rows"])
    assert (
        dict(view["checks"])["Destination API task now"]
        == "COMPLETED; checked separately from counts"
    )
    pending = vm.verification_view(None, None, 0, True)
    assert pending["outcome"] == "pending" and pending["rows"] == []
    assert dict(pending["checks"])["Query method"] == "9.1 query API · Preview"
    incomplete = vm.verification_view(
        {"outcome": "incomplete_verification", "complete": False, "reason": "destination_busy"},
        status,
        1,
        False,
    )
    assert incomplete["banner"][0] == "warn" and incomplete["tiles"][1][1] == "Pending"
    mismatch = dict(result)
    mismatch["outcome"] = "verified_discrepancy"
    mismatch["daily"] = [{**mismatch["daily"][0], "actual": 1, "difference": -1}]
    bad = vm.verification_view(mismatch, status, 0, False)
    assert bad["banner"][0] == "bad" and "fewer" in bad["banner"][2]


def test_config_rows_and_results_from_engine_shapes():
    comparison = {
        "rules": [
            {
                "selector": "partitions:Archive",
                "type": "partitions",
                "name": "Archive",
                "status": "missing",
                "changed_fields": [],
                "warnings": ["restart"],
            },
            {
                "selector": "filters:drop-debug",
                "type": "filters",
                "name": "drop-debug",
                "status": "differs",
                "changed_fields": ["enabled"],
                "warnings": [],
            },
            {
                "selector": "forwarding:archive",
                "type": "forwarding",
                "name": "archive",
                "status": "missing",
                "changed_fields": [],
                "warnings": ["acceptCert:true adds trust"],
            },
            {
                "selector": "partitions:Default",
                "type": "partitions",
                "name": "Default",
                "status": "identical",
                "changed_fields": [],
                "warnings": [],
            },
        ],
        "destination_only": [],
    }
    rows = vm.config_rows(comparison)
    assert [r["action"] for r in rows] == [
        "Create · one destination restart",
        "Update enabled",
        "Create · trust target certificate",
        "Skip",
    ]
    assert [r["selected"] for r in rows] == [True, True, True, False]
    assert rows[1]["source"] == "Differs in enabled"
    result = {
        "rules": [
            {**comparison["rules"][0], "status": "created", "restart_required": True},
            {**comparison["rules"][1], "status": "updated", "restart_required": False},
            {
                **comparison["rules"][2],
                "status": "failed",
                "reason": "invalid_input",
                "restart_required": False,
            },
            {**comparison["rules"][3], "status": "skipped", "restart_required": False},
        ],
        "complete": False,
        "restart_required": True,
        "maintenance": "restart",
    }
    table = vm.config_result_rows(result)
    assert table[0] == ("Partition · Archive", "Created", "Restart required before routing")
    assert table[2][1] == "Failed · invalid input"
    assert vm.config_banner(result)[0] == "warn" and "archive" in vm.config_banner(result)[2]
    assert vm.config_banner({**result, "complete": True})[0] == "ok"
    assert vm.config_rows(None) == [] and vm.config_result_rows(None) == []


def test_report_view_records_the_acknowledgment(rig):
    engine, _, journal = rig
    run = planned(engine)
    execute(engine, run)
    stored, events = journal.load(run["id"]), journal.events(run["id"])
    view = vm.report_view(stored, events, engine.status(), "America/Chicago")
    assert view["ack_recorded"].startswith("Recorded before start · ")
    assert dict(view["rows"])["Selected groups"] == "3 sealed · 1 open excluded"
    assert vm.run_label(stored).startswith(f"Run {run['id'][:8]}")
    assert vm.run_label(stored, 1).startswith("Run 1 ·")


def test_settings_round_trip_and_bounds(tmp_path):
    path = tmp_path / "settings.ini"
    settings = Settings(
        theme="dark",
        timezone="UTC",
        refresh_seconds=0,
        verify_budget_minutes=5,
        count_poll_seconds=30,
    )
    assert settings.refresh_seconds == 1 and settings.verify_attempts == 10
    settings.save(path)
    loaded = Settings.load(path)
    assert loaded.theme == "dark" and loaded.export_folder == settings.export_folder
    (tmp_path / "broken.ini").write_text("[client]\nrefresh_seconds = many\ntheme = neon\n")
    fallback = Settings.load(tmp_path / "broken.ini")
    assert fallback.theme == "light" and fallback.refresh_seconds == 5
    assert Settings(count_poll_seconds=0).verify_attempts == 2


def test_describe_error_covers_every_code():
    from logs_migrate.errors import Code

    for code in Code:
        assert vm.describe_error(code.value)
    assert "failed" in vm.describe_error("something_else")


def test_connection_checks_mention_compatibility():
    discovered = {
        "source": json.loads((FIXTURES / "sealed-populated.fixture").read_text())["source"],
        "destination": json.loads((FIXTURES / "sealed-populated.fixture").read_text())[
            "destination"
        ],
    }
    rows = dict(vm.connection_checks(discovered, None))
    assert rows["Version compatibility"] == "Passed"
    assert rows["Source and destination API inventory"].startswith("Passed · 8.18.7 · 2 nodes")
    assert dict(vm.connection_checks(discovered, "unsupported_capability"))[
        "Version compatibility"
    ].startswith("Failed")
    assert dict(vm.connection_checks(None, None))["Version compatibility"] == "Not tested"


@pytest.mark.parametrize("name", ["sealed-populated.fixture", "sealed-911.fixture"])
def test_history_extent_covers_sealed_buckets(name):
    cluster = json.loads((FIXTURES / name).read_text())["source"]
    extent = vm.history_extent(cluster)
    assert extent is not None and extent[1] > extent[0]
    assert vm.history_extent({"buckets": []}) is None
    assert Path(name).suffix == ".fixture"


def test_start_notes_only_promise_no_post_for_pre_post_refusals():
    assert vm.start_note("acknowledgment_required").endswith("No API request was submitted.")
    assert "Do not submit again" in vm.start_note("remote_outcome_uncertain")
    assert "Do not submit again" in vm.start_note("operation_failed")
    view = {"blockers": [], "needs_ack": False}
    assert vm.start_state(view, True, True, started=True)[0] is False
    assert "already submitted" in vm.start_state(view, True, True, started=True)[1]


def test_format_time_renders_zone_local_time_without_platform_flags():
    assert vm.format_time("2026-11-06T15:04:05+00:00", "America/Chicago") == "Nov 6, 09:04:05 CST"
    assert vm.format_time("2026-03-02T01:02:03+00:00", "UTC") == "Mar 2, 01:02:03 UTC"
    assert vm.format_time(None, "UTC") == vm.UNAVAILABLE


def test_validation_helpers_say_what_is_wrong(tmp_path):
    problems = vm.validate_settings(
        "/nowhere/x.fixture", str(tmp_path / "j.sqlite"), "Mars/Olympus"
    )
    assert problems["fixture"].startswith("File not found")
    assert "Mars/Olympus" in problems["timezone"] and "journal" not in problems
    assert vm.validate_settings("", str(tmp_path), "UTC") == {
        "journal": "The journal path must name a file, not a folder."
    }
    assert vm.validate_settings("", str(tmp_path / "j.sqlite"), "UTC") == {}
    assert vm.window_error(date(2026, 3, 2), date(2026, 3, 1)).startswith("End date")
    assert vm.window_error(date(2026, 3, 1), date(2026, 3, 1)) is None
    values = {"source_host": "a", "ssh_user": "root"}
    assert "SSH password" in vm.missing_fields(values, False)
    assert "VCF Operations token" in vm.missing_fields(values, True)
    assert "Destination Logs API password" not in vm.missing_fields(values, True)


def test_plan_error_text_distinguishes_an_empty_window(rig):
    engine, _, _ = rig
    discovered = engine.discover()
    empty = vm.window_from_days(date(2025, 1, 1), date(2025, 1, 31))
    text = vm.plan_error_text("incomplete_evidence", discovered, empty)
    assert text.startswith("No sealed history in these dates.") and "spans" in text
    inside = vm.window_from_days(date(2026, 3, 1), date(2026, 3, 1))
    assert "unproven" in vm.plan_error_text("incomplete_evidence", discovered, inside)
    assert vm.plan_error_text("destination_busy", discovered, inside) == vm.describe_error(
        "destination_busy"
    )
    assert vm.describe_error("gui:custom text") == "custom text"


def test_plan_error_text_lists_only_the_selected_conflicting_groups(rig):
    engine, _, _ = rig
    discovered = engine.discover()
    first = discovered["source"]["buckets"][0]
    code = (
        "bucket_identity_conflict: Selected groups already exist on the destination: "
        f"{first['node']}/{first['id']} (2026-03-01 to 2026-03-01 UTC). "
        "Choose dates that exclude these groups."
    )
    text = vm.plan_error_text(code, discovered, None)
    assert text.startswith("1 selected groups already exist on the destination")
    assert text.count(" (") == 1
    duplicate = (
        f"bucket_identity_conflict: Source groups have duplicate identities: "
        f"{first['node']}/{first['id']}. Inspect the source inventory before retrying."
    )
    assert vm.plan_error_text(duplicate, discovered, None) == (
        f"Source groups have duplicate identities: {first['node']}/{first['id']}. "
        "Inspect the source inventory before retrying."
    )


def test_skipped_group_with_count_loss_is_labelled_a_discrepancy(rig):
    engine, env, _ = rig
    env.data["skip_buckets"] = [env.data["source_events"][0]["bucket"]]
    run = planned(engine)
    execute(engine, run)
    engine.status()
    env.data["destination_events"] = [
        e for e in env.data["destination_events"] if e.get("cohort") != "baseline"
    ]
    result = engine.verify(run["id"])
    stored = engine.journal.load(run["id"])
    assert result["outcome"] == "verified_discrepancy"
    assert not vm.received_counts_match(result)
    assert vm.outcome_label(result) == "counts differ · 1 group not received"
    banner = vm.verification_view(result, engine.status(), 1, False, stored)["banner"]
    assert "also differ" in banner[2] and "match their counts" not in banner[2]
    report = vm.report_view(stored, [], engine.status(), "UTC")["banner"]
    assert "also differ" in report[2] and "match their counts" not in report[2]


def test_outcome_labels_and_missing_days(rig):
    engine, env, _ = rig
    key = env.data["source_events"][0]["bucket"]
    env.data["skip_buckets"] = [key]
    run = planned(engine)
    execute(engine, run)
    engine.status()
    result = engine.verify(run["id"])
    stored = engine.journal.load(run["id"])
    assert result["buckets"]["not_received"] == [key]
    assert result["outcome"] == "incomplete_verification"
    assert vm.outcome_label(result) == "incomplete · 1 group not received"
    assert vm.outcome_label(None) == "verification pending"
    assert vm.outcome_label({"outcome": "verified_discrepancy"}) == "counts differ"
    days = vm.missing_days(result, stored)
    assert days and all(count > 0 for _, count in days)
    rows = vm.daily_rows(result, stored)
    assert any(row[3] == "Not received" for row in rows)
    view = vm.verification_view(result, engine.status(), 1, False, stored)
    assert view["outcome"] == "partial" and view["banner"][0] == "bad"
    assert "not received" in view["banner"][1]
    assert vm.run_label(stored, 2).startswith("Run 2 · ")


def test_rule_summaries_show_substance():
    forwarder = {"host": "siem.lab.example", "port": 6514, "protocol": "syslog", "sslEnabled": True}
    assert vm.rule_summary("forwarding", forwarder) == "siem.lab.example:6514 · syslog · TLS"
    assert vm.rule_summary(
        "partitions", {"criteria": 'appname="demo"', "retention_seconds": 86400}
    ) == ('appname="demo" · 1 days')
    assert vm.rule_summary(
        "masking", {"selector": "secret=(\\S+)", "maskValue": "****", "fieldName": "text"}
    ).startswith("secret=(\\S+) → **** (text)")
    assert vm.rule_summary("filters", {"filter": 'appname="debug"', "enabled": False}).endswith(
        "disabled"
    )
    assert vm.rule_summary("filters", None) == "Not readable"
    comparison = {
        "rules": [
            {
                "selector": "filters:drop",
                "type": "filters",
                "name": "drop",
                "status": "differs",
                "changed_fields": ["enabled"],
                "warnings": [],
            },
            {
                "selector": "forwarding:fw",
                "type": "forwarding",
                "name": "fw",
                "status": "missing",
                "changed_fields": [],
                "warnings": ["acceptCert"],
            },
        ],
        "destination_only": [],
        "details": {
            "filters:drop": {
                "source": {"filter": "a", "enabled": True},
                "destination": {"filter": "a", "enabled": False},
            },
            "forwarding:fw": {"source": forwarder, "destination": None},
        },
    }
    rows = vm.config_rows(comparison)
    assert rows[0]["source"] == "a · enabled"
    assert rows[0]["destination"] == "Same name · enabled: false"
    assert rows[0]["action"] == "Update enabled: false → true"
    assert rows[1]["source"].startswith("siem.lab.example:6514")
    assert rows[1]["action"] == "Create · trust target certificate"
    assert vm.axis_labels((1_780_272_000_000, 1_780_272_000_000 + 2 * DAY)) == (
        "Jun 1",
        "Jun 2",
        "Jun 2",
    )


def test_exported_markdown_uses_the_pages_words(rig):
    engine, env, journal = rig
    key = env.data["source_events"][0]["bucket"]
    env.data["skip_buckets"] = [key]
    run = planned(engine)
    unstarted = planned(engine)  # A preview that is never submitted, before history lands.
    execute(engine, run)
    status = engine.status()
    engine.verify(run["id"])
    stored, events = journal.load(run["id"]), journal.events(run["id"])
    text = vm.report_markdown(stored, events, status, "UTC", ("src.example", "dst.example"), 1)
    assert "verified_success" not in text and "incomplete_verification" not in text
    assert "Unix ms" not in text and "{" not in text and "Query `status`" not in text
    assert "## Not received" in text and "Group " in text and key not in text
    assert "Not received" in text and "Run 1 · incomplete · 1 group not received" in text
    assert "src.example → dst.example" in text
    text = vm.report_markdown(unstarted, journal.events(unstarted["id"]), status, "UTC")
    assert "not started" in text and "## Daily reconciliation" not in text
    assert vm.run_label(unstarted, 2).endswith("not started")
    assert (
        vm.plan_error_kind(
            "incomplete_evidence",
            engine.discover(),
            vm.window_from_days(date(2025, 1, 1), date(2025, 1, 2)),
        )
        == "info"
    )


def test_uncertain_start_banner_states_when_the_window_frees_up():
    status = {"status": "idle", "hold_until": "2026-11-06T16:00:00+00:00"}
    view = vm.progress_view(status, stale=False, last_ok=None, zone="UTC", start_uncertain=True)
    kind, _, text = view["banner"]
    assert kind == "warn"
    assert "outcome is unknown" in text and "15 minutes" in text
    assert vm.format_time("2026-11-06T16:00:00+00:00", "UTC") in text
    bare = vm.progress_view(None, stale=False, last_ok=None, zone="UTC", start_uncertain=True)
    assert "for 15 minutes" in bare["banner"][2]


def test_verification_view_names_what_timed_out():
    result = {
        "outcome": "incomplete_verification",
        "complete": False,
        "reason": "connection_failed",
        "detail": "destination count query from 2026-03-01 timed out",
    }
    view = vm.verification_view(result, None, 0, False)
    assert "destination count query from 2026-03-01 timed out" in " ".join(
        str(part) for part in view["banner"]
    )
