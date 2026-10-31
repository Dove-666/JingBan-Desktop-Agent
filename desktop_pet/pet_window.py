"""Cached sprite rendering and pointer interaction driven by one frame timer."""

import math
from typing import Callable

from PySide6.QtCore import QElapsedTimer, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QCursor, QGuiApplication, QImage, QImageReader, QMouseEvent, QPainter, QPixmap, QTransform,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from .bubble import SpeechBubble
from .animation_controller import AnimationController, AnimationState
from .config import PetConfig
from .eye_tracking import EyeTracker, cursor_in_source
from .part_interaction import PartInteraction
from .placement import clamp_visible_position, screen_for_point, screen_for_rect
from .windows_work_area import work_area


class PetImageError(RuntimeError):
    """An actionable error when the character asset cannot be loaded."""


def _alpha_bounds(image: QImage) -> QRectF:
    """Measure cached display pixels once at layout time, without extra packages."""
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    raw = alpha.constBits()
    width, height, stride = alpha.width(), alpha.height(), alpha.bytesPerLine()
    left, top, right, bottom = width, height, 0, 0
    for y in range(height):
        row = bytes(raw[y * stride:y * stride + width])
        if row.strip(b"\0"):
            left = min(left, width - len(row.lstrip(b"\0")))
            right = max(right, len(row.rstrip(b"\0")))
            top, bottom = min(top, y), y + 1
    return QRectF(left, top, right - left, bottom - top) if right > left else QRectF()


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
        self._eye_tracking = EyeTracker()
        self._eye_tracking.bind_source(self._source.toImage())
        self._part_interaction = PartInteraction()
        self._part_interaction.bind_source(self._source.toImage())
        self._part_interaction_enabled = True
        self._sequence_frames: tuple[QImage, ...] = ()
        self._sequence_sprites: tuple[tuple[QPixmap, QImage], ...] = ()
        self._sequence_alpha_rects: tuple[QRectF, ...] = ()
        self._sequence_index = 0
        self._sequence_anchor = (.5, .9375)
        self._sprite = QPixmap()
        self._hit_image = QImage()
        self._sprite_rect = QRect()
        self._character_placement_bounds = QRectF()
        self._visible_alpha_rect = QRectF()
        self._press_global: QPoint | None = None
        self._press_origin = QPoint()
        self._drag_grab_fraction = QPointF(.5, .5)
        self._dragged = False
        self._pressed_part: str | None = None
        self._edge_recovery = None
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
        self.move(clamp_visible_position(desired, self.character_placement_bounds, available))

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
        pose = self.animation.pose
        return self._pose_transform(pose.scale, pose.offset_y)

    def _pose_transform(self, scale: float, offset_y: float) -> QTransform:
        base = QRectF(self._sprite_rect)
        center = base.center()
        area = self._drawing_area
        scale = min(scale, area.width() / base.width(), area.height() / base.height())
        half_width, half_height = base.width() * scale / 2, base.height() * scale / 2
        x = max(area.left() + half_width, min(center.x(), area.right() - half_width))
        y = max(area.top() + half_height, min(center.y() + offset_y * self._scene_scale, area.bottom() - half_height))
        transform = QTransform()
        transform.translate(x, y)
        transform.scale(scale, scale)
        transform.translate(-center.x(), -center.y())
        return transform

    @property
    def animated_sprite_rect(self) -> QRectF:
        return self.character_transform.mapRect(QRectF(self._sprite_rect))

    @property
    def visible_character_rect(self) -> QRectF:
        """Current painted alpha extent in local logical coordinates, including pose."""
        alpha = self._visible_alpha_rect
        if self.part_interaction_active and self._part_interaction.animating:
            alpha = self._part_display_bounds(self._part_interaction.visible_bounds())
        visible = (alpha.translated(self._sprite_rect.topLeft())
                   if not alpha.isEmpty() else QRectF(self._sprite_rect))
        return self.character_transform.mapRect(visible)

    def speech_anchor_geometry(self) -> tuple[QRectF, QRectF, QPointF]:
        """Global visible body, protected face, and speech target without pixel scans."""
        visible = self.visible_character_rect
        tracker = self._eye_tracking
        if not self._sequence_frames and tracker.source_matches and tracker.rig is not None:
            rig = tracker.rig
            point = QPointF(self._sprite_rect.left() + rig.face_center.x() * self._sprite.width() / rig.canvas.width(),
                            self._sprite_rect.top() + rig.face_center.y() * self._sprite.height() / rig.canvas.height())
            anchor = self.character_transform.map(point)
        else:
            anchor = QPointF(visible.left() + visible.width() * .43,
                             visible.top() + visible.height() * .36)
        head = QRectF(anchor.x() - visible.width() * .23,
                      anchor.y() - visible.height() * .11,
                      visible.width() * .46, visible.height() * .29).intersected(visible)
        origin = QPointF(self.mapToGlobal(QPoint()))
        return visible.translated(origin), head.translated(origin), anchor + origin

    @property
    def character_placement_bounds(self) -> QRectF:
        """Stable visible envelope, independent of a clip's current frame/phase."""
        return QRectF(self._character_placement_bounds)

    def _cache_placement_bounds(self, alpha: QRectF) -> None:
        if self.part_interaction_active:
            alpha = alpha.united(self._part_display_bounds(self._part_interaction.swept_bounds()))
        if alpha.isEmpty():
            self._character_placement_bounds = QRectF(self.rect())
            return
        visible = alpha.translated(self._sprite_rect.topLeft())
        base, area = QRectF(self._sprite_rect), self._drawing_area
        tuning = self.config.animation
        high = min(tuning.max_scale, area.width() / base.width(), area.height() / base.height())
        low = min(1.0 - tuning.breathing_amplitude, high)
        scales = {low, high}
        offsets = (-tuning.max_upward_offset, tuning.max_downward_offset)
        # Include the clamp transition points as well as the extreme poses.
        # This also bounds unusually large, valid custom animation amplitudes.
        transitions = [2 * (base.center().x() - area.left()) / base.width(),
                       2 * (area.right() - base.center().x()) / base.width()]
        for offset in offsets:
            center_y = base.center().y() + offset * self._scene_scale
            transitions.extend((2 * (center_y - area.top()) / base.height(),
                                2 * (area.bottom() - center_y) / base.height()))
        scales.update(value for value in transitions if low <= value <= high)
        envelope = QRectF()
        for scale in scales:
            for offset in offsets:
                envelope = envelope.united(self._pose_transform(scale, offset).mapRect(visible))
        self._character_placement_bounds = envelope

    def _constrain_character(self, screen=None) -> None:
        bounds = self.visible_character_rect if self.animation.state == AnimationState.DRAGGING else self.character_placement_bounds
        screen = screen or screen_for_rect(bounds.translated(self.pos()))
        before = self.pos()
        self.move(clamp_visible_position(before, bounds, work_area(screen)))
        if self._press_global is not None:
            self._press_origin += self.pos() - before

    def _drag_grab_local(self) -> QPoint:
        return self.character_transform.map(QPointF(
            self._sprite_rect.x() + self._drag_grab_fraction.x() * self._sprite.width(),
            self._sprite_rect.y() + self._drag_grab_fraction.y() * self._sprite.height(),
        )).toPoint()

    def _fit_to_screen(self, screen) -> None:
        available = work_area(screen)
        requested = self._scale_percent / 100
        size = QSize(
            min(round(self.config.window_width * requested), available.width()),
            min(round(self.config.window_height * requested), available.height()),
        )
        if size != self.size() or self._sprite.isNull():
            grab = self._drag_grab_local() if self._press_global is not None else None
            self.setFixedSize(size)
            self._layout_character()
            if grab is not None:
                # A smaller/scaled monitor may change the cached sprite size.
                # Keep the grabbed source pixel under the same pointer offset.
                self._press_origin += grab - self._drag_grab_local()

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
        if self._sequence_frames:
            # Rebuild only when the display size changes or a clip is installed.
            # Every cached frame has the same canvas/placement; never crop each
            # frame to its own alpha bounds (that makes the character wobble).
            cached = []
            alpha_rects = []
            alpha = QRectF()
            for image in self._sequence_frames:
                scaled = image.scaled(bounds, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
                cached.append((QPixmap.fromImage(scaled), scaled))
                frame_alpha = _alpha_bounds(scaled)
                alpha_rects.append(frame_alpha)
                alpha = alpha.united(frame_alpha)
            self._sequence_sprites = tuple(cached)
            self._sequence_alpha_rects = tuple(alpha_rects)
            self._sprite, self._hit_image = self._sequence_sprites[self._sequence_index]
            self._visible_alpha_rect = self._sequence_alpha_rects[self._sequence_index]
        else:
            self._sprite = self._source.scaled(
                bounds, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._hit_image = self._sprite.toImage()
            alpha = _alpha_bounds(self._hit_image)
            self._visible_alpha_rect = alpha
        self._sprite_rect = QRect(QPoint(), self._sprite.size())
        self._sprite_rect.moveCenter(self.rect().center())
        if self._sequence_frames:
            # Align the manifest's anchor with the common canvas reference.
            # The pose transform still clamps the entire canvas to the window.
            self._sprite_rect.translate(
                round((.5 - self._sequence_anchor[0]) * self._sprite.width()),
                round((.9375 - self._sequence_anchor[1]) * self._sprite.height()),
            )
        self._cache_placement_bounds(alpha)
        self.bubble.set_scale(factor)
        self.bubble.reposition()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if hasattr(self, "bubble") and not self._sprite.isNull():
            self.bubble.reposition()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.bubble.reposition()

    def hideEvent(self, event) -> None:
        self.bubble.dismiss()
        super().hideEvent(event)

    def set_scale_percent(self, percent: int) -> None:
        if not 50 <= percent <= 200:
            raise ValueError("大小必须为 50%～200%。")
        footprint = self.character_placement_bounds.translated(self.pos())
        center = footprint.center()
        old_position = self.pos()
        self._scale_percent = percent
        self._edge_recovery = None
        screen = screen_for_rect(footprint)
        self._fit_to_screen(screen)
        desired = (center - self.character_placement_bounds.center()).toPoint()
        self.move(clamp_visible_position(desired, self.character_placement_bounds, work_area(screen)))
        if self._press_global is not None:
            self._press_origin += self.pos() - old_position
        self.update()

    def set_character_source(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        if pixmap.cacheKey() != self._source.cacheKey():
            self._source = pixmap
            source_image = pixmap.toImage()
            self._eye_tracking.bind_source(source_image)
            self._part_interaction.bind_source(source_image)
            self._layout_character()
            self._constrain_character()
            self.update()

    @property
    def eye_tracking_active(self) -> bool:
        """Whether the current sprite uses a matching optional eye renderer."""
        return self._eye_tracking.active and not self._sequence_frames

    @property
    def part_interaction_active(self) -> bool:
        return self._part_interaction_enabled and self._part_interaction.active and not self._sequence_frames

    def set_part_interaction_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._part_interaction_enabled == enabled:
            return
        self._part_interaction_enabled = enabled
        self._part_interaction.reset()
        self._pressed_part = None
        self._layout_character()
        self._constrain_character()
        self.update()

    def _part_display_bounds(self, source: QRectF) -> QRectF:
        canvas = self._part_interaction.rig.canvas
        sx, sy = self._sprite.width() / canvas.width(), self._sprite.height() / canvas.height()
        return QRectF(source.x() * sx, source.y() * sy,
                      source.width() * sx, source.height() * sy).toAlignedRect().toRectF()

    def _part_source_position(self, local: QPoint | QPointF) -> QPointF | None:
        if not self.part_interaction_active or self._sprite.isNull():
            return None
        inverse, invertible = self.character_transform.inverted()
        if not invertible:
            return None
        base = inverse.map(QPointF(local)) - QPointF(self._sprite_rect.topLeft())
        canvas = self._part_interaction.rig.canvas
        return QPointF(base.x() * canvas.width() / self._sprite.width(),
                       base.y() * canvas.height() / self._sprite.height())

    def _part_at(self, local: QPoint | QPointF) -> str | None:
        source = self._part_source_position(local)
        return self._part_interaction.hit_test(source) if source is not None else None

    def set_eye_tracking_enabled(self, enabled: bool) -> None:
        self._eye_tracking.set_enabled(enabled)
        self.update()

    def _advance_eye_tracking(self, seconds: float, cursor_global: QPoint | QPointF | None = None) -> None:
        # Freeze the current gaze during drag/recovery, preserving the image
        # beneath the pointer. Cached sequence frames always bypass the rig.
        if (not self.eye_tracking_active
                or self.animation.state in {AnimationState.DRAGGING, AnimationState.RETURNING}):
            return
        tracker = self._eye_tracking
        source_position = cursor_in_source(
            QPointF(QCursor.pos() if cursor_global is None else cursor_global),
            QPointF(self.mapToGlobal(QPoint())), QRectF(self._sprite_rect),
            self.character_transform, tracker.rig.canvas,
        )
        if source_position is not None:
            tracker.advance(seconds, source_position)

    def set_frame_sequence(self, frames: tuple[QImage, ...], anchor=(.5, .9375)) -> None:
        """Install already decoded images; called on the GUI thread once per clip."""
        if not frames or any(image.isNull() or image.size() != frames[0].size() for image in frames):
            raise ValueError("动画帧必须有效且共享同一画布。")
        self._sequence_frames = frames
        self._part_interaction.reset()
        self._pressed_part = None
        self._sequence_index = 0
        self._sequence_anchor = anchor
        self._layout_character()
        self._constrain_character()
        self.update()

    def set_frame_index(self, index: int) -> None:
        """A frame switch swaps cached paint/hit images without I/O or scaling."""
        if not 0 <= index < len(self._sequence_sprites):
            return
        self._sequence_index = index
        self._sprite, self._hit_image = self._sequence_sprites[index]
        self._visible_alpha_rect = self._sequence_alpha_rects[index]
        if self.bubble.isVisible():
            self.bubble.reposition()
        self.update()

    def clear_frame_sequence(self) -> None:
        if self._sequence_frames:
            self._sequence_frames = self._sequence_sprites = ()
            self._sequence_alpha_rects = ()
            self._sequence_index = 0
            self._layout_character()
            self._constrain_character()
            self.update()

    def character_contains(self, local: QPoint | QPointF) -> bool:
        source = self._part_source_position(local)
        if source is not None:
            return self._part_interaction.contains(source)
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
        screen.logicalDotsPerInchChanged.connect(self._work_area_changed)

    def _screen_added(self, screen) -> None:
        self._watch_screen(screen)
        self._work_area_changed()

    def _work_area_changed(self, *_args) -> None:
        self._edge_recovery = None
        screen = screen_for_rect(self.character_placement_bounds.translated(self.pos()))
        self._fit_to_screen(screen)
        self._constrain_character(screen)

    def _on_frame(self) -> None:
        seconds = self._clock.restart() / 1000.0
        self._advance_frame(seconds)

    def _advance_frame(self, seconds: float) -> None:
        """One deterministic GUI tick; also used by isolated geometry checks."""
        self.animation.advance(seconds)
        self._advance_edge_recovery(seconds)
        self.frame_tick.emit(seconds)
        self._advance_eye_tracking(seconds)
        self._part_interaction.advance(seconds, suspended=(
            not self.part_interaction_active or self.animation.state == AnimationState.DRAGGING))
        if self.part_interaction_active and self._part_interaction.animating and self._press_global is None:
            # A local tail swing may extend beyond the resting silhouette.
            bounds = self.visible_character_rect
            screen = screen_for_rect(bounds.translated(self.pos()))
            self.move(clamp_visible_position(self.pos(), bounds, work_area(screen)))
        if self.bubble.isVisible():
            self.bubble.reposition()
        self.update()

    def _begin_edge_recovery(self) -> None:
        """Reserve upcoming poses smoothly after the frozen frame touched an edge."""
        screen = screen_for_rect(self.visible_character_rect.translated(self.pos()))
        area = work_area(screen)
        start = QPoint(self.pos())
        target = clamp_visible_position(start, self.character_placement_bounds, area)
        self._edge_recovery = (start, target, 0.0, area) if target != start else None

    def _advance_edge_recovery(self, seconds: float) -> None:
        if self._edge_recovery is None or self.animation.state == AnimationState.DRAGGING:
            return
        start, target, elapsed, area = self._edge_recovery
        elapsed += seconds
        progress = min(1.0, elapsed / self.config.animation.return_duration)
        weight = progress * progress * (3.0 - 2.0 * progress)
        desired = QPoint(round(start.x() + (target.x() - start.x()) * weight),
                         round(start.y() + (target.y() - start.y()) * weight))
        # Returning pose extents change continuously; keep those exact pixels
        # inside the work area even before the union-safe target is reached.
        self.move(clamp_visible_position(desired, self.visible_character_rect, area))
        self._edge_recovery = (start, target, elapsed, area) if progress < 1.0 else None

    def paintEvent(self, event) -> None:
        # Unpainted pixels stay alpha=0: Windows lets clicks reach windows below.
        painter = QPainter(self)
        # Erase the previous pose to actual alpha=0, including vacated hit regions.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setTransform(self.character_transform)
        if self.part_interaction_active:
            rig = self._part_interaction.rig
            painter.translate(QPointF(self._sprite_rect.topLeft()))
            painter.scale(self._sprite.width() / rig.canvas.width(), self._sprite.height() / rig.canvas.height())
            self._part_interaction.paint(painter,
                eye_rig=self._eye_tracking.rig if self.eye_tracking_active else None,
                gaze=self._eye_tracking.gaze)
        elif self.eye_tracking_active:
            rig = self._eye_tracking.rig
            painter.translate(QPointF(self._sprite_rect.topLeft()))
            painter.scale(self._sprite.width() / rig.canvas.width(), self._sprite.height() / rig.canvas.height())
            rig.paint(painter, self._eye_tracking.gaze)
        else:
            painter.drawPixmap(self._sprite_rect.topLeft(), self._sprite)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.character_contains(event.position().toPoint()):
            self._press_global = event.globalPosition().toPoint()
            self._press_origin = self.pos()
            inverse, _ = self.character_transform.inverted()
            base = inverse.map(event.position())
            self._drag_grab_fraction = QPointF((base.x() - self._sprite_rect.x()) / self._sprite.width(),
                                               (base.y() - self._sprite_rect.y()) / self._sprite.height())
            self._dragged = False
            self._pressed_part = self._part_at(event.position())
            self._edge_recovery = None
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
            self.move(clamp_visible_position(
                self._press_origin + delta, self.visible_character_rect, work_area(screen)
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
            pressed_part, self._pressed_part = self._pressed_part, None
            self._dragged = False
            self.animation.finish_dragging()
            self._begin_edge_recovery()
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            if clicked and pressed_part is not None:
                if self._part_at(event.position()) == pressed_part:
                    self._part_interaction.trigger(pressed_part)
                    self.update()
            elif clicked:
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
        self._pressed_part = None
        self._dragged = False
        self.animation.finish_dragging()
        self._begin_edge_recovery()
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if self._menu is not None:
            self._menu.close()
            self._menu.deleteLater()
        self._menu = QMenu(self)
        self._menu.addAction("打开调试面板", self.debug_requested.emit)
        self._menu.addAction("打开聊天窗口", self.chat_requested.emit)
        if self._eye_tracking.has_rigs:
            # A session-only opt-out keeps this visual trial easy to disable.
            gaze_action = self._menu.addAction("眼睛跟随鼠标")
            gaze_action.setCheckable(True)
            gaze_action.setChecked(self._eye_tracking.enabled)
            gaze_action.toggled.connect(self.set_eye_tracking_enabled)
        if self._part_interaction.source_matches:
            parts_action = self._menu.addAction("耳尾点击互动")
            parts_action.setCheckable(True)
            parts_action.setChecked(self._part_interaction_enabled)
            parts_action.toggled.connect(self.set_part_interaction_enabled)
        self._menu.addSeparator()
        self._menu.addAction("退出桌宠", self.close)
        # Nonmodal popup keeps frame/bubble timers running while the menu is open.
        self._menu.popup(event.globalPos())
        event.accept()

    def closeEvent(self, event) -> None:
        self.refresh_timer.stop()
        self.bubble.dismiss()
        if self._menu is not None:
            self._menu.close()
        self.bubble.hide()
        self.closed.emit()
        super().closeEvent(event)

