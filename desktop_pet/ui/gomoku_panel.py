"""A compact Gomoku board using the desktop game's established chat shell."""
from __future__ import annotations

from collections import deque
import math
import re

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QComboBox, QSizePolicy, QWidget

from .theme import color, current_theme, style_widget
from .xiangqi_panel import XiangqiWindow, _STYLE


_COLUMNS = "ABCDEFGHIJKLMNO"
_RULES = (("freestyle", "自由五子棋"), ("standard", "标准五子棋 · 恰好五子"))


def _coordinates(square):
    if not isinstance(square, str) or not re.fullmatch(r"[A-O](?:[1-9]|1[0-5])", square):
        raise ValueError("五子棋坐标无效。")
    return int(square[1:])-1, _COLUMNS.index(square[0])


def _square(row, column):
    return _COLUMNS[column]+str(row+1)


def _board_rows(state):
    rows = state.get("board", ())
    if len(rows) != 15 or any(len(row) != 15 for row in rows):
        return tuple(tuple("." for _ in range(15)) for _ in range(15))
    return tuple(tuple(row) for row in rows)


class GomokuBoardWidget(QWidget):
    """One reversible grid transform for pointer, keyboard and painted stones."""
    move_requested = Signal(str)
    animation_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("gomokuBoard")
        self.setAccessibleName("五子棋棋盘")
        self.setAccessibleDescription("A1位于左上角。点击空交点落子，方向键和回车也可操作。")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 280)
        self._state = {}
        self._target_board = ()
        self._shown_board = ()
        self._history = ()
        self._identity = None
        self._shown_last = None
        self._pending_submission = False
        self._keyboard_square = "H8"
        self._keyboard_focus = False
        self._active = None
        self._queue = deque()
        self._progress = 0.0
        self._placement = QVariantAnimation(self)
        self._placement.setStartValue(0.0)
        self._placement.setEndValue(1.0)
        self._placement.setDuration(220)
        self._placement.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._placement.valueChanged.connect(self._frame)
        self._placement.finished.connect(self._land)

    def sizeHint(self):
        return QSize(610, 650)

    @property
    def animating(self):
        return self._active is not None

    def _geometry(self):
        cell = max(1.0, min((self.width()-54)/14, (self.height()-62)/14))
        return QRectF((self.width()-14*cell)/2, (self.height()-14*cell)/2, 14*cell, 14*cell), cell

    def point_for_square(self, square):
        row, column = _coordinates(square)
        grid, cell = self._geometry()
        return QPointF(grid.left()+column*cell, grid.top()+row*cell)

    def square_at(self, point):
        grid, cell = self._geometry()
        column = round((point.x()-grid.left())/cell)
        row = round((point.y()-grid.top())/cell)
        if not 0 <= row < 15 or not 0 <= column < 15:
            return None
        center = QPointF(grid.left()+column*cell, grid.top()+row*cell)
        if math.hypot(point.x()-center.x(), point.y()-center.y()) > cell*.48:
            return None
        return _square(row, column)

    def set_state(self, state):
        rows = _board_rows(state)
        history = tuple(row.get("move") if isinstance(row, dict) else row
                        for row in state.get("moves", ()))
        identity = state.get("rule", "freestyle"), state.get("human_side", "black")
        append_only = (self._target_board and identity == self._identity
                       and len(history) >= len(self._history) and history[:len(self._history)] == self._history)
        animate = (self.isVisible() and not self.window().isMinimized()
                   and self._state.get("engine_ready") and append_only
                   and (state.get("outcome") or {}).get("reason") != "resign")
        additions = []
        current = self._target_board
        if animate:
            try:
                for index, move in enumerate(history[len(self._history):], len(self._history)):
                    row, column = _coordinates(move)
                    if current[row][column] != ".":
                        raise ValueError
                    changed = [list(line) for line in current]
                    piece = "X" if index % 2 == 0 else "O"
                    changed[row][column] = piece
                    current = tuple(tuple(line) for line in changed)
                    additions.append((move, piece, current))
                if current != rows:
                    animate = False
            except (ValueError, TypeError, IndexError):
                animate = False
        self._state = dict(state)
        self._target_board, self._history, self._identity = rows, history, identity
        self._pending_submission = False
        if not animate:
            self.stop_motion()
        else:
            self._queue.extend(additions)
            if self._active is None and self._queue:
                self._start_next()
        self._refresh_cursor()
        self.update()

    def _start_next(self):
        self._active = self._queue.popleft()
        self._progress = 0.0
        self._placement.start()
        self.animation_changed.emit(True)

    def _frame(self, value):
        self._progress = float(value)
        self.update()

    def _land(self):
        if self._active is not None:
            self._shown_last, _, self._shown_board = self._active
        self._active = None
        if self._queue:
            self._start_next()
        else:
            self._refresh_cursor()
            self.animation_changed.emit(False)
        self.update()

    def stop_motion(self):
        self._placement.stop()
        self._queue.clear()
        self._active = None
        self._shown_board = self._target_board
        self._shown_last = self._history[-1] if self._history else self._state.get("last_move")
        self._refresh_cursor()
        self.animation_changed.emit(False)
        self.update()

    def hideEvent(self, event):
        self.stop_motion()
        super().hideEvent(event)

    def _can_play(self):
        return (self._state.get("engine_ready", False) and not self._state.get("busy")
                and not self.animating and not self._pending_submission and not self._state.get("outcome")
                and self._state.get("turn") == self._state.get("human_side", "black"))

    def _refresh_cursor(self):
        self.setCursor(Qt.CursorShape.PointingHandCursor if self._can_play() else Qt.CursorShape.ArrowCursor)

    def _choose(self, square):
        if not square or not self._can_play() or square not in self._state.get("legal_moves", ()):
            return
        self._keyboard_square = square
        self._pending_submission = True
        self._refresh_cursor()
        self.move_requested.emit(square)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._keyboard_focus = False
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._choose(self.square_at(event.position()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def keyPressEvent(self, event):
        self._keyboard_focus = True
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._choose(self._keyboard_square)
            return
        deltas = {Qt.Key.Key_Left: (0, -1), Qt.Key.Key_Right: (0, 1),
                  Qt.Key.Key_Up: (-1, 0), Qt.Key.Key_Down: (1, 0)}
        if event.key() in deltas:
            row, column = _coordinates(self._keyboard_square)
            dy, dx = deltas[event.key()]
            self._keyboard_square = _square(max(0, min(14, row+dy)), max(0, min(14, column+dx)))
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
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = current_theme() == "dark"
        grid, cell = self._geometry()
        painter.setPen(QPen(QColor("#4b463b" if dark else "#e5dbc8"), 1))
        painter.setBrush(QColor("#302e29" if dark else "#fcf8ef"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 15, 15)
        line = QColor("#928670" if dark else "#b29e7c")
        painter.setPen(QPen(line, .9))
        for index in range(15):
            painter.drawLine(QPointF(grid.left()+index*cell, grid.top()), QPointF(grid.left()+index*cell, grid.bottom()))
            painter.drawLine(QPointF(grid.left(), grid.top()+index*cell), QPointF(grid.right(), grid.top()+index*cell))
        painter.setBrush(line)
        for row, column in ((3, 3), (3, 11), (7, 7), (11, 3), (11, 11)):
            painter.drawEllipse(self.point_for_square(_square(row, column)), 2.7, 2.7)
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(max(10, min(12, round(cell*.35))))
        painter.setFont(font)
        painter.setPen(QColor("#cbbfa9" if dark else "#897658"))
        for index in range(15):
            x, y = grid.left()+index*cell, grid.top()+index*cell
            painter.drawText(QRectF(x-cell*.4, grid.top()-27, cell*.8, 18), Qt.AlignmentFlag.AlignCenter, _COLUMNS[index])
            painter.drawText(QRectF(grid.left()-28, y-10, 22, 20), Qt.AlignmentFlag.AlignCenter, str(index+1))
        winning = self._state.get("winning_line", ()) if not self.animating else ()
        if len(winning) >= 5:
            painter.setPen(QPen(QColor("#e5b76f" if dark else "#b97629"), max(2.5, cell*.10),
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(self.point_for_square(winning[0]), self.point_for_square(winning[-1]))
        for row, line in enumerate(self._shown_board):
            for column, piece in enumerate(line):
                if piece in ("X", "O"):
                    self._paint_stone(painter, self.point_for_square(_square(row, column)), piece, cell)
        if self._active:
            move, piece, _ = self._active
            painter.save()
            painter.setOpacity(self._progress)
            self._paint_stone(painter, self.point_for_square(move), piece, cell, .78+.22*self._progress)
            painter.restore()
        last = self._active[0] if self._active else self._shown_last
        if last:
            try:
                point = self.point_for_square(last)
                painter.setPen(QPen(QColor(color("accent")), 1.8))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(point, cell*.47, cell*.47)
            except ValueError:
                pass
        if winning:
            painter.setPen(QPen(QColor("#e5b76f" if dark else "#b97629"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for square in winning:
                painter.drawEllipse(self.point_for_square(square), cell*.46, cell*.46)
        hint = self._state.get("hint")
        threat = self._state.get("threat") or {}
        marks = ([hint] if isinstance(hint, str) else []) + list(threat.get("moves", ()))
        if not self.animating:
            painter.setPen(QPen(QColor(color("accent")), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for square in marks:
                try:
                    painter.drawEllipse(self.point_for_square(square), cell*.19, cell*.19)
                except ValueError:
                    continue
        if self.hasFocus() and self._keyboard_focus:
            painter.setPen(QPen(QColor(color("focus")), 1.5, Qt.PenStyle.DotLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(self.point_for_square(self._keyboard_square), cell*.43, cell*.43)

    @staticmethod
    def _paint_stone(painter, point, piece, cell, scale=1.0):
        radius = cell*.405*scale
        gradient = QRadialGradient(point-QPointF(radius*.3, radius*.3), radius*1.6)
        gradient.setColorAt(0, QColor("#535963" if piece == "X" else "#ffffff"))
        gradient.setColorAt(1, QColor("#11151c" if piece == "X" else "#deded8"))
        painter.setPen(QPen(QColor("#0b1018" if piece == "X" else "#a9a99f"), .8))
        painter.setBrush(gradient)
        painter.drawEllipse(point, radius, radius)


class GomokuWindow(XiangqiWindow):
    """Reuse the rounded controls, chat scrolling and draft-preserving game UI."""
    def __init__(self, controller, parent=None, icon_path=None):
        self._gomoku_ready = False
        super().__init__(controller, parent=parent, icon_path=icon_path, board_factory=GomokuBoardWidget)
        self.setObjectName("gomokuWindow")
        style_widget(self, _STYLE.replace("#xiangqiWindow", "#gomokuWindow") + """
QLabel#gomokuStatus { color:#4d82b8; font-weight:600; }
QLabel#gomokuStatus[gameState="win"] { color:#315b4c; background:#edf7f2; padding:4px 10px; border-radius:9px; }
QLabel#gomokuStatus[gameState="loss"], QLabel#gomokuStatus[gameState="threat"] { color:#a12622; background:#fff0ed; padding:4px 10px; border-radius:9px; }
""")
        self.setWindowTitle("和鲸鱼娘下五子棋 - 鲸伴")
        self.status_label.setObjectName("gomokuStatus")
        self.status_label.setWordWrap(False)
        self.status_label.setAccessibleName("当前五子棋状态")
        self.side_choice.setAccessibleName("五子棋执子")
        self.side_choice.clear()
        self.side_choice.addItem("我执黑 · 先手", "black")
        self.side_choice.addItem("我执白 · 后手", "white")
        self.difficulty_choice.setAccessibleName("五子棋难度")
        self.new_button.setAccessibleName("新建五子棋对局")
        self.undo_button.setAccessibleName("五子棋悔棋一回合")
        self.hint_button.setAccessibleName("请求五子棋提示")
        self.resign_button.setAccessibleName("认输结束五子棋")
        self.retry_button.setAccessibleName("重试五子棋引擎")
        self.error_label.setAccessibleName("五子棋错误信息")
        self.tabs.setAccessibleName("五子棋侧栏")
        self.chat_scroll.setAccessibleName("五子棋陪玩对话")
        self.question_input.setAccessibleName("五子棋对局问题")
        self.question_input.setPlaceholderText("这步怎么考虑？")
        self.send_button.setAccessibleName("发送五子棋对局问题")
        self.move_list.setAccessibleName("五子棋棋谱")
        self.export_button.setAccessibleName("导出五子棋棋谱")
        self.rule_choice = QComboBox()
        self.rule_choice.setAccessibleName("五子棋规则")
        for rule, label in _RULES:
            self.rule_choice.addItem(label, rule)
        side_layout = self.new_button.parentWidget().layout()
        side_layout.insertWidget(side_layout.indexOf(self.new_button), self.rule_choice)
        self.rule_choice.currentIndexChanged.connect(self._rule_changed)
        self._gomoku_ready = True
        self._last_side = None
        self._state_changed(controller.snapshot())

    def _state_changed(self, state):
        self._state = dict(state)
        if not self._gomoku_ready:
            return
        self.board.set_state(self._state)
        human = state.get("human_side", "black")
        if self._last_side != human:
            self.side_choice.setCurrentIndex(max(0, self.side_choice.findData(human)))
            self._last_side = human
        for choice, value in ((self.difficulty_choice, state.get("difficulty", "standard")),
                              (self.rule_choice, state.get("rule", "freestyle"))):
            choice.blockSignals(True)
            choice.setCurrentIndex(max(0, choice.findData(value)))
            choice.blockSignals(False)
        busy = bool(state.get("busy"))
        self.progress.setVisible(busy)
        self.difficulty_choice.setEnabled(not busy)
        self.rule_choice.setEnabled(not busy)
        self.side_label.setText("你执黑 · 先手" if human == "black" else "你执白 · 后手")
        self.board_help.setText("点击空交点落子" if not state.get("outcome") else "可以开始新一局，或聊聊这盘棋")
        self.error_label.setText(str(state.get("error") or state.get("storage_warning") or ""))
        self.error_label.setVisible(bool(self.error_label.text()))
        self.retry_button.setVisible(not state.get("engine_ready") and not busy)
        self.retry_button.setEnabled(not busy)
        self.undo_button.setEnabled(bool(state.get("can_undo")))
        self.resign_button.setEnabled(bool(state.get("engine_ready")) and not state.get("outcome"))
        self._update_move_controls()
        self._update_send()
        moves = state.get("moves", ())
        key = tuple(str(move) for move in moves)
        if key != self._moves_key:
            self._moves_key = key
            self.move_list.clear()
            if not moves:
                self.move_list.addItem("落子后，棋谱会记录在这里")
            for number, row in enumerate(moves):
                side = "黑" if row.get("side") == "black" else "白"
                self.move_list.addItem(f"{number+1:02d}  {side}方   {row.get('label') or row.get('move', '')}")
            self.move_list.scrollToBottom()
            self.tabs.setTabText(1, f"棋谱 · {len(moves)}")
        self._render_commentary(state.get("commentary", ()))

    def _update_move_controls(self, _moving=False):
        if not self._gomoku_ready:
            return
        state = self._state
        moving, busy = self.board.animating, bool(state.get("busy"))
        feedback = "normal"
        outcome = state.get("outcome")
        if outcome and not moving:
            winner = outcome.get("winner")
            feedback = "win" if winner == state.get("human_side") else "loss" if winner != "draw" else "normal"
        elif not moving and (state.get("threat") or {}).get("side") not in (None, state.get("human_side")):
            feedback = "threat"
        if self.status_label.property("gameState") != feedback:
            self.status_label.setProperty("gameState", feedback)
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
        self.status_label.setText(str(state.get("status") or "轮到你了"))
        self.hint_button.setEnabled(bool(state.get("engine_ready")) and not busy and not moving
                                    and not state.get("outcome") and state.get("turn") == state.get("human_side", "black"))
        self.character_state.setText("正在落子…" if moving else ("正在想这一步…" if busy else
                                     ("这一局下完啦" if state.get("outcome") else "准备接招")))

    def _new_game(self):
        if self._state.get("moves") and not self._state.get("outcome"):
            if not self._confirm("开始新对局", "当前棋局将结束，要开始新的一局吗？", "开始新局"):
                return
        self._chat_follow_latest = True
        self.controller.new_game(human_side=self.side_choice.currentData(),
                                 difficulty=self.difficulty_choice.currentData(), rule=self.rule_choice.currentData())

    def _rule_changed(self, _index):
        rule = self.rule_choice.currentData()
        if rule == self._state.get("rule", "freestyle"):
            return
        if self._state.get("moves") and not self._state.get("outcome"):
            if not self._confirm("更换五子棋规则", "更换规则会开始新的一局，继续吗？", "开始新局"):
                self.rule_choice.blockSignals(True)
                self.rule_choice.setCurrentIndex(self.rule_choice.findData(self._state.get("rule", "freestyle")))
                self.rule_choice.blockSignals(False)
                return
        self._chat_follow_latest = True
        self.controller.set_rule(rule)

    def _export_record(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "导出五子棋棋谱", "鲸鱼娘五子棋棋谱.txt", "文本棋谱 (*.txt)")
        if path:
            self.controller.export_record(path)
