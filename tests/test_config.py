import json
from copy import deepcopy
from pathlib import Path

import pytest

from logs_migrate.adapters.config import ROUTES, RestConfigEnvironment
from logs_migrate.adapters.rest import Logs90, Logs911, RecordedTransport
from logs_migrate.adapters.simulated import SimulatedEnvironment
from logs_migrate.cli import main
from logs_migrate.config import RULE_TYPES, ConfigMigration
from logs_migrate.engine import Engine
from logs_migrate.errors import Code, MigrationError
from logs_migrate.security import Secret

FIXTURE = Path(__file__).parent / "fixtures" / "config.fixture"


@pytest.fixture
def config_env():
    return SimulatedEnvironment.from_file(FIXTURE)


def test_compare_selection_updates_and_source_untouched(config_env):
    engine = Engine(config_env, None)
    source = deepcopy(config_env.data["source"])
    destination = deepcopy(config_env.data["destination"])
    preview = engine.config_compare()
    states = {r["selector"]: r["status"] for r in preview["rules"]}
    assert states == {
        "partitions:Archive": "missing",
        "partitions:Default": "identical",
        "filters:drop-debug": "differs",
        "filters:drop-test": "missing",
        "masking:mask-secret": "missing",
        "forwarding:archive": "missing",
    }
    assert preview["destination_only"] == [{"type": "filters", "name": "local-only"}]
    assert not config_env.data["trace"]
    result = engine.config_apply(["masking:mask-secret", "filters:drop-debug"])
    assert result["complete"] and not result["restart_required"]
    assert {r["selector"]: r["status"] for r in result["rules"] if r["status"] != "skipped"} == {
        "masking:mask-secret": "created",
        "filters:drop-debug": "updated",
    }
    assert config_env.data["source"] == source
    assert config_env.data["destination"]["partitions"] == destination["partitions"]
    assert config_env.config_rules("destination", "forwarding") == []
    assert (
        config_env.config_rules("destination", "filters")[1] == destination["config"]["filters"][1]
    )
    assert len(config_env.data["trace"]) == 2
    again = engine.config_apply(["masking:mask-secret", "filters:drop-debug"])
    assert all(r["status"] in {"skipped", "unchanged"} for r in again["rules"])
    assert len(config_env.data["trace"]) == 2


@pytest.mark.parametrize("version", ["9.0.2.0200", "9.1.1-preview"])
def test_all_types_apply_restart_notice_and_tls(config_env, version):
    config_env.data["destination"]["version"] = version
    engine = Engine(config_env, None)
    selectors = [r["selector"] for r in engine.config_compare()["rules"]]
    result = engine.config_apply(selectors)
    assert result["complete"] and result["restart_required"]
    assert "normal maintenance" in result["maintenance"]
    assert config_env.data["trusted_forwarder_hosts"] == ["archive.invalid"]
    assert all(n["service"] == "running" for n in config_env.data["destination"]["nodes"])
    assert all(t["operation"] == "config.write" for t in config_env.data["trace"])
    assert all(r["status"] == "identical" for r in engine.config_compare()["rules"])
    # The same partition inventory is consumed by data migration preflight.
    assert engine.discover()["partition_diff"]["missing"] == []


@pytest.mark.parametrize(
    "selection",
    [
        [],
        "masking:mask-secret",
        ["filters:unknown"],
        ["filters:drop-debug", "smtp:no"],
        ["filters:drop-debug"] * 2,
        [None],
    ],
)
def test_bad_selection_has_no_partial_writes(config_env, selection):
    with pytest.raises(MigrationError, match=Code.INVALID_INPUT):
        Engine(config_env, None).config_apply(selection)
    assert config_env.data["trace"] == []


def test_duplicate_names_fail_closed(config_env):
    config_env.data["source"]["config"]["masking"] *= 2
    with pytest.raises(MigrationError, match=Code.SCHEMA):
        Engine(config_env, None).config_apply(["partitions:Archive"])
    assert config_env.data["trace"] == []


