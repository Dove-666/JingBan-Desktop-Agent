"""Screen placement uses logical Qt coordinates, including on scaled displays."""

from PySide6.QtCore import QPoint, QRect, QSize
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
    return screen if screen is not None else QGuiApplication.primaryScreen()

