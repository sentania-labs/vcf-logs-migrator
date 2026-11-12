import pytest

from logs_migrate.versions import evaluate


@pytest.mark.parametrize(
    ("source", "destination", "api", "passed"),
    [
        ("9.0.2", "9.0.2", "present", True),
        ("8.18.7", "9.0.2", "present", True),
        ("9.1.1", "9.0.2", "present", False),
        ("8.18.7", "8.18.7", "absent", False),
        ("8.18.7", "10.0.0", "present", True),
        ("8.18.7", "10.0.0", "unchecked", False),
    ],
)
def test_version_pairings(source, destination, api, passed):
    result = evaluate({"source": source}, {"destination": destination}, api, "test")
    assert result["passed"] is passed
    if destination == "10.0.0" and passed:
        assert "not been tested" in result["warnings"][0]
    if source == "9.1.1":
        assert source in result["message"] and destination in result["message"]


def test_mixed_nodes_name_the_disagreement():
    result = evaluate({"one": "8.18.7", "two": "8.18.8"}, {"dst": "9.0.2"}, "present", "test")
    assert not result["passed"]
    assert "one: 8.18.7" in result["message"] and "two: 8.18.8" in result["message"]


def test_inferred_minor_version_compares_at_that_precision():
    result = evaluate({"src": "9.1.1"}, {"cluster": "9.1"}, "present", "inferred")
    assert result["passed"]
    assert any("major and minor" in warning for warning in result["warnings"])


def test_plan_checks_before_inventory_and_records_evidence(rig):
    from conftest import planned

    engine, env, journal = rig
    env.data["source"]["node_versions"] = {"src-1": "10.0.0", "src-2": "10.0.0"}
    from logs_migrate.errors import MigrationError

    with pytest.raises(MigrationError, match="unsupported_capability"):
        planned(engine)
    assert not env.data["trace"]
    env.data["source"]["node_versions"] = {"src-1": "8.18.7", "src-2": "8.18.7"}
    run = planned(engine)
    assert run["plan"]["version_check"]["passed"]
    assert journal.events(run["id"])[0]["details"]["version_check"]["passed"]


@pytest.mark.parametrize(
    ("source", "destination", "api", "expected"),
    [
        ("9.0.2", "9.0.2", "present", True),
        ("8.18.7", "9.0.2", "present", True),
        ("10.0.0", "9.0.2", "present", False),
        ("8.18.7", "8.18.7", "absent", False),
        ("8.18.7", "10.0.0", "present", True),
        ("8.18.7", "10.0.0", "unchecked", False),
    ],
)
def test_simulator_versions_in_cli_and_desktop(
    tmp_path, monkeypatch, fixture, source, destination, api, expected
):
    import json

    import standin

    from logs_migrate.cli import main
    from logs_migrate.gui.session import Session

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))

    fixture["source"]["version"] = source
    fixture["destination"]["version"] = destination
    fixture["destination"]["migration_api"] = api
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture))
    result = main(
        [
            "--fixture",
            str(path),
            "--journal",
            str(tmp_path / "cli.sqlite"),
            "discover",
        ]
    )
    assert (result == 0) is expected
    session = Session()
    try:
        response = standin.sign_in(
            session,
            fixture=str(path),
            journal=str(tmp_path / "desktop.sqlite"),
            credentials={},
            connection={"source_host": "old.example", "destination": "new.example"},
        )
        assert response["passed"] is expected
    finally:
        session.close()


def test_mixed_simulator_nodes_refused_in_cli_and_desktop(tmp_path, monkeypatch, fixture, capsys):
    import json

    import standin

    from logs_migrate.cli import main
    from logs_migrate.gui.session import Session

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))

    fixture["source"]["node_versions"] = {
        node["id"]: "8.18.7" if index == 0 else "8.18.8"
        for index, node in enumerate(fixture["source"]["nodes"])
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture))
    result = main(
        [
            "--fixture",
            str(path),
            "--journal",
            str(tmp_path / "cli.sqlite"),
            "discover",
        ]
    )
    assert result != 0
    assert "different versions" in capsys.readouterr().err
    session = Session()
    try:
        from logs_migrate.gui import viewmodel as vm

        response = standin.sign_in(
            session,
            fixture=str(path),
            journal=str(tmp_path / "desktop.sqlite"),
            credentials={},
            connection={"source_host": "old.example", "destination": "new.example"},
        )
        assert not response["passed"]
        assert "different versions" in response["version_check"]["message"]
        assert (
            "source servers are on different versions"
            in vm.verdict(response["version_check"])["title"]
        )
    finally:
        session.close()
