"""The plain words the window shows, derived from engine results. No Qt."""

import re
from datetime import date

import pytest
from conftest import execute

from logs_migrate.errors import Code
from logs_migrate.gui import copies, copyview
from logs_migrate.gui import viewmodel as vm
from logs_migrate.models import Policy, Window, parse_time
from logs_migrate.reporting import display_run
from logs_migrate.versions import evaluate

BANNED = re.compile(
    r"\b(bucket|buckets|group id|sealed|active tail|api task|ingest|reconciliation|journal|"
    r"fixture|node|nodes|ssh|secure shell|port 22)\b",
    re.IGNORECASE,
)
MARCH = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))


def recorded(journal) -> list[dict]:
    return copies.build([display_run(run) for run in journal.runs()], journal.transfer_rows())


# Formatting --------------------------------------------------------------------------------


def test_sizes_counts_and_durations_read_the_way_a_person_says_them():
    assert vm.format_bytes(186_000_000_000) == "186 GB"
    assert vm.format_bytes(57_600_000_000) == "57.6 GB"
    assert vm.format_bytes(812_000_000) == "812 MB"
    assert vm.format_bytes(None) == "not known"
    assert vm.format_big(1_912_350_000) == "1.9 billion"
    assert vm.format_big(104_500_000) == "104 million"
    assert vm.format_big(123) == "123"
    assert vm.format_duration(47 * 60) == "47 min"
    assert vm.format_duration(2 * 3600 + 26 * 60) == "2 h 26 min"
    assert vm.format_duration(20) == "under a minute"
    assert vm.format_estimate(4 * 3600, 6 * 3600) == "about 4 to 6 hours"
    assert vm.format_estimate(600, 1500) == "about 10 to 25 minutes"
    assert vm.format_estimate(30, 100) == "a few minutes"
    assert vm.format_estimate(None, None) == "not known"
    assert vm.format_version("9.0.2.0200") == "9.0.2"


def test_dates_are_written_once_with_the_year_where_it_helps():
    assert vm.format_range(date(2026, 11, 10), date(2026, 11, 10)) == "Nov 10, 2026"
    assert vm.format_range(date(2026, 5, 11), date(2026, 11, 9)) == "May 11 to Nov 9, 2026"
    assert vm.format_range(date(2025, 11, 16), date(2026, 1, 31)) == "Nov 16, 2025 to Jan 31, 2026"
    ranges = [(date(2026, 7, 14), date(2026, 7, 16)), (date(2026, 9, 2), date(2026, 9, 3))]
    assert vm.format_ranges(ranges) == "Jul 14 to Jul 16, 2026 and Sep 2 to Sep 3, 2026"
    now = "2026-11-12T20:10:00+00:00"
    assert (
        vm.format_clock("2026-11-12T20:10:00+00:00", "America/Chicago", now) == "today at 2:10 PM"
    )
    assert (
        vm.format_clock("2026-11-13T07:05:00+00:00", "America/Chicago", now)
        == "tomorrow at 1:05 AM"
    )
    assert (
        vm.format_clock("2026-11-03T15:02:00+00:00", "America/Chicago", now)
        == "on Nov 3 at 9:02 AM"
    )
    assert vm.format_when("2026-11-03T15:02:00+00:00", "America/Chicago") == "Nov 3, 2026, 9:02 AM"
    assert vm.format_when(None, "UTC") == "not recorded"


# Errors ------------------------------------------------------------------------------------


def test_every_engine_code_has_plain_words_and_the_engine_sentence_never_shows():
    for code in Code:
        what, todo = vm.plain_error(code.value)
        assert what and todo, code
        assert BANNED.search(what + " " + todo) is None, code
    sentence = f"{Code.OVERLAP.value}: A transfer is running for these buckets: 0000-id (src-1)."
    what, todo = vm.plain_error(sentence)
    assert "0000-id" not in what + todo and "bucket" not in what + todo
    assert vm.plain_error("something_unknown") == vm.UNKNOWN_ERROR
    assert vm.plain_error("gui:Could not write to /nowhere") == ("Could not write to /nowhere", "")
    assert "What happened:" in vm.what_happened(Code.BUSY.value)


# Connect -----------------------------------------------------------------------------------


def test_what_is_still_needed_counts_down_in_the_order_it_is_typed():
    empty = vm.need_list({}, token=False)
    assert empty == [
        "source address",
        "source password",
        "source server password",
        "destination address",
        "destination password",
    ]
    values = {
        "source_host": "a",
        "source_password": "x",
        "ssh_password": "x",
        "destination_host": "b",
    }
    assert vm.need_list(values, token=False) == ["destination password"]
    assert vm.need_list(values, token=True) == ["VCF Operations address", "access token"]
    values["destination_password"] = "x"
    assert vm.need_list(values, token=False) == []
    assert vm.need_list(values, token=False, server_needed=True) == ["destination server sign-in"]
    values["destination_ssh_user"] = "root"
    assert vm.need_list(values, token=False) == [
        "destination server password (or clear the server user)"
    ]


