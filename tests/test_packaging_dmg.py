"""Exercise disk image retries without macOS or signing credentials."""

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def build_module():
    spec = importlib.util.spec_from_file_location(
        "packaging_build", Path(__file__).resolve().parents[1] / "packaging" / "build.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dmg_retries_busy_and_cleans_up(tmp_path, monkeypatch, build_module):
    fake = tmp_path / "hdiutil"
    fake.write_text(
        f"#!{sys.executable}\n"
        + """import pathlib, plistlib, sys
root = pathlib.Path(__file__).parent
log = root / "calls"
with log.open("a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "create":
    count = len([x for x in log.read_text().splitlines() if x.startswith("create ")])
    artifact = pathlib.Path(sys.argv[-1])
    assert not artifact.exists(), "partial output was not removed"
    artifact.write_text("partial" if count < 3 else "complete")
    if count < 3:
        print("hdiutil: create failed - Resource busy", file=sys.stderr)
        sys.exit(1)
elif sys.argv[1] == "info":
    sys.stdout.buffer.write(plistlib.dumps({"images": [{"system-entities": [
        {"mount-point": "/Volumes/VCF Logs Migrator"},
        {"mount-point": "/Volumes/Other"},
    ]}]}))
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(build_module.time, "sleep", lambda _: None)
    artifact = tmp_path / "output.dmg"
    build_module.create_dmg(tmp_path, artifact)
    assert artifact.read_text() == "complete"
    calls = (tmp_path / "calls").read_text().splitlines()
    assert len([call for call in calls if call.startswith("create ")]) == 3
    assert calls.count("detach /Volumes/VCF Logs Migrator") == 2
