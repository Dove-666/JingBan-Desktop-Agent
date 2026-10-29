"""A nonblocking speech bubble that follows its parent window."""

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget


class SpeechBubble(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._text = ""
        self._scale = 1.0
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)
        self.hide()

    def say(self, text: str, duration_ms: int) -> None:
        self._text = text
        self.show()
        self.update()
        self.hide_timer.start(duration_ms)

    def set_scale(self, factor: float) -> None:
        self._scale = factor
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        scale = self._scale
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#91bddb"), 1.2 * scale))
        painter.setBrush(QColor("#f5fbff"))
        body = QRectF(scale, scale, self.width() - 2 * scale, self.height() - 10 * scale)
        painter.drawRoundedRect(body, 14 * scale, 14 * scale)
        center = self.width() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(
            QPolygonF(
                [QPointF(center - 7 * scale, body.bottom() - scale),
                 QPointF(center + 7 * scale, body.bottom() - scale),
                 QPointF(center, self.height() - 1)]
            )
        )
        painter.setPen(QColor("#234660"))
        painter.setFont(QFont("Microsoft YaHei UI", max(6, round(10 * scale))))
        painter.drawText(
            body.adjusted(10 * scale, 3 * scale, -10 * scale, -3 * scale),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            self._text,
        )

