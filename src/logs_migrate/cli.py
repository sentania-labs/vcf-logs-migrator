"""Automation interface to the migration API, without a local task controller."""

import argparse
import json
import logging
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
from .reporting import markdown, report_data
from .security import ENVIRONMENT, Credentials, harden_process
from .trust import TrustRequired, TrustStore, shared_ssh_keys

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


class LicensesAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, **kwargs):
        super().__init__(option_strings, dest, nargs=0, default=default, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        from .licenses import text

        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text())
        parser.exit()


def parser():
    p = Parser(prog="logs-migrate", description="Migration API wrapper for 8.18 to 9.x")
    p.add_argument(
        "--debug-log",
        nargs="?",
        const="",
        metavar="=PATH",
        help="Write detailed troubleshooting log; choose the file with --debug-log=PATH",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Echo troubleshooting log to stderr"
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--licenses", action=LicensesAction, help="Show third-party license notices")
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
        help=(
            "Probe, enroll and print unknown TLS certificates and SSH host keys, then exit "
            "without sending any credential; verify the fingerprints and invoke again"
        ),
    )
    live.add_argument(
        "--expect-fingerprint",
        action="append",
        default=[],
        metavar="SHA256",
        help=(
            "A verified TLS or SSH fingerprint (hex, colons optional); an unknown identity "
            "that presents it is enrolled and the command proceeds. Repeatable."
        ),
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
    status = sub.add_parser("status", help="Read the recorded API task and per-node progress")
    status.add_argument(
        "run_id", nargs="?", type=uuid_arg, help="Run to inspect (default: latest plan)"
    )
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
    copy.add_argument("--ack-backup", action="store_true", help=argparse.SUPPRESS)
    copy.add_argument(
        "--ack-active", default="", help="Exact acknowledgment that latest data is skipped"
    )
    resume = sub.add_parser("resume", help="Resume a recorded transfer for the same dates")
    resume.add_argument("run_id", type=uuid_arg)
    resume.add_argument(
        "--preview", action="store_true", help="Preview the remainder without starting"
    )
    resume.add_argument("--json", action="store_true", help="Print the preview as JSON")
    resume.add_argument(
        "--ack-active", default="", help="Acknowledgment that latest data is skipped"
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
    Code.UNCERTAIN_START: 8,
    Code.CONNECTION: 7,
    Code.AUTH: 7,
    Code.SCHEMA: 7,
    Code.STORAGE: 7,
    Code.BUSY: 7,
}


class EnrolledOnly(Exception):
    """--trust-new enrolled identities; this run stops before any credential is sent."""

    def __init__(self, enrolled):
        super().__init__("enrolled")
        self.enrolled = enrolled


def _normalise(fingerprint: str) -> str:
    return fingerprint.strip().lower().replace(":", "")


def _accept(identity, args) -> str:
    """`verified` (fingerprint given), `enroll` (--trust-new, run stops), `prompt` (a human
    confirmed on a terminal) or `refuse`."""
    print("Unknown appliance identity: " + identity.display, file=sys.stderr)
    if identity.fingerprint in {_normalise(f) for f in args.expect_fingerprint}:
        print("Matches --expect-fingerprint; enrolled.", file=sys.stderr)
        return "verified"
    if args.trust_new:
        print("Enrolled (--trust-new); no credential is sent on this run.", file=sys.stderr)
        return "enroll"
    if not sys.stdin.isatty():
        return "refuse"
    answer = input("Trust this identity and remember it? [y/N] ").strip().lower()
    return "prompt" if answer in {"y", "yes"} else "refuse"


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
    env.on_progress = lambda message: print(message, file=sys.stderr, flush=True)
    for _ in range(4):
        try:
            return env.open()
        except TrustRequired as pending:
            existing = [
                {"kind": kind, "name": name, "fingerprint": fingerprint}
                for kind, name, fingerprint in trust.entries()
            ]
            pending_names = {i.name for i in pending.identities}
            shared = [
                (name, first)
                for name, first in shared_ssh_keys(existing + pending.identities)
                if name in pending_names
            ]
            for name, first in shared:
                print(f"SSH host {name} presents the same key as {first}.", file=sys.stderr)
            if shared:
                print(
                    "Appliances cloned from one image commonly share a host key; confirm "
                    "that is expected before trusting these hosts.",
                    file=sys.stderr,
                )
            decisions = {i.name: _accept(i, args) for i in pending.identities}
            refused = [i for i in pending.identities if decisions[i.name] == "refuse"]
            if refused:
                print(
                    json.dumps(
                        {
                            "error": Code.AUTH.value,
                            "untrusted": [i.display for i in refused],
                            "hint": (
                                "Rerun with --trust-new to enroll these without sending "
                                "credentials, then verify the printed fingerprints and run "
                                "again; or pass --expect-fingerprint SHA256 for each verified "
                                "identity; or answer the prompt on a terminal."
                            ),
                        }
                    ),
                    file=sys.stderr,
                )
                raise MigrationError(Code.AUTH) from None
            for identity in pending.identities:
                trust.trust(identity, env.key_line(identity))
            if any(d == "enroll" for d in decisions.values()):
                # Trust on first use is split across runs: this run never sends a credential
                # to an identity that was only just seen.
                raise EnrolledOnly([i.display for i in pending.identities]) from None
        except MigrationError as error:
            stage = env.notes.get("stage")
            if stage:
                error.stage = stage
            raise
    raise MigrationError(Code.AUTH)


def _diagnostic_args(argv):
    """Accept diagnostic switches on either side of a command."""
    args = list(sys.argv[1:] if argv is None else argv)
    options, remaining = [], []
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"-v", "--verbose"} or value.startswith("--debug-log="):
            options.append(value)
        elif value == "--debug-log":
            options.append("--debug-log=")
        else:
            remaining.append(value)
        index += 1
    return options + remaining


