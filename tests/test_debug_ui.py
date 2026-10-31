"""Exercise UI wiring without making external requests or storing real secrets."""

import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox, QPushButton

from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.debug_panel import ChatWindow, DebugPanel


class FakePet(QObject):
    scale_changed = Signal(int)
    expression_changed = Signal(str, str)
    error_occurred = Signal(str)

    def __init__(self):
        super().__init__()
        self.scale_percent = 100
        self.expression = "neutral"
        self.expression_status = "正常使用原始鲸鱼娘 PNG。"
        self.test_count = 0

    def expression_catalog(self):
        return [(code, label, "素材尚未提供，仅反馈动作或气泡") for code, label in (
            ("neutral", "正常"), ("happy", "开心"), ("surprised", "惊讶"),
            ("sleepy", "困倦"), ("angry", "生气"), ("thinking", "思考"), ("talking", "说话"),
        )]

    def set_scale(self, percent):
        self.scale_percent = percent
        self.scale_changed.emit(percent)

    def reset_scale(self):
        self.set_scale(100)

    def set_expression(self, expression):
        self.expression = expression
        self.expression_status = "素材尚未提供，仅反馈动作或气泡"
        self.expression_changed.emit(expression, self.expression_status)

    def test_expression(self):
        self.test_count += 1


class FakeSettings:
    credentials_available = True
    credential_status = "系统凭据库可用。"
    config_path = "test-only.ini"

    def __init__(self):
        self.config = ApiSettings(base_url="https://example.test/v1", model="test-model", timeout_seconds=10)
        self.key = "unit-test-key-not-a-real-secret"
        self.deleted = False

    def load_api_settings(self):
        return self.config

    def save_api_settings(self, settings, *, pending_api_key=""):
        self.config = settings

    def save_api_key(self, key):
        if not self.credentials_available:
            raise RuntimeError("系统凭据库不可用，密钥未保存。")
        self.key = key

    def get_api_key(self):
        return self.key

    def clear_api_key(self):
        self.deleted = True
        self.key = ""


class FakeChat(QObject):
    messages_changed = Signal()
    busy_changed = Signal(bool)
    error_changed = Signal(str)
    emotion_requested = Signal(str)
    connection_status_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.messages = []
        self.busy = False
        self.test_calls = []

    def send(self, text):
        if self.busy:
            return False
        self.messages.append({"role": "user", "content": text})
        self.messages_changed.emit()
        return True

    def clear(self):
        self.messages.clear()
        self.messages_changed.emit()

    def test_connection(self, settings, key):
        self.test_calls.append((settings, key))
        self.connection_status_changed.emit("连接成功")
        return True


class DebugUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.pet = FakePet()
        self.settings = FakeSettings()
        self.chat = FakeChat()
        self.panel = DebugPanel(self.pet, self.settings, self.chat)
        self.window = ChatWindow(self.chat)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.hide()
        self.window.hide()
        self.panel.deleteLater()
        self.window.deleteLater()
        self.app.processEvents()

    def test_scale_presets_slider_reset_and_controller_sync(self):
        self.panel.scale_slider.setValue(137)
        self.assertEqual(self.pet.scale_percent, 137)
        self.assertEqual(self.panel.scale_label.text(), "137%")
        self.panel.findChild(QPushButton, "scalePreset200").click()
        self.assertEqual(self.pet.scale_percent, 200)
        self.panel.findChild(QPushButton, "resetScaleButton").click()
        self.assertEqual(self.pet.scale_percent, 100)
        self.pet.set_scale(75)
        self.assertEqual(self.panel.scale_slider.value(), 75)

    def test_expression_feedback_test_button_and_external_sync(self):
        self.panel.expression_combo.setCurrentIndex(self.panel.expression_combo.findData("angry"))
        self.assertEqual(self.pet.expression, "angry")
        self.assertIn("素材尚未提供", self.panel.expression_status.text())
        self.panel.findChild(QPushButton, "testExpressionButton").click()
        self.assertEqual(self.pet.test_count, 1)
        self.pet.set_expression("thinking")
        self.assertEqual(self.panel.expression_combo.currentData(), "thinking")

    def test_pet_persistence_failure_is_visible_and_next_change_clears_it(self):
        self.pet.error_occurred.emit("大小已调整，但未能保存到本地；请检查配置目录权限。")
        self.assertFalse(self.panel.pet_error_label.isHidden())
        self.assertIn("未能保存", self.panel.pet_error_label.text())
        self.pet.set_scale(125)
        self.assertTrue(self.panel.pet_error_label.isHidden())

    def test_close_hides_without_reset_and_reopen_keeps_state(self):
        self.pet.set_scale(150)
        self.pet.set_expression("happy")
        self.panel.close()
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.pet.scale_percent, 150)
        self.panel.present()
        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.panel.expression_combo.currentData(), "happy")
        self.window.show()
        self.window.close()
        self.assertFalse(self.window.isVisible())

    def test_api_credential_summary_distinguishes_presence_from_connectivity(self):
        self.assertIn("聊天密钥已保存", self.panel.credential_label.text())
        self.assertNotIn(self.settings.key, self.panel.credential_label.text())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel.clear_key_button.click()
        self.assertIn("尚未保存聊天密钥", self.panel.credential_label.text())
        self.assertNotIn("连接成功", self.panel.credential_label.text())

    def test_api_credential_read_failure_is_static_and_does_not_echo_backend(self):
        with patch.object(self.settings, "get_api_key", side_effect=RuntimeError("backend-secret-test-payload")):
            self.panel._refresh_credential_status()
        self.assertIn("无法读取", self.panel.credential_label.text())
        self.assertNotIn("backend-secret-test-payload", self.panel.credential_label.text())

    def test_empty_enter_shift_enter_and_busy_block_duplicate(self):
        panel = self.panel.chat_panel
        self.assertFalse(panel.send_button.isEnabled())
        panel.input.setPlainText(" \n ")
        QTest.keyClick(panel.input, Qt.Key.Key_Return)
        self.assertEqual(self.chat.messages, [])
        panel.input.setPlainText("第一行")
        QTest.keyClick(panel.input, Qt.Key.Key_End)
        QTest.keyClick(panel.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        self.assertIn("\n", panel.input.toPlainText())
        QTest.keyClick(panel.input, Qt.Key.Key_Return)
        self.assertEqual(len(self.chat.messages), 1)
        self.assertEqual(panel.input.toPlainText(), "")
        self.chat.busy = True
        self.chat.busy_changed.emit(True)
        panel.input.setPlainText("第二次")
        self.assertFalse(panel.send_button.isEnabled())
        QTest.keyClick(panel.input, Qt.Key.Key_Return)
        self.assertEqual(len(self.chat.messages), 1)
        self.assertFalse(self.panel.test_connection_button.isEnabled())

    def test_two_views_sync_and_escape_provider_html(self):
        self.panel.tabs.setCurrentWidget(self.panel.chat_panel)
        self.window.show()
        for view in (self.panel.chat_panel, self.window.chat_panel):
            view._activate_native()
            self.assertTrue(view.transcript.isVisible())
        payload = '<img src="https://invalid.test/image.png"><b>纯文本</b>'
        self.chat.messages = [{"role": "assistant", "content": payload}]
        self.chat.messages_changed.emit()
        for view in (self.panel.chat_panel, self.window.chat_panel):
            self.assertIn(payload, view.transcript.toPlainText())
            self.assertFalse(view.transcript.openExternalLinks())
            self.assertNotIn('<img src="https://invalid.test', view.transcript.toHtml())

    def test_enter_during_chinese_input_composition_does_not_send(self):
        panel = self.panel.chat_panel
        panel.input.setPlainText("正在输入")
        self.app.sendEvent(panel.input, QInputMethodEvent("ni", []))
        QTest.keyClick(panel.input, Qt.Key.Key_Return)
        self.assertEqual(self.chat.messages, [])
        self.app.sendEvent(panel.input, QInputMethodEvent("", []))
        QTest.keyClick(panel.input, Qt.Key.Key_Return)
        self.assertEqual(len(self.chat.messages), 1)

    def test_clear_requires_confirmation_and_syncs(self):
        self.panel.tabs.setCurrentWidget(self.panel.chat_panel)
        self.window.show()
        for view in (self.panel.chat_panel, self.window.chat_panel):
            view._activate_native()
            self.assertTrue(view.transcript.isVisible())
        self.chat.send("你好")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.panel.chat_panel.clear_button.click()
        self.assertEqual(len(self.chat.messages), 1)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window.chat_panel.clear_button.click()
        self.assertEqual(self.chat.messages, [])
        self.assertIn("还没有消息", self.panel.chat_panel.transcript.toPlainText())

    def test_empty_key_save_keeps_credential_and_clear_deletes(self):
        original = self.settings.key
        self.panel.model_input.setText("updated-model")
        self.panel.save_api_button.click()
        self.assertEqual(self.settings.key, original)
        self.assertEqual(self.settings.config.model, "updated-model")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel.clear_key_button.click()
        self.assertTrue(self.settings.deleted)
        self.assertEqual(self.settings.key, "")

    def test_unsaved_key_test_request_and_config_revision(self):
        self.panel.key_input.setText("temporary-test-value")
        self.panel.test_connection_button.click()
        self.assertEqual(self.chat.test_calls[0][1], "temporary-test-value")
        self.assertNotEqual(self.settings.key, "temporary-test-value")
        self.assertIn("连接成功", self.panel.connection_label.text())
        self.panel.base_url_input.setText("https://other.test/v1")
        self.assertIn("尚未测试", self.panel.connection_label.text())
        self.chat.connection_status_changed.emit("连接成功")
        self.assertIn("此结果对应测试发起时", self.panel.connection_label.text())

    def test_clearing_saved_key_marks_pending_result_as_old_configuration(self):
        self.assertEqual(self.panel.key_input.text(), "")
        with patch.object(self.chat, "test_connection", return_value=True):
            self.panel.test_connection_button.click()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel.clear_key_button.click()
        self.assertEqual(self.settings.key, "")
        self.assertIn("已删除", self.panel.connection_label.text())
        self.chat.connection_status_changed.emit("连接成功")
        self.assertIn("此结果对应测试发起时", self.panel.connection_label.text())
        self.assertIn("密钥已修改", self.panel.connection_label.text())

    def test_credential_read_failure_replaces_previous_connection_success(self):
        self.panel.test_connection_button.click()
        self.assertIn("连接成功", self.panel.connection_label.text())
        self.settings.credential_status = "无法读取系统凭据中的 API Key。"
        with patch.object(self.settings, "get_api_key", side_effect=RuntimeError("无法读取系统凭据")):
            self.panel.test_connection_button.click()
        self.assertIn("连接未测试", self.panel.connection_label.text())
        self.assertNotIn("连接成功", self.panel.connection_label.text())
        self.assertIn("无法读取", self.panel.api_error_label.text())
        self.assertIn("无法读取", self.panel.credential_label.text())

    def test_api_field_edit_clears_error_for_previous_configuration(self):
        self.chat.error_changed.emit("旧配置的认证失败")
        self.assertFalse(self.panel.api_error_label.isHidden())
        self.panel.model_input.setText("corrected-model")
        self.assertTrue(self.panel.api_error_label.isHidden())
        self.assertIn("尚未测试当前配置", self.panel.connection_label.text())

    def test_credential_delete_failure_never_reports_deleted_or_success(self):
        self.panel.test_connection_button.click()
        with patch.object(self.settings, "clear_api_key", side_effect=RuntimeError("删除失败")), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel.clear_key_button.click()
        self.assertIn("无法确认", self.panel.connection_label.text())
        self.assertNotIn("连接成功", self.panel.connection_label.text())
        self.assertIn("删除失败", self.panel.api_error_label.text())
        self.assertNotEqual(self.settings.key, "")

    def test_cancelling_credential_deletion_preserves_saved_and_draft_key(self):
        original = self.settings.key
        self.panel.key_input.setText("synthetic-draft-not-a-secret")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.panel.clear_key_button.click()
        self.assertEqual(self.settings.key, original)
        self.assertEqual(self.panel.key_input.text(), "synthetic-draft-not-a-secret")
        self.assertFalse(self.settings.deleted)

    def test_credential_unavailable_is_visible_and_key_not_written(self):
        previous = self.settings.key
        self.settings.credentials_available = False
        self.settings.credential_status = "系统凭据库不可用，不能保存密钥。"
        self.panel.key_input.setText("cannot-save-test-value")
        self.panel.save_api_button.click()
        self.assertIn("不可用", self.panel.api_error_label.text())
        self.assertIn("普通配置已保存，密钥未保存", self.panel.connection_label.text())
        self.assertIn("普通配置已保存，密钥未保存", self.panel.api_error_label.text())
        self.assertNotIn("cannot-save-test-value", self.panel.api_error_label.text())
        self.assertEqual(self.settings.key, previous)
        self.assertIn("不可用", self.panel.credential_label.text())
        self.assertIn("输入临时密钥测试连接", self.panel.credential_label.text())
        self.panel.test_connection_button.click()
        self.assertEqual(self.chat.test_calls[0][1], "cannot-save-test-value")

    def test_key_visibility_and_successful_save_clear_field(self):
        self.panel.key_input.setText("new-unit-test-value")
        self.assertEqual(self.panel.show_key_button.text(), "显示")
        self.panel.show_key_button.click()
        self.assertEqual(self.panel.show_key_button.text(), "隐藏")
        self.panel.save_api_button.click()
        self.assertEqual(self.settings.key, "new-unit-test-value")
        self.assertEqual(self.panel.key_input.text(), "")
        self.assertFalse(self.panel.show_key_button.isChecked())

    def test_close_remasks_temporary_key_without_losing_draft_or_pet_state(self):
        self.panel.key_input.setText("temporary-test-value")
        self.panel.show_key_button.click()
        self.assertEqual(self.panel.key_input.echoMode(), QLineEdit.EchoMode.Normal)
        self.pet.set_scale(125)
        self.pet.set_expression("thinking")
        self.panel.close()
        self.panel.present()
        self.assertEqual(self.panel.key_input.text(), "temporary-test-value")
        self.assertEqual(self.panel.key_input.echoMode(), QLineEdit.EchoMode.Password)
        self.assertFalse(self.panel.show_key_button.isChecked())
        self.assertEqual(self.pet.scale_percent, 125)
        self.assertEqual(self.pet.expression, "thinking")


if __name__ == "__main__":
    unittest.main()
