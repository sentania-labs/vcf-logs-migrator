# Migration workflow

The current delivery is a tight, fixture-tested wrapper around Broadcom's
`/api/v2/data-migrations` API. Real appliance execution is disabled pending live
adapter qualification. See the [operator guide](operator-cli.md) for commands and
[official API validation](validation-2026-11-06-official-api.md) for historical
appliance evidence, which is separate from this engine's test results.

1. Compare source and destination partitions, including extracted fields for
   9.1.1 preview. Configuration migration is a separate, upcoming delivery.
2. Preview a date interval. Review selected whole sealed buckets, their coverage,
   active exclusions, the last fully sealed day when known and required versus
   available destination repository space per node.
3. Snapshot the destination before continuing; snapshotting the source is
   optional. The CLI records an operator attestation, not snapshot verification.
4. Start the API transfer once. The API moves data directly between appliances;
   the wrapper never copies files, stops the source or deletes destination data.
5. Query API status for task/per-node progress, elapsed time, relative rate and
   ETA. A client interruption does not stop the API task. Explicit cancellation
   targets its observed API task ID.
6. Verify complete, agreeing per-day counts against source counts, the run's
   destination baseline and new ingest. API completion alone is not success.
7. Export a plain Markdown or JSON report. It records count evidence and skipped
   active data; it does not substitute for current API status or authorize removal
   of uncopied source data.

A small local bucket-ID ledger prevents sending a bucket twice, including
adjacent windows selecting the same wide bucket. While a task runs, or if its POST
response was lost, its whole selection is held. Once the API reports the task
finished, the ledger keeps only the buckets it actually transferred, so a stopped,
cancelled or failed task leaves the remainder free to plan again. Ledger updates
happen when `plan`, `copy`, `verify` or a journal-backed `status`/`stop` observes
the finished task. There is no local task state machine, close/reconcile procedure
or tail-rerun bypass.

Verification is cumulative per destination: one result for everything the API has
reported as received so far, with expected counts attributed per received bucket.
Re-running it after a later transfer recomputes the cumulative result and never
turns an earlier success into a discrepancy. The result lists covered and
not-received buckets and covered days; a running transfer or any not-received
bucket makes it incomplete.

Active buckets may never seal after ingestion moves away. Leave uncopied data
available under the operator's retention plan. Fixup is set aside and hidden until
migration qualification is complete; this delivery performs no service stops.

The older workflow illustration and design sections in `spec.md` are historical
material, not instructions for the shipped CLI.
