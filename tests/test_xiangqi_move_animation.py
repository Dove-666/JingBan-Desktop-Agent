"""Observe native painted move frames against real controller positions."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.games.pikafish import EngineError, resolve_engine_path
from desktop_pet.games.xiangqi import XiangqiBoard
from desktop_pet.games.xiangqi_controller import XiangqiController
from desktop_pet.ui.xiangqi_panel import XiangqiWindow


class XiangqiMoveAnimationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        try:
            resolve_engine_path()
        except EngineError:
            raise unittest.SkipTest("Official engine package is not installed")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-xiangqi-move-")
        self.storage = Path(self.temporary.name) / "match.json"
        self.game = None
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.hide()
        if self.game is not None:
            self.game.shutdown()
            self.wait(lambda: not self.game.engine.running and not self.game.runner._tasks)
        if self.window is not None:
            self.window.deleteLater()
        if self.game is not None:
            self.game.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def wait(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(5)
            time.sleep(.002)
        self.assertTrue(predicate(), "The isolated engine or visual queue did not settle")

    def open_match(self, saved_board=None, human_side="red"):
        if saved_board is not None:
            self.storage.write_text(json.dumps({
                "schema_version": 1, "board": saved_board.to_payload(),
                "human_side": human_side, "difficulty": "casual",
                "commentary": [], "result": None,
            }), encoding="utf-8")
        self.game = XiangqiController(storage_path=self.storage)
        self.window = XiangqiWindow(self.game)
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.window.show()
        self.app.processEvents()
        self.game.start()
        self.wait(lambda: self.game.engine_ready and not self.game.busy)
        self.game.set_difficulty("casual")
        self.board = self.window.board

    def pause_at(self, milliseconds):
        animation = self.board._move_animation
        self.assertTrue(self.board.animating)
        if animation.state() == QAbstractAnimation.State.Running:
            animation.pause()
        animation.setCurrentTime(milliseconds)

    def finish_active_move(self):
        animation = self.board._move_animation
        self.assertTrue(self.board.animating)
        if animation.state() == QAbstractAnimation.State.Paused:
            animation.resume()
        animation.setCurrentTime(animation.duration())
        if self.board.animating:
            animation.pause()

    def painted_pieces(self):
        """Capture actual paint arguments while retaining the real painter."""
        pieces = []
        paint_piece = self.board._paint_piece

        def record(painter, piece, point, *arguments):
            pieces.append((piece, QPointF(point)))
            paint_piece(painter, piece, point, *arguments)

        image = QImage(self.board.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            with patch.object(self.board, "_paint_piece", new=record):
                self.board.render(painter, QPoint())
        finally:
            painter.end()
        self.assertGreater(image.pixelColor(image.width()//2, image.height()//2).alpha(), 0)
        return pieces

    def has_piece_at(self, pieces, piece, point):
        return any(value == piece and (position-point).manhattanLength() < .05
                   for value, position in pieces)

    def test_intermediate_frame_is_painted_between_source_and_destination(self):
        self.open_match()
        self.assertTrue(self.game.play_move("h2e2"))
        self.pause_at(250)
        source = self.board.point_for_square("h2")
        destination = self.board.point_for_square("e2")
        expected = (source+destination)*.5
        pieces = self.painted_pieces()
        self.assertTrue(self.has_piece_at(pieces, "C", expected))
        self.assertFalse(self.has_piece_at(pieces, "C", source))
        self.assertFalse(self.has_piece_at(pieces, "C", destination))
        self.assertEqual(self.game.board.squares[7][4], "C")
        self.assertEqual(self.board._motion.active.before[7][7], "C")

    def test_fast_engine_reply_and_commentary_preserve_first_clock_and_order(self):
        self.open_match()
        self.assertTrue(self.game.play_move("h2e2"))
        self.pause_at(250)
        first = self.board._motion.active
        self.game.post_commentary("我正在想下一步。", "thinking")
        self.wait(lambda: len(self.game.board.moves) == 2 and not self.game.busy)
        self.assertIs(self.board._motion.active, first)
        self.assertEqual(self.board._move_animation.currentTime(), 250)
        self.assertEqual(self.board._motion.pending_count, 2)
        self.assertFalse(self.window.hint_button.isEnabled())
        emitted = []
        self.board.move_requested.connect(emitted.append)
        QTest.mouseClick(self.board, Qt.MouseButton.LeftButton,
                         pos=self.board.point_for_square("a0").toPoint())
        QTest.mouseClick(self.board, Qt.MouseButton.LeftButton,
                         pos=self.board.point_for_square("a1").toPoint())
        self.assertEqual(emitted, [])
        self.assertEqual(len(self.game.board.moves), 2)
        self.finish_active_move()
        self.assertEqual(self.board._motion.active.move, self.game.board.moves[1])
        self.assertEqual(self.board._motion.last_move, "h2e2")
        self.pause_at(250)
        active = self.board._motion.active
        expected = (self.board.point_for_square(active.source)+self.board.point_for_square(active.target))*.5
        self.assertTrue(self.has_piece_at(self.painted_pieces(), active.piece, expected))
        self.finish_active_move()
        self.assertFalse(self.board.animating)
        self.assertEqual(self.board._motion.board, self.game.board.squares)
        self.assertTrue(self.window.hint_button.isEnabled())
        self.assertTrue(self.board._can_play())

    def test_capture_is_still_drawn_until_the_moving_rook_lands(self):
        saved = XiangqiBoard("4k4/9/9/9/4p4/4R4/9/9/9/3K5 w - - 0 1")
        self.open_match(saved)
        self.assertFalse(self.board.animating)
        self.assertTrue(self.game.play_move("e4e5"))
        self.pause_at(250)
        captured_point = self.board.point_for_square("e5")
        midpoint = (self.board.point_for_square("e4")+captured_point)*.5
        pieces = self.painted_pieces()
        self.assertTrue(self.has_piece_at(pieces, "p", captured_point))
        self.assertTrue(self.has_piece_at(pieces, "R", midpoint))
        self.finish_active_move()
        pieces = self.painted_pieces()
        self.assertFalse(self.has_piece_at(pieces, "p", captured_point))
        self.assertTrue(self.has_piece_at(pieces, "R", captured_point))

    def test_undo_and_new_game_discard_inflight_moves_and_late_engine_reply(self):
        self.open_match()
        self.assertTrue(self.game.play_move("h2e2"))
        self.pause_at(250)
        self.assertTrue(self.game.undo())
        self.assertFalse(self.board.animating)
        self.assertEqual(self.board._motion.pending_count, 0)
        self.assertEqual(self.board._motion.board, self.game.board.squares)
        self.wait(lambda: not self.game.runner._tasks)
        self.assertEqual(self.game.board.moves, [])
        self.assertTrue(self.game.play_move("h2e2"))
        self.pause_at(250)
        self.assertTrue(self.game.new_game("red", "casual"))
        self.assertFalse(self.board.animating)
        self.wait(lambda: not self.game.runner._tasks)
        self.assertEqual(self.game.board.moves, [])
        self.assertEqual(self.board._motion.board, self.game.board.squares)
        self.assertEqual(self.board._move_animation.state(), QAbstractAnimation.State.Stopped)

    def test_hide_and_minimize_snap_to_latest_board_and_reopen_without_replay(self):
        self.open_match()
        for action in ("hide", "minimize"):
            with self.subTest(action=action):
                self.assertTrue(self.game.new_game("red", "casual"))
                self.assertTrue(self.game.play_move("h2e2"))
                self.pause_at(250)
                self.window.hide() if action == "hide" else self.window.showMinimized()
                self.app.processEvents()
                self.assertFalse(self.board.animating)
                self.wait(lambda: len(self.game.board.moves) == 2 and not self.game.busy)
                self.assertFalse(self.board.animating)
                self.assertEqual(self.board._motion.board, self.game.board.squares)
                self.window.showNormal()
                self.app.processEvents()
                self.assertFalse(self.board.animating)
                self.assertEqual(self.board._motion.pending_count, 0)

    def test_resize_keeps_interpolated_square_and_black_orientation_matches_board(self):
        self.open_match()
        self.assertTrue(self.game.play_move("h2e2"))
        self.pause_at(250)
        self.window.resize(850, 650)
        self.app.processEvents()
        expected = (self.board.point_for_square("h2")+self.board.point_for_square("e2"))*.5
        self.assertTrue(self.has_piece_at(self.painted_pieces(), "C", expected))
        self.assertEqual(self.board._move_animation.currentTime(), 250)
        self.assertTrue(self.game.new_game("black", "casual"))
        self.wait(lambda: len(self.game.board.moves) == 1 and not self.game.busy)
        self.assertTrue(self.board.flipped)
        self.pause_at(250)
        active = self.board._motion.active
        self.assertIsNotNone(active)
        expected = (self.board.point_for_square(active.source)+self.board.point_for_square(active.target))*.5
        self.assertTrue(self.has_piece_at(self.painted_pieces(), active.piece, expected))

    def test_restored_saved_moves_are_visible_without_replaying_old_history(self):
        saved = XiangqiBoard()
        saved.push("h2e2")
        saved.push("h9g7")
        self.open_match(saved)
        self.assertFalse(self.board.animating)
        self.assertEqual(self.board._motion.pending_count, 0)
        self.assertEqual(self.board._motion.board, saved.squares)
        self.assertTrue(self.window.hint_button.isEnabled())

    def test_terminal_move_is_still_painted_and_finished(self):
        saved = XiangqiBoard("4k4/3R1P3/4P4/9/9/9/9/9/9/3K5 w - - 0 1")
        self.open_match(saved)
        self.assertFalse(self.board.animating)
        self.assertTrue(self.game.play_move("e7e8"))
        self.assertEqual(self.game.snapshot()["outcome"], {"winner": "red", "reason": "checkmate"})
        self.pause_at(250)
        expected = (self.board.point_for_square("e7")+self.board.point_for_square("e8"))*.5
        self.assertTrue(self.has_piece_at(self.painted_pieces(), "P", expected))
        self.assertFalse(self.window.hint_button.isEnabled())
        self.finish_active_move()
        self.assertFalse(self.board.animating)
        self.assertEqual(self.board._motion.board, self.game.board.squares)
        self.assertFalse(self.window.hint_button.isEnabled())
        self.assertFalse(self.board._can_play())


if __name__ == "__main__":
    unittest.main()
