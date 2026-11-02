"""Render an SVG to a square PNG with Qt (no extra tools needed).

Usage: python packaging/render_icon.py <in.svg> <out.png> [size=256]
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src, dst = argv[1], argv[2]
    size = int(argv[3]) if len(argv) == 4 else 256
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    _app = QGuiApplication([argv[0]])
    renderer = QSvgRenderer(src)
    if not renderer.isValid():
        print(f"render_icon: invalid SVG: {src}", file=sys.stderr)
        return 1
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    if not image.save(dst, "PNG"):
        print(f"render_icon: could not write {dst}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
