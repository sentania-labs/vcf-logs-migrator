"""Generate notices from PyInstaller's resolved analysis graph."""

import importlib.metadata as metadata
import sys
import sysconfig
from pathlib import Path


def generate(analysis, destination):
    root = Path.cwd()
    # Map every frozen Python module to its installed distribution, including
    # dependencies pulled in by hooks. Packages can belong to several distributions.
    owners = metadata.packages_distributions()
    names = set()
    for entry in (*analysis.pure, *analysis.binaries, *analysis.datas):
        module = entry[0].split(".")[0].split("/")[0].split("\\")[0]
        names.update(owners.get(module, ()))
    names.discard("vcf-logs-migrator")
    names.add("pyinstaller")  # The bootloader is linked into every executable.
    distributions = sorted(
        (metadata.distribution(name) for name in names), key=lambda d: d.metadata["Name"].lower()
    )
    lines = [
        "VCF Logs Migrator third-party notices",
        "",
        "Source and build instructions: https://github.com/sentania-labs/vcf-logs-migrator",
        "See README.md and packaging/build.py to rebuild against modified libraries.",
        "",
    ]
    for dist in distributions:
        name, version = dist.metadata["Name"], dist.version
        lines.extend([f"===== {name} {version} =====", ""])
        if name.lower().replace("_", "-") == "pyside6-essentials":
            from PySide6.QtCore import qVersion

            qt = qVersion()
            lines.extend(
                [
                    f"PySide6 source: https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{version}-src/pyside-setup-everywhere-src-{version}.tar.xz",
                    f"Qt source: https://download.qt.io/official_releases/qt/{'.'.join(qt.split('.')[:2])}/{qt}/single/qt-everywhere-src-{qt}.tar.xz",
                    "",
                ]
            )
        if name.lower() == "paramiko":
            lines.extend(
                [
                    f"Source: https://github.com/paramiko/paramiko/archive/refs/tags/{version}.tar.gz",
                    "",
                ]
            )
        found = []
        for file in dist.files or ():
            base = file.name.lower()
            if "licenses" in (part.lower() for part in file.parts) or base.startswith(
                ("license", "licence", "copying", "notice")
            ):
                path = Path(dist.locate_file(file))
                if path.is_file():
                    found.append(path)
        licenses = [
            p
            for p in found
            if p.name.lower().startswith(("license", "licence", "copying"))
            or "licenses" in (part.lower() for part in p.parts)
        ]
        # Qt for Python wheels omit license files; these distributions are offered
        # under LGPL-3.0 and the complete text is maintained with the build scripts.
        if not licenses and name.lower().replace("_", "-") in {"pyside6-essentials", "shiboken6"}:
            licenses = [root / "packaging" / "licenses" / "LGPL-3.0.txt"]
            found.extend(licenses)
        if not licenses:
            raise SystemExit(f"No license text found for bundled distribution {name} {version}")
        for path in sorted(set(found)):
            lines.extend([f"----- {path.name} -----", path.read_text(encoding="utf-8"), ""])
    python_paths = [
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix)
        / "share"
        / "licenses"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "LICENSE",
    ]
    python_license = next((p for p in python_paths if p.is_file()), None)
    if not python_license:
        raise SystemExit("Python license not found")
    lines.extend(
        [
            f"===== Python {sys.version.split()[0]} =====",
            python_license.read_text(encoding="utf-8"),
            "",
        ]
    )
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        lines.extend(
            [
                f"===== {name} =====",
                (root / "packaging" / "licenses" / name).read_text(encoding="utf-8"),
                "",
            ]
        )
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_text("\n".join(lines), encoding="utf-8")
