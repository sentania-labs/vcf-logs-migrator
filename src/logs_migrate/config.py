"""Selected, additive configuration migration. No journal or task controller."""

from copy import deepcopy
from typing import Protocol

from .errors import Code, MigrationError, require, safe_boundary
from .models import canonical

# Apply ingest configuration before forwarders.
RULE_TYPES = ("partitions", "filters", "masking", "forwarding")
RESTART_NOTICE = (
    "New partitions need one destination restart before routing. "
    "Arrange it through normal maintenance; this tool never restarts services."
)
TLS_NOTICE = "TLS forwarders use acceptCert:true, which can add a destination trusted certificate."
SELF_NOTICE = "9.0 refuses forwarders targeting itself; aliases are also checked by the appliance."


class ConfigEnvironment(Protocol):
    """Source has only a read operation; writes always target the destination.

    Rules are JSON objects from the API, keyed by their exact, case-sensitive name.
    Adapters must reject ambiguous/malformed inventories, never treat them as empty.
    The release gate permits simulation only, including recorded REST transports.
    """

    mode: str

    def exclusive(self): ...
    def config_rules(self, scope: str, kind: str) -> list[dict]: ...
    def config_write(self, kind: str, rule: dict, existing: dict | None) -> None: ...


def indexed(rules):
    require(isinstance(rules, list), Code.SCHEMA)
    result = {}
    for rule in rules:
        require(
            isinstance(rule, dict) and isinstance(rule.get("name"), str) and bool(rule["name"]),
            Code.SCHEMA,
        )
        require(rule["name"] not in result, Code.SCHEMA)
        result[rule["name"]] = deepcopy(rule)
    return result


def comparable(kind, rule):
    """Only known identity/request-only fields are omitted. Unknown settings still differ."""
    result = deepcopy(rule)
    result.pop("id", None)
    if kind == "forwarding":
        result.pop("acceptCert", None)
        result.pop("testConnection", None)
        if isinstance(result.get("transportProtocol"), str):
            result["transportProtocol"] = result["transportProtocol"].lower()
    return result


def write_body(kind, rule, existing):
    body = deepcopy(rule)
    if existing is not None:
        require(isinstance(existing.get("id"), str), Code.SCHEMA)
        body["id"] = existing["id"]
    elif kind != "forwarding":
        body.pop("id", None)
    if kind == "forwarding" and body.get("sslEnabled") is True:
        body["acceptCert"] = True
    return body


class ConfigMigration:
    """GUI/CLI service: compare(), then apply(['masking:name', ...]).

    apply re-reads both inventories under the adapter's local lock. Explicit
    selectors authorize the current source definition, including updates. It does
    not replay writes, delete destination-only rules, or persist a second plan.
    Results contain names, field names and fixed codes, never raw rule settings.
    """

    def __init__(self, environment: ConfigEnvironment):
        self.env = environment

    def _inventory(self):
        source, destination, rows = {}, {}, []
        for kind in RULE_TYPES:
            source[kind] = indexed(self.env.config_rules("source", kind))
            destination[kind] = indexed(self.env.config_rules("destination", kind))
            for name, rule in sorted(source[kind].items()):
                other = destination[kind].get(name)
                wanted = comparable(kind, rule)
                current = comparable(kind, other) if other is not None else {}
                changed = sorted(
                    k
                    for k in wanted.keys() | current.keys()
                    if k not in wanted
                    or k not in current
                    or canonical(wanted[k]) != canonical(current[k])
                )
                warnings = []
                if kind == "partitions" and other is None:
                    warnings.append(RESTART_NOTICE)
                if kind == "forwarding":
                    warnings.append(SELF_NOTICE)
                    if rule.get("sslEnabled") is True:
                        warnings.append(TLS_NOTICE)
                rows.append(
                    {
                        "selector": kind + ":" + name,
                        "type": kind,
                        "name": name,
                        "status": "missing"
                        if other is None
                        else "differs"
                        if changed
                        else "identical",
                        "changed_fields": changed,
                        "warnings": warnings,
                    }
                )
        return source, destination, rows

    @safe_boundary
    def compare(self):
        with self.env.exclusive():
            source, destination, rows = self._inventory()
            return {
                "rules": rows,
                "destination_only": [
                    {"type": kind, "name": name}
                    for kind in RULE_TYPES
                    for name in sorted(destination[kind].keys() - source[kind].keys())
                ],
            }

    @safe_boundary
    def apply(self, selection):
        require(self.env.mode == "simulation", Code.UNSUPPORTED)
        require(isinstance(selection, (list, tuple)) and bool(selection), Code.INVALID_INPUT)
        require(all(isinstance(s, str) for s in selection), Code.INVALID_INPUT)
        require(len(set(selection)) == len(selection), Code.INVALID_INPUT)
        with self.env.exclusive():
            source, destination, rows = self._inventory()
            require(set(selection) <= {r["selector"] for r in rows}, Code.INVALID_INPUT)
            results = []
            for row in rows:
                result = {**row, "comparison": row["status"], "restart_required": False}
                results.append(result)
                if row["selector"] not in selection:
                    result["status"] = "skipped"
                    continue
                if row["status"] == "identical":
                    result["status"] = "unchanged"
                    continue
                kind, name = row["type"], row["name"]
                rule, existing = source[kind][name], destination[kind].get(name)
                try:
                    write_body(kind, rule, existing)
                except MigrationError as error:
                    result.update(status="failed", reason=error.code.value)
                    continue
                try:
                    self.env.config_write(kind, rule, existing)
                except MigrationError as error:
                    # A transport/auth error can follow an accepted write. No automatic retry.
                    uncertain = error.code not in {
                        Code.INVALID_INPUT,
                        Code.UNSUPPORTED,
                        Code.INVENTORY_CHANGED,
                    }
                    result.update(
                        status="uncertain" if uncertain else "failed", reason=error.code.value
                    )
                except Exception:
                    result.update(status="uncertain", reason=Code.UNCERTAIN.value)
                else:
                    try:
                        actual = indexed(self.env.config_rules("destination", kind)).get(name)
                        require(
                            actual is not None
                            and canonical(comparable(kind, actual))
                            == canonical(comparable(kind, rule)),
                            Code.INCOMPLETE,
                        )
                    except Exception:
                        result.update(status="uncertain", reason=Code.INCOMPLETE.value)
                    else:
                        result["status"] = "created" if existing is None else "updated"
                result["restart_required"] = (
                    kind == "partitions"
                    and existing is None
                    and result["status"] in {"created", "uncertain"}
                )
            restart = any(r["restart_required"] for r in results)
            return {
                "rules": results,
                "complete": all(r["status"] not in {"failed", "uncertain"} for r in results),
                "restart_required": restart,
                "maintenance": RESTART_NOTICE if restart else None,
            }
