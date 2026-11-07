"""Small logical work areas keep both boards and game controls usable.

All windows are hidden owned Qt fixtures with in-memory controllers. No chess
engine, desktop capture, account or network connection is used.
"""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.gomoku_panel import GomokuWindow
from desktop_pet.ui.theme import ThemeManager
from desktop_pet.ui.window_state import WindowPlacement
from desktop_pet.ui.xiangqi_panel import XiangqiWindow
from tests.test_gomoku_ui import Controller as GomokuController
from tests.test_xiangqi_ui import _Controller as XiangqiController, _ThemeSettings


KINDS = ((XiangqiWindow, XiangqiController), (GomokuWindow, GomokuController))


class _Screen(QObject):
    availableGeometryChanged = Signal(QRect)

    def __init__(self, area):
        super().__init__()
        self.area = area

    def availableGeometry(self):
        return QRect(self.area)


class GameResponsiveLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    @contextmanager
    def window(self, kind, size=(800, 440)):
        Window, Controller = kind
        theme = ThemeManager(_ThemeSettings())
        controller = Controller()
        window = Window(controller)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.resize(*size)
        window.show()
        self.settle()
        try:
            yield window, controller
        finally:
            window.hide()
            window.deleteLater()
            controller.deleteLater()
            theme.dispose()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def settle(self):
        for _ in range(5):
            self.app.processEvents()

    def assert_inside(self, child, parent):
        bounds = QRect(child.mapTo(parent, QPoint()), child.size())
        self.assertTrue(parent.rect().contains(bounds), (child.objectName(), bounds, parent.rect()))

    def test_short_and_normal_windows_keep_board_and_default_composer_visible(self):
        for kind in KINDS:
            with self.subTest(game=kind[0].__name__), self.window(kind) as (window, _):
                for size in ((800, 440), (1024, 520), (1100, 800), (720, 420), (800, 440)):
                    with self.subTest(size=size):
                        window.resize(*size)
                        self.settle()
                        self.assertEqual((window.width(), window.height()), size)
                        self.assert_inside(window.board, window)
                        self.assert_inside(window.board, window.board.parentWidget())
                        self.assert_inside(window.question_input, window.sidebar_scroll.viewport())
                        self.assertEqual(window.sidebar_scroll.verticalScrollBar().maximum(), 0)
                        self.assertEqual(window.sidebar_scroll.horizontalScrollBar().maximum(), 0)

    def test_window_placement_restores_oversize_game_to_short_work_area(self):
        area = QRect(-800, 30, 800, 440)
        screen = _Screen(area)
        for kind in KINDS:
            with self.subTest(game=kind[0].__name__), self.window(kind, (1100, 800)) as (window, _), \
                 tempfile.TemporaryDirectory(prefix="aura-game-responsive-") as directory:
                window.move(-650, 100)
                with patch.object(QGuiApplication, "screens", return_value=[screen]):
                    placement = WindowPlacement(window, Path(directory) / "settings.ini", "game")
                    self.settle()
                    self.assertTrue(area.contains(window.frameGeometry()))
                    self.assertEqual((window.width(), window.height()), (800, 440))
                    self.assert_inside(window.board, window)
                    self.assert_inside(window.question_input, window.sidebar_scroll.viewport())
                    placement.save()

    def test_all_intersections_remain_reversible_and_inside_at_short_size(self):
        for kind in KINDS:
            with self.subTest(game=kind[0].__name__), self.window(kind) as (window, controller):
                if isinstance(window, GomokuWindow):
                    squares = [f"{column}{row}" for column in "ABCDEFGHIJKLMNO" for row in range(1, 16)]
                    sides = ("black", "white")
                else:
                    squares = [f"{column}{row}" for column in "abcdefghi" for row in range(10)]
                    sides = ("red", "black")
                for side in sides:
                    controller.update(human_side=side)
                    for square in squares:
                        point = window.board.point_for_square(square)
                        self.assertTrue(window.board.rect().contains(point.toPoint()))
                        self.assertEqual(window.board.square_at(point), square)

    def test_lowest_board_intersection_accepts_a_real_pointer_move(self):
        for kind in KINDS:
            with self.subTest(game=kind[0].__name__), self.window(kind) as (window, controller):
                board = window.board
                if isinstance(window, GomokuWindow):
                    controller.update(legal_moves=["O15"])
                    QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("O15").toPoint())
                    expected = "O15"
                else:
                    QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("a0").toPoint())
                    QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("a1").toPoint())
                    expected = "a0a1"
                self.assertIn(("move", expected), controller.calls)

    def test_resizing_preserves_draft_and_manual_chat_reading(self):
        for kind in KINDS:
            with self.subTest(game=kind[0].__name__), self.window(kind, (1100, 800)) as (window, controller):
                messages = [{"role": "assistant", "text": f"第{number}条棋局说明，" * 20} for number in range(18)]
                controller.update(commentary=messages)
                QTest.qWait(100)
                scrollbar = window.chat_scroll.verticalScrollBar()
                self.assertGreater(scrollbar.maximum(), 0)
                scrollbar.setValue(scrollbar.maximum() // 3)
                self.assertFalse(window._chat_follow_latest)
                window.question_input.setText("暂时保留这句问题")
                for size in ((800, 440), (1024, 520), (1100, 800)):
                    window.resize(*size)
                    QTest.qWait(60)
                    self.assertEqual(window.question_input.text(), "暂时保留这句问题")
                    self.assertFalse(window._chat_follow_latest)
                    self.assertLess(scrollbar.value(), scrollbar.maximum())
                self.assertEqual(controller.calls, [])

    def test_long_error_controls_and_composer_are_reachable_by_scrolling(self):
        for kind in KINDS:
            with self.subTest(game=kind[0].__name__), self.window(kind) as (window, controller):
                window.question_input.setText("请帮我分析")
                controller.update(engine_ready=False, error="棋力暂时无法连接，请稍后重试。" * 16)
                self.settle()
                scroll = window.sidebar_scroll
                self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
                for child in (window.retry_button, window.question_input, window.send_button):
                    scroll.ensureWidgetVisible(child, 8, 8)
                    self.settle()
                    self.assert_inside(child, scroll.viewport())
                QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
                self.assertIn(("ask", "请帮我分析"), controller.calls)
                self.assertEqual(window.question_input.text(), "")
                self.assert_inside(window.board, window)

    def test_500_messages_keep_reading_and_draft_while_resizing_then_follow_when_requested(self):
        for kind in KINDS:
            with self.subTest(game=kind[0].__name__), self.window(kind, (1100, 800)) as (window, controller):
                messages = [{"role": "user" if number % 2 else "assistant",
                             "text": f"第{number}条分析：先观察棋形，再判断落点。" * 3}
                            for number in range(500)]
                controller.update(commentary=messages)
                QTest.qWait(100)
                # Creating hundreds of wrapped rows can consume this whole
                # timer interval before their queued layout passes run.
                self.settle()
                scrollbar = window.chat_scroll.verticalScrollBar()
                self.assertGreater(scrollbar.maximum(), 0)
                scrollbar.setValue(scrollbar.maximum() // 2)
                window.question_input.setText("我还没发送的问题")
                for size in ((800, 440), (1024, 520), (1100, 800)):
                    window.resize(*size)
                    QTest.qWait(60)
                    self.assertFalse(window._chat_follow_latest)
                    self.assertLess(scrollbar.value(), scrollbar.maximum())
                    self.assertEqual(window.question_input.text(), "我还没发送的问题")
                messages.append({"role": "assistant", "text": "这条新消息不能把你从前文拉走。"})
                controller.update(commentary=messages)
                QTest.qWait(100)
                self.assertFalse(window._chat_follow_latest)
                self.assertLess(scrollbar.value(), scrollbar.maximum())
                scrollbar.setValue(scrollbar.maximum())
                self.assertTrue(window._chat_follow_latest)
                messages.append({"role": "assistant", "text": "回到底部后继续跟随。"})
                controller.update(commentary=messages)
                QTest.qWait(100)
                self.assertEqual(scrollbar.value(), scrollbar.maximum())
                self.assertEqual(len(controller.state["commentary"]), 502)
                self.assertEqual(controller.calls, [])


if __name__ == "__main__":
    unittest.main()
