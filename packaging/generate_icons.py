"""Render the native brand SVG and export Mac/Windows icon containers.

Development-only dependencies: PySide6 and Pillow. No image generation or
runtime dependency is required to reproduce the existing sidebar scissors.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main():
    root = Path(__file__).resolve().parents[1]
    icons = root / 'packaging/resources/icons'
    renderer = QSvgRenderer(str(icons / 'yijianjifa.svg'))
    if not renderer.isValid():
        raise SystemExit('Invalid brand SVG')
    canvas = QImage(1024, 1024, QImage.Format.Format_ARGB32)
    canvas.fill(0)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, 1024, 1024))
    painter.end()
    if not canvas.save(str(icons / 'yijianjifa.png')):
        raise SystemExit('Cannot write brand PNG')
    with Image.open(icons / 'yijianjifa.png') as source:
        source.save(icons / 'yijianjifa.ico', sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
        source.resize((256, 256), Image.Resampling.LANCZOS).save(root / 'src/yijianjifa/webui/assets/app-icon.png')
        if sys.platform == 'darwin':
            with tempfile.TemporaryDirectory() as temporary:
                iconset = Path(temporary) / 'yijianjifa.iconset'
                iconset.mkdir()
                for size in (16, 32, 128, 256, 512):
                    for scale in (1, 2):
                        suffix = '@2x' if scale == 2 else ''
                        source.resize((size * scale, size * scale), Image.Resampling.LANCZOS).save(iconset / f'icon_{size}x{size}{suffix}.png')
                subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(icons / 'yijianjifa.icns')], check=True)


if __name__ == '__main__':
    main()
