"""Thin automation CLI. All behavior belongs to Engine and its adapter contracts."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from . import __version__
from .adapters.simulated import SimulatedClock, SimulatedEnvironment
from .engine import Engine
from .errors import Code, MigrationError, require
from .journal import Journal
from .models import Policy, State, Verification, Window, parse_time
from .reporting import markdown, report_data
from .security import harden_process


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default echoes unknown arguments, including accidental passwords.
        self.print_usage(sys.stderr)
        self.exit(
            2, "Invalid arguments; use --help. Credentials must never be command arguments.\n"
        )


def parser():
    p = Parser(
        prog="logs-migrate", description="Historical log migration engine (fixture delivery)"
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--journal", type=Path, required=True, help="Durable private SQLite run journal")
    p.add_argument("--fixture", type=Path, help="Synthetic environment fixture; no appliance calls")
    p.add_argument(
        "--remote-state", type=Path, help="Persist the simulated appliance across processes"
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("discover")
    plan = sub.add_parser("plan")
    plan.add_argument("--since", required=True, help="ISO 8601 with explicit timezone")
    plan.add_argument("--until", required=True, help="Exclusive ISO 8601 end with timezone")
    plan.add_argument("--tail-of", help="Verified prior 9.0 run; never permitted on 9.1.1")
    plan.add_argument("--fixup-existing", action="store_true", help="Plan a standalone 9.0 fixup")
    plan.add_argument(
        "--create-missing", action="store_true", help="Consent to displayed routing/retention"
    )
    plan.add_argument("--ack-backup", action="store_true")
    plan.add_argument("--partition-hash", help="Reviewed partition_preparation_hash from discover")
    plan.add_argument("--ceiling-percent", type=int, default=90)
    plan.add_argument("--reserve-bytes", type=int, default=0)
    plan.add_argument("--growth-bytes", type=int, default=0)
    plan.add_argument("--attempts", type=int, default=6, help="Bounded verification query retries")
    plan.add_argument("--monitor-poll-seconds", type=float, default=30.0)
    plan.add_argument(
        "--contact-loss-seconds",
        type=float,
        default=600.0,
        help="Unreachable-destination window before a running task becomes uncertain",
    )
    for command in ("run", "copy"):
        run = sub.add_parser(command)
        run.add_argument("run_id")
        run.add_argument("--plan-hash", required=True)
        run.add_argument("--ack-backup", action="store_true")
        run.add_argument("--ack-active", default="", help="Exact active-bucket acknowledgment")
    for command in ("status", "verify", "resume", "stop", "recover-services"):
        s = sub.add_parser(command)
        s.add_argument("run_id")
    report = sub.add_parser("report")
    report.add_argument("run_id")
    report.add_argument("--format", choices=("json", "markdown"), default="markdown")
    fixup = sub.add_parser("fixup")
    fixup.add_argument("run_id")
    fixup.add_argument("--consent-node", action="append", default=[])
    fixup.add_argument("--ack-backup", action="store_true")
    fixup.add_argument("--plan-hash", required=True)
    return p


def exit_code(run):
    if run["state"] == State.RECOVERY_REQUIRED:
        return 8
    outcome = run["verification"]["outcome"]
    if outcome == Verification.DISCREPANCY:
        return 5
    if outcome == Verification.INCOMPLETE:
        return 6
    return {
        State.BLOCKED: 3,
        State.STOPPED: 4,
        State.PARTIAL: 5,
        State.FAILED: 7,
        State.MONITOR_DETACHED: 9,
    }.get(run["state"], 0)


def main(argv=None):
    args = parser().parse_args(argv)
    journal = None
    previous_signal = None
    try:
        harden_process()
        journal = Journal(args.journal)
        if args.command == "report":
            run = journal.load(args.run_id)
            events = journal.events(args.run_id)
            if args.format == "markdown":
                sys.stdout.write(markdown(run, events))
            else:
                print(json.dumps(report_data(run, events), sort_keys=True))
            return 0
        if args.command == "status":
            run = journal.load(args.run_id)
            print(
                json.dumps(
                    {
                        "observation": "journal_only; live revalidation required before action",
                        "run": run,
                    },
                    sort_keys=True,
                )
            )
            return 0
        require(args.fixture is not None, Code.UNSUPPORTED)
        remote_state = args.remote_state or args.journal.with_suffix(".simulated-state")
        # Loading a fixture state and using it must share the same journal lock. The
        # environment is loaded again inside Engine's lock by the file-backed simulator.
        env = SimulatedEnvironment.from_file(args.fixture, remote_state)
        clock = SimulatedClock()
        engine = Engine(env, journal, clock)
        if args.command in {"run", "copy", "resume", "stop"}:
            previous_signal = signal.signal(signal.SIGINT, lambda *_: engine.request_detach())
        if args.command == "discover":
            result = engine.discover()
        elif args.command == "plan":
            result = engine.plan(
                Window(parse_time(args.since), parse_time(args.until)),
                Policy(
                    args.ceiling_percent,
                    args.reserve_bytes,
                    args.growth_bytes,
                    attempts=args.attempts,
                    monitor_poll_seconds=args.monitor_poll_seconds,
                    contact_loss_seconds=args.contact_loss_seconds,
                ),
                tail_of=args.tail_of,
                create_missing=args.create_missing,
                backup_ack=args.ack_backup,
                fixup_existing=args.fixup_existing,
                partition_hash=args.partition_hash,
            )
        elif args.command in {"run", "copy"}:
            result = engine.run(
                args.run_id,
                plan_hash=args.plan_hash,
                backup_ack=args.ack_backup,
                active_ack=args.ack_active,
            )
        elif args.command == "verify":
            result = engine.verify(args.run_id)
        elif args.command in {"resume", "stop"}:
            if args.command == "stop":
                engine.request_stop()
            result = engine.resume(args.run_id)
        elif args.command == "recover-services":
            result = engine.recover_services(args.run_id)
        else:
            result = engine.fixup(
                args.run_id,
                consent_nodes=tuple(args.consent_node),
                backup_ack=args.ack_backup,
                plan_hash=args.plan_hash,
            )
        print(json.dumps(result, sort_keys=True))
        return exit_code(result) if "state" in result else 0
    except MigrationError as error:
        print(
            json.dumps({"error": error.code.value, "run_id": getattr(error, "run_id", None)}),
            file=sys.stderr,
        )
        return {Code.INVALID_INPUT: 2, Code.RECOVERY: 8, Code.UNCERTAIN: 8}.get(error.code, 3)
    except KeyboardInterrupt:
        print(
            '{"error":"observation_interrupted; remote state must be reconciled"}', file=sys.stderr
        )
        return 8
    except Exception:
        print('{"error":"operation_failed; remote state must be reconciled"}', file=sys.stderr)
        return 7
    finally:
        if previous_signal is not None:
            signal.signal(signal.SIGINT, previous_signal)
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    sys.exit(main())
