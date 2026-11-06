# Engine and CLI: fixture delivery

This first landing supplies the shared Python engine, an automation CLI, a durable
journal, deterministic appliance simulation, read-only SSH transport, and REST
authentication/request adapters. **Real appliance mutation is disabled.** There is
no command-line override. No appliance was contacted to validate this client.

The lab's API experiments establish the migration approach. They do not establish
that this client, its response decoders, cancellation, or fixup are ready for live
use. GUI implementation and standalone installers are separate deliveries.

## Install and exercise

Python 3.11 or newer is required for this source distribution:

```sh
python -m pip install .
logs-migrate --help
```

Create a private workspace outside the checkout. The example fixture contains six
sealed source events, one pre-existing destination event, one concurrent ingest
event, and an excluded active bucket. It is synthetic, not a captured appliance
response or a reproduction of the historical 2,429,018-event lab run.

```sh
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture discover

logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture plan \
  --since 2026-03-01T00:00:00Z --until 2026-04-01T00:00:00Z
```

Read the returned JSON, including `id`, `plan_hash`, requested and whole-bucket
coverage, active exclusions, partition differences, and storage reservations.
Then use those exact values:

```sh
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture run RUN_ID \
  --plan-hash PLAN_HASH --ack-backup \
  --ack-active "i understand you aren't going to copy the latest data"

logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture verify RUN_ID

logs-migrate --journal /private/migration/run.sqlite status RUN_ID
logs-migrate --journal /private/migration/run.sqlite report RUN_ID --format markdown
logs-migrate --journal /private/migration/run.sqlite report RUN_ID --format json
```

`copy` is an alias of `run`. Output is JSON on stdout, except Markdown reports.
Errors are static reason codes on stderr. The report commands write to stdout;
the operator controls where exports are saved. Protect redirected exports with
owner-only file permissions or ACLs. Human report timestamps use America/Chicago.
Query days use UTC. Every input timestamp requires an explicit offset or `Z`.

The simulated remote appliance persists next to the journal in
`run.simulated-state`; `--remote-state PATH` chooses a different location. Keep
both files to resume a fixture across CLI processes. The SQLite journal uses WAL,
FULL synchronous writes, a process lock and POSIX mode 600. Preserve its `-wal`
and `-shm` companions while it is open. Use a private local filesystem directory;
network filesystem lock/durability behavior is not qualified. Windows deployments
also require owner-only directory/file ACL qualification before release.

## Planning and prerequisites

- Any half-open date range is allowed. Whole sealed buckets whose observed event
  bounds intersect it are selected; a bucket's maximum timestamp is inclusive.
  Late backfill can make coverage much wider than the request. Coverage bounds
  are not a claim of continuous daily coverage.
- Active buckets are never transferred or stopped. Unknown/stale active bounds
  produce `last_fully_sealed_day: null`, displayed as unknown. The engine will
  not manufacture a last sealed day from zero or stale metadata.
- Unknown sealed bounds, incomplete manifests, source retention drift, changed
  topology/identity, orphan destination directories, and conflicting bucket IDs
  block execution. Directory and registration evidence must be reconciled by an
  adapter; directory existence alone is never success.
- Source and destination partition name, ID, criteria, ordering, and retention
  must match. The preview shows missing/different definitions. `plan
  --create-missing --ack-backup --partition-hash HASH` explicitly consents to the
  routing/retention definitions shown by `discover`; use its exact
  `partition_preparation_hash`. Changed definitions or identities revoke consent. A
  successful POST is not proof that routing is active. Real partition creation
  and any required rolling restart remain gated on live qualification.
- On 9.1.1, required extracted fields must already exist. This delivery checks
  them; it does not migrate extracted fields or other configuration.
- Capacity uses each node's service repository budget and physical threshold,
  plus `--reserve-bytes`, `--growth-bytes` and an allocation margin. Until the
  vendor's MurmurHash variant/seed/node order is captured and qualified, the
  conservative reservation requires each node to fit the entire batch. This can
  reject a batch that an accurately modeled placement would fit. It does not
  invent a placement algorithm or rely on the server's 95% cutoff.
- Unknown prior transfer history blocks. API task status, including COMPLETED,
  is not integrity evidence. A task can complete with skipped buckets.

Backup acknowledgment is bound to the plan fingerprint and recorded as an
operator attestation. The tool never claims it independently verified snapshots.

## Overlap, tail collection, and recovery

The transfer ledger survives crashes and blocks overlapping transfers, including
selected bucket IDs that straddle adjacent windows. Adapters must include earlier
external/UI transfers and hold exclusive destination ownership. A local journal
alone cannot arbitrate multiple workstations and the official UI.