def test_fingerprints_take_the_form_a_browser_and_an_administrator_show():
    digest = "3a1f9c44d207b85e61f02b937dc518a6e40b7239cf865a11d74e902cb368fd25"
    assert (
        vm.fingerprint_pairs(digest).startswith("3A:1F:9C:44")
        and len(vm.fingerprint_pairs(digest)) == 95
    )
    assert re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", vm.server_key(digest))
    assert vm.server_key("SHA256:already") == "SHA256:already"


@pytest.mark.parametrize(
    ("source", "destination", "api", "title", "badge"),
    [
        (
            {"a": "9.0.2"},
            {"b": "9.0.1"},
            "present",
            "the source is newer than the destination",
            ("destination_badge", "Needs upgrading"),
        ),
        (
            {"a": "8.18.5"},
            {"b": "8.18.7"},
            "absent",
            "the destination is too old",
            ("destination_badge", "Too old"),
        ),
        (
            {"a": "8.18.7", "b": "8.18.5", "c": "8.18.7"},
            {"d": "9.0.2"},
            "present",
            "the source servers are on different versions",
            ("source_badge", "Servers disagree"),
        ),
    ],
)
def test_a_refused_pairing_names_the_system_that_has_to_change(
    source, destination, api, title, badge
):
    view = vm.verdict(evaluate(source, destination, api, "test"))
    assert not view["passed"] and title in view["title"]
    assert view[badge[0]] == badge[1]
    assert "What to do:" in view["text"]


def test_versions_that_agree_pass_and_an_untried_one_says_so():
    view = vm.verdict(
        evaluate({"a": "8.18.7", "b": "8.18.7"}, {"cluster": "9.0.2"}, "present", "t")
    )
    assert view["passed"] and view["title"] == "✓ Connected. These two systems can migrate."
    assert view["source_servers"] == 2 and view["destination_servers"] is None
    assert view["caution"] is None
    assert vm.verdict(evaluate({"a": "8.18.7"}, {"b": "9.2.0"}, "present", "t"))["caution"]
    disagreement = vm.verdict(
        evaluate({"a": "8.18.7", "b": "8.18.5", "c": "8.18.7"}, {"d": "9.0.2"}, "present", "t")
    )
    assert "b is on 8.18.5 while a and c are on 8.18.7" in disagreement["text"]


def test_a_failed_test_points_at_one_box_and_says_what_the_other_side_did():
    wrong = {"side": "destination", "field": "password", "kind": "auth", "other_signed_in": True}
    assert vm.failure_banner(wrong)[1] == "The source signed in. The destination did not."
    assert vm.field_hint(wrong, "destination", "password").endswith("Type the password again.")
    assert vm.field_hint(wrong, "source", "password") is None
    assert vm.header_chips({"failure": wrong}) == [
        vm.chip("ok", "✓ Source signed in"),
        vm.chip("bad", "✕ Destination not signed in"),
    ]
    assert vm.failure_banner({**wrong, "edited": True})[0] == "info"
    lost = {"side": "source", "field": "address", "kind": "reach", "other_found": True}
    kind, title, text = vm.failure_banner(lost)
    assert title == "Could not find the source system." and "No password was sent to either" in text
    assert vm.header_chips({"failure": lost})[0] == vm.chip("bad", "✕ Source not found")
    assert vm.header_chips({}) == [vm.chip("", "Not connected")]


# Rules -------------------------------------------------------------------------------------

COMPARISON = {
    "rules": [
        {
            "selector": "partitions:Firewall",
            "type": "partitions",
            "name": "Firewall",
            "status": "missing",
            "changed_fields": [],
            "warnings": [],
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
            "selector": "forwarding:to-siem",
            "type": "forwarding",
            "name": "to-siem",
            "status": "missing",
            "changed_fields": [],
            "warnings": ["TLS forwarders use acceptCert:true"],
        },
        {
            "selector": "masking:same",
            "type": "masking",
            "name": "same",
            "status": "identical",
            "changed_fields": [],
            "warnings": [],
        },
    ],
    "destination_only": [{"type": "filters", "name": "local"}],
    "details": {
        "partitions:Firewall": {
            "source": {"name": "Firewall", "retention_seconds": 90 * 86400},
            "destination": None,
        },
        "filters:drop-debug": {
            "source": {"enabled": False, "filter": "x"},
            "destination": {"enabled": True, "filter": "x"},
        },
        "forwarding:to-siem": {
            "source": {"host": "siem.example.com", "protocol": "cfapi"},
            "destination": None,
        },
    },
}


