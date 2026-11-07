"""Compact settings keep real controls working without changing saved defaults.

The existing application fixture supplies temporary INI/SQLite files, fake
credentials and synthetic artwork. Startup registration is also a test double.
No test accesses the user's registry, models, camera or production settings.
"""

import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QPushButton, QScrollArea

from desktop_pet.ui.settings_widgets import CollapsibleSection
from tests import test_settings_navigation as navigation_fixture
from tests.test_application import spin_until
from tests.test_startup_settings_ui import FakeStartupManager


class SettingsRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.startup = FakeStartupManager()
        with patch("desktop_pet.application_controller.StartupManager", return_value=self.startup):
            navigation_fixture.EmbeddedSettingsNavigationTests.setUp(self)
        self.session.startup_manager = self.startup

    tearDown = navigation_fixture.EmbeddedSettingsNavigationTests.tearDown

    def open_settings(self, section="general"):
        self.session.open_settings(section)
        self.app.processEvents()
        return self.session.chat_window.settings_page

    def ini_bytes(self):
        self.settings._settings.sync()
        return (self.base / "settings.ini").read_bytes()

    def test_hidden_context_card_stays_hidden_after_legacy_updates(self):
        before = self.ini_bytes()
        page = self.open_settings()
        cards = page.findChildren(QFrame, "contextBudgetCard")
        self.assertTrue(cards)
        self.assertTrue(all(not card.isVisibleTo(page) for card in cards))
        with patch.object(self.session.chat, "update_context_budget") as save:
            page.context_total_input.setValue(2048)
            page.context_reserve_input.setValue(65536)
            page._context_budget_edited()
            page._save_context_budget()
            self.session.chat.context_usage_changed.emit("已使用摘要与近期消息")
            save.assert_not_called()
        self.assertEqual(page.context_usage_label.text(), "已使用摘要与近期消息")
        self.assertFalse(page._legacy_context_controls.isVisibleTo(page))
        self.assertEqual(self.ini_bytes(), before)

    def test_opening_then_clicking_startup_switch_changes_only_the_chosen_setting(self):
        before = self.ini_bytes()
        page = self.open_settings()
        self.assertEqual(self.ini_bytes(), before)
        self.assertEqual(self.startup.changes, [])
        self.assertFalse(self.settings.load_silent_start())
        api = self.settings.load_api_settings()
        QTest.mouseClick(page.silent_start_checkbox, Qt.MouseButton.LeftButton,
                         pos=QPoint(page.silent_start_checkbox.width() - 15, 20))
        self.assertTrue(self.settings.load_silent_start())
        self.assertTrue(page.silent_start_checkbox.isChecked())
        self.assertEqual(self.startup.changes, [])
        self.assertEqual(self.settings.load_api_settings(), api)
        self.camera_start.assert_not_called()

    def test_chat_font_persists_syncs_external_changes_and_resets(self):
        page = self.open_settings()
        page.chat_font_slider.setValue(20)
        self.assertEqual(self.settings.load_chat_font_size(), 20)
        self.assertEqual(self.session.chat.reading_font_size, 20)
        self.session.chat.set_reading_font_size(18)
        self.assertEqual(page.chat_font_slider.value(), 18)
        self.assertEqual(page.chat_font_value.text(), "18")
        page.findChild(QPushButton, "resetChatFontButton").click()
        self.assertEqual(page.chat_font_slider.value(), 16)
        self.assertEqual(self.settings.load_chat_font_size(), 16)

    def test_bubble_font_has_one_visible_home_and_still_updates_the_pet(self):
        page = self.open_settings("character")
        self.assertFalse(page.reading_slider.isVisibleTo(page))
        page.show_section("general")
        page.bubble_font_slider.setValue(19)
        self.assertEqual(self.settings.load_reading_font(), 19)
        self.assertEqual(self.session.pet_controller.reading_font_points, 19)
        self.assertEqual(page.reading_slider.value(), 19)
        page.findChild(QPushButton, "resetBubbleFontButton").click()
        self.assertEqual(self.settings.load_reading_font(), 14)
        page.show_section("character")
        self.assertFalse(page.reading_slider.isVisibleTo(page))

    def test_disclosures_and_category_changes_keep_drafts_without_saving(self):
        before = self.ini_bytes()
        page = self.open_settings("api")
        page.model_input.setText("unsaved-refresh-model")
        page.api_advanced_button.click()
        page.reasoning_controls.profile_combo.setCurrentIndex(page.reasoning_controls.profile_combo.findData("openai"))
        page.reasoning_effort_combo.setCurrentIndex(page.reasoning_effort_combo.findData("high"))
        page.api_advanced_button.click()
        page.show_section("character")
        for section in page._pages["character"].findChildren(CollapsibleSection):
            section.setExpanded(True)
            section.setExpanded(False)
        page.show_section("api")
        self.assertEqual(page.model_input.text(), "unsaved-refresh-model")
        self.assertEqual(page.reasoning_effort_combo.currentData(), "high")
        self.assertEqual(self.ini_bytes(), before)
        self.assertFalse(self.session.chat.busy)

    def test_browsing_all_categories_never_enables_observers_or_saves_preferences(self):
        before = self.ini_bytes()
        page = self.open_settings()
        for section, _title, _keywords in page.SECTIONS:
            page.show_section(section)
            self.app.processEvents()
        spin_until(lambda: not self.session.memory.busy)
        self.assertEqual(self.ini_bytes(), before)
        self.assertFalse(self.session.perception.settings.enabled)
        self.assertFalse(self.session.camera.settings.enabled)
        self.assertFalse(self.session.remote.enabled)
        self.assertFalse(self.session.chat.busy)
        self.camera_start.assert_not_called()

    def test_default_sections_fit_small_and_large_windows_without_horizontal_scroll(self):
        page = self.open_settings()
        window = self.session.chat_window
        for width in (420, 580, 1060):
            window.resize(width, 760)
            self.app.processEvents()
            self.assertLessEqual(window.width(), width + 2)
            for section, _title, _keywords in page.SECTIONS:
                with self.subTest(width=width, section=section):
                    page.show_section(section)
                    self.app.processEvents()
                    selected = page._pages[section]
                    scrolls = selected.findChildren(QScrollArea)
                    if isinstance(selected, QScrollArea):
                        scrolls.insert(0, selected)
                    for scroll in scrolls:
                        if not scroll.isVisibleTo(page) or scroll.widget() is None:
                            continue
                        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0,
                                         f"{section}: {scroll.objectName()} scrolls sideways")
                        self.assertLessEqual(scroll.widget().width(), scroll.viewport().width() + 1,
                                             f"{section}: {scroll.objectName()} exceeds the viewport")

    def test_expanded_options_remain_usable_in_a_narrow_window(self):
        page = self.open_settings()
        self.session.chat_window.resize(420, 650)
        for key, _title, _terms in page.SECTIONS:
            with self.subTest(section=key):
                page.show_section(key)
                selected = page._pages[key]
                for disclosure in selected.findChildren(CollapsibleSection):
                    disclosure.setExpanded(True)
                if key == "api":
                    page.api_advanced_button.setChecked(True)
                self.app.processEvents()
                for scroll in selected.findChildren(QScrollArea):
                    if scroll.isVisibleTo(page):
                        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)


if __name__ == "__main__":
    unittest.main()
