"""Re-render the README's pictures (docs/*.png) offscreen, from generated sample files.

    .venv/bin/python -I tools/screenshots.py

Nothing is converted and no window appears. The sample files live in a temporary folder
named "Holiday", so no real paths or file names end up in the pictures.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HALA_NO_LAYERSHELL"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QPointF, QRect  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

DOCS = ROOT / "docs"
BREEZE_ACCENT = QColor("#3daee9")  # Plasma's default accent, rather than the author's own


def save(image: QImage | QPixmap, name: str) -> None:
    path = DOCS / name
    if not image.save(str(path)):
        sys.exit(f"couldn't write {path}")
    print(path)


def photo(path: Path, w: int = 960, h: int = 640, sun: tuple[float, float] = (0.7, 0.3)) -> Path:
    """A made-up landscape: sky, sun, two hills and a sea."""
    img = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    sky = QLinearGradient(0, 0, 0, h)
    sky.setColorAt(0, QColor("#2b6cb0"))
    sky.setColorAt(0.6, QColor("#f6ad55"))
    p.fillRect(img.rect(), sky)
    glow = QRadialGradient(QPointF(w * sun[0], h * sun[1]), h * 0.25)
    glow.setColorAt(0, QColor("#fffbe6"))
    glow.setColorAt(0.3, QColor("#ffd27a"))
    glow.setColorAt(1, QColor(255, 210, 122, 0))
    p.fillRect(img.rect(), glow)
    p.fillRect(QRect(0, int(h * 0.72), w, h), QColor("#1f5f7a"))
    for x0, peak, colour in ((-0.1, 0.42, "#2f5d3a"), (0.35, 0.5, "#3f7a46")):
        hill = QPainterPath(QPointF(w * x0, h * 0.75))
        hill.quadTo(QPointF(w * (x0 + 0.35), h * peak), QPointF(w * (x0 + 0.75), h * 0.75))
        p.fillPath(hill, QColor(colour))
    p.end()
    img.save(str(path))
    return path


def backdrop(w: int, h: int) -> QPixmap:
    """Something like a desktop wallpaper for the transparent wheel to sit on."""
    pix = QPixmap(w, h)
    p = QPainter(pix)
    grad = QLinearGradient(0, 0, w, h)
    grad.setColorAt(0, QColor("#1d2b3a"))
    grad.setColorAt(1, QColor("#3b2f4a"))
    p.fillRect(pix.rect(), grad)
    p.end()
    return pix


def wheel(files: list[Path], name: str, hover: int) -> None:
    from hala.gui.geometry import BUBBLE_LABEL, BUBBLE_RADIUS
    from hala.gui.overlay import Overlay

    screen = QGuiApplication.primaryScreen()
    overlay = Overlay(screen, lambda *_: None)
    overlay.resize(1400, 1200)
    overlay.begin(files, QPointF(700, 560))
    w = overlay.wheel
    assert w is not None
    overlay.hovered = w.targets[hover]
    canvas = backdrop(overlay.width(), overlay.height())
    p = QPainter(canvas)
    overlay.render(p, QPoint())
    p.end()
    overlay.finish(None)
    # The wheel and its bubbles (with their labels), not the whole screen.
    reach = w.outer + 24
    lowest = max((by for _t, _bx, by in w.bubbles), default=w.cy)
    bottom = lowest + BUBBLE_RADIUS + BUBBLE_LABEL + 16
    top = w.cy - reach
    save(canvas.copy(QRect(int(w.cx - reach), int(top), int(2 * reach),
                           int(max(bottom, w.cy + reach) - top))), name)


def trim_dialog(clip: Path) -> None:
    from hala.gui.dialog import ToolDialog
    from hala.tools import tool

    dialog = ToolDialog(tool("trim"), [clip])
    dialog.show()
    for _ in range(20):
        QApplication.processEvents()
    save(dialog.grab(), "trim.png")
    dialog.close()


def shelf(files: list[Path]) -> None:
    from hala.gui.shelf import Shelf

    widget = Shelf()
    widget.add(files)
    widget.show()
    QApplication.processEvents()
    canvas = backdrop(widget.width() + 40, widget.height() + 40)
    p = QPainter(canvas)
    widget.render(p, QPointF(20, 20).toPoint())
    p.end()
    save(canvas, "shelf.png")
    widget.close()


def editor(picture: Path) -> None:
    from hala.editing import Step
    from hala.gui.editor import Editor

    window = Editor(picture)
    doc = window.doc
    doc.add(Step("arrow", points=(QPointF(250, 470), QPointF(560, 330)), width=8))
    doc.add(Step("ellipse", rect=QRect(560, 120, 240, 240), width=6))
    doc.add(Step("text", points=(QPointF(60, 560),), text="Best sunset of the trip",
                 color=QColor("#ffffff"), size=44))
    doc.add(Step("text", points=(QPointF(560, 600),), text="Booking ref: K7Q-29X4",
                 color=QColor("#ffffff"), size=30))
    doc.add(Step("pixelate", rect=QRect(552, 594, 380, 48)))
    window.canvas.changed.emit()
    window.resize(1000, 760)
    window.show()
    for _ in range(10):
        QApplication.processEvents()
    save(window.grab(), "editor.png")
    window.hide()  # close() would ask whether to save the edits


def main() -> None:
    app = QApplication(sys.argv[:1])
    # Offscreen Qt has no platform theme, so it only looks in its own resources for icons.
    QIcon.setThemeSearchPaths(["/usr/share/icons", *QIcon.themeSearchPaths()])
    QIcon.setThemeName("breeze")
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Highlight, BREEZE_ACCENT)
    app.setPalette(palette)
    app.setFont(QFont("Noto Sans", 10))
    DOCS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "Holiday"
        folder.mkdir()
        pics = [photo(folder / "beach.png"),
                photo(folder / "sunset.png", sun=(0.3, 0.45)),
                photo(folder / "harbour.png", sun=(0.55, 0.2))]
        clip = folder / "boat trip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=size=640x360:rate=25:duration=12", "-f", "lavfi", "-i",
                        "sine=frequency=330:duration=12", "-c:v", "libx264", "-pix_fmt",
                        "yuv420p", "-c:a", "aac", "-shortest", str(clip)], check=True)
        song = folder / "ukulele.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=2", str(song)], check=True)
        wheel(pics, "wheel.png", hover=1)
        wheel([clip], "wheel-video.png", hover=2)
        trim_dialog(clip)
        shelf([*pics, clip, song])
        editor(pics[1])
    del app


if __name__ == "__main__":
    main()
