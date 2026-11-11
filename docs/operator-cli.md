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
buckets are always excluded. A sealed bucket the appliance lists with no time range
(8.18 does this after a restart) cannot be shown to touch the window, so it is never
selected: the plan lists it by node and id as excluded, and the operator decides.
`last_fully_sealed_day` is the last day covered only by sealed buckets right now,
not a guarantee: it is the day before the first active bucket's proven start
(America/Chicago). An 8.18 listing gives dates for active buckets; an empty active
bucket (no events, sentinel dates) is ignored. When an active bucket has no usable
dates, it is the day before the oldest node's newest sealed bucket end, and unknown
when a node with an active bucket has no sealed bucket to go by. Active buckets can
still receive older-dated data (backfill or late agents); the preview warns about
that and lists `open_bucket_days`, the requested days shared with an open bucket whose
dates are known. Such days show as differences at verify.

The preview estimates each destination node's share as the selected bytes divided by
the node count, with a 25% margin for uneven bucket sizes, capped at the total
selection. `copy` refreshes storage and refuses (`insufficient_capacity`, exit 3)
if the estimate exceeds a node's repository budget or would reach its 95% disk
limit. This is an estimate, not a guarantee: individual bucket sizes and the API's
assignment can make a node's actual share larger. An over-budget 9.0 destination
can retire its own existing history. Missing budget or disk figures are shown as
unknown and warned about rather than refused. The
preview warns that the source root password is sent to the destination for the
transfer and that source retention can remove planned buckets before the run.

The preview reports `source_ingest` as growth in the active bucket directories over
a short sampled interval. Growth indicates the source is still receiving data; no
growth is only an observation for that interval, not a 24-hour event count. Open
buckets can contain late or backfilled data. `partial_days` lists the requested days
at or after the sealed frontier and days shared with unselected buckets.

If the plan excludes open groups, use the exact acknowledgment below.

```sh
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture copy RUN_UUID \
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
are updated by destination ID after matching their name (partitions are updated at
`PUT /api/v1/partitioning` with the ID in the body; the per-ID route is a 404 on 9.0); creates of partitions,
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
rules are still attempted. An HTTP error answer (4xx) means the write was refused and
is `failed`. A 2xx answer whose body is not JSON (9.0 answers a partition update with
plain text) counts as accepted and is confirmed by the readback. A lost write response is
uncertain and never retried
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

## Status belongs to the run

`status` reads the destination API and displays a task only when its ID matches
this run's journal record. Supply the same `--journal` used to start the run.
It defaults to the latest plan; pass a run UUID to select another run. A missing
or unidentifiable task shows idle rather than another run's status.

Without a journal (or with a journal that holds no such run), `status` still succeeds
but says so: `run` is `null`, `no_run` gives the reason and a hint (pass `--journal`),
and `destination_tasks` lists what the destination currently reports (task id, state,
window, progress), labelled as destination tasks not tied to any run of this tool.

```sh
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture status RUN_ID
```

The result includes per-node progress and scheduled and total bucket counts by
source node when supplied by the API. Rate and ETA are estimates from available
bytes or node progress and elapsed time; missing evidence leaves them unavailable.
An API completion does not itself claim verified success.

The journal stores plans, the sent-bucket ledger, counts and verification evidence.
The API remains the source of task state. Status records finished transfers in the
ledger only after matching the task to the run.

An interrupted client or lost POST response does not imply a stopped task. Query
status. When the start's response was lost (never acknowledged), status looks in the
appliance's task list for a single task with exactly this run's requested window,
created no earlier than five minutes before the start attempt (a margin for clock
difference between this machine and the appliance), and not belonging to another run.
A match is shown with its progress and remembered for this run, and its ID can be
used to stop it. If none is found, status reports `idle` with `start_outcome: unknown`,
`hold_until` (the start attempt plus 15 minutes) and a `start_hint`: the start's
outcome is unknown and no task has appeared yet; keep checking status; if none
appears, the window stays held until `hold_until`, after which you plan and copy
again. The Migration page shows the same wording with the time the window frees up.
A start that was acknowledged without a task ID is looked up the same way, but
reports plain `idle` while no task matches. Explicit cancellation uses the
currently observed **API task ID**:

```sh
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture stop API_TASK_ID
```

The wrapper checks that ID before cancelling, then returns the API's observation.
It never automatically cancels a transfer because local inventory changed.

To finish a stopped or failed transfer, keep the original journal and check the
remainder before starting it:

```sh
logs-migrate --journal /private/migration/run.sqlite --fixture tests/fixtures/sealed-populated.fixture \
  resume RUN_ID --preview
logs-migrate --journal /private/migration/run.sqlite --fixture tests/fixtures/sealed-populated.fixture \
  copy NEW_RUN_ID --ack-active "i understand you aren't going to copy the latest data"