def test_per_rule_failures_uncertainty_and_no_retries(config_env):
    config_env.data["faults"] = {
        "config.write.partitions.after": "connection_failed",
        "config.write.masking.before": "invalid_input",
    }
    result = Engine(config_env, None).config_apply(
        ["partitions:Archive", "masking:mask-secret", "filters:drop-test"]
    )
    statuses = {r["selector"]: r["status"] for r in result["rules"]}
    assert statuses["partitions:Archive"] == "uncertain"
    assert statuses["masking:mask-secret"] == "failed"
    assert statuses["filters:drop-test"] == "created"
    assert not result["complete"] and result["restart_required"]
    assert len(config_env.data["trace"]) == 2
    assert "Archive" in [p["name"] for p in config_env.config_rules("destination", "partitions")]


def test_readback_mismatch_never_claims_success(config_env):
    config_env.config_write = lambda *args: None
    result = Engine(config_env, None).config_apply(["masking:mask-secret"])
    assert not result["complete"]
    assert next(r for r in result["rules"] if r["type"] == "masking")["status"] == "uncertain"


def test_self_forwarding_is_reported_without_mutation(config_env):
    config_env.data["source"]["config"]["forwarding"][0]["host"] = "DST-1."
    result = Engine(config_env, None).config_apply(["forwarding:archive"])
    assert result["rules"][-1]["status"] == "failed"
    assert result["rules"][-1]["reason"] == Code.INVALID_INPUT
    assert config_env.data["trace"] == []


def test_live_engine_and_rest_are_gated_before_network(config_env):
    config_env.mode = "live"
    with pytest.raises(MigrationError, match=Code.UNSUPPORTED):
        Engine(config_env, None).config_apply(["masking:mask-secret"])

    class NoNetwork:
        def request(self, *args, **kwargs):
            pytest.fail("live call escaped release gate")

    source = Logs90(NoNetwork(), "admin", Secret("test"))
    destination = Logs911(NoNetwork(), NoNetwork(), Secret("test"), lambda x: "test")
    rest = RestConfigEnvironment(source, destination, "9.1.1")
    with pytest.raises(MigrationError, match=Code.UNSUPPORTED):
        ConfigMigration(rest).compare()
    for path, _ in ROUTES.values():
        for method, route in [
            ("GET", path),
            ("POST", path),
            ("PUT", path + "/id"),
            ("DELETE", path + "/id"),
        ]:
            if (method, route) == ("GET", ROUTES["partitions"][0]):
                continue
            with pytest.raises(MigrationError, match=Code.UNSUPPORTED):
                destination.call(method, route, {})


class InspectingTransport(RecordedTransport):
    def __init__(self, records):
        super().__init__(records)
        self.requests = []

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, deepcopy(body), deepcopy(headers)))
        return super().request(method, path, body, headers)


def record(method, path, response):
    return {"method": method, "path": path, "response": response}


def client(records, version="9.0"):
    transport = InspectingTransport(records)
    if version == "9.0":
        transport.records.insert(0, record("POST", "/api/v1/sessions", {"sessionId": "fixture"}))
        return Logs90(transport, "admin", Secret("fixture"))
    ops = RecordedTransport(
        [record("POST", "/suite-api/api/auth/token/exchange", {"jwt": "fixture"})]
    )
    return Logs911(transport, ops, Secret("fixture"), lambda r: r["jwt"])