On **9.0**, `plan --tail-of VERIFIED_RUN_ID` permits a new sealed tail in the same
window only with unchanged source identity, destination node set, matching prior
bucket manifests, and unambiguous history. Previously imported bucket IDs are
accounted for rather than counted as new. The live variant additionally requires
qualified deterministic placement and cross-node reconciliation.

On **9.1.1**, same-window tail reruns are blocked. Choose a window selecting no
previously imported bucket. If overlapping bucket ranges make that impossible,
the tail cannot safely be collected with this API. Merely choosing adjacent dates
does not establish disjoint bucket selection.

`resume RUN_ID` reattaches to an existing task; it never repeats the POST.
It also reconciles an uncertain partition-create request by observing matching
definitions, without repeating creation. After that reconciliation, create a fresh
migration plan. Unresolved preparation blocks other writes to the destination.
An unacknowledged POST remains recovery-required when the returned task cannot
be unambiguously attributed. Raw task correlation fields are not yet captured.
`stop RUN_ID` requests cancellation of this run's known task and waits for a
terminal observation. It cancels even when the source inventory has changed, and a
confirmed cancellation records `PARTIAL` with the revalidation reason rather than
`RECOVERY_REQUIRED`. During `copy`, `resume` and `stop`, Ctrl-C only detaches the
monitor, including while waiting out a lost connection: the run records
`MONITOR_DETACHED` (exit 9), the appliance task keeps running, and `resume RUN_ID`
reattaches by reading the server task. Ctrl-C before the task is started aborts
with `interrupted_before_start` and starts nothing. Other commands keep the default
interrupt behavior. Closing or losing the workstation does not
stop an appliance task. Cancel never means rollback and never removes partial
destination data.

A task that is still running is an expected state, not an uncertain one. The
monitor polls every plan `--monitor-poll-seconds` (default 30) for as long as the
task runs or makes progress. It records `RECOVERY_REQUIRED` only when the task state
is genuinely unknown, or when the destination stays unreachable for the plan's
`--contact-loss-seconds` (default 600).
Task polls, inventory revalidation and bucket attestation all retry through that
same window; a transient connection error never cancels a healthy task.
`resume` and `stop` accept only running, detached or recovery-required runs, or an
authorized run whose start intent was recorded (which becomes recovery-required
because the POST outcome is unknown); any other state is rejected unchanged.
Partition creation (`plan --create-missing`) also refuses while the destination
reports a running task.
A detached run does not block other destination runs by itself; the appliance's
own running task and the overlap ledger do. `verify` refuses a detached run.

`status` reads the durable journal and labels it as journal-only observation.
Use `resume` for live revalidation through the selected adapter. Unknown service
state or an unresolved effect outranks ordinary progress and prevents new writes.

For a conflicting or partial destination bucket:

1. Preserve the directory, catalog, journal, and manifests.
2. Inspect every destination node for the same ID and compare source/destination
   contents. Do not rerun the overlapping API window.
3. Use a clean destination or obtain a separately reviewed manual recovery
   procedure. The engine never deletes or overwrites the conflict.

## Verification

The engine freezes the whole-bucket coverage and selected cohort, obtains two
agreeing explicitly complete observations, and rejects missing completeness.
The default separation is 30 seconds on the injected clock. Fixture clocks
advance without sleeping. Verification query retries are bounded by the plan's
`--attempts`; this is separate from task monitoring.

Plan gates bind the endpoint identities, retention, partitions, extracted fields and
the exact selected buckets (presence, status, size, counts and manifest). Active
bucket rotation is informational. A bucket that seals inside the window after
planning is recorded as tail data and reported as not copied; it blocks only before
the task starts, because the appliance would otherwise select it. On 9.0 plan the
same window with `--tail-of`; on 9.1.1 plan a later run that selects no previously
imported bucket.

Expected destination content is the exact disjoint sealed source cohort plus
the captured pre-existing destination baseline plus independently attributed new
ingest. The simulator verifies daily counts and event-and-field identity
multiplicity. A missing event replaced by a duplicate fails even when totals
agree. Unexplained retirement, missing baseline content, unstable ingest,
ambiguous cohorts, or incomplete query evidence cannot yield verified success.
Reports store only counts and identity digests, never raw log event text.

Legacy count-only REST queries use frozen daily windows to avoid the 100-bin
limit. Those counts alone do not satisfy the engine's multiset/cohort proof.
The 9.1.1 query-response codec and exact attribution adapter still need captured,
sanitized live schemas. The legacy query route is explicitly rejected for 9.1.1.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Command completed; `run` may still say `verification_pending` |
| 2 | Invalid arguments |
| 3 | Blocked prerequisite or unsupported capability |
| 4 | Confirmed stopped task |
| 5 | Partial result or verified discrepancy |
| 6 | Incomplete verification |
| 7 | Operation or verification failure |
| 8 | Recovery required or uncertain remote outcome |
| 9 | Monitor detached; the appliance task is still running |

