"""Deterministic speech placement around actual painted character bounds.

Every coordinate is a global, logical Qt coordinate. The caller measures text
and supplies the complete bubble size, including its scaled tail margin. No
screen queries, I/O, randomness, or mutable global state are used here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math

from PySide6.QtCore import QPointF, QRectF, QSizeF

from ._vendor.pyqttooltip_geometry import get_tooltip_rect


SIDES = ("top-right", "top-left", "right", "left", "top", "bottom-right", "bottom-left", "bottom")
_EPSILON = 1e-6


@dataclass(frozen=True)
class BubblePlacement:
    rect: QRectF
    side: str
    target: QPointF
    sequence: int
    constrained: bool


@dataclass(frozen=True)
class _Candidate:
    rect: QRectF
    side: str
    shift: float
    violations: tuple[float, float, float, float]
    score: float


def _finite_rect(rect: QRectF) -> bool:
    return all(math.isfinite(value) for value in (rect.x(), rect.y(), rect.width(), rect.height()))


def _area(rect: QRectF) -> float:
    return (rect.width() * rect.height()
            if rect.width() > _EPSILON and rect.height() > _EPSILON else 0.0)


def _nearest(point: QPointF, rect: QRectF) -> QPointF:
    return QPointF(max(rect.left(), min(point.x(), rect.right())),
                   max(rect.top(), min(point.y(), rect.bottom())))


def _distance(first: QPointF, second: QPointF) -> float:
    return math.hypot(first.x() - second.x(), first.y() - second.y())


def _clamp_rect(rect: QRectF, available: QRectF) -> QRectF:
    # Preserve the measured text window size. If it is larger than the work
    # area, pagination must reduce it in the caller; placement cannot reflow it.
    return QRectF(max(available.left(), min(rect.left(), available.right() - rect.width())),
                  max(available.top(), min(rect.top(), available.bottom() - rect.height())),
                  rect.width(), rect.height())


def place_bubble(available: QRectF, speaker: QRectF, bubble_size: QSizeF, *,
                 anchor: QPointF | None = None, head: QRectF | None = None,
                 obstacles: Sequence[QRectF] = (), gap: float = 10.0,
                 tail_margin: float = 11.0, sequence: int = 0,
                 previous: BubblePlacement | None = None) -> BubblePlacement:
    """Choose a nearby bubble position and a genuine speaker target.

    ``speaker`` is the visible alpha bounding box after the character pose,
    rather than its transparent sprite canvas. Optional ``head`` and ``anchor``
    refine face avoidance and tail aiming. ``obstacles`` can reserve tray or
    popup regions not already excluded from ``available``.

    Increment ``sequence`` for each new speech and pass the previous result on
    later position/animation updates. Preference varies deterministically per
    speech; the chosen side remains stable until it no longer fits. A small
    clamping allowance prevents a one-pixel animation at an edge flipping sides.

    The supplied size is preserved. ``constrained`` reports clamping, overlap,
    or an impossible oversized window, allowing callers to paginate first.
    """
    available, speaker = QRectF(available).normalized(), QRectF(speaker).normalized()
    size = QSizeF(bubble_size)
    if not _finite_rect(available) or available.isEmpty():
        raise ValueError("The available work area must be finite and nonempty")
    if (not all(math.isfinite(value) and value > 0 for value in (size.width(), size.height()))
            or not math.isfinite(gap) or gap < 0 or not math.isfinite(tail_margin) or tail_margin < 0):
        raise ValueError("Bubble size and spacing must be finite and nonnegative")
    if not _finite_rect(speaker) or speaker.isEmpty():
        center = available.center()
        speaker = QRectF(center.x() - .5, center.y() - .5, 1, 1)
    if head is not None and _finite_rect(QRectF(head)):
        head = QRectF(head).normalized().intersected(speaker)
    else:
        head = QRectF()
    if head.isEmpty():
        head = QRectF(speaker.left() + speaker.width() * .18,
                      speaker.top() + speaker.height() * .04,
                      speaker.width() * .64, speaker.height() * .32)
    target = QPointF(anchor) if anchor is not None else head.center()
    if not math.isfinite(target.x()) or not math.isfinite(target.y()):
        target = head.center()
    visible = speaker.intersected(available)
    target = _nearest(target, visible if not visible.isEmpty() else speaker)
    reserved = tuple(rect for item in obstacles
                     if _finite_rect(rect := QRectF(item).normalized()) and not rect.isEmpty())
    width, height = size.width(), size.height()
    margin = min(tail_margin, width * .45, height * .45)
    scale = max(36.0, math.sqrt(width * height))
    # Reuse pyqttooltip's adapted cardinal geometry. Offsets align it with the
    # visible face instead of the widget/canvas center; our complete measured
    # size already includes the tail margin, so no extra triangle is appended.
    offsets = {
        "top": QPointF(target.x() - speaker.center().x(), -gap),
        "bottom": QPointF(target.x() - speaker.center().x(), gap),
        "left": QPointF(-gap, target.y() - speaker.center().y()),
        "right": QPointF(gap, target.y() - speaker.center().y()),
    }
    positions = {side: get_tooltip_rect(speaker, side, size, offset=offset).topLeft()
                 for side, offset in offsets.items()}
    positions.update({
        # Diagonals stay near the face, rather than using the character's feet
        # as their vertical reference. Their bodies remain beside the speaker.
        "top-left": QPointF(speaker.left() - width - gap, target.y() - height * .92),
        "top-right": QPointF(speaker.right() + gap, target.y() - height * .92),
        "bottom-left": QPointF(speaker.left() - width - gap, target.y() - height * .08),
        "bottom-right": QPointF(speaker.right() + gap, target.y() - height * .08),
    })
    preference = SIDES[sequence % len(SIDES):] + SIDES[:sequence % len(SIDES)]
    candidates = {}
    for side, position in positions.items():
        raw = QRectF(position, size)
        rect = _clamp_rect(raw, available)
        body = rect.adjusted(margin, margin, -margin, -margin)
        shift = _distance(raw.topLeft(), rect.topLeft())
        overflow = max(0.0, _area(rect) - _area(rect.intersected(available)))
        violations = tuple(0.0 if value <= _EPSILON else value for value in (
            overflow, _area(body.intersected(head)), _area(body.intersected(speaker)),
            sum(_area(rect.intersected(obstacle)) for obstacle in reserved)))
        # Screen, face, body, and reserved regions take priority over variation.
        # Among clear choices, proximity limits long tails and the preference
        # rank supplies per-speech variety without random frame-to-frame jitter.
        # A card below the feet reads as a subtitle detached from the speaker.
        # Keep it available at constrained edges, but prefer clear alternatives
        # beside the face even when the sequence would otherwise favour bottom.
        score = (_distance(target, _nearest(target, body)) / scale
                 + .30 * preference.index(side) + .75 * shift / scale
                 + (3.0 if side == "bottom" else 0.0))
        candidates[side] = _Candidate(rect, side, shift, violations, score)
    selected = None
    if previous is not None and previous.sequence == sequence and previous.side in candidates:
        held = candidates[previous.side]
        if (not any(value > _EPSILON for value in held.violations)
                and held.shift <= max(2.0, margin * .5)):
            selected = held
    if selected is None:
        selected = min(candidates.values(), key=lambda item: (*item.violations, item.score))
    constrained = selected.shift > _EPSILON or any(value > _EPSILON for value in selected.violations)
    return BubblePlacement(QRectF(selected.rect), selected.side, QPointF(target), int(sequence), constrained)
