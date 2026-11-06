# CLI operator guide

This delivery runs deterministic fixtures only. It does not connect the CLI to
real appliances. The REST/SSH transport seams are separately tested, but typed
live discovery, version-specific count queries, ingest attribution and migration
response decoders still need qualification. No fixture result proves a real
appliance migration safe. Sources are 8.18.x; destinations are 9.0.x or 9.1.1 preview.

## Install and exercise

Python 3.11 or later:

```sh
python -m pip install .
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
buckets are always excluded. The last fully sealed day is reported in
America/Chicago when bounds establish it; otherwise it is unknown.

The preview also shows storage: the selected buckets' total bytes beside each
destination node's own available repository budget. The API assigns buckets to
nodes, so every node must be able to hold the whole selection. `copy` re-reads the
destination and refuses (`insufficient_capacity`, exit 3) when any node falls short;
an over-budget 9.0 destination retires and deletes its own existing history. The
preview warns that the source root password is sent to the destination for the
transfer and that source retention can remove planned buckets before the run.

**No undo: migrated data stays on the destination. Snapshot the destination before
continuing (snapshotting the source too is optional).** `--ack-backup` records the
operator's attestation, not proof that a snapshot exists. Active exclusions require
the exact acknowledgment below.

```sh
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture run RUN_UUID \
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
On 9.1.1, extracted fields are compared too. Creating configuration is the next
delivery, not a hidden side effect of planning.

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
  but not received (for example API-skipped); those are reported, not verified.
  The fixture adapter supplies attribution; unverifiable attribution must remain
  incomplete on a future live adapter.
- Daily count equality is count evidence, not proof of identical message content,
  partition routing or field extraction. A report preserves active exclusions and
  does not authorize retiring source data that was not copied.

No tail rerun bypass exists. Newly sealed data needs a non-overlapping window that
selects no previously sent bucket. Some active data may never seal after ingest
moves away; keep that source data available under the operator's retention plan.

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

The optional 9.0 fixup implementation is retained in `_set_aside_fixup.py`, marked
set aside and not instantiable or exposed in CLI help/commands. Reintroduction
requires separate review, typed source/destination capability boundaries, positive
adapter qualification, trusted helper integrity, independent restart safeguards
and actual appliance tests. The current migration path never stops services.

Run `make check` and, after installing the package, `make smoke` for the same
fixture checks used by CI. GUI, native packaging, live appliance qualification and
configuration-rule migration are separate deliveries.
