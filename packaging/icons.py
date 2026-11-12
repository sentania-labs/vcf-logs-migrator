"""Render the app icon (src/logs_migrate/gui/assets/app.svg) into the packagers' formats.

The release build calls ico() and icns() so no raster file is committed. To regenerate
by hand (needs the gui extra): `python packaging/icons.py [OUTDIR]` writes the .ico, the
.icns when iconutil exists (macOS), and 16/32/256 px PNG previews into OUTDIR (default build/).
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "src" / "logs_migrate" / "gui" / "assets" / "app.svg"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICNS_SIZES = (16, 32, 128, 256, 512)


def png_bytes(size: int) -> bytes:
    """The SVG as transparent PNG bytes, `size` pixels square."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        raise RuntimeError(f"Could not read {SVG}")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("Could not encode icon PNG")
    return bytes(data)


def ico(out: Path) -> Path:
    """One .ico holding every size as an embedded PNG (Windows Vista and later)."""
    images = [png_bytes(size) for size in ICO_SIZES]
    offset = 6 + 16 * len(ICO_SIZES)
    entries = []
    for size, data in zip(ICO_SIZES, images, strict=True):
        # A width or height byte of 0 means 256.
        entries.append(
            struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        )
        offset += len(data)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = struct.pack("<HHH", 0, 1, len(ICO_SIZES))
    out.write_bytes(header + b"".join(entries) + b"".join(images))
    return out


def icns(out: Path) -> Path:
    """An .icns with Retina variants; needs macOS's iconutil."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        iconset = Path(directory) / "app.iconset"
        iconset.mkdir()
        for size in ICNS_SIZES:
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                (iconset / f"icon_{size}x{size}{suffix}.png").write_bytes(png_bytes(size * scale))
        subprocess.run(["iconutil", "-c", "icns", "-o", str(out), str(iconset)], check=True)
    return out


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else ROOT / "build"
    print(ico(out / "logs-migrate.ico"))
    if shutil.which("iconutil"):
        print(icns(out / "logs-migrate.icns"))
    for size in (16, 32, 256):
        path = out / f"icon-{size}.png"
        path.write_bytes(png_bytes(size))
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
