"""Exercise tag-derived wheel and sdist versions in an isolated clone."""

import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "clone"
        run("git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(clone), cwd=tmp)
        untagged = run(sys.executable, "packaging/build.py", "version", cwd=clone)
        assert ".dev" in untagged, untagged
        run("git", "tag", "v9999.0.0", cwd=clone)
        tagged = run(sys.executable, "packaging/build.py", "version", cwd=clone)
        assert tagged == "9999.0.0", tagged
        run(sys.executable, "-m", "build", "--no-isolation", cwd=clone)
        wheel = next((clone / "dist").glob("*.whl"))
        sdist = next((clone / "dist").glob("*.tar.gz"))
        with zipfile.ZipFile(wheel) as archive:
            metadata = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
            assert "Version: 9999.0.0\n" in archive.read(metadata).decode()
        with tarfile.open(sdist) as archive:
            metadata = next(
                m
                for m in archive.getmembers()
                if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO")
            )
            assert "Version: 9999.0.0\n" in archive.extractfile(metadata).read().decode()
        target = Path(tmp) / "installed"
        run(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(target),
            str(wheel),
            cwd=tmp,
        )
        env = dict(os.environ, PYTHONPATH=str(target))
        cli = run(
            sys.executable,
            "-c",
            "from logs_migrate.cli import main; main(['--version'])",
            cwd=tmp,
            env=env,
        )
        assert cli == "9999.0.0", cli
        print(f"untagged={untagged}; tag, wheel, sdist, CLI={tagged}")


if __name__ == "__main__":
    main()
