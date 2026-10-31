"""Persistent visual errors with synthetic devices, frames and model responses."""

from types import SimpleNamespace
import unittest

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.api_client import ApiError
from desktop_pet.perception.camera_controller import CameraController
from desktop_pet.perception.context_analyzer import ContextEvent
from desktop_pet.perception.controller import PerceptionController
from desktop_pet.ui.camera_panel import CameraPanel
from desktop_pet.ui.perception_panel import PerceptionPanel
from tests.test_application import spin_until
from tests.test_camera_controller import CameraStore, SyntheticCameraObserver
from tests.test_perception_integration import SyntheticScene
from tests.test_perception_permissions import QuietChat


class SequenceAnalyzer:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def analyze(self, *_args, **_kwargs):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def shutdown(self):
        pass


def quiet_result():
    return ContextEvent("none", "合成场景无须评论", "", "neutral", .1, .95, "recovery-none")


class CameraErrorRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.store, self.chat = CameraStore(), QuietChat()
        self.observer = SyntheticCameraObserver()
        self.analyzer = SequenceAnalyzer(ApiError("provider", "合成模型不支持图片输入。"), quiet_result())
        self.pet = SimpleNamespace(pet=SimpleNamespace(animation=SimpleNamespace(state=AnimationState.IDLE)),
                                   respond=lambda *_args, **_kwargs: None, play_motion=lambda *_args: None)
        self.controller = CameraController(self.store, self.pet, self.chat, observer=self.observer,
                                           analyzer_factory=lambda: self.analyzer, clock=lambda: 1000.)
        self.controller._camera_start_timer.stop()
        self.controllers, self.panels = [self.controller], []

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        for obj in [*self.controllers, self.observer, self.chat]:
            obj.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def enable(self):
        self.assertTrue(self.controller.set_enabled(True, allow_remote=True))
        self.controller._timer.stop()
        self.observer.produce()

    def fail_analysis(self):
        self.enable()
        self.assertTrue(self.controller.comment_current_preview())
        spin_until(lambda: not self.controller.busy)
        self.assertIn("不支持图片", self.controller.runtime_snapshot()["analysisError"])

    def test_device_error_keeps_persistence_warning_until_actual_save_succeeds(self):
        self.enable()
        self.store.fail = True
        self.observer.error_changed.emit("合成摄像头已断开。")
        self.assertFalse(self.controller.settings.enabled)
        self.assertFalse(self.observer.running)
        self.assertTrue(self.store.camera.enabled)
        state = self.controller.runtime_snapshot()
        self.assertTrue(state["permissionSaveFailed"])
        self.assertIn("当前摄像头已关闭", state["persistenceWarning"])
        self.assertIn("保存失败", state["persistenceWarning"])
        self.assertIn("重启", state["persistenceWarning"])
        self.assertIn("保存失败", self.controller.status)
        self.assertTrue(all(item.get("时间") for item in self.controller.history))
        self.controller.set_paused(True)
        self.controller.set_paused(False)
        self.assertTrue(self.controller.runtime_snapshot()["permissionSaveFailed"])
        self.assertFalse(self.observer.running)
        self.store.fail = False
        self.assertTrue(self.controller.update_settings(self.controller.settings))
        self.assertFalse(self.store.camera.enabled)
        self.assertFalse(self.controller.runtime_snapshot()["permissionSaveFailed"])
        self.assertEqual(self.controller.runtime_snapshot()["persistenceWarning"], "")

    def test_next_frames_and_pause_do_not_clear_model_error_or_duplicate_history(self):
        changes = []
        self.controller.runtime_changed.connect(changes.append)
        self.fail_analysis()
        before = self.controller.status
        self.assertEqual(len(self.controller.history), 1)
        self.assertEqual(self.controller.history[0]["原因"], "analysis_failed")
        self.assertTrue(self.controller.history[0]["时间"])
        for _ in range(3):
            self.observer.produce()
        self.assertEqual(self.controller.status, before)
        self.assertEqual(len(self.controller.history), 1)
        self.assertTrue(any(state["analysisError"] for state in changes))
        saved = self.store.camera
        self.controller.set_paused(True)
        self.controller.set_paused(False)
        self.assertEqual(self.store.camera, saved)
        self.assertIn("不支持图片", self.controller.runtime_snapshot()["analysisError"])

    def test_visible_retry_uses_existing_preview_and_success_clears_error(self):
        panel = CameraPanel(self.controller)
        self.panels.append(panel)
        panel.show()
        self.fail_analysis()
        self.assertTrue(panel.runtime_error_label.isVisible())
        self.assertIn("不支持图片", panel.runtime_error_label.text())
        self.assertTrue(panel.retry_button.isVisible())
        self.assertTrue(panel.retry_button.isEnabled())
        self.assertIn("识别请求失败", panel.history_view.toPlainText())
        self.assertIn("时间", panel.history_view.toPlainText())
        saved = self.store.camera
        panel.retry_button.click()
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.analyzer.calls, 2)
        self.assertEqual(self.controller.runtime_snapshot()["analysisError"], "")
        self.assertTrue(panel.retry_button.isHidden())
        self.assertTrue(panel.runtime_error_label.isHidden())
        self.assertEqual(self.store.camera, saved)
        self.assertTrue(any(row["原因"] == "analysis_recovered" for row in self.controller.history))
        self.assertIn("识别已恢复", panel.history_view.toPlainText())

    def test_configuration_failure_is_visible_and_retry_does_not_enable_permissions(self):
        self.enable()
        self.store.get_api_key = lambda: ""
        self.assertFalse(self.controller.comment_current_preview())
        state = self.controller.runtime_snapshot()
        self.assertIn("密钥", state["analysisError"])
        self.assertEqual(self.controller.history[-1]["原因"], "analysis_failed")
        self.controller.set_remote_enabled(False)
        panel = CameraPanel(self.controller)
        self.panels.append(panel)
        panel.show()
        self.assertTrue(panel.retry_button.isVisible())
        self.assertFalse(panel.retry_button.isEnabled())
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertIn("密钥", panel.runtime_error_label.text())

    def test_invalid_model_result_is_recorded_as_actionable_error(self):
        self.analyzer.results = [None]
        self.enable()
        self.assertTrue(self.controller.comment_current_preview())
        spin_until(lambda: not self.controller.busy)
        self.assertIn("结构化输出", self.controller.runtime_snapshot()["analysisError"])
        self.assertEqual(self.controller.history[-1]["原因"], "analysis_failed")

    def test_screen_uses_same_persistent_error_contract_and_chinese_history(self):
        self.store.screen = self.store.screen.__class__(enabled=True, allow_remote=True)
        scene = SyntheticScene()
        screen = PerceptionController(self.store, self.pet, self.chat, observer=scene.observer(),
                                      analyzer_factory=lambda: self.analyzer, clock=lambda: 1000.)
        self.controllers.append(screen)
        screen._timer.stop()
        panel = PerceptionPanel(screen)
        self.panels.append(panel)
        panel.show()
        self.assertTrue(screen.observe_now())
        self.assertTrue(screen.comment_current_preview())
        spin_until(lambda: not screen.busy)
        self.assertIn("不支持图片", screen.runtime_snapshot()["analysisError"])
        self.assertTrue(panel.runtime_error_label.isVisible())
        self.assertIn("识别请求失败", panel.history_view.toPlainText())


if __name__ == "__main__":
    unittest.main()
