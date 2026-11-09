"""Build and package the standalone download for the platform this runs on.

`build` freezes `packaging/launcher.py` with PyInstaller: a one-file console executable
on Linux and Windows, and a one-dir `VCF Logs Migrator.app` on macOS (PyInstaller
discourages one-file windowed macOS apps). Windows also gets a windowed
`VCF Logs Migrator.exe` from `packaging/gui_entry.py`, so a double click never opens a
console. CI signs between `build` and `package`.
`package` wraps the (signed) executable with the example fixtures, licenses and a
quickstart into `dist/vcf-logs-migrator-<label>-<platform>.tar.gz|.zip`.

Run from the repository root with the pinned tools from packaging/requirements.txt
installed next to the package (`python -m pip install -r packaging/requirements.txt '.[ssh]'`).
"""

from __future__ import annotations

import argparse
import importlib.metadata
import plistlib
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
PLATFORMS = ("linux-x86_64", "windows-x64", "macos-arm64", "macos-x86_64")
NAME = "logs-migrate"
APP = "VCF Logs Migrator.app"
GUI_EXE = "VCF Logs Migrator.exe"
BUNDLE_ID = "net.sentania.vcf-logs-migrator"
EXAMPLES = ("sealed-populated.fixture", "sealed-911.fixture", "config.fixture")
# Distributions frozen into the executable besides this package and Python itself.
BUNDLED = (
    "PySide6-Essentials",
    "shiboken6",
    "tzdata",
    "paramiko",
    "bcrypt",
    "cryptography",
    "PyNaCl",
    "cffi",
    "pycparser",
)


def version() -> str:
    text = (ROOT / "src" / "logs_migrate" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def executable(platform: str) -> Path:
    """Path of the built program inside dist/, the file signing and smoke tests target."""
    if platform.startswith("macos-"):
        return DIST / APP / "Contents" / "MacOS" / NAME
    return DIST / (NAME + (".exe" if platform == "windows-x64" else ""))


def freeze(name: str, script: str, *layout: str) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            *layout,
            "--clean",
            "--noconfirm",
            "--name",
            name,
            "--distpath",
            str(DIST),
            "--workpath",
            str(BUILD / "pyinstaller" / name),
            "--specpath",
            str(BUILD),
            # The CLI imports the GUI and adapters lazily; take every module and the
            # Lattice QSS, palette and icons.
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
    )


def build(platform: str) -> Path:
    mac = platform.startswith("macos-")
    windows = platform == "windows-x64"
    for stale in (DIST / APP, DIST / f"{NAME}.app", DIST / GUI_EXE, executable(platform)):
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()
    if mac:
        freeze(NAME, "launcher.py", "--onedir", "--windowed", "--osx-bundle-identifier", BUNDLE_ID)
    else:
        freeze(NAME, "launcher.py", "--onefile", "--console")
    if windows:
        freeze(GUI_EXE.removesuffix(".exe"), "gui_entry.py", "--onefile", "--windowed")
        if not (DIST / GUI_EXE).is_file():
            raise SystemExit(f"PyInstaller did not produce {DIST / GUI_EXE}")
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


def python_license() -> Path:
    candidates = [
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "share" / "licenses" / f"python{sys.version_info[0]}"
        f".{sys.version_info[1]}" / "LICENSE",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit("Could not find the Python license to include in the download")


def notices() -> str:
    python = ".".join(map(str, sys.version_info[:3]))
    lines = [
        "VCF Logs Migrator bundles the following third-party software.",
        "",
        f"Python {python}: Python Software Foundation License, text below.",
    ]
    for name in BUNDLED:
        meta = importlib.metadata.metadata(name)
        licence = meta.get("License-Expression") or meta.get("License") or "see project"
        lines.append(f"{name} {meta['Version']}: {licence}")
    pyside = importlib.metadata.version("PySide6-Essentials")
    lines += [
        f"PyInstaller {importlib.metadata.version('pyinstaller')} bootloader: GPL-2.0 with "
        "the PyInstaller bootloader exception, which permits this distribution.",
        "",
        "Qt and Qt for Python (PySide6, shiboken6) are used under the GNU Lesser General",
        "Public License version 3 (licenses/LGPL-3.0.txt, which incorporates",
        "licenses/GPL-3.0.txt). They are bundled as unmodified shared libraries that the",
        "executable unpacks at run time. Their source is available from",
        f"https://download.qt.io/official_releases/QtForPython/pyside6/ (version {pyside})",
        "and https://code.qt.io. This application's complete source and build script",
        "(packaging/build.py) are public at",
        "https://github.com/sentania-labs/vcf-logs-migrator, so the download can be rebuilt",
        "against a modified Qt or PySide6.",
        "",
        "tzdata is used under the Apache License 2.0; its license files follow.",
        "",
    ]
    for name in BUNDLED:
        for file in importlib.metadata.files(name) or ():
            if "licenses" in file.parts or file.name.upper().startswith("LICENSE"):
                lines += [f"----- {name}: {file.name} -----", file.read_text(encoding="utf-8"), ""]
    lines += ["----- Python license -----", python_license().read_text(encoding="utf-8")]
    return "\n".join(lines)


def package(platform: str, label: str) -> Path:
    stem = f"vcf-logs-migrator-{label}-{platform}"
    staging = BUILD / "package" / stem
    if staging.parent.exists():
        shutil.rmtree(staging.parent)
    (staging / "examples").mkdir(parents=True)
    (staging / "licenses").mkdir()
    for name in EXAMPLES:
        shutil.copy2(ROOT / "tests" / "fixtures" / name, staging / "examples" / name)
    for path in (PACKAGING / "licenses").iterdir():
        shutil.copy2(path, staging / "licenses" / path.name)
    shutil.copy2(ROOT / "LICENSE", staging / "LICENSE.txt")
    shutil.copy2(PACKAGING / "QUICKSTART.txt", staging / "README.txt")
    (staging / "THIRD-PARTY-NOTICES.txt").write_text(notices(), encoding="utf-8")
    if platform.startswith("macos-"):
        # ditto keeps the bundle's signatures, stapled ticket and symlinks intact.
        subprocess.run(["ditto", str(DIST / APP), str(staging / APP)], check=True)
        archive = DIST / f"{stem}.zip"
        archive.unlink(missing_ok=True)
        subprocess.run(
            ["ditto", "-c", "-k", "--keepParent", str(staging), str(archive)], check=True
        )
    elif platform == "windows-x64":
        shutil.copy2(executable(platform), staging / f"{NAME}.exe")
        shutil.copy2(DIST / GUI_EXE, staging / GUI_EXE)
        archive = DIST / f"{stem}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staging.rglob("*")):
                bundle.write(path, path.relative_to(staging.parent).as_posix())
    else:
        shutil.copy2(executable(platform), staging / NAME)
        (staging / NAME).chmod(0o755)
        archive = DIST / f"{stem}.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(staging, arcname=stem)
    print(archive)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "package"):
        sub = commands.add_parser(command)
        sub.add_argument("platform", choices=PLATFORMS)
    commands.choices["package"].add_argument(
        "--label", help="Version label in the archive name (default: v<package version>)"
    )
    commands.add_parser("version", help="Print the embedded package version")
    args = parser.parse_args()
    if args.command == "version":
        print(version())
    elif args.command == "build":
        build(args.platform)
    else:
        package(args.platform, args.label or f"v{version()}")
