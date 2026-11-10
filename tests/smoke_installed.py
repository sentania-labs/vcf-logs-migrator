"""Drive the installed console script from outside the checkout, using fixtures only."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ACTIVE_ACK = "i understand you aren't going to copy the latest data"


def smoke(fixture: Path, executable: str) -> None:
    data = json.loads(fixture.read_text())
    # Each console-script invocation must load the installed package, not the source tree.
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="logs-migrate-smoke-") as directory:
        base = [
            executable,
            "--journal",
            str(Path(directory) / "run.sqlite"),
            "--fixture",
            str(fixture),
        ]

        def invoke(*args: str, code: int = 0, parse: bool = True):
            result = subprocess.run(
                [*base, *args],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != code:
                raise RuntimeError(
                    f"{fixture.name}: {args[0]} returned {result.returncode}, expected {code}: "
                    f"{result.stdout}{result.stderr}"
                )
            output = result.stdout if code == 0 else result.stderr
            return json.loads(output) if parse else output

        discovered = invoke("discover")
        assert discovered["destination"]["version"] == data["destination"]["version"]
        plan = invoke("plan", "--since", "2026-03-01T00:00:00Z", "--until", "2026-04-01T00:00:00Z")
        run_id = plan["id"]
        refused = invoke("copy", run_id, code=3)
        assert refused["error"] == "acknowledgment_required"
        copied = invoke(
            "copy",
            run_id,
            "--ack-backup",
            "--ack-active",
            ACTIVE_ACK,
        )
        assert copied["task"]["status"] == "running"
        assert copied["verification"]["outcome"] == "verification_pending"
        verified = invoke("verify", run_id)
        evidence = verified
        assert evidence["outcome"] == "verified_success"
        assert evidence["source_count"] == len(data["source_events"])
        assert evidence["actual_count"] == evidence["expected_count"] == len(data["source_events"])
        assert invoke("status")["status"] == "completed"
        report = invoke("report", run_id, "--format", "json")
        assert report["run"]["verification"] == evidence
        assert "verified_success" in invoke("report", run_id, parse=False)
        print(f"{fixture.name}: installed CLI verified {evidence['expected_count']} fixture events")


def config_smoke(executable: str) -> None:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="logs-config-smoke-") as directory:
        base = [
            executable,
            "--fixture",
            str(FIXTURES / "config.fixture"),
            "--remote-state",
            str(Path(directory) / "remote"),
        ]

        def invoke(*args):
            response = subprocess.run(
                [*base, "config", *args],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            return json.loads(response.stdout)

        before = invoke("compare")
        assert {r["status"] for r in before["rules"]} == {"missing", "identical", "differs"}
        result = invoke(
            "apply",
            "--select",
            "partitions:Archive",
            "--select",
            "filters:drop-debug",
            "--select",
            "masking:mask-secret",
            "--select",
            "forwarding:archive",
        )
        assert result["complete"] and result["restart_required"]
        assert {r["status"] for r in result["rules"]} == {"created", "updated", "skipped"}
        after = invoke("compare")
        assert all(
            r["status"] == "identical"
            for r in after["rules"]
            if r["selector"] != "filters:drop-test"
        )
        assert after["destination_only"] == [{"type": "filters", "name": "local-only"}]
        state = json.loads((Path(directory) / "remote").read_text())
        assert state["source"] == json.loads((FIXTURES / "config.fixture").read_text())["source"]
        assert all(t["operation"] == "config.write" for t in state["trace"])
        print("config.fixture: installed CLI compared and applied all four rule types")


if __name__ == "__main__":
    executable = shutil.which("logs-migrate")
    if not executable:
        raise SystemExit("Install the package before running the console-script smoke check.")
    for name in ("sealed-populated.fixture", "sealed-911.fixture"):
        smoke(FIXTURES / name, executable)
    config_smoke(executable)