def test_rules_say_what_they_do_and_mark_the_one_the_copy_depends_on():
    rows = vm.rule_rows(COMPARISON)
    assert [row["name"] for row in rows] == [
        "Storage group “Firewall”",
        "Filter “drop-debug”",
        "Forwarding “to-siem”",
    ]
    needed, switched, forward = rows
    assert needed["needed"] and "Logs cannot be copied until this exists" in needed["note"]
    assert needed["does"] == "Keeps the logs it selects for 90 days."
    assert (switched["now"], switched["will"]) == (
        "Switched on",
        "Switch it off here too, so those logs are kept",
    )
    assert forward["does"] == "Sends a copy of new logs to siem.example.com."
    assert "trust that system’s certificate" in forward["note"]
    for row in rows:
        assert "cfapi" not in str(row) and BANNED.search(" ".join(map(str, row.values()))) is None
    title, text = vm.rules_banner(rows)
    assert title.startswith("3 rules on the source are missing or different")
    assert (
        "One of them, a storage group marked Needed" in text
        and "The other 2 are your choice." in text
    )
    assert vm.rules_tail(vm.rules_summary(COMPARISON)) == (
        "1 more rule is already the same on both sides. "
        "1 rule exists only on the destination and is left alone."
    )
    assert vm.rules_state(COMPARISON, None) == "diff"
    assert (
        vm.rules_state({"rules": [COMPARISON["rules"][3]], "destination_only": []}, None) == "none"
    )
    assert vm.needed_outstanding(COMPARISON, None)


def test_rule_results_are_copied_left_out_or_not_copied_with_a_reason():
    result = {
        "complete": False,
        "rules": [
            {
                "selector": "partitions:Firewall",
                "type": "partitions",
                "name": "Firewall",
                "status": "created",
                "comparison": "missing",
            },
            {
                "selector": "filters:drop-debug",
                "type": "filters",
                "name": "drop-debug",
                "status": "skipped",
                "comparison": "differs",
            },
            {
                "selector": "forwarding:to-siem",
                "type": "forwarding",
                "name": "to-siem",
                "status": "failed",
                "comparison": "missing",
                "reason": "connection_failed",
            },
            {
                "selector": "masking:same",
                "type": "masking",
                "name": "same",
                "status": "unchanged",
                "comparison": "identical",
            },
        ],
    }
    rows = vm.rule_results(result)
    assert [row["badge"][1] for row in rows] == ["Copied", "Left out", "Not copied"]
    assert rows[2]["text"] == "The destination did not answer, or refused it"
    assert vm.rules_state(COMPARISON, result) == "partial"
    assert not vm.needed_outstanding(COMPARISON, result)


# Dates and the plan ------------------------------------------------------------------------


def test_presets_count_back_from_the_last_day_that_can_be_picked():
    extent = {"first": date(2025, 3, 3), "last": date(2026, 11, 9), "newest": date(2026, 11, 10)}
    assert [name for name, _ in vm.PRESETS] == [
        "Last 3 months",
        "Last 6 months",
        "Last 12 months",
        "Everything",
    ]
    assert vm.preset_days(3, extent) == (date(2026, 8, 10), date(2026, 11, 9))
    assert vm.preset_days(12, extent) == (date(2025, 11, 10), date(2026, 11, 9))
    assert vm.preset_days(None, extent) == (date(2025, 3, 3), date(2026, 11, 9))
    short = {"first": date(2026, 10, 1), "last": date(2026, 11, 9), "newest": None}
    assert vm.preset_days(6, short)[0] == date(2026, 10, 1)
    assert (
        vm.dates_problem(date(2026, 5, 1), date(2026, 4, 1), extent)
        == "The end date is before the start date."
    )
    assert "still being written" in vm.dates_problem(date(2026, 5, 1), date(2026, 11, 11), extent)
    assert vm.dates_problem(date(2026, 5, 1), date(2026, 11, 9), extent) is None


