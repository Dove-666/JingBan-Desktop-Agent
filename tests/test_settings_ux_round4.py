"""API settings use effective request snapshots and named accessible controls."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.ui.debug_panel import DebugPanel
from tests.mock_api import MockApiServer
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_debug_ui import FakeChat, FakePet, FakeSettings


class SettingsUxRound4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.settings, self.chat = FakeSettings(), FakeChat()
        self.panel = DebugPanel(FakePet(), self.settings, self.chat)
        self.panel.tabs.setCurrentIndex(1)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def begin_held_test(self):
        with patch.object(self.chat, "test_connection", return_value=True):
            self.panel.test_connection_button.click()

    def test_saving_tested_temporary_key_does_not_mark_unchanged_result_stale(self):
        self.panel.key_input.setText("synthetic-round4-temporary-token")
        self.begin_held_test()
        self.assertNotIn("synthetic-round4-temporary-token", repr(self.panel._test_signature))
        self.panel.save_api_button.click()
        self.assertEqual(self.panel.key_input.text(), "")
        self.chat.connection_status_changed.emit("连接成功：合成结果")
        self.assertIn("连接成功", self.panel.connection_label.text())
        self.assertNotIn("此结果对应测试发起时", self.panel.connection_label.text())

    def test_editing_then_restoring_the_same_payload_keeps_pending_result_current(self):
        original = self.panel.model_input.text()
        self.begin_held_test()
        self.panel.model_input.setText("temporary-other-model")
        self.panel.model_input.setText(original)
        self.chat.connection_status_changed.emit("连接成功：合成结果")
        self.assertNotIn("此结果对应测试发起时", self.panel.connection_label.text())

    def test_changed_model_or_effective_key_keeps_old_result_warning(self):
        self.begin_held_test()
        self.panel.model_input.setText("genuinely-different-model")
        self.chat.connection_status_changed.emit("连接成功：旧模型")
        self.assertIn("此结果对应测试发起时", self.panel.connection_label.text())
        self.panel.model_input.setText("test-model")
        self.panel.key_input.setText("genuinely-different-temporary-token")
        self.chat.connection_status_changed.emit("连接成功：旧密钥")
        self.assertIn("此结果对应测试发起时", self.panel.connection_label.text())

    def test_failed_credential_deletion_still_invalidates_pending_result(self):
        self.begin_held_test()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                patch.object(self.settings, "clear_api_key", side_effect=RuntimeError("synthetic delete denied")):
            self.panel.clear_key_button.click()
        self.chat.connection_status_changed.emit("连接成功：清除前的旧结果")
        self.assertIn("此结果对应测试发起时", self.panel.connection_label.text())
        self.assertFalse(self.panel.api_error_label.isHidden())

    def test_unreadable_saved_key_does_not_validate_a_late_result(self):
        self.begin_held_test()
        self.panel.model_input.setText("test-model ")
        with patch.object(self.settings, "get_api_key", side_effect=RuntimeError("synthetic private backend detail")):
            self.chat.connection_status_changed.emit("连接成功：旧结果")
        self.assertIn("此结果对应测试发起时", self.panel.connection_label.text())
        self.assertNotIn("synthetic private backend detail", self.panel.connection_label.text())

    def test_api_controls_expose_specific_accessible_names_and_key_label_buddy(self):
        expected = ((self.panel.base_url_input, "聊天 API 根地址（Base URL）"),
                    (self.panel.key_input, "聊天 API Key"), (self.panel.model_input, "聊天模型名称"))
        for widget, name in expected:
            with self.subTest(name=name):
                interface = QAccessible.queryAccessibleInterface(widget)
                self.assertEqual(interface.text(QAccessible.Text.Name), name)
                self.assertTrue(interface.text(QAccessible.Text.Description))
        label = next(item for item in self.panel.findChildren(QLabel) if item.text() == "API Key")
        self.assertIs(label.buddy(), self.panel.key_input)
        self.assertEqual(self.panel.show_key_button.accessibleName(), "显示 API Key")
        self.panel.show_key_button.click()
        self.assertEqual(self.panel.show_key_button.accessibleName(), "隐藏 API Key")

    def test_native_tab_order_reaches_key_reveal_and_model_in_form_order(self):
        self.panel.base_url_input.setFocus()
        self.app.processEvents()
        for expected in (self.panel.key_input, self.panel.show_key_button,
                         self.panel.model_combo, self.panel.fetch_models_button):
            QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Tab)
            self.assertIs(self.app.focusWidget(), expected)

    def test_local_http_result_matches_the_saved_payload_after_save_while_pending(self):
        with tempfile.TemporaryDirectory(prefix="aura-settings-round4-") as directory, MockApiServer() as server:
            path = Path(directory)
            settings = isolated_settings(path / "settings.ini", MemoryCredentialBackend())
            chat = ChatController(settings, store=ConversationStore(path / "archive.sqlite3"))
            panel = DebugPanel(FakePet(), settings, chat)
            panel.tabs.setCurrentIndex(1)
            panel.show()
            try:
                server.enqueue("连接成功", delay=.3)
                panel.base_url_input.setText(server.base_url)
                panel.model_input.setText("synthetic-local-model")
                panel.key_input.setText("synthetic-local-only-token")
                panel.test_connection_button.click()
                spin_until(server.received.is_set)
                panel.save_api_button.click()
                spin_until(lambda: not chat.busy)
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(server.requests[0]["body"]["model"], settings.load_api_settings().model)
                self.assertEqual(server.requests[0]["authorization"], "Bearer " + settings.get_api_key())
                self.assertEqual(panel.key_input.text(), "")
                self.assertIn("连接成功", panel.connection_label.text())
                self.assertNotIn("此结果对应测试发起时", panel.connection_label.text())
                self.assertNotIn("synthetic-local-only-token", (path / "settings.ini").read_text(encoding="utf-8"))
            finally:
                chat.shutdown()
                spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
                panel.hide()
                panel.deleteLater()
                chat.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
