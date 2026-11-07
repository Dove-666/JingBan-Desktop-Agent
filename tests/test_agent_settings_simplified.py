"""Embedded Agent settings expose permissions and reuse the one global API."""

from dataclasses import replace
import os
import subprocess
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QVBoxLayout, QWidget

from desktop_pet.ui.agent_panel import AgentSettingsPanel
from tests.test_agent_settings_ui import ChatDouble, MemorySettings, OLD_KEY


class SimplifiedAgentSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.settings = MemorySettings()
        self.settings.settings = replace(
            self.settings.settings, gui_provider="doubao-1.5-20b",
            gui_coordinate_profile="native", gui_base_url="https://example.test/v1",
            gui_model="existing-vision-model", max_steps=37, timeout_seconds=450,
        )
        self.chat = ChatDouble()
        self.host = QWidget()
        layout = QVBoxLayout(self.host)
        with patch.object(subprocess, "Popen") as spawn:
            self.panel = AgentSettingsPanel(self.settings, self.chat, self.host, embedded=True)
            spawn.assert_not_called()
        layout.addWidget(self.panel)
        self.host.resize(450, 500)
        self.host.show()
        self.app.processEvents()

    def tearDown(self):
        self.host.hide()
        self.host.deleteLater()
        self.app.processEvents()

    def test_opening_embedded_page_has_one_save_and_no_separate_api_or_diagnostics(self):
        before = self.settings.settings
        self.assertEqual(self.settings.calls, [])
        self.assertEqual(self.settings.key_reads, 0)
        self.assertEqual(self.settings.gui_key, OLD_KEY)
        self.assertEqual(self.panel.findChildren(QLineEdit), [])
        for name in ("gui_base_url_input", "gui_model_input", "gui_key_input",
                     "gui_provider_combo", "gui_profile_combo", "model_options_section",
                     "diagnostics_section", "save_key_button", "clear_key_button"):
            self.assertFalse(hasattr(self.panel, name))
        self.assertEqual(self.panel.findChildren(QPushButton), [self.panel.save_button])
        self.assertFalse(self.panel._dirty)
        self.assertEqual(self.panel.styleSheet(), "")
        self.assertEqual(self.settings.settings, before)

    def test_embedded_save_persists_permissions_without_touching_existing_credentials(self):
        self.panel.tools_checkbox.setChecked(False)
        self.panel.computer_checkbox.setChecked(True)
        self.assertEqual(self.settings.calls, [])
        QTest.mouseClick(self.panel.save_button, Qt.MouseButton.LeftButton)
        saved = self.settings.settings
        self.assertFalse(saved.tools_enabled)
        self.assertTrue(saved.computer_enabled)
        self.assertEqual((saved.max_steps, saved.timeout_seconds), (0, 0))
        self.assertEqual([item[0] for item in self.settings.calls], ["public"])
        self.assertEqual(self.settings.gui_key, OLD_KEY)
        self.assertEqual(self.settings.key_reads, 0)
        self.assertEqual(self.chat.refreshes, 1)
        self.assertFalse(self.panel._dirty)

    def test_save_failure_is_visible_without_expanding_or_scrolling_settings(self):
        self.settings.fail_public = True
        self.panel.tools_checkbox.setChecked(False)
        QTest.mouseClick(self.panel.save_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.panel.error_label.isVisibleTo(self.host))
        self.assertIn("未能保存", self.panel.error_label.text())
        self.assertNotIn("synthetic-private", self.panel.error_label.text())
        self.assertFalse(self.panel.tools_checkbox.isChecked())
        self.assertEqual(self.settings.calls, [])

    def test_short_embedded_view_keeps_save_reachable_without_horizontal_scrolling(self):
        self.host.resize(360, 230)
        self.app.processEvents()
        self.assertEqual(self.panel.scroll.horizontalScrollBar().maximum(), 0)
        self.assertTrue(self.panel.save_button.isVisibleTo(self.host))
        self.assertTrue(self.panel.rect().contains(self.panel.save_button.mapTo(self.panel, self.panel.save_button.rect().center())))
        self.assertEqual(self.settings.calls, [])


if __name__ == "__main__":
    unittest.main()