def test_the_plan_reads_as_short_lines_from_what_the_engine_planned(rig):
    engine, env, journal = rig
    run = engine.plan(MARCH, Policy(poll_seconds=0))
    inventory = engine.discover()
    extent = vm.history(inventory["source"])
    preview = copies.preview([display_run(run)])
    view = copyview.plan_view(
        preview, (date(2026, 3, 1), date(2026, 3, 31)), extent, server_sign_in=True
    )
    assert view["phase"] == "ready"
    keys = [line["key"] for line in view["lines"]]
    assert keys[:4] == ["Copies", "Size", "Time", "Room"] and keys[-1] == "Left out"
    assert "will not be copied this time" in view["ack"] and view["needs_ack"]
    assert BANNED.search(" ".join(line["text"] + line["small"] for line in view["lines"])) is None
    blind = copyview.plan_view(
        preview, (date(2026, 3, 1), date(2026, 3, 31)), extent, server_sign_in=False
    )
    room = next(line for line in blind["lines"] if line["key"] == "Room")
    assert "Not checked." in room["text"] and "cannot be continued from this app" in room["small"]
    months = copyview.months_view(view)
    assert months[0][0] == "Mar 2026" and months[-1][3] == "No. Still being written."
    assert copyview.plan_detail_csv(view).splitlines()[0].startswith("status,source_server")


def test_a_refusal_becomes_a_heading_the_days_affected_and_a_button_that_fixes_it():
    asked = (date(2026, 5, 10), date(2026, 11, 9))
    day = 86_400_000
    start = parse_time("2026-07-14T00:00:00Z")
    broken = {
        "kind": "broken",
        "groups": [
            {
                "id": "g1",
                "source_node": "old-1",
                "start": start,
                "end": start + 3 * day - 1000,
                "destination_node": "new-2",
                "reason": "incomplete",
            },
            {
                "id": "g2",
                "source_node": "old-2",
                "start": start + 50 * day,
                "end": start + 52 * day - 1000,
                "destination_node": "new-1",
                "reason": "differs",
            },
        ],
    }
    view = copyview.refusal_view(
        "bucket_identity_conflict: g1 g2 …", broken, asked, None, zone="UTC"
    )
    assert view["title"].startswith("5 days cannot be copied")
    assert view["table"][1] == [
        ("Jul 14 to Jul 16, 2026", "new-2", "Only part of it arrived"),
        ("Sep 2 to Sep 3, 2026", "new-1", "Size differs from the source"),
    ]
    assert view["actions"] == [("Plan without these 5 days", "exclude", "", "primary")]
    assert "g1" not in str(view) and "old-1" not in str(view)
    assert copyview.exclusions(broken) == [
        {"first": "2026-07-14", "last": "2026-07-16"},
        {"first": "2026-09-02", "last": "2026-09-03"},
    ]
    details = copyview.refusal_details(broken, {"source": "old", "destination": "new"})
    assert "identifier: g1" in details and "source server: old-1" in details

    running = {
        "kind": "running",
        "run": "run-1",
        "groups": [
            {
                "id": "g",
                "source_node": "s",
                "start": parse_time("2026-08-12T00:00:00Z"),
                "end": parse_time("2026-11-09T23:00:00Z"),
            }
        ],
        "task": {
            "bytes_transferred": 31,
            "total_bytes": 100,
            "progress": None,
            "started_at": "2026-11-12T20:10:00+00:00",
        },
    }
    view = copyview.refusal_view(
        "overlapping_transfer: …", running, asked, None, zone="America/Chicago"
    )
    assert view["title"] == "Part of these dates is already being copied"
    assert view["table"][1][0] == ("Aug 12 to Nov 9, 2026", "In the copy running now (31% done)")
    assert view["table"][1][1] == ("May 10 to Aug 11, 2026", "Free to copy")
    assert [action[:3] for action in view["actions"]] == [
        ("Watch the running copy", "watch", "run-1"),
        ("Copy only May 10 to Aug 11, 2026", "use-dates", "2026-05-10|2026-08-11"),
    ]

    held = {**running, "kind": "held", "held_until": "2026-11-12T20:25:00+00:00"}
    view = copyview.refusal_view(
        "uncertain_start_pending: …", held, asked, None, zone="America/Chicago"
    )
    assert view["title"] == "These dates are locked until 2:25 PM"
    assert "locked for 15 minutes" in view["text"]

    extent = {"first": date(2025, 3, 3), "last": date(2026, 11, 9), "newest": date(2026, 11, 10)}
    nothing = copyview.refusal_view(
        "incomplete_evidence: No dated…", {"kind": "empty"}, asked, extent, zone="UTC"
    )
    assert (
        nothing["card"] == "Nothing to do"
        and nothing["actions"][0][0] == "Use Mar 3, 2025 to Nov 9, 2026"
    )
    other = copyview.refusal_view(Code.BUSY.value, None, asked, extent, zone="UTC")
    assert other["title"] == "These dates could not be checked" and "What to do:" in other["text"]


# Copies and their results ------------------------------------------------------------------


