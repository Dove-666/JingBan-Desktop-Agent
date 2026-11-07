"""Live theme switching through Qt settings and the shipped WebEngine chat.

All INI/SQLite/artwork lives in the existing temporary application fixture.
Credential, startup, memory and camera services are test doubles. No production
configuration, desktop capture, real API or OS credential store is accessed.
"""

from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.settings_manager import SettingsError
from desktop_pet.ui.theme import ThemeManager, color, current_theme, theme_manager
from tests.test_application import isolated_settings, spin_until
from tests.test_assistant_ui import eval_js, set_dom_text, wait_js
from tests import test_startup_settings_ui as startup_fixture


class _InitialPreferences:
    def load_theme(self):
        return "dark"


class ThemeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        previous = theme_manager()
        if previous is not None:
            previous.close()
        self.manager = ThemeManager(_InitialPreferences())
        try:
            startup_fixture.StartupSettingsUiTests.setUp(self)
        except Exception:
            self.manager.close()
            raise
        self.manager.settings_manager = self.settings

    def tearDown(self):
        try:
            startup_fixture.StartupSettingsUiTests.tearDown(self)
        finally:
            self.manager.close()

    def open_general(self):
        self.session.open_settings("general")
        self.app.processEvents()
        return self.session.chat_window.settings_page

    def preference_snapshot(self):
        store = self.settings._settings
        store.sync()
        return {key: store.value(key) for key in store.allKeys()
                if not key.startswith("ui/windows/")}

    @staticmethod
    def choose_light(page):
        page.theme_combo.setFocus()
        QTest.keyClick(page.theme_combo, Qt.Key.Key_End)

    def test_initial_default_is_light_in_settings_native_palette_and_chat_state(self):
        self.manager.close()
        self.manager = ThemeManager(self.settings)
        before = self.preference_snapshot()
        page = self.open_general()
        self.assertEqual(page.theme_combo.currentData(), "light")
        self.assertEqual(page.property("auraTheme"), "light")
        self.assertEqual(self.session.chat_window.chat_panel.web_state()["theme"], "light")
        self.assertGreater(self.app.palette().color(QPalette.ColorRole.Window).lightness(), 127)
        self.assertEqual(self.preference_snapshot(), before)
        self.assertFalse(self.settings._settings.contains("ui/theme"))

    def test_actual_combo_key_selection_persists_and_survives_a_fresh_settings_manager(self):
        page = self.open_general()
        self.choose_light(page)
        self.assertEqual(current_theme(), "light")
        self.assertEqual(page.property("auraTheme"), "light")
        self.assertEqual(self.settings.load_theme(), "light")
        reopened = isolated_settings(Path(self.settings.config_path), self.backend)
        self.assertEqual(reopened.load_theme(), "light")
        self.assertEqual(self.session.chat_window.chat_panel.web_state()["theme"], "light")
        QTest.keyClick(page.theme_combo, Qt.Key.Key_Home)
        self.assertEqual(current_theme(), "dark")
        self.assertEqual(self.settings.load_theme(), "dark")
        self.assertEqual(page.property("auraTheme"), "dark")

    def test_loaded_web_theme_changes_without_reload_or_losing_conversation_and_draft(self):
        self.session.open_chat_window()
        window = self.session.chat_window
        panel = window.chat_panel
        spin_until(lambda: panel._frontend_ready, timeout=20)
        wait_js(panel, "document.documentElement.dataset.theme === 'dark'")
        wait_js(panel, "Boolean(document.querySelector('.composer textarea'))")
        conversation = self.session.chat.current_conversation_id
        view, document = panel.web_view, panel.web_view.page()
        loads = QSignalSpy(view.loadStarted)
        self.assertTrue(eval_js(panel, "(() => {window.__auraThemeMarker='same-document';return true;})()"))
        draft = "主题切换后继续写这段未发送的草稿"
        set_dom_text(panel, ".composer textarea", draft)
        spin_until(lambda: panel.web_state()["drafts"].get(conversation) == draft)
        page = self.open_general()
        self.choose_light(page)
        wait_js(panel, "document.documentElement.dataset.theme === 'light'")
        self.assertIs(panel.web_view, view)
        self.assertIs(panel.web_view.page(), document)
        self.assertEqual(eval_js(panel, "window.__auraThemeMarker"), "same-document")
        self.assertEqual(loads.count(), 0)
        self.assertEqual(self.session.chat.current_conversation_id, conversation)
        self.assertEqual(panel.web_state()["drafts"][conversation], draft)
        page.back_button.click()
        wait_js(panel, "document.querySelector('.composer textarea').value.length > 0")
        self.assertEqual(eval_js(panel, "document.querySelector('.composer textarea').value"), draft)

    def test_lazy_theme_manager_binds_an_existing_chat_once_and_preserves_draft(self):
        self.manager.close()
        self.assertIsNone(theme_manager())
        self.session.open_chat_window()
        panel = self.session.chat_window.chat_panel
        spin_until(lambda: panel._frontend_ready, timeout=20)
        wait_js(panel, "document.documentElement.dataset.theme === 'light'")
        wait_js(panel, "Boolean(document.querySelector('.composer textarea'))")
        conversation = self.session.chat.current_conversation_id
        draft = "先打开聊天，再切换外观也保留草稿"
        set_dom_text(panel, ".composer textarea", draft)
        spin_until(lambda: panel.web_state()["drafts"].get(conversation) == draft)
        loads = QSignalSpy(panel.web_view.loadStarted)
        page = self.open_general()
        self.assertEqual(page.theme_combo.currentData(), "light")
        with patch.object(panel, "_theme_changed", wraps=panel._theme_changed) as changed:
            # A direct embedded controller starts light without an app manager.
            # Choosing dark first creates the fallback and binds existing views.
            page.theme_combo.setCurrentIndex(page.theme_combo.findData("dark"))
            self.manager = theme_manager() or self.manager
            self.assertIsNotNone(theme_manager())
            wait_js(panel, "document.documentElement.dataset.theme === 'dark'")
            self.choose_light(page)
            wait_js(panel, "document.documentElement.dataset.theme === 'light'")
            QTest.keyClick(page.theme_combo, Qt.Key.Key_Home)
            wait_js(panel, "document.documentElement.dataset.theme === 'dark'")
            self.assertEqual(changed.call_count, 3, "Existing chat was bound more than once")
        self.assertEqual(loads.count(), 0)
        self.assertEqual(self.session.chat.current_conversation_id, conversation)
        self.assertEqual(panel.web_state()["drafts"][conversation], draft)
        page.back_button.click()
        self.assertEqual(eval_js(panel, "document.querySelector('.composer textarea').value"), draft)
        self.assertEqual(self.settings.load_theme(), "dark")

    def test_manual_reload_after_switch_to_light_uses_one_current_bootstrap_and_restores_draft(self):
        self.session.open_chat_window()
        panel = self.session.chat_window.chat_panel
        spin_until(lambda: panel._frontend_ready, timeout=20)
        wait_js(panel, "document.documentElement.dataset.theme === 'dark'")
        wait_js(panel, "Boolean(document.querySelector('.composer textarea'))")
        conversation = self.session.chat.current_conversation_id
        draft = "浅色模式下手动重新加载也保留草稿"
        set_dom_text(panel, ".composer textarea", draft)
        spin_until(lambda: panel.web_state()["drafts"].get(conversation) == draft)
        page = self.open_general()
        self.choose_light(page)
        wait_js(panel, "document.documentElement.dataset.theme === 'light'")
        bootstrap = panel.web_view.page().scripts().find("aura-initial-theme")
        self.assertEqual(len(bootstrap), 1)
        self.assertIn('"light"', bootstrap[0].sourceCode())
        self.assertNotIn('"dark"', bootstrap[0].sourceCode())
        page.back_button.click()
        self.assertTrue(eval_js(panel, "(() => {window.__beforeManualReload=true;return true;})()"))
        loads = QSignalSpy(panel.web_view.loadStarted)
        panel._reload_web()
        spin_until(lambda: panel._frontend_ready, timeout=20)
        wait_js(panel, "document.documentElement.dataset.theme === 'light'")
        self.assertEqual(eval_js(panel, "document.documentElement.style.colorScheme"), "light")
        self.assertEqual(eval_js(panel, "typeof window.__beforeManualReload"), "undefined")
        self.assertGreaterEqual(loads.count(), 1)
        self.assertEqual(len(panel.web_view.page().scripts().find("aura-initial-theme")), 1)
        self.assertEqual(self.session.chat.current_conversation_id, conversation)
        self.assertEqual(panel.web_state()["drafts"][conversation], draft)
        wait_js(panel, "document.querySelector('.composer textarea').value.length > 0")
        self.assertEqual(eval_js(panel, "document.querySelector('.composer textarea').value"), draft)

    def test_failed_save_restores_ui_palette_and_stored_value_with_safe_error(self):
        page = self.open_general()
        before = self.preference_snapshot()
        palette = QPalette(self.app.palette())
        with patch.object(self.settings, "_sync", side_effect=SettingsError("synthetic-private-theme-directory")):
            self.choose_light(page)
        self.assertEqual(current_theme(), "dark")
        self.assertEqual(page.theme_combo.currentData(), "dark")
        self.assertEqual(page.property("auraTheme"), "dark")
        self.assertEqual(self.app.palette(), palette)
        self.assertEqual(self.settings.load_theme(), "light")
        self.assertEqual(self.preference_snapshot(), before)
        self.assertFalse(page.theme_status_label.isHidden())
        self.assertIn("未能保存", page.theme_status_label.text())
        self.assertNotIn("synthetic-private", page.theme_status_label.text())
        self.assertEqual(self.session.chat_window.chat_panel.web_state()["theme"], "dark")

    def test_browsing_and_preview_switch_preserve_drafts_settings_and_observer_permissions(self):
        before = self.preference_snapshot()
        self.session.open_settings("api")
        page = self.session.chat_window.settings_page
        page.model_input.setText("unsaved-theme-switch-model")
        for section, _title, _terms in page.SECTIONS:
            page.show_section(section)
            self.app.processEvents()
        spin_until(lambda: not self.session.memory.busy)
        self.manager.set_theme("light", persist=False)
        page.show_section("api")
        self.assertEqual(page.model_input.text(), "unsaved-theme-switch-model")
        self.assertEqual(self.preference_snapshot(), before)
        self.assertEqual(self.startup.changes, [])
        self.assertFalse(self.session.perception.settings.enabled)
        self.assertFalse(self.session.camera.settings.enabled)
        self.assertFalse(self.session.remote.enabled)
        self.assertFalse(self.session.chat.busy)
        self.camera_start.assert_not_called()

    def test_missing_and_invalid_theme_values_fall_back_without_reading_credentials(self):
        self.assertFalse(self.settings._settings.contains("ui/theme"))
        self.assertEqual(self.settings.load_theme(), "light")
        with patch.object(self.settings, "get_api_key") as vault:
            for value in ("sepia", 17, True, ["dark"]):
                with self.subTest(value=value):
                    self.settings._settings.setValue("ui/theme", value)
                    self.assertEqual(self.settings.load_theme(), "light")
                    self.assertEqual(self.settings._settings.value("ui/theme"), value)
            vault.assert_not_called()
        for name in ("dark", "light"):
            self.settings.save_theme(name)
            self.assertEqual(self.settings.load_theme(), name)
        with self.assertRaises(SettingsError):
            self.settings.save_theme("sepia")
        self.assertEqual(self.settings.load_theme(), "light")


if __name__ == "__main__":
    unittest.main()
