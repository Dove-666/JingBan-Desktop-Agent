"""R3 companion/manual commentary with real Qt and a loopback SDK request.

Only synthetic images and a temporary settings backend are used. These tests
never capture the real desktop or read the user's credential store.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from PySide6.QtCore import QEvent, QThreadPool, QTimer
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.chat_controller import ChatController
from desktop_pet.config import PetConfig
from desktop_pet.perception.context_analyzer import parse_context
from desktop_pet.perception.controller import PerceptionController
from desktop_pet.perception.proactive_agent import Decision
from desktop_pet.perception.settings import PerceptionSettings
from desktop_pet.pet_controller import PetController
from desktop_pet.pet_window import PetWindow
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer
from tests.test_application import (
    LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until,
)
from tests.test_perception_integration import AdjustableClock, SyntheticScene, event_json


def media_event(**overrides):
    event = json.loads(event_json(
        kind="media", importance=0.1,
        reply="这个合成画面的蓝色看起来很清爽。",
    ))
    event.update(overrides)
    return json.dumps(event, ensure_ascii=False)


class CommentaryFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-commentary-tests-")
        self.path = Path(self.temp.name) / "settings.ini"
        self.settings = isolated_settings(self.path, MemoryCredentialBackend())
        self.pet = PetWindow(PetConfig())
        self.character = PetController(self.pet, self.settings, animation_directory=self.path.parent / "empty-animations")
        self.chat = ChatController(self.settings)
        self.scene = SyntheticScene()
        self.source_info = self.scene.info
        self.clock = AdjustableClock()
        self.controller = PerceptionController(
            self.settings, self.character, self.chat,
            observer=self.scene.observer(), clock=self.clock,
        )
        self.pet.show()
        self.app.processEvents()

    def tearDown(self):
        self.controller.shutdown()
        self.chat.shutdown()
        self.pet.close()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for obj in (self.controller, self.chat, self.character, self.pet):
            obj.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def configure(self, server=None, **overrides):
        if server is not None:
            self.settings.save_api_settings(ApiSettings(server.base_url, "test-model", 5))
            self.settings.save_api_key(LOCAL_TEST_KEY)
        values = {"enabled": True, "allow_remote": server is not None}
        values.update(overrides)
        self.assertTrue(self.controller.update_settings(PerceptionSettings(**values)))
        # Tests drive observations explicitly; the production cadence is tested
        # independently in the perception integration suite.
        self.controller._timer.stop()

    def prepare_preview(self, server, **settings):
        self.configure(server, **settings)
        self.assertTrue(self.controller.observe_now())
        self.assertEqual(server.requests, [])
        self.assertFalse(self.controller.preview.isNull())
        self.scene.info = replace(
            self.source_info, hwnd=87654, pid=os.getpid(),
            process_name="python.exe", title="AURA synthetic debug panel",
        )
        self.assertTrue(self.controller.comment_available)

    def wait_for_result(self):
        spin_until(lambda: not self.controller.busy)

    def test_companion_ordinary_media_can_speak_with_low_honest_importance(self):
        with MockApiServer() as server:
            self.configure(server, commentary_mode="companion")
            server.enqueue(media_event())
            self.assertTrue(self.controller.poll())
            self.wait_for_result()
            self.assertEqual(len(server.requests), 1)
            self.assertTrue(self.pet.bubble.isVisible())
            self.assertEqual(self.controller.history[-1]["动作"], "speak")
            self.assertEqual(self.controller.history[-1]["原因"], "companion_event")
            self.assertIn("陪伴评论", server.requests[0]["body"]["messages"][0]["content"])
            self.assertEqual(self.chat.messages[-1]["source"], "proactive_screen")
            self.assertEqual(self.chat.messages[-1]["screen_event"]["event_type"], "media")

    def test_quiet_mode_keeps_ordinary_media_silent_and_reaches_prompt(self):
        with MockApiServer() as server:
            self.configure(server, commentary_mode="quiet")
            server.enqueue(media_event())
            self.controller.poll()
            self.wait_for_result()
            self.assertEqual(len(server.requests), 1)
            self.assertFalse(self.pet.bubble.isVisible())
            self.assertEqual(self.controller.history[-1]["原因"], "low_importance")
            self.assertIn("重要事件优先", server.requests[0]["body"]["messages"][0]["content"])

    def test_manual_comment_uses_cached_preview_while_debug_panel_is_foreground(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            captures = self.scene.capture_calls
            server.enqueue(media_event())
            self.assertTrue(self.controller.comment_current_preview())
            self.assertTrue(self.controller.busy)
            self.assertFalse(self.controller.comment_available)
            self.assertFalse(self.controller.comment_current_preview())
            self.wait_for_result()
            self.assertEqual(self.scene.capture_calls, captures, "Manual commentary must not recapture AURA")
            self.assertEqual(len(server.requests), 1)
            self.assertTrue(self.pet.bubble.isVisible())
            self.assertEqual(self.character.expression, "happy")
            self.assertIn("合成画面", self.pet.bubble._text)
            self.assertEqual(self.controller.history[-1]["动作"], "speak")
            self.assertEqual(self.controller.history[-1]["原因"], "manual_comment")
            self.assertEqual(self.controller.history[-1]["分类"], "media")
            self.assertEqual(self.controller.history[-1]["候选评论"], json.loads(media_event())["reply"])
            self.assertTrue(self.controller.comment_available)
            self.assertEqual(self.chat.messages[-1]["source"], "proactive_screen")
            self.assertEqual(self.chat.messages[-1]["content"], json.loads(media_event())["reply"])

    def test_manual_comment_bypasses_auto_budget_without_modifying_it(self):
        with MockApiServer() as server:
            self.prepare_preview(server, max_speeches_per_hour=1, commentary_mode="quiet")
            self.controller.agent.record(Decision("speak", "important_event"), self.clock())
            budget = tuple(self.controller.agent._speeches)
            last_speech = self.controller.agent._last_speech_at
            self.controller._last_analysis_at = self.clock()
            payload = media_event(importance=0.0)
            event = parse_context(payload)
            self.controller.deduplicator.remember("event:" + event.event_key, self.clock())
            for _ in range(2):
                server.enqueue(payload)
                self.assertTrue(self.controller.comment_current_preview())
                self.wait_for_result()
                self.assertEqual(self.controller.history[-1]["原因"], "manual_comment")
                self.assertTrue(self.pet.bubble.isVisible())
                self.pet.bubble.hide()
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(tuple(self.controller.agent._speeches), budget)
            self.assertEqual(self.controller.agent._last_speech_at, last_speech)
            decision = self.controller.agent.decide(event, self.clock(), self.controller.settings)
            self.assertNotEqual(decision.action, "speak", "Manual requests must not reset auto quotas")

    def test_manual_comment_rejects_none_low_confidence_and_empty_reply(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            cases = (
                media_event(kind="none", reply="不能把无内容当作评论。"),
                media_event(confidence=0.59),
                media_event(reply="  "),
                "ordinary text without a validated visual event",
            )
            for payload in cases:
                with self.subTest(payload=payload):
                    server.enqueue(payload)
                    self.assertTrue(self.controller.comment_current_preview())
                    self.wait_for_result()
                    self.assertFalse(self.pet.bubble.isVisible())
                    self.assertEqual(self.controller.history[-1]["动作"], "quiet")
            self.assertEqual(len(server.requests), len(cases))

    def test_manual_comment_is_unavailable_without_consent_or_safe_preview(self):
        self.assertFalse(self.controller.comment_available)
        self.assertFalse(self.controller.comment_current_preview())
        self.assertEqual(self.scene.capture_calls, 0)
        self.configure()
        self.assertTrue(self.controller.observe_now())
        self.assertFalse(self.controller.comment_available)
        self.assertFalse(self.controller.comment_current_preview())
        self.assertEqual(self.scene.capture_calls, 1)

    def test_manual_comment_defers_to_dragging_and_local_observation_reservation(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            self.pet.animation.start_dragging()
            self.assertEqual(self.pet.animation.state, AnimationState.DRAGGING)
            self.assertFalse(self.controller.comment_available)
            self.assertFalse(self.controller.comment_current_preview())
            self.pet.animation.finish_dragging()
            self.assertTrue(self.controller.begin_local_observation())
            self.assertFalse(self.controller.comment_available)
            self.assertFalse(self.controller.comment_current_preview())
            self.controller.cancel_local_observation()
            self.assertTrue(self.controller.comment_available)
            self.assertEqual(server.requests, [])

    def test_explicit_comment_after_completed_chat_bypasses_auto_quiet_period(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            self.chat.emotion_requested.connect(self.character.set_expression)
            server.enqueue('{"reply":"刚才的主动聊天已经完成。","emotion":"talking"}')
            self.assertTrue(self.chat.send("先聊一句，再让我看看画面。"))
            spin_until(lambda: not self.chat.busy)
            self.assertEqual(self.character.expression, "talking")
            self.assertEqual(self.controller._last_user_chat_at, self.clock())
            self.assertTrue(self.controller.comment_available)
            # No clock advance: the automatic 60-second quiet period is still
            # active, but this new explicit user request should be honored.
            server.enqueue(media_event())
            self.assertTrue(self.controller.comment_current_preview())
            self.wait_for_result()
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(self.controller.history[-1]["原因"], "manual_comment")
            self.assertTrue(self.pet.bubble.isVisible())
            self.assertEqual(self.character.expression, "happy")
            self.assertEqual(len(self.chat.messages), 3)
            self.assertEqual(self.chat.messages[-1]["source"], "proactive_screen")

    def test_new_chat_during_manual_request_keeps_priority_after_chat_finishes(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            self.chat.emotion_requested.connect(self.character.set_expression)
            server.enqueue(media_event(), delay=0.65)
            self.assertTrue(self.controller.comment_current_preview())
            spin_until(server.received.is_set)
            server.enqueue('{"reply":"先回答用户的新消息。","emotion":"talking"}')
            self.assertTrue(self.chat.send("这条新消息比旧预览更优先。"))
            spin_until(lambda: not self.chat.busy)
            self.assertTrue(self.controller.busy)
            self.assertEqual(self.character.expression, "talking")
            chat_bubble = self.pet.bubble._text
            # Missing talking art can itself show a local feedback bubble.
            # Hide that existing feedback to detect a newly played comment.
            self.pet.bubble.hide()
            # A simple chat.busy check would now allow the older comment to
            # overwrite the response. The request's chat revision must differ.
            self.wait_for_result()
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(self.controller.history[-1]["原因"], "user_busy")
            self.assertEqual(self.controller.history[-1]["动作"], "quiet")
            self.assertFalse(self.pet.bubble.isVisible())
            self.assertEqual(self.pet.bubble._text, chat_bubble)
            self.assertEqual(self.character.expression, "talking")
            self.assertEqual(len(self.chat.messages), 2)

    def test_privacy_or_other_source_rejects_cached_manual_request_before_network(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            captures = self.scene.capture_calls
            self.scene.info = replace(self.source_info, title="密码 - SYNTHETIC PRIVATE WINDOW")
            self.assertFalse(self.controller.comment_current_preview())
            self.assertTrue(self.controller.preview.isNull())
            self.assertFalse(self.controller.comment_available)
            self.assertEqual(server.requests, [])
            self.scene.info = self.source_info
            self.controller.observe_now()
            self.scene.info = replace(self.source_info, hwnd=99999, title="Different synthetic application")
            self.assertFalse(self.controller.comment_current_preview())
            self.assertTrue(self.controller.preview.isNull())
            self.assertEqual(server.requests, [])
            self.assertEqual(self.scene.capture_calls, captures + 1)

    def test_privacy_change_during_manual_request_discards_late_reply(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            server.enqueue(media_event(), delay=0.3)
            self.assertTrue(self.controller.comment_current_preview())
            spin_until(server.received.is_set)
            self.scene.info = replace(self.source_info, title="密码 - SYNTHETIC PRIVATE WINDOW")
            self.wait_for_result()
            self.assertFalse(self.pet.bubble.isVisible())
            self.assertTrue(self.controller.preview.isNull())
            self.assertEqual(self.controller.history, [])
            self.assertEqual(self.character.expression, "neutral")

    def test_switch_to_different_external_window_discards_late_manual_reply(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            server.enqueue(media_event(), delay=0.3)
            self.controller.comment_current_preview()
            spin_until(server.received.is_set)
            self.scene.info = replace(self.source_info, hwnd=99999, title="Different synthetic application")
            self.wait_for_result()
            self.assertFalse(self.pet.bubble.isVisible())
            self.assertTrue(self.controller.preview.isNull())
            self.assertEqual(self.controller.history, [])

    def _assert_pending_manual_cancelled(self, action):
        with MockApiServer() as server:
            self.prepare_preview(server)
            server.enqueue(media_event(), delay=0.4)
            self.controller.comment_current_preview()
            spin_until(server.received.is_set)
            started = time.monotonic()
            action()
            self.assertLess(time.monotonic() - started, 0.3)
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            self.assertFalse(self.controller.busy)
            self.assertFalse(self.pet.bubble.isVisible())
            self.assertTrue(self.controller.preview.isNull())
            self.assertEqual(self.controller.history, [])

    def test_disable_cancels_manual_comment_promptly(self):
        self._assert_pending_manual_cancelled(
            lambda: self.controller.update_settings(replace(self.controller.settings, enabled=False)))

    def test_session_reset_cancels_manual_comment_promptly(self):
        self._assert_pending_manual_cancelled(self.controller.reset_session)

    def test_shutdown_cancels_manual_comment_promptly(self):
        self._assert_pending_manual_cancelled(self.controller.shutdown)

    def test_slow_manual_request_keeps_gui_animation_and_controls_responsive(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            server.enqueue(media_event(), delay=0.65)
            ticks = []
            timer = QTimer()
            timer.setInterval(15)
            timer.timeout.connect(lambda: ticks.append(time.monotonic()))
            frames = QSignalSpy(self.pet.frame_tick)
            timer.start()
            try:
                self.controller.comment_current_preview()
                spin_until(server.received.is_set)
                self.character.set_scale(125)
                self.character.set_expression("sleepy")
                self.assertEqual(self.character.scale_percent, 125)
                self.assertEqual(self.character.expression, "sleepy")
                self.wait_for_result()
                self.assertGreater(len(ticks), 12)
                self.assertGreater(frames.count(), 6)
                self.assertTrue(self.pet.rect().toRectF().contains(self.pet.animated_sprite_rect))
            finally:
                timer.stop()

    def test_manual_api_failure_is_visible_without_leaking_credentials(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            server.enqueue("untrusted provider detail " + LOCAL_TEST_KEY, status=401)
            self.controller.comment_current_preview()
            self.wait_for_result()
            self.assertIn("认证失败", self.controller.status)
            self.assertNotIn(LOCAL_TEST_KEY, self.controller.status)
            self.assertFalse(self.pet.bubble.isVisible())
            self.assertFalse(self.controller.preview.isNull())
            self.assertTrue(self.controller.comment_available)
            self.character.set_scale(75)
            self.assertEqual(self.character.scale_percent, 75)

    def test_manual_reply_is_archived_without_screenshot_or_credentials(self):
        with MockApiServer() as server:
            self.prepare_preview(server)
            reply = "SYNTHETIC MANUAL REPLY FOR CONVERSATION ARCHIVE"
            server.enqueue(media_event(reply=reply))
            self.controller.comment_current_preview()
            self.wait_for_result()
            self.assertEqual(self.controller.history[-1]["鲸鱼娘"], reply)
            saved = self.path.read_text(encoding="utf-8")
            for secret in (LOCAL_TEST_KEY, self.source_info.title, reply, "data:image/"):
                self.assertNotIn(secret, saved)
            rows = self.chat.store.load_messages(self.chat.current_conversation_id)
            self.assertEqual(rows[-1]["content"], reply)
            self.assertEqual(rows[-1]["source"], "proactive_screen")
            archive = json.dumps(rows, ensure_ascii=False)
            for forbidden in (LOCAL_TEST_KEY, self.source_info.title, "data:image/"):
                self.assertNotIn(forbidden, archive)
            self.assertTrue({p.suffix for p in Path(self.temp.name).iterdir()}
                            <= {".ini", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm"})


if __name__ == "__main__":
    unittest.main()
