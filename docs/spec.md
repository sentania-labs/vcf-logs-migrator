# Spec: `logs-migrate`, an 8.x to 9 migration helper

Date: 2026-11-06. Status: design draft. The engine and CLI landed as a fixture
delivery with real appliance mutation disabled; shipped commands, flags and exit
codes are in [`operator-cli.md`](operator-cli.md), which wins over this draft.
The principal's later 2026-11-06 re-scope makes this delivery a tight API wrapper:
date preview, start, direct API status/progress, per-day verification, partition
comparison and a plain report. Local task/recovery states, close/reconcile,
plan-hash gates and tail reruns described below are not shipped. Fixup is set aside
and hidden; forwarding, partition, masking and filter rule migration is covered in
[`operator-cli.md`](operator-cli.md). Only data
protection guards and required backup/active-data attestations remain. The API is
the sole authority for task progress, with local sent-bucket and count evidence.

Basis: [`research-guide.md`](research-guide.md), [`lab-runs.md`](lab-runs.md),
and the 2026-11-06 validation docs. The principal's 2026-11-06 interview
decisions supersede the earlier manual copy design, kept in Appendix A.

## 0. Earlier design decisions (superseded where noted)

1. **API-only mover for v1.** Use Broadcom's `/api/v2/data-migrations`.
   It has no 90-day cap, copies sealed buckets from every source node and
   leaves the source untouched. There is no stage/commit fallback. See
   [`validation-2026-11-06-official-api.md`](validation-2026-11-06-official-api.md).
2. **9.0.x and 9.1.1 (preview) destinations.** 9.0 copies and registers
   without a service stop. 9.1 re-indexes events into OpenSearch; there is no
   destination bucket store. Its partition routing is established by code
   reading, not yet proven live. A 9.0-to-9.1 run needs a lab test before
   release; the v1 source baseline is 8.18.x.
3. **Never delete on the destination.** Skip matching existing buckets only
   after integrity and inventory checks. Flag a mismatched destination
   bucket, block execution, and print manual remediation steps. Never
   automatically remove or replace it.
4. **Always skip active buckets.** Never stop the source to copy them.
   Preview names the last fully sealed day and requires the acknowledgment
   "i understand you aren't going to copy the latest data". A later run of
   the same window collects the tail once those buckets seal, subject to
   the rerun guards below.
5. **Partitions are a data prerequisite.** Compare source and destination
   partitions for both targets and offer to create missing ones. Matching
   filters must exist on 9.1 before the run or events route to Default.
   On 9.0 the optional fix-up needs the partitions too. Config migration
   beyond partitions is a follow-on release.
6. **Optional 9.0 fix-up.** The API registers into Default with creation
   time = now. Fix-up restores source partitions and creation time = source
   bucket endTime. Configure it before the migration to run afterwards, or
   invoke it later. It needs per-node credentials and stop consent. 9.1
   needs no fix-up.
7. **Guard reruns across nodes.** 9.0 assignment is deterministic for a
   given destination node count; dedupe checks only the assigned node.
   Refuse a rerun after that count changes. Inspect earlier transfers,
   including the official UI's, before permitting overlap. 9.1 rerun dedupe
   is unvalidated; uncertain overlap blocks execution.

## 1. Why

Broadcom's Log Data Transfer UI stops at 90 days; its API does not. The tool
adds date-based planning, capacity and overlap guards, durable progress,
verification, and a record a customer or support engineer can read afterward.
Never use the stock `bucket-tools --bucketMigrationStart`: it selects by
directory mtime and deletes buckets on the source.

## 2. Scope

In:

- Aria Operations for Logs 8.18.x sources, single-node and clusters, to VCF
  Operations for Logs 9.0.x or 9.1.1 (preview).
- Any date range, including destinations that already hold data, with
  baseline accounting and overlap checks.
- API copy only; source data and services are never modified.
- Partition comparison and offer-to-create, pre-run extracted-field checks,
  and optional 9.0 partition/creation-time fix-up.
