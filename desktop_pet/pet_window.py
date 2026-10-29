"""Cached sprite rendering and pointer interaction driven by one frame timer."""

import math
from typing import Callable

from PySide6.QtCore import QElapsedTimer, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QGuiApplication, QImage, QImageReader, QMouseEvent, QPainter, QPixmap, QTransform,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from .bubble import SpeechBubble
from .animation_controller import AnimationController
from .config import PetConfig
from .placement import clamp_position, screen_for_point
from .windows_work_area import work_area


class PetImageError(RuntimeError):
    """An actionable error when the character asset cannot be loaded."""


def load_character(config: PetConfig) -> QPixmap:
    path = config.image_path.expanduser().resolve()
    if not path.is_file():
        raise PetImageError(
            f"找不到桌宠图片：\n{path}\n\n"
            "请恢复该文件，或使用 --image 指定透明 PNG 图片。"
        )
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise PetImageError(f"无法读取桌宠图片：\n{path}\n\n{reader.errorString()}")
    return QPixmap.fromImage(image)


class PetWindow(QWidget):
    # Future sprite/part renderers can consume animation.state and animation.pose.
    frame_tick = Signal(float)
    debug_requested = Signal()
    chat_requested = Signal()
    closed = Signal()

    def __init__(self, config: PetConfig) -> None:
        super().__init__()
        self.config = config
        self.animation = AnimationController(config.animation)
        self._source = load_character(config)
        self._sprite = QPixmap()
        self._hit_image = QImage()
        self._sprite_rect = QRect()
        self._press_global: QPoint | None = None
        self._press_origin = QPoint()
        self._dragged = False
        self._menu: QMenu | None = None
        self._next_click_happy = True
        self._drawing_area = QRectF()
        self._scale_percent = 100
        self._scene_scale = 1.0
        self.click_handler: Callable[[str], None] | None = None

        self.setWindowTitle("AURA · 鲸鱼娘桌宠")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

        self.bubble = SpeechBubble(self)
        screen = QGuiApplication.primaryScreen()
        self._fit_to_screen(screen)
        available = work_area(screen)
        desired = QPoint(
            available.x() + available.width() - self.width() - 16,
            available.y() + available.height() - self.height() - 16,
        )
        self.move(clamp_position(desired, self.size(), available))

        for display in QGuiApplication.screens():
            self._watch_screen(display)
        app = QApplication.instance()
        app.screenAdded.connect(self._screen_added)
        app.screenRemoved.connect(self._work_area_changed)

        self._clock = QElapsedTimer()
        self._clock.start()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.refresh_timer.setInterval(max(1, round(1000 / config.refresh_fps)))
        self.refresh_timer.timeout.connect(self._on_frame)
        self.refresh_timer.start()

    @property
    def sprite_rect(self) -> QRect:
        return QRect(self._sprite_rect)

    @property
    def character_transform(self) -> QTransform:
        """The same bounded transform is used for painting and inverse hit tests."""
        base = QRectF(self._sprite_rect)
        center = base.center()
        pose = self.animation.pose
        area = self._drawing_area
        scale = min(pose.scale, area.width() / base.width(), area.height() / base.height())
        half_width, half_height = base.width() * scale / 2, base.height() * scale / 2
        x = max(area.left() + half_width, min(center.x(), area.right() - half_width))
        y = max(area.top() + half_height, min(center.y() + pose.offset_y * self._scene_scale, area.bottom() - half_height))
        transform = QTransform()
        transform.translate(x, y)
        transform.scale(scale, scale)
        transform.translate(-center.x(), -center.y())
        return transform

    @property
    def animated_sprite_rect(self) -> QRectF:
        return self.character_transform.mapRect(QRectF(self._sprite_rect))

    def _fit_to_screen(self, screen) -> None:
        available = work_area(screen)
        requested = self._scale_percent / 100
        size = QSize(
            min(round(self.config.window_width * requested), available.width()),
            min(round(self.config.window_height * requested), available.height()),
        )
        if size != self.size() or self._sprite.isNull():
            self.setFixedSize(size)
            self._layout_character()

    def _layout_character(self) -> None:
        # Reserve the worst jump and breathing extents once, not per frame.
        factor = min(self.width() / self.config.window_width, self.height() / self.config.window_height)
        self._scene_scale = factor
        padding = self.config.padding * factor
        headroom = min((self.config.bubble_height + self.config.padding) * factor, self.height() / 4)
        self._drawing_area = QRectF(
            padding, headroom,
            max(1, self.width() - 2 * padding),
            max(1, self.height() - headroom - padding),
        )
        center = QRectF(self.rect()).center()
        tuning = self.config.animation
        max_width = (self.width() - 2 * padding) / tuning.max_scale
        max_height = 2 * min(
            center.y() - headroom - tuning.max_upward_offset * factor,
            self.height() - padding - center.y() - tuning.max_downward_offset * factor,
        ) / tuning.max_scale
        # Excessive custom jumps should reach a limit, not shrink the pet away.
        static_height = self._drawing_area.height() / tuning.max_scale
        max_height = min(static_height, max(min(64.0 * factor, static_height), max_height))
        bounds = QSize(
            max(1, math.floor(min(self.config.character_size * factor, max_width))),
            max(1, math.floor(min(self.config.character_size * factor, max_height))),
        )
        self._sprite = self._source.scaled(
            bounds, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._hit_image = self._sprite.toImage()
        self._sprite_rect = QRect(QPoint(), self._sprite.size())
        self._sprite_rect.moveCenter(self.rect().center())
        bubble_width = round(min(270 * factor, self.width() - 2 * padding))
        self.bubble.set_scale(factor)
        self.bubble.setGeometry(
            (self.width() - bubble_width) // 2, round(padding),
            bubble_width, max(1, round(headroom - padding)),
        )

    def set_scale_percent(self, percent: int) -> None:
        if not 50 <= percent <= 200:
            raise ValueError("大小必须为 50%～200%。")
        center = self.frameGeometry().center()
        old_position = self.pos()
        self._scale_percent = percent
        screen = screen_for_point(center)
        self._fit_to_screen(screen)
        desired = center - self.rect().center()
        self.move(clamp_position(desired, self.size(), work_area(screen)))
        if self._press_global is not None:
            self._press_origin += self.pos() - old_position
        self.update()

    def set_character_source(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        if pixmap.cacheKey() != self._source.cacheKey():
            self._source = pixmap
            self._layout_character()
            self.update()

    def character_contains(self, local: QPoint | QPointF) -> bool:
        inverse, invertible = self.character_transform.inverted()
        if not invertible:
            return False
        base = inverse.map(QPointF(local))
        x = math.floor(base.x() - self._sprite_rect.x())
        y = math.floor(base.y() - self._sprite_rect.y())
        if not (0 <= x < self._hit_image.width() and 0 <= y < self._hit_image.height()):
            return False
        return self._hit_image.pixelColor(x, y).alpha() > 0

    def _watch_screen(self, screen) -> None:
        screen.availableGeometryChanged.connect(self._work_area_changed)

    def _screen_added(self, screen) -> None:
        self._watch_screen(screen)
        self._work_area_changed()

    def _work_area_changed(self, *_args) -> None:
        screen = screen_for_point(self.frameGeometry().center())
        self._fit_to_screen(screen)
        self.move(clamp_position(self.pos(), self.size(), work_area(screen)))

    def _on_frame(self) -> None:
        seconds = self._clock.restart() / 1000.0
        self.animation.advance(seconds)
        self.frame_tick.emit(seconds)
        self.update()

    def paintEvent(self, event) -> None:
        # Unpainted pixels stay alpha=0: Windows lets clicks reach windows below.
        painter = QPainter(self)
        # Erase the previous pose to actual alpha=0, including vacated hit regions.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setTransform(self.character_transform)
        painter.drawPixmap(self._sprite_rect.topLeft(), self._sprite)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.character_contains(event.position().toPoint()):
            self._press_global = event.globalPosition().toPoint()
            self._press_origin = self.pos()
            self._dragged = False
            self.animation.start_dragging()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press_global is None or not event.buttons() & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        global_pos = event.globalPosition().toPoint()
        delta = global_pos - self._press_global
        if delta.manhattanLength() >= QApplication.startDragDistance():
            self._dragged = True
        if self._dragged:
            screen = screen_for_point(global_pos)
            self._fit_to_screen(screen)
            self.move(clamp_position(
                self._press_origin + delta, self.size(), work_area(screen)
            ))
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press_global is not None:
            delta = event.globalPosition().toPoint() - self._press_global
            clicked = (
                not self._dragged
                and delta.manhattanLength() < QApplication.startDragDistance()
                and self.character_contains(event.position().toPoint())
            )
            self._press_global = None
            self._dragged = False
            self.animation.finish_dragging()
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            if clicked:
                expression = "happy" if self._next_click_happy else "surprised"
                if self.click_handler is not None:
                    self.click_handler(expression)
                else:
                    self.bubble.say(self.config.bubble_text, self.config.bubble_duration_ms)
                    if self._next_click_happy:
                        self.animation.play_happy()
                    else:
                        self.animation.play_surprised()
                self._next_click_happy = not self._next_click_happy
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        if not self.character_contains(event.pos()):
            event.ignore()
            return
        self._press_global = None
        self._dragged = False
        self.animation.finish_dragging()
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if self._menu is not None:
            self._menu.close()
            self._menu.deleteLater()
        self._menu = QMenu(self)
        self._menu.addAction("打开调试面板", self.debug_requested.emit)
        self._menu.addAction("打开聊天窗口", self.chat_requested.emit)
        self._menu.addSeparator()
        self._menu.addAction("退出桌宠", self.close)
        # Nonmodal popup keeps frame/bubble timers running while the menu is open.
        self._menu.popup(event.globalPos())
        event.accept()

    def closeEvent(self, event) -> None:
        self.refresh_timer.stop()
        self.bubble.hide_timer.stop()
        if self._menu is not None:
            self._menu.close()
        self.bubble.hide()
        self.closed.emit()
        super().closeEvent(event)

