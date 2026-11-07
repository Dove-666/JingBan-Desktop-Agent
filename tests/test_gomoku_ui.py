"""Native Gomoku placement, shared chrome and reader-friendly game chat."""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch

from PySide6.QtCore import QAbstractAnimation, QEvent, QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from desktop_pet.config import PROJECT_ROOT
from desktop_pet.ui.gomoku_panel import GomokuWindow, _coordinates, _square
from desktop_pet.ui.theme import ThemeManager
from tests.test_xiangqi_ui import _ThemeSettings


def empty_board():
    return [["."]*15 for _ in range(15)]


class Controller(QObject):
    changed = Signal(dict)

    def __init__(self):
        super().__init__()
        self.state = {"board": empty_board(), "turn": "black", "human_side": "black",
                      "rule": "freestyle", "difficulty": "standard", "engine_ready": True,
                      "busy": False, "commentary_busy": False, "status": "轮到你了",
                      "error": "", "moves": [], "last_move": None, "outcome": None,
                      "legal_moves": ["H8", "I8"], "commentary": [], "winning_line": [],
                      "threat": None, "hint": None, "can_undo": False}
        self.calls = []

    def snapshot(self): return deepcopy(self.state)

    def update(self, **values):
        self.state.update(values)
        self.changed.emit(self.snapshot())

    def play_move(self, move):
        self.calls.append(("move", move))
        self.update(busy=True)

    def ask(self, text):
        self.calls.append(("ask", text))
        self.update(commentary=self.state["commentary"] + [{"role": "user", "text": text}])

    def new_game(self, **values):
        self.calls.append(("new_game", values))
        self.update(board=empty_board(), moves=[], last_move=None, **values)

    def set_rule(self, value):
        self.calls.append(("rule", value))
        self.update(rule=value)

    def set_difficulty(self, value):
        self.calls.append(("difficulty", value))
        self.update(difficulty=value)

    def undo(self): self.calls.append(("undo",))
    def request_hint(self): self.calls.append(("hint",))
    def resign(self): self.calls.append(("resign",))
    def start(self): self.calls.append(("start",))
    def export_record(self, path): self.calls.append(("export", path))


class GomokuUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.theme = ThemeManager(_ThemeSettings())
        self.controller = Controller()
        self.window = GomokuWindow(self.controller)
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.hide()
        self.window.deleteLater()
        self.controller.deleteLater()
        self.theme.dispose()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def append_stone(self, move, piece, *, busy=False):
        rows = deepcopy(self.controller.state["board"])
        row, column = _coordinates(move)
        rows[row][column] = piece
        side = "black" if piece == "X" else "white"
        records = self.controller.state["moves"] + [{"move": move, "label": move, "side": side}]
        self.controller.update(board=rows, moves=records, last_move=move,
                               turn="white" if side == "black" else "black", busy=busy)

    def finish_placement(self):
        board = self.window.board
        animation = board._placement
        if animation.state() == QAbstractAnimation.State.Paused:
            animation.resume()
        animation.setCurrentTime(animation.duration())

    def test_all_intersections_match_click_coordinates_without_side_flipping(self):
        board = self.window.board
        for size in ((850, 650), (1100, 800)):
            self.window.resize(*size)
            self.app.processEvents()
            for side in ("black", "white"):
                self.controller.update(human_side=side)
                for row in range(15):
                    for column in range(15):
                        square = _square(row, column)
                        point = board.point_for_square(square)
                        self.assertEqual(board.square_at(point), square)
                        self.assertTrue(board.rect().contains(point.toPoint()))
                self.assertLess(board.point_for_square("A1").x(), board.point_for_square("O15").x())
                self.assertLess(board.point_for_square("A1").y(), board.point_for_square("O15").y())

    def test_mouse_and_keyboard_submit_only_legal_single_moves(self):
        board = self.window.board
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("A1").toPoint())
        self.assertEqual(self.controller.calls, [])
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("H8").toPoint())
        QTest.mouseDClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("H8").toPoint())
        self.assertEqual([row for row in self.controller.calls if row[0] == "move"], [("move", "H8")])
        self.assertFalse(board._keyboard_focus)
        self.controller.update(busy=False)
        QTest.keyClick(board, Qt.Key.Key_Right)
        self.assertTrue(board._keyboard_focus)
        QTest.keyClick(board, Qt.Key.Key_Return)
        self.assertIn(("move", "I8"), self.controller.calls)

    def test_fast_reply_placements_are_ordered_and_metadata_does_not_restart_them(self):
        board = self.window.board
        self.append_stone("H8", "X", busy=True)
        self.assertTrue(board.animating)
        board._placement.pause()
        board._placement.setCurrentTime(110)
        progress = board._progress
        self.assertGreater(progress, 0)
        self.assertLess(progress, 1)
        self.assertEqual(board._shown_board[7][7], ".")
        self.controller.update(commentary=[{"role": "assistant", "text": "这里有个变化。"}])
        self.assertEqual(board._progress, progress)
        self.append_stone("I8", "O")
        self.assertEqual(board._active[0], "H8")
        self.assertFalse(self.window.hint_button.isEnabled())
        self.finish_placement()
        self.assertEqual(board._active[0], "I8")
        self.assertEqual(board._shown_board[7][7], "X")
        self.assertEqual(board._shown_board[7][8], ".")
        self.finish_placement()
        self.assertFalse(board.animating)
        self.assertEqual(board._shown_board[7][8], "O")
        self.assertTrue(self.window.hint_button.isEnabled())

    def test_undo_new_game_hide_and_restore_do_not_replay_old_placements(self):
        board = self.window.board
        self.append_stone("H8", "X")
        self.controller.update(board=empty_board(), moves=[], last_move=None, turn="black")
        self.assertFalse(board.animating)
        self.assertEqual(board._shown_board[7][7], ".")
        self.append_stone("H8", "X")
        self.window.hide()
        self.assertFalse(board.animating)
        self.assertEqual(board._shown_board[7][7], "X")
        self.window.present()
        self.app.processEvents()
        self.assertFalse(board.animating)
        self.controller.update(human_side="white", rule="standard")
        self.assertFalse(board.animating)
        self.assertEqual(self.window.side_choice.currentData(), "white")

    def test_rules_side_controls_and_question_route_to_the_gomoku_controller(self):
        self.window.question_input.setText("这里可以冲四吗？")
        QTest.keyClick(self.window.question_input, Qt.Key.Key_Return)
        self.assertIn(("ask", "这里可以冲四吗？"), self.controller.calls)
        self.assertEqual(self.window.question_input.text(), "")
        self.window.side_choice.setCurrentIndex(1)
        self.window.difficulty_choice.setCurrentIndex(2)
        self.window.rule_choice.setCurrentIndex(1)
        self.window.new_button.click()
        self.assertIn(("new_game", {"human_side": "white", "difficulty": "serious", "rule": "standard"}), self.controller.calls)
        self.assertEqual(self.window.rule_choice.count(), 2)
        self.controller.update(can_undo=True, engine_ready=True)
        self.window.undo_button.click()
        self.window.hint_button.click()
        with patch.object(self.window, "_confirm", return_value=True):
            self.window.resign_button.click()
        self.assertIn(("undo",), self.controller.calls)
        self.assertIn(("resign",), self.controller.calls)

    def test_chat_follows_long_wrapped_replies_and_preserves_manual_reading_and_drafts(self):
        self.window.resize(850, 650)
        messages = [{"role": "assistant", "text": f"第{index}条："+"这段中文会换行，用来检查最新消息是否完整显示。"*6}
                    for index in range(14)]
        self.controller.update(commentary=messages)
        QTest.qWait(70)
        scrollbar = self.window.chat_scroll.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        self.assertEqual(scrollbar.value(), scrollbar.maximum())
        scrollbar.setValue(0)
        draft = "我还没有发出的五子棋问题"
        self.window.question_input.setText(draft)
        self.controller.update(commentary=messages+[{"role": "assistant", "text": "新增的一段回复"*12}], busy=True)
        self.theme.set_theme("dark", persist=False)
        QTest.qWait(70)
        self.assertEqual(scrollbar.value(), 0)
        self.assertEqual(self.window.question_input.text(), draft)
        self.controller.update(busy=False)
        self.window.send_button.click()
        QTest.qWait(70)
        self.assertEqual(scrollbar.value(), scrollbar.maximum())

    def test_compact_rounded_chrome_keeps_real_controls_reachable_at_both_sizes(self):
        window = self.window
        self.assertTrue(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        labels = [label.text() for label in window.findChildren(QLabel)]
        self.assertFalse(any("执红" in text or "将军" in text for text in labels))
        for size in ((850, 650), (1100, 800)):
            window.resize(*size)
            self.app.processEvents()
            self.assertLessEqual(window.splitter.mapTo(window, QPoint()).y(), 20)
            caption = window.chrome.controls.geometry()
            for widget in (window.board, window.side_choice, window.difficulty_choice, window.rule_choice,
                           window.new_button, window.question_input, window.send_button):
                bounds = QRect(widget.mapTo(window, QPoint()), widget.size())
                self.assertTrue(window.rect().contains(bounds))
                self.assertFalse(caption.intersects(bounds))
            for label in window.character_header.findChildren(QLabel):
                bounds = QRect(label.mapTo(window, QPoint()), label.size())
                self.assertFalse(caption.intersects(bounds))
        draft = "先把这句话留在输入框里"
        window.question_input.setText(draft)
        window.chrome.maximize_button.click()
        self.app.processEvents()
        window.chrome.minimize_button.click()
        window.present()
        self.assertTrue(window.isMaximized())
        window.chrome.close_button.click()
        self.assertFalse(window.isVisible())
        window.present()
        self.assertEqual(window.question_input.text(), draft)

    def test_winning_line_and_both_themes_render_without_solid_corner_pixels(self):
        rows = empty_board()
        winning = ["F8", "G8", "H8", "I8", "J8"]
        for square in winning:
            row, column = _coordinates(square)
            rows[row][column] = "X"
        self.controller.update(board=rows, moves=[], last_move="J8", winning_line=winning,
                               outcome={"winner": "black", "reason": "five"}, status="五子连成一线，你赢了！")
        self.assertEqual(self.window.status_label.property("gameState"), "win")
        self.assertFalse(self.window.hint_button.isEnabled())
        directory = PROJECT_ROOT/"artifacts/gomoku_ui_20261107"
        for theme in ("light", "dark"):
            self.theme.set_theme(theme, persist=False)
            for width, height in ((850, 650), (1100, 800)):
                self.window.resize(width, height)
                self.app.processEvents()
                picture = QImage(self.window.size(), QImage.Format.Format_ARGB32_Premultiplied)
                picture.fill(Qt.GlobalColor.transparent)
                painter = QPainter(picture)
                self.window.render(painter, QPoint())
                painter.end()
                self.assertEqual(picture.pixelColor(0, 0).alpha(), 0)
                self.assertGreater(picture.pixelColor(20, 20).alpha(), 0)
                if os.environ.get("AURA_GOMOKU_UI_CAPTURE") == "1":
                    directory.mkdir(parents=True, exist_ok=True)
                    self.assertTrue(picture.save(str(directory/f"gomoku_{theme}_{width}.png")))

    def test_terminal_status_stays_on_one_line_beside_the_player_color(self):
        statuses = (("五子连成线，这盘我赢啦。", "black", "white"),
                    ("连成五子，这盘你赢了！", "black", "black"),
                    ("棋盘下满啦，这盘和棋。", "draw", "white"),
                    ("这盘先到这里，随时可以再开一局。", "white", "black"))
        for width, height in ((850, 650), (1100, 800)):
            self.window.resize(width, height)
            for status, winner, human in statuses:
                with self.subTest(width=width, status=status):
                    self.controller.update(status=status, human_side=human,
                                           outcome={"winner": winner, "reason": "five"})
                    self.app.processEvents()
                    label = self.window.status_label
                    self.assertFalse(label.wordWrap())
                    self.assertEqual(self.window.width(), width)
                    self.assertGreaterEqual(label.width(), label.sizeHint().width())
                    self.assertLessEqual(label.height(), label.fontMetrics().height()+12)
                    label_bounds = QRect(label.mapTo(self.window, QPoint()), label.size())
                    side_bounds = QRect(self.window.side_label.mapTo(self.window, QPoint()), self.window.side_label.size())
                    self.assertFalse(label_bounds.intersects(side_bounds))
                    self.assertTrue(self.window.rect().contains(side_bounds))


if __name__ == "__main__":
    unittest.main()
