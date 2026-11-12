"""Stand-in log systems for trying the window without real ones.

Writes a simulator fixture with a believable amount of history (about twenty months across
three source servers, around a gigabyte a day) and a copy that takes time, so every screen
can be walked the way a person would use it. Nothing here contacts a real system.

    python tests/standin.py --output /tmp/standin [--scenario broken] [--speed 60]
    logs-migrate gui --fixture /tmp/standin/standin.fixture --journal /tmp/standin/journal.sqlite

In the window, the source is `logs-old.example.com` and the destination is
`logs-new.example.com`. Any password is accepted except the word `wrong`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

DAY = 86_400_000
GB = 1_000_000_000
SOURCE_NODES = ("logs-old-1", "logs-old-2", "logs-old-3")
DESTINATION_NODES = ("logs-new-1", "logs-new-2", "logs-new-3")
SPAN = 4  # Days each stored piece of logs covers on the source.
HISTORY_DAYS = 616
RETENTION_DAYS = 620
PER_DAY_BYTES = 1_016_000_000
PER_DAY_ENTRIES = 10_450_000
SCENARIOS = {
    "plain": "Everything works.",
    "broken": "The destination already holds broken copies of some days.",
    "space": "One destination server is nearly full.",
    "listfail": "A source server does not answer the first time dates are checked.",
    "missing": "The copy finishes, but one source server's logs for a few days do not arrive.",
    "short": "The copy finishes, but one day arrives a few entries short.",
    "fail": "The copy fails by itself part-way.",
    "stall": "No new logs arrive for a while part-way through.",
    "offline": "The window loses touch with the destination part-way through.",
    "notstarted": "The destination cannot reach the source, so the copy does not start.",
    "unsure": "The destination does not answer the request to start.",
    "srcnewer": "The source is newer than the destination.",
    "dstold": "The destination is too old to take logs in.",
    "mixed": "The source's servers are on different versions.",
    "ops9": "The destination is version 9.1 and signs in with an access token.",
    "rules-none": "Both systems already have the same rules.",
    "rules-many": "Fourteen rules differ.",
    "rules-partial": "One rule cannot be copied.",
    "unsteady": "The destination's counts do not settle when the result is checked.",
    "untested": "The destination is a version the app has not been tried with.",
}


def day_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def group_id(node: int, index: int) -> str:
    return f"{node + 1:08x}-0000-4000-8000-{index:012x}"


def groups(today: date) -> list[dict]:
    """Stored pieces of logs: every source server keeps the same four-day stretches."""
    last = today - timedelta(days=3)  # The newest finished day.
    first = last - timedelta(days=HISTORY_DAYS - 1)
    found = []
    for index in range(HISTORY_DAYS // SPAN):
        start = first + timedelta(days=index * SPAN)
        end = start + timedelta(days=SPAN - 1)
        for node, name in enumerate(SOURCE_NODES):
            wobble = 1 + ((index * 7 + node * 3) % 9 - 4) / 100
            identifier = group_id(node, index)
            found.append(
                {
                    "id": identifier,
                    "node": name,
                    "partition": "",
                    "status": "archived",
                    "size": int(PER_DAY_BYTES * SPAN / 3 * wobble),
                    "messages": PER_DAY_ENTRIES // 3 * SPAN,
                    "start": day_ms(start),
                    "end": day_ms(end) + DAY - 1000,
                    "manifest": hashlib.sha256(identifier.encode()).hexdigest(),
                    "bounds_proven": True,
                    "complete": True,
                }
            )
    for node, name in enumerate(SOURCE_NODES):  # What the source is still writing.
        found.append(
            {
                "id": group_id(node, 999_999),
                "node": name,
                "partition": "",
                "status": "active",
                "size": 400_000_000,
                "messages": 3_000_000,
                "start": day_ms(last + timedelta(days=1)),
                "end": day_ms(today),
                "manifest": None,
                "bounds_proven": True,
                "complete": False,
            }
        )
    return found


def events(found: list[dict]) -> list[dict]:
    """One counted entry per stored piece and day; `multiplicity` carries the real count."""
    listed = []
    for group in found:
        if group["status"] == "active":
            continue
        for offset in range(SPAN):
            listed.append(
                {
                    "id": f"{group['id']}-{offset}",
                    "time": group["start"] + offset * DAY + DAY // 2,
                    "bucket": f"{group['node']}/{group['id']}",
                    "multiplicity": group["messages"] // SPAN,
                    "fields": {"appname": "standin"},
                }
            )
    return listed


RULES = {
    "source": {
        "filters": [
            {"id": "f1", "name": "drop-debug", "enabled": False, "filter": 'appname="debug"'},
            {"id": "f2", "name": "drop-test", "enabled": False, "filter": 'hostname="test*"'},
        ],
        "masking": [
            {
                "id": "m1",
                "name": "hide-secrets",
                "enabled": True,
                "applyLogsFilter": "",
                "fieldName": "text",
                "selector": "password=(\\S+)",
                "maskValue": "****",
            },
            {
                "id": "m2",
                "name": "hide-card-numbers",
                "enabled": True,
                "applyLogsFilter": "",
                "fieldName": "text",
                "selector": "\\d{16}",
                "maskValue": "****",
            },
        ],
        "forwarding": [
            {
                "id": "w1",
                "name": "to-siem",
                "host": "siem.example.com",
                "port": 9543,
                "protocol": "cfapi",
                "sslEnabled": True,
                "transportProtocol": "TCP",
            }
        ],
    },
    "destination": {
        "filters": [
            {"id": "f1", "name": "drop-debug", "enabled": True, "filter": 'appname="debug"'},
            {"id": "f9", "name": "local-noise", "enabled": True, "filter": 'appname="noise"'},
        ],
        "masking": [
            {
                "id": "m2",
                "name": "hide-card-numbers",
                "enabled": True,
                "applyLogsFilter": "",
                "fieldName": "text",
                "selector": "\\d{16}",
                "maskValue": "****",
            }
        ],
        "forwarding": [],
    },
}


def build(
    scenario: str = "plain", *, today: date | None = None, speed: float = 60, delay: float = 0.7
) -> dict:
    """The fixture for one scenario. `speed` is how much faster than real time the copy's own
    clock runs; `delay` is the pause per progress step so a person can watch it."""
    if scenario not in SCENARIOS:
        raise SystemExit(f"Unknown scenario {scenario!r}. Choose from: {', '.join(SCENARIOS)}")
    today = today or datetime.now(UTC).date()
    found = groups(today)
    default = {
        "name": "Default",
        "id": "",
        "criteria": "",
        "retention_seconds": RETENTION_DAYS * 86400,
        "order": 0,
    }
    firewall = {
        "name": "Firewall",
        "id": "p-fw",
        "criteria": 'appname="firewall"',
        "retention_seconds": 90 * 86400,
        "order": 1,
    }
    source_nodes = [
        {
            "id": name,
            "capacity": 2_000 * GB,
            "used": 700 * GB,
            "repository_available": 1_200 * GB,
            "service": "running",
        }
        for name in SOURCE_NODES
    ]
    free = (310, 298, 305)
    destination_nodes = [
        {
            "id": name,
            "capacity": 600 * GB,
            "used": 200 * GB,
            "repository_available": free[i] * GB,
            "service": "running",
        }
        for i, name in enumerate(DESTINATION_NODES)
    ]
    rules = json.loads(json.dumps(RULES))
    source_partitions = [default, firewall]
    destination_partitions = [default]
    if scenario == "rules-none":
        rules["destination"] = json.loads(json.dumps(rules["source"]))
        destination_partitions = [default, firewall]
    if scenario == "rules-many":
        for name in ("web", "db", "vpn", "dns", "mail", "auth", "backup", "print", "lab"):
            rules["source"]["forwarding"].append(
                {
                    "id": f"w-{name}",
                    "name": f"{name}-archive",
                    "host": "archive.example.com",
                    "port": 514,
                    "protocol": "syslog",
                    "sslEnabled": False,
                    "transportProtocol": "TCP",
                }
            )
    data = {
        "fixture_schema": "logs-migrate.simulator.v1",
        "fixture_id": f"standin-{scenario}",
        "source": {
            "id": "logs-old",
            "version": "8.18.7",
            "nodes": source_nodes,
            "partitions": source_partitions,
            "fields": ["appname", "hostname"],
            "buckets": found,
            "identity": "standin-source-certificate",
            "history_complete": True,
            "config": rules["source"],
        },
        "destination": {
            "id": "logs-new",
            "version": "9.0.2.0200",
            "nodes": destination_nodes,
            "partitions": destination_partitions,
            "fields": ["appname", "hostname"],
            "buckets": [],
            "identity": "standin-destination-certificate",
            "history_complete": True,
            "inventory_available": True,
            "config": rules["destination"],
        },
        "source_events": events(found),
        "destination_events": [],
        "ingest_during_run": [],
        "artifact_verified": True,
        "addresses": {
            "source": "logs-old.example.com",
            "destination": "logs-new.example.com",
            "ops": "ops.example.com",
        },
        "sign_in": {
            kind: ["wrong"]
            for kind in (
                "source_api",
                "source_ssh",
                "destination_api",
                "destination_ssh",
                "ops_token",
            )
        },
        "stage_delay": delay,
        "pace": {"bytes_per_second": 11_500_000, "time_scale": speed},
        "faults": {},
    }
    sealed = [g for g in found if g["status"] != "active"]
    recent = sealed[-3 * 30 :]  # Roughly the last four months, newest last.

    def key(group: dict) -> str:
        return f"{group['node']}/{group['id']}"

    if scenario == "broken":
        partial, differs = recent[10], recent[41]
        data["destination"]["buckets"] = [
            {
                **partial,
                "node": "logs-new-2",
                "size": partial["size"] // 2,
                "complete": False,
                "registered": False,
                "manifest": None,
            },
            {**differs, "node": "logs-new-1", "size": differs["size"] - 4096},
        ]
    elif scenario == "space":
        destination_nodes[1]["repository_available"] = 51 * GB
        destination_nodes[1]["used"] = 520 * GB
    elif scenario == "listfail":
        data["faults"]["discover"] = [None, "connection"]
    elif scenario == "missing":
        data["skip_buckets_once"] = [key(recent[13]), key(recent[43])]
    elif scenario == "short":
        data["short_events"] = {key(recent[20]): 123}
    elif scenario == "fail":
        data["fail_at"] = {"fraction": 0.31, "node": "logs-new-2", "once": True}
    elif scenario == "stall":
        data["stall"] = {"at_fraction": 0.2, "seconds": 2400}
    elif scenario == "offline":
        data["offline"] = {"at_fraction": 0.2, "seconds": 20}
    elif scenario == "notstarted":
        data["faults"]["test_mover"] = ["connection"]
    elif scenario == "unsure":
        data["faults"]["start.before"] = ["connection"]
    elif scenario == "srcnewer":
        data["source"]["version"] = "9.0.2"
        data["destination"]["version"] = "9.0.1"
    elif scenario == "dstold":
        data["destination"]["version"] = "8.18.5"
        data["destination"]["migration_api"] = "absent"
    elif scenario == "mixed":
        data["source"]["node_versions"] = {
            "logs-old-1": "8.18.7",
            "logs-old-2": "8.18.5",
            "logs-old-3": "8.18.7",
        }
    elif scenario == "ops9":
        data["destination"]["version"] = "9.1.1"
    elif scenario == "rules-partial":
        data["faults"]["config.write.forwarding.before"] = ["connection"]
    elif scenario == "unsteady":
        data["query_completeness"] = {"destination": [False] * 4000}
    elif scenario == "untested":
        data["destination"]["version"] = "9.2.0"
    return data


def write(folder: Path, scenario: str = "plain", **options) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "standin.fixture"
    path.write_text(json.dumps(build(scenario, **options)))
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--output", type=Path, required=True, help="Folder to write into")
    parser.add_argument("--scenario", default="plain", choices=sorted(SCENARIOS))
    parser.add_argument(
        "--speed", type=float, default=60, help="Copy clock speed against real time"
    )
    parser.add_argument("--delay", type=float, default=0.7, help="Seconds per progress step")
    args = parser.parse_args(argv)
    path = write(args.output, args.scenario, speed=args.speed, delay=args.delay)
    print(f"{SCENARIOS[args.scenario]}")
    print(f"logs-migrate gui --fixture {path} --journal {args.output / 'journal.sqlite'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
