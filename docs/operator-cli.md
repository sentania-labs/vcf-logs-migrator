# CLI operator guide

The CLI drives real appliances (`--source`, `--destination`) or the built-in
simulator (`--fixture`) through the same engine. Sources are 8.18.x; destinations
are 9.0.x or 9.1.1 preview. [Testing against a lab](testing-against-a-lab.md) is the
step-by-step for real hosts, including the `selftest` command, identity enrollment
and where credentials come from. The examples below use the simulator so they run
anywhere; replace `--fixture FILE` with `--source HOST --destination HOST[:PORT]`
for appliances.

## Install and exercise

Python 3.11 or later (`'.[ssh]'` adds Paramiko for appliance SSH):

```sh
python -m pip install '.[ssh]'
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture discover
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture partitions
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture plan \
  --since 2026-03-01T00:00:00Z --until 2026-04-01T00:00:00Z
```

Use the returned run UUID below. Dates must include a timezone. The end is
exclusive; there is no 90-day limit. Selection copies whole sealed buckets, so the
preview includes both the requested interval and actual bucket coverage. Active
buckets are always excluded. The last fully sealed day is the day
before the first active bucket's proven start (America/Chicago). A live 8.18 listing
shows no dates for active buckets, so there it is the day before the newest sealed
bucket's end (America/Chicago); it is unknown only when there is no sealed bucket to go by.

The preview also shows storage: the selected buckets' total bytes beside each
destination node's own available repository budget. The API assigns buckets to
nodes, so every node must be able to hold the whole selection. `copy` re-reads the
destination and refuses (`insufficient_capacity`, exit 3) when any node falls short;
an over-budget 9.0 destination retires and deletes its own existing history. A node
whose budget was not observed (no destination SSH, or 9.1.1) is listed as unknown
and warned about rather than refused; the API's own 95% disk stop then applies and
verification shows what landed. The
preview warns that the source root password is sent to the destination for the
transfer and that source retention can remove planned buckets before the run.

The preview also reports `source_ingest`: how many events the source counted in the
last 24 hours. A non-zero figure is a warning that cutover has not happened; open
buckets keep growing and a later run of the same dates collects the tail once they
seal. `partial_days` lists the covered days that share events with open or
unselected buckets; verification marks them and does not judge the run on them.

**No undo: migrated data stays on the destination. Snapshot the destination before
continuing (snapshotting the source too is optional).** `--ack-backup` records the
operator's attestation, not proof that a snapshot exists. Active exclusions require
the exact acknowledgment below.

```sh
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture copy RUN_UUID \
  --ack-backup \
  --ack-active "i understand you aren't going to copy the latest data"
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture status
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture verify RUN_UUID
logs-migrate --journal /private/migration/run.sqlite report RUN_UUID
logs-migrate --journal /private/migration/run.sqlite report RUN_UUID --format json
```

`copy` sends one API request and returns;
completion and verification are separate observations. Partition differences are
reported before start and prevent transfer until matching configuration exists.
On 9.1.1, extracted fields are compared too. Configuration changes use the
explicit `config` commands below and are never a hidden side effect of planning.

## Configuration rules

`config compare` lists source forwarding, partition, masking and filter rules by
exact name as `missing`, `identical` or `differs`. Destination-only rules appear
separately and are preserved. JSON includes a `selector`, changed field names and
warnings for each source rule; raw definitions and credentials are not printed.

```sh
logs-migrate --fixture tests/fixtures/config.fixture \
  --remote-state /private/migration/config.simulated-state config compare
logs-migrate --fixture tests/fixtures/config.fixture \
  --remote-state /private/migration/config.simulated-state config apply \
  --select 'partitions:Archive' --select 'masking:mask-secret' \
  --select 'filters:drop-debug' --select 'forwarding:archive'
```

Repeat `--select TYPE:NAME` for each rule to create or update, using the exact
selector returned by compare. Names may contain colons or spaces. Selection is
required; there is no implicit apply-all. Each selection authorizes copying that
rule's **current** source definition. Apply re-reads both inventories, rejects
unknown or duplicate selectors before any write, skips unselected rules, and
leaves identical rules unchanged. Compare again before applying if configuration
has changed. There is no stored config plan, journal requirement or extra
acknowledgment. This is additive migration: nothing is deleted on either side,
and the source is never written.