- Before and after verification by per-day counts and sampled events.

Out:

- Deletion on either side; source decommissioning is a human step.
- Active-bucket copying, source stops, and a manual mover fallback.
- Config migration beyond partitions (dashboards, alerts, extracted fields,
  agent groups and other configuration). Checks do not migrate those
  objects; prepare them separately before the run where needed.
- Replay and archive import paths (Path C).

## 3. Shape

A cross-platform desktop GUI and automation-friendly Python CLI share one
engine and state model. Engine and CLI with fixture tests land first, then
the GUI, then packaging.

The workstation uses SSH and REST for discovery and verification. The API
has the destination pull from the source over SSH; bucket data does not pass
through the workstation. Any source member works: the API reads its newest
`loginsight-config.xml`, and every listed member must resolve and answer on
22. Check all of those addresses before the run.

9.0 uses the normal Logs session with `Authorization: Bearer`. For 9.1.1,
exchange an Ops token for a Logs JWT using
`POST /suite-api/api/auth/token/exchange` with `{"serviceKeys":["ops-li"]}`.
Send `X-JWT-Token` to the Logs FQDN on 9543, not the 443 ingress or bare VIP.
9.1 verification uses its query API.

## 4. Commands

```
logs-migrate plan    --source <host> --target <host> --since 2026-05-01 [--until ...]
logs-migrate copy    --plan plan.json [--dry-run]
logs-migrate verify  --plan plan.json [--samples 3]
logs-migrate status  --plan plan.json
logs-migrate fixup   --plan plan.json [--dry-run]     # 9.0 only, optional, see 4.4
```

`copy` runs the API mover. The CLI and GUI require the active-data
acknowledgment in section 0; it replaces `--allow-source-stop`.
Credentials come from environment or a prompt, never argv:
`LOGS_SRC_ROOT_PW`, `LOGS_SRC_ADMIN_PW`, `LOGS_TGT_ROOT_PW`, `LOGS_TGT_ADMIN_PW`
(admin for Logs REST, root for SSH). Admin auth provider defaults to `Local`.
9.1 additionally needs the Ops-token exchange above. Never persist secrets
in plans, logs or reports.

### 4.1 `plan`

Discovery is read-only on both sides. Produces `plan.json` and a human
summary; creating missing partitions is an offered, authorized action.

1. Discover versions, capabilities and cluster membership. Inventory every
   source node. Check connectivity to every source address the API uses,
   including SSH from the destination.
2. Read `bucket-index show` per source node for id, partition, status,
   messages and time range; record directory size and file count. Select
   sealed buckets by range overlap, never directory mtime or creation time.
3. List active buckets as skipped. Name the last fully sealed day and the
   omitted tail. Show wide late-arrival ranges and uncertainty rather than
   claiming exact date boundaries.
4. Compare partitions on both targets and offer missing-partition creation
   by REST. Check extracted fields before the run: on 9.1 custom fields can
   be stored but unsearchable without a mapping. Matching partition filters
   must exist before import; first match wins. Block unmet prerequisites.
5. Inventory destination data and earlier transfers, including the official
   UI's. Compare bucket identity across nodes on 9.0; matching buckets skip,
   mismatches block with manual steps. Changed destination node count blocks
   a rerun. Uncertain overlap blocks, including unvalidated 9.1 reruns.
6. On 9.0, budget from the service's own `available` in the runtime log's
   `Repository space monitor` line, not `df`. Include migrated data plus
   live growth and the configured margin; stop before the 90 percent mark.
   Never set `max-repository-size` below 2 GiB. Do not assume this capacity
   adapter applies to 9.1; uncertain capacity blocks execution. The API's
   95 percent guard is not sufficient.
7. Report source retention risks. Without fix-up, 9.0 retires migrated data
   by registration time, ahead of live data. Recommend fix-up when source
   partition retention/archive settings differ from Default or the space
   budget makes retirement order matter. 9.1 data ages with today's backing
   index, not the original event dates.
