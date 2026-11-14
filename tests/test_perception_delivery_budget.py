"""Screen replies commit their limits only after durable delivery; no screenshots/API."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.perception.context_analyzer import ContextEvent
from desktop_pet.perception.controller import PerceptionController
from desktop_pet.perception.settings import PerceptionSettings
from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.perception_panel import PerceptionPanel
from tests.test_perception_integration import AdjustableClock, SyntheticScene


class MemorySettings:
    def __init__(self):
        self.settings = PerceptionSettings(enabled=True, allow_remote=True,
                                           speech_cooldown_seconds=180, max_speeches_per_hour=6)
        self.saves = 0

    def load_perception_settings(self):
        return self.settings

    def save_perception_settings(self, settings):
        self.settings = settings
        self.saves += 1


class DeferredScreenChat(QObject):
    busy_changed = Signal(bool)
    screen_comment_finished = Signal(object, str)
    busy = False
    current_conversation_id = "isolated-conversation"

    def __init__(self):
        super().__init__()
        self.references = []

    def record_screen_comment(self, cid, text, **kwargs):
        reference = {"conversation_id": cid, "message_id": str(uuid4()), "content": text, "emotion": "happy"}
        self.references.append(reference)
        return reference


class PerceptionDeliveryBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.settings = MemorySettings()
        self.clock = AdjustableClock(1000)
        self.chat = DeferredScreenChat()
        self.scene = SyntheticScene()
        self.replies = []
        self.pet = SimpleNamespace(pet=SimpleNamespace(animation=SimpleNamespace(state=AnimationState.IDLE)),
            respond=lambda *args, **kwargs: self.replies.append((args, kwargs)), play_motion=lambda *_: None)
        self.controller = PerceptionController(self.settings, self.pet, self.chat, observer=self.scene.observer(), clock=self.clock)
        self.controller._timer.stop()
        self.event = ContextEvent("interesting", "Synthetic chart", "Synthetic comment", "happy", .9, .95, "unique-chart")

    def tearDown(self):
        self.controller.shutdown()
        self.controller.deleteLater()
        self.chat.deleteLater()
        self.app.processEvents()

    def schedule(self, manual=False):
        observation = self.scene.observer().observe(self.controller.settings)
        self.controller._request_id += 1
        self.controller._pending = (observation, "synthetic-app", "frame-1", manual,
                                    self.controller._chat_revision, self.chat.current_conversation_id, "2026-10-31T00:00:00+00:00")
        self.controller.busy = True
        self.controller._finished(self.controller._request_id, self.event, "")
        return self.chat.references[-1]

    def test_failed_save_consumes_neither_quota_cooldown_nor_event(self):
        reference = self.schedule()
        self.assertIn("正在保存", self.controller.status)
        self.assertEqual(len(self.controller.agent._speeches), 0)
        self.chat.screen_comment_finished.emit(reference, "synthetic disk failure")
        self.assertEqual(self.replies, [])
        self.assertEqual(self.controller.runtime_snapshot()["cooldownSeconds"], 0)
        self.assertEqual(self.controller.runtime_snapshot()["hourlyRemaining"], 6)
        self.assertFalse(self.controller.deduplicator.is_duplicate("event:" + self.event.event_key, self.clock()))
        self.assertEqual(self.controller.history[-1]["原因"], "save_failed")
        self.assertEqual(self.controller.agent.decide(self.event, self.clock(), self.controller.settings).action, "speak")

    def test_success_counts_once_at_delivery_time_and_decrements_real_countdown(self):
        reference = self.schedule()
        self.clock.advance(3)
        self.chat.screen_comment_finished.emit(reference, "")
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(self.controller.runtime_snapshot()["cooldownSeconds"], 180)
        self.assertEqual(self.controller.runtime_snapshot()["hourlyRemaining"], 5)
        self.chat.screen_comment_finished.emit(reference, "")
        self.assertEqual(len(self.controller.agent._speeches), 1)
        self.clock.advance(30)
        self.assertEqual(self.controller.runtime_snapshot()["cooldownSeconds"], 150)

    def test_saved_while_user_busy_is_archived_without_spending_speech_quota(self):
        reference = self.schedule()
        self.chat.busy = True
        self.chat.screen_comment_finished.emit(reference, "")
        self.assertEqual(self.replies, [])
        self.assertEqual(len(self.controller.agent._speeches), 0)
        self.assertTrue(self.controller.deduplicator.is_duplicate("event:" + self.event.event_key, self.clock()))
        self.assertEqual(self.controller.history[-1]["原因"], "saved_only")

    def test_manual_comment_does_not_consume_automatic_budget(self):
        reference = self.schedule(manual=True)
        self.chat.screen_comment_finished.emit(reference, "")
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(len(self.controller.agent._speeches), 0)

    def test_window_change_during_save_retains_archive_without_late_bubble(self):
        for manual in (False, True):
            with self.subTest(manual=manual):
                reference = self.schedule(manual=manual)
                self.scene.change(hwnd=self.scene.info.hwnd + 1, title="Another synthetic window")
                self.chat.screen_comment_finished.emit(reference, "")
                self.assertEqual(self.replies, [])
                self.assertEqual(len(self.controller.agent._speeches), 0)
                self.assertEqual(self.controller.history[-1]["原因"], "saved_only")

    def test_invalid_analysis_does_not_clear_error_or_accept_frame(self):
        observation = self.scene.observer().observe(self.controller.settings)
        self.controller._record_analysis_error("Synthetic previous error")
        self.controller._request_id += 1
        self.controller._pending = (observation, "synthetic-app", "frame-1", False,
            self.controller._chat_revision, self.chat.current_conversation_id, "2026-11-08T00:00:00+00:00")
        self.controller._finished(self.controller._request_id, ContextEvent(valid=False), "")
        self.assertTrue(self.controller.runtime_snapshot()["analysisError"])
        self.assertIsNone(self.controller._last_successful_analysis_at)
        self.assertTrue(self.controller.detector.evaluate(observation.image, "synthetic-app").changed)
        self.assertEqual(self.replies, [])
        self.assertEqual(self.chat.references, [])

    def test_pause_cancels_late_feedback_and_never_changes_saved_consent(self):
        reference = self.schedule()
        self.controller.set_paused(True)
        self.chat.screen_comment_finished.emit(reference, "")
        self.assertEqual(self.replies, [])
        self.assertEqual(len(self.controller.agent._speeches), 0)
        self.assertEqual(self.settings.saves, 0)
        self.assertTrue(self.settings.settings.allow_remote)

    def test_preview_timestamp_age_and_runtime_panel_pause_are_truthful(self):
        self.controller.observe_now()
        self.clock.advance(75)
        panel = PerceptionPanel(self.controller)
        try:
            panel._runtime_changed(self.controller.runtime_snapshot())
            self.assertEqual(self.controller.runtime_snapshot()["previewAgeSeconds"], 75)
            self.assertIn("75 秒", panel.preview_status_label.text())
            self.assertIn("采集于", panel.preview_status_label.text())
            self.assertIn("剩余 6 次", panel.runtime_label.text())
            panel.pause_button.click()
            self.assertTrue(self.controller.paused)
            self.assertEqual(self.settings.saves, 0)
        finally:
            panel.deleteLater()

    def test_manual_analysis_keeps_preview_capture_time_instead_of_request_time(self):
        self.controller.observe_now()
        captured_at = self.controller._preview_wall_time
        self.clock.advance(75)
        self.settings.load_api_settings = lambda: ApiSettings("http://127.0.0.1:1/v1", "fixture")
        self.settings.get_api_key = lambda: "local-only-fixture"
        observation = self.scene.observer().observe(self.controller.settings)
        with patch.object(QThreadPool.globalInstance(), "start"):
            self.assertTrue(self.controller._start_analysis(observation, "synthetic-app", "frame-1", True))
        self.assertEqual(self.controller._pending[6], captured_at)


if __name__ == "__main__":
    unittest.main()
