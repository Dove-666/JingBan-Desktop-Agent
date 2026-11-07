"""Real local HTTP commentary, independent cancellation and stale-board guards."""
import json
import tempfile
import threading
from pathlib import Path
import time
import unittest

from PySide6.QtCore import QObject, QEvent, QSettings, QThreadPool, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ChatReply
from desktop_pet.games.xiangqi import XiangqiBoard
from desktop_pet.games.xiangqi_commentary import XiangqiCommentary, game_context
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer


def wait_for(predicate, timeout=8):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline and not predicate():
        QTest.qWait(5)
        time.sleep(.002)
    if not predicate():
        raise AssertionError("Commentary did not complete")


class Game(QObject):
    changed = Signal(dict)
    commentary_requested = Signal(str, dict)

    def __init__(self):
        super().__init__()
        board = XiangqiBoard()
        self.state = {"board": board.squares, "fen": board.fen, "turn": "red", "human_side": "red",
                      "difficulty": "standard", "moves": [], "revision": 0, "commentary": []}
        self.replies = []
        self.busy = False

    def snapshot(self):
        return dict(self.state)

    def set_commentary_busy(self, busy):
        self.busy = busy
        self.changed.emit(self.snapshot())

    def post_commentary(self, text, emotion="neutral"):
        self.replies.append((text, emotion))


class Settings:
    def __init__(self, url="http://127.0.0.1:9/v1", key="local-game-test-key"):
        self.url, self.key = url, key

    def load_api_settings(self):
        return ApiSettings(base_url=self.url, model="local-test-model", timeout_seconds=4)

    def get_api_key(self):
        return self.key


class XiangqiCommentaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.game = Game()
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.shutdown()
        wait_for(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for service in self.services:
            service.deleteLater()
        self.game.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def service(self, settings, client=None):
        service = XiangqiCommentary(settings, self.game, api_client=client)
        self.services.append(service)
        return service

    def test_real_local_http_receives_game_facts_and_emotion_without_routing(self):
        with MockApiServer() as server:
            server.enqueue('{"reply":"你执红先行，我执黑陪你下。","emotion":"happy"}')
            service = self.service(Settings(server.base_url))
            self.assertTrue(service.request("我们各执哪一方？", self.game.snapshot()))
            wait_for(lambda: not self.game.busy)
            self.assertEqual(self.game.replies[-1], ("你执红先行，我执黑陪你下。", "happy"))
            self.assertEqual(self.game.state["moves"], [])

    def test_game_revision_change_cancels_and_cannot_publish_old_position(self):
        started, cancelled = threading.Event(), threading.Event()

        class Client:
            def complete_context(self, _settings, _key, _messages, **kwargs):
                started.set()
                kwargs["cancellation_event"].wait(2)
                return ChatReply("旧棋盘分析不应出现", "angry")
            def cancel_request(self, event):
                cancelled.set()
                event.set()
            def shutdown(self):
                pass

        service = self.service(Settings(), Client())
        service.request("分析这一步", self.game.snapshot())
        wait_for(started.is_set)
        self.game.state["revision"] = 1
        self.game.changed.emit(self.game.snapshot())
        wait_for(lambda: not self.game.busy)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.game.replies, [])

    def test_no_key_preserves_local_game_and_does_not_autospam(self):
        service = self.service(Settings(key=""))
        self.assertFalse(service.request("", self.game.snapshot()))
        self.assertEqual(self.game.replies, [])
        self.assertFalse(service.request("这步怎么走", self.game.snapshot()))
        self.assertIn("本地对弈", self.game.replies[-1][0])
        self.assertEqual(self.game.state["moves"], [])

    def test_restore_changes_fen_even_if_a_producer_reuses_revision(self):
        started = threading.Event()

        class Client:
            def complete_context(self, _settings, _key, _messages, **kwargs):
                started.set()
                kwargs["cancellation_event"].wait(2)
                return ChatReply("初始棋盘的旧讲解", "neutral")
            def cancel_request(self, event): event.set()
            def shutdown(self): pass

        service = self.service(Settings(), Client())
        service.request("分析当前棋局", self.game.snapshot())
        wait_for(started.is_set)
        restored = XiangqiBoard()
        restored.push("h2e2")
        self.game.state["fen"] = restored.fen
        self.game.changed.emit(self.game.snapshot())
        wait_for(lambda: not self.game.busy)
        self.assertEqual(self.game.replies, [])

    def test_context_excludes_private_fields_and_bounds_analysis(self):
        state = self.game.snapshot()
        state.update(api_key="not-for-the-game", config_path="private.ini", analysis="x"*14000)
        context = game_context(state)
        self.assertNotIn("api_key", context)
        self.assertNotIn("config_path", context)
        self.assertIsNone(context["analysis"])

    def test_personality_selector_only_accepts_engine_candidates(self):
        service = self.service(Settings())

        class MoveClient:
            def __init__(self):
                self.answer = '{"move":"h2e2","reply":"我先把炮摆过来。","emotion":"happy"}'
            def request_text(self, settings, key, messages, **kwargs):
                self.messages = messages
                return self.answer
            def shutdown(self):
                pass

        service.move_client.shutdown()
        client = service.move_client = MoveClient()
        selector = service.move_selector(self.game.snapshot())
        chosen = selector([{"move": "h2e2", "score_cp": 30}], cancellation_event=threading.Event())
        self.assertEqual(chosen["move"], "h2e2")
        self.assertEqual(chosen["emotion"], "happy")
        client.answer = '{"move":"a0a9","reply":"候选以外的走法不能执行"}'
        self.assertIsNone(selector([{"move": "h2e2"}], cancellation_event=threading.Event()))

    def test_personality_selector_cancellation_never_requests_a_move(self):
        service = self.service(Settings())
        event = threading.Event()
        event.set()
        selector = service.move_selector(self.game.snapshot())
        self.assertIsNone(selector([{"move": "h2e2"}], cancellation_event=event))

    def test_selector_has_recent_conversation_and_exact_move_facts_and_can_stay_quiet(self):
        with MockApiServer() as server:
            server.enqueue('{"move":"h7e7","reply":"","emotion":"thinking"}')
            self.game.state.update(checked_side="black", last_move_details={
                "move": "h2e2", "label": "炮二平五", "side": "red", "piece": "C", "captured": ".",
                "check": False, "private_field": "do-not-send"}, commentary=[
                {"role": "assistant", "text": f"旧对话{i}"} for i in range(10)])
            self.game.state["commentary"][-1] = {"role": "user", "text": "这一步就想吃我的车？"}
            service = self.service(Settings(server.base_url))
            selector = service.move_selector(self.game.snapshot())
            # Settings and dialogue are captured before a worker starts.
            self.game.state["commentary"][-1]["text"] = "后来改动的内容"
            selected = selector([{"move": "h7e7", "score_cp": 20}], cancellation_event=threading.Event())
            self.assertEqual(selected, {"move": "h7e7", "reply": "", "emotion": "thinking"})
            payload = json.loads(server.requests[0]["body"]["messages"][-1]["content"])
            self.assertEqual(len(payload["recent_dialogue"]), 8)
            self.assertEqual(payload["recent_dialogue"][0]["content"], "旧对话2")
            self.assertEqual(payload["recent_dialogue"][-1]["content"], "这一步就想吃我的车？")
            self.assertEqual(payload["position"]["checked_side"], "black")
            self.assertEqual(payload["position"]["last_move_details"]["captured"], ".")
            self.assertNotIn("private_field", payload["position"]["last_move_details"])

    def test_selector_suppresses_repeated_reaction_without_losing_selected_move(self):
        with MockApiServer() as server:
            self.game.state["commentary"] = [{"role": "assistant", "text": "这边的马可要小心啦。"}]
            server.enqueue('{"move":"h7e7","reply":"这边的马可要小心啦～","emotion":"happy"}')
            service = self.service(Settings(server.base_url))
            selector = service.move_selector(self.game.snapshot())
            selected = selector([{"move": "h7e7"}], cancellation_event=threading.Event())
            self.assertEqual(selected["move"], "h7e7")
            self.assertEqual(selected["reply"], "")
            server.enqueue('{"move":"h7e7","reply":"你的炮已经过河了，看来这边要热闹起来。","emotion":"surprised"}')
            selected = selector([{"move": "h7e7"}], cancellation_event=threading.Event())
            self.assertEqual(selected["reply"], "你的炮已经过河了，看来这边要热闹起来。")

    def test_automatic_reactions_allow_silence_and_omit_duplicate_messages(self):
        with MockApiServer() as server:
            service = self.service(Settings(server.base_url))
            self.game.state["commentary"] = [{"role": "assistant", "text": "你这步马跳得挺凶。"}]
            for response in ('{"reply":"","emotion":"neutral"}',
                             '{"reply":"你这步马跳得挺凶～","emotion":"surprised"}'):
                server.enqueue(response)
                self.assertTrue(service.request("", self.game.snapshot()))
                wait_for(lambda: not self.game.busy)
                self.assertEqual(self.game.replies, [])
            server.enqueue('{"reply":"这边的兵也跟上来了。","emotion":"thinking"}')
            self.assertTrue(service.request("", self.game.snapshot()))
            wait_for(lambda: not self.game.busy)
            self.assertEqual(self.game.replies, [("这边的兵也跟上来了。", "thinking")])

    def test_direct_question_keeps_explicit_answer_even_if_it_matches_recent_dialogue(self):
        with MockApiServer() as server:
            answer = "你执红，我执黑。"
            self.game.state["commentary"] = [{"role": "assistant", "text": answer}]
            server.enqueue(json.dumps({"reply": answer, "emotion": "neutral"}, ensure_ascii=False))
            service = self.service(Settings(server.base_url))
            self.assertTrue(service.request("再说一次我们分别执哪一方？", self.game.snapshot()))
            wait_for(lambda: not self.game.busy)
            self.assertEqual(self.game.replies, [(answer, "neutral")])
