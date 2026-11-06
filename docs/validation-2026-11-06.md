# Validation run, 2026-11-06: the open questions

Six questions came back from the tool design review. Five were tested on the
lab pair (single-node 8.18.7 source, single-node 9.0.2.0200 target); the sixth
(multi-node, non-default partitions) stays open. Prototype used:
[`scripts/bucket-migrate.sh`](../scripts/bucket-migrate.sh).

Test data:

- Source reset to clean install, bucket size lowered to 10-20 MB
  (`<repository><min-bucket-size value="10485760"/><max-bucket-size value="20971520"/>`)
  so data seals into many small buckets. 2,227,824 synthetic ESXi events,
  2026-03-11 to 2026-11-06, ingested in date order: 31 sealed buckets plus
  the active ones.
- Target already holding 2,429,018 migrated events from the night before
  (destination with data), plus simulated live traffic (200 events every 2 s)
  during the tests.
- Late arrivals: 98,529 events stamped Apr 10-13, sent after everything else.

## Headline corrections to earlier findings

1. **Registering a bucket while the 9 service runs is NOT safe.** The earlier
   "no stop needed" result (research guide 4.2) came from a few buckets added
   in one window. With 12 buckets added one after another into a running
   target, the service picked up the first two and ignored the other ten.
   After a restart the ten appeared, but **two previously registered buckets
   had dropped out of the index** (files still on disk). The bucket list is
   a file (`store/buckets`) that the running service also writes. Re-adding
   the two with the service stopped recovered them exactly. Rule: copy with
   the target running, **register with the target stopped**, exactly as the
   tool's own help says.
2. **`bucket-index add` exits 0 when it refuses.** Re-adding an existing id
   prints `Bucket ... already exists.` and changes nothing (safe), but the
   exit code is 0. Check the index afterwards, never the exit code.
3. **The 9 node retires buckets by registration time, not by data age.**
   When over its repository cap, 9.0 retired its own two oldest buckets,
   then the migrated buckets in the order they had been registered
   (data from Jul 28 to Nov 6 first), then a bucket re-added later holding
   1.2M events from March to November. The March to July buckets, registered
   last, survived. Retired buckets are deleted from disk.

## 1. Picking buckets by date

**Result: works, at bucket granularity, with one caveat.**

- `bucket-index show` on the source gives `startTime`, `endTime` and
  `messages` for every sealed bucket. The sum of `messages` over the 12
  buckets selected for "back to 2026-08-06" was 874,099; the target showed
  exactly 874,099 after the copy. The oldest event on the target was on
  2026-07-28, the predicted reach (oldest `startTime` in the selection).
- Days from the cutoff to the last sealed bucket were complete on the target;
  the days before the cutoff came along partially (the head bucket). The only
  days after the cutoff that were short are the newest ones, held in active
  buckets that were not copied (31,240 events, exactly the source total minus
  the sealed `messages` sum).
- **Two writers per partition** (`writers-per-partition` 2): buckets come in
  two interleaved streams, each covering about two weeks, overlapping by a
  week. Selection by overlap handles this.
- **Late arrivals stretch a bucket's range.** The bucket that took the April
  backlog lists `start 04-10, end 11-06` with 56k events, mostly from today.
  The listing is accurate (true min and max), but "oldest start" is a bad
  reach estimate when one bucket holds a backlog. The preview should show the
  reach of the main bucket stream and list outlier buckets separately.
- Active buckets report `size=0` and no range; treat them as "now".

## 2. Merging into a destination that already holds data

**Result: works; one trap.**

- The target held 2.43M migrated events and live traffic. After adding 31
  more buckets in several batches, every dataset counted exactly
  (2,429,018 old, 2,057,761 new = all sealed source buckets except two).
