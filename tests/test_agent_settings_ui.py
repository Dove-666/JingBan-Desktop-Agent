"""Agent settings UI with an in-memory store/vault and synthetic Qt events."""

from dataclasses import asdict, replace
import os
import subprocess
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGroupBox, QLineEdit, QPushButton, QSpinBox

from desktop_pet.agents import AgentSettings
from desktop_pet.ui.agent_panel import AgentSettingsPanel


KEY = "new-local-ui-test-secret"
OLD_KEY = "saved-local-ui-test-secret"


class MemorySettings:
    def __init__(self):
        self.settings = AgentSettings()
        self.gui_key = OLD_KEY
        self.calls = []
        self.key_reads = 0
        self.fail_public = False
        self.fail_key = False
        self.fail_status = False

    def load_agent_settings(self):
        return self.settings

    def save_agent_settings(self, settings, *, pending_api_key=""):
        settings.validate()
        if any(isinstance(value, str) and secret and secret in value
               for secret in (pending_api_key, self.gui_key) for value in asdict(settings).values()):
            raise ValueError("synthetic-private-configuration-error " + pending_api_key)
        if self.fail_public:
            raise OSError("synthetic-private-settings-error " + pending_api_key)
        self.calls.append(("public", settings, pending_api_key))
        self.settings = settings

    def get_agent_gui_api_key(self):
        self.key_reads += 1
        return self.gui_key

    def save_agent_gui_api_key(self, key):
        if self.fail_key:
            raise OSError("synthetic-private-vault-error " + key)
        self.calls.append(("save_key", key))
        self.gui_key = key

    def clear_agent_gui_api_key(self):
        self.calls.append(("clear_key",))
        self.gui_key = ""

    @property
    def agent_gui_credential_status(self):
        if self.fail_status:
            raise OSError("synthetic-private-status-error " + OLD_KEY)
        return "测试系统凭据可用；密钥不写入普通设置。"


class ChatDouble(QObject):
    agent_settings_changed = Signal()

    def __init__(self):
        super().__init__()
        self.refreshes = 0

    def refresh_agent_settings(self):
        self.refreshes += 1
        self.agent_settings_changed.emit()


class AgentSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.settings = MemorySettings()
        self.chat = ChatDouble()
        self.panel = AgentSettingsPanel(self.settings, self.chat)
        self.panel.resize(450, 650)

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_open_only_reads_public_settings_and_checks_files(self):
        with patch.object(subprocess, "Popen") as spawn:
            panel = AgentSettingsPanel(self.settings, self.chat)
            panel.refresh_backend_status()
            self.assertTrue(panel.enabled_checkbox.isChecked())
            self.assertTrue(panel.tools_checkbox.isChecked())
            self.assertFalse(panel.computer_checkbox.isChecked())
            self.assertIn("普通聊天", panel.status_label.text())
            self.assertIn("安装文件", panel.backend_status_label.text())
            spawn.assert_not_called()
            panel.deleteLater()
        self.assertEqual(self.settings.calls, [])
        self.assertEqual(self.settings.key_reads, 0)
        self.assertEqual(self.chat.refreshes, 0)

    def test_stored_credentials_are_never_prefilled_or_read(self):
        self.assertEqual(self.panel.gui_key_input.text(), "")
        self.assertEqual(self.panel.gui_key_input.echoMode(), QLineEdit.EchoMode.Password)
        self.panel.reload_settings()
        self.assertEqual(self.settings.key_reads, 0)
        self.assertNotIn(OLD_KEY, self.panel.credential_status_label.text())

    def test_editing_is_not_saved_until_save_and_hidden_developer_fields_are_preserved(self):
        original = self.settings.settings
        self.panel.tools_checkbox.setChecked(False)
        self.panel.gui_model_input.setText("ui-tars-test-model")
        self.panel.gui_profile_combo.setCurrentIndex(self.panel.gui_profile_combo.findData("pixel"))
        self.assertEqual(self.settings.settings, original)
        self.assertEqual(self.settings.calls, [])
        self.panel.save_button.click()
        saved = self.settings.settings
        self.assertFalse(saved.tools_enabled)
        self.assertEqual((saved.timeout_seconds, saved.max_steps), (0, 0))
        self.assertEqual(saved.python_executable, original.python_executable)
        self.assertEqual(saved.workspace, original.workspace)
        self.assertEqual(saved.gui_coordinate_profile, "pixel")
        self.assertEqual(self.chat.refreshes, 1)
        self.assertIn("已保存", self.panel.status_label.text())

    def test_save_public_settings_before_key_and_clear_key_draft(self):
        self.panel.gui_model_input.setText("ui-tars-test-model")
        self.panel.gui_key_input.setText(KEY)
        self.panel.show_key_button.setChecked(True)
        self.panel.save_button.click()
        self.assertEqual([call[0] for call in self.settings.calls], ["public", "save_key"])
        self.assertEqual(self.settings.calls[0][2], KEY)
        self.assertNotIn(KEY, repr(asdict(self.settings.settings)))
        self.assertEqual(self.settings.gui_key, KEY)
        self.assertEqual(self.panel.gui_key_input.text(), "")
        self.assertEqual(self.panel.gui_key_input.echoMode(), QLineEdit.EchoMode.Password)
        self.assertFalse(self.panel.show_key_button.isChecked())

    def test_key_in_public_field_blocks_both_save_paths_without_partial_writes(self):
        for button in (self.panel.save_button, self.panel.save_key_button):
            self.panel.gui_model_input.setText("model-" + KEY)
            self.panel.gui_key_input.setText(KEY)
            button.click()
            self.assertEqual(self.settings.calls, [])
            self.assertEqual(self.settings.gui_key, OLD_KEY)
            self.assertIn("非密钥设置", self.panel.error_label.text())
            self.assertNotIn(KEY, self.panel.error_label.text())

    def test_invalid_public_model_address_never_saves_key(self):
        self.panel.gui_base_url_input.setText("https://user:password@example.test/v1")
        self.panel.gui_key_input.setText(KEY)
        self.panel.save_button.click()
        self.assertEqual(self.settings.calls, [])
        self.assertFalse(self.panel.error_label.isHidden())
        self.assertNotIn("password", self.panel.error_label.text())

    def test_empty_key_does_not_clear_existing_vault_credential(self):
        self.panel.save_button.click()
        self.assertEqual(self.settings.gui_key, OLD_KEY)
        self.assertEqual([call[0] for call in self.settings.calls], ["public"])

    def test_dedicated_key_buttons_change_only_gui_credential(self):
        original = self.settings.settings
        self.panel.gui_model_input.setText("pending-unsaved-model")
        self.panel.gui_key_input.setText(KEY)
        self.panel.save_key_button.click()
        self.assertEqual(self.settings.gui_key, KEY)
        self.assertEqual(self.settings.settings, original)
        self.assertEqual(self.panel.gui_model_input.text(), "pending-unsaved-model")
        self.panel.clear_key_button.click()
        self.assertEqual(self.settings.gui_key, "")
        self.assertEqual(self.settings.settings, original)
        self.assertEqual(self.chat.refreshes, 2)
        self.assertEqual(self.settings.key_reads, 0)

    def test_vault_failure_is_static_and_distinguishes_saved_public_preferences(self):
        self.settings.fail_key = True
        self.panel.gui_key_input.setText(KEY)
        self.panel.tools_checkbox.setChecked(False)
        self.panel.save_button.click()
        self.assertEqual(self.settings.settings.timeout_seconds, 0)
        self.assertFalse(self.settings.settings.tools_enabled)
        self.assertIn("设置已保存", self.panel.status_label.text())
        self.assertNotIn(KEY, self.panel.error_label.text())
        self.assertNotIn("synthetic-private", self.panel.error_label.text())
        self.assertEqual(self.settings.gui_key, OLD_KEY)

    def test_window_deactivation_and_hide_clear_draft_and_restore_password(self):
        self.panel.gui_key_input.setText(KEY)
        self.panel.show_key_button.setChecked(True)
        self.app.sendEvent(self.panel, QEvent(QEvent.Type.WindowDeactivate))
        self.assertEqual(self.panel.gui_key_input.text(), "")
        self.assertEqual(self.panel.gui_key_input.echoMode(), QLineEdit.EchoMode.Password)
        self.panel.show()
        self.app.processEvents()
        self.panel.gui_key_input.setText(KEY)
        self.panel.show_key_button.setChecked(True)
        self.panel.hide()
        self.assertEqual(self.panel.gui_key_input.text(), "")
        self.assertFalse(self.panel.show_key_button.isChecked())

    def test_internal_focus_change_to_save_button_keeps_key_until_save(self):
        self.panel.show()
        self.app.processEvents()
        self.panel.gui_key_input.setText(KEY)
        self.panel.gui_key_input.setFocus()
        self.panel.scroll.ensureWidgetVisible(self.panel.save_key_button)
        self.app.processEvents()
        QTest.mouseClick(self.panel.save_key_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.settings.gui_key, KEY)
        self.assertEqual(self.panel.gui_key_input.text(), "")

    def test_external_refresh_signal_preserves_unsaved_fields(self):
        self.panel.gui_model_input.setText("draft-model")
        self.panel.gui_key_input.setText(KEY)
        self.chat.agent_settings_changed.emit()
        self.assertEqual(self.panel.gui_model_input.text(), "draft-model")
        self.assertEqual(self.panel.gui_key_input.text(), KEY)
        self.assertEqual(self.settings.calls, [])

    def test_450_pixel_panel_scrolls_vertically_without_horizontal_overflow(self):
        self.panel.show()
        self.app.processEvents()
        self.assertEqual(self.panel.scroll.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(self.panel.scroll.widget().width(), self.panel.scroll.viewport().width())
        self.assertGreater(self.panel.scroll.verticalScrollBar().maximum(), 0)
        for group in self.panel.findChildren(QGroupBox):
            self.assertLessEqual(group.geometry().right(), self.panel.scroll.widget().width())
        self.assertEqual(self.panel.duration_status.text(), "不设上限")
        self.assertIsNone(self.panel.findChild(QSpinBox, "agentTimeoutSeconds"))
        self.assertEqual(self.panel.steps_status.text(), "不设上限")
        self.assertIsNone(self.panel.findChild(QSpinBox, "agentMaxSteps"))
        self.assertEqual(self.panel.gui_provider_combo.count(), 4)
        self.assertFalse(any("测试连接" in button.text() for button in self.panel.findChildren(QPushButton)))

    def test_credential_status_failure_never_displays_private_details(self):
        self.settings.fail_status = True
        self.panel.reload_settings()
        self.assertNotIn(OLD_KEY, self.panel.credential_status_label.text())
        self.assertNotIn("synthetic-private", self.panel.credential_status_label.text())
        self.assertEqual(self.settings.key_reads, 0)

    def test_legacy_finite_setting_becomes_unlimited_on_explicit_save(self):
        self.settings.settings = replace(self.settings.settings, max_steps=12, timeout_seconds=120)
        self.panel.reload_settings()
        self.assertIn("不设上限", self.panel.steps_status.text())
        self.assertIn("保存后生效", self.panel.steps_status.text())
        self.assertEqual(self.settings.settings.max_steps, 12)
        self.assertIn("保存后生效", self.panel.duration_status.text())
        self.assertEqual(self.settings.settings.timeout_seconds, 120)
        self.panel.save_button.click()
        self.assertEqual(self.settings.settings.max_steps, 0)
        self.assertEqual(self.panel.steps_status.text(), "不设上限")
        self.assertEqual(self.panel.duration_status.text(), "不设上限")
        self.assertEqual(self.settings.settings.timeout_seconds, 0)


if __name__ == "__main__":
    unittest.main()
