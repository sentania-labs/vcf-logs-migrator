"""Smoke-test a finished download exactly as a tester receives it.

Copies the download into an empty temporary directory, then runs the program from
there with no Python, virtualenv or source tree on its paths: `--version`, `--help`,
argument refusal, the complete simulator migration and configuration workflows from
tests/smoke_installed.py against repository fixtures, the real SSH probe against a
reserved localhost port, and the real `gui` path rendered offscreen until its first
frame. `--native` adds desktop launches on each platform. `--signed`
verifies signatures on the download first. Screenshots and launch reports land in
`--evidence`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "packaging"))

from smoke_installed import config_smoke, smoke  # noqa: E402

from build import APP, NAME, version  # noqa: E402

TIMEOUT = 180


def extract(artifact: Path, into: Path) -> Path:
    if artifact.suffix == ".dmg":
        mount = into / "mounted"
        mount.mkdir()
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), str(artifact)], check=True
        )
        try:
            subprocess.run(["ditto", str(mount / APP), str(into / APP)], check=True)
            if not (mount / "Applications").exists():
                raise SystemExit("disk image missing Applications link")
        finally:
            subprocess.run(["hdiutil", "detach", str(mount)], check=True)
        mount.rmdir()
    else:
        shutil.copy2(artifact, into / (NAME + (".exe" if os.name == "nt" else "")))
    return into


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


def check_ssh(program: Path, work: Path, evidence: Path) -> None:
    report = work / "ssh-smoke.json"
    run(program, "--version", cwd=work, env=dict(os.environ, LOGS_MIGRATE_SSH_SMOKE=str(report)))
    status = json.loads(report.read_text(encoding="utf-8"))
    if (
        not status.get("paramiko_version")
        or status.get("code") != "connection_failed"
        or not status.get("message", "").startswith("SSH TCP connection")
    ):
        raise SystemExit(f"SSH dependency/probe smoke failed: {status}")
    shutil.copy2(report, evidence / report.name)
    print(f"PASS frozen SSH dependency and TCP probe: {status}", flush=True)


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
        subprocess.run(["spctl", "--assess", "--type", "execute", str(app)], check=True)
    elif sys.platform == "win32":
        for name in (f"{NAME}.exe",):
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
        examples = ROOT / "tests" / "fixtures"
        if args.signed:
            if mac:
                subprocess.run(["xcrun", "stapler", "validate", str(archive)], check=True)
                subprocess.run(["codesign", "--verify", str(archive)], check=True)
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
        if "GNU LESSER GENERAL PUBLIC LICENSE" not in run(program, "--licenses", cwd=work):
            raise SystemExit("embedded licenses missing")
        run(program, cwd=work, code=2)
        run(program, "plan", "--password", "x", cwd=work, code=2)
        print(f"PASS CLI {printed}: --version, --help and argument refusal", flush=True)
        for name in ("sealed-populated.fixture", "sealed-911.fixture"):
            smoke(examples / name, str(program))
        config_smoke(str(program))
        check_ssh(program, work, evidence)

        report = work / "gui-offscreen.png"
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", LOGS_MIGRATE_GUI_SMOKE=str(report))
        journal = str(work / "gui.sqlite")
        fixture = str(examples / "sealed-populated.fixture")
        run(program, "gui", "--fixture", fixture, "--journal", journal, cwd=work, env=env)
        check_launch(report, evidence, "GUI offscreen", platform="offscreen")

        if not args.native:
            return
        if sys.platform == "win32":
            # A fresh console simulates Explorer launching the program without arguments.
            report = work / "gui-windowed.png"
            subprocess.run(
                [str(program)],
                cwd=work,
                env=dict(os.environ, LOGS_MIGRATE_GUI_SMOKE=str(report)),
                timeout=TIMEOUT,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
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
