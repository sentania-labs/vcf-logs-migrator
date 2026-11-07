"""Automation interface to the migration API, without a local task controller."""

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from . import __version__
from .adapters.simulated import SimulatedClock, SimulatedEnvironment
from .engine import Engine
from .errors import Code, MigrationError, require
from .journal import Journal
from .models import Verification, Window, parse_time
from .planning import BACKUP_ACK
from .reporting import markdown, report_data
from .security import ENVIRONMENT, Credentials, harden_process
from .trust import TrustRequired, TrustStore

CONNECTION_ENVIRONMENT = {
    "source": "LOGS_SOURCE",
    "destination": "LOGS_DESTINATION",
    "ops_url": "LOGS_OPS_URL",
    "source_api_user": "LOGS_SOURCE_API_USER",
    "source_ssh_user": "LOGS_SOURCE_SSH_USER",
    "destination_api_user": "LOGS_DESTINATION_API_USER",
    "destination_ssh_user": "LOGS_DESTINATION_SSH_USER",
}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(
            2, "Invalid arguments; use --help. Credentials must never be command arguments.\n"
        )


def uuid_arg(value):
    try:
        if str(UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("invalid run id")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(CONNECTION_ENVIRONMENT[name], default)


def parser():
    p = Parser(prog="logs-migrate", description="Migration API wrapper for 8.18 to 9.x")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "--journal", type=Path, help="Private plans, sent-bucket ledger and count evidence"
    )
    simulator = p.add_argument_group("simulator (CI and demos)")
    simulator.add_argument("--fixture", type=Path, help="Synthetic appliance fixture")
    simulator.add_argument(
        "--remote-state", type=Path, help="Persisted simulated appliance; independent of journal"
    )
    live = p.add_argument_group(
        "live appliances",
        "Secrets come from the environment ("
        + ", ".join(ENVIRONMENT.values())
        + "), a --credentials file (mode 600, JSON keyed "
        + ", ".join(ENVIRONMENT)
        + ") or a prompt. Addresses may also come from "
        + ", ".join(CONNECTION_ENVIRONMENT.values())
        + ".",
    )
    live.add_argument("--source", default=_env("source"), help="Any 8.18 cluster member")
    live.add_argument(
        "--destination", default=_env("destination"), help="9.x Logs host[:port], default 9543"
    )
    live.add_argument(
        "--ops-url", default=_env("ops_url"), help="VCF Operations URL; selects 9.1.1 token sign-in"
    )
    live.add_argument("--source-api-user", default=_env("source_api_user", "admin"))
    live.add_argument("--source-ssh-user", default=_env("source_ssh_user", "root"))
    live.add_argument("--destination-api-user", default=_env("destination_api_user", "admin"))
    live.add_argument(
        "--destination-ssh-user",
        default=_env("destination_ssh_user"),
        help="Optional 9.0 root SSH for node budgets and bucket inventory",
    )
    live.add_argument("--credentials", type=Path, help="JSON file, mode 600; never argv")
    live.add_argument(
        "--trust-new",
        action="store_true",
        help="Enroll unknown TLS certificates and SSH host keys after printing them",
    )
    live.add_argument("--timeout", type=float, default=60.0, help="Seconds per request")
    sub = p.add_subparsers(dest="command", required=True)
    config = sub.add_parser("config", help="Compare/apply selected configuration rules")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("compare")
    apply = config_sub.add_parser("apply")
    apply.add_argument(
        "--select",
        action="append",
        required=True,
        metavar="TYPE:NAME",
        help="Exact selector from compare; repeat for each create or update",
    )
    gui = sub.add_parser("gui", help="Open the desktop window (needs the optional PySide6 extra)")
    gui.add_argument(
        "--fixture", dest="gui_fixture", type=Path, help="Prefill the simulator fixture"
    )
    gui.add_argument("--journal", dest="gui_journal", type=Path, help="Prefill the private journal")
    sub.add_parser("discover")
    sub.add_parser("partitions", help="Compare partitions (and extracted fields on 9.1.1)")
    sub.add_parser("status", help="Read current task and per-node progress directly from the API")
    selftest = sub.add_parser(
        "selftest", help="Read-only checks against real hosts with a pass/fail summary"
    )
    selftest.add_argument("--since", help="Plan preview start, ISO 8601 with timezone")
    selftest.add_argument("--until", help="Plan preview exclusive end, ISO 8601 with timezone")
    selftest.add_argument("--json", action="store_true", help="Machine-readable result")
    trust = sub.add_parser("trust", help="List or forget enrolled appliance identities")
    trust_sub = trust.add_subparsers(dest="trust_command", required=True)
    trust_sub.add_parser("list")
    forget = trust_sub.add_parser("forget")
    forget.add_argument("kind", choices=("tls", "ssh"))
    forget.add_argument("name", help="host:port as listed")
    plan = sub.add_parser("plan", help="Preview whole sealed buckets selected by date")
    plan.add_argument("--since", required=True, help="ISO 8601 with timezone")
    plan.add_argument("--until", required=True, help="Exclusive end, ISO 8601 with timezone")
    copy = sub.add_parser("copy", help="Start the API task once; status monitors it")
    copy.add_argument("run_id", type=uuid_arg)
    copy.add_argument("--ack-backup", action="store_true", help=BACKUP_ACK)
    copy.add_argument(
        "--ack-active", default="", help="Exact acknowledgment that latest data is skipped"
    )
    stop = sub.add_parser("stop", help="Cancel the currently observed API task by its API task ID")
    stop.add_argument("task_id")
    verify = sub.add_parser("verify", help="Compare complete, stable per-day counts")
    verify.add_argument("run_id", type=uuid_arg)
    report = sub.add_parser("report")
    report.add_argument("run_id", type=uuid_arg)
    report.add_argument("--format", choices=("json", "markdown"), default="markdown")
    return p


