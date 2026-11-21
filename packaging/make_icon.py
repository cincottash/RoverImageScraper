"""Draw the app icon (packaging/icon.svg) as PNG or as a Windows .ico, using Qt (already installed for the app)."""
from __future__ import annotations

import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_SVG = os.path.join(HERE, "icon.svg")
_app = None


def render_png(size: int, svg: str = ICON_SVG) -> bytes:
    """The icon drawn at size x size pixels, as PNG bytes."""
    global _app
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    if QGuiApplication.instance() is None:           # Qt needs an application object to draw
        _app = QGuiApplication([])
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(svg).render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def write_png(path: str, size: int = 256) -> None:
    with open(path, "wb") as f:
        f.write(render_png(size))


def write_ico(path: str, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    """A Windows .ico holding the icon at several sizes (PNG-compressed entries, supported since Windows Vista)."""
    images = [render_png(n) for n in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for n, png in zip(sizes, images):
        dim = 0 if n >= 256 else n            # 0 means 256 in the .ico format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    with open(path, "wb") as f:
        f.write(header + entries + b"".join(images))
