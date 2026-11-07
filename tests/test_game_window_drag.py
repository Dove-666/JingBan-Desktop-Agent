"""Game-window dragging through real Qt child hit targets and window positions.

The native move/resize boundary is controlled. All windows are hidden owned
fixtures, with in-memory game controllers and no engine, account or clipboard.
"""
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWindow
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.gomoku_panel import GomokuWindow
from desktop_pet.ui.theme import ThemeManager
from desktop_pet.ui.xiangqi_panel import XiangqiWindow
from tests.test_gomoku_ui import Controller as GomokuController
from tests.test_xiangqi_ui import _Controller as XiangqiController, _ThemeSettings


KINDS = ((XiangqiWindow, XiangqiController), (GomokuWindow, GomokuController))
DELTA = QPoint(37, 23)


class GameWindowDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    @contextmanager
    def window(self, kind):
        Window, Controller = kind
        theme = ThemeManager(_ThemeSettings())
        controller = Controller()
        window = Window(controller)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.resize(960, 700)
        window.move(150, 140)
        window.show()
        self.app.processEvents()
        try:
            yield window
        finally:
            window.hide()
            window.deleteLater()
            controller.deleteLater()
            theme.dispose()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    @staticmethod
    def event(target, kind, global_position, *, button=Qt.MouseButton.NoButton,
              buttons=Qt.MouseButton.NoButton):
        local = target.mapFromGlobal(global_position)
        value = QMouseEvent(kind, QPointF(local), QPointF(global_position), button,
                            buttons, Qt.KeyboardModifier.NoModifier)
        QCoreApplication.sendEvent(target, value)

    def press(self, window, point):
        target = window.childAt(point) or window
        position = window.mapToGlobal(point)
        self.event(target, QEvent.Type.MouseButtonPress, position,
                   button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
        return target, position

    def move(self, target, position, *, held=True):
        self.event(target, QEvent.Type.MouseMove, position,
                   buttons=Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton)

    def release(self, target, position):
        self.event(target, QEvent.Type.MouseButtonRelease, position,
                   button=Qt.MouseButton.LeftButton)
        self.app.processEvents()

    @staticmethod
    def status_point(window):
        return window.status_label.mapTo(window, window.status_label.rect().center())

    def test_top_blank_and_status_row_gap_reach_native_move(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                right = window.status_label.mapTo(window, QPoint(window.status_label.width(), 0)).x()
                left = window.side_label.mapTo(window, QPoint()).x()
                self.assertGreater(left, right)
                y = self.status_point(window).y()
                for area, point in (("top", QPoint(window.width() // 2, 12)),
                                    ("state-row-gap", QPoint((right + left) // 2, y))):
                    with self.subTest(area=area), patch.object(QWindow, "startSystemMove", return_value=True) as native:
                        target, position = self.press(window, point)
                        self.release(target, position)
                        self.assertEqual(native.call_count, 1)

    def test_rejected_native_move_really_moves_both_windows(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                origin = window.pos()
                with patch.object(QWindow, "startSystemMove", return_value=False) as native:
                    target, position = self.press(window, self.status_point(window))
                    self.move(target, position + DELTA)
                    self.release(target, position + DELTA)
                self.assertEqual(native.call_count, 1)
                self.assertEqual(window.pos() - origin, DELTA)

    def test_top_band_fallback_continues_after_pointer_leaves_band(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                origin, delta = window.pos(), QPoint(47, 111)
                with patch.object(QWindow, "startSystemMove", return_value=False):
                    target, position = self.press(window, QPoint(window.width() // 2, 12))
                    self.move(window.pages, position + delta)
                    self.release(target, position + delta)
                self.assertEqual(window.pos() - origin, delta)

    def test_release_prevents_later_stale_held_move(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                with patch.object(QWindow, "startSystemMove", return_value=False):
                    target, position = self.press(window, self.status_point(window))
                    self.move(target, position + DELTA)
                    self.release(target, position + DELTA)
                    stopped = window.pos()
                    self.move(target, position + DELTA * 2)
                self.assertEqual(window.pos(), stopped)

    def test_missing_left_button_cancels_fallback_without_resuming(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                origin = window.pos()
                with patch.object(QWindow, "startSystemMove", return_value=False):
                    target, position = self.press(window, self.status_point(window))
                    self.move(target, position + DELTA, held=False)
                    self.move(target, position + DELTA * 2)
                    self.release(target, position + DELTA * 2)
                self.assertEqual(window.pos(), origin)

    def test_hide_and_deactivate_cancel_pending_drag(self):
        for kind in KINDS:
            for reason in ("hide", "deactivate"):
                with self.subTest(window=kind[0].__name__, reason=reason), self.window(kind) as window:
                    origin = window.pos()
                    with patch.object(QWindow, "startSystemMove", return_value=False):
                        target, position = self.press(window, self.status_point(window))
                        if reason == "hide":
                            window.hide()
                        else:
                            QCoreApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
                        self.move(target, position + DELTA)
                        self.release(target, position + DELTA)
                    self.assertEqual(window.pos(), origin)

    def test_actual_board_input_buttons_and_page_bottom_do_not_drag(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                window.question_input.setText("Keep this question draft")
                points = [
                    window.question_input.mapTo(window, window.question_input.rect().center()),
                    window.new_button.mapTo(window, window.new_button.rect().center()),
                    window.chrome.close_button.mapTo(window, window.chrome.close_button.rect().center()),
                    window.pages.mapTo(window, QPoint(window.pages.width() // 2, window.pages.height() - 2)),
                    window.board.mapTo(window, window.board.point_for_square(
                        "e4" if kind[0] is XiangqiWindow else "H8").toPoint()),
                ]
                origin = window.pos()
                with patch.object(QWindow, "startSystemMove", return_value=False) as native, \
                        patch.object(window, "_confirm", return_value=False):
                    for point in points:
                        target, position = self.press(window, point)
                        self.move(target, position + DELTA)
                        self.release(target, position + DELTA)
                        self.assertEqual(window.pos(), origin)
                    native.assert_not_called()
                self.assertEqual(window.question_input.text(), "Keep this question draft")

    def test_native_success_does_not_apply_manual_delta(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                origin = window.pos()
                with patch.object(QWindow, "startSystemMove", return_value=True) as native, \
                        patch.object(window, "move", wraps=window.move) as manual:
                    target, position = self.press(window, self.status_point(window))
                    self.move(target, position + DELTA)
                    self.release(target, position + DELTA)
                    native.assert_called_once()
                    manual.assert_not_called()
                self.assertEqual(window.pos(), origin)

    def test_double_click_header_still_maximizes_and_restores(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                size = window.size()
                for maximized in (True, False):
                    point = self.status_point(window)
                    target = window.childAt(point) or window
                    self.event(target, QEvent.Type.MouseButtonDblClick, window.mapToGlobal(point),
                               button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
                    self.app.processEvents()
                    self.assertEqual(window.isMaximized(), maximized)
                self.assertEqual(window.size(), size)

    def test_maximized_fallback_restores_size_and_keeps_following_delta(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                normal_size = window.size()
                window.showMaximized()
                self.app.processEvents()
                with patch.object(QWindow, "startSystemMove", return_value=False):
                    target, position = self.press(window, self.status_point(window))
                    self.move(target, position + DELTA)
                    self.app.processEvents()
                    self.assertFalse(window.isMaximized())
                    self.assertEqual(window.size(), normal_size)
                    anchor = window.mapFromGlobal(position + DELTA)
                    self.assertGreaterEqual(anchor.x(), 0)
                    self.assertLess(anchor.x(), window.width())
                    self.assertGreaterEqual(anchor.y(), 0)
                    self.assertLess(anchor.y(), 120)
                    restored = window.pos()
                    continuation = QPoint(19, 13)
                    self.move(target, position + DELTA + continuation)
                    self.release(target, position + DELTA + continuation)
                self.assertEqual(window.pos() - restored, continuation)

    def test_fullscreen_header_never_starts_move(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                window.showFullScreen()
                self.app.processEvents()
                origin = window.pos()
                with patch.object(QWindow, "startSystemMove", return_value=False) as native:
                    target, position = self.press(window, self.status_point(window))
                    self.move(target, position + DELTA)
                    self.release(target, position + DELTA)
                    native.assert_not_called()
                self.assertTrue(window.isFullScreen())
                self.assertEqual(window.pos(), origin)

    def test_resize_border_takes_priority_over_top_drag_region(self):
        for kind in KINDS:
            with self.subTest(window=kind[0].__name__), self.window(kind) as window:
                origin = window.pos()
                with patch.object(QWindow, "startSystemResize", return_value=True) as resize, \
                        patch.object(QWindow, "startSystemMove", return_value=False) as native:
                    target, position = self.press(window, QPoint(window.width() // 2, 2))
                    self.move(target, position + DELTA)
                    self.release(target, position + DELTA)
                    resize.assert_called_once()
                    native.assert_not_called()
                self.assertEqual(window.pos(), origin)


if __name__ == "__main__":
    unittest.main()
