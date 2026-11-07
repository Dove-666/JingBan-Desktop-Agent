"""Small native settings controls with keyboard and accessibility support."""

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QCheckBox, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)
from .theme import color as theme_color, current_theme


class SettingsToggle(QCheckBox):
    """A native checkbox presented as a label and a trailing switch.

    Keeping QCheckBox's input and accessible interface preserves Space, focus,
    checked-state announcements, and the ordinary ``toggled`` signal.
    """

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(40)

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 60, 40)

    def minimumSizeHint(self):
        return QSize(80, 40)

    def hitButton(self, point):
        return self.rect().contains(point)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rtl = self.layoutDirection() == Qt.LayoutDirection.RightToLeft
        switch_width, switch_height = 34, 20
        switch_x = 2 if rtl else self.width() - switch_width - 2
        track = QRectF(switch_x, (self.height() - switch_height) / 2,
                       switch_width, switch_height)
        enabled = self.isEnabled()
        checked = self.isChecked()
        dark = current_theme() == "dark"
        color = "#4d82b8" if checked else "#c8d0da"
        if dark:
            color = theme_color("accent") if checked else theme_color("toggle_off")
        if not enabled:
            color = ("#4e6680" if checked else "#263244") if dark else ("#aebfd2" if checked else "#e1e6ec")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(track, 10, 10)
        thumb_on_right = checked != rtl
        thumb_x = track.right() - 17 if thumb_on_right else track.left() + 3
        thumb = ("#142131" if checked else "#c2cede") if dark else ("#ffffff" if enabled else "#f5f7fa")
        painter.setBrush(QColor(thumb))
        painter.drawEllipse(QRectF(thumb_x, track.top() + 3, 14, 14))

        text_rect = QRect(48 if rtl else 2, 0,
                          max(0, self.width() - 50), self.height())
        flags = Qt.AlignmentFlag.AlignVCenter | (
            Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft)
        palette = QPalette(self.palette())
        palette.setColor(QPalette.ColorRole.WindowText,
                         QColor(theme_color("text" if enabled else "disabled") if dark else ("#344054" if enabled else "#8a96a5")))
        text = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, text_rect.width())
        self.style().drawItemText(painter, text_rect, flags, palette,
                                  enabled, text, QPalette.ColorRole.WindowText)
        if self.hasFocus():
            pen = QPen(QColor(theme_color("focus") if dark else "#4d82b8"), 1, Qt.PenStyle.DotLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 5, 5)


class CollapsibleSection(QWidget):
    """A disclosure whose content stays mounted while the section is closed."""

    expandedChanged = Signal(bool)

    def __init__(self, title, parent=None, *, content=None, expanded=False):
        super().__init__(parent)
        self.setObjectName("settingsDisclosure")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.button = QToolButton(self)
        self.button.setObjectName("settingsDisclosureButton")
        self.button.setText(title)
        self.button.setAccessibleName(title)
        self.button.setCheckable(True)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.button.setMinimumHeight(36)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.content = content if content is not None else QWidget(self)
        self.content.setParent(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.button)
        layout.addWidget(self.content)
        self.button.toggled.connect(self._apply_expanded)
        self.setExpanded(expanded)

    def isExpanded(self):
        return self.button.isChecked()

    def setExpanded(self, expanded):
        expanded = bool(expanded)
        if self.button.isChecked() == expanded:
            self._apply_expanded(expanded, emit=False)
        else:
            self.button.setChecked(expanded)

    def _apply_expanded(self, expanded, *, emit=True):
        closed_arrow = (Qt.ArrowType.LeftArrow
                        if self.layoutDirection() == Qt.LayoutDirection.RightToLeft
                        else Qt.ArrowType.RightArrow)
        self.button.setArrowType(Qt.ArrowType.DownArrow if expanded else closed_arrow)
        self.button.setAccessibleDescription("已展开，再次按下收起" if expanded else "已收起，按下展开")
        self.content.setVisible(expanded)
        if emit:
            self.expandedChanged.emit(expanded)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LayoutDirectionChange:
            self._apply_expanded(self.isExpanded(), emit=False)
        super().changeEvent(event)
