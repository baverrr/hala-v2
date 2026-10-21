"""The editor's document: a base image plus an undoable list of steps, rendered in order.

Steps are plain data, so rendering needs no widgets and is testable offscreen. Redaction burns
into the pixels (black or pixelated), and saving encodes a fresh copy of the pixels alone, so
nothing hidden survives in the result: not the covered pixels, not EXIF, XMP, PNG text chunks
or a thumbnail.
"""

from __future__ import annotations

import math
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QLineF, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QImageReader,
    QImageWriter,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
)

from . import formats as f
from .convert import _place
from .engines.images import check_picture
from .limits import check_pixels
from .runner import ConversionError

MIN_BLOCKS = 6  # a pixelated area is at least this many blocks wide or tall
MIN_BLOCK_PX = 6  # ...but blocks are never finer than this, so small text stays unreadable
# What we offer to save as, if this Qt has a writer for it (HEIC/AVIF need kimageformats).
WANTED_SAVE_EXTS = ("png", "jpg", "webp", "tiff", "bmp", "avif", "heic")
JPEG_QUALITY = 92
# Qt refuses images above 256 MB decoded (about 8k x 8k); camera panoramas are bigger.
ALLOCATION_LIMIT_MB = 2048
DEFAULT_COLOR = "#e03131"

Kind = Literal["crop", "rotate", "flip", "line", "arrow", "rect", "ellipse", "pen", "text",
               "black", "pixelate"]


def save_exts() -> tuple[str, ...]:
    writable = {bytes(b.data()).decode() for b in QImageWriter.supportedImageFormats()}
    return tuple(e for e in WANTED_SAVE_EXTS
                 if e in writable or (e == "jpg" and "jpeg" in writable))


@dataclass(frozen=True)
class Step:
    """One edit. Steps are never changed in place; undo just drops them."""

    kind: Kind
    rect: QRect | None = None  # crop, rect, ellipse, black, pixelate
    points: tuple[QPointF, ...] = ()  # line/arrow: two points; pen: the path; text: top left
    color: QColor = field(default_factory=lambda: QColor(DEFAULT_COLOR))
    width: float = 4.0  # line width; text uses size instead
    text: str = ""
    size: int = 28  # text pixel height
    amount: int = 0  # rotate: degrees clockwise (90/180/270); flip: 1 = horizontal, 2 = vertical


def load_image(path: Path) -> QImage:
    """The picture, upright (EXIF orientation applied), as ARGB. Formats Qt lacks go via vips."""
    check_picture(path)  # Qt's image plugins pick a decoder by content, like vips
    QImageReader.setAllocationLimit(ALLOCATION_LIMIT_MB)
    reader = QImageReader(str(path))
    if (size := reader.size()).isValid():  # from the header, before anything is decoded
        check_pixels(size.width(), size.height(), path.name)
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        qt_error = reader.errorString()
        if reader.error() != QImageReader.ImageReaderError.UnsupportedFormatError:
            raise ConversionError(f"can't open {path.name}: {qt_error}")
        image = _load_via_vips(path)
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def _load_via_vips(path: Path) -> QImage:
    try:
        with tempfile.TemporaryDirectory(prefix="hala-edit-") as tmp:
            png = Path(tmp) / "in.png"
            subprocess.run(["vips", "autorot", str(path), str(png)], check=True,
                           capture_output=True, timeout=120)
            image = QImage(str(png))
    except FileNotFoundError:
        raise ConversionError(f"can't open {path.name}: Qt can't read this format and vips "
                              "isn't installed (apt install libvips-tools)") from None
    except subprocess.TimeoutExpired:
        raise ConversionError(f"can't open {path.name}: reading it timed out") from None
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip().splitlines()
        raise ConversionError(f"can't open {path.name} as a picture"
                              + (f" ({detail[-1]})" if detail else "")) from None
    except OSError as exc:
        raise ConversionError(f"can't open {path.name}: {exc.strerror}") from None
    if image.isNull():
        raise ConversionError(f"can't open {path.name} as a picture")
    return image


