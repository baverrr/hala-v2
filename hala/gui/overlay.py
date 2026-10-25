"""The full-screen format wheel: appears mid-drag, and the drop onto a segment picks the target.

It can also be opened in click mode (files already dropped, pick with the mouse) for when a
drop lands on the strip itself.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QEnterEvent,
    QFont,
    QFontMetricsF,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPaintEvent,
    QPalette,
    QPen,
    QRadialGradient,
    QScreen,
)
from PySide6.QtWidgets import QWidget

from .. import formats as f
from ..actions import ACTIONS, actions_for
from ..i18n import _, n_files
from ..registry import common_targets
from ..tools import tools, tools_for
from . import layershell as ls
from .geometry import ACTION_PREFIX, BUBBLE_RADIUS, TOOL_PREFIX, Segment, Wheel, place
from .preview import human_size, total_size

Launch = Callable[[str, list[Path]], None]
log = logging.getLogger("hala")
# How far the hovered petal pops out of the ring.
HOVER_GROW = 12.0
# Petals: frosted red, rounded, with a small gap between neighbours.
PETAL = QColor(196, 32, 44)
PETAL_GAP = 5.0
PETAL_ROUNDING = 12.0
# Tool petals: frosted white, so the two rings read as different things.
TOOL_PETAL = QColor(255, 255, 255, 215)
# Barely-there wash: enough for the compositor to treat the whole surface as a drop target.
WASH = QColor(0, 0, 0, 14)


def _room(seg: Segment, radius: float, depth: float) -> float:
    """Usable label width across a petal: its chord at radius, minus padding, capped by how
    far a horizontal label can go within the ring's depth."""
    chord = 2 * radius * math.sin(math.radians(min(seg.span, 180.0)) / 2)
    return max(24.0, min(chord - PETAL_GAP - 2 * PETAL_ROUNDING, depth * 1.6, 130.0))


def petal_path(wheel: Wheel, seg: Segment, inner: float, outer: float) -> QPainterPath:
    """An annular sector with rounded corners and a gap to its neighbours.

    Built as the sector shrunk by the corner radius, then grown back by stroking it with a
    round-joined pen: that rounds every corner by exactly that radius.
    """
    room = (inner * math.radians(seg.span) - PETAL_GAP) / 2  # half the petal's inner width
    r = max(2.0, min(PETAL_ROUNDING, room / 2, (outer - inner) / 4))
    ri, ro = inner + r, outer - r

    def inset(radius: float) -> float:  # degrees lost at each side for gap + rounding
        return math.degrees((PETAL_GAP / 2 + r) / radius)

    a0o, a1o = seg.start + inset(ro), seg.start + seg.span - inset(ro)
    a0i, a1i = seg.start + inset(ri), seg.start + seg.span - inset(ri)
    if a1i <= a0i:
        a0i = a1i = seg.middle
    ring_o = QRectF(wheel.cx - ro, wheel.cy - ro, 2 * ro, 2 * ro)
    ring_i = QRectF(wheel.cx - ri, wheel.cy - ri, 2 * ri, 2 * ri)
    # Qt measures arcs from 3 o'clock, counter-clockwise; ours start at 12, clockwise.
    core = QPainterPath()
    core.arcMoveTo(ring_o, 90 - a0o)
    core.arcTo(ring_o, 90 - a0o, -(a1o - a0o))
    core.arcTo(ring_i, 90 - a1i, a1i - a0i)
    core.closeSubpath()
    stroker = QPainterPathStroker()
    stroker.setWidth(2 * r)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    return core.united(stroker.createStroke(core)).simplified()


def local_files(event: QDragEnterEvent | QDropEvent) -> list[Path]:
    return [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]


