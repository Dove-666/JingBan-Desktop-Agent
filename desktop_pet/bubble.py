"""A readable, input-transparent speech window owned by the pet."""

import math

from PySide6.QtCore import QPointF, QRect, QRectF, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPolygonF, QTextLayout, QTextOption
from PySide6.QtWidgets import QWidget

from .bubble_placement import place_bubble
from .placement import screen_for_point
from .windows_work_area import work_area


BASE_FONT_POINTS = 14.0
MAX_TEXT_WIDTH = 300
MAX_BUBBLE_HEIGHT = 230
LINE_SPACING = 1.18
TAIL_MARGIN = 12
TAIL_HALF_WIDTH = 5
CORNER_RADIUS = 11


class SpeechBubble(QWidget):
    conversation_requested = Signal(str, str)

    def __init__(self, parent: QWidget) -> None:
        # An owned tool window can grow beyond a small character canvas. Create
        # it once; showing a comment never recreates a native window.
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
        self.setObjectName("petSpeechBubble")
        self.setAccessibleName("鲸鱼娘说的话")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._text = ""
        self._conversation_link = None
        self._scale = 1.0
        self._character_scale = 1.0
        self._font_points = int(BASE_FONT_POINTS)
        self._hover_paused = False
        self._paused_hide_ms = 0
        self._paused_page_ms = 0
        self._previous_page_rect = QRectF()
        self._next_page_rect = QRectF()
        self._layout = QTextLayout()
        self._text_rect = QRectF()
        self._body_rect = QRectF()
        self._pages = [""]
        self._page_index = 0
        self._page_text = ""
        self._tail_side = None
        self._tail_target = QPointF()
        self._tail_polygon = QPolygonF()
        self._layout_key = None
        self._page_layout_key = None
        self._speech_sequence = -1
        self._placement = None
        self._tail_margin = float(TAIL_MARGIN)
        self._padding = 12.0
        self._footer_height = 0.0
        self._page_timer = QTimer(self)
        self._page_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._page_timer.setSingleShot(True)
        self._page_timer.timeout.connect(self._next_page)
        self.hide_timer = QTimer(self)
        self.hide_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.dismiss)
        self.set_font_points(int(BASE_FONT_POINTS))
        self.hide()

    def say(self, text: str, duration_ms: int, *, conversation_link=None) -> None:
        clickable = bool(conversation_link and conversation_link.get("conversation_id")
                         and conversation_link.get("message_id"))
        if clickable != bool(self._conversation_link):
            # Only linked proactive comments accept a click. Ordinary feedback
            # keeps the established native input-transparent behavior.
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not clickable)
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, not clickable)
            self.setCursor(Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor)
        self._conversation_link = dict(conversation_link) if clickable else None
        self.setToolTip("悬停暂停阅读；页脚可前后翻页；点击正文查看对话" if clickable else "")
        self._text = text
        self.setAccessibleDescription(text)
        self._speech_sequence += 1
        self._placement = None
        self._page_index = 0
        self._layout_key = None
        self._page_timer.stop()
        self._hover_paused = False
        self._paused_hide_ms = self._paused_page_ms = 0
        self.reposition()
        # A hidden owner must not leave an independent visible speech window.
        if not self.parentWidget().isVisible():
            return
        self.show()
        self.update()
        duration = max(duration_ms, self._reading_time() if len(self._pages) > 1 else duration_ms)
        self.hide_timer.start(duration)
        self._schedule_page()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._conversation_link:
            if self._previous_page_rect.contains(event.position()):
                self._previous_page()
                event.accept()
                return
            if self._next_page_rect.contains(event.position()):
                self._next_page()
                event.accept()
                return
            link = self._conversation_link
            self.conversation_requested.emit(link["conversation_id"], link["message_id"])
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:
        if self._conversation_link and not self._hover_paused:
            self._hover_paused = True
            self._paused_hide_ms = max(0, self.hide_timer.remainingTime())
            self._paused_page_ms = max(0, self._page_timer.remainingTime())
            self.hide_timer.stop()
            self._page_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._hover_paused:
            self._hover_paused = False
            if self.isVisible():
                self.hide_timer.start(max(1, self._paused_hide_ms))
                if self._page_index + 1 < len(self._pages):
                    self._page_timer.start(max(1, self._paused_page_ms or self._page_reading_time()))
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        if self._conversation_link and event.angleDelta().y():
            (self._previous_page if event.angleDelta().y() > 0 else self._next_page)()
            event.accept()
            return
        super().wheelEvent(event)

    def dismiss(self) -> None:
        self._page_timer.stop()
        self.hide_timer.stop()
        self._hover_paused = False
        self.hide()

    def hideEvent(self, event) -> None:
        self._page_timer.stop()
        self.hide_timer.stop()
        super().hideEvent(event)

    def set_scale(self, factor: float) -> None:
        """Track role scale without shrinking its independently readable speech."""
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("Speech scale must be finite and positive")
        self._character_scale = factor

    @property
    def font_points(self) -> int:
        return self._font_points

    def set_font_points(self, points: int) -> None:
        if type(points) is not int or not 12 <= points <= 22:
            raise ValueError("Speech font must be an integer from 12 to 22 pt")
        if self._font_points == points and self.font().pointSizeF() == points:
            return
        self._font_points = points
        self._scale = points / BASE_FONT_POINTS
        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(points)
        self.setFont(font)
        self._layout_key = None
        if self._text:
            self.reposition()
        self.update()

    def _make_layout(self, text: str, width: float) -> QTextLayout:
        layout = QTextLayout(text.replace("\n", "\u2028"), self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        height = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            line.setPosition(QPointF(0, height))
            height += line.height() * LINE_SPACING
        layout.endLayout()
        return layout

    def _paginate(self, layout: QTextLayout, width: float, height: float) -> list[str]:
        # Measure each page as it will actually be drawn. A slice ending in a
        # paragraph break adds an empty line when laid out on its own.
        encoded = self._text.encode("utf-16-le")
        pages = []
        start = previous_end = 0
        for index in range(layout.lineCount()):
            line = layout.lineAt(index)
            end = line.textStart() + line.textLength()
            if end <= previous_end:
                continue
            candidate = encoded[start * 2:end * 2].decode("utf-16-le")
            if (previous_end > start
                    and self._make_layout(candidate, width).boundingRect().height() > height):
                pages.append(encoded[start * 2:previous_end * 2].decode("utf-16-le"))
                start = previous_end
            previous_end = end
        pages.append(encoded[start * 2:].decode("utf-16-le"))
        return pages

    def _relayout(self, available: QRect) -> bool:
        pages_changed = False
        key = (self._text, self.font().pointSizeF(), self._scale, available.width(), available.height())
        if key != self._layout_key:
            self._layout_key = key
            metrics = QFontMetricsF(self.font())
            scale = self._scale
            edge = max(1.0, 4 * scale)
            maximum_width = max(1, math.floor(available.width() - 2 * edge))
            maximum_height = max(1, math.floor(min(available.height() - 2 * edge, MAX_BUBBLE_HEIGHT * scale)))
            natural_width = max((metrics.horizontalAdvance(line) for line in self._text.splitlines()), default=0)
            width = min(maximum_width, math.ceil(max(64 * scale, min(MAX_TEXT_WIDTH * scale, natural_width + 48 * scale))))
            self._tail_margin = min(TAIL_MARGIN * scale, width * .16, maximum_height * .16)
            self._padding = min(12 * scale, width * .12, maximum_height * .12)
            # An unusually short work area still needs room for a text line
            # and the page hint. Reduce decoration before clipping either one.
            vertical_budget = max(0.0, (maximum_height - 2 * metrics.height()) / 2 - .5)
            decoration = self._tail_margin + self._padding
            if decoration > vertical_budget:
                ratio = vertical_budget / decoration
                self._tail_margin *= ratio
                self._padding *= ratio
            inset = 2 * (self._padding + self._tail_margin)
            text_width = max(1.0, width - inset)
            text_height = max(1.0, maximum_height - inset)
            full = self._make_layout(self._text, text_width)
            previous_pages = self._pages
            reading_offset = sum(len(page) for page in previous_pages[:self._page_index])
            if full.boundingRect().height() > text_height:
                text_height = max(1.0, text_height - metrics.height())
                self._pages = self._paginate(full, text_width, text_height)
            else:
                self._pages = [self._text]
            pages_changed = self._pages != previous_pages
            # Reflow can change page lengths. Keep the current reading position
            # in view rather than reusing a page number that may skip sentences.
            self._page_index = 0
            page_end = 0
            for index, page in enumerate(self._pages):
                page_end += len(page)
                if page_end > reading_offset:
                    self._page_index = index
                    break
            self._page_layouts = [self._make_layout(page, text_width) for page in self._pages]
            self._footer_height = metrics.height() if len(self._pages) > 1 else 0.0
            # Keep the card still as pages advance, even when the final page is short.
            content_height = max([metrics.height()] + [item.boundingRect().height() for item in self._page_layouts])
            self.resize(width, min(maximum_height, math.ceil(content_height + inset + self._footer_height)))
        page_key = (key, self._page_index)
        if page_key == self._page_layout_key:
            return pages_changed
        self._page_layout_key = page_key
        self._page_text = self._pages[self._page_index]
        self._layout = self._page_layouts[self._page_index]
        return pages_changed

    def reposition(self) -> None:
        owner = self.parentWidget()
        if owner is None:
            return
        if hasattr(owner, "speech_anchor_geometry"):
            speaker, head, anchor = owner.speech_anchor_geometry()
        else:
            sprite = owner.sprite_rect
            speaker = QRectF(QRect(owner.mapToGlobal(sprite.topLeft()), sprite.size()))
            head, anchor = QRectF(), speaker.center()
        available = work_area(screen_for_point(anchor.toPoint()))
        pages_changed = self._relayout(available)
        edge = min(max(1.0, 4 * self._scale), available.width() * .04, available.height() * .04)
        safe = QRectF(available).adjusted(edge, edge, -edge, -edge)
        self._placement = place_bubble(safe, speaker, QSizeF(self.size()), anchor=anchor, head=head,
                                       gap=4 * self._scale, tail_margin=self._tail_margin,
                                       sequence=self._speech_sequence, previous=self._placement)
        position = self._placement.rect.topLeft().toPoint()
        if self.pos() != position:
            self.move(position)
        margin = self._tail_margin
        self._body_rect = QRectF(self.rect()).adjusted(margin, margin, -margin, -margin)
        self._text_rect = self._body_rect.adjusted(self._padding, self._padding, -self._padding,
                                                  -self._padding - self._footer_height)
        self._previous_page_rect = self._next_page_rect = QRectF()
        if len(self._pages) > 1 and self._conversation_link:
            footer_y = self._text_rect.bottom()
            button_width = min(self._text_rect.width() * .32, QFontMetricsF(self.font()).horizontalAdvance("‹ 上页") + 10)
            self._previous_page_rect = QRectF(self._text_rect.left(), footer_y, button_width, self._footer_height)
            self._next_page_rect = QRectF(self._text_rect.right() - button_width, footer_y,
                                          button_width, self._footer_height)
        self._aim_tail(self._placement.target)
        if pages_changed and self.isVisible() and (self.hide_timer.isActive() or self._hover_paused):
            # Moving to a smaller work area can create pages after say(). Keep
            # every remaining page reachable without restarting on plain moves.
            self._page_timer.stop()
            remaining = sum(max(4000, len(page) * 140) for page in self._pages[self._page_index:])
            if self._hover_paused:
                self._paused_hide_ms = max(self._paused_hide_ms, remaining)
            else:
                self.hide_timer.start(max(self.hide_timer.remainingTime(), remaining))
            self._schedule_page()
        self.update()

    def _aim_tail(self, anchor) -> None:
        """Aim from the final bubble position, including after edge clamping."""
        point = anchor.center() if isinstance(anchor, (QRect, QRectF)) else anchor
        target = QPointF(point) - QPointF(self.pos())
        self._tail_target = target
        self._tail_polygon = QPolygonF()
        self._tail_side = None
        # In a work area too small for separate placement, an outward arrow
        # would point away from an overlapping pet. Keep just the speech body.
        if QRectF(self.rect()).contains(target):
            return
        body = self._body_rect
        delta = target - body.center()
        half_width = TAIL_HALF_WIDTH * self._scale
        clearance = min((CORNER_RADIUS + TAIL_HALF_WIDTH + 2) * self._scale,
                        min(body.width(), body.height()) * .45)
        horizontal = abs(delta.x()) * body.height() > abs(delta.y()) * body.width()
        if horizontal:
            self._tail_side = "right" if delta.x() > 0 else "left"
            x = body.right() if delta.x() > 0 else body.left()
            y = max(body.top() + clearance, min(target.y(), body.bottom() - clearance))
            base = QPointF(x, y)
            half_base = QPointF(0, half_width)
        else:
            self._tail_side = "bottom" if delta.y() > 0 else "top"
            x = max(body.left() + clearance, min(target.x(), body.right() - clearance))
            y = body.bottom() if delta.y() > 0 else body.top()
            base = QPointF(x, y)
            half_base = QPointF(half_width, 0)
        direction = target - base
        shadow_room = min(3 * self._scale, self._tail_margin * .3)
        outer = QRectF(self.rect()).adjusted(shadow_room, shadow_room, -shadow_room, -shadow_room)
        limits = []
        if direction.x():
            edge = outer.right() if direction.x() > 0 else outer.left()
            limits.append((edge - base.x()) / direction.x())
        if direction.y():
            edge = outer.bottom() if direction.y() > 0 else outer.top()
            limits.append((edge - base.y()) / direction.y())
        distance = min([1.0] + [limit for limit in limits if limit > 0])
        tip = base + direction * distance
        self._tail_polygon = QPolygonF([base - half_base, base + half_base, tip])

    def _reading_time(self) -> int:
        return sum(max(4000, len(page) * 140) for page in self._pages)

    def _page_reading_time(self) -> int:
        return max(4000, len(self._page_text) * 140)

    def _schedule_page(self) -> None:
        if self._page_index + 1 < len(self._pages):
            if self._hover_paused:
                self._paused_page_ms = self._page_reading_time()
            else:
                self._page_timer.start(self._page_reading_time())

    def _change_page(self, delta: int) -> None:
        target = self._page_index + delta
        if not 0 <= target < len(self._pages):
            return
        self._page_timer.stop()
        self._page_index = target
        self.reposition()
        # Manual backwards navigation must leave enough time to read the
        # revisited page. Position-only reflows never restart these timers.
        remaining = sum(max(4000, len(page) * 140) for page in self._pages[self._page_index:])
        if self._hover_paused:
            self._paused_hide_ms = max(self._paused_hide_ms, remaining)
        elif self.isVisible():
            self.hide_timer.start(max(self.hide_timer.remainingTime(), remaining))
        self._schedule_page()

    def _next_page(self) -> None:
        self._change_page(1)

    def _previous_page(self) -> None:
        self._change_page(-1)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = self._body_rect
        outline = QPainterPath()
        outline.addRoundedRect(body, CORNER_RADIUS * self._scale, CORNER_RADIUS * self._scale)
        if not self._tail_polygon.isEmpty():
            tail = QPainterPath()
            tail.addPolygon(self._tail_polygon)
            tail.closeSubpath()
            outline = outline.united(tail)
        painter.save()
        painter.translate(0, self._scale)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for spread, alpha in ((5, 4), (3.5, 6), (2, 9)):
            pen = QPen(QColor(28, 49, 72, alpha), spread * self._scale)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPath(outline)
        painter.restore()
        painter.setPen(QPen(QColor("#d7e3ee"), max(.55, .85 * self._scale)))
        painter.setBrush(QColor("#fcfdff"))
        painter.drawPath(outline)
        painter.setPen(QColor("#243449"))
        self._layout.draw(painter, self._text_rect.topLeft())
        if len(self._pages) > 1:
            painter.setFont(self.font())
            painter.setPen(QColor("#65788d"))
            footer = QRectF(self._text_rect.left(), self._text_rect.bottom(),
                            self._text_rect.width(), self._footer_height)
            painter.drawText(footer, Qt.AlignmentFlag.AlignBottom | (Qt.AlignmentFlag.AlignHCenter
                             if self._conversation_link else Qt.AlignmentFlag.AlignRight),
                             f"{self._page_index + 1} / {len(self._pages)}")
            if self._conversation_link:
                for rectangle, label, enabled in ((self._previous_page_rect, "‹ 上页", self._page_index > 0),
                        (self._next_page_rect, "下页 ›", self._page_index + 1 < len(self._pages))):
                    painter.setPen(QColor("#415d78") if enabled else QColor("#a4afba"))
                    painter.drawText(rectangle, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter, label)