class Document:
    """The base image and the steps on it, with undo/redo and a "saved" marker."""

    def __init__(self, base: QImage):
        self.base = base
        self._steps: list[Step] = []
        self._redo: list[Step] = []
        self._cache: QImage | None = None
        self._clean: tuple[Step, ...] = ()  # the steps the last save held (none: the original)

    @property
    def steps(self) -> tuple[Step, ...]:
        return tuple(self._steps)

    # --- editing ----------------------------------------------------------------------------

    def add(self, step: Step) -> None:
        self._steps.append(step)
        self._redo.clear()
        self._cache = None

    def undo(self) -> bool:
        if not self._steps:
            return False
        self._redo.append(self._steps.pop())
        self._cache = None
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._steps.append(self._redo.pop())
        self._cache = None
        return True

    @property
    def can_undo(self) -> bool:
        return bool(self._steps)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def edited(self) -> bool:
        """Differs from the original file (so there's something worth saving)."""
        return bool(self._steps)

    @property
    def modified(self) -> bool:
        """Differs from what was last saved (identity, not equality: undo + redraw counts)."""
        return (len(self._steps) != len(self._clean)
                or any(a is not b for a, b in zip(self._steps, self._clean, strict=True)))

    def mark_clean(self) -> None:
        self._clean = tuple(self._steps)

    # --- rendering --------------------------------------------------------------------------

    def render(self) -> QImage:
        """Every step applied to the base; cached until the steps change."""
        if self._cache is None:
            self._cache = render(self.base, self._steps)
        return self._cache


def render(base: QImage, steps: Sequence[Step]) -> QImage:
    image = base.copy()
    for step in steps:
        image = apply(image, step)
    return image


def apply(image: QImage, step: Step) -> QImage:
    if step.kind == "crop":
        assert step.rect is not None
        return image.copy(step.rect.intersected(image.rect()))
    if step.kind == "rotate":
        return image.transformed(QTransform().rotate(step.amount),
                                 Qt.TransformationMode.SmoothTransformation)
    if step.kind == "flip":
        return image.flipped(Qt.Orientation.Horizontal if step.amount == 1
                             else Qt.Orientation.Vertical)
    if step.kind == "pixelate":
        assert step.rect is not None
        _pixelate(image, step.rect)
        return image
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    try:
        _draw(painter, step)
    finally:
        painter.end()
    return image


def _pen(step: Step) -> QPen:
    pen = QPen(step.color, step.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
               Qt.PenJoinStyle.RoundJoin)
    return pen


def _draw(p: QPainter, step: Step) -> None:
    kind = step.kind
    p.setPen(_pen(step))
    p.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "black":
        assert step.rect is not None
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(step.rect, QColor(0, 0, 0))  # opaque, whatever the colour/alpha in use
    elif kind == "rect" and step.rect is not None:
        p.drawRect(step.rect)
    elif kind == "ellipse" and step.rect is not None:
        p.drawEllipse(step.rect)
    elif kind == "line" and len(step.points) >= 2:
        p.drawLine(step.points[0], step.points[-1])
    elif kind == "arrow" and len(step.points) >= 2:
        _arrow(p, step, step.points[0], step.points[-1])
    elif kind == "pen" and step.points:
        if len(step.points) == 1:
            p.drawPoint(step.points[0])
        else:
            path = QPainterPath(step.points[0])
            for pt in step.points[1:]:
                path.lineTo(pt)
            p.drawPath(path)
    elif kind == "text":
        if not (step.text and step.points):
            return
        font = QFont()
        font.setPixelSize(step.size)
        font.setBold(True)
        p.setFont(font)
        p.setPen(step.color)
        origin = step.points[0]
        for i, line in enumerate(step.text.splitlines() or [step.text]):
            p.drawText(QPointF(origin.x(), origin.y() + step.size * (1 + 1.2 * i)), line)
    elif kind not in ("rect", "ellipse", "line", "arrow", "pen"):
        raise ValueError(f"unknown edit step {kind!r}")