8. Baseline source and destination per-day counts with complete, stable
   queries. Record expected additions from eligible sealed data and events
   outside the requested window. Source bucket `messages` totals are a
   cross-check; wide buckets make them insufficient for windowed counts.

### 4.1a Choosing how far back: the date preview

Show presets (90 days, 3, 6, 12 months, everything) and a free date range.
No duration is a fixed delivery limit. Show requested and observed coverage,
existing destination history, eligible and skipped buckets, size, capacity,
and estimated duration with uncertainty.

The whole bucket comes along if its range overlaps. Main reach comes from
the main bucket stream; list wide late-arrival buckets separately ("reaches
Jul 28, plus 1 bucket with late events back to Apr 10"). Do not present the
oldest stray event as continuous coverage. Name the last fully sealed day;
a later run of the same window collects the tail after sealing when rerun
checks permit it.

### 4.2 `copy`

1. Revalidate inventory, capacity, partitions, credentials and overlap
   against the immutable plan. Missing source buckets or changed inventory
   stop the run; never trust a local checkpoint alone.
2. Require explicit backup/snapshot acknowledgment and the active-data
   acknowledgment. Do not claim snapshots exist without evidence.
3. Test with `POST /api/v2/data-migrations/test`, then start with
   `POST /api/v2/data-migrations`: source host, credentials,
   `bucketStartTimeMillis` and `bucketEndTimeMillis`. Both services stay
   running for the API copy; no manual stage/commit fallback.
4. Poll `GET /api/v2/data-migrations`. Persist task and per-node progress,
   selected and skipped buckets, errors and recovery state. `status` reports
   API state and incomplete work.
5. Stop cooperatively through the API cancellation operation. Revalidate
   remote task state and inventories before resuming; never blindly submit
   an overlapping window.
6. Verify. The 9.0 API can report COMPLETED after skipping buckets at its
   disk limit. Task completion alone is never verified success.
7. Run optional 9.0 fix-up if configured, after task completion and passing
   verification. Reverify afterwards.

### 4.3 `verify`

1. Wait for complete results on both sides and two counts 30 seconds apart
   to agree. Legacy queries require `complete=true`; 9.1 uses its query API.
2. Compare per-day counts and coverage, accounting for the destination
   baseline, existing events, concurrent ingest, skipped active buckets and
   whole-bucket coverage outside the window. Report missing, unexpected or
   duplicate data. An active tail means incomplete requested coverage.
3. Sample N windows across the range, first 20 events each; compare text and
   fields. Report 9.1 mapping/searchability limits explicitly.
4. Exit 0 only for exact expected per-day counts and identical samples with
   complete verification. Distinguish verified success, discrepancy,
   incomplete verification and failure. Write `verify.json` and a short
   Markdown report.

### 4.4 `fixup` (9.0 destinations, optional)

Re-registers migrated buckets with their source partition and a creation
time equal to the source bucket's data end time. Never needed on 9.1, which
routes each event through the partition filters during the migration.

**When it is configured.** Either:

- in the import config, before the migration: `fixup: {enabled: true,
  partitions: true, creation_time: true}` in `plan.json`; it runs
  automatically after the API task reports COMPLETED and `verify` passes; or
- afterwards, against a completed migration: `logs-migrate fixup --plan
  plan.json`, or `--task <data-migration task id>` when there is no plan
  (the tool rebuilds the bucket-to-partition map from the source listings
  and the destination listings, matching by bucket id, since ids are kept).

**Inputs.** The bucket id to partition and end-time map comes from the
source listings (`bucket-index show` on every source node, read-only). Which
destination node holds which bucket comes from the destination listings, not
from the task, so a fixup run later still finds them.

**Credentials per node.** The fix-up needs root SSH on every destination
node that received buckets, and read access to every source node. Members
of one cluster do not always share a root password (rebuilt nodes, rotated
passwords, different build teams). So:

1. Discover members first, from the `<distributed>` section of each
   cluster's newest `loginsight-config.xml` (the same file the API reads on
   the source). One SSH login to any member is enough to list the rest.
