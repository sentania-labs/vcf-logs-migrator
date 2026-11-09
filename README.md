# VCF Logs Migrator

A shared Python engine and automation CLI for historical log migration from
**Aria Operations for Logs 8.18.x** to **VCF Operations for Logs 9.0.x** and
**9.1.1 preview**, including history older than 90 days.

The engine, CLI and desktop GUI run against real appliances (Logs REST on 9543,
root SSH to the 8.18 members, the 9.0 data-migrations API, the 9.1.1 Ops token
exchange) and against a built-in simulator for CI and demos. Start with
[Testing against a lab](docs/testing-against-a-lab.md). Standalone downloads for Windows, macOS and
Linux need no Python install ([Download](#download)). This project is not supported by Broadcom.

The mover is the destination's `/api/v2/data-migrations` API. The wrapper previews
sealed buckets by date, starts the transfer once, reads status directly from the
API, checks partitions, verifies per-day counts, and produces Markdown/JSON reports.
A small local record keeps sent buckets, baseline counts and audit evidence. There
is no local task state machine. It never stops the source, deletes buckets,
overwrites destination data, or falls back to manual copying.

## Download

Each platform has one download with both the desktop window and CLI:

| Platform | Download | Open the window | Run the CLI |
|---|---|---|---|
| Windows x64 | `vcf-logs-migrator-vX.Y.Z-windows-x64.exe` | Double-click the executable | `vcf-logs-migrator-vX.Y.Z-windows-x64.exe <command>` |
| macOS Apple silicon / Intel | `vcf-logs-migrator-vX.Y.Z-macos-arm64.dmg` / `-macos-x86_64.dmg` | Open the disk image and drag the app to Applications | `/Applications/VCF Logs Migrator.app/Contents/MacOS/logs-migrate <command>` |
| Linux x86_64, glibc 2.35+ | `vcf-logs-migrator-vX.Y.Z-linux-x86_64` | Make executable, then run with `gui` | Run the executable with a command |

[GitHub Releases](https://github.com/sentania-labs/vcf-logs-migrator/releases) also
provide `SHA256SUMS` and `THIRD-PARTY-NOTICES.txt`. Notices travel with the program:
use `--licenses` or About → Licenses. On Linux the window needs a desktop session
and the usual Qt libraries (libEGL/libGL, fontconfig, libxkbcommon-x11,
libxcb-cursor0).

### How the downloads are built and signed

`packaging/build.py` uses pinned PyInstaller tools and Python 3.12.10. The binary
builds run natively on GitHub-hosted Linux, Windows and macOS runners; smoke tests
exercise the CLI, simulator, a real SSH probe against a reserved localhost port and
GUI from the finished download. Every download bundles Paramiko (pinned in
`packaging/requirements.txt`). PyInstaller's analysis
provides the bundled distribution inventory used to generate the embedded notices.

Windows is Authenticode signed through Azure Artifact Signing. macOS uses the org's
Developer ID signing and notarization actions; the disk image is also signed,
notarized and stapled. Linux is covered by `SHA256SUMS`. Signing credentials remain
in organization secrets. Pull requests build and smoke unsigned downloads.

To build locally, install `python -m pip install -r packaging/requirements.txt '.[ssh]'`,
then run `python packaging/build.py build <platform>` and `python packaging/build.py
package <platform>`. Run `python packaging/smoke.py dist/<download>` to smoke it.

## Try it

Against a lab (read-only until `copy`):

```sh
python -m pip install '.[ssh]'
export LOGS_SOURCE=aria-logs-1.lab.example LOGS_DESTINATION=vcf-logs.lab.example
export LOGS_SOURCE_API_PASSWORD=... LOGS_SOURCE_SSH_PASSWORD=... LOGS_DESTINATION_API_PASSWORD=...
logs-migrate --trust-new selftest   # enrolls the identities it sees and exits; no credential sent
logs-migrate selftest               # read-only checks against the pinned identities
```

Against the simulator:

```sh
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture discover
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture plan \
  --since 2026-03-01T00:00:00Z --until 2026-04-01T00:00:00Z
```

Use the returned run ID with `copy`, then query `status` and run
`verify`. Status exposes API task and per-node observations, elapsed time, relative
transfer rate and ETA when the API supplies enough data. `stop` explicitly cancels
the observed API task by its task ID. The [CLI operator guide](docs/operator-cli.md)
has the complete workflow, attestations, exit codes and qualification limits.

API completion is not verified success: it can hide skipped data. Verification
requires complete, agreeing counts and accounts for pre-existing data and new
ingest. Active exclusions remain visible. Buckets already received (or in flight)
are refused, including on 9.1.1 where no destination bucket store exists.
Verification is cumulative per destination, so a later transfer never invalidates
an earlier success.

Configuration migration supports forwarding, partition, masking and filter rules
through `config compare` and `config apply --select TYPE:NAME`. Only explicitly
selected rules are created or updated; nothing is deleted, and source rules stay
untouched. New partitions report a destination restart for normal maintenance.
Existing fixup code is retained privately and **set aside**, unavailable from the
engine/CLI, pending migration qualification.

Appliance identities (TLS certificates, SSH host keys) are trusted on first use with
an explicit prompt or dialog, or enrolled by `--trust-new` on a run that sends no
credential, or matched against `--expect-fingerprint`; they are remembered under the
user's config directory.
Credentials come from the GUI, environment variables, a private file or a prompt;
never from command arguments, logs or reports.

### Troubleshooting logs

Detailed logging is off by default. Run `logs-migrate --debug-log status` to write a
credential-redacted log under the client configuration directory (it keeps the 10 most
recent logs, each capped at 5 MiB plus 2 backups), or
`--debug-log=/path/to/log` to choose a file; add `-v` to echo it to stderr. In the
desktop app, tick **Enable detailed troubleshooting log** in Settings, retry, then use
**Show details**. Logs contain appliance addresses, so review before sharing. Details:
[operator troubleshooting](docs/operator-cli.md#troubleshooting-logs).

## Desktop GUI

`logs-migrate gui` opens a PySide6 window over the same engine: Overview, Connections,
Configuration, Discovery & Plan, Migration, Verification, Reports and Settings, styled
with the shared [Lattice](https://github.com/sentania-labs/lattice) tokens in light and
dark. The GUI holds no migration logic; every number it shows is an engine or API
observation, and estimates read Unavailable when the API gives no counters. Engine
calls run on a worker thread so the window stays responsive. Settings (theme, display
timezone, status refresh, verification budget, report folder, simulator fixture,
journal file and troubleshooting log) are editable in the window and stored in an INI
file under the user's config directory; credentials stay in session memory only.

```sh
python -m pip install '.[gui]'
logs-migrate gui --fixture tests/fixtures/sealed-populated.fixture \
  --journal /private/migration/run.sqlite
```

Leave the fixture path empty in Settings to connect to real appliances from the
Connections page; set one to drive the simulator. `python tests/gui_screenshots.py
--output DIR` renders every workspace state offscreen for review
(`QT_QPA_PLATFORM=offscreen`).

## Development

Python 3.11+; the core and CLI do not import Qt. `gui` installs the optional PySide6
dependency for the desktop window. `ssh` installs the optional Paramiko adapter.

```sh
python -m pip install '.[dev,gui,ssh]'
make check
make smoke
```

GUI tests use Qt's offscreen platform and skip when PySide6 is absent. Wire-level
SSH tests skip when Paramiko is absent; CI installs both extras and runs them.

`make check` runs Ruff and pytest. Tests use synthetic fixtures, recorded-transport
contracts (including the live adapter's listing, status and token-exchange codecs),
and simulated failures. `make smoke` drives the installed `logs-migrate`
console script from a temporary directory through both data fixtures and the
configuration workflow. Linux CI installs the package and runs these same targets
on pull requests and pushes to main. These checks do not contact appliances; the
lab guide's `selftest` is the check that does. Fixture helper behavior does not
prove that updating an already-registered vendor bucket is safe; fixup is not an
enabled operation.

## Research and historical evidence

| Document | Evidence |
|---|---|
| [Official API validation](docs/validation-2026-11-06-official-api.md) | 9.0/9.1.1 requests, placement, dedupe, disk skips, active exclusions, partition behavior |
| [Partitions and multi-node validation](docs/validation-2026-11-06-partitions-multinode.md) | Partition registration, creation time, three-node source and target experiments |
| [Validation run](docs/validation-2026-11-06.md) | Date selection, populated targets, retention and interrupted manual transfers |
| [Lab runs](docs/lab-runs.md) | Historical experiments, including the manual 2,429,018-event run |
| [Research guide](docs/research-guide.md) | Original investigation and experimental procedures |
| [Config migration](docs/config-migration.md) | Config API findings; broader config migration is outside this delivery |
| [Specification](docs/spec.md) | Product design and historical appendix; the operator guide describes shipped commands |
| [Testing against a lab](docs/testing-against-a-lab.md) | Step-by-step for real hosts: setup, exact commands, what correct output looks like |

The files under `scripts/` are historical research prototypes, not this CLI's
execution path. Some contain source stops, insecure transport or deletion flows.
Do not use them as production operator instructions. The engine invokes none of
them, including the destructive stock bucket migration command.

MIT licensed. No production-readiness claim is made from fixture tests or the
recorded lab results alone; a lab run of this tool against real appliances is the
evidence that counts.
