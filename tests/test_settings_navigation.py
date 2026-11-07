"""In-window settings navigation with isolated preferences and credentials.

The application, Qt stack and WebChannel are real. Artwork, SQLite and INI
files live in a temporary directory; no OS vault, real camera or model is used.
"""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QLineEdit, QStackedWidget

from desktop_pet.application_controller import ApplicationController
from desktop_pet import APP_VERSION
from desktop_pet.config import PetConfig
from desktop_pet.expression_manager import ExpressionManager
from desktop_pet.perception.settings import PerceptionSettings
from desktop_pet.settings_manager import ApiSettings
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_assistant_ui import click_dom, wait_js
from tests.test_memory_controller import FakeMemoryService


class EmbeddedSettingsNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-settings-navigation-")
        self.base = Path(self.directory.name)
        image = QImage(80, 100, QImage.Format.Format_ARGB32)
        image.fill(QColor("#649ed0"))
        image_path = self.base / "synthetic-character.png"
        self.assertTrue(image.save(str(image_path)))
        self.backend = MemoryCredentialBackend()
        self.settings = isolated_settings(self.base / "settings.ini", self.backend)
        self.settings.save_perception_settings(PerceptionSettings(enabled=False, allow_remote=False))
        self.settings.save_api_settings(ApiSettings("https://example.invalid/v1", "synthetic-model", 5))
        self.settings.save_api_key("synthetic-saved-settings-token")
        self.memory = FakeMemoryService()
        self.tray = SimpleNamespace(available=False, refresh=Mock(), shutdown=Mock())
        self.camera_start = Mock(return_value=False)
        patches = (
            patch("desktop_pet.application_controller.DesktopTray", return_value=self.tray),
            patch("desktop_pet.application_controller.MemoryService", return_value=self.memory),
            patch("desktop_pet.pet_controller.ExpressionManager", return_value=ExpressionManager(
                self.base / "expressions", character_directory=self.base / "character")),
            patch("desktop_pet.perception.camera_observer.CameraObserver.start", self.camera_start),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.session = ApplicationController(PetConfig(image_path=image_path), settings=self.settings)
        self.session.pet_controller.asset_player.directory = self.base / "animations"
        self.session.pet.refresh_timer.stop()
        spin_until(lambda: not self.session.chat.history_loading and not self.session.chat._runner._tasks
                   and not self.session.memory.busy)

    def tearDown(self):
        self.session.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for window in (self.session.pet, self.session.debug_panel, self.session.chat_window):
            if window is not None:
                window.deleteLater()
        self.session.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def open_settings(self, section="general"):
        self.session.open_settings(section)
        window = self.session.chat_window
        self.assertIsInstance(window.pages, QStackedWidget)
        page = window.settings_page
        self.assertIs(window.pages.currentWidget(), page)
        self.assertEqual(page.section_key, section)
        self.assertIs(page.window(), window)
        self.assertFalse(page.isWindow())
        self.assertIsNone(self.session.debug_panel)
        return window, page

    def preference_snapshot(self):
        return (
            self.settings.load_api_settings(), self.settings.get_api_key(),
            self.settings.load_scale(), self.settings.load_reading_font(),
            self.settings.load_chat_font_size(), self.settings.load_auto_expression(),
            self.settings.load_perception_settings(), self.settings.load_camera_settings(),
        )

    def test_rendered_sidebar_settings_button_opens_embedded_page(self):
        self.session.open_chat_window()
        window = self.session.chat_window
        panel = window.chat_panel
        spin_until(lambda: panel._frontend_ready, timeout=20)
        self.assertIsNotNone(panel.web_view)
        # Game launchers share the visual class and have their own test IDs.
        # Confirm the rendered label before clicking the settings entry.
        settings_button = ".sidebar-setting:not([data-testid])"
        wait_js(panel, f"document.querySelector({json.dumps(settings_button)})?.textContent.trim() === {json.dumps('设置')}")
        click_dom(panel, settings_button)
        spin_until(lambda: window.settings_page is not None
                   and window.pages.currentWidget() is window.settings_page)
        self.assertIs(window.pages.currentWidget(), window.settings_page)
        self.assertEqual(window.settings_page.section_key, "general")
        self.assertIsNone(self.session.debug_panel)
        self.assertIs(window.settings_page.window(), window)

    def test_general_settings_only_shows_a_quiet_version_number(self):
        window, page = self.open_settings()
        self.assertEqual(page.version_label.text(), APP_VERSION)
        self.assertEqual(page.version_label.accessibleName(), "版本")
        self.assertEqual(page.version_label.toolTip(), "")
        self.assertNotIn("版本", page.version_label.text())
        self.assertFalse(any(marker in page.version_label.text().casefold()
                             for marker in ("beta", "test", "测试", "正式", "体验")))
        self.assertNotIn(APP_VERSION, window.windowTitle())

    def test_character_card_import_apply_and_reset_through_embedded_settings(self):
        window, page = self.open_settings("character")
        card_panel = page.character_card_panel
        self.assertTrue(card_panel.isVisible())
        path = self.base / "owned-character-card.json"
        path.write_text(json.dumps({"name": "海月", "personality": "温柔俏皮，讲话简洁。",
            "description": "来自海边的小伙伴。", "first_mes": "你好呀。"}, ensure_ascii=False), encoding="utf-8")
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "")):
            card_panel.import_button.click()
        self.assertIsNone(self.session.chat.character_card)
        card_panel.apply_button.click()
        self.assertEqual(self.session.chat.character_card.name, "海月")
        self.assertEqual(self.settings.load_character_card().name, "海月")
        self.assertIn("海月", card_panel.current_label.text())
        self.session.open_settings("general")
        self.session.open_settings("character")
        self.assertIs(page.character_card_panel, card_panel)
        card_panel.reset_button.click()
        self.assertIsNone(self.session.chat.character_card)
        self.assertIsNone(self.settings.load_character_card())

    def test_settings_entry_reuses_one_window_and_one_settings_page(self):
        window, page = self.open_settings()
        self.session.open_settings("character")
        self.assertIs(self.session.chat_window, window)
        self.assertIs(window.settings_page, page)
        self.assertEqual(page.section_key, "character")
        self.assertEqual(window.pages.count(), 2)
        self.assertIsNone(self.session.debug_panel)

    def test_character_library_save_and_switch_through_embedded_settings(self):
        from desktop_pet.character_cards import CharacterCard

        window, page = self.open_settings("character")
        panel = page.character_card_panel
        path = self.base / "owned-library-card.json"
        path.write_text(json.dumps({"name": "海月", "personality": "温柔而简洁。"},
                                   ensure_ascii=False), encoding="utf-8")
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "")):
            panel.import_button.click()
        panel.save_button.click()
        self.assertIsNone(self.session.chat.character_card)
        self.assertEqual(len(self.settings.list_character_cards()), 1)
        panel.apply_button.click()
        self.assertEqual(self.session.chat.character_card.name, "海月")

        another = self.settings.save_library_character_card(CharacterCard.from_dict({
            "name": "星遥", "personality": "活泼又耐心。"}))
        panel.refresh_button.click()
        index = panel.library_combo.findData(another.id)
        self.assertGreaterEqual(index, 0)
        panel.library_combo.setCurrentIndex(index)
        self.assertIn("星遥", panel.name_label.text())
        self.assertEqual(self.session.chat.character_card.name, "海月")
        panel.apply_button.click()
        self.assertEqual(self.session.chat.character_card.name, "星遥")
        self.assertEqual(self.settings.load_character_card().name, "星遥")
        self.session.open_settings("general")
        self.session.open_settings("character")
        self.assertIs(page.character_card_panel, panel)
        self.assertIs(self.session.chat_window, window)

    def test_specialized_entry_points_open_the_expected_settings_category(self):
        window, page = self.open_settings()
        for opener, section in (
            (self.session.open_api_settings, "api"),
            (self.session.open_perception_settings, "perception"),
            (self.session.open_camera_settings, "camera"),
            (self.session.open_remote_settings, "remote"),
            (self.session.open_memory_window, "memory"),
        ):
            with self.subTest(section=section):
                opener()
                self.assertIs(self.session.chat_window, window)
                self.assertIs(window.pages.currentWidget(), page)
                self.assertEqual(page.section_key, section)
                self.assertIsNone(self.session.debug_panel)

    def test_legacy_api_bridge_and_observer_settings_signals_use_embedded_settings(self):
        self.session.open_chat_window()
        panel = self.session.chat_window.chat_panel
        self.assertTrue(json.loads(panel.bridge.dispatch("settings", "{}"))["ok"])
        self.assertEqual(self.session.chat_window.settings_page.section_key, "api")
        self.assertTrue(json.loads(panel.bridge.dispatch("openPerceptionSettings", "{}"))["ok"])
        self.assertEqual(self.session.chat_window.settings_page.section_key, "perception")
        self.assertTrue(json.loads(panel.bridge.dispatch("cameraSettings", "{}"))["ok"])
        self.assertEqual(self.session.chat_window.settings_page.section_key, "camera")
        self.assertIsNone(self.session.debug_panel)

    def test_return_to_chat_preserves_conversation_messages_and_unsent_web_draft(self):
        self.session.open_chat_window()
        window = self.session.chat_window
        panel = window.chat_panel
        cid = self.session.chat.current_conversation_id
        self.session.chat.store.append_message(cid, "user", "已保存的合成对话", "user", message_id="settings-u")
        self.assertTrue(self.session.chat.open_conversation(cid))
        spin_until(lambda: not self.session.chat.history_loading)
        draft = "设置调整完以后继续写这段草稿"
        self.assertTrue(panel.dispatch_web_action("draft", {"conversationId": cid, "text": draft}))
        _, page = self.open_settings("api")
        page.back_button.click()
        self.assertIs(window.pages.currentWidget(), panel)
        self.assertEqual(self.session.chat.current_conversation_id, cid)
        self.assertEqual(panel.web_state()["drafts"][cid], draft)
        self.assertEqual(self.session.chat.messages[0]["content"], "已保存的合成对话")
        self.assertEqual(self.session.chat.store.load_messages(cid)[0]["content"], "已保存的合成对话")

    def test_closing_then_opening_chat_keeps_page_instances_and_session(self):
        window, page = self.open_settings("api")
        cid = self.session.chat.current_conversation_id
        page.model_input.setText("unsaved-model-draft")
        window.close()
        self.assertFalse(window.isVisible())
        self.session.open_chat_window()
        self.assertIs(self.session.chat_window, window)
        self.assertIs(window.pages.currentWidget(), window.chat_panel)
        self.assertEqual(self.session.chat.current_conversation_id, cid)
        self.session.open_api_settings()
        self.assertIs(window.settings_page, page)
        self.assertEqual(page.model_input.text(), "unsaved-model-draft")
        self.assertEqual(self.settings.load_api_settings().model, "synthetic-model")

    def test_switching_categories_hides_key_but_keeps_form_draft_out_of_web_state(self):
        window, page = self.open_settings("api")
        draft = "synthetic-unsaved-private-token"
        page.key_input.setText(draft)
        page.show_key_button.click()
        self.assertEqual(page.key_input.echoMode(), QLineEdit.EchoMode.Normal)
        self.session.open_settings("general")
        self.assertEqual(page.key_input.echoMode(), QLineEdit.EchoMode.Password)
        self.assertFalse(page.show_key_button.isChecked())
        self.session.open_api_settings()
        self.assertEqual(page.key_input.text(), draft)
        self.assertNotIn(draft, window.chat_panel.bridge.getState())
        self.assertEqual(self.settings.get_api_key(), "synthetic-saved-settings-token")
        self.assertNotIn(draft, (self.base / "settings.ini").read_text(encoding="utf-8"))

    def test_return_and_close_reset_key_visibility_without_losing_api_draft(self):
        window, page = self.open_settings("api")
        page.key_input.setText("synthetic-temporary-key")
        for leave in (page.back_button.click, window.close):
            with self.subTest(leave=leave):
                self.session.open_api_settings()
                page.show_key_button.setChecked(True)
                leave()
                self.assertFalse(page.show_key_button.isChecked())
                self.assertEqual(page.key_input.echoMode(), QLineEdit.EchoMode.Password)
                self.session.open_api_settings()
                self.assertEqual(page.key_input.text(), "synthetic-temporary-key")

    def test_browsing_all_settings_does_not_modify_preferences_or_start_camera(self):
        before = self.preference_snapshot()
        _, page = self.open_settings()
        for section in ("character", "api", "perception", "camera", "memory", "remote", "general"):
            self.session.open_settings(section)
            self.app.processEvents()
        spin_until(lambda: not self.session.memory.busy)
        self.assertEqual(self.preference_snapshot(), before)
        self.camera_start.assert_not_called()
        self.assertFalse(self.session.camera.settings.enabled)
        self.assertFalse(self.session.perception.settings.enabled)
        self.assertFalse(self.session.remote.enabled)
        self.assertFalse(self.session.chat.busy)
        self.assertIsNone(self.session.debug_panel)
        self.assertIs(page, self.session.chat_window.settings_page)

    def test_memory_manager_is_a_child_of_the_current_settings_page(self):
        self.session.open_memory_window()
        window, page = self.open_settings("memory")
        memory = page.memory_panel
        self.assertIsNotNone(memory)
        self.assertFalse(memory.isWindow())
        self.assertIs(memory.window(), window)
        self.assertIs(self.session.memory_panel, memory)
        self.assertIs(page.section_stack.currentWidget(), memory)
        page.back_button.click()
        self.assertIs(window.pages.currentWidget(), window.chat_panel)
        self.session.open_memory_window()
        self.assertIs(page.memory_panel, memory)
        self.assertIsNone(self.session.debug_panel)

    def test_search_filters_categories_without_replacing_unsaved_form(self):
        _, page = self.open_settings("api")
        page.model_input.setText("keep-search-draft")
        page.search_input.setText("摄像")
        visible = [page.navigation.item(index).text() for index in range(page.navigation.count())
                   if not page.navigation.item(index).isHidden()]
        self.assertEqual(visible, ["摄像头"])
        page.search_input.setText("不存在的设置分类")
        self.assertFalse(page.search_empty_label.isHidden())
        page.search_input.clear()
        self.assertTrue(page.search_empty_label.isHidden())
        page.show_section("general")
        page.show_section("api")
        self.assertEqual(page.model_input.text(), "keep-search-draft")
        self.assertEqual(self.settings.load_api_settings().model, "synthetic-model")

    def test_narrow_window_uses_compact_navigation_and_can_return_to_chat(self):
        window, page = self.open_settings("api")
        window.resize(580, 650)
        self.app.processEvents()
        self.assertLessEqual(window.width(), 600)
        self.assertTrue(page.sidebar.isHidden())
        self.assertFalse(page.compact_header.isHidden())
        page.section_combo.setCurrentIndex(page.section_combo.findData("general"))
        self.assertEqual(page.section_key, "general")
        page.compact_back_button.click()
        self.assertIs(window.pages.currentWidget(), window.chat_panel)

    def test_memory_source_navigation_switches_back_to_the_selected_conversation(self):
        self.session.chat.store.append_message("memory-source-session", "user", "记忆来源中的真实内容",
                                               "user", message_id="source-u")
        window, page = self.open_settings("memory")
        page.memory_panel.conversation_requested.emit("memory-source-session")
        spin_until(lambda: not self.session.chat.history_loading)
        self.assertIs(window.pages.currentWidget(), window.chat_panel)
        self.assertEqual(self.session.chat.current_conversation_id, "memory-source-session")
        self.assertEqual(self.session.chat.messages[0]["content"], "记忆来源中的真实内容")
        self.assertIsNone(self.session.debug_panel)


if __name__ == "__main__":
    unittest.main()
