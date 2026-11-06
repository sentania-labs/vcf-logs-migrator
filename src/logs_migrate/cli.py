"""Automation interface to the migration API, without a local task controller."""

import argparse
import json
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
from .security import harden_process


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


def parser():
    p = Parser(prog="logs-migrate", description="Migration API wrapper (fixture delivery)")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "--journal", type=Path, help="Private plans, sent-bucket ledger and count evidence"
    )
    p.add_argument("--fixture", type=Path, help="Synthetic appliance fixture")
    p.add_argument(
        "--remote-state", type=Path, help="Persisted simulated appliance; independent of journal"
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("discover")
    sub.add_parser("partitions", help="Compare partitions (and extracted fields on 9.1.1)")
    sub.add_parser("status", help="Read current task and per-node progress directly from the API")
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


def main(argv=None):
    args = parser().parse_args(argv)
    journal = None
    try:
        harden_process()
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
        require(args.fixture is not None, Code.UNSUPPORTED)
        remote = args.remote_state or (
            args.journal.with_suffix(".simulated-state") if args.journal else None
        )
        require(remote is not None, Code.INVALID_INPUT)
        env = SimulatedEnvironment.from_file(args.fixture, remote)
        engine = Engine(env, journal, SimulatedClock())
        if args.command in {"discover", "partitions"}:
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
