"""Character snapshots over synthetic visual/game data and in-memory clients."""
from dataclasses import replace
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtGui import QColor, QImage

from desktop_pet.api_client import ApiError, ChatReply
from desktop_pet.character_cards import CharacterCard
from desktop_pet.character_prompts import CHARACTER_EXPRESSION_POLICY, character_identity_prompt
from desktop_pet.games.xiangqi_commentary import GAME_PROMPT, MOVE_PROMPT, XiangqiCommentary
from desktop_pet.perception.context_analyzer import (
    CAMERA_CONTEXT_PROMPT, COMMENTARY_PROMPTS, CONTEXT_PROMPT, REQUESTED_COMMENT_PROMPT,
    ContextAnalyzer, ContextEvent,
)
from desktop_pet.perception.controller import PerceptionController
from desktop_pet.perception.screen_observer import ForegroundInfo, Observation
from desktop_pet.perception.settings import CameraSettings, PerceptionSettings
from desktop_pet.settings_manager import ApiSettings


FIRST_CARD = CharacterCard("星璃", personality="温柔沉稳，称呼用户为旅伴")
NEXT_CARD = CharacterCard("阿岚", personality="爽朗活泼，称呼用户为朋友")


def synthetic_observation(source="screen"):
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(QColor("#486b8a"))
    return Observation(ForegroundInfo(1, 2, "owned-fixture.exe", "Owned synthetic scene", 1, True),
                       image, source=source)


class CapturingClient:
    def __init__(self):
        self.requests = []
        self.move_reply = '{"move":"h2e2","reply":"我先走这一步。","emotion":"happy"}'

    def request_text(self, settings, key, messages, **kwargs):
        self.requests.append((messages, kwargs, threading.get_ident()))
        if isinstance(messages[-1]["content"], list):
            return '{"kind":"none","reply":"","confidence":0,"importance":0}'
        return self.move_reply

    def complete_context(self, settings, key, messages, **kwargs):
        self.requests.append((messages, kwargs, threading.get_ident()))
        return ChatReply("受控角色讲解", "happy")

    def shutdown(self):
        pass


class SnapshotSettings:
    def __init__(self, card):
        self.card = card
        self.loads = []

    def load_character_card(self):
        self.loads.append(threading.get_ident())
        return self.card

    def load_api_settings(self):
        return ApiSettings("http://127.0.0.1:1/v1", "owned-no-model", 5)

    def get_api_key(self):
        return "owned-synthetic-never-sent-key"

    def load_perception_settings(self):
        return PerceptionSettings()

    def load_camera_settings(self):
        return CameraSettings()

    def save_perception_settings(self, settings):
        raise AssertionError("Character snapshot checks must not persist visual settings")

    def save_camera_settings(self, settings):
        raise AssertionError("Character snapshot checks must not persist camera settings")


class QuietChat(QObject):
    busy_changed = Signal(bool)
    busy = False
    current_conversation_id = "owned-synthetic-conversation"


class HoldingPool:
    def start(self, worker):
        self.worker = worker


class HoldingRunner:
    def __init__(self, *args):
        self.work = self.done = None

    def submit(self, work, done):
        self.work, self.done = work, done
        return "owned-fake-task"

    def shutdown(self):
        pass


class Game(QObject):
    changed = Signal(dict)
    commentary_requested = Signal(str, dict)

    def __init__(self):
        super().__init__()
        self.state = {"board": [], "fen": "owned-fixture-fen", "turn": "red", "human_side": "red",
                      "revision": 0, "moves": [], "commentary": []}
        self.replies = []

    def snapshot(self):
        return dict(self.state)

    def set_commentary_busy(self, busy):
        self.changed.emit(self.snapshot())

    def post_commentary(self, text, emotion):
        self.replies.append((text, emotion))


class CharacterCommentaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def tearDown(self):
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def run_in_thread(self, function):
        results, errors = [], []

        def run():
            try:
                results.append(function())
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run, name="owned-character-commentary-test")
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        return results[0]

    def test_default_visual_prompt_keeps_all_existing_rules(self):
        for source in ("screen", "camera"):
            for mode in ("companion", "quiet"):
                for requested in (False, True):
                    with self.subTest(source=source, mode=mode, requested=requested):
                        client = CapturingClient()
                        ContextAnalyzer(client).analyze(synthetic_observation(source), ApiSettings(), "owned-key",
                            commentary_mode=mode, requested_comment=requested)
                        expected = CAMERA_CONTEXT_PROMPT if source == "camera" else CONTEXT_PROMPT
                        if source == "camera":
                            expected += ("当前为陪伴评论模式，可对清楚可见的普通日常场景给出简短评论，频率由本地限额控制。"
                                         if mode == "companion" else "当前为安静模式，仅重要事件提供评论。")
                            if requested:
                                expected += "本次是用户主动请求评论摄像头预览，可评论普通场景，仍需遵守可见性和隐私限制。"
                        else:
                            expected += COMMENTARY_PROMPTS[mode]
                            if requested:
                                expected += REQUESTED_COMMENT_PROMPT
                        self.assertEqual(client.requests[0][0][0]["content"], expected)

    def test_custom_visual_prompt_changes_identity_and_retains_facts_and_privacy(self):
        for source in ("screen", "camera"):
            with self.subTest(source=source):
                baseline_client, custom_client = CapturingClient(), CapturingClient()
                observation = synthetic_observation(source)
                for client, card in ((baseline_client, None), (custom_client, FIRST_CARD)):
                    ContextAnalyzer(client).analyze(observation, ApiSettings(), "owned-key",
                        requested_comment=True, character_card=card)
                base = baseline_client.requests[0][0]
                custom = custom_client.requests[0][0]
                self.assertEqual(custom[0]["content"], character_identity_prompt(FIRST_CARD)
                    + CHARACTER_EXPRESSION_POLICY + base[0]["content"].replace("鲸鱼娘", "当前角色"))
                self.assertEqual(custom[1], base[1])
                self.assertIn("express", custom[0]["content"])
                self.assertIn("不可信", custom[0]["content"])

    def test_invalid_character_or_blocked_scene_cannot_reach_transport(self):
        client = CapturingClient()
        analyzer = ContextAnalyzer(client)
        with self.assertRaises(ApiError) as caught:
            analyzer.analyze(synthetic_observation(), ApiSettings(), "owned-key", character_card={})
        self.assertEqual(caught.exception.code, "configuration")
        blocked = replace(synthetic_observation(), blocked_reason="Owned privacy gate")
        with self.assertRaises(ApiError) as caught:
            analyzer.analyze(blocked, ApiSettings(), "owned-key", character_card=FIRST_CARD)
        self.assertEqual(caught.exception.code, "privacy")
        self.assertEqual(client.requests, [])

    def test_visual_controller_captures_card_once_before_worker_runs(self):
        for source in ("screen", "camera"):
            with self.subTest(source=source):
                settings, pool = SnapshotSettings(FIRST_CARD), HoldingPool()
                received = []

                class Analyzer:
                    def analyze(self, observation, api, key, **options):
                        received.append((options["character_card"], threading.get_ident()))
                        return ContextEvent()

                    def shutdown(self):
                        pass

                controller = PerceptionController(settings, SimpleNamespace(), QuietChat(),
                    observer=SimpleNamespace(), analyzer_factory=Analyzer, source_kind=source,
                    settings_loader=settings.load_camera_settings if source == "camera" else None,
                    settings_saver=settings.save_camera_settings if source == "camera" else None)
                try:
                    with patch("desktop_pet.perception.controller.QThreadPool.globalInstance", return_value=pool):
                        self.assertTrue(controller._start_analysis(synthetic_observation(source), "owned-app", "owned-frame"))
                    settings.card = NEXT_CARD
                    self.run_in_thread(pool.worker.run)
                    self.assertEqual(settings.loads, [threading.get_ident()])
                    self.assertIs(received[0][0], FIRST_CARD)
                    self.assertNotEqual(received[0][1], settings.loads[0])
                finally:
                    controller.shutdown()
                    controller.deleteLater()

    def test_default_visual_analyzer_keeps_legacy_keyword_signature(self):
        saved = SnapshotSettings(None)
        settings = SimpleNamespace(load_api_settings=saved.load_api_settings,
                                   get_api_key=saved.get_api_key,
                                   load_perception_settings=saved.load_perception_settings,
                                   save_perception_settings=saved.save_perception_settings)
        pool, calls = HoldingPool(), []

        class LegacyAnalyzer:
            def analyze(self, observation, api, key, *, commentary_mode, requested_comment):
                calls.append((commentary_mode, requested_comment))
                return ContextEvent()

            def shutdown(self):
                pass

        controller = PerceptionController(settings, SimpleNamespace(), QuietChat(), observer=SimpleNamespace(),
                                          analyzer_factory=LegacyAnalyzer)
        try:
            with patch("desktop_pet.perception.controller.QThreadPool.globalInstance", return_value=pool):
                self.assertTrue(controller._start_analysis(synthetic_observation(), "owned-app", "owned-frame"))
            self.run_in_thread(pool.worker.run)
            self.assertEqual(calls, [("companion", False)])
        finally:
            controller.shutdown()
            controller.deleteLater()

    def game_service(self, settings, client):
        with patch("desktop_pet.games.xiangqi_commentary.ApiClient", return_value=client), \
                patch("desktop_pet.games.xiangqi_commentary.TaskRunner", HoldingRunner):
            service = XiangqiCommentary(settings, Game(), api_client=client)
        self.addCleanup(self.close_game_service, service)
        return service

    def close_game_service(self, service):
        service.shutdown()
        service.controller.deleteLater()
        service.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_game_chat_uses_main_thread_snapshot_and_keeps_game_rules(self):
        for card in (None, FIRST_CARD):
            with self.subTest(card=card):
                settings, client = SnapshotSettings(card), CapturingClient()
                service = self.game_service(settings, client)
                self.assertTrue(service.request("讲解这一步", service.controller.snapshot()))
                settings.card = NEXT_CARD
                reply = self.run_in_thread(service.runner.work)
                service.runner.done(reply, None)
                expected = (GAME_PROMPT if card is None else character_identity_prompt(card)
                            + CHARACTER_EXPRESSION_POLICY + GAME_PROMPT.replace("鲸鱼娘", "当前角色"))
                self.assertEqual(client.requests[0][0][0]["content"], expected)
                self.assertEqual(settings.loads, [threading.get_ident()])
                self.assertNotEqual(client.requests[0][2], settings.loads[0])
                self.assertEqual(service.controller.replies, [("受控角色讲解", "happy")])

    def test_game_move_selector_keeps_captured_role_and_candidate_validation(self):
        for card in (None, FIRST_CARD):
            with self.subTest(card=card):
                settings, client = SnapshotSettings(card), CapturingClient()
                service = self.game_service(settings, client)
                selector = service.move_selector(service.controller.snapshot())
                settings.card = NEXT_CARD
                move = self.run_in_thread(lambda: selector([{"move": "h2e2"}], cancellation_event=threading.Event()))
                self.assertEqual(move["move"], "h2e2")
                expected = (MOVE_PROMPT if card is None else character_identity_prompt(card)
                            + CHARACTER_EXPRESSION_POLICY + MOVE_PROMPT.replace("鲸鱼娘", "当前角色"))
                self.assertEqual(client.requests[0][0][0]["content"], expected)
                self.assertEqual(settings.loads, [threading.get_ident()])
                client.move_reply = '{"move":"a0a9","reply":"禁止凭空走子"}'
                self.assertIsNone(selector([{"move": "h2e2"}], cancellation_event=threading.Event()))
                cancelled = threading.Event()
                cancelled.set()
                count = len(client.requests)
                self.assertIsNone(selector([{"move": "h2e2"}], cancellation_event=cancelled))
                self.assertEqual(len(client.requests), count)

    def test_game_accepts_legacy_settings_without_a_character_loader(self):
        saved, client = SnapshotSettings(None), CapturingClient()
        settings = SimpleNamespace(load_api_settings=saved.load_api_settings, get_api_key=saved.get_api_key)
        service = self.game_service(settings, client)
        selector = service.move_selector(service.controller.snapshot())
        selected = selector([{"move": "h2e2"}], cancellation_event=threading.Event())
        self.assertEqual(selected["move"], "h2e2")
        self.assertEqual(client.requests[0][0][0]["content"], MOVE_PROMPT)
        self.assertTrue(service.request("讲解这一步", service.controller.snapshot()))
        reply = service.runner.work()
        service.runner.done(reply, None)
        self.assertEqual(client.requests[-1][0][0]["content"], GAME_PROMPT)
        self.assertEqual(saved.loads, [])


if __name__ == "__main__":
    unittest.main()
