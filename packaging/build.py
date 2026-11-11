"""Build and package the platform's standalone download.

Freeze with PyInstaller, then package a disk image on macOS or rename the
single executable on Windows and Linux. CI signs and smoke-tests the result.

Run from the repository root with the pinned tools from packaging/requirements.txt
installed next to the package (`python -m pip install -r packaging/requirements.txt '.[ssh]'`).
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
PLATFORMS = ("linux-x86_64", "windows-x64", "macos-arm64")
NAME = "logs-migrate"
APP = "VCF Logs Migrator.app"
BUNDLE_ID = "net.sentania.vcf-logs-migrator"


def version() -> str:
    # The CLI reads pyproject.toml, just like the build backend that embeds metadata.
    return subprocess.check_output(
        [sys.executable, "-m", "setuptools_scm"], cwd=ROOT, text=True
    ).strip()


def executable(platform: str) -> Path:
    """Path of the built program inside dist/, the file signing and smoke tests target."""
    if platform.startswith("macos-"):
        return DIST / APP / "Contents" / "MacOS" / NAME
    return DIST / (NAME + (".exe" if platform == "windows-x64" else ""))


def freeze(name: str, script: str, *layout: str) -> None:
    # Insert the notices after Analysis, so their inventory comes from the actual
    # module and binary graph rather than the build environment's installed packages.
    BUILD.mkdir(exist_ok=True)
    env = dict(
        os.environ, PYTHONPATH=str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller.utils.cliutils.makespec",
            *layout,
            "--name",
            name,
            "--specpath",
            str(BUILD),
            "--paths",
            str(ROOT / "src"),
            "--copy-metadata",
            "vcf-logs-migrator",
            "--collect-submodules",
            "logs_migrate",
            "--collect-data",
            "logs_migrate",
            "--collect-data",
            "tzdata",
            # SSH is a release capability, including lazy algorithm/backend imports.
            "--collect-all",
            "paramiko",
            "--exclude-module",
            "tkinter",
            str(PACKAGING / script),
        ],
        check=True,
        cwd=ROOT,
        env=env,
    )
    spec = BUILD / f"{name}.spec"
    text = spec.read_text(encoding="utf-8")
    if text.count("pyz = PYZ(a.pure)") != 1:
        raise SystemExit("Unexpected PyInstaller spec; cannot embed notices")
    text = text.replace(
        "pyz = PYZ(a.pure)",
        f"exec(compile(open({str(PACKAGING / 'notices.py')!r}, encoding='utf-8').read(), "
        f"{str(PACKAGING / 'notices.py')!r}, 'exec'))\n"
        f"generate(a, {str(DIST / 'THIRD-PARTY-NOTICES.txt')!r})\n"
        f"a.datas += [({('logs_migrate/THIRD-PARTY-NOTICES.txt')!r}, "
        f"{str(DIST / 'THIRD-PARTY-NOTICES.txt')!r}, 'DATA')]\n"
        "pyz = PYZ(a.pure)",
    )
    spec.write_text(text, encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(DIST),
            "--workpath",
            str(BUILD / "pyinstaller"),
            str(spec),
        ],
        check=True,
        cwd=ROOT,
        env=env,
    )


def build(platform: str) -> Path:
    mac = platform.startswith("macos-")
    for stale in (DIST / APP, DIST / f"{NAME}.app", executable(platform)):
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()
    if mac:
        freeze(NAME, "launcher.py", "--onedir", "--windowed", "--osx-bundle-identifier", BUNDLE_ID)
    else:
        freeze(NAME, "launcher.py", "--onefile", "--console")
    if mac:
        built = DIST / f"{NAME}.app"
        built.rename(DIST / APP)
        plist_path = DIST / APP / "Contents" / "Info.plist"
        plist = plistlib.loads(plist_path.read_bytes())
        plist.update(
            CFBundleName="VCF Logs Migrator",
            CFBundleDisplayName="VCF Logs Migrator",
            CFBundleShortVersionString=version(),
            CFBundleVersion=version(),
            NSHighResolutionCapable=True,
        )
        plist_path.write_bytes(plistlib.dumps(plist))
        # Refresh PyInstaller's ad-hoc seal after the Info.plist change; signed builds
        # replace it with Developer ID signatures on all nested code.
        subprocess.run(["codesign", "--force", "--sign", "-", str(DIST / APP)], check=True)
    program = executable(platform)
    if not program.is_file():
        raise SystemExit(f"PyInstaller did not produce {program}")
    print(program)
    return program


def create_dmg(staging: Path, artifact: Path) -> None:
    command = [
        "hdiutil",
        "create",
        "-volname",
        "VCF Logs Migrator",
        "-srcfolder",
        str(staging),
        "-format",
        "UDZO",
        str(artifact),
    ]
    for attempt in range(3):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            print(result.stdout, end="")
            return
        if "Resource busy" not in result.stderr or attempt == 2:
            print(result.stderr, end="", file=sys.stderr)
            raise subprocess.CalledProcessError(
                result.returncode, command, output=result.stdout, stderr=result.stderr
            )
        print(f"hdiutil create: {result.stderr.strip()}; retrying", file=sys.stderr)
        artifact.unlink(missing_ok=True)
        info = subprocess.run(["hdiutil", "info", "-plist"], check=True, capture_output=True)
        for image in plistlib.loads(info.stdout).get("images", []):
            for entity in image.get("system-entities", []):
                mount = entity.get("mount-point", "")
                if mount == "/Volumes/VCF Logs Migrator":
                    subprocess.run(["hdiutil", "detach", mount], check=True)
        time.sleep(2 * (attempt + 1))


def package(platform: str, label: str) -> Path:
    stem = f"vcf-logs-migrator-{label}-{platform}"
    if platform.startswith("macos-"):
        staging = BUILD / "dmg"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        subprocess.run(["ditto", str(DIST / APP), str(staging / APP)], check=True)
        (staging / "Applications").symlink_to("/Applications")
        artifact = DIST / f"{stem}.dmg"
        artifact.unlink(missing_ok=True)
        create_dmg(staging, artifact)
    else:
        artifact = DIST / (stem + (".exe" if platform == "windows-x64" else ""))
        shutil.copy2(executable(platform), artifact)
    print(artifact)
    return artifact


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "package"):
        sub = commands.add_parser(command)
        sub.add_argument("platform", choices=PLATFORMS)
    commands.choices["package"].add_argument(
        "--label", help="Version label in the download name (default: v<package version>)"
    )
    commands.add_parser("version", help="Print the embedded package version")
    args = parser.parse_args()
    if args.command == "version":
        print(version())
    elif args.command == "build":
        build(args.platform)
    else:
        package(args.platform, args.label or f"v{version()}")
