"""Synthetic-screen Qt integration tests; no user desktop or external API.

All images are generated in memory, configuration is a temporary INI, and the
only network target is tests.mock_api's loopback OpenAI-compatible HTTP server.
These tests never use the user's credential vault or physical mouse.
"""

from dataclasses import replace
import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from PySide6.QtCore import QEvent, QThreadPool, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig
from desktop_pet.perception.controller import PerceptionController
from desktop_pet.perception.screen_observer import ForegroundInfo, ScreenObserver
from desktop_pet.perception.settings import PerceptionSettings
from desktop_pet.pet_controller import PetController
from desktop_pet.pet_window import PetWindow
from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.perception_panel import PerceptionPanel
from tests.mock_api import MockApiServer
from tests.test_application import (
    LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until,
)


class AdjustableClock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class SyntheticScene:
    """A bounded synthetic foreground window, never a screen capture."""

    def __init__(self):
        self.info = ForegroundInfo(
            hwnd=54321, pid=54321, process_name="synthetic-editor.exe",
            title="Synthetic drawing window", idle_seconds=0, available=True,
        )
        self.image = synthetic_image("#4e9fc6")
        self.metadata_calls = 0
        self.capture_calls = 0

    def metadata(self):
        self.metadata_calls += 1
        return self.info

    def capture(self, info):
        self.capture_calls += 1
        return QImage(self.image)

    def observer(self):
        return ScreenObserver(metadata_provider=self.metadata, capture=self.capture)

    def change(self, color="#d69d47", **metadata):
        self.image = synthetic_image(color)
        if metadata:
            self.info = replace(self.info, **metadata)


def synthetic_image(color: str) -> QImage:
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def event_json(*, key="synthetic-chart", emotion="happy", importance=0.9,
               reply="这个合成图表很有趣！", kind="interesting") -> str:
    return json.dumps({
        "kind": kind, "summary": "合成画面出现新图表", "reply": reply,
        "emotion": emotion, "importance": importance, "confidence": 0.95,
        "event_key": key,
    }, ensure_ascii=False)


class PerceptionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-perception-tests-")
        self.path = Path(self.directory.name) / "settings.ini"
        self.settings = isolated_settings(self.path, MemoryCredentialBackend())
        self.settings.save_perception_settings(PerceptionSettings(enabled=False, allow_remote=False))
        self.pet = PetWindow(PetConfig())
        self.character = PetController(self.pet, self.settings, animation_directory=self.path.parent / "empty-animations")
        self.chat = ChatController(self.settings)
        self.chat.emotion_requested.connect(self.character.set_expression)
        self.scene = SyntheticScene()
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
        self.directory.cleanup()

    def configure(self, server: MockApiServer | None = None, **overrides):
        if server is not None:
            self.settings.save_api_settings(ApiSettings(server.base_url, "test-model", 5))
            self.settings.save_api_key(LOCAL_TEST_KEY)
        values = {"enabled": True, "allow_remote": server is not None}
        values.update(overrides)
        self.assertTrue(self.controller.update_settings(PerceptionSettings(**values)))

    def poll_and_wait(self):
        self.controller.poll()
        spin_until(lambda: not self.controller.busy)

    def test_disabled_startup_does_not_observe_or_request(self):
        self.assertFalse(self.controller.settings.enabled)
        self.assertFalse(self.controller.settings.allow_remote)
        self.controller.poll()
        self.app.processEvents()
        self.assertEqual(self.scene.metadata_calls, 0)
        self.assertEqual(self.scene.capture_calls, 0)
        self.assertFalse(self.controller.busy)
        self.assertEqual(self.controller.history, [])

    def test_manual_local_observation_never_sends_even_when_remote_allowed(self):
        with MockApiServer() as server:
            self.configure(server)
            self.controller.observe_now()
            spin_until(lambda: not self.controller.busy)
            self.assertGreater(self.scene.capture_calls, 0)
            self.assertEqual(server.requests, [])
            self.assertTrue(self.controller.details)
            self.assertEqual(self.controller.details["截图方式"], "可见窗口区域（遮挡已遮蔽）")
            self.assertEqual(self.chat.messages, [])

    def test_countdown_reservation_blocks_actual_automatic_ticks_and_then_restores_analysis(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(event_json())
            panel = PerceptionPanel(self.controller)
            previous_settings = self.path.read_text(encoding="utf-8")
            previous_analysis = self.clock() - 61
            self.controller._last_analysis_at = previous_analysis
            automatic_ticks = []
            # Accelerate both real Qt timers while keeping three countdown
            # ticks. Automatic polling must actually run during that window.
            panel._observation_timer.setInterval(30)
            self.controller._timer.setInterval(10)
            self.controller._timer.timeout.connect(lambda: automatic_ticks.append(self.scene.capture_calls))
            # Freeze the next automatic period as soon as the explicit local
            # preview is delivered so we inspect the reservation in isolation.
            self.controller.preview_changed.connect(lambda image: self.controller._timer.stop())
            try:
                panel.observe_button.click()
                self.assertFalse(self.controller.poll())
                self.assertEqual(self.scene.capture_calls, 0)
                spin_until(lambda: self.scene.capture_calls == 1)
                self.assertGreaterEqual(len(automatic_ticks), 3)
                self.assertTrue(all(count == 0 for count in automatic_ticks))
                self.assertEqual(server.requests, [])
                self.assertFalse(self.controller.busy)
                self.assertFalse(self.controller.preview.isNull())
                self.assertFalse(panel._observation_timer.isActive())
                self.assertEqual(self.controller._last_analysis_at, previous_analysis)
                self.assertEqual(self.path.read_text(encoding="utf-8"), previous_settings)
                self.assertTrue(self.controller.settings.enabled)
                self.assertTrue(self.controller.settings.allow_remote)
                # Completion released the reservation and did not accept a
                # frame or spend the API budget on behalf of automatic work.
                self.assertTrue(self.controller.poll())
                spin_until(lambda: not self.controller.busy)
                self.assertEqual(len(server.requests), 1)
            finally:
                panel._cancel_pending_observation()
                panel.deleteLater()

    def test_reservation_cancellation_reset_settings_and_shutdown_do_not_reset_api_cooldown(self):
        self.configure()
        marker = self.clock() - 5
        self.controller._last_analysis_at = marker
        for release in (
            self.controller.cancel_local_observation,
            self.controller.reset_session,
            lambda: self.controller.update_settings(self.controller.settings),
        ):
            with self.subTest(release=release):
                self.assertTrue(self.controller.begin_local_observation())
                before = self.scene.capture_calls
                self.assertFalse(self.controller.poll())
                self.assertEqual(self.scene.capture_calls, before)
                release()
                self.assertTrue(self.controller.poll())
                self.assertEqual(self.scene.capture_calls, before + 1)
                self.assertEqual(self.controller._last_analysis_at, marker)
        self.assertTrue(self.controller.begin_local_observation())
        self.controller.update_settings(replace(self.controller.settings, enabled=False))
        self.assertFalse(self.controller._local_observation_pending)
        self.assertFalse(self.controller.poll())
        self.controller.update_settings(replace(self.controller.settings, enabled=True))
        self.assertTrue(self.controller.begin_local_observation())
        self.controller.shutdown()
        self.assertFalse(self.controller._local_observation_pending)
        self.assertFalse(self.controller.poll())
        self.assertEqual(self.controller._last_analysis_at, marker)

    def test_existing_analysis_cannot_be_reserved_or_cancelled_by_local_preview(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(event_json(), delay=0.25)
            self.controller.poll()
            spin_until(server.received.is_set)
            pending = self.controller._pending
            worker = self.controller._worker
            request_id = self.controller._request_id
            self.assertFalse(self.controller.begin_local_observation())
            self.assertIn("等待完成", self.controller.status)
            self.assertTrue(self.controller.busy)
            self.assertIs(self.controller._pending, pending)
            self.assertIs(self.controller._worker, worker)
            self.assertEqual(self.controller._request_id, request_id)
            self.assertFalse(worker.cancelled.is_set())
            self.assertFalse(self.controller._local_observation_pending)
            spin_until(lambda: not self.controller.busy)
            self.assertEqual(len(server.requests), 1)
            self.assertTrue(self.controller.history)

    def test_sensitive_title_is_filtered_before_capture_and_network(self):
        with MockApiServer() as server:
            self.configure(server)
            private_title = "付款窗口：密码和验证码 - SYNTHETIC PRIVATE TITLE"
            self.scene.info = replace(self.scene.info, title=private_title)
            self.poll_and_wait()
            self.assertEqual(self.scene.capture_calls, 0)
            self.assertEqual(server.requests, [])
            self.assertNotIn(private_title, repr(self.controller.details))
            self.assertNotIn(private_title, repr(self.controller.history))
            self.assertTrue(self.pet.isVisible())

    def test_return_to_aura_keeps_labeled_preview_and_detector_baseline(self):
        self.configure()
        original_info = self.scene.info
        self.controller.observe_now()
        self.assertFalse(self.controller.preview.isNull())
        preview_key = self.controller.preview.cacheKey()
        captures = self.scene.capture_calls
        self.scene.info = replace(original_info, hwnd=87654, pid=os.getpid(),
                                  process_name="python.exe", title="AURA synthetic debug panel")
        self.assertFalse(self.controller.poll())
        self.assertEqual(self.scene.capture_calls, captures)
        self.assertEqual(self.controller.preview.cacheKey(), preview_key)
        self.assertEqual(self.controller.details["title"], original_info.title)
        self.assertTrue(self.controller.details["preview_retained"])
        self.assertIn("上次有效观察", self.controller.details["status"])
        self.assertIn("未截图、未外发", self.controller.status)
        self.scene.info = original_info
        self.controller.observe_now()
        self.assertEqual(self.controller.details["reason"], "stable")

    def test_aura_without_previous_external_observation_has_clear_status(self):
        self.configure()
        self.scene.info = replace(self.scene.info, pid=os.getpid())
        self.assertFalse(self.controller.observe_now())
        self.assertEqual(self.scene.capture_calls, 0)
        self.assertTrue(self.controller.preview.isNull())
        self.assertIn("尚无外部窗口预览", self.controller.details["status"])
        self.assertFalse(self.controller.details["preview_retained"])

    def test_privacy_filter_evicts_previous_preview_before_return_to_aura(self):
        self.configure()
        original_info = self.scene.info
        self.controller.observe_now()
        self.assertFalse(self.controller.preview.isNull())
        self.scene.info = replace(original_info, title="密码 - SYNTHETIC PRIVATE WINDOW")
        self.assertFalse(self.controller.poll())
        self.assertTrue(self.controller.preview.isNull())
        self.assertNotIn(original_info.title, repr(self.controller.details))
        self.scene.info = replace(original_info, pid=os.getpid())
        self.controller.observe_now()
        self.assertTrue(self.controller.preview.isNull())
        self.assertNotIn(original_info.title, repr(self.controller.details))

    def test_unavailable_capture_never_reaches_model(self):
        with MockApiServer() as server:
            self.configure(server)
            self.controller.observe_now()
            self.scene.image = QImage()
            self.assertFalse(self.controller.poll())
            self.assertEqual(server.requests, [])
            self.assertFalse(self.controller.busy)
            self.assertTrue(self.controller.preview.isNull())
            self.assertTrue(self.controller.details.get("reason"))

    def test_own_window_late_reply_preserves_preview_without_action_or_reanalysis(self):
        with MockApiServer() as server:
            self.configure(server)
            original_info = self.scene.info
            server.enqueue(event_json(reply="切回 AURA 后不应触发这条回复。"), delay=0.4)
            self.controller.poll()
            spin_until(server.received.is_set)
            self.assertTrue(self.controller.busy)
            captures = self.scene.capture_calls
            self.scene.info = replace(original_info, pid=os.getpid(), hwnd=87654)
            self.assertFalse(self.controller.poll())
            self.assertTrue(self.controller.busy)
            self.assertEqual(self.scene.capture_calls, captures)
            spin_until(lambda: not self.controller.busy)
            self.assertEqual(self.character.expression, "neutral")
            self.assertFalse(self.pet.bubble.isVisible())
            self.assertEqual(self.controller.history, [])
            self.assertFalse(self.controller.preview.isNull())
            self.assertEqual(self.controller.details["title"], original_info.title)
            self.assertTrue(self.controller.details["preview_retained"])
            self.assertIn("晚到分析未触发动作", self.controller.status)
            self.scene.info = original_info
            self.clock.advance(61)
            self.poll_and_wait()
            self.assertEqual(self.controller.details["reason"], "stable")
            self.assertEqual(len(server.requests), 1)

    def test_disable_and_session_reset_remove_retained_preview(self):
        self.configure()
        self.controller.observe_now()
        self.assertFalse(self.controller.preview.isNull())
        self.controller.reset_session()
        self.assertTrue(self.controller.preview.isNull())
        self.assertEqual(self.controller.details, {})
        self.controller.observe_now()
        self.assertFalse(self.controller.preview.isNull())
        self.controller.update_settings(replace(self.controller.settings, enabled=False))
        self.assertTrue(self.controller.preview.isNull())
        self.assertEqual(self.controller.details, {})

    def test_api_error_remains_visible_when_returning_to_aura_with_retained_preview(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue("sensitive remote error " + LOCAL_TEST_KEY, status=401, delay=0.2)
            self.controller.poll()
            spin_until(server.received.is_set)
            self.scene.info = replace(self.scene.info, pid=os.getpid(), hwnd=87654)
            spin_until(lambda: not self.controller.busy)
            self.assertIn("认证失败", self.controller.status)
            self.assertNotIn(LOCAL_TEST_KEY, self.controller.status)
            self.assertFalse(self.controller.preview.isNull())
            self.assertTrue(self.controller.details["preview_retained"])
            self.assertFalse(self.pet.bubble.isVisible())
    def test_excluded_process_and_unavailable_window_are_safe(self):
        self.configure()
        self.scene.info = replace(self.scene.info, process_name="Bitwarden.exe")
        self.poll_and_wait()
        self.assertEqual(self.scene.capture_calls, 0)
        self.scene.info = replace(self.scene.info, process_name="other.exe", available=False)
        self.poll_and_wait()
        self.assertEqual(self.scene.capture_calls, 0)
        self.assertTrue(self.pet.isVisible())

    def test_foreground_change_during_capture_discards_image(self):
        settings = PerceptionSettings(enabled=True)
        before = self.scene.info
        after = replace(before, hwnd=98765, title="密码窗口 - SYNTHETIC")
        calls = iter((before, after))
        observer = ScreenObserver(metadata_provider=lambda: next(calls), capture=self.scene.capture)
        observation = observer.observe(settings)
        self.assertEqual(self.scene.capture_calls, 1)
        self.assertIsNone(observation.image)
        self.assertTrue(observation.blocked_reason)

    def test_window_metadata_and_capture_failures_degrade_without_raising(self):
        def fail(*args):
            raise RuntimeError("synthetic native capture failure")

        settings = PerceptionSettings(enabled=True)
        observer = ScreenObserver(metadata_provider=fail, capture=self.scene.capture)
        result = observer.observe(settings)
        self.assertTrue(result.blocked_reason)
        self.assertEqual(self.scene.capture_calls, 0)
        observer = ScreenObserver(metadata_provider=self.scene.metadata, capture=fail)
        result = observer.observe(settings)
        self.assertTrue(result.blocked_reason)
        self.assertIsNone(result.image)

    def test_long_user_idle_prevents_remote_request(self):
        with MockApiServer() as server:
            self.configure(server)
            self.scene.info = replace(self.scene.info, idle_seconds=601)
            self.poll_and_wait()
            self.assertEqual(server.requests, [])
            self.assertEqual(self.character.expression, "neutral")

    def test_actual_multimodal_sdk_request_drives_character_and_bubble(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(event_json())
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 1)
            request = server.requests[0]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(request["body"]["model"], "test-model")
            content = request["body"]["messages"][-1]["content"]
            self.assertIsInstance(content, list)
            images = [part["image_url"]["url"] for part in content if part.get("type") == "image_url"]
            self.assertEqual(len(images), 1)
            prefix, encoded = images[0].split(",", 1)
            self.assertTrue(prefix.startswith("data:image/"))
            transmitted = QImage.fromData(base64.b64decode(encoded))
            self.assertFalse(transmitted.isNull())
            self.assertLessEqual(max(transmitted.width(), transmitted.height()), 1280)
            self.assertNotIn("response_format", request["body"])
            self.assertEqual(self.character.expression, "happy")
            self.assertIn("合成图表", self.pet.bubble._text)
            self.assertEqual(self.chat.messages[-1]["source"], "proactive_screen")
            self.assertEqual(self.chat.messages[-1]["screen_event"]["event_type"], "interesting")
            self.assertTrue(self.controller.history)

    def test_stable_frame_and_remote_interval_avoid_repeated_requests(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(event_json())
            self.poll_and_wait()
            self.clock.advance(61)
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 1)
            self.scene.change()
            server.enqueue(event_json(key="second-chart", reply="第二个合成事件。"))
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 2)
            self.scene.change("#3b3849")
            self.clock.advance(5)
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 2)

    def test_same_event_is_not_repeated_after_changed_frame(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(event_json())
            self.poll_and_wait()
            before = self.pet.bubble._text
            self.pet.bubble.hide()
            self.scene.change()
            self.clock.advance(181)
            server.enqueue(event_json(reply="重复事件不得再次发言。"))
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(self.pet.bubble._text, before)
            self.assertFalse(self.pet.bubble.isVisible())

    def test_hourly_speech_limit_keeps_motion_silent(self):
        with MockApiServer() as server:
            self.configure(server, max_speeches_per_hour=1)
            server.enqueue(event_json())
            self.poll_and_wait()
            self.pet.bubble.hide()
            self.scene.change()
            self.clock.advance(181)
            server.enqueue(event_json(key="new-event", emotion="surprised", reply="不该显示第二次发言。"))
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(self.character.expression, "surprised")
            self.assertFalse(self.pet.bubble.isVisible())

    def test_slow_request_leaves_gui_and_character_controls_responsive(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(event_json(), delay=0.65)
            ticks = []
            timer = QTimer()
            timer.setInterval(15)
            timer.timeout.connect(lambda: ticks.append(time.monotonic()))
            frames = QSignalSpy(self.pet.frame_tick)
            timer.start()
            try:
                self.controller.poll()
                spin_until(server.received.is_set)
                self.assertTrue(self.controller.busy)
                self.character.set_scale(125)
                self.character.set_expression("sleepy")
                self.assertEqual(self.character.scale_percent, 125)
                self.assertEqual(self.character.expression, "sleepy")
                spin_until(lambda: not self.controller.busy)
                self.assertGreater(len(ticks), 12)
                self.assertGreater(frames.count(), 6)
                self.assertTrue(self.pet.rect().toRectF().contains(self.pet.animated_sprite_rect))
            finally:
                timer.stop()

    def test_disable_during_request_is_prompt_and_late_reply_has_no_effect(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(event_json(reply="禁用后不得出现这条。"), delay=0.6)
            self.controller.poll()
            spin_until(server.received.is_set)
            before = self.character.expression
            started = time.monotonic()
            self.assertTrue(self.controller.update_settings(replace(self.controller.settings, enabled=False)))
            self.assertLess(time.monotonic() - started, 0.3)
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            self.assertFalse(self.controller.busy)
            self.assertEqual(self.character.expression, before)
            self.assertNotIn("禁用后不得出现", self.pet.bubble._text)
            self.assertEqual(self.controller.history, [])

    def test_privacy_change_while_request_is_pending_suppresses_feedback(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(event_json(reply="切换隐私窗口后不得显示。"), delay=0.4)
            self.controller.poll()
            spin_until(server.received.is_set)
            self.scene.info = replace(self.scene.info, title="密码管理器 - SYNTHETIC PRIVATE WINDOW")
            spin_until(lambda: not self.controller.busy)
            self.assertEqual(self.character.expression, "neutral")
            self.assertNotIn("切换隐私窗口后不得显示", self.pet.bubble._text)

    def test_user_chat_takes_priority_over_screen_analysis(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue('{"reply":"用户主动聊天优先。","emotion":"talking"}', delay=0.4)
            self.assertTrue(self.chat.send("这是一条主动消息"))
            spin_until(server.received.is_set)
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 1)
            spin_until(lambda: not self.chat.busy)
            self.assertEqual(self.character.expression, "talking")
            self.assertEqual(len(self.chat.messages), 2)

    def test_dragging_suppresses_feedback_without_interrupting_mouse_state(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(event_json(), delay=0.35)
            self.controller.poll()
            spin_until(server.received.is_set)
            self.pet.animation.start_dragging()
            spin_until(lambda: not self.controller.busy)
            self.assertEqual(self.pet.animation.state, AnimationState.DRAGGING)
            self.assertEqual(self.character.expression, "neutral")
            self.assertFalse(self.pet.bubble.isVisible())
            self.pet.animation.finish_dragging()
            self.assertEqual(self.pet.animation.state, AnimationState.RETURNING)

    def test_plain_text_stays_quiet_but_unknown_emotion_keeps_valid_comment(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue("普通文本没有可靠的结构化情境；应当保持安静。")
            self.poll_and_wait()
            self.assertEqual(self.character.expression, "neutral")
            self.assertFalse(self.pet.bubble.isVisible())
            self.scene.change()
            self.clock.advance(61)
            server.enqueue(event_json(key="invalid-emotion", emotion="execute_python"))
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(self.character.expression, "neutral")
            self.assertTrue(self.pet.bubble.isVisible())
            self.assertEqual(self.pet.bubble._text, "这个合成图表很有趣！")

    def test_clearing_debug_events_does_not_reset_request_and_speech_limits(self):
        with MockApiServer() as server:
            self.configure(server, max_speeches_per_hour=1)
            server.enqueue(event_json())
            self.poll_and_wait()
            self.controller.reset_session()
            self.pet.bubble.hide()
            self.scene.change()
            self.clock.advance(5)
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 1)
            self.clock.advance(181)
            server.enqueue(event_json(key="after-clear", emotion="surprised"))
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(self.character.expression, "surprised")
            self.assertFalse(self.pet.bubble.isVisible())

    def test_http_error_does_not_leak_secret_and_pet_remains_usable(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue("untrusted response " + LOCAL_TEST_KEY, status=401)
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 1)
            self.assertNotIn(LOCAL_TEST_KEY, self.controller.status)
            self.assertNotIn(LOCAL_TEST_KEY, repr(self.controller.history))
            self.assertTrue(self.controller.status)
            self.character.set_scale(75)
            self.character.set_expression("happy")
            self.assertEqual(self.character.scale_percent, 75)
            self.assertTrue(self.pet.isVisible())
            self.clock.advance(5)
            self.poll_and_wait()
            self.assertEqual(len(server.requests), 1, "Failed requests must also respect the configured interval")

    def test_comment_and_event_are_archived_without_screen_image_or_secret(self):
        with MockApiServer() as server:
            self.configure(server)
            unique_reply = "SYNTHETIC REPLY FOR CONVERSATION ARCHIVE"
            server.enqueue(event_json(reply=unique_reply))
            self.poll_and_wait()
            text = self.path.read_text(encoding="utf-8")
            for value in (LOCAL_TEST_KEY, self.scene.info.title, unique_reply, "data:image/"):
                self.assertNotIn(value, text)
            rows = self.chat.store.load_messages(self.chat.current_conversation_id)
            self.assertEqual(rows[-1]["content"], unique_reply)
            self.assertTrue(rows[-1]["screen_event_id"])
            archive = json.dumps(rows, ensure_ascii=False)
            for forbidden in (LOCAL_TEST_KEY, self.scene.info.title, "data:image/"):
                self.assertNotIn(forbidden, archive)
            self.assertTrue({path.suffix for path in Path(self.directory.name).iterdir()}
                            <= {".ini", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm"})
            self.controller.reset_session()
            self.assertEqual(self.controller.history, [])
            self.assertEqual(self.controller.details, {})


if __name__ == "__main__":
    unittest.main()
