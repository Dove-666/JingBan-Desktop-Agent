"""Settings stay usable after removing their duplicate chat-menu entries.

The application fixture uses temporary preferences, archives and fake credentials.
Observer doubles never capture a screen or open a camera.
"""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

from desktop_pet.ui.settings_page import SettingsPage
from tests import test_settings_navigation as navigation_fixture


class ObserverDouble(QObject):
    paused_changed = Signal(bool)
    settings_changed = Signal()
    status_changed = Signal(str)

    def __init__(self, *, enabled=True):
        super().__init__()
        self.settings = SimpleNamespace(enabled=enabled)
        self.paused = False
        self.calls = []

    def set_paused(self, paused):
        self.calls.append(paused)
        self.paused = paused
        self.paused_changed.emit(paused)


class SettingsConsolidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    setUp = navigation_fixture.EmbeddedSettingsNavigationTests.setUp
    tearDown = navigation_fixture.EmbeddedSettingsNavigationTests.tearDown

    def settings_page(self):
        self.session.open_settings("general")
        return self.session.chat_window.settings_page

    def test_context_policy_is_read_only_and_opening_does_not_change_settings(self):
        budget = self.settings.load_context_budget()
        with patch.object(self.session.chat, "update_context_budget",
                          wraps=self.session.chat.update_context_budget) as save:
            page = self.settings_page()
            self.assertTrue(page.context_total_input.isHidden())
            self.assertTrue(page.context_reserve_input.isHidden())
            self.assertTrue(page.context_budget_save_button.isHidden())
            self.assertFalse(page.context_budget_save_button.isEnabled())
            save.assert_not_called()
            self.assertEqual(self.settings.load_context_budget(), budget)
        self.assertIn("模型服务管理", page.context_budget_status.text())
        self.assertTrue(page.context_budget_error.isHidden())
        self.camera_start.assert_not_called()

    def test_legacy_hidden_budget_callback_cannot_restore_manual_limits(self):
        original = self.settings.load_context_budget()
        page = self.settings_page()
        page.context_total_input.setValue(2048)
        page.context_reserve_input.setValue(65536)
        with patch.object(self.session.chat, "update_context_budget") as setter:
            page._save_context_budget()
            page.context_budget_save_button.click()
            setter.assert_not_called()
        self.assertEqual(self.settings.load_context_budget(), original)
        self.assertEqual(self.session.chat.context_budget, original)
        self.assertTrue(page.context_budget_error.isHidden())
        self.assertTrue(page.context_total_input.isHidden())

    def test_usage_refresh_keeps_model_policy_and_hidden_legacy_fields(self):
        page = self.settings_page()
        page.context_total_input.setValue(24576)
        self.session.chat.context_usage_changed.emit("当前请求使用摘要和近期消息")
        self.assertEqual(page.context_usage_label.text(), "当前请求使用摘要和近期消息")
        self.assertTrue(page.context_total_input.isHidden())
        self.assertTrue(page.context_reserve_input.isHidden())
        self.assertIn("模型服务管理", page.context_budget_status.text())

    def test_pause_all_observers_never_enables_disabled_observers(self):
        self.session.open_chat_window()
        screen, camera = ObserverDouble(), ObserverDouble(enabled=False)
        page = SettingsPage(None, self.settings, self.session.chat,
                            parent=self.session.chat_window,
                            perception_controller=screen, camera_controller=camera)
        self.assertEqual(screen.calls, [])
        self.assertEqual(camera.calls, [])
        self.assertTrue(page.pause_all_button.isEnabled())
        page.pause_all_button.click()
        self.assertEqual(screen.calls, [True])
        self.assertEqual(camera.calls, [])
        self.assertTrue(screen.paused)
        self.assertFalse(camera.settings.enabled)
        self.assertFalse(page.pause_all_button.isEnabled())
        self.assertIn("屏幕感知：已暂停", page.visual_status_label.text())
        self.assertIn("摄像头：已关闭", page.visual_status_label.text())
        screen.set_paused(False)
        self.assertTrue(page.pause_all_button.isEnabled())
        self.camera_start.assert_not_called()

    def test_native_recovery_uses_sidebar_for_new_chat_and_one_settings_entry(self):
        self.session.open_chat_window()
        window = self.session.chat_window
        panel = window.chat_panel
        self.assertTrue(panel.sidebar.isAncestorOf(panel.new_button))
        self.assertEqual(panel._native_header_row.indexOf(panel.new_button), -1)
        configured_widgets = {panel.settings_button, panel.memory_button,
                              panel.context_budget_button, panel.context_usage_label}
        for action in panel.tools_menu.actions():
            if callable(getattr(action, "defaultWidget", None)) and action.defaultWidget() in configured_widgets:
                self.assertFalse(action.isVisible())
        panel.native_settings_button.click()
        self.assertIs(window.pages.currentWidget(), window.settings_page)
        self.assertEqual(window.settings_page.section_key, "general")
        window.show_chat()
        panel._show_context_budget()
        self.assertIs(window.pages.currentWidget(), window.settings_page)
        self.assertEqual(window.settings_page.section_key, "general")
        self.assertFalse(any(widget.isWindow() and widget.isVisible()
                             for widget in panel.findChildren(QWidget)))


if __name__ == "__main__":
    unittest.main()