- **Bucket ids are not unique across rebuilds.** A clean-install 8.18
  appliance creates its first buckets at install time, so a reverted,
  cloned or rebuilt source reuses ids. Two of the new source buckets had the
  same ids as buckets already on the target, with different content. A tool
  that skips "already registered" ids loses that data silently. The
  prototype compares file manifests and stops (exit 8) on an id that is
  registered with different content. The same check catches an active bucket
  copied early whose source copy kept growing.
- Live ingest during a commit: the target refuses connections for the whole
  stop/start (about 3 minutes per commit). The trickle client saw 585 refused
  batches across the test's restarts. Agents buffer; UDP syslog would be lost.

## 3. Recovering after a crash

Injected hard kills (`kill -9`) at each step:

| Crash point | State left | Target restart effect | Rerun |
|---|---|---|---|
| Mid-transfer | partial dir in staging | ignored | partial removed, bucket copied again |
| After staging, before commit | verified staged dir + manifest | ignored | skipped as already staged |
| During commit, after move, before register | **target service left stopped**; dir in store, not in index | service ignores the unregistered dir (neither adopts nor deletes it) | `commit` verifies the dir against the manifest and registers it |
| Re-register an existing id | none | n/a | refused, harmless (exit code 0, see above) |

After recovery all counts were exact. The one dangerous state is a crash
during commit: a hard kill cannot run the trap that restarts the target, so
the tool's `status` must detect a stopped target and a leftover manifest.
The first prototype (copy straight into the store, skip if the dir exists)
would have turned a partial copy into a silently skipped bucket; staging
removes that failure.

## 4. Storage through the run, not just at the start

- 9.0 has its own budget: `Repository size limit` (default: the
  `/storage/core` partition) minus `reserved-space-percentage` (3 %). The
  runtime log prints it every minute (`Repository space monitor: ...
  available=<bytes>`). A tool should read that, not `df`.
- `max-repository-size` below **2 GiB** stops the service from starting
  (`Allocated repository is too small ... requires at least 2147483648
  bytes`); the node crash-looped every 2.5 minutes until fixed. Never write a
  limit under 2 GiB, and check that the service comes up after any change.
- Over budget, the target retires buckets in registration order (see
  correction 3) and deletes them. So the space check is not just "will the
  copy fit": the budget has to hold the migrated data plus live growth for as
  long as the history must be kept, because migrated buckets are always older
  by registration than anything ingested after cutover. Register in date
  order (oldest data first) so that, if it ever comes to it, the oldest
  history goes first.
- The prototype re-checks free space before each bucket against a reserve;
  the real tool should check against the service's `available` value and a
  projection of live growth.

## 5. Staging area, then add

**Result: proven.** `/storage/core/migrate-staging/<id>.partial` during the
transfer, file manifest (path + size per file) compared with the source,
renamed to `<id>` when verified, then at commit: stop the target, `mv` into
the store (same filesystem, atomic rename), verify the manifest again,
`bucket-index add`, confirm in the index, start the target. The target
ignores the staging directory across restarts. Staging costs no extra space
at commit (rename, not copy). Measured: 20 MB buckets stage in 1-3 s; commit
of 12 buckets took about 50 s plus the 9 service start (2-2.5 min).

## 6. Multi-node clusters and non-default partitions

Not tested. Open questions for a multi-node 9 destination: whether the
bucket list is purely per node, whether a commit can stop one node while the
rest of the cluster keeps ingesting, whether queries on any node see buckets
registered on another, and where to place buckets. For a multi-node 8.18
source: per-node inventories and per-node stops for active buckets.

## Numbers

| | |
|---|---|
| Source | 2,227,824 events, 31 sealed buckets (10-20 MB), 241 days |
| Date selection (back to Aug 6) | 12 buckets, predicted 874,099 events and reach Jul 28; got exactly that |
| Full merge | 2,057,761 + 2,429,018 events on target, per-dataset exact |
| Crash points tested | 4, all recovered with exact counts |
| Commit outage | about 3 min per commit (stop, register, service start) |
| Retirement test | 15 buckets retired in registration order, deleted from disk |