def test_a_plan_that_was_never_started_is_not_a_run(rig):
    engine, env, journal = rig
    run = engine.plan(MARCH, Policy(poll_seconds=0))
    assert recorded(journal) == []
    execute(engine, run)
    engine.status(run["id"])
    (copy,) = recorded(journal)
    assert copy["state"] == "finished" and copy["done_bytes"] == copy["total_bytes"]
    assert copy["segments"] == 1 and copy["days"][0] == date(2026, 3, 1)
    assert copies.check(copy)["phase"] == "unchecked"
    assert copyview.run_result(copy, copies.check(copy)) == ("", "Finished, not checked")


def test_a_finished_copy_whose_days_all_match_says_everything_arrived(rig):
    engine, env, journal = rig
    run = engine.plan(MARCH, Policy(poll_seconds=0))
    execute(engine, run)
    engine.status(run["id"])
    engine.verify(run["id"])
    (copy,) = recorded(journal)
    result = copies.check(copy)
    assert result["phase"] == "ok" and result["source_total"] == result["arrived_total"] > 0
    view = copyview.check_view(copy, result)
    assert view["verdict"][2] == "Everything arrived"
    assert [action[0] for action in view["actions"]] == ["Copy more dates", "Save report"]
    assert view["actions"][-1][3] == "primary"
    assert copyview.run_result(copy, result) == ("ok", "Everything arrived")
    report = copyview.report_markdown(copy, result, zone="UTC")
    assert "**Everything arrived.**" in report and "## All days" in report
    assert BANNED.search(report) is None and run["id"] not in report


def test_a_copy_that_stopped_is_never_called_complete(fixture, tmp_path):
    from logs_migrate.adapters.simulated import SimulatedClock, SimulatedEnvironment
    from logs_migrate.engine import Engine
    from logs_migrate.journal import Journal

    fixture["cancel_after"] = 1
    clock = SimulatedClock()
    journal = Journal(tmp_path / "journal.sqlite", clock)
    engine = Engine(SimulatedEnvironment(fixture), journal, clock)
    run = engine.plan(MARCH, Policy(poll_seconds=0))
    execute(engine, run)
    assert engine.status(run["id"])["status"] == "cancelled"
    (copy,) = recorded(journal)
    assert copy["state"] == "stopped" and 0 < copy["done_bytes"] < copy["total_bytes"]
    assert copyview.run_result(copy, copies.check(copy))[1].startswith("Stopped at ")
    engine.verify(run["id"])
    (copy,) = recorded(journal)
    result = copies.check(copy)
    assert result["phase"] == "partial" and result["unfinished"]
    view = copyview.check_view(copy, result)
    assert "So far so good" in view["verdict"][2] and "Everything arrived" not in str(view)
    assert view["actions"][-1][0] == "Continue copying"
    # Continued and finished: the earlier count no longer speaks for the copy.
    del engine.env.data["cancel_after"]
    resumed = engine.resume(run["id"])
    execute(engine, resumed)
    engine.status(resumed["id"])
    (copy,) = recorded(journal)
    assert copy["state"] == "finished" and copy["segments"] == 2
    assert copies.check(copy)["phase"] == "unchecked"
    engine.verify(resumed["id"])
    (copy,) = recorded(journal)
    assert copies.check(copy)["phase"] == "ok"
    journal.close()


def test_logs_that_never_arrived_are_listed_by_day_and_source_server(fixture, tmp_path):
    from logs_migrate.adapters.simulated import SimulatedClock, SimulatedEnvironment
    from logs_migrate.engine import Engine
    from logs_migrate.journal import Journal

    skipped = "src-1/00000000-0000-0000-0000-000000000001"
    fixture["skip_buckets"] = [skipped]
    clock = SimulatedClock()
    journal = Journal(tmp_path / "journal.sqlite", clock)
    engine = Engine(SimulatedEnvironment(fixture), journal, clock)
    run = engine.plan(MARCH, Policy(poll_seconds=0))
    execute(engine, run)
    engine.status(run["id"])
    engine.verify(run["id"])
    (copy,) = recorded(journal)
    assert copy["state"] == "missing"
    result = copies.check(copy)
    assert result["phase"] == "diff" and result["can_copy_missing"]
    (issue,) = result["issues"]
    assert (issue["kind"], issue["node"], issue["first"]) == ("missing", "src-1", date(2026, 3, 1))
    assert issue["source"] > issue["arrived"]
    view = copyview.check_view(copy, result)
    assert "need" in view["verdict"][2] and view["verdict"][0] == "bad"
    assert view["actions"][-1][:2] == ("Copy what is missing", "copy-missing")
    assert view["table"][1][0][3] == ("bad", "Part never arrived")
    assert skipped not in str(view) and "00000000" not in str(view)
    past = copyview.check_view(copy, result, past=True)
    assert past["actions"][-1][0] == "Connect to copy what is missing"
    assert copyview.run_result(copy, result)[0] == "bad"
    journal.close()