The engine applies partitions, filters, masking, then forwarding. Existing rules
are updated by destination ID after matching their name; creates of partitions,
masking and filters omit the source `id` so the destination assigns its own.
Masking and filters retain their API definitions. Partition retention/archive
settings travel with the rule. A newly created partition needs **one destination
restart before it routes**; `restart_required` and `maintenance` report this.
Arrange that restart through normal maintenance. The tool never restarts services. The report covers
this invocation only; an identical rule on a later compare does not prove that a
restart happened or that routing works. Preserve the apply result for maintenance.

TLS forwarders send `acceptCert:true`, which can add the target certificate to
the destination's trust store. Forwarder batch creates wrap the selected item in
`{"forwarders":[...]}`. 9.0 refuses a forwarder targeting itself; the simulator
checks known destination names, and the appliance must also check aliases during
live qualification. The tool does not retarget forwarders or rewrite filters.

Apply returns `created`, `updated`, `unchanged`, `skipped`, `failed` or `uncertain`
per source rule. Created/updated means the destination readback matches its
configuration; it does not prove log delivery, masking, filtering or partition
routing. A failed rule does not roll back earlier changes, and other selected
rules are still attempted. A lost write response is uncertain and never retried
automatically. Compare again to inspect the destination before selecting a retry.
Exit 8 means at least one uncertain result; exit 7 means a failed rule; exit 0
means the selected changes were observed (even if maintenance remains necessary).

### Engine interface for a GUI

```python
from logs_migrate.engine import Engine

engine = Engine(environment, journal=None)
comparison = engine.config_compare()
result = engine.config_apply(["masking:mask-secret", "filters:drop-debug"])
```

Both return the same JSON-compatible dictionaries as the CLI. For config-only
consumers, `logs_migrate.config.ConfigMigration(environment).compare()` and
`.apply(selection)` expose the same service. The small `ConfigEnvironment`
protocol has `exclusive()`, `config_rules(scope, kind)`, `config_write(kind, rule,
existing)` and `mode`. Reads return lists of API rule objects; `scope` is source
or destination. Writes always target the destination, with `existing=None` for a
create. Apply works against real hosts and writes only to the destination.

`adapters.config.RestConfigEnvironment` uses the `Logs90` source/Bearer client and
`Logs90` or `Logs911` destination auth client, including OpsToken exchange for 9.1.1
preview; `adapters.live.LiveEnvironment` wires it to real hosts. The routes and GET
wrappers follow [config-migration.md](config-migration.md); recorded fixtures
exercise the codec and the lab guide is where a real run is checked.

## Status belongs to the API

`status` queries the destination's current task and per-node details on every
invocation. It needs no run UUID or journal. For the simulator, use `--remote-state`
to name the same simulated appliance without opening a journal:

```sh
logs-migrate --fixture tests/fixtures/sealed-populated.fixture \
  --remote-state /private/migration/run.simulated-state status
```

The result exposes task ID, API status, completed/skipped buckets, bytes and node
observations. Rate is transferred bytes divided by API elapsed seconds; ETA uses
remaining bytes and that average rate. Unknown elapsed time, total size or rate
produces null estimates. These are relative estimates, not promises. The simulator
uses configured poll intervals as synthetic elapsed time, not measured throughput.
An API completion does not itself claim verified success.

The local journal stores plans, the sent-bucket ledger, pre-transfer source and
destination counts, verification evidence and operator audit events. It does not
maintain running/stopped/recovery task states. Reports are historical evidence;
query `status` for current progress. With `--journal`, `status` and `stop` also
record into the ledger which buckets the finished task actually transferred.

An interrupted client or lost POST response does not imply a stopped task. Query
status. Explicit cancellation uses the currently observed **API task ID**:

```sh
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture stop API_TASK_ID
```

The wrapper checks that ID before cancelling, then returns the API's observation.
It never automatically cancels a transfer because local inventory changed. There
is no resume/close/reconcile workflow and no local recovery state to clear.

A start the appliance answered with an HTTP error was never accepted, so nothing is
held and you can plan and copy again. A start with no answer (timeout, lost
connection) exits 8 and keeps its selection blocked; the next `plan` releases it when
the appliance shows no task for it, and settles it as usual when a task matches.
A start the appliance accepted (2xx) stays held until a matching task is observed,
even while its status still shows nothing.

## Data protection and verification

- Source operations are read-only. No source stop, bucket delete, overwrite or
  manual-copy fallback exists in the engine path.
