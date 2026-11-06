# Spec: `logs-migrate`, an 8.x to 9 bucket migration helper

Date: 2026-11-05. Status: draft, nothing built yet.
Basis: the lab runs in [`research-guide.md`](research-guide.md) section 4
(4.3 is the full one: 2.43M events, 8 months, exact match) and
[`lab-runs.md`](lab-runs.md).

## 1. Why

Broadcom's Log Data Transfer stops at 90 days, and the stock
`bucket-tools --bucketMigrationStart` behind it is unsafe to run by hand: it
picks buckets by directory mtime (so it takes the active bucket), deletes
each bucket on the source, and prompts twice. The manual copy path works and
is fast (632 MB in 44 s), but it is a dozen hand-typed steps where the
mistakes are silent: a bucket left out, a source that retires buckets
mid-run, a verification read while 8.18 is still warming up and reporting
partial counts. The tool turns the runbook into plan, copy, verify, with a
record a customer or support engineer can read afterward.

## 2. Scope

In:

- Operations for Logs 8.18.x source (single node first, cluster second) to
  9.0.x target (single node first).
- Copy mode only. The source is never modified apart from a stop and start
  of `loginsight` when active buckets are copied.
- Any time range, chosen by date, not by bucket count.
- Before and after verification by per-day event counts and sampled events.

Out:

- Deleting anything on the source. Decommissioning 8.18 is a human step.
- Content (dashboards, alerts, extracted fields, agent groups): content pack.
- 9.1 targets (VCF Management Services) until tested.
- Replay and archive import paths (Path C). Possibly a later `--via archive`.

## 3. Shape

One Python 3 CLI, stdlib plus `paramiko` (no `sshpass`), run from an admin
workstation that can reach both clusters on 22 and 9543. It drives both sides
over SSH and REST. Nothing is installed on either appliance; remote commands
are the appliance's own (`bucket-index`, `tar`, `df`, `systemctl`).

Bucket data moves source to target directly (target pulls over SSH, `tar`
stream), not through the workstation. The target needs SSH from itself to the
source, as Broadcom's transfer already requires.

## 4. Commands

```
logs-migrate plan    --source <host> --target <host> --since 2026-05-01 [--until ...]
logs-migrate copy    --plan plan.json [--allow-source-stop] [--dry-run]
logs-migrate verify  --plan plan.json [--samples 3]
logs-migrate status  --plan plan.json
```

Credentials from environment or a prompt, never argv:
`LOGS_SRC_ROOT_PW`, `LOGS_SRC_ADMIN_PW`, `LOGS_TGT_ROOT_PW`, `LOGS_TGT_ADMIN_PW`
(admin for REST, root for SSH). Admin auth provider defaults to `Local`.

### 4.1 `plan`

Read-only on both sides. Produces `plan.json` and a human summary.

1. Versions of both nodes (`/api/v1/version`), cluster membership on the
   source (all nodes get planned; a bucket lives on one node).
2. Bucket inventory per source node: `bucket-index show` for id, partition,
   status, plus `du -sb` and file count of each store directory. A bucket
   is in the plan when its directory exists and is non-empty. The listing's
   size, message count and time range are recorded but not trusted (4.3:
   all four buckets reported `size=0` while two held 2.43M events).
3. Time range per bucket from the data, not the metadata: one
   aggregated-events query per bucket is not possible over REST, so the plan
   takes every non-empty bucket in the source whose `created` time is before
   `--until` and reports the per-day count for the requested window as the
   expected result. Open question 8.1.
4. Source risks, each a warning with the evidence:
   - retention settings in `loginsight-config.xml#*` (`retention-bucket-count`,
     time-based retention shorter than the window)
   - active buckets in the plan (they need `--allow-source-stop`)
   - buckets already present on the target (skipped)
   - non-default partitions (not proven; flagged)
5. Target capacity: `/storage/core` free space against the plan's total
   with a margin; stop below the target's 90 percent mark.
6. Baseline: per-day counts on the source for the window, through REST,
   30-day windows with 1-day bins, retried until `complete=true`.

### 4.1a Choosing how far back: the date preview

The operator thinks in dates ("import back to August 6"), the data moves in
buckets. `plan --since` (and the later UI) shows the trade before anything
moves:

| Go back to | Buckets | Size | Events (est.) | Copy time (est.) | Actually reaches |
|---|---|---|---|---|---|
| 3 months (Aug 6) | 92 | 46 GB | 27M | ~15 min | Aug 4 |
| 5 months | 151 | 75 GB | 45M | ~25 min | ... |
| 8 months | 240 | 120 GB | 72M | ~40 min | ... |

