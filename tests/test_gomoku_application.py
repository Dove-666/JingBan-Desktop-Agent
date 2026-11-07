"""The shipped desktop entry points open one playable, independent Gomoku match."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool, Qt
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.ui.gomoku_panel import GomokuWindow
from tests.test_application import isolated_settings, MemoryCredentialBackend
from tests.test_xiangqi_application import spin


class GomokuApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-gomoku-entry-")
        settings = isolated_settings(Path(self.temp.name)/"settings.ini", MemoryCredentialBackend())
        self.session = ApplicationController(PetConfig(), settings=settings)
        self.session.pet.refresh_timer.stop()
        self.session.pet.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        present = GomokuWindow.present

        def hidden_present(window):
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            present(window)

        self.present_patch = patch.object(GomokuWindow, "present", new=hidden_present)
        self.present_patch.start()

    def tearDown(self):
        self.session.shutdown()
        spin(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.present_patch.stop()
        for panel in (self.session.gomoku_window, self.session.chat_window, self.session.pet):
            if panel is not None:
                panel.deleteLater()
        self.session.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def test_pet_tray_and_chat_route_to_one_game_without_starting_xiangqi(self):
        self.assertIsNone(self.session.gomoku)
        self.session.pet.gomoku_requested.emit()
        game, window = self.session.gomoku, self.session.gomoku_window
        spin(lambda: game.engine_ready and not game.busy)
        self.assertEqual(len(game.snapshot()["legal_moves"]), 225)
        window.close()
        self.assertFalse(window.isVisible())
        self.session.tray.gomoku_action.trigger()
        self.assertIs(self.session.gomoku_window, window)
        self.assertTrue(window.isVisible())
        chat = self.session._ensure_chat_window().chat_panel
        self.assertTrue(chat.web_state()["canGomoku"])
        self.assertTrue(chat.dispatch_web_action("openGomoku", {}))
        self.assertIs(self.session.gomoku, game)
        self.assertIsNone(self.session.xiangqi)

    def test_real_engine_reply_hint_undo_and_white_first_turn(self):
        self.session.open_gomoku_window()
        game = self.session.gomoku
        spin(lambda: game.engine_ready and not game.busy)
        self.assertTrue(game.play_move("H8"))
        spin(lambda: len(game.board.moves) == 2 and not game.busy)
        self.assertEqual(game.board.turn, "black")
        self.assertTrue(game.request_hint())
        spin(lambda: not game.busy)
        self.assertTrue(game.board.is_legal(game.snapshot()["hint"]))
        self.assertTrue(game.undo())
        self.assertEqual(game.board.moves, [])
        self.assertTrue(game.new_game(human_side="white", difficulty="casual", rule="standard"))
        spin(lambda: len(game.board.moves) == 1 and not game.busy)
        self.assertEqual(game.board.turn, "white")
        self.assertEqual(game.rule, "standard")
        self.assertIsNone(self.session.xiangqi)