@pytest.mark.parametrize("version", ["9.0", "9.1.1"])
@pytest.mark.parametrize("kind", RULE_TYPES)
@pytest.mark.parametrize("update", [False, True])
def test_rest_wire_shapes_and_auth(kind, version, update):
    path, wrapper = ROUTES[kind]
    rule = {"id": "source-id", "name": "example", "enabled": True}
    if kind == "forwarding":
        rule.update(host="collector.invalid", sslEnabled=True)
    response = {wrapper: [rule]} if wrapper else [rule]
    source = client([record("GET", path, response)])
    existing = {**rule, "id": "destination-id"} if update else None
    route = (
        path + "/destination-id" if update else path + "/batch" if kind == "forwarding" else path
    )
    destination = client([record("PUT" if update else "POST", route, {})], version)
    env = RestConfigEnvironment(source, destination, version + ".0")
    assert env.config_rules("source", kind) == [rule]
    env.config_write(kind, rule, existing)
    method, actual_route, body, headers = destination.transport.requests[-1]
    expected = {**rule, "id": "destination-id"} if update else dict(rule)
    if not update and kind != "forwarding":
        del expected["id"]
    if kind == "forwarding":
        expected["acceptCert"] = True
        if not update:
            expected = {"forwarders": [expected]}
    assert (method, actual_route, body) == ("PUT" if update else "POST", route, expected)
    assert headers == (
        {"Authorization": "Bearer fixture"} if version == "9.0" else {"X-JWT-Token": "fixture"}
    )
    assert source.transport.calls == [("POST", "/api/v1/sessions"), ("GET", path)]
    assert "acceptCert" not in rule


@pytest.mark.parametrize("body", [{}, {"logMaskRules": None}, {"logMaskRules": [{}]}, []])
def test_rest_invalid_inventory_not_empty(body):
    source = client([record("GET", ROUTES["masking"][0], body)])
    env = RestConfigEnvironment(source, client([]), "9.0.2")
    with pytest.raises(MigrationError, match=Code.SCHEMA):
        env.config_rules("source", "masking")


