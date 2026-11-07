"""Visible check alerts follow landed moves, with compact native controls."""
from __future__ import annotations

from unittest.mock import patch
import unittest
import warnings

from PySide6.QtCore import QAbstractAnimation, QPoint, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.games.xiangqi import XiangqiBoard
from tests import test_xiangqi_ui as fixture


class XiangqiCheckUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    setUp = fixture.XiangqiUiTests.setUp
    tearDown = fixture.XiangqiUiTests.tearDown

    def match_states(self):
        board = XiangqiBoard("4k4/R8/9/9/4p4/9/9/9/9/4K4 w - - 0 1")
        states, records = [], []
        for move in (None, "a8e8", "e9f9"):
            if move:
                records.append({"move": move, "label": board.move_label(move), "side": board.turn})
                board.push(move)
            states.append({"board": [list(row) for row in board.squares], "fen": board.fen,
                           "initial_fen": board.initial_fen, "revision": len(records),
                           "moves": [dict(row) for row in records], "last_move": move,
                           "turn": board.turn, "human_side": "red", "engine_ready": True,
                           "busy": False, "outcome": None, "legal_moves": list(board.legal_moves()),
                           "checked_side": board.turn if board.in_check() else None})
        self.assertIsNone(states[0]["checked_side"])
        self.assertEqual(states[1]["checked_side"], "black")
        self.assertIsNone(states[2]["checked_side"])
        return states

    def render_board(self):
        board = self.window.board
        image = QImage(board.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            board.render(painter, QPoint())
        finally:
            painter.end()
        return image

    def finish_move(self):
        animation = self.window.board._move_animation
        if animation.state() == QAbstractAnimation.State.Paused:
            animation.resume()
        animation.setCurrentTime(animation.duration())
        if self.window.board.animating:
            animation.pause()

    def test_checked_king_and_badge_remain_when_hint_or_difficulty_status_replaces_text(self):
        checked = self.match_states()[1]
        self.window.hide()
        self.controller.update(**checked, status="已切换为轻松陪玩。")
        self.window.present()
        self.app.processEvents()
        self.assertEqual(self.window.board.displayed_checked_side, "black")
        self.assertTrue(self.window.status_label.property("checkWarning"))
        self.assertIn("将军", self.window.status_label.text())
        self.controller.update(status="可以考虑这一步，棋盘上已经标出来了。", hint="e9f9")
        self.assertTrue(self.window.status_label.property("checkWarning"))
        painted = []
        original = self.window.board._paint_piece

        def record(painter, piece, point, cell, dark, is_checked=False):
            if is_checked:
                painted.append(piece)
            return original(painter, piece, point, cell, dark, is_checked)

        with patch.object(self.window.board, "_paint_piece", new=record):
            self.render_board()
        self.assertEqual(painted, ["k"])

    def test_queued_check_is_announced_on_landing_before_fast_ai_escape_finishes(self):
        before, checked, escaped = self.match_states()
        self.controller.update(**before)
        self.controller.update(**checked)
        board = self.window.board
        self.assertTrue(board.animating)
        board._move_animation.pause()
        board._move_animation.setCurrentTime(250)
        self.controller.update(**escaped)
        self.assertEqual(board._motion.pending_count, 2)
        self.assertIsNone(board.displayed_checked_side)
        self.finish_move()
        self.assertTrue(board.animating)
        self.assertEqual(board._motion.active.move, "e9f9")
        self.assertEqual(board.displayed_checked_side, "black")
        self.assertTrue(self.window.status_label.property("checkWarning"))
        self.assertLess(board._check_progress, 1.0)
        self.finish_move()
        self.assertFalse(board.animating)
        self.assertIsNone(board.displayed_checked_side)
        self.assertFalse(self.window.status_label.property("checkWarning"))
        self.assertEqual(board._check_progress, 1.0)

    def test_restored_checked_position_has_persistent_alert_without_replaying_notice(self):
        self.window.hide()
        self.controller.update(**self.match_states()[1], status="上次的棋局已恢复。")
        self.window.present()
        self.app.processEvents()
        board = self.window.board
        self.assertFalse(board.animating)
        self.assertEqual(board.displayed_checked_side, "black")
        self.assertEqual(board._check_progress, 1.0)
        self.assertEqual(board._check_animation.state(), QAbstractAnimation.State.Stopped)
        self.assertTrue(self.window.status_label.property("checkWarning"))

    def test_mouse_focus_is_quiet_and_keyboard_navigation_uses_a_circular_cue(self):
        board = self.window.board
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            QApplication.setActiveWindow(self.window)
        board.setFocus(Qt.FocusReason.OtherFocusReason)
        self.app.processEvents()
        self.assertTrue(board.hasFocus())
        baseline = self.render_board()
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("e4").toPoint())
        self.assertIsNone(board._selected)
        self.assertEqual(self.render_board(), baseline)
        QTest.keyClick(board, Qt.Key.Key_Right)
        keyboard = self.render_board()
        point = board.point_for_square("f4")
        _, cell = board._geometry()
        radius = cell*.46
        edge = QPoint(round(point.x()-radius), round(point.y()))
        corner = QPoint(round(point.x()-radius), round(point.y()-radius))
        self.assertTrue(any(keyboard.pixelColor(edge+QPoint(dx, dy)) != baseline.pixelColor(edge+QPoint(dx, dy))
                            for dx in range(-3, 4) for dy in range(-3, 4)))
        self.assertTrue(all(keyboard.pixelColor(corner+QPoint(dx, dy)) == baseline.pixelColor(corner+QPoint(dx, dy))
                            for dx in range(-2, 3) for dy in range(-2, 3)))
        QTest.mouseClick(board, Qt.MouseButton.LeftButton, pos=board.point_for_square("e4").toPoint())
        self.assertEqual(self.render_board(), baseline)

    def test_board_card_starts_near_top_at_both_supported_sizes(self):
        for width, height in ((850, 650), (1100, 800)):
            with self.subTest(width=width):
                self.window.resize(width, height)
                self.app.processEvents()
                card = self.window.board.parentWidget()
                self.assertLessEqual(card.mapTo(self.window, QPoint()).y(), 20)
                self.assertGreaterEqual(self.window.board.height(), 420)


if __name__ == "__main__":
    unittest.main()
