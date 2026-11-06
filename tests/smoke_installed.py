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
        refused = invoke("run", run_id, "--plan-hash", plan["plan_hash"], code=3)
        assert refused["error"] == "acknowledgment_required"
        copied = invoke(
            "run",
            run_id,
            "--plan-hash",
            plan["plan_hash"],
            "--ack-backup",
            "--ack-active",
            ACTIVE_ACK,
        )
        assert copied["state"] == "VERIFYING"
        assert copied["verification"]["outcome"] == "verification_pending"
        verified = invoke("verify", run_id)
        assert verified["state"] == "VERIFIED"
        evidence = verified["verification"]
        assert evidence["outcome"] == "verified_success"
        assert evidence["source_count"] == len(data["source_events"])
        expected = sum(
            len(data[key]) for key in ("source_events", "destination_events", "ingest_during_run")
        )
        assert evidence["actual_count"] == evidence["expected_count"] == expected
        assert invoke("status", run_id)["run"]["state"] == "VERIFIED"
        report = invoke("report", run_id, "--format", "json")
        assert report["run"]["verification"] == evidence
        assert "verified_success" in invoke("report", run_id, parse=False)
        print(f"{fixture.name}: installed CLI verified {expected} fixture events")


if __name__ == "__main__":
    executable = shutil.which("logs-migrate")
    if not executable:
        raise SystemExit("Install the package before running the console-script smoke check.")
    for name in ("sealed-populated.fixture", "sealed-911.fixture"):
        smoke(FIXTURES / name, executable)
