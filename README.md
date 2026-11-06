# vcf-logs-migrator

Moving log data from **Aria Operations for Logs 8.18** to **VCF Operations for
Logs 9.0** past the 90-day limit of Broadcom's Log Data Transfer.

Status: **research and a spec, no tool yet.** Everything here comes from lab
runs on single-node clusters with synthetic data. It is not supported by
Broadcom. Snapshot both sides before trying any of it.

## The short version

- 8.18 cannot upgrade in place to 9.x. The supported path is a new 9.0
  appliance plus the Log Data Transfer in the 9.0 UI (Administration >
  Control Panel > Logs Data Transfer), capped at the newest 90 days.
- That transfer is a bucket copy under the hood. Copying more buckets by hand
  works: in the lab, **8 months / 2.43M events moved in 44 s of copy, per-day
  counts identical, every field preserved, original timestamps kept.**
- The 9 node can stay running. The 8.18 node only stops while its active
  (unsealed) buckets are copied, about a minute.
- Do **not** run the stock `bucket-tools --bucketMigrationStart` by hand: it
  takes the active bucket by mtime and deletes every bucket on the source.

## What is here

| Path | What |
|---|---|
| [`docs/research-guide.md`](docs/research-guide.md) | The full guide: options (keep 8.18 as cold archive, bucket copy, archive import, replay), what was proven, the runbook with every command |
| [`docs/lab-runs.md`](docs/lab-runs.md) | What was run, when, and the numbers |
| [`docs/spec.md`](docs/spec.md) | Draft spec for `logs-migrate`: plan / copy / verify, date-based preview ("import back to this date") |
| [`scripts/bucket-copy.sh`](scripts/bucket-copy.sh) | Copy-mode bucket transfer, run on the 9 node as root |
| [`scripts/count-events.py`](scripts/count-events.py) | Per-day / per-month event counts over REST, for before and after |
| [`scripts/replay.py`](scripts/replay.py) | Replay events between instances through the agent-format ingest, keeping timestamps |
| [`scripts/synth/`](scripts/synth/) | Synthetic data generators used for the tests (ESXi syslog, mixed syslog) |

## Copy in four steps

From [`docs/research-guide.md`](docs/research-guide.md) section 4.3:

```
# 1. source (8.18), as root: which buckets hold data, and retention settings
bucket-index show
du -sh /storage/core/loginsight/cidata/store/*-*-*-*-*
grep -o '<retention[^>]*>' /storage/core/loginsight/config/loginsight-config.xml#*

# 2. baseline counts
LI_PASS=... LI_OUT=before.json scripts/count-events.py <8.18 node> 300

# 3. copy (stop the source only if active buckets are in the list)
ssh root@<8.18> systemctl stop loginsight
ssh root@<9> "SSHPASS=... /root/bucket-copy.sh <8.18 ip> <bucket id> ..."
ssh root@<8.18> systemctl start loginsight

# 4. verify (wait for the source to finish warming up before comparing)
LI_PASS=... LI_OUT=after.json scripts/count-events.py <9 node> 300
```

Content (dashboards, alerts, extracted fields, agent groups) does not travel
with buckets; move it as a content pack.

## License

MIT