When a command fails after a run has reached a partial, stopped, detached, failed or
recovery-required state, the exit code follows that persisted state rather than the
error reason, and the stderr error names the run.

`verify` returns zero only on verified success. `status` and `report` return zero
when reading/exporting an unsuccessful run successfully; inspect their contents.
Skipped active data is always reported separately from the verified sealed scope.

## Optional 9.0 fixup

After a fixture migration, use `fixup RUN_ID --plan-hash PLAN_HASH --ack-backup
--consent-node dst-1 --consent-node dst-2 --consent-node dst-3`, with the same
global journal and fixture arguments. For pre-existing equal destination buckets,
first use `plan --fixup-existing`; it creates a standalone fixup plan that cannot
start a migration. A normal migration plan never silently adopts unrelated copies.

The shared credential provider supports the later prompt “Same password on all
N nodes?” or distinct per-node credentials. All credential checks finish before
any stop. Every node must be healthy, only one stops at a time, and the primary
goes last. The engine requires a verified helper, records a run-owned remote
restoration marker, snapshots the catalog, checks the catalog remains intact,
confirms partition/creation-time metadata, and checks service, REST, and complete
query readiness before proceeding. Counts must remain unchanged.

Buckets that would immediately expire under destination retention block fixup.
Lengthen destination retention through an independently reviewed procedure or
leave creation-time fixup unapplied. No acknowledgment overrides this block.

Restoration runs in `finally`. A damaged catalog is the exception: restarting
could compound loss, so the engine preserves the backup, holds the service, and
records recovery-required. `recover-services RUN_ID` re-observes ownership and
catalog safety before starting an engine-owned node. It never starts an unrelated
stopped node. The simulator models an independent remote restorer; a real transient
unit/helper must still pass workstation-loss and catalog-corruption lab tests.

**The existing research helper has not proven safe reassignment of API-registered
buckets from Default to another partition, or persistent creation-time updates.**
Real fixup remains blocked. If it requires bucket deletion, it cannot be enabled
under the current no-delete requirement. The simulator's metadata update is a
contract test, not evidence that the vendor catalog supports that update.

## Security and remaining release gates

No passwords are accepted in argv or environment variables. Library credential
providers use masked prompts or an input stream and keep values in memory only.
Source SSH has a fixed read allowlist and no write/service-control method. HTTPS
requires CA/hostname validation plus an enrolled certificate digest, checked before
sending credentials. SSH requires an enrolled known-host key and digest before
password authentication. Unknown/changed identities have no bypass.

9.0 sessions use Bearer authentication. 9.1.1 exchanges an in-memory OpsToken for
an `ops-li` JWT and uses `X-JWT-Token` at the Logs FQDN on 9543; both HTTPS endpoints
must be enrolled. Safe GETs can refresh once; mutations are never replayed on an
uncertain response. The exchange response decoder is injectable pending a genuine
wire capture. Recorded transports do not retain authentication/request bodies.

The official mover sends source root credentials to the destination. Use a
temporary migration password and rotate it afterwards; the engine does not change
source passwords. Credential leakage and destination-to-source host validation
inside the vendor mover are not established by this client. Credential tests are
sequential, stop at the first failure, and never retry failed SSH authentication.
Check the source lockout policy before a future qualified run.

Before enabling real operations, retain evidence for:

- Exact task/query/auth schemas and full source/destination inventory reconciliation.
- MurmurHash placement variant/seed/order and collision behavior on all nodes.
- Mid-bucket cancel, node restart, partial directory, and 95% disk-limit outcomes.
- Source command audit and canary-secret sweeps of logs, argv, stored state, and bundles.
- Independent remote service restoration after workstation/SSH loss.
- Catalog backup/superset integrity, safe Default reassignment, creation time and retention.
- 9.1.1 partition readiness, field searchability, masking behavior, and query accounting.
- OS-specific trust, crash reporting, private storage permissions, and locking.

Capability labels: the engine/CLI simulation is **Experimental**; the historical
8.18.7→9.0.2 API approach has **Validated lab evidence** at the documented builds;
9.1.1 is **Experimental / preview**. Active copying, source stops, deletion,
overwrite, and a fallback mover are **Unsupported**. Uncaptured wire formats,
unqualified builds and 9.0→9.1.1 source migration are **Unknown**. No production
readiness is inferred from fixture tests.
