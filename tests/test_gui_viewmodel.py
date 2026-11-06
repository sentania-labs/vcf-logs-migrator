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
    assert vm.compress_daily(daily) == [
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
    assert view["banner"][0] == "ok" and view["tiles"][0][1] == "8"
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
        "Create · TLS certificate accepted",
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
    assert vm.run_label(stored).startswith(run["id"][:8])


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
