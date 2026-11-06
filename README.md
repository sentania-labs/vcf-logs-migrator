# vcf-logs-migrator

Moving log data from **Aria Operations for Logs 8.18** to **VCF Operations for
Logs 9.0** past the 90-day limit of Broadcom's Log Data Transfer.

Status: **research and a spec, no tool yet.** Everything here comes from lab
runs on single-node clusters with synthetic data. It is not supported by
Broadcom. Snapshot both sides before trying any of it.

## The workflow

![Migration workflow](docs/images/migration-workflow.svg)

Step by step: [`docs/migration-workflow.md`](docs/migration-workflow.md).

## The short version

- **Start here:** Broadcom's own transfer API (`/api/v2/data-migrations`, on 9.0 and 9.1.1) takes any time window; the 90-day limit is only in the UI. It is the supported mover; this repo's tools add planning, dedupe, verification and the partition/creation-time fix-ups it lacks. See [`docs/validation-2026-11-06-official-api.md`](docs/validation-2026-11-06-official-api.md).

- 8.18 cannot upgrade in place to 9.x. The supported path is a new 9.0
  appliance plus the Log Data Transfer in the 9.0 UI (Administration >
  Control Panel > Logs Data Transfer), capped at the newest 90 days.
- That transfer is a bucket copy under the hood. Copying more buckets by hand
  works: in the lab, **8 months / 2.43M events moved in 44 s of copy, per-day
  counts identical, every field preserved, original timestamps kept.**
- Copy into a staging area with both sides running; **register with the 9
  node stopped** (about 3 minutes of ingest outage per commit). The 8.18
  node only stops while its active (unsealed) buckets are copied.
- Once over its space budget, 9.0 retires buckets by *registration* time, so
  migrated history is always the first to go. Size the target for migrated
  data plus live growth. Validation details:
  [`docs/validation-2026-11-06.md`](docs/validation-2026-11-06.md).
  Registering with the bucket's data end time as its creation time fixes
  that, and a small program keeps non-default index partitions (stock
  `bucket-index add` puts everything in Default).
- Do **not** run the stock `bucket-tools --bucketMigrationStart` by hand: it
  takes the active bucket by mtime and deletes every bucket on the source.

## What is here

| Path | What |
|---|---|
| [`docs/migration-workflow.md`](docs/migration-workflow.md) | The end-to-end workflow with `logs-migrate`, with the diagram |
| [`docs/research-guide.md`](docs/research-guide.md) | The full guide: options (keep 8.18 as cold archive, bucket copy, archive import, replay), what was proven, the runbook with every command |
| [`docs/lab-runs.md`](docs/lab-runs.md) | What was run, when, and the numbers |
| [`docs/validation-2026-11-06.md`](docs/validation-2026-11-06.md) | Date selection, merge into a populated target, crash recovery, space, staging: tested |
| [`docs/validation-2026-11-06-partitions-multinode.md`](docs/validation-2026-11-06-partitions-multinode.md) | Partition-aware registration (engineered and proven), creation-time fix, 3-node source and destination clusters |
| [`docs/validation-2026-11-06-official-api.md`](docs/validation-2026-11-06-official-api.md) | Broadcom's `/api/v2/data-migrations` on 9.0 and 9.1.1: no 90-day cap in the API, how 9.1 re-indexes legacy buckets into OpenSearch, what it loses |
| [`docs/config-migration.md`](docs/config-migration.md) | Config migration 8.18 to 9.0: forwarders, masking, partitions, users, alerts, content; what moves by REST and what is manual |
| [`docs/spec.md`](docs/spec.md) | Draft spec for `logs-migrate`: plan / copy / verify, date-based preview ("import back to this date") |
| [`scripts/bucket-migrate.sh`](scripts/bucket-migrate.sh) | Prototype: `stage` (live, verified, resumable) and `commit` (target stopped), `status` |
| [`scripts/PartitionBucketAdd.java`](scripts/PartitionBucketAdd.java), [`scripts/partition-add.sh`](scripts/partition-add.sh) | Register buckets into a named partition with a chosen creation time (target stopped) |
| [`scripts/bucket-copy.sh`](scripts/bucket-copy.sh) | First copy script (superseded: registers with the target running) |
| [`scripts/count-events.py`](scripts/count-events.py) | Per-day / per-month event counts over REST, for before and after |
| [`scripts/replay.py`](scripts/replay.py) | Replay events between instances through the agent-format ingest, keeping timestamps |
| [`scripts/synth/`](scripts/synth/) | Synthetic data generators used for the tests (ESXi syslog, mixed syslog) |

## Copy in four steps (prototype)

From [`docs/research-guide.md`](docs/research-guide.md) section 4.3:

```
# 1. source (8.18), as root: which buckets hold data, and retention settings
bucket-index show
du -sh /storage/core/loginsight/cidata/store/*-*-*-*-*
grep -o '<retention[^>]*>' /storage/core/loginsight/config/loginsight-config.xml#*

# 2. baseline counts
LI_PASS=... LI_OUT=before.json scripts/count-events.py <8.18 node> 300

# 3. stage with both sides running (sealed buckets), then commit
ssh root@<9> "SSHPASS=... /root/bucket-migrate.sh stage <8.18 ip> <bucket id> ..."
#    active buckets: stop 8.18, stage them, start 8.18
ssh root@<9> /root/bucket-migrate.sh commit     # stops 9, moves + registers, starts 9

# 4. verify (wait for the source to finish warming up before comparing)
LI_PASS=... LI_OUT=after.json scripts/count-events.py <9 node> 300
```

Content (dashboards, alerts, extracted fields, agent groups) does not travel
with buckets; move it as a content pack.

## License

MIT
