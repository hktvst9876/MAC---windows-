"""Render the application icon to packaging/icons (PNG set, .ico and a macOS .iconset).

Usage: python tools/make_icons.py
On macOS, turn the iconset into .icns with:
    iconutil -c icns packaging/icons/MirrorBridge.iconset -o packaging/icons/MirrorBridge.icns
"""

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from mirrorbridge.ui.icons import app_pixmap  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "packaging" / "icons"


def png_bytes(size):
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    app_pixmap(size).save(buf, "PNG")
    return bytes(buf.data())


def write_ico(path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """ICO container with PNG-compressed images (supported since Windows Vista)."""
    images = [png_bytes(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for size, data in zip(sizes, images):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(header + entries + b"".join(images))


def main():
    QGuiApplication.instance() or QGuiApplication([])
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "MirrorBridge.png").write_bytes(png_bytes(512))
    write_ico(OUT / "MirrorBridge.ico")
    iconset = OUT / "MirrorBridge.iconset"
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        (iconset / ("icon_%dx%d.png" % (size, size))).write_bytes(png_bytes(size))
        (iconset / ("icon_%dx%d@2x.png" % (size, size))).write_bytes(png_bytes(size * 2))
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
