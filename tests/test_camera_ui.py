"""Camera controls use synthetic images and never access a camera or network."""

from dataclasses import dataclass, replace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop_pet.perception.settings import PerceptionSettings
from desktop_pet.ui.camera_panel import CameraPanel


@dataclass(frozen=True)
class FakeCameraSettings(PerceptionSettings):
    device_id: str = ""


class FakeCameraController(QObject):
    settings_changed = Signal(object)
    status_changed = Signal(str)
    details_changed = Signal(object)
    preview_changed = Signal(QImage)
    history_changed = Signal(object)
    runtime_changed = Signal(object)
    paused_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.settings = FakeCameraSettings()
        self.status = "摄像头已关闭。"
        self.details = {}
        self.history = []
        self.preview = QImage()
        self.busy = False
        self.paused = False
        self.device_active = False
        self.enable_calls = []
        self.remote_calls = []
        self.selected_devices = []
        self.saved_settings = []
        self.observe_count = 0
        self.comment_count = 0
        self.reset_count = 0
        self.save_fails = False
        self.select_fails = False
        self.devices = [
            {"id": "built-in", "description": "内置摄像头", "is_default": True},
            {"id": "usb", "description": "USB 摄像头", "is_default": False},
        ]

    def available_devices(self):
        return list(self.devices)

    def set_enabled(self, enabled, allow_remote=False):
        self.enable_calls.append(enabled)
        self.device_active = enabled
        self.settings = replace(self.settings, enabled=enabled)
        self.status = "仅本地预览。" if enabled else "摄像头已停止。"
        if not enabled:
            self.busy = False
            self.preview = QImage()
            self.preview_changed.emit(self.preview)
        self.settings_changed.emit(self.settings)
        self.status_changed.emit(self.status)
        self.runtime_changed.emit(self.runtime_snapshot())
        return not self.save_fails

    def set_remote_enabled(self, allowed):
        self.remote_calls.append(allowed)
        if allowed and self.save_fails:
            self.status = "远程许可未能保存。"
            return False
        self.settings = replace(self.settings, allow_remote=allowed)
        self.settings_changed.emit(self.settings)
        return not self.save_fails

    def select_device(self, device_id):
        self.selected_devices.append(device_id)
        if self.select_fails:
            self.status = "设备切换失败。"
            return False
        self.settings = replace(self.settings, device_id=device_id)
        self.settings_changed.emit(self.settings)
        return True

    def update_settings(self, settings):
        if self.save_fails:
            self.status = "摄像头参数保存失败。"
            return False
        self.saved_settings.append(settings)
        self.settings = settings
        self.settings_changed.emit(settings)
        return True

    @property
    def comment_available(self):
        return self.settings.enabled and self.settings.allow_remote and not self.paused and not self.busy and not self.preview.isNull()

    def observe_local_now(self):
        if not self.settings.enabled or self.busy or self.paused:
            return False
        self.observe_count += 1
        return True

    def comment_current_preview(self):
        if not self.comment_available:
            return False
        self.comment_count += 1
        self.busy = True
        self.status_changed.emit("正在识别摄像头画面。")
        return True

    def set_paused(self, paused):
        self.paused = paused
        self.device_active = self.settings.enabled and not paused
        self.paused_changed.emit(paused)
        self.runtime_changed.emit(self.runtime_snapshot())

    def runtime_snapshot(self):
        return {"enabled": self.settings.enabled, "allowRemote": self.settings.allow_remote,
                "paused": self.paused, "cameraActive": self.device_active, "deviceName": "测试摄像头"}

    def reset_session(self):
        self.reset_count += 1
        self.history = []
        self.details = {}
        self.preview = QImage()
        self.history_changed.emit([])
        self.details_changed.emit({})
        self.preview_changed.emit(QImage())


class CameraUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.controller = FakeCameraController()
        self.panel = CameraPanel(self.controller)
        self.panel.resize(690, 720)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.processEvents()

    def publish_preview(self):
        image = QImage(320, 180, QImage.Format.Format_ARGB32)
        image.fill(0xFF517CA4)
        self.controller.preview = image
        self.controller.preview_changed.emit(image)
        return image

    def allow_remote(self):
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes):
            self.panel.remote_checkbox.click()

    def test_panel_opens_with_camera_and_remote_off_without_accessing_capture(self):
        self.assertFalse(self.panel.enabled_button.isChecked())
        self.assertFalse(self.panel.remote_checkbox.isChecked())
        self.assertEqual(self.panel.enabled_button.text(), "开启摄像头")
        self.assertEqual(self.panel.runtime_label.text(), "摄像头已关闭 · 不采集画面")
        self.assertEqual(self.controller.enable_calls, [])
        self.assertEqual(self.controller.selected_devices, [])
        self.assertEqual(self.controller.observe_count, 0)
        self.assertEqual(self.controller.comment_count, 0)
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.assertFalse(self.panel.observe_button.isEnabled())
        self.assertTrue(self.panel.scroll.widgetResizable())

    def test_local_toggle_does_not_ask_remote_consent_or_invoke_api(self):
        with patch.object(QMessageBox, "warning") as consent:
            self.panel.enabled_button.click()
        consent.assert_not_called()
        self.assertEqual(self.controller.enable_calls, [True])
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertEqual(self.controller.remote_calls, [])
        self.assertEqual(self.controller.comment_count, 0)
        self.assertIn("仅本地", self.panel.runtime_label.text())
        self.assertTrue(self.panel.observe_button.isEnabled())

    def test_camera_remote_requires_its_own_confirmation_and_cancellation_stays_off(self):
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.No) as consent:
            self.panel.remote_checkbox.click()
        self.assertIn("摄像头画面", consent.call_args.args[2])
        self.assertIn("不沿用屏幕感知", consent.call_args.args[2])
        self.assertFalse(self.panel.remote_checkbox.isChecked())
        self.assertEqual(self.controller.remote_calls, [])
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertEqual(self.controller.enable_calls, [])

    def test_remote_consent_does_not_open_camera_and_revoke_needs_no_dialog(self):
        self.allow_remote()
        self.assertTrue(self.controller.settings.allow_remote)
        self.assertEqual(self.controller.enable_calls, [])
        with patch.object(QMessageBox, "warning") as consent:
            self.panel.remote_checkbox.click()
        consent.assert_not_called()
        self.assertEqual(self.controller.remote_calls, [True, False])
        self.assertFalse(self.controller.settings.allow_remote)

    def test_off_stops_busy_capture_and_clears_preview_even_when_persistence_fails(self):
        self.allow_remote()
        self.panel.enabled_button.click()
        self.publish_preview()
        self.controller.busy = True
        self.controller.save_fails = True
        with patch.object(QMessageBox, "warning") as consent:
            self.panel.enabled_button.click()
        consent.assert_not_called()
        self.assertFalse(self.controller.device_active)
        self.assertEqual(self.controller.enable_calls, [True, False])
        self.assertFalse(self.panel.enabled_button.isChecked())
        self.assertTrue(self.controller.settings.allow_remote)
        self.assertTrue(self.panel.remote_checkbox.isChecked())
        self.assertEqual(self.panel.runtime_label.text(), "摄像头已关闭 · 不采集画面")
        self.assertTrue(self.panel._preview_image.isNull())
        self.assertTrue(self.panel.feedback_label.text())

    def test_failed_remote_grant_rolls_back_ui_without_starting_capture(self):
        self.controller.save_fails = True
        self.allow_remote()
        self.assertEqual(self.controller.remote_calls, [True])
        self.assertFalse(self.panel.remote_checkbox.isChecked())
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertTrue(self.panel.feedback_label.text())
        self.assertEqual(self.controller.enable_calls, [])

    def test_device_refresh_and_selection_never_start_camera(self):
        self.panel.refresh_devices_button.click()
        self.assertEqual(self.controller.selected_devices, [])
        self.panel.device_combo.setCurrentIndex(self.panel.device_combo.findData("usb"))
        self.assertEqual(self.controller.selected_devices, ["usb"])
        self.assertEqual(self.controller.settings.device_id, "usb")
        self.assertEqual(self.controller.enable_calls, [])

    def test_failed_device_selection_restores_saved_choice(self):
        self.controller.select_fails = True
        self.panel.device_combo.setCurrentIndex(self.panel.device_combo.findData("usb"))
        self.assertEqual(self.panel.device_combo.currentData(), "")
        self.assertEqual(self.controller.settings.device_id, "")
        self.assertIn("失败", self.panel.feedback_label.text())

    def test_missing_saved_device_is_visible_without_switching_to_another(self):
        self.controller.settings = replace(self.controller.settings, device_id="unplugged")
        self.controller.devices = []
        self.panel.refresh_devices_button.click()
        self.assertEqual(self.panel.device_combo.currentData(), "unplugged")
        self.assertIn("未连接", self.panel.device_combo.currentText())
        self.assertEqual(self.controller.selected_devices, [])

    def test_preview_is_scaled_without_mutating_source_and_ignored_when_off(self):
        self.publish_preview()
        self.assertTrue(self.panel._preview_image.isNull())
        self.panel.enabled_button.click()
        image = self.publish_preview()
        self.assertEqual((image.width(), image.height()), (320, 180))
        self.assertFalse(self.panel.preview_label.pixmap().isNull())
        self.panel.preview_label.resize(180, 170)
        self.app.processEvents()
        self.assertLessEqual(self.panel.preview_label.pixmap().width(), self.panel.preview_label.contentsRect().width())
        self.assertEqual(self.panel._preview_image, image)

    def test_comment_needs_remote_and_current_preview_then_invokes_only_once(self):
        self.panel.enabled_button.click()
        self.publish_preview()
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.allow_remote()
        self.assertTrue(self.panel.comment_button.isEnabled())
        self.panel.comment_button.click()
        self.panel.comment_button.click()
        self.assertEqual(self.controller.comment_count, 1)
        self.assertEqual(self.controller.observe_count, 0)
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.assertTrue(self.panel.enabled_button.isEnabled())

    def test_pause_hides_preview_disables_actions_and_resume_remains_local(self):
        self.panel.enabled_button.click()
        self.publish_preview()
        self.panel.pause_button.click()
        self.assertTrue(self.controller.paused)
        self.assertTrue(self.panel._preview_image.isNull())
        self.assertFalse(self.panel.observe_button.isEnabled())
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.panel.pause_button.click()
        self.assertFalse(self.controller.paused)
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertEqual(self.controller.comment_count, 0)

    def test_local_refresh_does_not_grant_remote_or_comment(self):
        self.panel.enabled_button.click()
        self.panel.observe_button.click()
        self.assertEqual(self.controller.observe_count, 1)
        self.assertEqual(self.controller.comment_count, 0)
        self.assertFalse(self.controller.settings.allow_remote)

    def test_parameter_save_preserves_device_and_permissions_without_capture(self):
        self.panel.device_combo.setCurrentIndex(self.panel.device_combo.findData("usb"))
        self.panel.analysis_interval_input.setValue(90)
        self.panel.speech_cooldown_input.setValue(300)
        self.panel.max_speeches_input.setValue(3)
        self.panel.commentary_mode_combo.setCurrentIndex(self.panel.commentary_mode_combo.findData("quiet"))
        self.panel.save_button.click()
        settings = self.controller.settings
        self.assertEqual(settings.device_id, "usb")
        self.assertEqual(settings.analysis_interval_seconds, 90)
        self.assertEqual(settings.speech_cooldown_seconds, 300)
        self.assertEqual(settings.max_speeches_per_hour, 3)
        self.assertEqual(settings.commentary_mode, "quiet")
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.allow_remote)
        self.assertEqual(self.controller.enable_calls, [])

    def test_details_and_bounded_event_history_remain_plain_text(self):
        self.controller.details_changed.emit({"device_name": "<b>摄像头</b>", "reason": "第一行\n第二行"})
        self.assertIn("<b>摄像头</b>", self.panel.details_view.toPlainText())
        self.assertIn("第一行 第二行", self.panel.details_view.toPlainText())
        self.controller.history_changed.emit([{"event": f"event-{index}"} for index in range(45)])
        lines = self.panel.history_view.toPlainText().splitlines()
        self.assertEqual(len(lines), 30)
        self.assertIn("event-15", lines[0])
        self.assertIn("event-44", lines[-1])
        self.assertEqual(self.panel.status_label.textFormat(), Qt.TextFormat.PlainText)

    def test_reset_requires_confirmation_and_does_not_change_camera_permissions(self):
        self.allow_remote()
        self.panel.enabled_button.click()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.panel.reset_button.click()
        self.assertEqual(self.controller.reset_count, 0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel.reset_button.click()
        self.assertEqual(self.controller.reset_count, 1)
        self.assertTrue(self.controller.settings.enabled)
        self.assertTrue(self.controller.settings.allow_remote)


if __name__ == "__main__":
    unittest.main()
