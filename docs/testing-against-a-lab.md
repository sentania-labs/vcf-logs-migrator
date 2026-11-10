# Testing against a lab

A step-by-step for a human or an agent with two real appliances: an Aria Operations
for Logs 8.18.x source and a VCF Operations for Logs 9.0.x destination (9.1.1 is a
preview; see the end). Every step before "Start the transfer" is read-only.

## What you need

| Item | Why |
|---|---|
| One 8.18 cluster member address | Logs REST on 9543 and root SSH on 22. The tool reads the member list from that node's `loginsight-config.xml` and then needs SSH to every member. |
| 8.18 Logs API user and password | `POST /api/v1/sessions`, provider Local. Read-only calls. |
| 8.18 root SSH password | `bucket-index show`, `du`, `df`, `systemctl is-active`, `cat` of the config file. The destination also needs it: the API sends it to 9.0 so the destination can pull buckets. |
| 9.0 Logs address, `host[:port]` | Default port 9543. |
| 9.0 Logs API user and password | data-migrations test/start/status/cancel, partitions, counts, config rules. |
| 9.0 root SSH (optional) | Adds each destination node's repository budget and bucket inventory to the plan. Without it, the API's own 95% disk stop is the only storage guard and the plan says so. |
| A workstation with the [download](../README.md#download), or Python 3.11+ | The download needs no Python; run it wherever these commands say `logs-migrate`. From source: `python -m pip install '.[ssh]'` (add `gui` for the window). |
| Snapshots of both appliances | The transfer has no undo. |

Secrets never go on the command line. Give them one of three ways:

```sh
# 1. Environment variables, read without echo so they never enter shell history
read -rs -p 'Source API password: ' LOGS_SOURCE_API_PASSWORD; echo; export LOGS_SOURCE_API_PASSWORD
read -rs -p 'Source SSH password: ' LOGS_SOURCE_SSH_PASSWORD; echo; export LOGS_SOURCE_SSH_PASSWORD
read -rs -p 'Destination API password: ' LOGS_DESTINATION_API_PASSWORD; echo; export LOGS_DESTINATION_API_PASSWORD
# optional: LOGS_DESTINATION_SSH_PASSWORD; 9.1.1 only: LOGS_OPS_TOKEN

# 2. A private file (mode 600) written in an editor, never with echo or printf:
#    JSON keyed source_api, source_ssh, destination_api, destination_ssh, ops_token
umask 077; "${EDITOR:-vi}" ~/.logs-creds.json
logs-migrate --credentials ~/.logs-creds.json ...

# 3. A prompt, when nothing above supplies a value and you are on a terminal
```

Addresses and user names can also come from `LOGS_SOURCE`, `LOGS_DESTINATION`,
`LOGS_SOURCE_API_USER` (default admin), `LOGS_SOURCE_SSH_USER` (default root),
`LOGS_DESTINATION_API_USER` (default admin), `LOGS_DESTINATION_SSH_USER` and
`LOGS_OPS_URL`.

## Step 1: enroll the appliance identities

The first contact shows each TLS certificate and SSH host key fingerprint and asks
you to trust it. Nothing is sent until you do, and on the run that first sees an
identity no credential is sent to it at all. On a terminal you get a `[y/N]` prompt
per identity, where you confirm the fingerprint you read off the appliance. In
automation there are two ways, both without a human at a prompt:

- `--trust-new` probes, enrolls and prints the identities it saw, then **exits
  without sending any credential**. Verify the printed fingerprints against the
  appliances (the TLS certificate on the appliance console or `openssl x509
  -fingerprint -sha256`, the host key with `ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub`
  on the node), then run the same command again; that second run talks only to
  pinned identities.
- `--expect-fingerprint SHA256` (repeatable, hex with or without colons) carries a
  fingerprint you verified beforehand. An unknown identity that presents it is
  enrolled and the run proceeds in one go; any other unknown identity is refused.

```sh
export LOGS_SOURCE=aria-logs-1.lab.example LOGS_DESTINATION=vcf-logs.lab.example
logs-migrate --trust-new selftest        # probes, prints, enrolls, exits; no credential sent
logs-migrate selftest                    # second run, against the pinned identities
```

Correct output of the first run starts with one line per identity and ends with a
JSON line saying what was enrolled and that no credential was sent:

```
Unknown appliance identity: TLS aria-logs-1.lab.example:9543 SHA256 3f:0a:...
Enrolled (--trust-new); no credential is sent on this run.
Unknown appliance identity: SSH aria-logs-1.lab.example:22 (ssh-ed25519) SHA256 9c:...
Enrolled (--trust-new); no credential is sent on this run.
{"enrolled": [...], "credentials_sent": false, "next": "Verify each fingerprint ... run the same command again without --trust-new."}
```

Cluster members found in the config file are seen only once the typed member's SSH
identity is pinned, so with `--trust-new` a multi-node source takes one more
enroll-and-exit run before the first full run; with `--expect-fingerprint` pass every
member's host key fingerprint.
A probe that fails prints which host it was contacting, for example
`{"stage": "tls aria-logs-1.lab.example:9543", "error": "connection_failed"}`, and
nothing from the remote side.

When several SSH hosts present the same host key (common for appliances cloned from
one image), the CLI prints `SSH host X presents the same key as Y.` to stderr and the
trust dialog adds the same line. Each host is still enrolled separately; confirm the
sharing is expected.

Enrolled identities live in `~/.config/vcf-logs-migrator/trusted-identities.json`
and `known_hosts` (mode 600). `logs-migrate trust list` shows them;
`logs-migrate trust forget ssh host:22` removes one. A changed identity is refused
with `authentication_failed` until you forget the old entry.

## Step 2: run the self-test (read-only)

```sh
logs-migrate selftest
```

Correct output looks like this (hosts and numbers will differ):

```
PASS  Source API version                                 8.18.7
PASS  Source partitions                                  2 partitions
PASS  Source cluster members (SSH)                       3 members: aria-logs-1.lab.example, aria-logs-2.lab.example, aria-logs-3.lab.example
PASS  Node aria-logs-1.lab.example (SSH listing, du, df, service)  service running · 44 sealed (44 with dates), 2 active · 46 sized by du · 118.2 GB free · listing keys: created, endtime, id, messages, partition, size, starttime, status
PASS  Node aria-logs-2.lab.example (SSH listing, du, df, service)  ...
PASS  Node aria-logs-3.lab.example (SSH listing, du, df, service)  ...
PASS  Source inventory model                             8.18.7 · 3 node(s) · 88 sealed buckets · 2025-01-01 to 2025-06-22
PASS  Destination API version                            9.0.2.0200
PASS  Destination partitions                             2 partitions
PASS  Destination data-migrations status                 task idle · 0 node entries · keys: ...
PASS  Destination inventory model                        9.0.2.0200 · 1 node(s) · 0 buckets listed · budget known for 0
PASS  Version compatibility                              8.18.7 → 9.0.2.0200
PASS  Partition comparison                               0 missing, 0 differ
PASS  Source daily counts                                5,414,021 events on 30 days (2025-10-08 to 2025-11-06) · complete
PASS  Destination daily counts                           0 events on 0 days (...) · complete
PASS  Source ingest (cutover check)                      no active bucket growth in 5 seconds
PASS  Plan preview (no request sent)                     88 buckets, 4.21 GB, covers 2025-01-01 to 2025-06-22 · 2 active skipped · last day covered only by sealed buckets 2025-06-21 · destination budget unknown

Summary: 17 passed, 0 failed, 0 skipped. All checks passed.
Read-only: nothing was started, written or stopped.
```

What to look at:

- **Node lines.** `N sealed (N with dates)` must match. A sealed bucket without
  dates means the listing format differs from what the parser knows; the `listing
  keys` on that line are the key names it saw. Report them; nothing can be planned
  until they are mapped.
- **Destination data-migrations status.** The `keys:` list is the shape of the
  status body. The decoder is lenient; report those names so it can be tightened.
- **Source ingest.** Non-zero means the source is still receiving events. That is a
  warning, not a block: open buckets keep growing and a later run of the same dates
  picks up the tail once they seal.
- **Plan preview.** `destination budget unknown` means no destination SSH; add
  `--destination-ssh-user root` and the password to see each node's budget.

`selftest --json` prints the same as JSON (`steps`, `summary`, `passed`, `notes`).
Exit 0 means every check passed; exit 1 means at least one failed.
`--since/--until` run the plan preview over those dates instead of all sealed history.

## Step 3: compare and apply configuration (optional, writes to the destination)

```sh
logs-migrate config compare
logs-migrate config apply --select 'partitions:secops'
```

Partitions must match before a transfer (on 9.1 they decide routing). A created
partition needs one destination restart, which the tool never performs.

## Step 4: preview a plan

```sh
logs-migrate --journal ~/migration/lab.sqlite plan \
  --since 2025-01-01T00:00:00Z --until 2025-07-01T00:00:00Z
```

The JSON names the run id, the selected sealed buckets (whole buckets; coverage can
extend past the dates), active buckets skipped, the last day covered only by sealed buckets, the
partition comparison, the storage check and the warnings. `partial_days` lists the
requested days at or after the sealed frontier and days shared with unselected
buckets; their counts are expected to differ at verification.

## Step 5: start the transfer (writes to the destination)

Snapshot the destination first. Then:

```sh
logs-migrate --journal ~/migration/lab.sqlite copy RUN_ID \
  --ack-backup \
  --ack-active "i understand you aren't going to copy the latest data"
```

Before the POST the tool re-reads both inventories, refuses if anything changed,
checks every source node's SSH login, calls `POST /api/v2/data-migrations/test`,
records the per-day source counts and the destination baseline over the requested
UTC days (two agreeing complete observations each, 30 s apart), writes the selection to
the journal, and only then sends `POST /api/v2/data-migrations` once. Correct output
is the task as the API reports it (`"status": "running"`).

## Step 6: watch and verify

```sh
logs-migrate --journal ~/migration/lab.sqlite status     # repeat; reads the API each time
logs-migrate --journal ~/migration/lab.sqlite verify RUN_ID
logs-migrate --journal ~/migration/lab.sqlite report RUN_ID
```

`status` shows the API task, per-node entries, bytes and the buckets it reports.
`verify` waits for a finished task, counts the destination over the requested UTC days
again (two agreeing complete observations), subtracts the baseline and compares
with the source per day. Exit 0 is `verified_success`: every fully covered day
matches. Partial days (shared with an unselected bucket, or at or after the sealed
frontier where open buckets hold the newest data) are listed with their differences
and do not decide the outcome. Exit 5 is a difference on a fully covered day,
including new ingest that landed on those dates, which counts cannot tell apart from
migrated events. Exit 6 is incomplete evidence (task still running, counts not
complete, nothing received, or no fully covered day at all).

A 9.0 task can report COMPLETED after skipping buckets at its disk limit; the
verification is what says whether the dates landed.

## Running it as an agent

Everything above is non-interactive with environment variables or a `--credentials`
file, `--json` on `selftest`, and either `--expect-fingerprint` for fingerprints the
operator handed you, or `--trust-new` runs that enroll and exit before any credential
is sent (repeat until a run no longer reports `credentials_sent: false`). The
read-only sequence is:

```sh
logs-migrate --trust-new selftest --json     # repeat while it exits with enrolled identities
logs-migrate selftest --json
logs-migrate discover
logs-migrate partitions
logs-migrate --journal J plan --since ... --until ...
```

Never run `copy`, `stop` or `config apply` without an operator's instruction; they
write to the destination.

## The desktop window

`logs-migrate gui` with an empty fixture path in Settings connects to appliances.
Enter the addresses and passwords on Connections and press Test; unknown identities
open a dialog with the fingerprints. The Connections page lists the same checks as
the self-test (Run self-test button) and the rest of the workflow is unchanged:
Discovery & Plan, Start, Migration, Verification, Reports.

## 9.1.1 preview

Set `--ops-url https://ops.lab.example` (or fill the VCF Operations URL field on the
destination card of the Connections page, which reveals the token field) and supply
`LOGS_OPS_TOKEN`. The tool exchanges the Ops token for a Logs JWT at
`POST /suite-api/api/auth/token/exchange` and sends `X-JWT-Token` to the Logs FQDN on
9543. What is not qualified: the 9.1 query route for counts (a transfer can start, but
no destination baseline is recorded and verification reports `unsupported_capability`),
the version route on the log-processor (the self-test says when the version is
assumed) and partition routing of migrated events. The plan says so before you start:
count verification is not available for 9.1.1, because no 9.1 query route has been
captured and a guessed route would prove nothing. Against a 9.1.1 destination the
self-test adds a read-only "Destination 9.1 count query probe" step that prints the
key names of the response (never values) and keeps them in `notes`; send that line
back so the route can be built from real evidence. A 9.0 to 9.1.1 lab run has not been
recorded.

## What the self-test cannot tell you

- Whether the destination can reach every source node on port 22. The API's own
  test call (`copy` runs it before starting) is the check for that.
- Whether the status body's bucket lists are complete. If the API reports no
  per-bucket detail, the selection minus whatever the API lists as skipped stays
  recorded as sent, for completed, cancelled and failed tasks alike, so it is never
  sent twice; the counts then show what landed.
- The ledger is keyed on the destination's TLS certificate, not on the address you
  typed, so a VIP, a node name and an IP of the same endpoint share one record. Two
  nodes with different certificates do not.
- The source member list must be readable from the typed node's config file; if it is
  not, the connection fails (stage `ssh <host>:<port> (members)`) rather than planning from one node.