def test_cli_persistence_json_and_required_selection(tmp_path, capsys):
    args = ["--fixture", str(FIXTURE), "--remote-state", str(tmp_path / "remote")]
    assert main([*args, "config", "compare"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["rules"]
    with pytest.raises(SystemExit) as error:
        main([*args, "config", "apply"])
    assert error.value.code == 2
    capsys.readouterr()
    assert (
        main(
            [
                *args,
                "config",
                "apply",
                "--select",
                "partitions:Archive",
                "--select",
                "forwarding:archive",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["restart_required"]
    assert main([*args, "config", "compare"]) == 0
    rows = {r["selector"]: r for r in json.loads(capsys.readouterr().out)["rules"]}
    assert rows["partitions:Archive"]["status"] == "identical"
    assert rows["forwarding:archive"]["status"] == "identical"
    assert rows["masking:mask-secret"]["status"] == "missing"


def test_default_partition_update_uses_simulator_empty_id(config_env):
    config_env.data["source"]["partitions"][0]["retention_seconds"] = 86400
    result = Engine(config_env, None).config_apply(["partitions:Default"])
    assert result["complete"] and not result["restart_required"]
    assert config_env.data["destination"]["partitions"][0]["retention_seconds"] == 86400


def test_secret_exceptions_are_not_reported(config_env):
    config_env.data["faults"] = {"config.write.masking.after": "secret"}
    result = Engine(config_env, None).config_apply(["masking:mask-secret"])
    assert not result["complete"]
    assert "DO-NOT-PRINT" not in json.dumps(result)


def test_cli_partial_failure_exit_and_result(tmp_path, capsys):
    fixture = json.loads(FIXTURE.read_text())
    fixture["faults"] = {"config.write.masking.after": "connection_failed"}
    path = tmp_path / "fault.fixture"
    path.write_text(json.dumps(fixture))
    assert (
        main(
            [
                "--fixture",
                str(path),
                "--remote-state",
                str(tmp_path / "remote"),
                "config",
                "apply",
                "--select",
                "masking:mask-secret",
            ]
        )
        == 8
    )
    result = json.loads(capsys.readouterr().out)
    assert next(r for r in result["rules"] if r["type"] == "masking")["status"] == "uncertain"


def test_json_type_difference_is_not_identical(config_env):
    config_env.data["source"]["config"]["filters"][0]["enabled"] = 1
    rows = Engine(config_env, None).config_compare()["rules"]
    row = next(r for r in rows if r["selector"] == "filters:drop-debug")
    assert row["status"] == "differs" and row["changed_fields"] == ["enabled"]


def test_schema_failure_after_write_is_uncertain(config_env):
    def write(*args):
        raise MigrationError(Code.SCHEMA)

    config_env.config_write = write
    result = Engine(config_env, None).config_apply(["partitions:Archive"])
    assert not result["complete"] and result["restart_required"]
    assert result["rules"][0]["status"] == "uncertain"


def test_preview_auth_exchange_cannot_escape_config_gate():
    class NoNetwork:
        def request(self, *args, **kwargs):
            pytest.fail("Live OpsToken exchange escaped config release gate")

    destination = Logs911(RecordedTransport([]), NoNetwork(), Secret("test"), lambda r: "test")
    env = RestConfigEnvironment(client([]), destination, "9.1.1")
    with pytest.raises(MigrationError, match=Code.UNSUPPORTED):
        ConfigMigration(env).compare()
    with pytest.raises(MigrationError, match=Code.UNSUPPORTED):
        destination.call("GET", "/api/v1/log-mask")


def test_prewrite_validation_failure_is_failed_not_uncertain(config_env):
    config_env.data["destination"]["partitions"][0]["id"] = 5
    config_env.data["source"]["partitions"][0]["retention_seconds"] = 86400
    result = Engine(config_env, None).config_apply(["partitions:Default"])
    row = next(r for r in result["rules"] if r["selector"] == "partitions:Default")
    assert row["status"] == "failed" and row["reason"] == Code.SCHEMA
    assert config_env.data["trace"] == []


def test_rest_default_partition_empty_id_fails_before_any_write():
    def inventory(partitions):
        return [
            record("GET", ROUTES["partitions"][0], {"partitions": partitions}),
            record("GET", ROUTES["filters"][0], {"logFilterRules": []}),
            record("GET", ROUTES["masking"][0], {"logMaskRules": []}),
            record("GET", ROUTES["forwarding"][0], []),
        ]

    source = client(inventory([{"id": "", "name": "Default", "retention_seconds": 86400}]))
    destination = client(inventory([{"id": "", "name": "Default", "retention_seconds": 1}]))
    env = RestConfigEnvironment(source, destination, "9.0.2")
    result = ConfigMigration(env).apply(["partitions:Default"])
    row = next(r for r in result["rules"] if r["selector"] == "partitions:Default")
    assert row["status"] == "failed" and row["reason"] == Code.UNSUPPORTED
    assert not result["complete"]
    assert [(m, p) for m, p, *_ in destination.transport.requests if m != "GET"] == [
        ("POST", "/api/v1/sessions")
    ]


def test_partitions_read_is_not_narrowed_by_config_gate():
    class Live:
        def __init__(self):
            self.requests = []

        def request(self, method, path, body=None, headers=None):
            self.requests.append((method, path))
            return {"partitions": []}

    transport = Live()
    live = Logs90(transport, "admin", Secret("test"))
    live.token = Secret("t")
    assert live.partitions() == {"partitions": []}
    assert transport.requests == [("GET", "/api/v1/partitioning")]
    with pytest.raises(MigrationError, match=Code.UNSUPPORTED):
        live.call("GET", "/api/v1/log-mask")


@pytest.mark.parametrize("kind,name", [("partitions", "Archive"), ("masking", "mask-secret")])
def test_create_omits_source_id_and_destination_assigns_one(config_env, kind, name):
    source_rules = config_env.config_rules("source", kind)
    assert next(r for r in source_rules if r["name"] == name).get("id")
    result = Engine(config_env, None).config_apply([kind + ":" + name])
    assert result["complete"]
    created = next(r for r in config_env.config_rules("destination", kind) if r["name"] == name)
    source_id = next(r for r in source_rules if r["name"] == name)["id"]
    assert isinstance(created["id"], str) and created["id"] and created["id"] != source_id