(Illustrative numbers.)

- Rows: presets (90 days, 3, 6, 12 months, everything) plus a free date.
- A bucket is in a row when its time range overlaps the window. The bucket is
  the unit: it cannot be trimmed, so a row usually reaches a little further
  back than asked. "Actually reaches" is the start of the oldest bucket in the
  row, and it is the honest answer to "how far back will I get".
- Size from `du` on the source store; copy time from a measured rate (the lab
  saw about 7 MB/s, 632 MB in 44 s) with the first bucket re-measuring it;
  free space on the target shown beside it.
- Event estimate from the source's per-day counts over REST for the window
  (real numbers, not a guess from bytes).
- After `copy`, `verify` reports the oldest day actually present on the
  target, so the estimate is checked against the result.
- Stated plainly in the output: edges are approximate, because late-arriving
  events (an agent flushing a backlog) land in whatever bucket was open when
  they arrived; and if Broadcom's 90-day transfer already ran, the preview
  starts where it stopped.
- Bucket time ranges come from the source's `bucket-index show` for sealed
  buckets. Active buckets report stale or empty metadata (4.3) and are
  placed at "now". Whether sealed-bucket ranges are accurate is open
  question 8.1 and the first test in section 7.

### 4.2 `copy`

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

### 4.3 `verify`

1. Wait until the source answers with `complete=true` and two counts 30 s
   apart agree (4.3: 8.18 returns 897k of 2.43M for about a minute after a
   restart).
2. Per-day counts on both sides over the window; report every day that
   differs.
3. Sampled events: N windows across the range, first 20 events in each, text
   and all fields compared.
4. Exit 0 only on an exact per-day match and identical samples. Writes
   `verify.json` and a short Markdown report.

## 5. Safety rules

- Never call `bucket-index delete`, `--bucketMigrationStart`, or anything that
  writes to the source store.
- Stop the source only with `--allow-source-stop`, and only for the copy of
  its active buckets.
- Snapshots are the operator's job; `plan` prints a reminder and the VM
  names it found (no vCenter integration in v1).
- No password on any command line, local or remote (the hand runs passed
  `SSHPASS` inline in the remote command, which shows in `ps` on the target).
- `--dry-run` on `copy` prints every remote command without running it.

## 6. Output

- `plan.json`: inputs, inventory, warnings, baseline counts, per-bucket state.
- `copy.log`: timestamped, one line per bucket (size, copy s, register s).
- `verify.json` and `report.md`: per-month table, differing days, samples.

## 7. Test plan (an 8.18 and a 9.0 test node)

0. Date selection: chronological ingest, 100 MB buckets, `--since` 3 months,
   compare predicted "actually reaches" and event estimate with the result.
1. The 4.3 case again: reset 9.0, two active buckets, `--allow-source-stop`,
   exact match expected.
2. Sealed buckets: lower `max-bucket-size` on 8.18 to 100 MB, refill, copy
   with the source running, no stop.
3. Retention trap: `retention-bucket-count=4` on the source, plan must warn;
   seal a new bucket between plan and copy, copy must refuse.
4. Resume: kill `copy` mid-run, rerun, no duplicates on the target (per-day
   counts still exact).
5. Space: shrink the target margin, copy must stop before 90 percent.
6. Partition: a non-default index partition on 8.18, find out whether the
   registered bucket is queryable on 9 and in which partition.

## 8. Open questions

1. **Picking buckets by time.** REST cannot ask "which bucket holds this
   day". A bucket directory is a Lucene index plus `repository/data.blob`
   with no plain time-range file, so the range has to come from
   `bucket-index show`. Unproven: whether its startTime/endTime are accurate
   for sealed buckets (they are wrong for active ones). Test: ingest in date
   order with a 100 MB bucket cap so many dated buckets seal, then copy
   only the last 3 months and check the target's oldest day. Fallback if the
   listing is unreliable: bucket created/sealed times, which track arrival
   order on a live site.
2. **Multi-node source clusters.** Broadcom's transfer wants root on every
   source node. Same here; untested in the lab (one node each side).
3. **Multi-node target.** Which target node should receive a bucket, and
   does the 9 cluster rebalance archived buckets? Untested.
4. **Duplicates with the 90-day UI transfer.** If the official transfer has
   already moved the newest 90 days (and deleted them on the source), the
   tool should take only what is older. Plan should detect bucket ids already
   on the target; overlap by time is not detected.
