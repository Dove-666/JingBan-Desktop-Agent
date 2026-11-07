"""Native board interaction, resizing and draft-preserving state updates."""
from copy import deepcopy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.config import PROJECT_ROOT
from desktop_pet.ui.theme import ThemeManager
from desktop_pet.ui.xiangqi_panel import XiangqiBoardWidget, XiangqiWindow, _coordinates, _square


class _ThemeSettings:
    def load_theme(self):
        return "light"

    def save_theme(self, _theme):
        pass


class _Controller(QObject):
    changed = Signal(dict)

    def __init__(self):
        super().__init__()
        rows = ["rnbakabnr", ".........", ".c.....c.", "p.p.p.p.p", ".........",
                ".........", "P.P.P.P.P", ".C.....C.", ".........", "RNBAKABNR"]
        self.state = {"board": [list(row) for row in rows], "turn": "red", "human_side": "red",
                      "difficulty": "standard", "busy": False, "status": "轮到你了",
                      "error": "", "engine_ready": True, "engine_label": "皮卡鱼",
                      "last_move": None, "legal_moves": ["h2e2", "h2h5", "a0a1"], "moves": [],
                      "outcome": None, "commentary": [{"role": "assistant", "text": "来下一局吧，轮到你先手。"}],
                      "hint": None, "can_undo": False}
        self.calls = []

    def snapshot(self):
        return deepcopy(self.state)

    def update(self, **values):
        self.state.update(values)
        self.changed.emit(self.snapshot())

    def play_move(self, move):
        self.calls.append(("move", move))
        self.update(busy=True)

    def new_game(self, **values):
        self.calls.append(("new_game", values))
        self.update(**values)

    def set_difficulty(self, value):
        self.calls.append(("difficulty", value))
        self.update(difficulty=value)

    def ask(self, value):
        self.calls.append(("ask", value))
        self.update(commentary=self.state["commentary"] + [{"role": "user", "text": value}])

    def undo(self): self.calls.append(("undo",))
    def request_hint(self): self.calls.append(("hint",))
    def resign(self): self.calls.append(("resign",))
    def start(self): self.calls.append(("start",))
    def shutdown(self): self.calls.append(("shutdown",))


class XiangqiUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.theme = ThemeManager(_ThemeSettings())
        self.controller = _Controller()
        self.window = XiangqiWindow(self.controller)
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

    def test_all_ninety_squares_have_reversible_clicked_coordinates_on_both_sides(self):
        for size in ((850, 650), (1100, 800)):
            self.window.resize(*size)
            self.app.processEvents()
            for side in ("red", "black"):
                self.controller.update(human_side=side)
                for row in range(10):
                    for column in range(9):
                        square = _square(row, column)
                        self.assertEqual(_coordinates(square), (row, column))
                        point = self.window.board.point_for_square(square)
                        self.assertTrue(self.window.board.rect().contains(point.toPoint()))
                        self.assertEqual(self.window.board.square_at(point), square)
                source = self.window.board.point_for_square("a0")
                destination = self.window.board.point_for_square("i9")
                self.assertEqual(source.x() < destination.x(), side == "red")
                self.assertEqual(source.y() > destination.y(), side == "red")
                self.assertEqual(self.window.board.file_labels(0), ("1", "九") if side == "red" else ("一", "9"))
                self.assertEqual(self.window.board.file_labels(8), ("9", "一") if side == "red" else ("九", "1"))

    def test_select_then_destination_submits_exactly_one_move_and_double_click_cannot_repeat(self):
        board = self.window.board
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("h2").toPoint())
        self.assertEqual(board._selected, "h2")
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("e2").toPoint())
        QTest.mouseDClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("e2").toPoint())
        self.assertEqual([c for c in self.controller.calls if c[0] == "move"], [("move", "h2e2")])

    def test_black_orientation_emits_original_iccs_coordinates_and_wrong_turn_blocks_input(self):
        board = self.window.board
        self.controller.update(human_side="black", turn="black", legal_moves=["h7e7"])
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("h7").toPoint())
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("e7").toPoint())
        self.assertIn(("move", "h7e7"), self.controller.calls)
        before = len(self.controller.calls)
        self.controller.update(human_side="red", turn="black", busy=False)
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("h7").toPoint())
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("e7").toPoint())
        self.assertEqual(len(self.controller.calls), before)

    def test_state_changes_theme_changes_and_new_commentary_preserve_question_draft(self):
        draft = "我想先问问这一步，还没有发送。"
        self.window.question_input.setText(draft)
        self.controller.update(busy=True, can_undo=True, status="鲸鱼娘正在思考…")
        self.assertFalse(self.window.hint_button.isEnabled())
        self.assertFalse(self.window.difficulty_choice.isEnabled())
        self.assertTrue(self.window.undo_button.isEnabled())
        self.theme.set_theme("dark", persist=False)
        self.controller.update(commentary=self.controller.state["commentary"] +
                               [{"role": "assistant", "text": "你可以考虑先出子。"}])
        self.app.processEvents()
        self.assertEqual(self.window.question_input.text(), draft)
        self.assertEqual(self.window.board.property("auraTheme"), None)
        self.assertIn("#151c26", self.window.styleSheet())
        self.theme.set_theme("light", persist=False)
        self.assertEqual(self.window.question_input.text(), draft)

    def test_question_enter_and_game_controls_call_the_controller(self):
        self.window.question_input.setText("这步炮怎么走？")
        QTest.keyClick(self.window.question_input, Qt.Key.Key_Return)
        self.assertIn(("ask", "这步炮怎么走？"), self.controller.calls)
        self.assertEqual(self.window.question_input.text(), "")
        self.window.side_choice.setCurrentIndex(1)
        self.window.difficulty_choice.setCurrentIndex(2)
        QTest.mouseClick(self.window.new_button, Qt.MouseButton.LeftButton)
        self.assertIn(("new_game", {"human_side": "black", "difficulty": "serious"}), self.controller.calls)
        self.controller.update(can_undo=True)
        QTest.mouseClick(self.window.undo_button, Qt.MouseButton.LeftButton)
        self.assertIn(("undo",), self.controller.calls)
        with patch.object(self.window, "_confirm", return_value=True):
            QTest.mouseClick(self.window.resign_button, Qt.MouseButton.LeftButton)
        self.assertIn(("resign",), self.controller.calls)

    def test_close_hides_the_window_without_shutting_down_the_game(self):
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertNotIn(("shutdown",), self.controller.calls)
        self.window.present()
        self.assertTrue(self.window.isVisible())

    def test_render_both_themes_and_sizes_without_clipping_controls_or_board(self):
        output = PROJECT_ROOT / "artifacts/xiangqi_integration_20261104"
        for name in ("light", "dark"):
            self.theme.set_theme(name, persist=False)
            for width, height in ((850, 650), (1100, 800)):
                with self.subTest(theme=name, width=width, height=height):
                    self.window.resize(width, height)
                    self.app.processEvents()
                    self.assertEqual(self.window.size().width(), width)
                    for widget in (self.window.board, self.window.side_choice, self.window.new_button,
                                   self.window.question_input, self.window.send_button):
                        self.assertTrue(self.window.rect().contains(widget.mapTo(self.window, QPoint())))
                        self.assertTrue(self.window.rect().contains(widget.mapTo(self.window, widget.rect().bottomRight())))
                    image = QImage(self.window.size(), QImage.Format.Format_ARGB32_Premultiplied)
                    image.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(image)
                    self.window.render(painter, QPoint())
                    painter.end()
                    self.assertGreater(image.pixelColor(10, 10).alpha(), 0)
                    if os.environ.get("AURA_XIANGQI_UI_CAPTURE") == "1":
                        output.mkdir(parents=True, exist_ok=True)
                        self.assertTrue(image.save(str(output / f"xiangqi_{name}_{width}.png"), "PNG"))


if __name__ == "__main__":
    unittest.main()
