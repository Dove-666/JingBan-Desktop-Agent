"""Model selection uses isolated credentials and never touches user settings."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QEventLoop, QThreadPool, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from shiboken6 import isValid

from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.settings_controls import SettingsControlsMixin
from desktop_pet.ui.settings_page import SettingsPage
from tests.test_debug_ui import FakeChat, FakePet, FakeSettings
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_model_list_api import ModelApiServer, model_body


def wait_event_loop(condition, timeout_ms=15000):
    """Let Qt release the GIL normally during the SDK's first lazy import."""
    if condition():
        return
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(10)
    poll.timeout.connect(lambda: loop.quit() if condition() else None)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll.start()
    deadline.start(timeout_ms)
    loop.exec()
    poll.stop()
    deadline.stop()
    if not condition():
        raise AssertionError("Isolated model picker did not finish within its deadline")


class ModelChat(FakeChat):
    def __init__(self):
        super().__init__()
        self.model_calls = []

    def fetch_models(self, settings, key, callback):
        self.model_calls.append((settings, key, callback))
        return True


class ApiHost(QWidget, SettingsControlsMixin):
    def __init__(self, settings, chat):
        super().__init__()
        self.settings_manager, self.chat_controller = settings, chat
        QVBoxLayout(self).addWidget(self._make_api_tab())
        self.resize(720, 650)


class ModelListUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.settings, self.chat = FakeSettings(), ModelChat()
        self.panel = ApiHost(self.settings, self.chat)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        if isValid(self.panel):
            self.panel.hide()
            self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def finish(self, models=None, error=""):
        self.chat.model_calls[-1][2](models if models is not None else ["alpha-chat", "beta-chat"], error)
        self.app.processEvents()

    def test_fetch_uses_unsaved_address_and_temporary_key_without_requiring_model(self):
        saved = self.settings.config
        self.panel.base_url_input.setText("https://new-provider.invalid/v1")
        self.panel.key_input.setText("synthetic-temporary-list-token")
        self.panel.model_input.clear()
        self.panel.fetch_models_button.click()
        config, key, _ = self.chat.model_calls[0]
        self.assertEqual(config.base_url, "https://new-provider.invalid/v1")
        self.assertEqual(config.model, "")
        self.assertEqual(key, "synthetic-temporary-list-token")
        self.assertEqual(self.settings.config, saved)
        self.assertNotEqual(self.settings.key, key)
        self.panel.fetch_models_button.click()
        self.assertEqual(len(self.chat.model_calls), 1)
        self.finish()
        self.assertEqual(self.panel.model_input.text(), "")
        self.assertEqual(self.panel.model_combo.count(), 2)
        self.assertTrue(self.panel.fetch_models_button.isEnabled())
        self.assertIn("2", self.panel.models_status_label.text())

    def test_refresh_preserves_manual_model_and_does_not_invalidate_connection_result(self):
        self.panel.model_input.setText("custom-model-not-in-list")
        revision = self.panel._connection_fields_revision
        self.panel.fetch_models_button.click()
        self.finish()
        self.assertEqual(self.panel.model_input.text(), "custom-model-not-in-list")
        self.assertEqual(self.panel._connection_fields_revision, revision)

    def test_selected_or_manually_entered_model_uses_existing_save_path_and_restores(self):
        self.panel.fetch_models_button.click()
        self.finish()
        self.panel.model_combo.setCurrentIndex(1)
        self.assertEqual(self.panel.model_input.text(), "beta-chat")
        self.panel.save_api_button.click()
        self.assertEqual(self.settings.config.model, "beta-chat")
        restored = ApiHost(self.settings, ModelChat())
        try:
            self.assertEqual(restored.model_input.text(), "beta-chat")
            self.assertEqual(restored.model_combo.count(), 0)
            self.panel.model_input.setText("manual-deployment")
            self.panel.save_api_button.click()
            self.assertEqual(self.settings.config.model, "manual-deployment")
        finally:
            restored.deleteLater()

    def test_typing_filters_model_names_case_insensitively(self):
        self.panel.fetch_models_button.click()
        self.finish(["alpha-chat", "BETA-vision", "beta-chat"])
        self.panel.model_input.setFocus()
        self.panel.model_input.selectAll()
        QTest.keyClicks(self.panel.model_input, "beta")
        self.app.processEvents()
        completer = self.panel.model_combo.completer()
        self.assertEqual(completer.filterMode(), Qt.MatchFlag.MatchContains)
        self.assertEqual(completer.caseSensitivity(), Qt.CaseSensitivity.CaseInsensitive)
        self.assertEqual(completer.completionCount(), 2)
        completer.popup().hide()

    def test_model_edit_during_fetch_is_preserved(self):
        self.panel.fetch_models_button.click()
        self.panel.model_input.setText("typed-while-waiting")
        self.panel.reasoning_effort_combo.setCurrentIndex(2)
        self.finish()
        self.assertEqual(self.panel.model_input.text(), "typed-while-waiting")
        self.assertEqual(self.panel.model_combo.count(), 2)

    def test_old_response_is_ignored_after_base_url_or_key_changes(self):
        for field, value in ((self.panel.base_url_input, "https://other.invalid/v1"),
                             (self.panel.key_input, "synthetic-new-token")):
            self.panel.fetch_models_button.click()
            field.setText(value)
            self.finish()
            self.assertEqual(self.panel.model_combo.count(), 0)
            self.assertIn("忽略旧列表", self.panel.models_status_label.text())
            self.assertTrue(self.panel.fetch_models_button.isEnabled())

    def test_loaded_choices_clear_on_provider_change_but_manual_text_remains(self):
        self.panel.fetch_models_button.click()
        self.finish()
        before = self.panel.model_input.text()
        self.panel.base_url_input.setText("https://other.invalid/v1")
        self.assertEqual(self.panel.model_combo.count(), 0)
        self.assertEqual(self.panel.model_input.text(), before)
        self.assertIn("重新获取", self.panel.models_status_label.text())

    def test_saving_temporary_key_while_fetching_keeps_effective_source_valid(self):
        self.panel.key_input.setText("synthetic-save-during-list")
        self.panel.fetch_models_button.click()
        self.panel.save_api_button.click()
        self.assertEqual(self.panel.key_input.text(), "")
        self.finish()
        self.assertEqual(self.panel.model_combo.count(), 2)
        self.assertNotIn("忽略", self.panel.models_status_label.text())

    def test_missing_key_or_bad_address_is_reported_without_sending(self):
        self.settings.key = ""
        self.panel.fetch_models_button.click()
        self.assertIn("API Key", self.panel.models_status_label.text())
        self.panel.base_url_input.setText("invalid")
        self.panel.fetch_models_button.click()
        self.assertIn("Base URL", self.panel.models_status_label.text())
        self.assertEqual(self.chat.model_calls, [])

    def test_credential_backend_exception_is_not_displayed(self):
        with patch.object(self.settings, "get_api_key", side_effect=RuntimeError("private-token-and-internals")):
            self.panel.fetch_models_button.click()
        self.assertIn("凭据库", self.panel.models_status_label.text())
        self.assertNotIn("private-token", self.panel.models_status_label.text())
        self.assertEqual(self.chat.model_calls, [])

    def test_list_failure_does_not_block_manual_input_or_clear_working_choices(self):
        self.panel.fetch_models_button.click()
        self.finish()
        self.panel.fetch_models_button.click()
        self.finish([], "该服务商不支持获取模型列表，请手动填写模型名称。")
        self.assertIn("不支持", self.panel.models_status_label.text())
        self.assertEqual(self.panel.model_combo.count(), 2)
        self.panel.model_input.setText("manual-after-failure")
        self.assertTrue(self.panel.fetch_models_button.isEnabled())

    def test_fetch_is_independent_of_chat_busy_state(self):
        self.chat.busy = True
        self.chat.busy_changed.emit(True)
        self.panel.fetch_models_button.click()
        self.assertEqual(len(self.chat.model_calls), 1)
        self.assertFalse(self.panel.test_connection_button.isEnabled())
        self.finish()
        self.assertTrue(self.chat.busy)

    def test_late_callback_after_widget_destruction_is_safe(self):
        self.panel.fetch_models_button.click()
        callback = self.chat.model_calls[-1][2]
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.panel))
        callback(["late-model"], "")

    def test_embedded_settings_reuses_same_picker_without_another_window(self):
        page = SettingsPage(FakePet(), self.settings, self.chat)
        try:
            page.show_section("api")
            self.assertIs(page.fetch_models_button.window(), page)
            page.model_input.clear()
            page.fetch_models_button.click()
            self.chat.model_calls[-1][2](["embedded-model"], "")
            self.assertEqual(page.model_combo.itemText(0), "embedded-model")
            page.show_section("character")
            page.show_section("api")
            self.assertEqual(page.model_combo.itemText(0), "embedded-model")
        finally:
            page.deleteLater()

    def test_real_local_get_populates_embedded_picker_while_gui_remains_responsive(self):
        with tempfile.TemporaryDirectory(prefix="aura-model-picker-") as directory, ModelApiServer() as server:
            root = Path(directory)
            vault = MemoryCredentialBackend()
            settings = isolated_settings(root / "settings.ini", vault)
            settings.save_api_settings(ApiSettings(server.base_url, "keep-this-model", 10))
            settings.save_api_key("synthetic-model-picker-token")
            chat = ChatController(settings, store=ConversationStore(root / "chat.sqlite3"))
            page = SettingsPage(FakePet(), settings, chat)
            ticks = []
            timer = QTimer()
            timer.setInterval(10)
            timer.timeout.connect(lambda: ticks.append(True))
            try:
                page.resize(1000, 780)
                page.show_section("api")
                page.show()
                self.assertEqual(page.base_url_input.text(), server.base_url)
                before = (root / "settings.ini").read_bytes()
                server.enqueue(model_body("z-chat", "a-chat", "a-chat"), delay=.3)
                timer.start()
                page.fetch_models_button.click()
                wait_event_loop(lambda: server.received.is_set() or page.fetch_models_button.isEnabled())
                self.assertTrue(server.received.is_set(), page.models_status_label.text())
                self.assertFalse(chat.busy)
                page.show_section("character")
                page.scale_slider.setValue(125)
                self.assertEqual(page.pet_controller.scale_percent, 125)
                page.show_section("api")
                wait_event_loop(lambda: page.fetch_models_button.isEnabled())
                self.assertGreaterEqual(len(ticks), 3)
                self.assertEqual(page.model_combo.count(), 2)
                self.assertEqual(page.model_input.text(), "keep-this-model")
                self.assertEqual((root / "settings.ini").read_bytes(), before)
                self.assertEqual(server.requests, [{"method": "GET", "path": "/v1/models",
                    "authorization": "Bearer synthetic-model-picker-token"}])
                page.model_combo.setCurrentIndex(0)
                page.save_api_button.click()
                restored_settings = isolated_settings(root / "settings.ini", vault)
                restored_page = ApiHost(restored_settings, ModelChat())
                try:
                    self.assertEqual(restored_page.model_input.text(), "a-chat")
                    self.assertEqual(restored_settings.get_api_key(), "synthetic-model-picker-token")
                    self.assertNotIn("synthetic-model-picker-token", (root / "settings.ini").read_text(encoding="utf-8"))
                finally:
                    restored_page.deleteLater()
            finally:
                timer.stop()
                chat.shutdown()
                spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
                page.hide()
                page.deleteLater()
                chat.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