def test_a_result_the_engine_did_not_verify_is_never_shown_as_everything_arrived(rig):
    engine, env, journal = rig
    run = engine.plan(MARCH, Policy(poll_seconds=0))
    execute(engine, run)
    engine.status(run["id"])
    engine.verify(run["id"])
    stored = journal.load(run["id"])
    stored["verification"]["outcome"] = "verified_discrepancy"
    stored["verification"]["bucket_check"] = {
        "matched_groups": 0,
        "total_groups": 1,
        "matched_events": 0,
        "total_events": 2,
        "unmatched": [
            stored["plan"]["selected"][0]["node"] + "/" + stored["plan"]["selected"][0]["id"]
        ],
        "unknown": [],
    }
    journal.save(stored, "test")
    (copy,) = recorded(journal)
    result = copies.check(copy)
    assert result["phase"] == "diff" and result["issues"][0]["kind"] == "differs"
    view = copyview.check_view(copy, result)
    assert view["table"][1][0][3] == ("bad", "Count differs") and view["verdict"][0] == "bad"
    stored["verification"]["bucket_check"]["unmatched"] = []
    stored["verification"]["outcome"] = "incomplete_verification"
    stored["verification"]["reason"] = "destination_busy"
    journal.save(stored, "test")
    (copy,) = recorded(journal)
    result = copies.check(copy)
    assert result["phase"] == "incomplete"
    view = copyview.check_view(copy, result)
    assert (
        view["verdict"][2] == "Could not finish checking" and "still running" in view["verdict"][3]
    )


# Found by the self-review -----------------------------------------------------------------


