"""The format wheel's geometry, kept free of Qt so it can be tested on its own.

Angles are in degrees, 0 at 12 o'clock, growing clockwise (screen y points down).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

INNER_RADIUS = 72.0  # the hole in the middle: dropping there cancels
TOOL_RING = 84.0  # width of the inner ring of tool petals, when there are tools
# Wheel targets for tools carry this prefix ("tool:compress"), actions "action:" ("action:zip");
# formats are bare ("jpg").
TOOL_PREFIX = "tool:"
ACTION_PREFIX = "action:"
# Action bubbles: a row of circles just outside the ring, below it (above, near the bottom).
BUBBLE_RADIUS = 30.0
BUBBLE_GAP = 14.0  # between neighbouring bubbles
BUBBLE_OFFSET = 30.0  # from the ring's rim to the bubbles' near edge
BUBBLE_LABEL = 26.0  # the label pill under each bubble
BUBBLE_ROW = BUBBLE_OFFSET + 2 * BUBBLE_RADIUS + BUBBLE_LABEL
MIN_OUTER_RADIUS = 190.0
RADIUS_PER_TARGET = 14.0
# A drop this far past the ring still counts as aimed at a segment.
OUTER_SLACK = 48.0
EDGE_MARGIN = 24.0


@dataclass(frozen=True)
class Segment:
    target: str
    start: float  # degrees
    span: float

    @property
    def middle(self) -> float:
        return self.start + self.span / 2


@dataclass(frozen=True)
class Wheel:
    """Format petals in a ring from `split` (or `inner`, without tools) out to `outer`; tool
    petals, if any, in the ring between `inner` and `split`; action bubbles in a row below
    the ring (or above, when `actions_above`)."""

    targets: tuple[str, ...]
    cx: float
    cy: float
    inner: float
    outer: float
    tools: tuple[str, ...] = ()
    split: float = 0.0
    actions: tuple[str, ...] = ()
    actions_above: bool = False

    @property
    def format_inner(self) -> float:
        return self.split if self.tools else self.inner

    @property
    def segments(self) -> list[Segment]:
        return _ring(self.targets)

    @property
    def tool_segments(self) -> list[Segment]:
        return _ring(tuple(TOOL_PREFIX + t for t in self.tools))

    @property
    def bubbles(self) -> list[tuple[str, float, float]]:
        """("action:name", x, y) for each action bubble's centre, left to right."""
        if not self.actions:
            return []
        step = 2 * BUBBLE_RADIUS + BUBBLE_GAP
        first = self.cx - step * (len(self.actions) - 1) / 2
        reach = self.outer + BUBBLE_OFFSET + BUBBLE_RADIUS
        y = self.cy - reach if self.actions_above else self.cy + reach
        return [(ACTION_PREFIX + a, first + i * step, y) for i, a in enumerate(self.actions)]

    def target_at(self, x: float, y: float) -> str | None:
        """The format, "tool:name" or "action:name" under (x, y); None for the middle and
        well outside."""
        for target, bx, by in self.bubbles:  # checked first: they sit in the ring's slack
            if self._in_bubble(x, y, bx, by):
                return target
        dx, dy = x - self.cx, y - self.cy
        distance = math.hypot(dx, dy)
        if distance < self.inner or distance > self.outer + OUTER_SLACK:
            return None
        angle = math.degrees(math.atan2(dx, -dy)) % 360.0
        if self.tools and distance < self.split:
            return _pick(tuple(TOOL_PREFIX + t for t in self.tools), angle)
        return _pick(self.targets, angle)

    def _in_bubble(self, x: float, y: float, bx: float, by: float) -> bool:
        """The bubble plus its label, which sits on the side away from the ring: a drop on
        the label must pick the action, not the petal behind it."""
        reach = BUBBLE_RADIUS + BUBBLE_GAP / 2
        top, bottom = ((by - BUBBLE_RADIUS - BUBBLE_LABEL, by + reach) if self.actions_above
                       else (by - reach, by + BUBBLE_RADIUS + BUBBLE_LABEL))
        return abs(x - bx) <= reach and top <= y <= bottom

    def point_at(self, angle: float, radius: float) -> tuple[float, float]:
        rad = math.radians(angle)
        return self.cx + radius * math.sin(rad), self.cy - radius * math.cos(rad)


def _ring(names: tuple[str, ...]) -> list[Segment]:
    if not names:
        return []
    span = 360.0 / len(names)
    return [Segment(n, i * span, span) for i, n in enumerate(names)]


def _pick(names: tuple[str, ...], angle: float) -> str | None:
    if not names:
        return None
    return names[min(int(angle // (360.0 / len(names))), len(names) - 1)]


def outer_radius(count: int) -> float:
    return max(MIN_OUTER_RADIUS, 60.0 + RADIUS_PER_TARGET * count)


def place(targets: list[str], x: float, y: float, width: float, height: float,
          tools: list[str] | None = None, actions: list[str] | None = None) -> Wheel:
    """A wheel centred near (x, y) but pulled inwards so it fits on a width x height screen.

    The pointer usually arrives at a screen edge (that's where the drop strip is), so the
    wheel opens just inside the edge, next to the pointer rather than under it. Action
    bubbles go below the ring, or above it when the wheel sits low on the screen.
    """
    tools, actions = tools or [], actions or []
    split = INNER_RADIUS + TOOL_RING if tools else 0.0
    outer = outer_radius(len(targets)) + (TOOL_RING if tools else 0.0)
    reach = outer + EDGE_MARGIN
    row = BUBBLE_ROW if actions else 0.0
    # Sideways the bubble row may be wider than the ring.
    half_row = (len(actions) * (2 * BUBBLE_RADIUS + BUBBLE_GAP) - BUBBLE_GAP) / 2
    side = max(reach, half_row + EDGE_MARGIN)
    cx = min(max(x, side), width - side) if width > 2 * side else width / 2
    # Vertically: room for the ring plus the row on one side, whichever side has it.
    above = False
    if height > 2 * reach + row:
        cy = min(max(y, reach), height - reach - row)
        if y > height - reach - row and y >= reach + row:  # low on the screen: row on top
            cy, above = min(max(y, reach + row), height - reach), True
    elif height - outer - row >= outer:
        cy = min(max(y, outer), height - outer - row)  # tight: drop the margins, keep it all
    elif actions and height - outer - row > 0:
        # Too short for everything: the bubbles stay whole (a petal can be hit half-shown).
        cy = height - outer - row
    else:
        cy = height / 2
    return Wheel(tuple(targets), cx, cy, INNER_RADIUS, outer, tuple(tools), split,
                 tuple(actions), above)
