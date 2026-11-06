# Migration workflow

How a migration from Aria Operations for Logs 8.18 to VCF Operations for
Logs 9.0 is meant to run with `logs-migrate`.

**Status:** `logs-migrate` is planned, not built; see [`spec.md`](spec.md).
The transfer API underneath it, Broadcom's `/api/v2/data-migrations`, exists
today and has been tested (see
[`validation-2026-11-06-official-api.md`](validation-2026-11-06-official-api.md)).

The example has two 3-node clusters and six months of history on 1 TB. The
numbers in the diagram are the order of work.

![Migration workflow](images/migration-workflow.svg)

## Steps

1. **Build the new cluster** next to the old one. 8.18 cannot be upgraded in
   place to 9.x ([`research-guide.md`](research-guide.md) section 1). Give it
   more storage than the source: it holds the migrated history plus live
   ingest from the cutover onward.
2. **Prepare: configuration.** This is a separate phase, not part of the
   tool. Follow the order in [`config-migration.md`](config-migration.md):
   - Auth and identity first, by hand. Those secrets cannot be exported.
   - Partitions next, then restart the new cluster once. Matching partitions
     have to exist before any data arrives.
   - Then filters, masking rules, content, datasets and roles, alerts, and
     agent groups by REST.
   - SMTP, the query forwarder, integrations, certificates and licenses are
     manual.
   - Forwarders wait until after step 3.

   Optionally, turn on NFS archiving on the old cluster. It only covers
   buckets sealed from then on, so it does not protect existing history.
3. **Cut over the VIP.** Point the ingest name (`logs.example.com` here) and
   any agents at the new cluster. New events now land on 9.0. Add the
   forwarders now.
4. **Migrate history.**
   - `logs-migrate plan` takes a date ("import back to this date"). It shows
     the buckets that reaches, their size and event counts, and the last
     fully sealed day.
   - The tool then starts Broadcom's transfer, which takes any time window.
     The 90-day limit is only in the UI.
   - The destination nodes pull sealed buckets straight from every source
     node over SSH. The data never passes through the machine running the
     tool.
5. **Validate.** `logs-migrate verify` compares per-day event counts up to
   the last fully sealed day. On 9.0 you can then run the optional fix-up:
   - It puts buckets back in their source partitions and gives them their
     real creation times.
   - It stops one destination node at a time, for about a minute each.
6. **Retire the old cluster** once the active-bucket tail is handled (see
   below): either copied with the source stopped, or left on the old cluster
   until it ages out.

## Things to know

- **Active buckets stay behind, and after the cutover they never seal.**
  The transfer only copies sealed buckets, and a bucket seals only when it
  hits its size cap. Restarting 8.18 does not seal it. Once ingest has moved
  to the new cluster, the source's active buckets stop growing, so waiting
  and re-running the transfer will never collect them. On a quiet partition
  that tail can be days or weeks of events; in the lab it was two weeks.
  Two ways to finish:
  - **Copy the active buckets with the source stopped.** This is proven:
    stop `loginsight` on each source node, copy its active buckets to the new
    cluster, register them, start the source again. It took about 30 seconds
    of source downtime per node in the lab
    ([`research-guide.md`](research-guide.md) section 4.2). After the cutover
    the source takes no new events, so nothing is lost while it is down. This
    is the case for the tool's fallback copy path.
  - **Leave the tail on the old cluster** and keep it read-only until that
    data ages out.
- **COMPLETED does not mean everything moved.** At 95% disk use a node
  stops copying, skips the rest, and still reports COMPLETED. Verify catches
  it; size the new cluster so it does not happen.
- **Every source node must answer on SSH port 22** from the new cluster's
  nodes, with the root password given to the transfer. Any cluster member
  works as the source address; the transfer finds the rest from that node's
  config.
- **Do not change the new cluster's node count between transfer runs.** The
  transfer only checks the node it assigns each bucket to, so a different
  node count can copy a bucket twice.
- **Wide buckets.** A bucket is selected when its time range overlaps the
  window. One stray old event can stretch a bucket's range, and the whole
  bucket comes along. The plan lists them.
- **9.1 destinations** are out of scope for the first version. Reading the
  code suggests the transfer routes each event through the partition filters
  there, so no fix-up would be needed; this is not yet proven live. On 9.1,
  retention runs from the import day, not the event date.

Details and evidence: [`spec.md`](spec.md) and
[`validation-2026-11-06-official-api.md`](validation-2026-11-06-official-api.md).
