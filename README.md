# VCF Logs Migrator

A shared Python engine and automation CLI for historical log migration from
**Aria Operations for Logs 8.18.x** to **VCF Operations for Logs 9.0.x** and
**9.1.1 preview**, including history older than 90 days.

**Current delivery: engine, CLI and desktop GUI with deterministic fixtures. Real
appliance mutation is disabled pending safety and lab qualification.** Standalone
downloads for Windows, macOS and Linux need no Python install. This project is not
supported by Broadcom.

The mover is the destination's `/api/v2/data-migrations` API. The wrapper previews
sealed buckets by date, starts the transfer once, reads status directly from the
API, checks partitions, verifies per-day counts, and produces Markdown/JSON reports.
A small local record keeps sent buckets, baseline counts and audit evidence. There
is no local task state machine. It never stops the source, deletes buckets,
overwrites destination data, or falls back to manual copying.

## Download

Each platform gets one archive holding a single program that is both the desktop
window and the `logs-migrate` CLI, plus a quickstart (`README.txt`), the simulator
fixtures under `examples/`, and license notices:

| Platform | Archive | Open the window | Run the CLI |
|---|---|---|---|
| Windows x64 | `vcf-logs-migrator-vX.Y.Z-windows-x64.zip` | double-click `logs-migrate.exe` | `logs-migrate.exe <command>` |
| macOS Apple silicon / Intel | `vcf-logs-migrator-vX.Y.Z-macos-arm64.zip` / `-macos-x86_64.zip` | double-click `VCF Logs Migrator.app` | `"VCF Logs Migrator.app/Contents/MacOS/logs-migrate" <command>` |
| Linux x86_64, glibc 2.35+ | `vcf-logs-migrator-vX.Y.Z-linux-x86_64.tar.gz` | `./logs-migrate gui` | `./logs-migrate <command>` |

Tagged versions are attached to [GitHub Releases](https://github.com/sentania-labs/vcf-logs-migrator/releases)
with `SHA256SUMS` and build provenance attestations. Every push to `main` also
leaves signed builds as artifacts of its *Binaries* workflow run. Arguments always
mean the CLI, so an agent drives the same program a person double-clicks; with no
arguments, only an Explorer or Finder launch opens the window. On Linux the window
needs a desktop session with the usual Qt libraries (libEGL/libGL, fontconfig,
libxkbcommon-x11, libxcb-cursor0); the CLI needs none of them.

### How the downloads are built and signed

`packaging/build.py` freezes `packaging/launcher.py` with PyInstaller (pinned in
`packaging/requirements.txt`): a one-file executable on Windows and Linux, a one-dir
app bundle on macOS. `.github/workflows/binaries.yml` builds natively on GitHub-hosted
Ubuntu 22.04, Windows, macOS arm64 and macOS Intel runners, then
`packaging/smoke.py` extracts each final archive into an empty directory and runs it
with no Python on its path: `--version`, `--help`, argument refusal, both simulator
migrations end to end, the configuration workflow, and the `gui` path rendered once
offscreen and once natively (an Explorer-style launch with the console hidden on
Windows, `open` and a PATH symlink on macOS, X11 under Xvfb on Linux). The Linux
build is smoked again on Ubuntu 24.04. Launch screenshots are kept as workflow
artifacts.

Signing uses the org's shared actions in `sentania-labs/.github`, pinned by commit,
with credentials held as sentania-labs organization secrets (none are stored in
this repository):

- **macOS**: `macos-sign` signs the app's nested code and then the bundle with the
  Developer ID Application identity, hardened runtime and a secure timestamp
  (`MACOS_CERT_P12`, `MACOS_CERT_PASSWORD`, `MACOS_TEAM_ID`); `macos-notarize`
  submits it to Apple's notary service, staples the ticket and requires Gatekeeper
  to accept it (`NOTARY_ISSUER_ID`, `NOTARY_KEY_ID`, `NOTARY_KEY_P8`).
- **Windows**: `windows-sign` Authenticode-signs `logs-migrate.exe` through Azure
  Artifact Signing with an RFC 3161 timestamp (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
  `AZURE_CLIENT_SECRET`, `ARTIFACT_SIGNING_ENDPOINT`, `ARTIFACT_SIGNING_ACCOUNT`,
  `ARTIFACT_SIGNING_PROFILE`).

Pushes to `main`, manual runs and release tags sign, and the smoke then verifies the
signatures on the extracted archive (`codesign --verify --deep --strict`,
`stapler validate` and `spctl --assess` on macOS, `Get-AuthenticodeSignature` on
Windows). Pull requests build and smoke unsigned, because pull requests from forks
cannot read secrets. Linux archives are not signed; they are covered by
`SHA256SUMS` and the attestation.

`.github/workflows/release.yml` runs on a `vX.Y.Z` tag. It refuses a malformed tag, a
tag not reachable from `main`, a tag that differs from the program's version, or a
missing signing secret, runs the test suite, builds and smoke-tests every platform
through `binaries.yml` with signing required, and only then publishes all four
archives with `SHA256SUMS` on the release. A published release is never modified by
a rerun. To build locally: `python -m pip install -r packaging/requirements.txt .`,
then `python packaging/build.py build <platform>`, `python packaging/build.py package
<platform>` and `python packaging/smoke.py dist/<archive>` (unsigned).

## Try the fixture CLI

```sh
python -m pip install .
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
Configuration execution remains fixture-only, with the same live release gate.
Existing fixup code is retained privately and **set aside**, unavailable from the
engine/CLI, pending migration qualification.

## Desktop GUI

`logs-migrate gui` opens a PySide6 window over the same engine: Overview, Connections,
Configuration, Discovery & Plan, Migration, Verification, Reports and Settings, styled
with the shared [Lattice](https://github.com/sentania-labs/lattice) tokens in light and
dark. The GUI holds no migration logic; every number it shows is an engine or API
observation, and estimates read Unavailable when the API gives no counters. Engine
calls run on a worker thread so the window stays responsive. Settings (theme, display
timezone, status refresh, verification budget, report folder, simulator fixture and
journal file) are editable in the window and stored in an INI file under the user's
config directory; credentials stay in session memory only.

```sh
python -m pip install '.[gui]'
logs-migrate gui --fixture tests/fixtures/sealed-populated.fixture \
  --journal /private/migration/run.sqlite
```

This build connects to the deterministic simulator only; live appliance connections
remain behind the same release gate as the CLI. `python tests/gui_screenshots.py
--output DIR` renders every workspace state offscreen for review
(`QT_QPA_PLATFORM=offscreen`).

## Development

Python 3.11+; the core and CLI do not import Qt. `gui` installs the optional PySide6
dependency for the desktop window. `ssh` installs the optional Paramiko adapter.

```sh
python -m pip install '.[dev,gui]'
make check
make smoke
```

GUI tests use Qt's offscreen platform and skip when PySide6 is absent.

`make check` runs Ruff and pytest. Tests use synthetic fixtures, recorded-transport
contracts, and simulated failures. `make smoke` drives the installed `logs-migrate`
console script from a temporary directory through both data fixtures and the
configuration workflow. Linux CI installs the package and runs these same targets
on pull requests and pushes to main. These checks do not contact appliances. In particular,
fixture helper behavior does not prove that updating an already-registered vendor
bucket is safe; fixup is not an enabled operation.

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

The files under `scripts/` are historical research prototypes, not this CLI's
execution path. Some contain source stops, insecure transport or deletion flows.
Do not use them as production operator instructions. The engine invokes none of
them, including the destructive stock bucket migration command.

MIT licensed. No production-readiness claim is made from fixture tests or the
recorded lab results alone.
