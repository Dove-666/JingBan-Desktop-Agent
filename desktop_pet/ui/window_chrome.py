"""Small caption controls and native movement for the frameless chat window.

Controls overlay the existing header instead of adding another title-bar row.
The host keeps its own close/hide policy and delegates nativeEvent here.
"""

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QComboBox, QHBoxLayout, QLineEdit,
    QPlainTextEdit, QTextEdit, QWidget,
)


class _CaptionButton(QAbstractButton):
    """DPI-independent caption glyph; no font or image assets are required."""

    def __init__(self, kind, name, accessible_name, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.restoring = False
        self._keyboard_focus = False
        self.setObjectName(name)
        self.setAccessibleName(accessible_name)
        self.setToolTip(accessible_name)
        self.setFixedSize(44, 36)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        self._keyboard_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        self._keyboard_focus = False
        super().focusOutEvent(event)
        self.update()

    def mousePressEvent(self, event):
        # Clicking an already keyboard-focused button does not send focusIn
        # again. Clear its keyboard-only indication here as well.
        self._keyboard_focus = False
        super().mousePressEvent(event)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        hovered = self.underMouse()
        keyboard_focus = self.hasFocus() and self._keyboard_focus
        background = "#ffffff"
        foreground = "#526e88"
        if self.kind == "close" and (hovered or self.isDown() or keyboard_focus):
            background = "#c50f1f" if self.isDown() or keyboard_focus else "#e81123"
            foreground = "#ffffff"
        elif self.isDown():
            background = "#dfedfb"
        elif keyboard_focus:
            background = "#e7f1fb"
        elif hovered:
            background = "#edf5fd"
        painter.fillRect(self.rect(), QColor(background))
        pen = QPen(QColor(foreground), 1.1)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        painter.setPen(pen)
        x, y = self.width() / 2, self.height() / 2
        if self.kind == "minimize":
            painter.drawLine(QPointF(x - 5, y + 0.5), QPointF(x + 5, y + 0.5))
        elif self.kind == "maximize" and self.restoring:
            # Two offset windows, with the back rectangle's covered edges omitted.
            painter.drawLine(QPointF(x - 2.5, y - 5.5), QPointF(x + 5.5, y - 5.5))
            painter.drawLine(QPointF(x + 5.5, y - 5.5), QPointF(x + 5.5, y + 2.5))
            painter.drawLine(QPointF(x + 5.5, y + 2.5), QPointF(x + 3.5, y + 2.5))
            painter.drawRect(QRectF(x - 5.5, y - 3.5, 9, 9))
        elif self.kind == "maximize":
            painter.drawRect(QRectF(x - 5, y - 5, 10, 10))
        else:
            painter.drawLine(QPointF(x - 5, y - 5), QPointF(x + 5, y + 5))
            painter.drawLine(QPointF(x - 5, y + 5), QPointF(x + 5, y - 5))


class _MinMaxInfo(ctypes.Structure):
    _fields_ = [
        ("ptReserved", wintypes.POINT),
        ("ptMaxSize", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _WindowOutline(QWidget):
    """A thin rim above native/WebEngine children without intercepting input."""

    def __init__(self, parent, radius):
        super().__init__(parent)
        self.radius = radius
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#d5e3f0"), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5),
                                self.radius - .5, self.radius - .5)


class WindowChrome(QObject):
    """Overlay caption buttons, explicitly opted-in drag regions and resizing."""

    CONTROL_WIDTH = 132
    CONTROL_HEIGHT = 36
    RESIZE_BORDER = 4
    CORNER_RADIUS = 24
    FRAME_MARGIN = 8

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._drag_regions = set()
        self._resize_cursor_set = False
        self._user32 = None
        self._shape_key = None
        self.surface = getattr(window, "pages", None)
        self.outline = _WindowOutline(window, self.CORNER_RADIUS)
        if self.surface is not None:
            self.surface.installEventFilter(self)
        self.controls = QWidget(window)
        self.controls.setObjectName("windowControls")
        self.controls.setFixedSize(self.CONTROL_WIDTH, self.CONTROL_HEIGHT)
        self.controls.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.controls.setStyleSheet("QWidget#windowControls { background: #ffffff; }")
        self.controls.setCursor(Qt.CursorShape.ArrowCursor)
        layout = QHBoxLayout(self.controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.minimize_button = _CaptionButton("minimize", "windowMinimize", "最小化", self.controls)
        self.maximize_button = _CaptionButton("maximize", "windowMaximize", "最大化", self.controls)
        self.close_button = _CaptionButton("close", "windowClose", "关闭窗口", self.controls)
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            layout.addWidget(button)
        self.minimize_button.clicked.connect(window.showMinimized)
        self.maximize_button.clicked.connect(self.toggle_maximized)
        self.close_button.clicked.connect(window.close)
        window.setMouseTracking(True)
        window.installEventFilter(self)
        self._place_controls()
        self._refresh_maximize_button()
        self._refresh_window_shape()
        self.controls.show()
        self.raise_controls()

    @staticmethod
    def _rounded_region(rect, radius):
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), radius, radius)
        return QRegion(path.toFillPolygon().toPolygon())

    def _refresh_window_shape(self):
        rounded = not (self.window.isMaximized() or self.window.isFullScreen())
        surface_rect = self.surface.rect() if self.surface is not None else None
        surface_position = self.surface.pos() if self.surface is not None else None
        key = (self.window.size(), surface_rect, surface_position, rounded)
        if key == self._shape_key:
            return
        self._shape_key = key
        self.outline.setGeometry(self.window.rect())
        self.outline.setVisible(rounded)
        # Only the inner content is clipped. The top-level silhouette is painted
        # into an alpha surface, so its edge retains subpixel antialiasing.
        if not self.window.mask().isEmpty():
            self.window.clearMask()
        if self.surface is not None:
            if rounded:
                inset = max(0, min(self.surface.x(), self.surface.y()))
                self.surface.setMask(self._rounded_region(surface_rect, max(0, self.CORNER_RADIUS - inset)))
            else:
                self.surface.clearMask()
        self.window.update()
        self.outline.update()
        self.outline.raise_()

    def _place_controls(self):
        if self.window.isMaximized() or self.window.isFullScreen():
            right = top = 0
        else:
            layout = self.window.layout()
            margins = layout.contentsMargins() if layout is not None else self.window.contentsMargins()
            right, top = margins.right(), margins.top()
        self.controls.move(max(0, self.window.width() - right - self.CONTROL_WIDTH), top)
        self.raise_controls()

    def raise_controls(self):
        self.controls.raise_()
        self.outline.raise_()

    def _refresh_maximize_button(self):
        restoring = self.window.isMaximized()
        self.maximize_button.restoring = restoring
        label = "还原窗口" if restoring else "最大化"
        self.maximize_button.setAccessibleName(label)
        self.maximize_button.setToolTip(label)
        self.maximize_button.update()

    def start_move(self) -> bool:
        """Use the platform drag loop so snapping and monitor changes stay native."""
        try:
            handle = self.window.windowHandle()
            return bool(handle is not None and handle.startSystemMove())
        except RuntimeError:
            return False

    def toggle_maximized(self):
        if self.window.isMaximized():
            self.window.showNormal()
        else:
            self.window.showMaximized()

    def add_drag_region(self, widget):
        """Opt a header/label into dragging; child inputs remain interactive."""
        if not isinstance(widget, QWidget) or widget in self._drag_regions:
            return
        self._drag_regions.add(widget)
        widget.installEventFilter(self)
        widget.setMouseTracking(True)
        widget.setCursor(Qt.CursorShape.ArrowCursor)
        widget.destroyed.connect(lambda: self._drag_regions.discard(widget))

    def _resize_edges(self, position):
        if self.window.isMaximized() or self.window.isFullScreen():
            return Qt.Edge(0)
        border = self.RESIZE_BORDER
        edges = Qt.Edge(0)
        if position.x() < border:
            edges |= Qt.Edge.LeftEdge
        elif position.x() >= self.window.width() - border:
            edges |= Qt.Edge.RightEdge
        if position.y() < border:
            edges |= Qt.Edge.TopEdge
        elif position.y() >= self.window.height() - border:
            edges |= Qt.Edge.BottomEdge
        # Rounded tips are outside the window. The visible curved frame still
        # needs a diagonal resize target; straight-edge targets remain 4px.
        radius = self.CORNER_RADIUS
        if position.x() < radius and position.y() < radius:
            edges = Qt.Edge.LeftEdge | Qt.Edge.TopEdge
        elif position.x() >= self.window.width() - radius and position.y() < radius:
            edges = Qt.Edge.RightEdge | Qt.Edge.TopEdge
        elif position.x() < radius and position.y() >= self.window.height() - radius:
            edges = Qt.Edge.LeftEdge | Qt.Edge.BottomEdge
        elif position.x() >= self.window.width() - radius and position.y() >= self.window.height() - radius:
            edges = Qt.Edge.RightEdge | Qt.Edge.BottomEdge
        return edges

    def _update_resize_cursor(self, edges):
        horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if horizontal and vertical:
            forward = bool(edges & Qt.Edge.LeftEdge) == bool(edges & Qt.Edge.TopEdge)
            cursor = Qt.CursorShape.SizeFDiagCursor if forward else Qt.CursorShape.SizeBDiagCursor
        elif horizontal:
            cursor = Qt.CursorShape.SizeHorCursor
        elif vertical:
            cursor = Qt.CursorShape.SizeVerCursor
        else:
            if self._resize_cursor_set:
                self.window.unsetCursor()
                self._resize_cursor_set = False
            return
        self.window.setCursor(cursor)
        self._resize_cursor_set = True

    @staticmethod
    def _interactive_child_at(region, event):
        child = region.childAt(event.position().toPoint())
        interactive = (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit, QTextEdit)
        while child is not None and child is not region:
            if isinstance(child, interactive):
                return True
            child = child.parentWidget()
        return False

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self.window:
            if kind == QEvent.Type.WinIdChange:
                self._shape_key = None
            if kind in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.WindowStateChange,
                        QEvent.Type.LayoutRequest, QEvent.Type.WindowActivate, QEvent.Type.WinIdChange):
                self._place_controls()
                self._refresh_maximize_button()
                self._refresh_window_shape()
            elif kind == QEvent.Type.MouseMove:
                self._update_resize_cursor(self._resize_edges(event.position()))
            elif kind == QEvent.Type.Leave:
                self._update_resize_cursor(Qt.Edge(0))
            elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                edges = self._resize_edges(event.position())
                handle = self.window.windowHandle()
                if edges and handle is not None and handle.startSystemResize(edges):
                    event.accept()
                    return True
        if watched is self.surface and kind in (QEvent.Type.Resize, QEvent.Type.Move):
            self._refresh_window_shape()
        if watched in self._drag_regions:
            if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                if event.button() == Qt.MouseButton.LeftButton and not self._interactive_child_at(watched, event):
                    if kind == QEvent.Type.MouseButtonDblClick:
                        self.toggle_maximized()
                        event.accept()
                        return True
                    if self.start_move():
                        event.accept()
                        return True
        return super().eventFilter(watched, event)

    def _windows_api(self):
        if self._user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            self._user32 = user32
        return self._user32

    def native_event(self, event_type, message):
        """Return a Qt nativeEvent result for Windows work-area maximization.

        A normal frameless window can otherwise maximize over the taskbar.
        Keep manual resize bounds untouched so spanning monitors remains possible.
        """
        if sys.platform != "win32":
            return None
        try:
            if bytes(event_type) not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                return None
            address = int(message)
            if not address:
                return None
            native = ctypes.cast(address, ctypes.POINTER(wintypes.MSG)).contents
            if native.message != 0x0024 or not native.lParam:  # WM_GETMINMAXINFO
                return None
            user32 = self._windows_api()
            monitor = user32.MonitorFromWindow(native.hWnd, 2)  # MONITOR_DEFAULTTONEAREST
            if not monitor:
                return None
            info = _MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return None
            limits = ctypes.cast(native.lParam, ctypes.POINTER(_MinMaxInfo)).contents
            limits.ptMaxPosition.x = info.rcWork.left - info.rcMonitor.left
            limits.ptMaxPosition.y = info.rcWork.top - info.rcMonitor.top
            limits.ptMaxSize.x = info.rcWork.right - info.rcWork.left
            limits.ptMaxSize.y = info.rcWork.bottom - info.rcWork.top
            ratio = self.window.devicePixelRatioF()
            limits.ptMinTrackSize.x = max(limits.ptMinTrackSize.x, round(self.window.minimumWidth() * ratio))
            limits.ptMinTrackSize.y = max(limits.ptMinTrackSize.y, round(self.window.minimumHeight() * ratio))
            return True, 0
        except (AttributeError, OSError, TypeError, ValueError, RuntimeError):
            # Non-Windows test backends and a disappearing HWND may safely defer
            # to Qt instead of making window activation or shutdown fail.
            return None
