# Validation run 2, 2026-11-06: partitions, creation time, multi-node

Follow-up to [`validation-2026-11-06.md`](validation-2026-11-06.md). Same lab
pair, then grown to a 3-node 8.18.7 source cluster and a 3-node 9.0.2.0200
destination cluster. Config migration (forwarders, masking, users, and so on)
is a separate report: [`config-migration.md`](config-migration.md).

## 1. Non-default index partitions

Setup: partition `secops` on the 8.18 source, routing filter `appname="Vpxa"`
(`POST /api/v1/partitioning`). **A new partition only routes after
`systemctl restart loginsight`, and only events ingested after that land in
it**; older events stay where they are.

Facts found:

- **Partition ids come from the name**: `UUID.nameUUIDFromBytes(name)` (Java,
  MD5-based). `Default` = name `""` = `d41d8cd9-8f00-3204-a980-0998ecf8427e`;
  `secops` = `d82bb6e4-bcaf-305b-9b55-30924b7f9cb6` on both clusters. A partition
  created with the same name on the destination has the same id.
- **`bucket-index add` always registers into Default.** It has no partition
  option (`add` and `modify` take only statuses, sizes, creation time). With or
  without a same-named partition on the destination, migrated partition
  buckets became Default buckets: fully searchable, but under Default's
  retention and outside searches scoped to the partition.
- **The engine supports partitions.** `ingestion-service.jar` class
  `com.vmware.loginsight.ingestion.BucketIndex` has
  `addNewBucket(String partitionId, BucketId)` and
  `setBucketStatus(BucketId, String partitionId, BucketStatus, Long size)`. The
  CLI (`BucketIndexConsole$AddBucketsTask`) just calls the overloads without a
  partition.
- The bucket catalog is a Lucene index directory
  (`/storage/core/loginsight/cidata/store/buckets`) with a writer lock
  (`strata_writer.lock`) held by the running service. That is why registering
  with the service running loses entries (validation 1).

### The fix: partition-aware registration (PROVEN)

[`scripts/PartitionBucketAdd.java`](../scripts/PartitionBucketAdd.java) and
[`scripts/partition-add.sh`](../scripts/partition-add.sh): a 90-line program
that calls the partition overloads through reflection (no vendor jars needed
to compile; runs on the appliance JRE against the appliance's own
`ingestion-service.jar`). It mirrors the CLI: open `BucketIndex(true)`,
`addNewBucket(partition, id)`, `setBucketStatus(id, partition, ARCHIVED,
size)`, `commit()`, `close()`. It computes the bucket size by walking the
directory (the vendor size helper returns 0 outside its CLI) and updates an
existing entry in place (same partition) instead of adding twice.

```
javac --release 11 PartitionBucketAdd.java          # appliance JRE is 11
scp PartitionBucketAdd.class partition-add.sh root@<9 node>:/root/
# on the 9 node, service stopped:
systemctl stop loginsight
/root/partition-add.sh <partitionId> archived <createTimeMillis> <bucketId>[,...]
systemctl start loginsight
```

`partition-add.sh` sources `loginsight.conf` first. Without
`PROJECTS_ROOT=/storage/core/loginsight` the program silently writes a new,
empty catalog under `/usr/lib/loginsight/projects/` (seen once, removed).

Results on 9.0:

- Four `secops` buckets registered with `partition=d82bb6e4-...`; 253,844 events
  searchable, exactly the sum of their `messages`.
- **Membership is real, not cosmetic**: with `secops` retention set to 110
  days (and a restart), 9.0 retired exactly the two buckets whose data ended
  before the cutoff (Jul 12 and Jul 15) and deleted them from disk; the two
  August buckets and all Default data (2,429,018 events) were untouched.
- Service stopped 8 seconds for the registration step itself.

## 2. Creation time: fixing retirement order (PROVEN)

Validation 1 found that 9.0 retires buckets by creation time, which for a
migrated bucket is the moment it was registered, so migrated history is
retired before any live data. `bucket-index add -t <millis>` (and the
program above) set the creation time. Registering with **creation time =
the bucket's data endTime** (from `bucket-index show` on the source) puts
migrated buckets in their true place: the listing showed
`created=2026-07-12T19:44:57` for a bucket whose data ends then, and partition
retention retired it by that date. Use this for every migrated bucket.

## 3. Copied while active, then sealed

Two `secops` buckets were copied while still active (source stopped), then
the source kept writing and sealed them with more events (65,207 and 58,556
instead of the copied snapshots). The target copy is then stale under the
same id. The manifest check catches it; the fix is
`bucket-index delete` on the target (removes entry and files), copy the
sealed version, register again. Re-registering a bucket that is correct but
in the wrong partition: move its directory aside, `bucket-index delete`, move
it back, register with the partition.

## 4. Multi-node destination (3-node 9.0 cluster)

Cluster join on these builds is token-based:

```
GET  /api/v2/deployment/token                       (primary, admin bearer) -> {"token": ...}
POST /api/v2/deployment/join  {"masterFQDN": "<primary fqdn>", "acceptCert": true}
     on the new node, header Authorization: Bearer <token>
```

The token pre-approves the node (`/deployment/approve` afterwards returns
"Cannot approve worker membership"); the v1 join without a token is denied
("Master denied join request ... 400"). Same on 8.18.7 and 9.0.2.

Findings:

- **The bucket catalog is per node.** A new worker's `bucket-index show` was
  empty; each node registers only buckets in its own store.
- **Queries fan out**: buckets registered on a worker are searchable through
  the primary right after that worker's service restarts.
- **Commit can stop one node.** Staging and registering on a worker stopped
  only that worker; the cluster stayed up. It is not free: ingest sent to the
  primary got HTTP 500 for part of each worker restart (see numbers). Agents
  retry; a migration plan should still commit node by node in a quiet window.
- Placement is the tool's choice: spread buckets across nodes by each node's
  own space budget. 9.0 did not move buckets between nodes.

## 5. Multi-node source (3-node 8.18 cluster)

- Each source node has its own store and bucket list; inventory and copy from
  every node (root SSH to each, as Broadcom's transfer also requires).
- **Events sent to one node are spread across the cluster.** Two datasets
  were sent only to the two workers; afterwards the primary had also gained
  buckets in that window, and the workers' sealed buckets held only part of
  each dataset (src-n2: 241,682 of 492,155 events from the worker's sealed
  buckets). So "one source host's logs live on node X" is false: take all
  nodes, verify cluster-wide.
- Partition and bucket-size config replicate to joined nodes.

## Numbers

| | |
|---|---|
| Partition registration | 4 buckets into `secops`, 253,844 events exact, 8 s service stop |
| Partition retention check | 2 of 4 retired exactly per data date; Default untouched |
| Destination cluster | 3 nodes, per-node catalogs, fan-out queries, single-node commits |
| Live ingest during 2 worker commits | 70 batches (about 140 s) got HTTP 500 from the primary, all inside the commit windows, none after |
| Source cluster | 3 nodes; one dataset spread over all nodes |
