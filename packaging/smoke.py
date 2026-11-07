"""Smoke-test a finished download exactly as a tester receives it.

Extracts the archive into an empty temporary directory, then runs the program from
there with no Python, virtualenv or source tree on its paths: `--version`, `--help`,
argument refusal, the complete simulator migration and configuration workflows from
tests/smoke_installed.py against the bundled examples, and the real `gui` path rendered
offscreen until its first frame. `--native` adds the desktop launches (the windowed
program on Windows, `open` and a PATH symlink on macOS, the X11 platform on Linux). `--signed`
verifies signatures on the extracted bytes first. Screenshots and launch reports land in
`--evidence`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "packaging"))

from build import APP, GUI_EXE, NAME, version  # noqa: E402
from smoke_installed import config_smoke, smoke  # noqa: E402

TIMEOUT = 180


def extract(archive: Path, into: Path) -> Path:
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive) as bundle:
            bundle.extractall(into, filter="tar")
    elif sys.platform == "darwin":
        # ditto restores the app's extended attributes and symlinks as Finder would.
        subprocess.run(["ditto", "-x", "-k", str(archive), str(into)], check=True)
    else:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(into)
    (top,) = into.iterdir()
    return top


def clean_environment(home: Path) -> dict[str, str]:
    """Nothing Python-shaped on the program's paths; settings in a throwaway home."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "QT_QPA_PLATFORM"}
        and not k.startswith("LOGS_MIGRATE_")
    }
    pythons = ("python", "python3", "python.exe", "python3.exe")
    env["PATH"] = os.pathsep.join(
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if entry and not any((Path(entry) / name).exists() for name in pythons)
    )
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData"))
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    return env


def run(program: Path, *args: str, code: int = 0, cwd: Path, env=None) -> str:
    result = subprocess.run(
        [str(program), *args],
        cwd=cwd,
        env=env if env is not None else os.environ,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    if result.returncode != code:
        raise SystemExit(
            f"{args} returned {result.returncode}, expected {code}\n{result.stdout}{result.stderr}"
        )
    return result.stdout + result.stderr


def check_launch(report: Path, evidence: Path, label: str, **expected) -> None:
    status = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
    problems = {k: status.get(k) for k, v in expected.items() if status.get(k) != v}
    if not status["saved"] or not status["visible"] or problems:
        raise SystemExit(f"{label}: window launch failed: {status}")
    if report.stat().st_size < 1000:
        raise SystemExit(f"{label}: screenshot is implausibly small")
    shutil.copy2(report, evidence / report.name)
    shutil.copy2(report.with_suffix(".json"), evidence / report.with_suffix(".json").name)
    print(f"PASS {label}: {status}", flush=True)


def verify_signatures(top: Path) -> None:
    if sys.platform == "darwin":
        app = top / APP
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        subprocess.run(["xcrun", "stapler", "validate", str(app)], check=True)
        subprocess.run(["spctl", "--assess", "--type", "execute", str(app)], check=True)
    elif sys.platform == "win32":
        for name in (f"{NAME}.exe", GUI_EXE):
            exe = str(top / name).replace("'", "''")
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"$s = Get-AuthenticodeSignature -LiteralPath '{exe}'; $s | Format-List; "
                    "if ($s.Status -ne 'Valid') { exit 1 }",
                ],
                check=True,
            )
    else:
        raise SystemExit("--signed applies to Windows and macOS downloads")
    print("PASS signatures verified on the extracted download", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument("--native", action="store_true", help="Also launch as a desktop does")
    parser.add_argument("--signed", action="store_true", help="Verify signatures first")
    parser.add_argument("--evidence", type=Path, default=ROOT / "dist" / "smoke-evidence")
    args = parser.parse_args()
    archive = args.archive.resolve()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="vlm-download-") as directory:
        scratch = Path(directory)
        (scratch / "download").mkdir()
        (scratch / "home").mkdir()
        (scratch / "work").mkdir()
        top = extract(archive, scratch / "download")
        mac = (top / APP).is_dir()
        program = top / (
            f"{APP}/Contents/MacOS/{NAME}" if mac else NAME + (".exe" if os.name == "nt" else "")
        )
        examples = top / "examples"
        for name in ("LICENSE.txt", "THIRD-PARTY-NOTICES.txt", "README.txt"):
            if not (top / name).is_file():
                raise SystemExit(f"download is missing {name}")
        if args.signed:
            verify_signatures(top)
        # The smoke_installed helpers inherit this process's environment.
        clean = clean_environment(scratch / "home")
        os.environ.clear()
        os.environ.update(clean)
        work = scratch / "work"

        printed = run(program, "--version", cwd=work).strip()
        if printed != version():
            raise SystemExit(f"--version printed {printed!r}, expected {version()!r}")
        if "plan" not in run(program, "--help", cwd=work):
            raise SystemExit("--help does not list the commands")
        run(program, cwd=work, code=2)
        run(program, "plan", "--password", "x", cwd=work, code=2)
        print(f"PASS CLI {printed}: --version, --help and argument refusal", flush=True)
        for name in ("sealed-populated.fixture", "sealed-911.fixture"):
            smoke(examples / name, str(program))
        config_smoke(str(program))

        report = work / "gui-offscreen.png"
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", LOGS_MIGRATE_GUI_SMOKE=str(report))
        journal = str(work / "gui.sqlite")
        fixture = str(examples / "sealed-populated.fixture")
        run(program, "gui", "--fixture", fixture, "--journal", journal, cwd=work, env=env)
        check_launch(report, evidence, "GUI offscreen", platform="offscreen")

        if not args.native:
            return
        if sys.platform == "win32":
            # The windowed program a double click starts: no arguments, no console.
            report = work / "gui-windowed.png"
            subprocess.run(
                [str(top / GUI_EXE)],
                cwd=work,
                env=dict(os.environ, LOGS_MIGRATE_GUI_SMOKE=str(report)),
                timeout=TIMEOUT,
                check=True,
            )
            check_launch(
                report,
                evidence,
                "Windowed launch",
                console_attached=False,
                platform="windows",
            )
        elif mac:
            link = scratch / "bin" / NAME
            link.parent.mkdir()
            link.symlink_to(program)
            if run(link, "--version", cwd=work).strip() != version():
                raise SystemExit("CLI through a PATH symlink printed the wrong version")
            print("PASS CLI through a PATH symlink", flush=True)
            report = work / "gui-finder.png"
            subprocess.run(
                [
                    "/usr/bin/open",
                    "-n",
                    "-W",
                    "--env",
                    f"LOGS_MIGRATE_GUI_SMOKE={report}",
                    str(top / APP),
                ],
                cwd=work,
                timeout=TIMEOUT,
                check=True,
            )
            check_launch(report, evidence, "Finder-style launch", platform="cocoa")
        else:
            report = work / "gui-x11.png"
            native = dict(os.environ, QT_QPA_PLATFORM="xcb", LOGS_MIGRATE_GUI_SMOKE=str(report))
            run(program, "gui", "--fixture", fixture, cwd=work, env=native)
            check_launch(report, evidence, "X11 launch", platform="xcb")


if __name__ == "__main__":
    main()