EXIT_CODES = {
    Code.INVALID_INPUT: 2,
    Code.UNCERTAIN: 8,
    Code.CONNECTION: 7,
    Code.AUTH: 7,
    Code.SCHEMA: 7,
    Code.STORAGE: 7,
    Code.BUSY: 7,
}


def _accept(identity, trust_new: bool) -> bool:
    print("Unknown appliance identity: " + identity.display, file=sys.stderr)
    if trust_new:
        print("Enrolled (--trust-new).", file=sys.stderr)
        return True
    if not sys.stdin.isatty():
        return False
    answer = input("Trust this identity and remember it? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def open_live(args):
    """Build the live environment; enroll identities the operator accepts."""
    from .adapters.live import Connection, LiveEnvironment

    require(bool(args.source.strip()) and bool(args.destination.strip()), Code.INVALID_INPUT)
    connection = Connection(
        source_host=args.source.strip(),
        destination=args.destination.strip(),
        source_api_user=args.source_api_user,
        source_ssh_user=args.source_ssh_user,
        destination_api_user=args.destination_api_user,
        destination_ssh_user=args.destination_ssh_user or "",
        ops_url=args.ops_url or "",
        timeout=args.timeout,
    )
    credentials = (
        Credentials.from_file(args.credentials, interactive=sys.stdin.isatty())
        if args.credentials
        else Credentials(interactive=True)
    )
    trust = TrustStore()
    env = LiveEnvironment(connection, credentials, trust)
    for _ in range(4):
        try:
            return env.open()
        except TrustRequired as pending:
            refused = [i for i in pending.identities if not _accept(i, args.trust_new)]
            if refused:
                print(
                    json.dumps(
                        {
                            "error": Code.AUTH.value,
                            "untrusted": [i.display for i in refused],
                            "hint": "Rerun with --trust-new or answer the prompt on a terminal.",
                        }
                    ),
                    file=sys.stderr,
                )
                raise MigrationError(Code.AUTH) from None
            for identity in pending.identities:
                trust.trust(identity, env.key_line(identity))
    raise MigrationError(Code.AUTH)


def main(argv=None):
    args = parser().parse_args(argv)
    journal = None
    try:
        harden_process()
        if args.command == "gui":
            from .gui import run

            return run(
                fixture=args.gui_fixture or args.fixture, journal=args.gui_journal or args.journal
            )
        if args.command == "trust":
            store = TrustStore()
            if args.trust_command == "list":
                print(
                    json.dumps([{"kind": k, "name": n, "sha256": d} for k, n, d in store.entries()])
                )
                return 0
            print(json.dumps({"forgotten": store.forget(args.kind, args.name)}))
            return 0
        if args.command in {"plan", "copy", "verify", "report"}:
            require(args.journal is not None, Code.INVALID_INPUT)
            journal = Journal(args.journal)
        elif args.command in {"status", "stop"} and args.journal is not None:
            journal = Journal(args.journal)
        if args.command == "report":
            run, events = journal.load(args.run_id), journal.events(args.run_id)
            if args.format == "markdown":
                sys.stdout.write(markdown(run, events))
            else:
                print(json.dumps(report_data(run, events), sort_keys=True))
            return 0
        live = bool(args.source.strip() or args.destination.strip())
        require(args.fixture is not None or live, Code.INVALID_INPUT)
        if args.fixture is not None:
            require(args.command != "selftest", Code.INVALID_INPUT)
            remote = args.remote_state or (
                args.journal.with_suffix(".simulated-state") if args.journal else None
            )
            require(remote is not None, Code.INVALID_INPUT)
            env = SimulatedEnvironment.from_file(args.fixture, remote)
            engine = Engine(env, journal, SimulatedClock())
        else:
            env = open_live(args)
            engine = Engine(env, journal)
        if args.command == "selftest":
            from . import selftest

            window = None
            if args.since or args.until:
                require(bool(args.since and args.until), Code.INVALID_INPUT)
                window = Window(parse_time(args.since), parse_time(args.until))
            result = selftest.run(env, window)
            if args.json:
                print(json.dumps(result.data(), sort_keys=True))
            else:
                sys.stdout.write(result.text())
            return 0 if result.passed else 1
        if args.command == "config":
            result = (
                engine.config_compare()
                if args.config_command == "compare"
                else engine.config_apply(args.select)
            )
        elif args.command in {"discover", "partitions"}:
            result = engine.discover()
            if args.command == "partitions":
                result = result["partition_diff"]
        elif args.command == "plan":
            result = engine.plan(Window(parse_time(args.since), parse_time(args.until)))
        elif args.command == "copy":
            result = engine.run(args.run_id, backup_ack=args.ack_backup, active_ack=args.ack_active)
        elif args.command == "status":
            result = engine.status()
        elif args.command == "stop":
            result = engine.stop(args.task_id)
        else:
            result = engine.verify(args.run_id)
        print(json.dumps(result, sort_keys=True))
        if args.command == "config" and args.config_command == "apply":
            if any(r["status"] == "uncertain" for r in result["rules"]):
                return 8
            return 0 if result["complete"] else 7
        if args.command == "verify":
            return {
                Verification.SUCCESS: 0,
                Verification.DISCREPANCY: 5,
                Verification.INCOMPLETE: 6,
            }.get(result["outcome"], 7)
        if args.command == "stop" and result["status"] not in {"completed", "cancelled", "failed"}:
            return 8
        return 0
    except MigrationError as error:
        print(
            json.dumps({"error": error.code.value, "run_id": getattr(args, "run_id", None)}),
            file=sys.stderr,
        )
        return EXIT_CODES.get(error.code, 3)
    except KeyboardInterrupt:
        print('{"error":"observation_interrupted; query API status"}', file=sys.stderr)
        return 8
    except Exception:
        print('{"error":"operation_failed; query API status"}', file=sys.stderr)
        return 7
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    sys.exit(main())