def main(argv=None):
    args = parser().parse_args(_diagnostic_args(argv))
    journal = None
    try:
        harden_process()
        if args.debug_log is not None or args.verbose:
            from .diagnostics import configure

            configure(Path(args.debug_log) if args.debug_log else None, verbose=args.verbose)
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
        if args.command in {"plan", "copy", "resume", "verify", "report"}:
            require(args.journal is not None, Code.INVALID_INPUT)
            journal = Journal(args.journal)
        elif (
            args.command in {"status", "stop", "selftest", "discover", "partitions"}
            and args.journal is not None
        ):
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
            try:
                env = open_live(args)
            except EnrolledOnly as enrolled:
                print(
                    json.dumps(
                        {
                            "enrolled": enrolled.enrolled,
                            "credentials_sent": False,
                            "next": (
                                "Verify each fingerprint against the appliance, then run the "
                                "same command again without --trust-new."
                            ),
                        }
                    )
                )
                return 0
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
            result = engine.run(args.run_id, active_ack=args.ack_active)
        elif args.command == "resume":
            preview = engine.resume(args.run_id)
            result = (
                preview
                if args.preview
                else {
                    "preview": preview,
                    "start": engine.run(preview["id"], active_ack=args.ack_active),
                }
            )
        elif args.command == "status":
            result = engine.status(args.run_id)
        elif args.command == "stop":
            result = engine.stop(args.task_id)
        else:
            result = engine.verify(args.run_id)
        if args.command == "resume" and args.preview and not args.json:
            from .gui.viewmodel import format_bytes, format_eta

            plan = result["plan"]
            received, remaining = plan.get("already_received", []), plan["selected"]
            estimate = plan.get("estimate") or {}

            def amount(buckets):
                count = len(buckets)
                size = sum(bucket["size"] for bucket in buckets)
                return f"{count} bucket{'s' if count != 1 else ''} · {format_bytes(size)}"

            print(f"Already received: {amount(received)}")
            print(f"Still to transfer: {amount(remaining)}")
            print(
                f"Estimated time: {format_eta(estimate.get('low_seconds'))} to "
                f"{format_eta(estimate.get('high_seconds'))}."
            )
            if plan.get("re_requested_unconfirmed"):
                print(
                    "The destination could not be inspected. Remaining buckets will be "
                    "requested again; some may already be there."
                )
            if plan.get("node_count_unchecked"):
                print(
                    "The destination's node count could not be checked. If nodes were added "
                    "or removed since the first run, history may be copied a second time. "
                    "Confirm it is unchanged before continuing."
                )
            print(f"Preview run ID: {result['id']} (use with copy to start)")
        else:
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
        logging.getLogger("logs_migrate.cli").exception("Command failed")
        print(
            json.dumps(
                {
                    "error": error.code.value,
                    "run_id": getattr(args, "run_id", None),
                    **({"message": error.message} if hasattr(error, "message") else {}),
                    **({"stage": error.stage} if hasattr(error, "stage") else {}),
                }
            ),
            file=sys.stderr,
        )
        return EXIT_CODES.get(error.code, 3)
    except KeyboardInterrupt:
        print('{"error":"observation_interrupted; query API status"}', file=sys.stderr)
        return 8
    except Exception:
        logging.getLogger("logs_migrate.cli").exception("Command failed")
        print('{"error":"operation_failed; query API status"}', file=sys.stderr)
        return 7
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    sys.exit(main())
