"""Permission-only Agent settings use no credential fields or running backend."""

from dataclasses import asdict, replace
import os
import subprocess
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QComboBox, QGroupBox, QLineEdit, QPushButton, QSpinBox

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

    def test_open_only_reads_public_settings_without_starting_backend_or_reading_credentials(self):
        with patch.object(subprocess, "Popen") as spawn:
            panel = AgentSettingsPanel(self.settings, self.chat)
            self.assertTrue(panel.enabled_checkbox.isChecked())
            self.assertTrue(panel.tools_checkbox.isChecked())
            self.assertFalse(panel.computer_checkbox.isChecked())
            self.assertEqual(panel.findChildren(QLineEdit), [])
            self.assertEqual(panel.findChildren(QComboBox), [])
            spawn.assert_not_called()
            panel.deleteLater()
        self.assertEqual(self.settings.calls, [])
        self.assertEqual(self.settings.key_reads, 0)
        self.assertEqual(self.chat.refreshes, 0)

    def test_editing_is_not_saved_until_save_and_hidden_developer_fields_are_preserved(self):
        original = self.settings.settings
        self.panel.enabled_checkbox.setChecked(False)
        self.panel.tools_checkbox.setChecked(False)
        self.panel.computer_checkbox.setChecked(True)
        self.assertEqual(self.settings.settings, original)
        self.assertEqual(self.settings.calls, [])
        self.panel.save_button.click()
        saved = self.settings.settings
        self.assertFalse(saved.enabled)
        self.assertFalse(saved.tools_enabled)
        self.assertTrue(saved.computer_enabled)
        self.assertEqual((saved.timeout_seconds, saved.max_steps), (0, 0))
        self.assertEqual(saved.python_executable, original.python_executable)
        self.assertEqual(saved.workspace, original.workspace)
        self.assertEqual(saved.gui_base_url, original.gui_base_url)
        self.assertEqual(saved.gui_model, original.gui_model)
        self.assertEqual(self.chat.refreshes, 1)
        self.assertEqual([call[0] for call in self.settings.calls], ["public"])
        self.assertIn("已保存", self.panel.status_label.text())
        self.assertEqual(self.settings.key_reads, 0)
        self.assertEqual(self.settings.gui_key, OLD_KEY)

    def test_permission_save_failure_keeps_original_settings_and_sanitizes_error(self):
        original = self.settings.settings
        self.settings.fail_public = True
        self.panel.tools_checkbox.setChecked(False)
        self.panel.save_button.click()
        self.assertEqual(self.settings.calls, [])
        self.assertEqual(self.settings.settings, original)
        self.assertFalse(self.panel.tools_checkbox.isChecked())
        self.assertFalse(self.panel.error_label.isHidden())
        self.assertIn("未能保存", self.panel.error_label.text())
        self.assertNotIn("synthetic-private", self.panel.error_label.text())
        self.assertNotIn(OLD_KEY, self.panel.error_label.text())
        self.assertEqual(self.settings.gui_key, OLD_KEY)
        self.assertEqual(self.settings.key_reads, 0)

    def test_external_refresh_signal_preserves_unsaved_fields(self):
        self.panel.enabled_checkbox.setChecked(False)
        self.panel.tools_checkbox.setChecked(False)
        self.panel.computer_checkbox.setChecked(True)
        self.chat.agent_settings_changed.emit()
        self.assertFalse(self.panel.enabled_checkbox.isChecked())
        self.assertFalse(self.panel.tools_checkbox.isChecked())
        self.assertTrue(self.panel.computer_checkbox.isChecked())
        self.assertTrue(self.panel._dirty)
        self.assertEqual(self.settings.calls, [])

    def test_compact_panel_has_no_hidden_api_controls_or_horizontal_overflow(self):
        for width in (450, 340):
            with self.subTest(width=width):
                self.panel.resize(width, 360)
                self.panel.show()
                self.app.processEvents()
                self.assertEqual(self.panel.scroll.horizontalScrollBar().maximum(), 0)
                self.assertLessEqual(self.panel.scroll.widget().width(), self.panel.scroll.viewport().width())
                for group in self.panel.findChildren(QGroupBox):
                    self.assertLessEqual(group.geometry().right(), self.panel.scroll.widget().width())
        self.assertEqual(self.panel.findChildren(QLineEdit), [])
        self.assertIsNone(self.panel.findChild(QSpinBox, "agentTimeoutSeconds"))
        self.assertIsNone(self.panel.findChild(QSpinBox, "agentMaxSteps"))
        self.assertEqual(self.panel.findChildren(QComboBox), [])
        self.assertFalse(any("测试连接" in button.text() for button in self.panel.findChildren(QPushButton)))

    def test_reload_failure_is_static_and_does_not_consume_credentials(self):
        with patch.object(self.settings, "load_agent_settings", side_effect=OSError("private-" + OLD_KEY)):
            self.panel.reload_settings()
        self.assertFalse(self.panel.enabled_checkbox.isChecked())
        self.assertFalse(self.panel.error_label.isHidden())
        self.assertNotIn(OLD_KEY, self.panel.error_label.text())
        self.assertNotIn("private-", self.panel.error_label.text())
        self.assertEqual(self.settings.key_reads, 0)
        self.assertEqual(self.settings.calls, [])

    def test_legacy_finite_setting_becomes_unlimited_on_explicit_save(self):
        self.settings.settings = replace(self.settings.settings, max_steps=12, timeout_seconds=120)
        self.panel.reload_settings()
        self.assertEqual(self.settings.settings.max_steps, 12)
        self.assertEqual(self.settings.settings.timeout_seconds, 120)
        self.panel.save_button.click()
        self.assertEqual(self.settings.settings.max_steps, 0)
        self.assertEqual(self.settings.settings.timeout_seconds, 0)


if __name__ == "__main__":
    unittest.main()
