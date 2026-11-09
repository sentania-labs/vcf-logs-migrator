"""Notices must fail closed when a frozen distribution lacks license text."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "build_notices", Path(__file__).resolve().parents[1] / "packaging" / "notices.py"
)
notices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notices)


def test_missing_distribution_license_fails_build(monkeypatch, tmp_path):
    distribution = SimpleNamespace(metadata={"Name": "unknown"}, version="1.0", files=[])
    monkeypatch.setattr(
        notices.metadata, "packages_distributions", lambda: {"unknown": ["unknown"]}
    )
    monkeypatch.setattr(notices.metadata, "distribution", Mock(return_value=distribution))
    analysis = SimpleNamespace(pure=[("unknown", "", "PYMODULE")], binaries=[], datas=[])
    with pytest.raises(SystemExit, match="No license text found for bundled distribution"):
        notices.generate(analysis, tmp_path / "notices.txt")