class Overlay(QWidget):
    def __init__(self, screen: QScreen, launch: Launch):
        super().__init__()
        self.launch = launch
        self.files: list[Path] = []
        self._targets: list[str] = []
        self._tools: list[str] = []
        self._actions: list[str] = []
        self._size = ""
        self.wheel: Wheel | None = None
        self.hovered: str | None = None
        self.click_mode = False
        # For a wheel-only process (`hala wheel`): told when this overlay's wheel is placed,
        # and when it closes.
        self.on_place: Callable[[Overlay], None] | None = None
        self.on_close: Callable[[Overlay], None] | None = None
        self.setWindowTitle("Hala")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setGeometry(screen.geometry())
        self.winId()
        handle = self.windowHandle()
        if handle is None or not ls.configure(
                handle, anchors=ls.Anchor.TOP | ls.Anchor.BOTTOM | ls.Anchor.LEFT
                | ls.Anchor.RIGHT, layer=ls.Layer.OVERLAY, screen=screen,
                keyboard=ls.Keyboard.ON_DEMAND):
            # No layer shell (X11, tests): a plain frameless full-screen window.
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)

    # --- opening and closing ----------------------------------------------------------------

    def begin(self, files: list[Path], at: QPointF | None = None, click_mode: bool = False) -> None:
        """Show the wheel for files; it's placed at `at`, or where the drag first moves."""
        self.files, self.click_mode, self.hovered = files, click_mode, None
        targets = common_targets([f.ext_of(p) for p in files]) if files else []
        self.wheel = None
        self._targets = targets
        self._tools = [t.name for t in tools_for(files)] if files else []
        self._actions = actions_for(files)
        size = total_size(files)
        self._size = human_size(size) if size is not None else ""
        if at is not None:
            self._place(at)
        self.show()
        if click_mode:
            self.activateWindow()
        self.update()

    def finish(self, target: str | None) -> None:
        files = self.files
        self.hide()
        self.files, self.wheel, self.hovered = [], None, None
        if target is not None and files:
            self.launch(target, files)
        if self.on_close is not None:
            self.on_close(self)

    def _place(self, at: QPointF) -> None:
        self.wheel = place(self._targets, at.x(), at.y(), self.width(), self.height(),
                           tools=self._tools, actions=self._actions)
        if self.on_place is not None:
            self.on_place(self)

    def _hover(self, at: QPointF) -> None:
        if self.wheel is None:
            self._place(at)
        assert self.wheel is not None
        hovered = self.wheel.target_at(at.x(), at.y())
        if hovered != self.hovered:
            log.debug("hover %s at (%.0f, %.0f)", hovered, at.x(), at.y())
            self.hovered = hovered
            self.update()

    # --- drag (the normal way in) -----------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not self.files:  # opened by a drag that skipped the strip somehow
            self.files = local_files(event)
            self._targets = common_targets([f.ext_of(p) for p in self.files])
            self._tools = [t.name for t in tools_for(self.files)]
            self._actions = actions_for(self.files)
        event.acceptProposedAction()
        self._hover(event.position())

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        event.acceptProposedAction()
        self._hover(event.position())

    def dragLeaveEvent(self, _event: object) -> None:
        self.finish(None)  # dragged off this screen, or the drag was cancelled

    def dropEvent(self, event: QDropEvent) -> None:
        event.acceptProposedAction()
        target = self.wheel.target_at(event.position().x(), event.position().y()) \
            if self.wheel else None
        self.finish(target)

    # --- click mode -------------------------------------------------------------------------

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.click_mode:
            self._hover(event.position())

    def enterEvent(self, event: QEnterEvent) -> None:
        # Opened by a click or a shortcut: the wheel appears where the pointer comes in (on
        # Wayland that's the first time the pointer's position is known at all).
        if self.click_mode and self.wheel is None:
            self._place(event.position())
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.click_mode:
            return
        pos = event.position()
        left = event.button() == Qt.MouseButton.LeftButton
        self.finish(self.wheel.target_at(pos.x(), pos.y()) if left and self.wheel else None)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.finish(None)

    # --- drawing ----------------------------------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), WASH)
        wheel = self.wheel
        if wheel is None:
            return
        self._shadow(p, wheel)
        if not self.files:
            self._centre(p, wheel, _("Only local files can be used"))
            return
        # A muted accent (e.g. Breeze's greyish lavender) must still stand out against red,
        # so the hovered petal is also brightened, outlined and popped out.
        accent = self.palette().color(QPalette.ColorRole.Highlight).lighter(125)
        label_font = QFont(self.font())
        label_font.setPointSizeF(max(8.5, min(12.0, 170.0 / len(wheel.targets))))
        label_font.setWeight(QFont.Weight.DemiBold)
        for seg in sorted(wheel.segments, key=lambda s: s.target == self.hovered):
            hot = seg.target == self.hovered
            inner = wheel.format_inner
            outer = wheel.outer + (HOVER_GROW if hot else 0.0)
            if hot:
                p.setBrush(accent)
                p.setPen(QPen(QColor(255, 255, 255, 235), 2.5))
            else:
                # Frosted look: deeper red towards the middle, lighter at the rim.
                glass = QRadialGradient(QPointF(wheel.cx, wheel.cy), outer)
                deep, light = QColor(PETAL), PETAL.lighter(135)
                deep.setAlpha(225)
                light.setAlpha(190)
                glass.setColorAt(inner / outer, deep)
                glass.setColorAt(1.0, light)
                p.setBrush(QBrush(glass))
                p.setPen(QPen(QColor(255, 255, 255, 90), 1.2))
            p.drawPath(petal_path(wheel, seg, inner, outer))
            mid = (inner + outer) / 2
            x, y = wheel.point_at(seg.middle, mid)
            self._label(p, x, y, f.label(seg.target), label_font, QColor("white"),
                        _room(seg, mid, outer - inner), shadow=True)
        self._tool_ring(p, wheel, accent)
        self._bubbles(p, wheel, accent)
        count = len(self.files)
        self._centre(p, wheel, self.files[0].name if count == 1 else n_files(count))

    def _tool_ring(self, p: QPainter, wheel: Wheel, accent: QColor) -> None:
        """Tool petals between the white middle and the formats: frosted white, red labels."""
        if not wheel.tools:
            return
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, min(10.5, 150.0 / len(wheel.tools))))
        font.setWeight(QFont.Weight.DemiBold)
        registry = tools()
        for seg in wheel.tool_segments:
            hot = seg.target == self.hovered
            p.setBrush(accent if hot else TOOL_PETAL)
            p.setPen(QPen(QColor(255, 255, 255, 235), 2.5) if hot
                     else QPen(QColor(PETAL.red(), PETAL.green(), PETAL.blue(), 70), 1.0))
            p.drawPath(petal_path(wheel, seg, wheel.inner, wheel.split - PETAL_GAP / 2))
            mid = (wheel.inner + wheel.split) / 2
            x, y = wheel.point_at(seg.middle, mid)
            t = registry[seg.target.removeprefix(TOOL_PREFIX)]
            self._label(p, x, y, _(t.short or t.label), font,
                        QColor("white") if hot else PETAL.darker(115),
                        _room(seg, mid, wheel.split - wheel.inner))

    def _bubbles(self, p: QPainter, wheel: Wheel, accent: QColor) -> None:
        """Action bubbles: frosted white circles with the action's icon, label underneath."""
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 0.5))
        font.setWeight(QFont.Weight.DemiBold)
        for target, x, y in wheel.bubbles:
            action = ACTIONS[target.removeprefix(ACTION_PREFIX)]
            label = _(action.label)
            hot = target == self.hovered
            radius = BUBBLE_RADIUS + (4.0 if hot else 0.0)
            p.setBrush(accent if hot else TOOL_PETAL)
            p.setPen(QPen(QColor(255, 255, 255, 235), 2.5) if hot
                     else QPen(QColor(PETAL.red(), PETAL.green(), PETAL.blue(), 70), 1.0))
            p.drawEllipse(QPointF(x, y), radius, radius)
            icon = QIcon.fromTheme(action.icon)
            side = 26
            if icon.isNull():  # no icon theme: the label's first letter
                p.setFont(font)
                p.setPen(QColor("white") if hot else PETAL)
                p.drawText(QRectF(x - radius, y - radius, 2 * radius, 2 * radius),
                           Qt.AlignmentFlag.AlignCenter, label[:1])
            else:
                p.drawPixmap(int(x - side / 2), int(y - side / 2), icon.pixmap(side, side))
            # The label sits on the desktop, not on a petal: give it a red pill to stand on.
            p.setFont(font)
            metrics = QFontMetricsF(font)
            width = metrics.horizontalAdvance(label) + 14
            tall = metrics.height() + 6
            # On the side away from the ring, so it never covers a petal.
            top = y - radius - 5 - tall if wheel.actions_above else y + radius + 5
            pill = QRectF(x - width / 2, top, width, tall)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent if hot else PETAL)
            p.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            p.setPen(QColor("white"))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, label)

    @staticmethod
    def _label(p: QPainter, x: float, y: float, text: str, font: QFont, colour: QColor,
               room: float, shadow: bool = False) -> None:
        """Centred at (x, y), shrunk (then elided) to fit `room` pixels of petal width."""
        font = QFont(font)
        while QFontMetricsF(font).horizontalAdvance(text) > room and font.pointSizeF() > 7.5:
            font.setPointSizeF(font.pointSizeF() - 0.5)
        text = QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, room)
        box = QRectF(x - room / 2, y - 14, room, 28)
        p.setFont(font)
        if shadow:  # soft shadow keeps white text crisp on any red
            p.setPen(QColor(0, 0, 0, 90))
            p.drawText(box.translated(1, 1), Qt.AlignmentFlag.AlignCenter, text)
        p.setPen(colour)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _shadow(self, p: QPainter, wheel: Wheel) -> None:
        reach = wheel.outer + HOVER_GROW + 26
        shade = QRadialGradient(QPointF(wheel.cx, wheel.cy + 6), reach)
        shade.setColorAt((wheel.outer - 30) / reach, QColor(0, 0, 0, 70))
        shade.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(shade))
        p.drawEllipse(QPointF(wheel.cx, wheel.cy + 6), reach, reach)

    def _centre(self, p: QPainter, wheel: Wheel, name: str) -> None:
        """White disc, a red size pill in the middle and a name pill along the bottom."""
        radius = wheel.inner - 6
        disc = QRectF(wheel.cx - radius, wheel.cy - radius, 2 * radius, 2 * radius)
        p.setPen(QPen(QColor(0, 0, 0, 45), 1.5))  # keeps the disc visible on a white desktop
        p.setBrush(QColor(255, 255, 255, 245))
        p.drawEllipse(disc)

        small = QFont(self.font())
        small.setPointSizeF(max(8.0, small.pointSizeF() - 1))
        small.setWeight(QFont.Weight.DemiBold)
        p.setFont(small)
        metrics = p.fontMetrics()
        if self._size:
            w = metrics.horizontalAdvance(self._size) + 18
            pill = QRectF(wheel.cx - w / 2, wheel.cy - 11, w, 22)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(PETAL)
            p.drawRoundedRect(pill, 11, 11)
            p.setPen(QColor("white"))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, self._size)

        accent = self.palette().color(QPalette.ColorRole.Highlight)
        text = metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, int(2 * wheel.inner + 40))
        w = metrics.horizontalAdvance(text) + 20
        pill = QRectF(wheel.cx - w / 2, wheel.cy + radius - 14, w, 24)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawRoundedRect(pill, 7, 7)
        p.setPen(QColor("white"))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