def _two_part_copy(journal, engine):
    """One copy made of two runs around a left-out stretch, both finished."""
    series = {
        "id": "series-1",
        "count": 2,
        "requested": {"start": MARCH.start, "end": MARCH.end},
        "excluded": [{"first": "2026-03-02", "last": "2026-03-02"}],
    }
    first = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-02T00:00:00Z"))
    second = Window(parse_time("2026-03-03T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    runs = []
    for index, window in enumerate((first, second)):
        run = engine.plan(window, Policy(poll_seconds=0), {"series": {**series, "index": index}})
        execute(engine, run)
        engine.status(run["id"])
        runs.append(run)
    return runs


def test_one_part_checked_says_nothing_about_a_part_that_was_not(rig):
    engine, env, journal = rig
    first, second = _two_part_copy(journal, engine)
    engine.verify(first["id"])
    (copy,) = recorded(journal)
    assert len(copy["parts"]) == 2 and copy["state"] == "finished"
    result = copies.check(copy)
    assert result["phase"] == "unchecked"
    assert copyview.run_result(copy, result) == ("", "Finished, not checked")
    engine.verify(second["id"])
    (copy,) = recorded(journal)
    assert copies.check(copy)["phase"] == "ok"
    # A check that was asked for and failed is not answered with the older result.
    view = copyview.check_view(copy, copies.check(copy), error=Code.BUSY.value)
    assert view["verdict"][2] == "Could not finish checking"


def test_a_stopped_copy_that_could_not_be_counted_is_not_called_so_far_so_good(fixture, tmp_path):
    from logs_migrate.adapters.simulated import SimulatedClock, SimulatedEnvironment
    from logs_migrate.engine import Engine
    from logs_migrate.journal import Journal

    fixture["cancel_after"] = 1
    clock = SimulatedClock()
    journal = Journal(tmp_path / "journal.sqlite", clock)
    engine = Engine(SimulatedEnvironment(fixture), journal, clock)
    run = engine.plan(MARCH, Policy(poll_seconds=0))
    execute(engine, run)
    engine.status(run["id"])
    stored = journal.load(run["id"])
    stored["verification"] = {
        "outcome": "incomplete_verification",
        "complete": False,
        "reason": "connection_failed",
        "at": clock.now(),
    }
    journal.save(stored, "test")
    (copy,) = recorded(journal)
    result = copies.check(copy)
    assert result["phase"] == "incomplete"
    view = copyview.check_view(copy, result)
    assert "So far so good" not in str(view) and view["verdict"][2] == "Could not finish checking"
    journal.close()


def test_what_earlier_runs_delivered_still_counts_while_the_copy_runs_again(fixture, tmp_path):
    from logs_migrate.adapters.simulated import SimulatedClock, SimulatedEnvironment
    from logs_migrate.engine import Engine
    from logs_migrate.journal import Journal

    fixture["cancel_after"] = 1
    fixture["complete_after_polls"] = 1
    clock = SimulatedClock()
    journal = Journal(tmp_path / "journal.sqlite", clock)
    engine = Engine(SimulatedEnvironment(fixture), journal, clock)
    run = engine.plan(MARCH, Policy(poll_seconds=0))
    execute(engine, run)
    engine.status(run["id"])
    (stopped,) = recorded(journal)
    del engine.env.data["cancel_after"]
    engine.env.data["complete_after_polls"] = 50
    resumed = engine.resume(run["id"])
    execute(engine, resumed)
    (running,) = recorded(journal)
    assert running["state"] == "running"
    assert running["done_bytes"] == stopped["done_bytes"] > 0
    journal.close()


def test_a_part_with_nothing_left_to_send_is_over_and_its_days_are_named(rig):
    engine, env, journal = rig
    series = {
        "id": "s",
        "count": 2,
        "requested": {"start": MARCH.start, "end": MARCH.end},
        "excluded": [],
    }
    first = Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-03-02T00:00:00Z"))
    second = Window(parse_time("2026-03-02T00:00:00Z"), parse_time("2026-04-01T00:00:00Z"))
    one = engine.plan(first, Policy(poll_seconds=0), {"series": {**series, "index": 0}})
    two = engine.plan(second, Policy(poll_seconds=0), {"series": {**series, "index": 1}})
    execute(engine, one)
    engine.status(one["id"])
    (copy,) = recorded(journal)
    assert copy["state"] == "between" and copies.next_part(copy)["index"] == 1
    stored = journal.load(two["id"])
    stored["client"]["series"]["skipped"] = "gone"
    stored["client"]["series"]["gone_ids"] = [g["id"] for g in stored["plan"]["selected"]]
    journal.save(stored, "part_skipped")
    (copy,) = recorded(journal)
    assert copy["state"] == "finished" and copies.next_part(copy) is None
    assert copy["gone"] and "no longer on the source" in copyview.left_out(copy)
    assert copy["total_bytes"] == copy["done_bytes"]


def test_a_part_whose_logs_are_gone_is_skipped_once_and_never_asked_for_again(
    tmp_path, monkeypatch
):
    import json

    import standin

    from logs_migrate.gui.session import Session

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fixture = standin.write(tmp_path, "plain", speed=50000, delay=0)
    journal = str(tmp_path / "journal.sqlite")
    names = {"source_host": "logs-old.example.com", "destination": "logs-new.example.com"}
    session = Session()
    try:
        session.open_record(journal=journal)
        session.connect(
            fixture=str(fixture),
            journal=journal,
            credentials={"destination_ssh": "x"},
            connection={**names, "destination_ssh_user": "root"},
        )
        session.config_apply(selection=["partitions:Firewall"])
        extent = vm.history(session.discover()["source"])
        start, end = vm.window_ms(*vm.preset_days(3, extent))
        hole = vm.preset_days(3, extent)[0].replace(day=28)
        plan = session.plan(
            start=start,
            end=end,
            attempts=2,
            poll_seconds=0,
            exclude=[{"first": hole.isoformat(), "last": hole.isoformat()}],
        )
        first, second = plan["parts"]
        series = {k: v for k, v in first["client"]["series"].items() if k != "index"}
        session.start(run_id=first["id"], acknowledged=True)
        import time

        for _ in range(200):
            if session.status(run_id=first["id"])["status"] != "running":
                break
            time.sleep(0.02)
        # The source deletes everything the second part would have copied.
        state = tmp_path / "journal.simulated-state"
        data = json.loads(state.read_text())
        window = series["windows"][1]
        data["source"]["buckets"] = [
            b
            for b in data["source"]["buckets"]
            if b["status"] == "active" or b["start"] < window["start"]
        ]
        state.write_text(json.dumps(data))
        result = session.start_part(
            series=series,
            index=1,
            attempts=2,
            poll_seconds=0,
            client={},
            acknowledged=True,
            preview=second["id"],
        )
        assert result == {"skipped": True, "index": 1}
        record = session.record()
        (copy,) = copies.build(record["runs"], record["rows"])
        assert copies.next_part(copy) is None and copy["gone"]
    finally:
        session.close()


def test_the_gap_quoted_for_short_days_is_the_total_across_them(rig):
    engine, env, journal = rig
    run = engine.plan(MARCH, Policy(poll_seconds=0))
    execute(engine, run)
    engine.status(run["id"])
    engine.verify(run["id"])
    (copy,) = recorded(journal)
    result = copies.check(copy)
    day = copy["days"][0]
    result["phase"] = "diff"
    result["issues"] = [
        {
            "kind": "short",
            "first": day,
            "last": day,
            "difference": 123,
            "source": 500,
            "arrived": 377,
        },
        {
            "kind": "short",
            "first": day,
            "last": day,
            "difference": 400,
            "source": 900,
            "arrived": 500,
        },
    ]
    result["differing_days"] = 1
    note = copyview.check_view(copy, result)["note"]
    assert "the gap is 523 entries" in note


def _split_copy_in_session(tmp_path, monkeypatch):
    """A copy planned around one left-out day, with its first part finished."""
    import time

    import standin

    from logs_migrate.gui.session import Session

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fixture = standin.write(tmp_path, "plain", speed=50000, delay=0)
    journal = str(tmp_path / "journal.sqlite")
    names = {"source_host": "logs-old.example.com", "destination": "logs-new.example.com"}
    session = Session()
    session.open_record(journal=journal)
    session.connect(
        fixture=str(fixture),
        journal=journal,
        credentials={"destination_ssh": "x"},
        connection={**names, "destination_ssh_user": "root"},
    )
    session.config_apply(selection=["partitions:Firewall"])
    extent = vm.history(session.discover()["source"])
    start, end = vm.window_ms(*vm.preset_days(3, extent))
    hole = vm.preset_days(3, extent)[0].replace(day=28)
    plan = session.plan(
        start=start,
        end=end,
        attempts=2,
        poll_seconds=0,
        exclude=[{"first": hole.isoformat(), "last": hole.isoformat()}],
        labels={"source": names["source_host"], "destination": names["destination"]},
    )
    first, second = plan["parts"]
    session.start(run_id=first["id"], acknowledged=True)
    for _ in range(200):
        if session.status(run_id=first["id"])["status"] != "running":
            break
        time.sleep(0.02)
    series = {k: v for k, v in first["client"]["series"].items() if k != "index"}
    return session, series, second, tmp_path / "journal.simulated-state"


def _next_part(session, series, second):
    return session.start_part(
        series=series,
        index=1,
        attempts=2,
        poll_seconds=0,
        client={},
        acknowledged=True,
        preview=second["id"],
    )


def test_logs_that_left_the_source_before_a_later_part_are_named_not_dropped(tmp_path, monkeypatch):
    """The plan the person agreed to is the oracle: what it held and the copy never sent
    must be reported as not copied, never silently left out of a finished copy."""
    import json
    import time

    session, series, second, state = _split_copy_in_session(tmp_path, monkeypatch)
    try:
        planned = second["plan"]["selected"]
        retired = planned[0]
        assert len(planned) > 1
        data = json.loads(state.read_text())
        data["source"]["buckets"] = [
            b for b in data["source"]["buckets"] if b["id"] != retired["id"]
        ]
        state.write_text(json.dumps(data))
        result = _next_part(session, series, second)
        assert "skipped" not in result
        for _ in range(200):
            if session.status(run_id=result["run"]["id"])["status"] != "running":
                break
            time.sleep(0.02)
        record = session.record()
        (copy,) = copies.build(record["runs"], record["rows"])
        assert copy["state"] == "finished"
        assert set(copies.group_days(retired)) <= set(copy["gone"])
        assert retired["id"] not in {group["id"] for group in copy["groups"]}
        assert "no longer on the source" in copyview.left_out(copy)
    finally:
        session.close()


@pytest.mark.parametrize("side", ["source", "destination"])
def test_a_later_part_is_refused_when_connected_to_a_different_system(tmp_path, monkeypatch, side):
    import json

    from logs_migrate.gui.session import Failure

    session, series, second, state = _split_copy_in_session(tmp_path, monkeypatch)
    try:
        data = json.loads(state.read_text())
        data[side]["id"] = "another-system"
        data[side]["identity"] = "another-identity"
        state.write_text(json.dumps(data))
        before = session.record()
        with pytest.raises(Failure) as refused:
            _next_part(session, series, second)
        assert vm.error_code(refused.value.code) == Code.TOPOLOGY
        assert refused.value.data["sent"] is False and refused.value.data["systems"] == [side]
        after = session.record()
        # Nothing was planned, skipped or started against the other system.
        assert [run["id"] for run in after["runs"]] == [run["id"] for run in before["runs"]]
        (copy,) = copies.build(after["runs"], after["rows"])
        assert copy["state"] == "between" and copies.next_part(copy)["index"] == 1
        shown = copyview.continue_failure(
            {"code": refused.value.code, **refused.value.data}, server_sign_in=True
        )
        assert shown["connect"] and f"the {side} you are connected to now" in shown["text"]
        assert "logs-old.example.com" in shown["text"]
    finally:
        session.close()