2. Ask once per cluster: "Same root password on all N nodes?" Yes: one
   prompt, applied to all. No: one prompt per node, labelled with the node's
   address and hostname.
3. Test every credential (SSH login plus `bucket-index show`) before any
   node is stopped. One failure stops the fix-up before it starts.
4. Credentials live in memory for the run only. `plan.json` records which
   nodes use shared or per-node credentials, never the values. Non-interactive
   runs take them from a credentials file the operator names
   (`--creds-file`, mode 600, `{"<node>": {"user": "root", "password_env":
   "VAR"}}` or a key path), never from argv.

**Steps, one destination node at a time:**

1. Recheck the partitions compared in `plan`. Create missing ones by REST
   only with the operator's authorization, and restart once before the first
   node if any were created.
2. With explicit stop consent for this node: stop `loginsight` (refuse to
   continue if it is already stopped for an unknown reason), run PartitionBucketAdd for that node's buckets
   (`<partitionId> archived <createTimeMillis> <ids>`, grouped by partition),
   start `loginsight`.
3. Confirm in `bucket-index show` that every id shows the expected partition
   and creation time. Never trust the exit code.
4. Wait for `complete=true` and re-run the per-day count for that node's
   window; it must match the pre-fix-up count.
5. Next node. A node's stop is an ingest blip for that node only; the
   preview states it ("about 1 minute per node, N nodes").

**Resumable.** Each node's state (pending, stopped, registered, verified)
goes to `plan.json`. A rerun skips verified nodes and finishes a node left
mid-way (start the service if the tool stopped it).

## 5. Safety rules

- Never call `bucket-index delete`, `--bucketMigrationStart`, or anything
  that writes to the source store. Never delete or replace destination data.
- Never stop the source; active buckets are always skipped.
- `fixup` requires per-node stop consent, stops at most one destination node
  at a time and starts it again in a `finally`. Verify stops and starts;
  persist recovery state rather than assuming they worked.
- Require explicit backup/snapshot acknowledgment before execution.
  Snapshots are the operator's job; no vCenter integration in v1.
- No secrets in command lines, logs, reports, diagnostics or error messages.
- `--dry-run` prints proposed operations without running them or exposing
  credentials.
- Unsupported versions or topologies, inventory changes, insufficient
  capacity and uncertain overlap or verification fail closed.

## 6. Output

- `plan.json`: inputs, versions, topology, inventory, warnings, partition
  and extracted-field checks, baseline counts, acknowledgments and run state.
- `copy.log`: timestamped API progress, bucket outcomes where known, skipped
  active data and destination service interventions for fix-up.
- `verify.json` and `report.md`: requested and actual coverage, per-day and
  per-month counts, samples, discrepancies, limitations and next actions.

## 7. Test plan

1. Date selection with sealed buckets, late-arrival ranges and an active
   tail: compare preview to actual coverage; never copy active buckets.
2. Populated destination, baseline and concurrent ingest: matching buckets
   skip; mismatches block with manual steps.
3. Retention or inventory change between plan and copy: refuse safely.
4. Interrupted API task and restart: revalidate, no blind resubmission.
   Same-window tail collection must pass overlap checks; a changed 9.0
   destination node count blocks reruns.
5. Capacity: stop before the configured threshold; API COMPLETED with
   skipped buckets is incomplete, never success.
6. Partition comparison and offer-to-create on both targets; extracted-field
   checks. Optional 9.0 fix-up configured before and invoked after migration,
   per-node credentials, stop consent, recovery and exact counts afterwards.
7. 9.1.1 preview: Ops-token exchange, query API verification, live partition
   routing with matching filters, field searchability. Test 9.0-to-9.1
   before release.

## 8. Remaining validation

- 9.1 partition routing is code-proven, not yet tested live with matching
  filters; masking on this path is inferred, not tested.
- A 9.0-to-9.1 run and 9.1 rerun dedupe remain untested.
- Do not claim every multi-node data distribution was exercised. The initial
  9.0 API cluster test selected source buckets from the primary; later tests
  included buckets from all three source nodes. Tie claims to lab evidence.

