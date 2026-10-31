"""Immediate permission revocation uses only synthetic frames and fake I/O."""

from dataclasses import replace
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QObject, QThreadPool, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop_pet.animation_controller import AnimationState
from desktop_pet.perception.context_analyzer import ContextEvent
from desktop_pet.perception.controller import PerceptionController
from desktop_pet.perception.settings import PerceptionSettings
from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.perception_panel import PerceptionPanel
from tests.test_application import spin_until
from tests.test_perception_integration import SyntheticScene


class PermissionStore:
    def __init__(self):
        self.persisted = PerceptionSettings(enabled=True, allow_remote=True)
        self.saved = []
        self.fail = False

    def load_perception_settings(self):
        return self.persisted

    def save_perception_settings(self, settings):
        self.saved.append(settings)
        if self.fail:
            raise OSError("sensitive backend detail")
        self.persisted = settings

    def load_api_settings(self):
        return ApiSettings("http://127.0.0.1:9/v1", "synthetic-only-model", 2)

    def get_api_key(self):
        return "local-only-no-http"


class QuietChat(QObject):
    busy_changed = Signal(bool)
    screen_comment_finished = Signal(object, str)
    current_conversation_id = "permission-test-conversation"
    busy = False

    def __init__(self):
        super().__init__()
        self.comments = []

    def record_screen_comment(self, *args, **kwargs):
        self.comments.append((args, kwargs))
        return {}


class ControlledAnalyzer:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.shutdown_count = 0
        self.raise_on_shutdown = False

    def analyze(self, *args, **kwargs):
        self.started.set()
        self.release.wait(3)
        return ContextEvent("interesting", "合成屏幕", "只用于检查晚到结果", "happy", 0.95, 0.95, "synthetic-event")

    def shutdown(self):
        self.shutdown_count += 1
        if self.raise_on_shutdown:
            raise RuntimeError("private transport detail")


class PerceptionPermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.store = PermissionStore()
        self.chat = QuietChat()
        self.scene = SyntheticScene()
        self.analyzer = ControlledAnalyzer()
        self.responses = []
        self.pet = SimpleNamespace(
            pet=SimpleNamespace(animation=SimpleNamespace(state=AnimationState.IDLE)),
            respond=lambda *args, **kwargs: self.responses.append((args, kwargs)),
        )
        self.controller = PerceptionController(
            self.store, self.pet, self.chat, observer=self.scene.observer(),
            analyzer_factory=lambda: self.analyzer,
        )
        self.panel = PerceptionPanel(self.controller)

    def tearDown(self):
        self.analyzer.release.set()
        self.controller.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.panel.deleteLater()
        self.controller.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_remote_checkbox_revokes_before_save_and_local_observation_continues(self):
        self.panel.remote_checkbox.setChecked(False)
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertFalse(self.store.persisted.allow_remote)
        self.assertTrue(self.controller.settings.enabled)
        self.assertTrue(self.controller._timer.isActive())
        self.controller.poll()
        self.assertEqual(self.scene.capture_calls, 1)
        self.assertFalse(self.analyzer.started.is_set())
        self.assertFalse(self.controller.busy)

    def test_local_checkbox_stops_capture_and_clears_existing_preview_immediately(self):
        self.controller.observe_now()
        self.assertFalse(self.controller.preview.isNull())
        captured = self.scene.capture_calls
        self.panel.enabled_checkbox.setChecked(False)
        self.assertFalse(self.controller.settings.enabled)
        self.assertFalse(self.controller._timer.isActive())
        self.assertTrue(self.controller.preview.isNull())
        self.assertIsNone(self.controller._last_observation)
        self.assertFalse(self.controller.poll())
        self.assertFalse(self.controller.observe_now())
        self.assertEqual(self.scene.capture_calls, captured)
        self.assertFalse(self.panel.enabled_checkbox.isChecked())

    def test_failed_persistence_keeps_remote_denied_and_repeated_save_cannot_restore_it(self):
        self.store.fail = True
        self.panel.remote_checkbox.setChecked(False)
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertTrue(self.store.persisted.allow_remote)
        self.assertFalse(self.panel.remote_checkbox.isChecked())
        self.assertIn("本次运行保持关闭", self.panel.status_label.text())
        self.assertIn("下次启动", self.panel.feedback_label.text())
        self.assertNotIn("sensitive backend", self.panel.feedback_label.text())
        self.panel.save_button.click()
        self.controller.poll()
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertFalse(self.panel.remote_checkbox.isChecked())
        self.assertTrue(all(not settings.allow_remote for settings in self.store.saved))
        self.assertFalse(self.analyzer.started.is_set())

    def test_failed_local_revocation_cannot_be_undone_by_resuming_runtime_pause(self):
        self.controller.set_paused(True)
        self.store.fail = True
        self.panel.enabled_checkbox.setChecked(False)
        self.controller.set_paused(False)
        self.assertFalse(self.controller.settings.enabled)
        self.assertFalse(self.controller._timer.isActive())
        self.assertFalse(self.controller.poll())
        self.assertEqual(self.scene.capture_calls, 0)

    def test_revocation_preserves_other_unsaved_parameter_drafts(self):
        self.panel.observation_interval_input.setValue(27)
        self.panel.privacy_keywords_input.setPlainText("草稿过滤词")
        self.panel.remote_checkbox.setChecked(False)
        self.assertEqual(self.panel.observation_interval_input.value(), 27)
        self.assertEqual(self.panel.privacy_keywords_input.toPlainText(), "草稿过滤词")
        self.assertEqual(self.controller.settings.observation_interval_seconds, 10)
        self.assertFalse(self.panel.remote_checkbox.isChecked())

    def test_remote_revocation_cancels_local_countdown_without_capturing(self):
        self.panel.observe_button.click()
        self.assertTrue(self.panel._observation_timer.isActive())
        self.assertTrue(self.controller._local_observation_pending)
        self.panel.remote_checkbox.setChecked(False)
        self.assertFalse(self.panel._observation_timer.isActive())
        self.assertFalse(self.controller._local_observation_pending)
        for _ in range(3):
            self.panel._advance_observation_countdown()
        self.assertEqual(self.scene.capture_calls, 0)

    def test_programmatic_settings_sync_never_writes_or_requests_consent(self):
        with patch.object(QMessageBox, "warning") as warning:
            self.controller.settings_changed.emit(replace(self.controller.settings, enabled=False, allow_remote=False))
        warning.assert_not_called()
        self.assertEqual(self.store.saved, [])
        self.assertFalse(self.panel.enabled_checkbox.isChecked())
        self.assertFalse(self.panel.remote_checkbox.isChecked())

    def test_reenabling_remote_still_requires_confirmation_and_successful_save(self):
        self.panel.remote_checkbox.setChecked(False)
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes) as warning:
            self.panel.remote_checkbox.setChecked(True)
        warning.assert_called_once()
        self.assertFalse(self.controller.settings.allow_remote)
        self.store.fail = True
        self.panel.save_button.click()
        self.assertFalse(self.controller.settings.allow_remote)
        self.store.fail = False
        self.panel.save_button.click()
        self.assertTrue(self.controller.settings.allow_remote)

    def test_late_analysis_is_discarded_after_revocation_even_when_transport_cleanup_raises(self):
        self.assertTrue(self.controller.poll())
        spin_until(self.analyzer.started.is_set)
        worker = self.controller._worker
        self.analyzer.raise_on_shutdown = True
        self.store.fail = True
        self.panel.remote_checkbox.setChecked(False)
        self.assertTrue(worker.cancelled.is_set())
        self.assertEqual(self.analyzer.shutdown_count, 1)
        self.assertFalse(self.controller.busy)
        self.assertIsNone(self.controller._pending)
        self.analyzer.release.set()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.app.processEvents()
        self.assertEqual(self.chat.comments, [])
        self.assertEqual(self.responses, [])
        self.assertEqual(len(self.controller.history), 1)
        self.assertEqual(self.controller.history[0]["原因"], "permission_save_failed")
        self.assertTrue(self.controller.history[0]["时间"])
        self.assertNotIn("只用于检查晚到结果", repr(self.controller.history))
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertIn("关闭状态未能保存", self.controller.status)

    def test_failed_direct_settings_update_also_revokes_before_writing(self):
        self.store.fail = True
        self.assertFalse(self.controller.update_settings(replace(self.controller.settings, enabled=False, allow_remote=False)))
        self.assertFalse(self.controller.settings.enabled)
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertFalse(self.controller._timer.isActive())
        self.assertFalse(self.controller.poll())

    def test_late_archive_callback_cannot_display_bubble_after_revocation(self):
        self.controller._saving_comment = "pending-message"
        self.controller.busy = True
        self.panel.remote_checkbox.setChecked(False)
        self.controller._screen_comment_finished({
            "message_id": "pending-message", "content": "原评论", "emotion": "happy",
        }, "")
        self.assertEqual(self.responses, [])
        self.assertFalse(self.controller.busy)


if __name__ == "__main__":
    unittest.main()
