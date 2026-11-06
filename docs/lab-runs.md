# Lab runs, 2026-10-30 to 2026-11-05

What was run to produce the findings in [`research-guide.md`](research-guide.md).
One lab, single-node clusters on both sides, synthetic data only.

## Test bed

- **Source:** Aria Operations for Logs 8.18.7 (build 25423541), small
  (4 vCPU / 8 GB), standalone, from the support-portal OVA. Root SSH works with
  the OVF password.
- **Target:** VCF Operations for Logs 9.0.2.0200 (build 25430789), standalone,
  from the depot OVA. xsmall (2 vCPU / 4 GB) is unusable: queries come back
  `complete=false` and the Importer is refused. Resized to 4 vCPU / 8 GB.
- Both on the same L2 segment; NFSv3 export on a NAS for the archive tests.
- vCenter snapshots before every state change; the target was reverted to a
  clean-install snapshot between runs.

## 2026-10-30: the four paths

- **Ingest API timestamps.** The public `{"events":[...]}` schema (v1 or v2)
  clamps every timestamp to receipt time, authenticated or not. The agent wire
  format, `POST /api/v2/events/ingest/<agentId>` with
  `{"timestampValidation":false,"messages":[...]}` and a Bearer session, keeps
  the original timestamps on 9.0.2 and on 8.18.7. `scripts/replay.py` uses it.
- **Importer** `--honor_timestamp` works with user/password over TLS; the
  stock cert mismatch needs a prompt answer or a pre-placed cert.
- **NFS archive import.** 8.18 archiving is per index partition; blobs land as
  `yyyy/mm/dd/hh/<bucket>/data.blob`. `loginsight repository import <dir>` on
  9.0.2 re-parses them (301k events from a 24 MB blob in about 25 min), keeping
  stored timestamps and fields.
- **Bucket copy.** `bucket-tools-importer` with the source-side delete removed:
  a 74 MB bucket copied in 2 s, registered in 5 s, events queryable on 9 at
  original timestamps with every field.
- Read from the appliance: `bucket-tools --bucketMigrationStart` (the engine
  behind the 90-day UI transfer) picks the newest N directories by mtime,
  which includes the active bucket, and deletes each bucket on the source.
- Test data: 2M synthetic events over 300 days via the agent-format ingest.
  8.18 indexes about 12k to 19k events/min on 4 vCPU.

## 2026-11-04: five buckets, live target, active buckets

- 1.28M events of realistic multi-host syslog over 6 months
  (`scripts/synth/gen_syslog_6mo.py`); ingest accepts about 500k/min, fully
  indexed within about 15 min.
- Three sealed buckets copied with the 9 node **running** (17 s total): no
  target outage needed.
- Two active buckets copied with the **source stopped** (32 s outage). A
  restart does not seal a bucket; only the size cap does.
- A test override `retention-bucket-count=4` on the source retired the two
  newest sealed buckets three hours after they sealed. About 740k events
  survived only in the NFS archive. Lesson: check source retention first.

## 2026-11-05: full migration, 8 months

- Source refilled from clean install (stock config): 24 synthetic ESXi hosts,
  2026-03-09 to 2026-11-04, 2,429,018 events, 476 MB raw
  (`scripts/synth/gen_vsphere_hosts.py`). All of it in two active buckets.
- Target reset to clean install, 0 events.
- Source stopped, two buckets (313 MB and 319 MB) copied in 17 s and 27 s and
  registered in 2 s each with `scripts/bucket-copy.sh`, source started:
  70 s source outage, target running throughout.
- Verification with `scripts/count-events.py`: 2,429,018 on both, per-day
  counts identical for all 241 days; 20-event samples on Mar 11, Jun 1 and
  Sep 1 identical in text and every field.
- Traps found: source `bucket-index show` reported `size=0 messages=0` for
  the two buckets holding all the data; the source returns partial counts
  (`complete=false`) for about a minute after a restart; aggregated-events
  with `view=SIMPLE` returns zero bins, and one query caps at 100 bins.