## Appendix A. Superseded manual path (reference only, not part of v1)

The earlier stage/commit and bucket-index design below is not a fallback
or a set of v1 requirements. Its source stops and destination deletion
proposal are superseded by sections 0 and 5. The research, validation docs
and prototype scripts remain historical evidence.

### Earlier validation changes

1. **`copy` splits into `stage` and `commit`.** Stage runs with both sides
   up: tar stream into `/storage/core/migrate-staging/<id>.partial`, file
   manifest (path + size) compared with the source, rename to `<id>`.
   Commit stops the 9 service, renames each staged bucket into the store,
   re-verifies, `bucket-index add`, confirms the id in the index, starts the
   service. Registering with the service running lost index entries in the
   lab. Commit is an ingest outage (about 3 minutes); the preview must say so.
2. **Never trust `bucket-index add`'s exit code** (0 on refusal). Confirm in
   `bucket-index show`.
3. **Id collisions.** An id already registered on the target is skipped only
   when its file manifest matches the source; otherwise stop. Rebuilt or
   reverted 8.18 appliances reuse install-time bucket ids.
4. **Space budget = the 9 service's own `available`** from the runtime log's
   `Repository space monitor` line, not `df`. Never write
   `max-repository-size` under 2 GiB (the service will not start). Warn that
   9.0 retires by registration time, so migrated buckets go before any live
   data; size the target for migrated data plus live growth over the
   retention the customer wants. Commit oldest-data-first.
5. **Preview reach** comes from the main bucket stream; buckets whose range
   is far wider than their neighbours (late-arriving backlogs) are listed
   separately ("reaches Jul 28, plus 1 bucket with late events back to
   Apr 10").
6. **Crash state.** A hard kill during commit leaves the target stopped and a
   manifest without an index entry. `status` reports: target service state,
   staged dirs, manifests, store dirs not in the index, index entries
   without a dir. `commit` rerun completes the job (proven).
7. Verification waits for `complete=true` on both sides and for two counts
   in a row to agree: both services return partial results for a minute or
   so after a start.

8. **Register with the partition-aware program, not `bucket-index add`.**
   `scripts/PartitionBucketAdd.java` calls `BucketIndex.addNewBucket(partition,
   id)` so buckets keep their source partition (ids are name-derived, so the
   tool creates same-named partitions on the target first, restarts, then
   registers). Plain `bucket-index add` puts everything in Default.
9. **Creation time = source bucket endTime** on every registration, so 9.0's
   retention and space retirement treat migrated buckets by data age.
10. **Per-node everything.** Inventory every source node; place buckets on
    destination nodes by each node's own budget; commit node by node (stops
    only that node; ingest through the cluster sees errors while it is down).
11. **Config migration is its own phase**, before data: see
    [`config-migration.md`](config-migration.md) for what moves by REST, what
    needs edits, and what is manual.
12. **Stale copies.** An id on the target whose manifest differs from a
    sealed source bucket (copied while active) is replaced: `bucket-index
    delete` on the target, copy, register.

### Earlier `copy` procedure

1. Re-read the inventory and refuse if it no longer matches the plan (a
   bucket gone means retention ran; a new sealed bucket is reported).
2. Sealed buckets first, source running, target running.
3. Active buckets last, only with `--allow-source-stop`: stop `loginsight`
   on that source node, copy, start it again, and record the stop time.
   Default is to leave active buckets out and say so.
4. Per bucket: free-space check on the target, `tar` stream from the source,
   compare file count and byte size against the source directory (the source
   is stopped or the bucket is sealed, so they must match), then
   `bucket-index add <id> --statuses archived`, then confirm the id shows
   `archived` in the target listing.
5. Resumable: progress written to `plan.json` after each bucket; a rerun
   skips buckets already registered on the target.
6. Soft stop on SIGINT after the current bucket; never leave the source
   stopped (start it in a `finally`).
