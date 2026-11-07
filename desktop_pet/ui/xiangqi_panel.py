"""A native, scalable Xiangqi board and a quiet companion conversation."""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QTabBar, QTabWidget, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from ..config import PROJECT_ROOT
from .theme import apply_titlebar, color, current_theme, style_widget, theme_manager
from .window_chrome import WindowChrome
from .xiangqi_motion import XiangqiMotion


_GLYPHS = dict(zip("KABNRCPkabnrcp", "帅仕相马车炮兵将士象马车炮卒"))
_COLUMNS = "abcdefghi"
_DIFFICULTIES = (("casual", "轻松陪玩"), ("standard", "日常切磋"), ("serious", "认真对弈"))


def _square(row, column):
    return _COLUMNS[column] + str(9-row)


def _coordinates(square):
    if not isinstance(square, str) or len(square) != 2 or square[0] not in _COLUMNS or square[1] not in "0123456789":
        raise ValueError("Invalid square")
    return 9-int(square[1]), _COLUMNS.index(square[0])


def _avatar(size):
    source = QPixmap(str(PROJECT_ROOT / "assets/expressions/neutral.png"))
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    if not source.isNull():
        face = source.copy(round(source.width()*.29), round(source.height()*.34),
                           round(source.width()*.36), round(source.height()*.32))
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, size, size))
        painter.setClipPath(path)
        painter.fillRect(result.rect(), QColor(color("selected")))
        fitted = face.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap((size-fitted.width())//2, (size-fitted.height())//2, fitted)
        painter.end()
    return result


class XiangqiBoardWidget(QWidget):
    """All painted and clicked coordinates use the same reversible transform."""
    move_requested = Signal(str)
    animation_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("xiangqiBoard")
        self.setAccessibleName("象棋棋盘")
        self.setAccessibleDescription("选择己方棋子，再点击有标记的落点；方向键和回车也可操作。")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Coordinates and pieces already scale with the available rectangle.
        # A desktop-sized minimum defeats that transform after a window is
        # fitted to a short work area (for example 800 x 440 at high DPI).
        self.setMinimumSize(240, 280)
        self._state = {}
        self._selected = None
        self._keyboard_square = "e0"
        self._keyboard_focus = False
        self._pending_submission = False
        self._board_key = None
        self._motion = XiangqiMotion()
        self._check_positions = {}
        self._checked_position = None
        self._check_progress = 1.0
        self._check_animation = QVariantAnimation(self)
        self._check_animation.setStartValue(0.0)
        self._check_animation.setEndValue(1.0)
        self._check_animation.setDuration(1100)
        self._check_animation.valueChanged.connect(self._check_frame)
        self._move_progress = 0.0
        self._arrival_strength = 0.0
        self._move_animation = QVariantAnimation(self)
        self._move_animation.setStartValue(0.0)
        self._move_animation.setEndValue(1.0)
        self._move_animation.setDuration(500)
        self._move_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._move_animation.valueChanged.connect(self._move_frame)
        self._move_animation.finished.connect(self._move_finished)
        self._arrival_animation = QVariantAnimation(self)
        self._arrival_animation.setStartValue(0.0)
        self._arrival_animation.setEndValue(1.0)
        self._arrival_animation.setDuration(700)
        self._arrival_animation.valueChanged.connect(self._arrival_frame)
        manager = theme_manager()
        if manager is not None:
            manager.theme_changed.connect(lambda _name: self.update())

    def sizeHint(self):
        return QSize(570, 620)

    @property
    def flipped(self):
        return self._state.get("human_side", "red") == "black"

    def set_state(self, state):
        rows = tuple(tuple(row) for row in state.get("board", []))
        checked = state.get("checked_side")
        if "checked_side" not in state and "将军" in str(state.get("status", "")):
            checked = state.get("turn")
        self._check_positions[rows] = checked
        if len(self._check_positions) > 8:
            self._check_positions.pop(next(iter(self._check_positions)))
        previous_last = self._motion.last_move
        animate = (self.isVisible() and not self.window().isMinimized()
                   and bool(self._state.get("engine_ready"))
                   and (state.get("outcome") or {}).get("reason") != "resign")
        motion_changed = self._motion.set_state(state, animate=animate)
        key = (tuple(tuple(row) for row in state.get("board", [])), state.get("turn"),
               state.get("human_side"), bool(state.get("busy")), state.get("error"))
        if key != self._board_key:
            self._pending_submission = False
            if self._board_key is not None and key[:3] != self._board_key[:3]:
                self._selected = None
        self._board_key = key
        self._state = state
        if self._selected and not any(move.startswith(self._selected) for move in self._legal_moves()):
            self._selected = None
        if motion_changed:
            self._start_move_animation()
        elif previous_last != self._motion.last_move:
            self._arrival_animation.stop()
            self._arrival_strength = 0.0
        self._refresh_check(announce=animate)
        self._refresh_cursor()
        self.update()

    @property
    def animating(self):
        return self._motion.active is not None

    @property
    def displayed_checked_side(self):
        return self._checked_position[1] if self._checked_position else None

    def _refresh_check(self, *, announce):
        board = self._motion.board
        side = self._check_positions.get(board)
        position = (board, side) if side in ("red", "black") else None
        if position != self._checked_position:
            self._checked_position = position
            self._check_animation.stop()
            self._check_progress = 1.0
            if position and announce and self.isVisible():
                self._check_progress = 0.0
                self._check_animation.start()

    def _check_frame(self, value):
        self._check_progress = float(value)
        self.update()

    def _refresh_cursor(self):
        self.setCursor(Qt.CursorShape.PointingHandCursor if self._can_play() else Qt.CursorShape.ArrowCursor)

    def _start_move_animation(self):
        self._move_animation.stop()
        self._arrival_animation.stop()
        self._move_progress = self._arrival_strength = 0.0
        if self.animating:
            self._move_animation.start()
        self._refresh_check(announce=True)
        self.animation_changed.emit(self.animating)

    def _move_frame(self, value):
        self._move_progress = float(value)
        self.update()

    def _arrival_frame(self, value):
        self._arrival_strength = 1.0-float(value)
        self.update()

    def _move_finished(self):
        if self._motion.finish():
            self._start_move_animation()
        else:
            self._move_progress = 0.0
            self._arrival_strength = 1.0
            self._arrival_animation.start()
            self._refresh_check(announce=True)
            self._refresh_cursor()
            self.animation_changed.emit(False)
        self.update()

    def stop_motion(self):
        self._move_animation.stop()
        self._arrival_animation.stop()
        self._check_animation.stop()
        self._motion.snap()
        self._refresh_check(announce=False)
        self._check_progress = 1.0
        self._move_progress = self._arrival_strength = 0.0
        self._refresh_cursor()
        self.animation_changed.emit(False)
        self.update()

    def hideEvent(self, event):
        self.stop_motion()
        super().hideEvent(event)

    def _geometry(self):
        # Leave room beyond the outermost discs for both coordinate labels.
        cell = max(1.0, min((self.width()-66)/8, (self.height()-98)/9))
        grid = QRectF((self.width()-cell*8)/2, (self.height()-cell*9)/2, cell*8, cell*9)
        return grid, cell

    def file_labels(self, column):
        if self.flipped:
            return "一二三四五六七八九"[column], str(9-column)
        return str(column+1), "九八七六五四三二一"[column]

    def point_for_square(self, square):
        row, column = _coordinates(square)
        if self.flipped:
            row, column = 9-row, 8-column
        grid, cell = self._geometry()
        return QPointF(grid.left()+column*cell, grid.top()+row*cell)

    def square_at(self, point):
        grid, cell = self._geometry()
        column = round((point.x()-grid.left())/cell)
        row = round((point.y()-grid.top())/cell)
        if not 0 <= row <= 9 or not 0 <= column <= 8:
            return None
        center = QPointF(grid.left()+column*cell, grid.top()+row*cell)
        if math.hypot(point.x()-center.x(), point.y()-center.y()) > cell*.47:
            return None
        if self.flipped:
            row, column = 9-row, 8-column
        return _square(row, column)

    def _legal_moves(self):
        return self._state.get("legal_moves", [])

    def _can_play(self):
        return (self._state.get("engine_ready", False) and not self._state.get("busy")
                and not self._state.get("outcome") and not self._pending_submission
                and not self.animating
                and self._state.get("turn") == self._state.get("human_side", "red"))

    def _choose(self, square):
        if not square or not self._can_play():
            return
        self._keyboard_square = square
        if self._selected and self._selected + square in self._legal_moves():
            move = self._selected + square
            self._selected = None
            self._pending_submission = True
            self.update()
            self.move_requested.emit(move)
            return
        self._selected = square if any(move.startswith(square) for move in self._legal_moves()) else None
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._keyboard_focus = False
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._choose(self.square_at(event.position()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        event.accept()  # The first press already committed exactly one move.

    def keyPressEvent(self, event):
        self._keyboard_focus = True
        if event.key() == Qt.Key.Key_Escape:
            self._selected = None
            self.update()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._choose(self._keyboard_square)
            return
        deltas = {Qt.Key.Key_Left: (0, -1), Qt.Key.Key_Right: (0, 1),
                  Qt.Key.Key_Up: (-1, 0), Qt.Key.Key_Down: (1, 0)}
        if event.key() in deltas:
            row, column = _coordinates(self._keyboard_square)
            dy, dx = deltas[event.key()]
            sign = -1 if self.flipped else 1
            self._keyboard_square = _square(max(0, min(9, row+dy*sign)), max(0, min(8, column+dx*sign)))
            self.update()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event):
        self._keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason,
                                                 Qt.FocusReason.BacktabFocusReason,
                                                 Qt.FocusReason.ShortcutFocusReason)
        super().focusInEvent(event)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        dark = current_theme() == "dark"
        paper = QColor("#302e29" if dark else "#fcf8ef")
        line = QColor("#9d917a" if dark else "#b29e7c")
        ink = QColor("#cbbfa9" if dark else "#897658")
        grid, cell = self._geometry()
        painter.setPen(QPen(QColor("#4b463b" if dark else "#e5dbc8"), 1))
        painter.setBrush(paper)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 15, 15)
        painter.setPen(QPen(line, 1.1))
        for row in range(10):
            y = grid.top()+row*cell
            painter.drawLine(QPointF(grid.left(), y), QPointF(grid.right(), y))
        for column in range(9):
            x = grid.left()+column*cell
            if column in (0, 8):
                painter.drawLine(QPointF(x, grid.top()), QPointF(x, grid.bottom()))
            else:
                painter.drawLine(QPointF(x, grid.top()), QPointF(x, grid.top()+4*cell))
                painter.drawLine(QPointF(x, grid.top()+5*cell), QPointF(x, grid.bottom()))
        for first in (0, 7):
            painter.drawLine(QPointF(grid.left()+3*cell, grid.top()+first*cell),
                             QPointF(grid.left()+5*cell, grid.top()+(first+2)*cell))
            painter.drawLine(QPointF(grid.left()+5*cell, grid.top()+first*cell),
                             QPointF(grid.left()+3*cell, grid.top()+(first+2)*cell))
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(max(16, round(cell*.35)))
        painter.setFont(font)
        painter.setPen(ink)
        painter.drawText(QRectF(grid.left()+cell*.5, grid.top()+4*cell, cell*3, cell), Qt.AlignmentFlag.AlignCenter, "楚 河")
        painter.drawText(QRectF(grid.left()+cell*4.5, grid.top()+4*cell, cell*3, cell), Qt.AlignmentFlag.AlignCenter, "汉 界")
        font.setPixelSize(max(10, min(13, round(cell*.23))))
        painter.setFont(font)
        for column in range(9):
            text, lower = self.file_labels(column)
            x = grid.left()+column*cell
            painter.drawText(QRectF(x-cell*.3, grid.top()-cell*.405-23, cell*.6, 18), Qt.AlignmentFlag.AlignCenter, text)
            painter.drawText(QRectF(x-cell*.3, grid.bottom()+cell*.405+5, cell*.6, 18), Qt.AlignmentFlag.AlignCenter, lower)
        # Last-move marks remain behind pieces and do not cover glyphs.
        active = self._motion.active
        last = active.move if active else self._motion.last_move
        if isinstance(last, str) and len(last) == 4:
            self._paint_move_path(painter, last, cell, active is not None)
        hint = self._state.get("hint")
        if not active and isinstance(hint, str) and len(hint) == 4:
            try:
                start, end = self.point_for_square(hint[:2]), self.point_for_square(hint[2:])
                painter.setPen(QPen(QColor(color("accent")), 2, Qt.PenStyle.DashLine))
                painter.drawLine(start, end)
            except ValueError:
                pass
        board = active.before if active else self._motion.board
        checked_side = self.displayed_checked_side
        checked_king = "K" if checked_side == "red" else "k" if checked_side == "black" else ""
        for row, pieces in enumerate(board[:10]):
            for column, piece in enumerate(pieces[:9]):
                if piece not in _GLYPHS:
                    continue
                square = _square(row, column)
                if active and square == active.source:
                    continue
                point = self.point_for_square(square)
                self._paint_piece(painter, piece, point, cell, dark, piece == checked_king)
        if active:
            start = self.point_for_square(active.source)
            end = self.point_for_square(active.target)
            point = start+(end-start)*self._move_progress
            self._paint_piece(painter, active.piece, point, cell, dark, active.piece == checked_king)
        # Legal destinations, including captures, use one consistent blue cue.
        if self._selected:
            selected = self.point_for_square(self._selected)
            painter.setPen(QPen(QColor(color("accent")), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(selected, cell*.46, cell*.46)
            for move in self._legal_moves():
                if move.startswith(self._selected) and len(move) == 4:
                    target = self.point_for_square(move[2:])
                    rr, cc = _coordinates(move[2:])
                    occupied = rr < len(board) and cc < len(board[rr]) and board[rr][cc] not in (".", " ", "")
                    painter.setPen(QPen(QColor(color("accent")), 2) if occupied else QPen(Qt.PenStyle.NoPen))
                    painter.setBrush(Qt.BrushStyle.NoBrush if occupied else QColor(color("accent")))
                    painter.drawEllipse(target, cell*(.45 if occupied else .065), cell*(.45 if occupied else .065))
        if self.hasFocus() and self._keyboard_focus and not self._selected and not active:
            point = self.point_for_square(self._keyboard_square)
            painter.setPen(QPen(QColor(color("focus")), 1.2, Qt.PenStyle.DotLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(point, cell*.46, cell*.46)
        self._paint_check_notice(painter, grid, cell)

    def _paint_piece(self, painter, piece, point, cell, dark, checked=False):
        radius = cell*.405
        if checked:
            halo = QColor(color("danger"))
            halo.setAlpha(35+round(25*math.sin(math.pi*self._check_progress)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(halo)
            painter.drawEllipse(point, radius+9, radius+9)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(31, 38, 46, 28))
        painter.drawEllipse(point+QPointF(0, 1.8), radius+1, radius+1)
        painter.setBrush(QColor("#e7decc" if dark else "#fffdf8"))
        painter.setPen(QPen(QColor("#bdb099" if dark else "#c8b99d"), 1.3))
        painter.drawEllipse(point, radius, radius)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#c8b99d"), .65))
        painter.drawEllipse(point, radius-3.5, radius-3.5)
        painter.setPen(QColor("#ac3e36" if piece.isupper() else "#38424c"))
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(max(17, round(cell*.47)))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(QRectF(point.x()-radius, point.y()-radius-1, radius*2, radius*2),
                         Qt.AlignmentFlag.AlignCenter, _GLYPHS[piece])
        if checked:
            painter.setPen(QPen(QColor(color("danger")), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(point, radius+3, radius+3)

    def _paint_move_path(self, painter, move, cell, moving):
        try:
            start, end = self.point_for_square(move[:2]), self.point_for_square(move[2:])
        except ValueError:
            return
        accent = QColor(color("accent"))
        accent.setAlpha(180)
        painter.setPen(QPen(accent, 1.8))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(start, cell*.13, cell*.13)
        painter.drawEllipse(end, cell*.45, cell*.45)
        emphasis = 1.0 if moving else self._arrival_strength
        if emphasis <= 0:
            return
        accent.setAlpha(round(130*emphasis))
        pen = QPen(accent, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(start, end)
        painter.drawEllipse(end, cell*(.46+.04*emphasis), cell*(.46+.04*emphasis))

    def _paint_check_notice(self, painter, grid, cell):
        if not self.displayed_checked_side or self._check_progress >= 1.0:
            return
        progress = self._check_progress
        opacity = min(1.0, progress/.1, (1.0-progress)/.25)
        width, height = max(108.0, cell*2.5), max(46.0, cell*.8)
        notice = QRectF(grid.center().x()-width/2, grid.center().y()-height/2, width, height)
        painter.save()
        painter.setOpacity(opacity)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#a63730"))
        painter.drawRoundedRect(notice, 12, 12)
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(max(24, round(cell*.43)))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#fffaf1"))
        painter.drawText(notice, Qt.AlignmentFlag.AlignCenter, "将 军")
        painter.restore()


_STYLE = """
QWidget#xiangqiWindow { background:transparent; color:#344054; font-family:'Microsoft YaHei UI'; font-size:13px; }
QLabel#xiangqiSecondary { color:#667386; }
QLabel#xiangqiStatus { color:#4d82b8; font-weight:600; }
QLabel#xiangqiStatus[checkWarning="true"] { color:#a12622; background:#fff0ed; padding:4px 10px; border-radius:9px; }
QLabel#xiangqiError { color:#a12622; background:#fff0ed; border-radius:8px; padding:9px; }
QFrame#xiangqiSidebar { background:transparent; border:0; }
QFrame#xiangqiBoardCard { background:#ffffff; border:1px solid #e6eaf0; border-radius:16px; }
QPushButton { min-height:34px; padding:0 13px; border:1px solid #d0d8e2; border-radius:8px; background:#ffffff; color:#344054; }
QPushButton:hover { background:#edf2f8; border-color:#87a8cb; }
QPushButton:pressed { background:#e6edf5; }
QPushButton:focus { border:1px solid #87a8cb; }
QPushButton:disabled { color:#8a96a5; border-color:#e6eaf0; background:#f8fafc; }
QPushButton#xiangqiNewGame { background:#356da7; color:#ffffff; border-color:#356da7; font-weight:600; }
QPushButton#xiangqiNewGame:hover { background:#285d8e; }
QPushButton#xiangqiNewGame:disabled { background:#e6edf5; border-color:#e6edf5; color:#8a96a5; }
QComboBox { min-height:34px; padding:0 10px; border:1px solid #d0d8e2; border-radius:8px; background:#ffffff; }
QComboBox QAbstractItemView { background:#ffffff; color:#344054; selection-background-color:#e6edf5; padding:4px; }
QLineEdit { min-height:36px; padding:0 10px; border:1px solid #d0d8e2; border-radius:8px; background:#ffffff; color:#344054; }
QLineEdit:focus { border-color:#87a8cb; }
QTabWidget::pane { border:0; background:transparent; }
QTabBar::tab { padding:10px 16px; color:#667386; border-bottom:2px solid transparent; }
QTabBar::tab:selected { color:#356da7; border-bottom:2px solid #356da7; }
QScrollArea, QWidget#xiangqiMessages { border:0; background:transparent; }
QLabel#xiangqiMessage { background:#ffffff; border:1px solid #e6eaf0; border-radius:9px; padding:10px; color:#344054; }
QLabel#xiangqiUserMessage { background:#e6edf5; border:0; border-radius:9px; padding:10px; color:#344054; }
QListWidget { border:0; background:transparent; outline:0; }
QListWidget::item { padding:10px 8px; border-bottom:1px solid #e6eaf0; color:#344054; }
QListWidget::item:selected { background:#e6edf5; }
QSplitter::handle { background:transparent; }
QProgressBar { border:0; background:#e6edf5; border-radius:2px; }
QProgressBar::chunk { background:#87a8cb; border-radius:2px; }
QFrame#xiangqiSidebar[compactGame="true"] QPushButton,
QFrame#xiangqiSidebar[compactGame="true"] QComboBox { min-height:26px; }
QFrame#xiangqiSidebar[compactGame="true"] QLineEdit { min-height:30px; }
QFrame#xiangqiSidebar[compactGame="true"] QTabBar::tab { padding:6px 12px; }
"""


class XiangqiWindow(QWidget):
    """An independent hide-on-close game window; the controller owns the game."""
    def __init__(self, controller, parent=None, icon_path=None, *, board_factory=None):
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setProperty("auraCustomChrome", True)
        self._restore_maximized = False
        self.controller = controller
        self._state = {}
        self._messages_key = None
        self._moves_key = None
        self._last_side = None
        self._chat_follow_latest = True
        self.setObjectName("xiangqiWindow")
        self.setWindowTitle("和鲸鱼娘下象棋 - 鲸伴")
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(720, 420)
        self.resize(1100, 800)
        image = icon_path or PROJECT_ROOT / "assets/expressions/neutral.png"
        self.setWindowIcon(QIcon(str(image)))
        style_widget(self, _STYLE)
        layout = QVBoxLayout(self)
        margin = WindowChrome.FRAME_MARGIN
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(0)
        self.pages = QWidget(self)
        layout.addWidget(self.pages)
        content = QVBoxLayout(self.pages)
        content.setContentsMargins(14, 10, 14, 10)
        content.setSpacing(8)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(16)
        self.splitter.setChildrenCollapsible(False)
        content.addWidget(self.splitter, 1)
        card = QFrame()
        card.setObjectName("xiangqiBoardCard")
        board_layout = QVBoxLayout(card)
        board_layout.setContentsMargins(15, 13, 15, 12)
        board_layout.setSpacing(8)
        self.board_header = QWidget()
        self.board_header.setObjectName("gameBoardHeader")
        state_row = QHBoxLayout(self.board_header)
        state_row.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel("准备开局")
        self.status_label.setObjectName("xiangqiStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("当前棋局状态")
        state_row.addWidget(self.status_label)
        state_row.addStretch(1)
        self.side_label = QLabel("你执红 · 先手")
        self.side_label.setObjectName("xiangqiSecondary")
        state_row.addWidget(self.side_label)
        board_layout.addWidget(self.board_header)
        self.board = (board_factory or XiangqiBoardWidget)()
        self.board.move_requested.connect(self.controller.play_move)
        board_layout.addWidget(self.board, 1)
        self.board_help = QLabel("选择己方棋子，再点击标记的落点")
        self.board_help.setObjectName("xiangqiSecondary")
        self.board_help.setAlignment(Qt.AlignmentFlag.AlignCenter)
        board_layout.addWidget(self.board_help)
        self.splitter.addWidget(card)
        sidebar = QFrame()
        sidebar.setObjectName("xiangqiSidebar")
        side_layout = QVBoxLayout(sidebar)
        self._sidebar_layout = side_layout
        side_layout.setContentsMargins(16, 16, 16, 12)
        side_layout.setSpacing(10)
        self.character_header = QWidget()
        character = QHBoxLayout(self.character_header)
        caption_space = (WindowChrome.CONTROL_WIDTH - content.contentsMargins().right()
                         - side_layout.contentsMargins().right())
        character.setContentsMargins(0, 0, caption_space, 0)
        avatar = QLabel()
        avatar.setAccessibleName("鲸鱼娘头像")
        avatar.setPixmap(_avatar(38))
        avatar.setFixedSize(38, 38)
        character.addWidget(avatar)
        character_name = QVBoxLayout()
        character_name.setSpacing(3)
        character_name.addWidget(QLabel("鲸鱼娘"))
        self.character_state = QLabel("准备和你下一局")
        self.character_state.setObjectName("xiangqiSecondary")
        character_name.addWidget(self.character_state)
        character.addLayout(character_name, 1)
        side_layout.addWidget(self.character_header)
        self.side_choice = QComboBox()
        self.side_choice.setAccessibleName("新对局执子")
        self.side_choice.addItem("我执红 · 先手", "red")
        self.side_choice.addItem("我执黑 · 后手", "black")
        side_layout.addWidget(self.side_choice)
        self.difficulty_choice = QComboBox()
        self.difficulty_choice.setAccessibleName("象棋难度")
        for code, text in _DIFFICULTIES:
            self.difficulty_choice.addItem(text, code)
        self.difficulty_choice.setCurrentIndex(1)
        self.difficulty_choice.currentIndexChanged.connect(self._difficulty_changed)
        side_layout.addWidget(self.difficulty_choice)
        self.new_button = self._button("开始新一局", "新建象棋对局", self._new_game)
        self.new_button.setObjectName("xiangqiNewGame")
        side_layout.addWidget(self.new_button)
        tools = QHBoxLayout()
        tools.setSpacing(7)
        self.undo_button = self._button("悔棋", "悔棋一回合", self.controller.undo)
        self.hint_button = self._button("提示", "请求走棋提示", self.controller.request_hint)
        self.resign_button = self._button("认输", "认输结束本局", self._resign)
        for button in (self.undo_button, self.hint_button, self.resign_button):
            tools.addWidget(button)
        side_layout.addLayout(tools)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        policy = self.progress.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.progress.setSizePolicy(policy)
        side_layout.addWidget(self.progress)
        self.error_label = QLabel()
        self.error_label.setObjectName("xiangqiError")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setAccessibleName("象棋错误信息")
        self.error_label.hide()
        side_layout.addWidget(self.error_label)
        self.retry_button = self._button("重新连接棋力", "重试象棋引擎", self._retry)
        self.retry_button.hide()
        side_layout.addWidget(self.retry_button)
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("象棋侧栏")
        self.chat_page = QWidget()
        chat_layout = QVBoxLayout(self.chat_page)
        chat_layout.setContentsMargins(0, 9, 0, 0)
        chat_layout.setSpacing(8)
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_scroll.setAccessibleName("象棋陪玩对话")
        self.messages_widget = QWidget()
        self.messages_widget.setObjectName("xiangqiMessages")
        self.messages_layout = QVBoxLayout(self.messages_widget)
        self.messages_layout.setContentsMargins(0, 0, 3, 0)
        self.messages_layout.setSpacing(10)
        self.messages_layout.addStretch(1)
        self.chat_scroll.setWidget(self.messages_widget)
        scrollbar = self.chat_scroll.verticalScrollBar()
        scrollbar.rangeChanged.connect(self._scroll_chat_end)
        scrollbar.valueChanged.connect(self._chat_scroll_changed)
        chat_layout.addWidget(self.chat_scroll, 1)
        question_label = self.question_label = QLabel("问问鲸鱼娘")
        question_label.setObjectName("xiangqiSecondary")
        chat_layout.addWidget(question_label)
        composer = QHBoxLayout()
        composer.setSpacing(7)
        self.question_input = QLineEdit()
        self.question_input.setAccessibleName("象棋对局问题")
        self.question_input.setPlaceholderText("这步怎么下？")
        self.question_input.setMaxLength(1000)
        self.question_input.returnPressed.connect(self._ask)
        self.question_input.textChanged.connect(self._update_send)
        question_label.setBuddy(self.question_input)
        composer.addWidget(self.question_input, 1)
        self.send_button = self._button("发送", "发送象棋对局问题", self._ask)
        composer.addWidget(self.send_button)
        chat_layout.addLayout(composer)
        self.tabs.addTab(self.chat_page, "陪你下棋")
        self.move_list = QListWidget()
        self.move_list.setAccessibleName("象棋棋谱")
        self.move_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.record_page = QWidget()
        record_layout = QVBoxLayout(self.record_page)
        record_layout.setContentsMargins(0, 9, 0, 0)
        record_layout.addWidget(self.move_list, 1)
        self.export_button = self._button("导出棋谱", "导出象棋棋谱", self._export_record)
        self.export_button.setEnabled(callable(getattr(self.controller, "export_record", None)))
        record_layout.addWidget(self.export_button)
        self.tabs.addTab(self.record_page, "棋谱")
        side_layout.addWidget(self.tabs, 1)
        # On a short screen the settings, error recovery and composer cannot
        # all keep their normal height. Keep every control reachable rather
        # than letting their minimum sizes push the board outside the window.
        # The chat retains its own scroll position; this scroll area only
        # moves the surrounding controls when they do not fit.
        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setObjectName("gameSidebarScroll")
        self.sidebar_scroll.setAccessibleName("棋局设置与对话")
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setMinimumWidth(285)
        self.sidebar_scroll.setMaximumWidth(360)
        self.sidebar_scroll.setWidget(sidebar)
        self.splitter.addWidget(self.sidebar_scroll)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([690, 330])
        self.chrome = WindowChrome(self)
        self.chrome.add_drag_region(self, height=WindowChrome.FRAME_MARGIN + WindowChrome.CONTROL_HEIGHT)
        self.chrome.add_drag_region(self.pages, height=WindowChrome.CONTROL_HEIGHT)
        self.chrome.add_drag_region(self.board_header)
        self.chrome.add_drag_region(self.status_label)
        self.chrome.add_drag_region(self.side_label)
        self.chrome.add_drag_region(self.character_header)
        self.board.animation_changed.connect(self._update_move_controls)
        self.controller.changed.connect(self._state_changed)
        manager = theme_manager()
        if manager is not None:
            manager.theme_changed.connect(self._theme_changed)
        self._state_changed(self.controller.snapshot())
        self._update_compact_layout()

    def _update_compact_layout(self):
        if not hasattr(self, "sidebar_scroll"):
            return
        sidebar = self.sidebar_scroll.widget()
        compact = self.height() < 580
        if sidebar.property("compactGame") == compact:
            return
        sidebar.setProperty("compactGame", compact)
        self._sidebar_layout.setContentsMargins(16, 10 if compact else 16, 16, 12)
        self._sidebar_layout.setSpacing(6 if compact else 10)
        # The composer placeholder already labels this field on short screens.
        self.question_label.setVisible(not compact)
        # Only these controls change size. Restyling the message labels here
        # rebuilds long chat layouts and can momentarily move their scrollbar
        # to the end, losing the reader's deliberate position.
        for widget in sidebar.findChildren(QWidget):
            if isinstance(widget, (QPushButton, QComboBox, QLineEdit, QTabBar)):
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_compact_layout()

    @staticmethod
    def _button(text, name, callback):
        button = QPushButton(text)
        button.setAccessibleName(name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _checked=False: callback())
        return button

    def _confirm(self, title, text, accept):
        box = QMessageBox(QMessageBox.Icon.Question, title, text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.button(QMessageBox.StandardButton.Yes).setText(accept)
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _new_game(self):
        if self._state.get("moves") and not self._state.get("outcome"):
            if not self._confirm("开始新对局", "当前棋局将结束，要开始新的一局吗？", "开始新局"):
                return
        self._chat_follow_latest = True
        self.controller.new_game(human_side=self.side_choice.currentData(),
                                 difficulty=self.difficulty_choice.currentData())

    def _resign(self):
        if self._confirm("结束本局", "这一局要认输吗？", "认输"):
            self.controller.resign()

    def _retry(self):
        callback = getattr(self.controller, "retry_engine", None) or self.controller.start
        callback()

    def _export_record(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "导出象棋棋谱", "鲸鱼娘象棋棋谱.txt", "文本棋谱 (*.txt)")
        if path:
            self.controller.export_record(path)

    def _difficulty_changed(self, _index):
        self.controller.set_difficulty(self.difficulty_choice.currentData())

    def _ask(self):
        question = self.question_input.text().strip()
        if not question or not self.send_button.isEnabled():
            return
        if self.controller.ask(question) is not False:
            self.question_input.clear()
            self._chat_follow_latest = True
            QTimer.singleShot(0, self._scroll_chat_end)

    def _update_send(self, _text=None):
        self.send_button.setEnabled(bool(self.question_input.text().strip())
                                    and not self._state.get("commentary_busy", False))

    def _theme_changed(self, _theme):
        self.board.update()
        apply_titlebar(self)

    def _update_move_controls(self, _moving=False):
        state = self._state
        busy = bool(state.get("busy"))
        moving = self.board.animating
        checked_side = self.board.displayed_checked_side
        in_check = checked_side is not None
        if self.status_label.property("checkWarning") != in_check:
            self.status_label.setProperty("checkWarning", in_check)
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
        self.status_label.setText(("将军 · 先应将" if checked_side == state.get("human_side") else "将军 · 对方应将")
                                 if in_check else str(state.get("status") or "轮到你了"))
        self.hint_button.setEnabled(bool(state.get("engine_ready")) and not busy and not moving
                                    and not state.get("outcome") and state.get("turn") == state.get("human_side", "red"))
        self.character_state.setText("正在落子…" if moving else ("正在想这一步…" if busy else
                                     ("这一局下完啦" if state.get("outcome") else "准备接招")))

    def _state_changed(self, state):
        self._state = dict(state)
        self.board.set_state(self._state)
        busy = bool(state.get("busy"))
        ready = bool(state.get("engine_ready"))
        human = state.get("human_side", "red")
        if self._last_side != human:
            self.side_choice.blockSignals(True)
            self.side_choice.setCurrentIndex(max(0, self.side_choice.findData(human)))
            self.side_choice.blockSignals(False)
            self._last_side = human
        self.difficulty_choice.blockSignals(True)
        index = self.difficulty_choice.findData(state.get("difficulty", "standard"))
        self.difficulty_choice.setCurrentIndex(max(0, index))
        self.difficulty_choice.blockSignals(False)
        self.progress.setVisible(busy)
        self.status_label.setText(str(state.get("status") or ("鲸鱼娘正在思考…" if busy else "轮到你了")))
        self.side_label.setText("你执红 · 先手" if human == "red" else "你执黑 · 后手")
        self.board_help.setText("鲸鱼娘正在思考，稍等一下" if busy else
                                ("可以开始新一局，或在旁边一起聊聊这盘棋" if state.get("outcome") else "选择己方棋子，再点击标记的落点"))
        error = str(state.get("error") or state.get("storage_warning") or "")
        self.error_label.setText(error)
        self.error_label.setVisible(bool(error))
        self.retry_button.setVisible(not ready and not busy)
        self.retry_button.setEnabled(not busy)
        self.undo_button.setEnabled(bool(state.get("can_undo")))
        self._update_move_controls()
        self.resign_button.setEnabled(not state.get("outcome") and ready)
        self.difficulty_choice.setEnabled(not busy)
        self._update_send()
        moves = state.get("moves", [])
        moves_key = tuple(str(move) for move in moves)
        if moves_key != self._moves_key:
            self._moves_key = moves_key
            self.move_list.clear()
            if not moves:
                self.move_list.addItem("落子后，棋谱会记录在这里")
            for number, move in enumerate(moves):
                entry = move if isinstance(move, dict) else {"label": str(move)}
                side = "红" if entry.get("side", "red" if number%2 == 0 else "black") == "red" else "黑"
                self.move_list.addItem(f"{number//2+1:02d}  {side}方   {entry.get('label') or entry.get('move', '')}")
            self.move_list.scrollToBottom()
            self.tabs.setTabText(1, f"棋谱 · {len(moves)}")
        self._render_commentary(state.get("commentary", []))

    def _render_commentary(self, messages):
        entries = [(str(item.get("role", "assistant")), str(item.get("text", "")))
                   for item in messages if isinstance(item, dict) and item.get("text")]
        if not entries:
            entries = [("assistant", "来下一局吧。想听提示或聊聊这一步，随时问我。")]
        key = tuple(entries)
        if key == self._messages_key:
            return
        old_count = len(self._messages_key or ())
        append_only = bool(self._messages_key) and key[:old_count] == self._messages_key
        if not append_only:
            while self.messages_layout.count() > 1:
                item = self.messages_layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            old_count = 0
        for role, text in entries[old_count:]:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            row_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            if role != "user":
                avatar = QLabel()
                avatar.setFixedSize(24, 24)
                avatar.setPixmap(_avatar(24))
                avatar.setAccessibleName("鲸鱼娘头像")
                row_layout.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
            label = QLabel(text)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setObjectName("xiangqiUserMessage" if role == "user" else "xiangqiMessage")
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            row_layout.addWidget(label, 1)
            self.messages_layout.insertWidget(self.messages_layout.count()-1, row)
        self._messages_key = key
        if self._chat_follow_latest:
            QTimer.singleShot(0, self._scroll_chat_end)

    def _chat_scroll_changed(self, value):
        scrollbar = self.chat_scroll.verticalScrollBar()
        # Rebuilding capped history can temporarily collapse its range to zero.
        # That layout adjustment does not mean the user scrolled back down.
        if scrollbar.maximum() > 0:
            self._chat_follow_latest = scrollbar.maximum()-value < 35

    def _scroll_chat_end(self, *_range):
        # Wrapped labels can resize over several layout passes. Follow each
        # new range rather than the old maximum from a single queued callback.
        if isValid(self) and self._chat_follow_latest:
            scrollbar = self.chat_scroll.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def present(self):
        if self.isMinimized():
            self.showMaximized() if self._restore_maximized else self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color("window")))
        radius = 0 if self.isMaximized() or self.isFullScreen() else WindowChrome.CORNER_RADIUS
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)

    def nativeEvent(self, event_type, message):
        chrome = getattr(self, "chrome", None)
        result = chrome.native_event(event_type, message) if chrome is not None else None
        return result if result is not None else super().nativeEvent(event_type, message)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self._restore_maximized = bool(event.oldState() & Qt.WindowState.WindowMaximized)
                if hasattr(self, "board"):
                    self.board.stop_motion()
            else:
                self._restore_maximized = self.isMaximized()
            if self.layout() is not None:
                margin = 0 if self.isMaximized() or self.isFullScreen() else WindowChrome.FRAME_MARGIN
                self.layout().setContentsMargins(margin, margin, margin, margin)
        super().changeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        apply_titlebar(self)

    def closeEvent(self, event):
        self.hide()
        event.ignore()