def _arrow(p: QPainter, step: Step, a: QPointF, b: QPointF) -> None:
    line = QLineF(a, b)
    if line.length() < 1:
        return
    head = max(12.0, step.width * 4.0)
    angle = math.atan2(-line.dy(), line.dx())
    spread = math.pi / 7
    left = QPointF(b.x() - head * math.cos(angle - spread), b.y() + head * math.sin(angle - spread))
    right = QPointF(b.x() - head * math.cos(angle + spread),
                    b.y() + head * math.sin(angle + spread))
    # The shaft stops inside the head, so a thick line doesn't poke out of the point.
    inset = head * 0.8
    shaft_end = QPointF(b.x() - inset * math.cos(angle), b.y() + inset * math.sin(angle))
    p.drawLine(a, shaft_end if line.length() > inset else b)
    p.setBrush(step.color)
    p.setPen(QPen(step.color, 1))
    p.drawPolygon(QPolygonF([b, left, right]))


def _pixelate(image: QImage, rect: QRect) -> None:
    area = rect.intersected(image.rect())
    if area.isEmpty():
        return
    block = max(MIN_BLOCK_PX, min(area.width(), area.height()) // MIN_BLOCKS)
    small = image.copy(area).scaled(
        max(1, math.ceil(area.width() / block)), max(1, math.ceil(area.height() / block)),
        Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    # Opaque blocks: with transparency left in, the original would show through a pixelated
    # area of a see-through PNG. Nearest-neighbour back up gives hard squares.
    small = small.convertToFormat(QImage.Format.Format_RGB32).convertToFormat(
        QImage.Format.Format_ARGB32)
    big = small.scaled(small.width() * block, small.height() * block,
                       Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.FastTransformation)
    painter = QPainter(image)
    try:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.setClipRect(area)
        painter.drawImage(QPoint(area.x(), area.y()), big)
    finally:
        painter.end()


# --- saving ---------------------------------------------------------------------------------


def output_ext(src: Path) -> str:
    ext = f.ext_of(src)
    return ext if ext in save_exts() else "png"  # gif/svg/jxl... come back as PNG


def _pixels_only(image: QImage, ext: str) -> QImage:
    """A fresh image holding just the pixels. Qt carries PNG text chunks (XMP, comments, GPS)
    from the loaded file through every copy and writes them back out; a new QImage has none.
    JPEG has no alpha, so it's flattened onto white instead of black."""
    alpha = image.hasAlphaChannel() and ext != "jpg"
    fresh = QImage(image.size(), QImage.Format.Format_ARGB32 if alpha
                   else QImage.Format.Format_RGB32)
    fresh.fill(Qt.GlobalColor.transparent if alpha else Qt.GlobalColor.white)
    painter = QPainter(fresh)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source if alpha
                               else QPainter.CompositionMode.CompositionMode_SourceOver)
    painter.drawImage(0, 0, image)
    painter.end()
    return fresh


def save_copy(image: QImage, src: Path, *, dest: Path | None = None) -> Path:
    """Write the edit as "name (edited).ext" beside the original (or as dest), never
    overwriting anything: a taken name becomes "name (2).ext". dest's extension is the format."""
    ext = f.ext_of(dest) if dest else output_ext(src)
    if ext not in save_exts():
        raise ConversionError(f"can't save as {ext or 'a file without an extension'}; "
                              f"pick one of {', '.join(save_exts())}")
    parent = dest.parent if dest else src.parent
    name = dest.name if dest else f"{f.stem_of(src)} (edited).{ext}"
    pixels = _pixels_only(image, ext)
    try:
        with tempfile.TemporaryDirectory(prefix=".hala-edit-", dir=parent) as tmp:
            scratch = Path(tmp) / f"out.{ext}"
            writer = QImageWriter(str(scratch), b"jpeg" if ext == "jpg" else ext.encode())
            if ext in ("jpg", "webp", "avif", "heic"):
                writer.setQuality(JPEG_QUALITY)
            if not writer.write(pixels) or not scratch.exists():
                raise ConversionError(f"couldn't write a {ext.upper()} file: "
                                      f"{writer.errorString()}")
            return _place(scratch, parent, name)
    except OSError as exc:
        raise ConversionError(f"can't save in {parent}: {exc.strerror or exc}") from None
