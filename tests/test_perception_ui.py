"""Perception UI tests use synthetic frames and a controller without networking."""

from dataclasses import dataclass, replace
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop_pet.ui.perception_panel import PerceptionPanel


@dataclass(frozen=True)
class FakePerceptionSettings:
    enabled: bool = False
    allow_remote: bool = False
    commentary_mode: str = "companion"
    observation_interval_seconds: int = 10
    analysis_interval_seconds: int = 60
    change_threshold: float = 0.08
    speech_cooldown_seconds: int = 180
    max_speeches_per_hour: int = 6
    idle_pause_seconds: int = 600
    privacy_keywords: tuple = ("密码",)
    excluded_apps: tuple = ("password-manager.exe",)


class FakePerceptionController(QObject):
    settings_changed = Signal(object)
    status_changed = Signal(str)
    details_changed = Signal(object)
    preview_changed = Signal(QImage)
    history_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.settings = FakePerceptionSettings()
        self.status = "本地观察未启用。"
        self.details = {}
        self.history = []
        self.preview = QImage()
        self.busy = False
        self.saved_settings = []
        self.observe_count = 0
        self.comment_count = 0
        self.reset_count = 0
        self._closed = False
        self._local_observation_pending = False

    @property
    def comment_available(self):
        return (not self._closed and self.settings.enabled and self.settings.allow_remote
                and not self.busy and not self._local_observation_pending
                and not self.preview.isNull())

    def comment_current_preview(self):
        if not self.comment_available:
            return False
        self.comment_count += 1
        self.busy = True
        self.status = "正在请求画面评论。"
        self.status_changed.emit(self.status)
        return True

    def update_settings(self, settings):
        self.cancel_local_observation()
        self.saved_settings.append(settings)
        self.settings = settings
        self.settings_changed.emit(settings)
        self.status = "参数已保存。"
        self.status_changed.emit(self.status)
        return True

    def revoke_permissions(self, *, local=False, remote=False):
        return self.update_settings(replace(
            self.settings,
            enabled=False if local else self.settings.enabled,
            allow_remote=False if remote else self.settings.allow_remote,
        ))

    def begin_local_observation(self):
        if self._closed or not self.settings.enabled or self.busy or self._local_observation_pending:
            return False
        self._local_observation_pending = True
        return True

    def cancel_local_observation(self):
        self._local_observation_pending = False

    def observe_now(self):
        self.cancel_local_observation()
        if not self.settings.enabled or self._closed:
            return False
        self.observe_count += 1
        self.status = "已完成本地观察，未发送到 API。"
        self.status_changed.emit(self.status)
        return True

    def shutdown(self):
        self._closed = True
        self.cancel_local_observation()
        self.status = "屏幕感知已停止。"
        self.status_changed.emit(self.status)

    def reset_session(self):
        self.cancel_local_observation()
        self.reset_count += 1
        self.history = []
        self.details = {}
        self.preview = QImage()
        self.history_changed.emit([])
        self.details_changed.emit({})
        self.preview_changed.emit(QImage())


class PerceptionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.controller = FakePerceptionController()
        self.panel = PerceptionPanel(self.controller)
        self.panel.resize(700, 700)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.processEvents()

    def enable_observation(self, allow_remote=False):
        self.controller.update_settings(replace(self.controller.settings, enabled=True,
                                                allow_remote=allow_remote))

    def finish_countdown(self):
        for _ in range(3):
            self.panel._advance_observation_countdown()

    def publish_safe_preview(self):
        image = QImage(80, 40, QImage.Format.Format_ARGB32)
        image.fill(0xFF336699)
        self.controller.preview = image
        self.controller.preview_changed.emit(image)

    def test_defaults_are_off_and_opening_panel_does_not_observe(self):
        self.assertFalse(self.panel.enabled_checkbox.isChecked())
        self.assertFalse(self.panel.remote_checkbox.isChecked())
        self.assertEqual(self.controller.observe_count, 0)
        self.assertEqual(self.controller.saved_settings, [])
        self.assertIn("未启用", self.panel.status_label.text())
        self.assertTrue(self.panel.scroll.widgetResizable())
        self.assertFalse(self.panel.observe_button.isEnabled())
        self.assertFalse(self.panel.comment_button.isEnabled())

    def test_commentary_mode_saves_and_restores_without_starting_requests(self):
        combo = self.panel.commentary_mode_combo
        self.assertEqual(combo.currentData(), "companion")
        self.assertEqual([combo.itemData(i) for i in range(combo.count())], ["companion", "quiet"])
        combo.setCurrentIndex(combo.findData("quiet"))
        self.panel.save_button.click()
        self.assertEqual(self.controller.settings.commentary_mode, "quiet")
        reopened = PerceptionPanel(self.controller)
        try:
            self.assertEqual(reopened.commentary_mode_combo.currentData(), "quiet")
        finally:
            reopened.deleteLater()
        self.controller.update_settings(replace(self.controller.settings, commentary_mode="companion"))
        self.assertEqual(combo.currentData(), "companion")
        self.assertEqual(self.controller.observe_count, 0)
        self.assertEqual(self.controller.comment_count, 0)

    def test_comment_button_requires_saved_permissions_and_safe_preview(self):
        self.publish_safe_preview()
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.panel._comment_current_preview()
        self.assertIn("暂不能评论", self.panel.feedback_label.text())
        self.assertEqual(self.controller.saved_settings, [])
        self.assertFalse(self.controller.settings.enabled)
        self.assertFalse(self.controller.settings.allow_remote)
        self.enable_observation(allow_remote=False)
        self.assertFalse(self.panel.comment_button.isEnabled())
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes):
            self.panel.remote_checkbox.click()
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.panel.save_button.click()
        self.assertTrue(self.panel.comment_button.isEnabled())
        self.controller.preview = QImage()
        self.controller.preview_changed.emit(QImage())
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.assertEqual(self.controller.observe_count, 0)
        self.assertEqual(self.controller.comment_count, 0)

    def test_comment_invokes_once_without_capture_or_permission_changes(self):
        self.enable_observation(allow_remote=True)
        self.publish_safe_preview()
        settings = self.controller.settings
        saved_count = len(self.controller.saved_settings)
        self.assertIn("API", self.panel.comment_button.toolTip())
        self.assertIn("不重新截图", self.panel.comment_button.toolTip())
        self.panel.comment_button.click()
        self.panel.comment_button.click()
        self.panel._comment_current_preview()
        self.assertEqual(self.controller.comment_count, 1)
        self.assertEqual(self.controller.observe_count, 0)
        self.assertIs(self.controller.settings, settings)
        self.assertEqual(len(self.controller.saved_settings), saved_count)
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.assertTrue(self.panel.save_button.isEnabled())
        self.assertTrue(self.panel.commentary_mode_combo.isEnabled())
        self.controller.busy = False
        self.controller.status_changed.emit("评论完成。")
        self.assertTrue(self.panel.comment_button.isEnabled())

    def test_comment_waits_for_local_countdown_and_honors_immediate_opt_out(self):
        self.enable_observation(allow_remote=True)
        self.publish_safe_preview()
        self.panel.observe_button.click()
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.panel._comment_current_preview()
        self.assertEqual(self.controller.comment_count, 0)
        self.finish_countdown()
        self.assertTrue(self.panel.comment_button.isEnabled())
        self.panel.remote_checkbox.setChecked(False)
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.assertFalse(self.controller.settings.allow_remote)
        self.panel.enabled_checkbox.setChecked(False)
        self.assertFalse(self.panel.comment_button.isEnabled())
        self.assertEqual(self.controller.observe_count, 1)
        self.assertEqual(self.controller.comment_count, 0)

    def test_comment_request_errors_do_not_reveal_raw_exception_text(self):
        self.enable_observation(allow_remote=True)
        self.publish_safe_preview()
        with patch.object(self.controller, "comment_current_preview",
                          side_effect=RuntimeError("private-key-and-window-title")):
            self.panel.comment_button.click()
        self.assertIn("未能请求画面评论", self.panel.feedback_label.text())
        self.assertNotIn("private-key-and-window-title", self.panel.feedback_label.text())

    def test_event_actions_and_quiet_reasons_have_plain_chinese_explanations(self):
        self.controller.history_changed.emit([
            {"动作": "quiet", "原因": "no_actionable_event", "情境": "<img src=x>"},
            {"action": "speak", "reason": "companion_event"},
            {"action": "motion", "reason": "cooldown"},
            {"action": "quiet", "reason": "unknown-provider-reason"},
        ])
        text = self.panel.history_view.toPlainText()
        self.assertIn("保持安静（quiet）", text)
        self.assertIn("模型未发现适合互动的内容（no_actionable_event）", text)
        self.assertIn("发表评论（speak）", text)
        self.assertIn("适合陪伴评论的内容（companion_event）", text)
        self.assertIn("主动发言冷却中（cooldown）", text)
        self.assertIn("unknown-provider-reason", text)
        self.assertIn("<img src=x>", text)
        self.assertEqual(self.panel.status_label.textFormat(), Qt.TextFormat.PlainText)

    def test_remote_requires_explicit_confirmation_with_clear_disclosure(self):
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.No) as warning:
            self.panel.remote_checkbox.click()
        self.assertFalse(self.panel.remote_checkbox.isChecked())
        disclosure = warning.call_args.args[2]
        self.assertIn("截图", disclosure)
        self.assertIn("窗口标题", disclosure)
        self.assertIn("无法保证", disclosure)
        self.assertEqual(warning.call_args.args[-1], QMessageBox.StandardButton.No)
        self.assertEqual(self.controller.saved_settings, [])
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes):
            self.panel.remote_checkbox.click()
        self.assertTrue(self.panel.remote_checkbox.isChecked())
        self.assertFalse(self.controller.settings.allow_remote)
        self.panel.save_button.click()
        self.assertTrue(self.controller.settings.allow_remote)

    def test_all_numeric_bounds_and_saved_values(self):
        for control, limits in (
            (self.panel.observation_interval_input, (5, 30)),
            (self.panel.analysis_interval_input, (30, 600)),
            (self.panel.change_threshold_input, (0.01, 1.0)),
            (self.panel.speech_cooldown_input, (30, 1800)),
            (self.panel.max_speeches_input, (1, 30)),
            (self.panel.idle_pause_input, (60, 7200)),
        ):
            self.assertEqual((control.minimum(), control.maximum()), limits)
            control.setValue(limits[1])
        self.panel.enabled_checkbox.setChecked(True)
        self.panel.save_button.click()
        settings = self.controller.settings
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.observation_interval_seconds, 30)
        self.assertEqual(settings.analysis_interval_seconds, 600)
        self.assertEqual(settings.change_threshold, 1.0)
        self.assertEqual(settings.speech_cooldown_seconds, 1800)
        self.assertEqual(settings.max_speeches_per_hour, 30)
        self.assertEqual(settings.idle_pause_seconds, 7200)

    def test_keywords_and_processes_accept_comma_and_newline(self):
        self.panel.privacy_keywords_input.setPlainText(" 密码,银行卡\n密码，Private\nprivate\n")
        self.panel.excluded_apps_input.setPlainText("secret.exe, vault.exe\nSECRET.EXE")
        self.panel.save_button.click()
        self.assertEqual(self.controller.settings.privacy_keywords, ("密码", "银行卡", "Private"))
        self.assertEqual(self.controller.settings.excluded_apps, ("secret.exe", "vault.exe"))

    def test_external_settings_sync_does_not_prompt_or_save_again(self):
        settings = replace(self.controller.settings, enabled=True, allow_remote=True,
                           observation_interval_seconds=15, privacy_keywords=("隐私",))
        with patch.object(QMessageBox, "warning") as warning:
            self.controller.settings_changed.emit(settings)
        warning.assert_not_called()
        self.assertTrue(self.panel.enabled_checkbox.isChecked())
        self.assertTrue(self.panel.remote_checkbox.isChecked())
        self.assertEqual(self.panel.observation_interval_input.value(), 15)
        self.assertEqual(self.panel.privacy_keywords_input.toPlainText(), "隐私")
        self.assertEqual(self.controller.saved_settings, [])

    def test_local_observe_waits_for_window_switch_and_runs_once_without_remote_change(self):
        self.enable_observation(allow_remote=True)
        saved_count = len(self.controller.saved_settings)
        self.panel.observe_button.click()
        self.assertEqual(self.controller.observe_count, 0)
        self.assertIn("3 秒", self.panel.observe_button.text())
        self.assertFalse(self.panel.observe_button.isEnabled())
        self.panel._observe_now()  # Repeated signals cannot start another countdown.
        self.panel._advance_observation_countdown()
        self.assertIn("2 秒", self.panel.observe_button.text())
        self.assertEqual(self.controller.observe_count, 0)
        self.finish_countdown()
        self.assertEqual(self.controller.observe_count, 1)
        self.assertIn("未发送", self.panel.status_label.text())
        self.assertTrue(self.controller.settings.allow_remote)
        self.assertEqual(len(self.controller.saved_settings), saved_count)
        self.assertFalse(self.panel._observation_timer.isActive())
        self.assertTrue(self.panel.observe_button.isEnabled())

    def test_local_observe_timer_actually_fires_without_blocking(self):
        self.enable_observation()
        self.panel._observation_timer.setInterval(10)
        self.panel.observe_button.click()
        # Windows can coalesce short timer events while earlier windows finish
        # painting. Wait for actual completion with a finite deadline instead
        # of assuming three ticks always fit a 70ms wall-clock sleep.
        deadline = time.monotonic() + 1.5
        while self.controller.observe_count == 0 and time.monotonic() < deadline:
            QTest.qWait(10)
            time.sleep(0.001)
        self.assertEqual(self.controller.observe_count, 1)
        self.assertFalse(self.panel._observation_timer.isActive())

    def test_disabled_observation_cannot_be_implicitly_enabled_by_button(self):
        self.panel._observe_now()
        self.finish_countdown()
        self.assertEqual(self.controller.observe_count, 0)
        self.assertFalse(self.panel._observation_timer.isActive())
        self.assertEqual(self.controller.saved_settings, [])
        self.assertIn("启用本地观察并保存", self.panel.feedback_label.text())
        self.assertFalse(self.controller.settings.allow_remote)

    def test_busy_analysis_blocks_and_cancels_queued_local_observation(self):
        self.enable_observation()
        self.panel.observe_button.click()
        self.controller.busy = True
        self.controller.status_changed.emit("分析进行中")
        self.assertFalse(self.panel.observe_button.isEnabled())
        self.panel._observe_now()
        self.finish_countdown()
        self.assertEqual(self.controller.observe_count, 0)
        self.assertFalse(self.panel._observation_timer.isActive())
        self.assertTrue(self.panel.save_button.isEnabled())
        self.controller.busy = False
        self.controller.status_changed.emit("就绪")
        self.assertTrue(self.panel.observe_button.isEnabled())

    def test_existing_analysis_refuses_reservation_with_explicit_hint(self):
        self.enable_observation(allow_remote=True)
        self.controller.busy = True
        self.controller.status_changed.emit("分析进行中")
        self.panel._observe_now()
        self.assertFalse(self.panel._observation_timer.isActive())
        self.assertFalse(self.controller._local_observation_pending)
        self.assertIn("等待完成", self.panel.feedback_label.text())
        self.assertIn("等待完成", self.panel.observe_button.toolTip())

    def test_failed_save_cancels_local_countdown_and_releases_reservation(self):
        self.enable_observation()
        self.panel.observe_button.click()
        self.assertTrue(self.controller._local_observation_pending)
        with patch.object(self.controller, "update_settings", return_value=False):
            self.panel.save_button.click()
        self.assertFalse(self.panel._observation_timer.isActive())
        self.assertFalse(self.controller._local_observation_pending)
        self.finish_countdown()
        self.assertEqual(self.controller.observe_count, 0)

    def test_disabling_observation_cancels_countdown_without_late_capture(self):
        self.enable_observation()
        self.panel.observe_button.click()
        self.panel.enabled_checkbox.setChecked(False)
        self.finish_countdown()
        self.assertEqual(self.controller.observe_count, 0)
        self.assertFalse(self.panel._observation_timer.isActive())
        self.panel.enabled_checkbox.setChecked(True)
        self.panel.observe_button.click()
        self.controller.update_settings(replace(self.controller.settings, enabled=False))
        self.finish_countdown()
        self.assertEqual(self.controller.observe_count, 0)
        self.assertFalse(self.panel.observe_button.isEnabled())

    def test_hidden_panel_keeps_countdown_but_shutdown_controller_refuses_capture(self):
        self.enable_observation()
        self.panel.observe_button.click()
        self.panel.close()
        self.assertTrue(self.panel._observation_timer.isActive())
        self.finish_countdown()
        self.assertEqual(self.controller.observe_count, 1)
        self.panel.show()
        self.panel.observe_button.click()
        self.controller.shutdown()
        self.finish_countdown()
        self.assertEqual(self.controller.observe_count, 1)

    def test_details_and_history_are_plain_text_with_thirty_event_limit(self):
        self.controller.details_changed.emit({"title": "已过滤", "reason": "<b>纯文本</b>\n第二行"})
        self.assertIn("<b>纯文本</b>", self.panel.details_view.toPlainText())
        self.assertIn("窗口标题：已过滤", self.panel.details_view.toPlainText())
        history = [{"event": f"event-{i}", "summary": "<img src=x>\n第二行"} for i in range(35)]
        self.controller.history_changed.emit(history)
        text = self.panel.history_view.toPlainText()
        self.assertEqual(self.panel.history_view.document().blockCount(), 30)
        self.assertNotIn("event-4 |", text)
        self.assertIn("event-5 |", text)
        self.assertIn("event-34 |", text)
        self.assertIn("<img src=x>", text)

    def test_synthetic_preview_preserves_ratio_and_can_be_cleared(self):
        image = QImage(400, 200, QImage.Format.Format_ARGB32)
        image.fill(0xFF336699)
        self.controller.preview_changed.emit(image)
        pixmap = self.panel.preview_label.pixmap()
        self.assertFalse(pixmap.isNull())
        self.assertAlmostEqual(pixmap.width() / pixmap.height(), 2, places=1)
        self.assertLessEqual(pixmap.width(), self.panel.preview_label.width())
        self.assertLessEqual(pixmap.height(), self.panel.preview_label.height())
        self.panel.preview_label.resize(120, 160)
        self.assertLessEqual(self.panel.preview_label.pixmap().width(), 120)
        self.assertAlmostEqual(self.panel.preview_label.pixmap().width() /
                               self.panel.preview_label.pixmap().height(), 2, places=1)
        self.controller.preview_changed.emit(QImage())
        self.assertTrue(self.panel.preview_label.pixmap().isNull())
        self.assertIn("尚无", self.panel.preview_label.text())

    def test_retained_preview_is_clearly_identified_and_not_presented_as_live(self):
        image = QImage(40, 40, QImage.Format.Format_ARGB32)
        image.fill(0xFF336699)
        self.controller.preview_changed.emit(image)
        self.controller.details_changed.emit({"reason": "AURA 自身窗口已跳过", "preview_retained": True})
        self.assertFalse(self.panel.preview_label.pixmap().isNull())
        self.assertIn("上次本地预览", self.panel.preview_status_label.text())
        self.assertIn("不是面板的实时画面", self.panel.preview_status_label.text())
        self.assertNotIn("preview_retained", self.panel.details_view.toPlainText())
        self.controller.preview_changed.emit(QImage())
        self.assertIn("尚无", self.panel.preview_status_label.text())

    def test_first_open_loads_the_existing_safe_preview_from_controller(self):
        image = QImage(80, 40, QImage.Format.Format_ARGB32)
        image.fill(0xFF336699)
        self.controller.preview = image
        self.controller.details = {"preview_retained": True, "title": "Synthetic external window"}
        new_panel = PerceptionPanel(self.controller)
        try:
            self.assertFalse(new_panel.preview_label.pixmap().isNull())
            self.assertIn("上次本地预览", new_panel.preview_status_label.text())
            self.assertIn("Synthetic external window", new_panel.details_view.toPlainText())
            self.assertEqual(self.controller.observe_count, 0)
        finally:
            new_panel.deleteLater()

    def test_reset_session_requires_confirmation_and_clears_preview(self):
        image = QImage(40, 40, QImage.Format.Format_ARGB32)
        image.fill(0xFF336699)
        self.controller.preview_changed.emit(image)
        self.enable_observation()
        self.panel.observe_button.click()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.panel.reset_button.click()
        self.assertEqual(self.controller.reset_count, 0)
        self.assertTrue(self.panel._observation_timer.isActive())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel.reset_button.click()
        self.assertEqual(self.controller.reset_count, 1)
        self.finish_countdown()
        self.assertFalse(self.panel._observation_timer.isActive())
        self.assertEqual(self.controller.observe_count, 0)
        self.assertTrue(self.panel.preview_label.pixmap().isNull())
        self.assertEqual(self.panel.history_view.toPlainText(), "")

    def test_controller_failures_do_not_reveal_raw_exception_data(self):
        private = "private-window-title"
        with patch.object(self.controller, "update_settings", side_effect=RuntimeError(private)):
            self.panel.save_button.click()
        self.assertFalse(self.panel.feedback_label.isHidden())
        self.assertNotIn(private, self.panel.feedback_label.text())
        self.enable_observation()
        with patch.object(self.controller, "observe_now", side_effect=RuntimeError(private)):
            self.panel.observe_button.click()
            self.finish_countdown()
        self.assertFalse(self.panel.feedback_label.isHidden())
        self.assertNotIn(private, self.panel.feedback_label.text())


if __name__ == "__main__":
    unittest.main()
