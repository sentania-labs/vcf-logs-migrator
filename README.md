# VCF Logs Migrator

A shared Python engine and automation CLI for historical log migration from
**Aria Operations for Logs 8.18.x** to **VCF Operations for Logs 9.0.x** and
**9.1.1 preview**, including history older than 90 days.

**Current delivery: engine and CLI with deterministic fixtures. Real appliance
mutation is disabled pending safety and lab qualification.** The GUI and native
installers are separate deliveries. This project is not supported by Broadcom.

The mover is the destination's `/api/v2/data-migrations` API. It accepts arbitrary
time windows and skips active buckets. The engine adds immutable planning,
partition prerequisites, capacity and overlap guards, durable recovery, explicit
verification, optional 9.0 fixup orchestration, and Markdown/JSON evidence.
It never stops the source, deletes buckets, overwrites destination data, or falls
back to manual copying.

## Try the fixture CLI

```sh
python -m pip install .
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture discover
logs-migrate --journal /private/migration/run.sqlite \
  --fixture tests/fixtures/sealed-populated.fixture plan \
  --since 2026-03-01T00:00:00Z --until 2026-04-01T00:00:00Z
```

Use the returned run ID and plan fingerprint with `run`/`copy`, then `verify`.
`status`, `resume`, `stop`, `report`, `fixup`, and `recover-services` share the same
engine and journal. Full commands, exit codes, recovery guidance, and release
gates are in the [CLI operator guide](docs/operator-cli.md).

`COMPLETED` from the appliance is not verified success: it can hide disk-limit
skips. Verification requires complete, agreeing observations and explicit
accounting for pre-existing data and new ingest. Active exclusions remain visible.
A 9.0 tail rerun requires unchanged topology and matching prior buckets; 9.1.1
must not re-import any previously selected bucket, even in an adjacent date window.

## Development

Python 3.11+; the core and CLI do not import Qt. `gui` reserves the optional
PySide6 dependency for the later GUI. `ssh` installs the optional Paramiko adapter.

```sh
python -m pip install '.[dev]'
make check
```

`make check` runs Ruff and pytest. Tests use synthetic fixtures, recorded-transport
contracts, and simulated failures. They do not contact appliances. In particular,
fixture helper behavior does not prove that updating an already-registered vendor
bucket is safe; real fixup is explicitly gated.

## Research and historical evidence

| Document | Evidence |
|---|---|
| [Official API validation](docs/validation-2026-11-06-official-api.md) | 9.0/9.1.1 requests, placement, dedupe, disk skips, active exclusions, partition behavior |
| [Partitions and multi-node validation](docs/validation-2026-11-06-partitions-multinode.md) | Partition registration, creation time, three-node source and target experiments |
| [Validation run](docs/validation-2026-11-06.md) | Date selection, populated targets, retention and interrupted manual transfers |
| [Lab runs](docs/lab-runs.md) | Historical experiments, including the manual 2,429,018-event run |
| [Research guide](docs/research-guide.md) | Original investigation and experimental procedures |
| [Config migration](docs/config-migration.md) | Config API findings; broader config migration is outside this delivery |
| [Specification](docs/spec.md) | Product design and historical appendix; the operator guide describes shipped commands |

The files under `scripts/` are historical research prototypes, not this CLI's
execution path. Some contain source stops, insecure transport or deletion flows.
Do not use them as production operator instructions. The engine invokes none of
them, including the destructive stock bucket migration command.

MIT licensed. No production-readiness claim is made from fixture tests or the
recorded lab results alone.
