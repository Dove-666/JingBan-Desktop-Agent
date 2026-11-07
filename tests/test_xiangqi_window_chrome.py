"""Synthetic native checks for the chess window's shared rounded chrome."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from desktop_pet.games.xiangqi import XiangqiBoard
from desktop_pet.ui.theme import ThemeManager, color
from desktop_pet.ui.window_chrome import WindowChrome
from desktop_pet.ui.xiangqi_panel import XiangqiWindow
from tests.test_xiangqi_ui import _Controller, _ThemeSettings


class XiangqiWindowChromeTests(unittest.TestCase):
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

    def render_window(self):
        picture = QImage(self.window.size(), QImage.Format.Format_ARGB32_Premultiplied)
        picture.fill(Qt.GlobalColor.transparent)
        painter = QPainter(picture)
        self.window.render(painter, QPoint())
        painter.end()
        return picture

    def seed_match(self):
        board = XiangqiBoard()
        records = []
        for move in ("h2e2", "h9g7"):
            records.append({"move": move, "side": board.turn, "label": board.move_label(move)})
            board.push(move)
        self.controller.update(board=[list(row) for row in board.squares], fen=board.fen,
                               initial_fen=board.initial_fen, moves=records, last_move="h9g7",
                               turn=board.turn, can_undo=True, legal_moves=list(board.legal_moves()))
        self.window.board.stop_motion()
        return board.fen, deepcopy(records)

    def test_same_rounded_surface_has_transparent_corners_in_both_themes(self):
        window = self.window
        self.assertTrue(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(window.property("auraCustomChrome"))
        self.assertIsInstance(window.chrome, WindowChrome)
        self.assertEqual(window.chrome.CORNER_RADIUS, 24)
        self.assertIs(window.chrome.surface, window.pages)
        self.assertFalse(window.pages.mask().contains(QPoint(0, 0)))
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                self.theme.set_theme(theme, persist=False)
                self.app.processEvents()
                picture = self.render_window()
                for point in (QPoint(0, 0), QPoint(picture.width()-1, 0),
                              QPoint(0, picture.height()-1),
                              QPoint(picture.width()-1, picture.height()-1)):
                    self.assertEqual(picture.pixelColor(point).alpha(), 0)
                self.assertEqual(picture.pixelColor(window.width()//2, 4).name(), color("window"))
        labels = [label.text() for label in window.findChildren(QLabel)]
        self.assertNotIn("和鲸鱼娘下象棋", labels)
        self.assertFalse(any(text.startswith("落一子，聊两句") for text in labels))
        self.assertNotIn("皮卡鱼 · 已就绪", labels)

    def test_caption_controls_do_not_cover_board_or_sidebar_at_supported_sizes(self):
        window = self.window
        for width, height in ((850, 650), (1100, 800)):
            with self.subTest(width=width, height=height):
                window.resize(width, height)
                self.app.processEvents()
                caption = QRect(window.chrome.controls.geometry())
                self.assertTrue(window.rect().contains(caption))
                self.assertEqual(caption.size().width(), WindowChrome.CONTROL_WIDTH)
                header_labels = window.character_header.findChildren(QLabel)
                for widget in (window.board, window.status_label, window.side_label, *header_labels, window.side_choice,
                               window.difficulty_choice, window.new_button,
                               window.question_input, window.send_button):
                    bounds = QRect(widget.mapTo(window, QPoint()), widget.size())
                    self.assertFalse(caption.intersects(bounds), widget.objectName())
                    self.assertTrue(window.rect().contains(bounds))
                for button, label in ((window.chrome.minimize_button, "最小化"),
                                      (window.chrome.maximize_button, "最大化"),
                                      (window.chrome.close_button, "关闭窗口")):
                    self.assertTrue(button.isVisible())
                    self.assertEqual(button.accessibleName(), label)
                    center = button.mapTo(window, button.rect().center())
                    self.assertIs(window.childAt(center), button)

    def test_static_headers_route_drag_and_inputs_remain_interactive(self):
        window = self.window
        with patch.object(window.chrome, "start_move", return_value=True) as move, \
                patch.object(window.chrome, "toggle_maximized") as maximize:
            position = window.status_label.rect().center()
            QTest.mousePress(window.status_label, Qt.MouseButton.LeftButton, pos=position)
            QTest.mouseRelease(window.status_label, Qt.MouseButton.LeftButton, pos=position)
            self.assertEqual(move.call_count, 1)
            QTest.mouseDClick(window.status_label, Qt.MouseButton.LeftButton, pos=position)
            self.assertEqual(maximize.call_count, 1)
            for region in (window.side_label, window.character_header):
                move.reset_mock()
                QTest.mousePress(region, Qt.MouseButton.LeftButton, pos=region.rect().center())
                QTest.mouseRelease(region, Qt.MouseButton.LeftButton, pos=region.rect().center())
                self.assertEqual(move.call_count, 1)
            move.reset_mock()
            maximize.reset_mock()
            QTest.mouseClick(window.board, Qt.MouseButton.LeftButton,
                             pos=window.board.point_for_square("e4").toPoint())
            QTest.mouseDClick(window.board, Qt.MouseButton.LeftButton,
                              pos=window.board.point_for_square("e4").toPoint())
            QTest.mouseClick(window.question_input, Qt.MouseButton.LeftButton)
            QTest.mouseDClick(window.question_input, Qt.MouseButton.LeftButton)
            move.assert_not_called()
            maximize.assert_not_called()

    def test_maximize_removes_corner_clip_and_restore_returns_rounded_surface(self):
        window = self.window
        window.chrome.maximize_button.click()
        self.app.processEvents()
        self.assertTrue(window.isMaximized())
        self.assertEqual(window.layout().contentsMargins().left(), 0)
        self.assertTrue(window.pages.mask().isEmpty())
        self.assertFalse(window.chrome.outline.isVisible())
        self.assertEqual(window.chrome.maximize_button.accessibleName(), "还原窗口")
        self.assertEqual(self.render_window().pixelColor(0, 0).alpha(), 255)
        window.chrome.maximize_button.click()
        self.app.processEvents()
        self.assertFalse(window.isMaximized())
        self.assertEqual(window.layout().contentsMargins().left(), WindowChrome.FRAME_MARGIN)
        self.assertFalse(window.pages.mask().isEmpty())
        self.assertTrue(window.chrome.outline.isVisible())
        self.assertEqual(self.render_window().pixelColor(0, 0).alpha(), 0)

    def test_minimize_maximized_restore_and_close_hide_keep_match_and_draft(self):
        window = self.window
        expected_fen, expected_moves = self.seed_match()
        draft = "先留着这个问题，等我想好再发。"
        window.question_input.setText(draft)
        window.chrome.maximize_button.click()
        self.app.processEvents()
        window.chrome.minimize_button.click()
        self.app.processEvents()
        self.assertTrue(window.isMinimized())
        window.present()
        self.app.processEvents()
        self.assertFalse(window.isMinimized())
        self.assertTrue(window.isMaximized())
        window.chrome.close_button.click()
        self.assertFalse(window.isVisible())
        self.assertNotIn(("shutdown",), self.controller.calls)
        window.present()
        self.app.processEvents()
        self.assertTrue(window.isVisible())
        self.assertEqual(window.question_input.text(), draft)
        self.assertEqual(self.controller.snapshot()["fen"], expected_fen)
        self.assertEqual(window._state["moves"], expected_moves)
        self.assertEqual(window.board._state["fen"], expected_fen)


if __name__ == "__main__":
    unittest.main()
