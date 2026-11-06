# Validation run 3, 2026-11-06: Broadcom's data-migrations API (9.0 and 9.1.1)

Both 9.0.2 and 9.1.1 ship the API behind the "Logs Data Transfer" card:

```
POST /api/v2/data-migrations/test   {"hostName","username","password"}            connectivity check
POST /api/v2/data-migrations        {"hostName","username","password",
                                     "bucketStartTimeMillis","bucketEndTimeMillis"} start
GET  /api/v2/data-migrations                                                       status, per-node detail
DELETE /api/v2/data-migrations                                                     cancel
```

`hostName` is one node of the legacy cluster; the destination discovers the
other source nodes itself and needs root SSH (port 22) to all of them.
**There is no partition field and no bucket-count field; the selection is a
time window, and the server does not cap it at 90 days** (a Mar 1 to Aug 1
window and a Jun 1 to Jun 21 window, all older than 90 days, both ran).

Auth: on 9.0, the normal Logs session (`Authorization: Bearer`). On 9.1.1 the
API is served by the `log-processor` pod on port 9543 of the Logs FQDN (the
443 ingress answers "not available on this port", the bare VIP 404s); it
takes a JWT from VCF Operations:
`POST /suite-api/api/auth/token/exchange {"serviceKeys":["ops-li"]}` with an
OpsToken, then header `X-JWT-Token`. Same request body as 9.0.

## 9.0.2 (destination: 3-node cluster)

Source: 3-node 8.18.7 cluster. Window Mar 1 to Aug 1. 44 buckets, all from
the source primary (the workers held only September data), finished in
about 2.5 minutes.

- **Spread across destination nodes automatically**: 16 / 14 / 14 buckets,
  one task per node, each node copies over SSH and registers in-process
  (`DataMigrationTaskDaemon`: `addNewBucket`, `setBucketCommitMetadata`,
  `IndexStore.addIndexBucketIfNecessary`). **No service stop.**
- **Copy, not move**: source buckets stay.
- **Selection**: sealed buckets whose range overlaps the window, from every
  source node (`bucket-index show | grep -v status=active` over SSH).
- **New data exact**: dataset `part-test` (never migrated before): 784,833
  on source, 784,833 on destination, 20 days.
- **Duplicates on rerun / overlap**: the "already exists, skip copy" check is
  per destination node. Buckets already registered on the primary were
  copied again onto the workers (8 ids now on two nodes); counts went over
  the source (synth-chrono 2,618,027 vs 2,326,353). The API is not safe to
  rerun over a window that overlaps an earlier transfer, ours or its own,
  unless the overlap was on the same node.
- **Partitions**: all into Default (two `secops` buckets landed in Default
  on a worker).
- **Creation time = registration time**: the retirement-order problem from
  validation 1 applies to API-migrated buckets too.
- Stops at 95 % disk per node; the rest of that node's buckets are skipped
  and reported.

## 9.1.1 (production, VMSP)

9.1.1 Log Management is a different engine: `log-processor` (ingest, API)
and `log-store` (OpenSearch) pods in namespace `ops-logs`. There are no
buckets on the destination.

Same request, window Jun 1 to Jun 21: **15 source buckets, finished in
about 2 minutes, 1,269,209 events, exactly the sum of the 15 buckets'
`messages`.**

How it works (`com.vmware.vcf.logs.legacy.migration.*` in
`ops-logs-1.0.0.jar`):

1. SSH to every legacy node, list sealed buckets in the window.
2. `scp` each bucket into the `log-processor` pod.
3. `LegacyImportService` decodes the legacy repository blob directly
   (zstd/zlib segments, thrift-serialised messages) and re-indexes every
   message into OpenSearch, then deletes the local copy. The source is
   untouched.

Where the data lands and what it looks like:

- **In the Default partition's live data stream**, today's write index
  (`.ds-logs-<default partition id>-NNNNNN`), not in a separate partition.
  (The code also has an "Archived Logs" import partition `li-next-import`
  with a 7-day default retention; that belongs to the archive-restore
  feature and was not used by the legacy migration.)
- `logTimestamp` and `ingestDate` both carry the original event time;
  `originalText` and the stored `fields` are intact.
- **Custom fields from the source are stored but not searchable as fields**
  unless 9.1 has a mapping for them (an extracted field or a known field):
  `fields.dataset` was in `_source` but a term or phrase query on it matched
  nothing. Text search finds the events.
- Retention: the migrated events live in today's backing index, so they age
  out with that index, like data ingested today, not by their original date.

What Ops does: the Control Panel "Logs Data Transfer" card is a front end
on this API (Ops issues the `ops-li` JWT; `log-processor` does the work).
Inferred from the code and the auth path; the UI calls themselves were not
captured (the Ops UI login form could not be automated cleanly).

## Consequences for the tool

| | 9.0 | 9.1.1 |
|---|---|---|
| Use the official API? | Yes for the copy: no stop, spreads across nodes, any window | Yes: it is the only way in (no bucket store to write to) |
| Partitions | Lost (Default). Fix up afterwards with `PartitionBucketAdd` (stop per node) | Lost (Default). Nothing to fix up; plan searches and retention around it |
| Creation time / retirement | Registration time. Fix up with `PartitionBucketAdd` (`-t` endTime) | Ages with the day of import |
| Reruns / overlap | Per-node dedupe only: plan non-overlapping windows, or verify per id across nodes before | Not tested; re-ingest would duplicate documents |
| Custom fields | Kept, searchable | Kept in `_source`, searchable only where 9.1 maps them; create extracted fields first |
| What the tool adds | Planner (window to buckets, preview), cross-node dedupe check, verification, partition and creation-time fix-up | Planner, preview, verification (counts against source `messages`), field mapping prep |
