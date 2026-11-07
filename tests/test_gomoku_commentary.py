"""Game-only prompts and transport keep Gomoku separate from Xiangqi and chat."""
import threading
import unittest

from PySide6.QtCore import QObject, QEvent, QThreadPool, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.games.gomoku_commentary import GomokuCommentary, game_context
from tests.mock_api import MockApiServer
from tests.test_xiangqi_commentary import Settings, wait_for as spin


class Game(QObject):
    changed = Signal(dict)
    commentary_requested = Signal(str, dict)

    def __init__(self):
        super().__init__()
        self.state = {"board": [["."]*15 for _ in range(15)], "position_key": "empty-freestyle",
                      "turn": "black", "human_side": "black", "rule": "freestyle", "difficulty": "casual",
                      "moves": [], "revision": 0, "commentary": []}
        self.replies, self.busy = [], False

    def snapshot(self):
        return dict(self.state)

    def set_commentary_busy(self, value):
        self.busy = value
        self.changed.emit(self.snapshot())

    def post_commentary(self, text, emotion="neutral"):
        self.replies.append((text, emotion))


class GomokuCommentaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.game = Game()
        self.service = None

    def tearDown(self):
        if self.service is not None:
            self.service.shutdown()
        spin(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        if self.service is not None:
            self.service.deleteLater()
        self.game.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_question_uses_gomoku_facts_and_no_private_configuration(self):
        with MockApiServer() as server:
            server.enqueue('{"reply":"你执黑先手，我执白。想从中间开始吗？","emotion":"happy"}')
            self.service = GomokuCommentary(Settings(server.base_url), self.game)
            self.assertTrue(self.service.request("我们各执哪一方？", self.game.snapshot()))
            spin(lambda: not self.game.busy)
            self.assertEqual(self.game.replies[-1], ("你执黑先手，我执白。想从中间开始吗？", "happy"))
            messages = server.requests[-1]["body"]["messages"]
            self.assertIn("五子棋", messages[0]["content"])
            self.assertIn("黑方先手", messages[0]["content"])
            self.assertIn("empty-freestyle", messages[1]["content"])
            context = game_context({**self.game.snapshot(), "api_key": "private-fixture", "main_chat": ["private"]})
            self.assertNotIn("api_key", context)
            self.assertNotIn("main_chat", context)

    def test_selector_rejects_outside_pool_and_allows_an_intentional_quiet_move(self):
        with MockApiServer() as server:
            self.service = GomokuCommentary(Settings(server.base_url), self.game)
            selector = self.service.move_selector(self.game.snapshot())
            choices = [{"move": "H8", "score_cp": 0}, {"move": "I8", "score_cp": -10}]
            server.enqueue('{"move":"Q40","reply":"我随便下这里。","emotion":"happy"}')
            self.assertIsNone(selector(choices, cancellation_event=threading.Event()))
            server.enqueue('{"move":"I8","reply":"","emotion":"neutral"}')
            response = selector(choices, cancellation_event=threading.Event())
            self.assertEqual(response["move"], "I8")
            self.assertEqual(response["reply"], "")

    def test_rule_or_position_change_cancels_a_stale_reply_even_without_revision_change(self):
        for changed in ({"position_key": "another-position"}, {"rule": "standard"}):
            with self.subTest(change=changed):
                entered = threading.Event()

                class Client:
                    def complete_context(self, _settings, _key, _messages, **kwargs):
                        from desktop_pet.api_client import ChatReply
                        entered.set()
                        kwargs["cancellation_event"].wait(3)
                        return ChatReply("这是旧局面的回答。", "neutral")

                    def cancel_request(self, event):
                        event.set()

                    def shutdown(self):
                        pass

                self.service = GomokuCommentary(Settings(), self.game, api_client=Client())
                self.assertTrue(self.service.request("这一步怎么走？", self.game.snapshot()))
                spin(entered.is_set)
                self.game.state.update(changed)
                self.game.changed.emit(self.game.snapshot())
                spin(lambda: not self.game.busy)
                self.assertEqual(self.game.replies, [])
                self.service.shutdown()
                self.service.deleteLater()
                self.service = None
