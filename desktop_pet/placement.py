"""Screen placement uses logical Qt coordinates, including on scaled displays."""

import math

from PySide6.QtCore import QPoint, QRect, QRectF, QSize
from PySide6.QtGui import QGuiApplication, QScreen


def clamp_position(position: QPoint, size: QSize, available: QRect) -> QPoint:
    """Keep the entire window outside the taskbar and inside one work area."""
    max_x = available.x() + max(0, available.width() - size.width())
    max_y = available.y() + max(0, available.height() - size.height())
    return QPoint(
        max(available.x(), min(position.x(), max_x)),
        max(available.y(), min(position.y(), max_y)),
    )


def screen_for_point(point: QPoint) -> QScreen:
    screen = QGuiApplication.screenAt(point)
    return screen if screen is not None else screen_for_rect(QRectF(point.x(), point.y(), 1, 1))


def clamp_visible_position(position: QPoint, footprint: QRectF, available: QRect) -> QPoint:
    """Bound painted content; its transparent window margins may cross an edge.

    QRectF uses continuous, half-open pixel extents. Round the permitted window
    positions inward, so fractional breathing bounds cannot cover the taskbar.
    """
    bounds = QRectF(footprint)
    if not available.isValid():
        return QPoint(position)
    if not bounds.isValid() or not all(math.isfinite(v) for v in
            (bounds.x(), bounds.y(), bounds.width(), bounds.height())):
        return available.topLeft()

    def limit(value, start, extent, offset, size):
        low = math.ceil(start - offset - 1e-9)
        high = math.floor(start + extent - offset - size + 1e-9)
        return low if high < low else max(low, min(value, high))

    return QPoint(
        limit(position.x(), available.x(), available.width(), bounds.x(), bounds.width()),
        limit(position.y(), available.y(), available.height(), bounds.y(), bounds.height()),
    )


def screen_for_rect(rect: QRectF) -> QScreen:
    """Choose by visible overlap, or the nearest display after unplugging one."""
    screens = QGuiApplication.screens()
    if not screens:
        return QGuiApplication.primaryScreen()
    bounds = QRectF(rect)
    center = bounds.center()

    def score(screen):
        area = QRectF(screen.availableGeometry())
        overlap = area.intersected(bounds)
        dx = max(area.left() - center.x(), 0.0, center.x() - area.right())
        dy = max(area.top() - center.y(), 0.0, center.y() - area.bottom())
        return max(0.0, overlap.width()) * max(0.0, overlap.height()), -(dx * dx + dy * dy)

    return max(screens, key=score)