```

The preview reports already received buckets and those still to transfer in plain
language. Add `--json` to `resume RUN_ID --preview` for the machine-readable form.
Resume
checks available space again. It also checks the destination node count when the
destination's nodes were observed both in the first plan and now (destination SSH on
9.0). On 9.0 without destination SSH and on 9.1.1 the node count cannot be checked,
so the preview shows a caution and the time estimate says it assumes the node count
listed by the earlier task. A missing source bucket,
unregistered destination directory, changed bucket size, or unresolved receipt
record stops the preview for investigation. With destination SSH on 9.0, the
destination's bucket inventory decides what arrived. Without it, a 9.0 task that
lists the buckets still to do per node is settled from its own report: the run's
selection minus those buckets and minus the ones it reports skipped. Status, verify
and the Migration page say plainly that such a record is taken from the task report
and not confirmed against the destination's bucket store (a disk-limit stop that
drops buckets without listing them cannot be seen this way). If the task lists some
received buckets but leaves others unaccounted for, resume requests those unaccounted
buckets again: the 9.0 destination skips a bucket whose directory is already present.
Without destination SSH the tool cannot check for a directory that is present but
missing from the bucket index, so inspect that condition separately if suspected.
A 9.0 task with neither receipt evidence nor a per-node remaining list stays held,
with the reason shown, until destination SSH is connected. 9.1.1 has no bucket store: it
settles only from explicit per-bucket receipt evidence in the task or journal. A
finished task without it, or one whose received list leaves selected buckets
unaccounted for, stays held (status names how many buckets are unaccounted for)
rather than guessing. After the remaining transfer finishes, run `verify NEW_RUN_ID`.
`resume RUN_ID --ack-active ...` previews and starts in one command; use `--preview`
when you need to review the remainder first. When the node count could not be
checked, the one-command form does not start: it prints the caution and exits with
code 2 unless you also pass `--ack-node-count-unchanged`, confirming that no nodes
were added or removed since the first run.

A start the appliance refused (any 4xx except 408, or 500 or 501) was never accepted,
so nothing is held and you can plan and copy again. A start with no answer (timeout,
lost connection) or a gateway-style answer (408, 502, 503, 504) may have been accepted:
it exits 8 and keeps its selection blocked; the next `plan` releases it when
the appliance shows no task for it, and settles it as usual when a task matches.
A start the appliance accepted (2xx) stays held until a matching task is observed,
even while its status still shows nothing.

## Data protection and verification

- Source operations are read-only. No source stop, bucket delete, overwrite or
  manual-copy fallback exists in the engine path.
- A new transfer may not select a source bucket that an in-flight task holds. Buckets
  the destination already received are shown separately from the remainder. The API
  receives the original dates and selects whole overlapping buckets; it skips a bucket
  only on the node to which it assigns that bucket. If placement cannot be checked,
  inspect it before starting because another copy could be created. Once the API reports
  a task finished, only the buckets it transferred stay recorded, so a stopped, cancelled
  or failed task's remainder is finished with `resume RUN_ID` (no hand-picked dates). Preserve
  the journal for each destination; external transfer history must also be known.
  Adjacent windows selecting the same whole bucket are refused too, especially on
  9.1.1 where destination bucket IDs disappear. A run starts at most once.
  A finished transfer started elsewhere (the product UI, say) with no received list
  covered whole buckets in its window. When the destination's bucket inventory was read
  (destination SSH), the exact id check decides and a selected bucket crossing that
  window is only a plan warning; without the inventory the plan is refused with
  `overlapping_transfer`. Either way the message names the earlier task, when it ran,
  its window and the selected groups crossing it.
- A plan is checked against fresh inventory and the planned source and destination
  identities before start. `verify` re-checks the destination identity and reports
  incomplete verification if it changed; source retention after a completed transfer
  is permitted. Existing destination
  copies that are incomplete, missing their index entry, or differ in size or message
  count block the window. The preview lists the affected copies; they must be dealt
  with on the destination before those dates can be copied.
  Unknown history and incompatible partitions can also block transfer.
- To investigate repeated destination entries, the read-only command
  `/usr/lib/loginsight/application/sbin/bucket-index show` can be run on each destination
  node. Share the affected bucket lines with the node names, and compare their IDs,
  counts and dates with the preview.
  The listing alone does not establish whether a bucket on another node would be skipped
  by the destination's placement logic.
- Verification uses complete=true and two agreeing count responses, then confirms
  destination and ingest observations still agree. Missing or unstable evidence
  returns incomplete, never success. Any running or unknown API task also prevents
  a successful verification. A destination copy of a selected group that is incomplete
  or missing its index entry turns a success into a discrepancy, and the result and
  report list those groups.
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
  With destination SSH, verify also re-reads the destination bucket inventory after the
  task and compares each selected group by id and message count with the source. The
  result carries `bucket_check` (groups and events matched, unmatched and unknown
  groups) and `daily_outcome`; the report leads with it and treats the daily counts as
  the search-level second check. A group missing or differing on the destination makes
  the outcome `verified_discrepancy`; a group whose count is unknown makes it
  `incomplete_verification`.
  Without destination SSH there is no inventory to compare: if the group coverage
  reaches days beyond the requested ones (`unmeasured_day_count`), a matching daily
  comparison is `incomplete_verification`, not `verified_success`.
  The simulator supplies per-event attribution. Live counts cannot: against real
  appliances verification is per run over the UTC days touched by the requested
  window (source per-day counts against destination counts minus the baseline
  recorded at start), new ingest is not attributed, and days shared with open or
  unselected buckets are reported as partial without deciding the outcome. When no
  requested day is fully covered (every day is partial) the outcome is
  `incomplete_verification` with reason `no_fully_covered_days`.
- Verification's selected-group expectation is the sum of the groups' recorded message counts.
  For live runs, the destination arrival figure is the change from the pre-transfer baseline for
  requested UTC days, not a count attributed to individual groups. If group coverage extends beyond
  those days, `actual_count` is unavailable; `measured_arrival_count`, `measured_window`, and
  `unmeasured_day_count` describe the narrower observation. The daily table retains the whole-day
  arithmetic and determines the verification verdict. If evidence shows other arrivals during the
  run, the arrival figure is unavailable. Count comparisons do not establish event-content equality.
  Results recorded by an earlier version lack `count_basis`; the GUI and report show no figures for
  them and ask for Verify to be run again.
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
| 8 | API outcome uncertain or client interrupted; query status. Also `uncertain_start_pending`: an earlier start of this window got no answer, so the window is held for 15 minutes and released once the API shows no task for it |

Secrets have no argv flags. Adapter failures are reduced to fixed error codes;
raw responses, credentials and exception strings must never enter reports or
logs. Private journal files use restrictive permissions and writer locking.

SSH failures, count-query timeouts and the refusals of planning, verification and the
self-test carry a `message` (CLI error JSON and the GUI) naming what to act on: the
conflicting or unproven groups with their node, id and UTC dates, or which counts were
incomplete. Connection failures also carry a `stage` naming the host and step in the
CLI error JSON. A timeout names the query, its scope (source or
destination) and its UTC interval; the same text appears as `detail` in a
verification that could not finish its counts. The SSH message comes from a closed
vocabulary. It names the stage that failed: TCP
reachability, handshake, algorithm negotiation, host-key trust, authentication
or remote command. Only recognized library diagnostics and numeric exit statuses
are retained; other exception text and remote stderr are suppressed. Host-key
and authentication failures use `authentication_failed`; the other stages use
`connection_failed`. All exit 7. No SSH algorithm policy was weakened.

`selftest` exits 0 when every read-only check passed and 1 otherwise.

On a large source the per-node SSH bucket listing is the slow step. Nodes are read in
parallel, the CLI prints a per-node progress line and each count query to stderr, and
a listing read within the last five minutes is reused by later steps (kept in the
`--journal` file when one is given). `du` sizes only the buckets a window selects;
`copy` always re-reads the appliances before it starts.

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
folder, plus the troubleshooting log switch below. Settings persist in `settings.ini`
under the user's configuration directory. Credentials entered on the Connections page stay in session memory and never reach the
journal, the settings file or exports.

The window is a presentation layer over the commands above: Discovery & Plan runs
`plan`, Start runs `copy` with the latest-data acknowledgment, Migration
polls `status`, Cancel runs `stop` for the observed task ID, Verification runs `verify`
and Reports writes the `report` Markdown and JSON. Configuration runs `config compare`
and `config apply` with the selected rules.

With an empty fixture path in Settings the window connects to real appliances: the
Connections page takes the addresses and passwords, unknown TLS certificates and SSH
host keys open a trust dialog with their fingerprints, and Run self-test lists the
same read-only checks as `logs-migrate selftest`. Test probes every configured
endpoint's identity first, with the addresses being contacted and elapsed time on the
page, and asks for trust before any credential is sent.

Standalone downloads of this same CLI and window are described in the README.

## Troubleshooting logs

Detailed logging is disabled by default. Add `--debug-log` to any command to create a
timestamped log under the client configuration directory, or use
`--debug-log=/path/to/file` to choose its location. `-v` also writes diagnostics to
stderr and enables a log when used alone. The default location keeps the 10 most recent
logs, each capped at 5 MiB plus 2 rotated files; files you choose with `--debug-log=` are never
pruned. The desktop Settings page has an **Enable detailed troubleshooting log** checkbox that
applies to subsequent operations. After an error, **Show details** appears in the header:
use it to inspect the log, open its folder, or save a copy.

The log includes request paths and status, timing, SSH transport negotiation, commands,
and exception traces. JSON error response bodies retain field names and numeric values;
untrusted text values are redacted because they can contain secrets. Passwords, tokens,
and other known credentials are filtered, but logs still contain appliance addresses and
operational details; review them before sharing.

On POSIX systems the log file is restricted to mode 0600, including an existing
file and files created after rotation. On Windows, log files inherit the folder's
access control list; choose a folder whose permissions restrict access to your account.
