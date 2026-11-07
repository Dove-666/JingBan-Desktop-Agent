"""Xiangqi is reachable from the real app without replacing chat or drafts."""
from pathlib import Path
import tempfile
import time
import unittest

from PySide6.QtCore import QEvent, QThreadPool, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from tests.test_application import isolated_settings, MemoryCredentialBackend


def spin(predicate, timeout=12):
    deadline = time.monotonic()+timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
        time.sleep(.002)
    if not predicate():
        raise AssertionError("Local game did not settle")


class XiangqiApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-xiangqi-app-")
        settings = isolated_settings(Path(self.temp.name)/"settings.ini", MemoryCredentialBackend())
        self.session = ApplicationController(PetConfig(), settings=settings)
        self.session.pet.refresh_timer.stop()
        self.session.pet.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)

    def tearDown(self):
        self.session.shutdown()
        spin(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for panel in (self.session.xiangqi_window, self.session.chat_window, self.session.pet):
            if panel:
                panel.deleteLater()
        self.session.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def test_pet_tray_and_web_entry_reuse_one_game_and_keep_chat(self):
        self.assertIsNone(self.session.xiangqi)
        self.assertTrue(hasattr(self.session.tray, "xiangqi_action"))
        self.session.pet.xiangqi_requested.emit()
        game, window = self.session.xiangqi, self.session.xiangqi_window
        spin(lambda: game.engine_ready and not game.busy)
        self.assertEqual(len(game.snapshot()["legal_moves"]), 44)
        window.close()
        self.assertFalse(window.isVisible())
        self.session.tray.xiangqi_action.trigger()
        self.assertIs(self.session.xiangqi_window, window)
        self.assertTrue(window.isVisible())
        chat = self.session._ensure_chat_window().chat_panel
        self.assertTrue(chat.web_state()["canXiangqi"])
        self.assertTrue(chat.dispatch_web_action("openXiangqi", {}))
        self.assertIs(self.session.xiangqi, game)
        self.assertIsNotNone(self.session.chat_window)

    def test_real_engine_move_hint_and_undo_leave_the_match_usable(self):
        self.session.open_xiangqi_window()
        game = self.session.xiangqi
        spin(lambda: game.engine_ready and not game.busy)
        self.assertTrue(game.play_move("h2e2"))
        spin(lambda: len(game.board.moves) == 2 and not game.busy)
        self.assertEqual(game.board.turn, "red")
        self.assertTrue(game.request_hint())
        spin(lambda: not game.busy)
        self.assertIsNotNone(game.snapshot()["hint"])
        self.assertTrue(game.undo())
        self.assertEqual(game.board.moves, [])
        self.assertEqual(len(game.snapshot()["legal_moves"]), 44)
