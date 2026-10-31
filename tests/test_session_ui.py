"""Exercise session selection and memory controls without providers or real keys."""

from dataclasses import dataclass, replace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from desktop_pet.ui.chat_panel import ChatPanel
from desktop_pet.ui.memory_panel import MemoryPanel
from tests.test_debug_ui import FakeChat


class SessionChat(FakeChat):
    conversations_changed = Signal()
    conversation_changed = Signal()
    history_busy_changed = Signal(bool)
    message_focus_requested = Signal(str)
    context_usage_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.current_conversation_id = "current"
        self.current_title = "开发桌宠"
        self.current_summary = "正在检查聊天和屏幕事件。"
        self.conversations = [
            {"id": "current", "title": "开发桌宠", "summary": "正在检查聊天和屏幕事件。",
             "updated_at": "2026-10-30T12:00:00", "message_count": 2},
            {"id": "old", "title": "旧会话", "summary": "只属于旧会话。",
             "updated_at": "2026-10-29T12:00:00", "message_count": 1},
        ]
        self.messages = [{"id": "message-1", "role": "user", "content": "开发桌宠"}]
        self.history_loading = False
        self.context_usage = "估算输入 120 / 8192 Token"
        self.calls = []

    def new_conversation(self):
        self.calls.append(("new",))
        self.current_conversation_id = "new-session"
        self.current_title = "新对话"
        self.messages = []
        self.conversation_changed.emit()
        return self.current_conversation_id

    def open_conversation(self, conversation_id, focus_message_id=None):
        self.calls.append(("open", conversation_id))
        self.current_conversation_id = conversation_id
        self.current_title = next(item["title"] for item in self.conversations if item["id"] == conversation_id)
        self.messages = [{"id": "old-message", "role": "assistant", "content": "历史记录"}]
        self.conversation_changed.emit()
        return True

    def search_conversations(self, query):
        self.calls.append(("search", query))

    def rename_conversation(self, conversation_id, title):
        self.calls.append(("rename", conversation_id, title))
        return True

    def delete_conversation(self, conversation_id):
        self.calls.append(("delete", conversation_id))
        return True

    def retry(self):
        self.calls.append(("retry",))
        return True


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = False
    automatic: bool = False
    user_id: str = "stable-test-user"
    use_chat_model: bool = True
    llm_base_url: str = "https://example.test/v1"
    llm_model: str = "memory-model"
    embedding_base_url: str = "https://embedding.test/v1"
    embedding_model: str = "embedding-model"
    embedding_dimensions: int = 1536
    timeout_seconds: float = 20
    relevance_threshold: float = 0.6
    max_results: int = 4
    extraction_context_tokens: int = 65536
    extraction_output_tokens: int = 1024


class MemoryControllerFake(QObject):
    settings_changed = Signal(object)
    busy_changed = Signal(bool)
    error_changed = Signal(str)
    status_changed = Signal(str)
    memories_changed = Signal()
    automatic_pause_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.settings = MemoryConfig()
        self.busy = False
        self.error = ""
        self.status = "记忆尚未启用。"
        self.memories = []
        self.calls = []
        self.automatic_paused = False

    def set_automatic_paused(self, paused):
        self.automatic_paused = bool(paused)
        self.calls.append(("pause_automatic", bool(paused)))
        self.automatic_pause_changed.emit(self.automatic_paused)
        return True

    def save_settings(self, settings, *, llm_key="", embedding_key=""):
        self.calls.append(("save", settings, llm_key, embedding_key))
        self.settings = settings
        self.settings_changed.emit(settings)
        return True

    def refresh(self, query=""):
        self.calls.append(("refresh", query))

    def delete_memory(self, memory_id):
        self.calls.append(("delete", memory_id))

    def clear_memories(self):
        self.calls.append(("clear",))

    def clear_llm_key(self):
        self.calls.append(("clear_llm_key",))

    def clear_embedding_key(self):
        self.calls.append(("clear_embedding_key",))

    def test_connection(self):
        self.calls.append(("test",))


class CredentialAwareMemoryControllerFake(MemoryControllerFake):
    credentials_status_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.credentials_status = "复用聊天密钥：已保存\nEmbedding 密钥：未保存"


class SessionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = SessionChat()
        self.memory = MemoryControllerFake()
        self.panel = ChatPanel(self.chat, memory_controller=self.memory)
        self.panel.resize(850, 650)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_sidebar_shows_only_title_and_current_selection(self):
        self.assertEqual(self.panel.title_label.text(), "开发桌宠")
        self.assertEqual(self.panel.conversation_list.count(), 2)
        item = self.panel.conversation_list.currentItem()
        self.assertEqual(item.data(Qt.ItemDataRole.UserRole), "current")
        self.assertEqual(item.text(), self.chat.current_title)
        self.assertEqual(item.toolTip(), f"<qt>{self.chat.current_title}</qt>")

    def test_new_conversation_clears_draft_input_and_selects_no_old_session(self):
        self.panel.input.setPlainText("未发送草稿")
        self.panel.new_button.click()
        self.assertEqual(self.chat.calls[-1], ("new",))
        self.assertEqual(self.panel.title_label.text(), "新对话")
        self.assertIsNone(self.panel.conversation_list.currentItem())
        self.assertIn("还没有消息", self.panel.transcript.toPlainText())
        self.assertEqual(self.panel.input.toPlainText(), "")

    def test_click_history_opens_correct_id_and_updates_title(self):
        item = self.panel.conversation_list.item(1)
        self.panel.conversation_list.itemClicked.emit(item)
        self.assertEqual(self.chat.calls[-1], ("open", "old"))
        self.assertEqual(self.panel.title_label.text(), "旧会话")
        self.assertIn("历史记录", self.panel.transcript.toPlainText())
        self.assertEqual(self.panel.conversation_list.currentItem().data(Qt.ItemDataRole.UserRole), "old")

    def test_search_is_debounced_and_passes_plain_query(self):
        self.panel.search_input.setText("桌")
        self.panel.search_input.setText("桌宠 API")
        QTest.qWait(350)
        self.assertEqual([call for call in self.chat.calls if call[0] == "search"], [("search", "桌宠 API")])

    def test_rename_cancel_and_delete_confirmation(self):
        with patch.object(QInputDialog, "getText", return_value=("重命名桌宠", False)):
            self.panel.rename_button.click()
        self.assertFalse(self.chat.calls)
        with patch.object(QInputDialog, "getText", return_value=("重命名桌宠", True)):
            self.panel.rename_button.click()
        self.assertEqual(self.chat.calls[-1], ("rename", "current", "重命名桌宠"))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.panel.delete_button.click()
        self.assertNotEqual(self.chat.calls[-1][0], "delete")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel.delete_button.click()
        self.assertEqual(self.chat.calls[-1], ("delete", "current"))

    def test_history_can_switch_while_request_pending_but_send_disabled(self):
        self.chat.busy = True
        self.chat.busy_changed.emit(True)
        self.panel.input.setPlainText("不能重复发送")
        self.assertFalse(self.panel.send_button.isEnabled())
        self.assertFalse(self.panel.delete_button.isEnabled())
        self.assertTrue(self.panel.conversation_list.isEnabled())
        self.panel.conversation_list.itemClicked.emit(self.panel.conversation_list.item(1))
        self.assertEqual(self.chat.current_conversation_id, "old")

    def test_load_failure_keeps_existing_messages_and_explicit_error(self):
        self.chat.history_loading = True
        self.chat.history_busy_changed.emit(True)
        self.assertIn("正在加载", self.panel.history_status.text())
        self.chat.error_changed.emit("历史读取失败，原会话仍保留。")
        self.chat.history_loading = False
        self.chat.history_busy_changed.emit(False)
        self.assertIn("开发桌宠", self.panel.transcript.toPlainText())
        self.assertIn("原会话仍保留", self.panel.error_label.text())
        self.assertTrue(self.panel.retry_button.isVisible())
        self.panel.retry_button.click()
        self.assertEqual(self.chat.calls[-1], ("retry",))

    def test_screen_source_details_focus_and_provider_html_stay_plain(self):
        payload = '<img src="https://invalid.test/screenshot"><b>别执行我</b>'
        self.chat.messages = [{
            "id": "screen-comment-1", "role": "assistant", "source": "proactive_screen", "content": payload,
            "screen_event": {"app_name": "<b>编辑器</b>", "event_type": "error", "observed_at": "2026-10-30",
                             "context_summary": "Python 错误：" + payload},
        }]
        self.chat.messages_changed.emit()
        self.assertIn("来自屏幕感知", self.panel.transcript.toPlainText())
        self.assertIn(payload, self.panel.transcript.toPlainText())
        self.assertNotIn('<img src="https://invalid.test', self.panel.transcript.toHtml())
        self.panel._open_event(QUrl("https://invalid.test"))
        self.assertTrue(self.panel.event_details.isHidden())
        self.chat.message_focus_requested.emit("screen-comment-1")
        self.assertFalse(self.panel.event_details.isHidden())
        self.assertEqual(self.panel.event_details.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("<b>编辑器</b>", self.panel.event_details.text())
        self.assertIn("Python 错误", self.panel.event_details.text())
        self.panel.close_event_button.click()
        self.panel._open_event(QUrl("aura-event:screen-comment-1"))
        self.assertFalse(self.panel.event_details.isHidden())

    def test_sidebar_toggle_budget_usage_and_memory_entry(self):
        self.panel.history_toggle.click()
        self.assertTrue(self.panel.sidebar.isHidden())
        self.panel.history_toggle.click()
        self.assertFalse(self.panel.sidebar.isHidden())
        self.chat.context_usage_changed.emit("估算 900 Token，保留 12 条消息")
        self.assertIn("900 Token", self.panel.context_usage_label.text())
        self.panel.memory_button.click()
        self.assertTrue(self.panel.memory_panel.isVisible())
        self.assertEqual(self.memory.calls[-1], ("refresh", ""))


class MemoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.controller = MemoryControllerFake()
        self.panel = MemoryPanel(self.controller)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_independent_embedding_settings_save_without_exposing_keys(self):
        self.panel.enabled_checkbox.setChecked(True)
        self.panel.use_chat_checkbox.setChecked(False)
        self.panel.llm_key_input.setText("not-a-real-llm-key")
        self.panel.embedding_key_input.setText("not-a-real-embedding-key")
        self.panel.embedding_model_input.setText("separate-embedding")
        self.panel.dimensions_input.setValue(768)
        self.panel.extraction_context_input.setValue(32768)
        self.panel.extraction_output_input.setValue(512)
        self.panel.save_button.click()
        call = self.controller.calls[-1]
        self.assertEqual(call[0], "save")
        self.assertEqual(call[1].embedding_model, "separate-embedding")
        self.assertEqual(call[1].embedding_dimensions, 768)
        self.assertEqual(call[1].extraction_context_tokens, 32768)
        self.assertEqual(call[1].extraction_output_tokens, 512)
        self.assertFalse(call[1].use_chat_model)
        self.assertEqual(call[2:], ("not-a-real-llm-key", "not-a-real-embedding-key"))
        self.assertEqual(self.panel.llm_key_input.text(), "")
        self.assertEqual(self.panel.embedding_key_input.text(), "")
        self.assertIn("stable-test-user", self.panel.user_id_label.text())

    def test_credential_status_is_separate_plain_text_and_survives_local_status_updates(self):
        controller = CredentialAwareMemoryControllerFake()
        panel = MemoryPanel(controller)
        try:
            self.assertIn("Embedding 密钥：未保存", panel.credentials_status_label.text())
            controller.status_changed.emit("已从本地读取 0 条长期记忆；此次操作未验证连接。")
            self.assertIn("Embedding 密钥：未保存", panel.credentials_status_label.text())
            controller.credentials_status_changed.emit("Embedding 密钥：<b>不可读取</b>")
            self.assertEqual(panel.credentials_status_label.text(), "Embedding 密钥：<b>不可读取</b>")
            self.assertEqual(panel.credentials_status_label.textFormat(), Qt.TextFormat.PlainText)
            self.assertIn("从本地读取", panel.status_label.text())
        finally:
            panel.close()
            panel.deleteLater()

    def test_legacy_controller_without_credential_signal_still_exposes_scoped_test(self):
        self.assertIn("凭据状态尚未读取", self.panel.credentials_status_label.text())
        self.assertEqual(self.panel.test_button.text(), "测试记忆检索")
        self.panel.test_button.click()
        self.assertEqual(self.controller.calls[-1], ("test",))
        self.assertIn("不会提炼", self.panel.test_button.toolTip())

    def test_pause_automatic_is_independent_from_reading_memories(self):
        self.controller.settings = replace(self.controller.settings, enabled=True, automatic=True)
        self.controller.settings_changed.emit(self.controller.settings)
        self.panel.pause_checkbox.click()
        self.assertTrue(self.controller.automatic_paused)
        self.assertTrue(self.controller.settings.automatic)
        self.assertTrue(self.controller.settings.enabled)
        self.assertFalse(any(call[0] == "save" for call in self.controller.calls))
        self.panel.search_input.setText("AURA 项目")
        self.panel.refresh_button.click()
        self.assertEqual(self.controller.calls[-1], ("refresh", "AURA 项目"))

    def test_delete_and_clear_require_confirmation_and_use_real_ids(self):
        self.controller.memories = [{"id": "memory-id", "memory": "用户正在开发 AURA"}]
        self.controller.memories_changed.emit()
        self.panel.memory_list.setCurrentRow(0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.panel.delete_button.click()
            self.panel.clear_button.click()
        self.assertFalse(self.controller.calls)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel.delete_button.click()
            self.panel.clear_button.click()
        self.assertEqual(self.controller.calls, [("delete", "memory-id"), ("clear",)])

    def test_busy_disables_destructive_operations_and_errors_are_plain_and_redacted(self):
        self.controller.busy = True
        self.controller.busy_changed.emit(True)
        self.assertFalse(self.panel.clear_button.isEnabled())
        self.assertFalse(self.panel.save_button.isEnabled())
        self.panel.embedding_key_input.setText("pending-test-key")
        self.controller.error_changed.emit("<b>失败</b> pending-test-key")
        self.assertIn("[密钥已隐藏]", self.panel.error_label.text())
        self.assertNotIn("pending-test-key", self.panel.error_label.text())
        self.assertEqual(self.panel.error_label.textFormat(), Qt.TextFormat.PlainText)

    def test_close_hides_and_discards_sensitive_drafts(self):
        self.panel.llm_key_input.setText("pending-test-value")
        self.panel.close()
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.panel.llm_key_input.text(), "")
        self.panel.present()
        self.assertTrue(self.panel.isVisible())


if __name__ == "__main__":
    unittest.main()