- A new transfer may not select a source bucket the destination already received or
  that an in-flight task holds. Once the API reports a task finished, only the
  buckets it transferred stay recorded, so a stopped, cancelled or failed task's
  remainder can be planned again (use a window that excludes what landed). Preserve
  the journal for each destination; external transfer history must also be known.
  Adjacent windows selecting the same whole bucket are refused too, especially on
  9.1.1 where destination bucket IDs disappear. A run starts at most once.
- A plan is checked against fresh inventory and the planned source and destination
  identities before start. `verify` re-checks the destination identity and reports
  incomplete verification if it changed; source retention after a completed transfer
  is permitted. Existing destination
  bucket collisions, unknown history and incompatible partitions block transfer.
- Verification uses complete=true and two agreeing count responses, then confirms
  destination and ingest observations still agree. Missing or unstable evidence
  returns incomplete, never success. Any running or unknown API task also prevents
  a successful verification.
- `verify RUN_UUID` is cumulative for that run's destination: all buckets the API
  has reported as received across every run, as one result. For every UTC day the
  expected destination count is source count of the received buckets (recorded per
  bucket at copy time) plus the destination's pre-transfer baseline plus new ingest.
  The baseline is the destination's counts before the first transfer; arrivals
  beyond it are ingest, so nothing is counted twice when coverage intersects.
  Re-running after a later transfer recomputes the result and never invalidates an
  earlier success. The result lists covered buckets and days and buckets selected
  but not received (for example API-skipped). A selection with any bucket not
  received is reported with reason `selected_buckets_not_received`. When every
  received bucket reconciles the outcome is `incomplete_verification` (exit 6):
  the counts are evidence, not a success for the requested dates. A count
  difference stays `verified_discrepancy` (exit 5).
  The simulator supplies per-event attribution. Live counts cannot: against real
  appliances verification is per run over the covered days (source per-day counts
  against destination counts minus the baseline recorded at start), new ingest is
  not attributed, and days shared with open or unselected buckets are reported as
  partial without deciding the outcome.
- Daily count equality is count evidence, not proof of identical message content,
  partition routing or field extraction. A report preserves active exclusions and
  does not authorize retiring source data that was not copied.

Some active data may never seal after ingest moves away; keep that source data
available under the operator's retention plan.

## Exits and boundaries

| Exit | Meaning |
|---|---|
| 0 | Requested observation/action completed; inspect its result |
| 2 | Invalid arguments/input |
| 3 | Required acknowledgment, prerequisite or supported capability missing |
| 5 | Complete count observations disagree |
| 6 | Verification incomplete |
| 7 | Operation failed: connection, authentication, unrecognized response, journal unavailable or destination busy |
| 8 | API outcome uncertain or client interrupted; query status |

Secrets have no argv flags. Adapter failures are reduced to fixed error codes;
raw responses, credentials and exception strings must never enter reports or
logs. Private journal files use restrictive permissions and writer locking.

`selftest` exits 0 when every read-only check passed and 1 otherwise.

The optional 9.0 fixup implementation is retained in `_set_aside_fixup.py`, marked
set aside and not instantiable or exposed in CLI help/commands. Reintroduction
requires separate review, typed source/destination capability boundaries, positive
adapter qualification, trusted helper integrity, independent restart safeguards
and actual appliance tests. The current migration path never stops services.

Run `make check` and, after installing the package, `make smoke` for the same
fixture checks used by CI.

## Desktop GUI

`logs-migrate gui [--fixture FILE] [--journal FILE]` opens the desktop window (install
the `gui` extra first). The flags prefill the simulator fixture and private journal; both
are also editable on the Settings page, together with theme, display timezone, status
refresh interval, verification retry budget, count query interval and the report export
folder. Settings persist in `settings.ini` under the user's configuration directory.
Credentials entered on the Connections page stay in session memory and never reach the
journal, the settings file or exports.

The window is a presentation layer over the commands above: Discovery & Plan runs
`plan`, Start runs `copy` with the snapshot and latest-data acknowledgments, Migration
polls `status`, Cancel runs `stop` for the observed task ID, Verification runs `verify`
and Reports writes the `report` Markdown and JSON. Configuration runs `config compare`
and `config apply` with the selected rules.

With an empty fixture path in Settings the window connects to real appliances: the
Connections page takes the addresses and passwords, unknown TLS certificates and SSH
host keys open a trust dialog with their fingerprints, and Run self-test lists the
same read-only checks as `logs-migrate selftest`.

Standalone downloads of this same CLI and window are described in the README.
