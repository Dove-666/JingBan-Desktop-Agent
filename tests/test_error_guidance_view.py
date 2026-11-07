"""Native inline guidance follows the currently bound, visible global error."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.agents.service import _ERROR_MESSAGES
from desktop_pet.chat_controller import ChatController, REPLY_COMMIT_UNCONFIRMED, AGENT_REPLAY_STATUS
from desktop_pet.ui.chat_panel import ChatPanel
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from tests.test_assistant_ui import AssistantUiChat


class GuidanceChat(AssistantUiChat):
    message_retry_state = ChatController.message_retry_state

    def __init__(self, mode="unknown"):
        super().__init__()
        self._unconfirmed_reply_commits = {}
        self._requests = {}
        self._recoverable_messages = {}
        self._closed = self._busy = self._history_loading = False
        cid, message_id = self.current_conversation_id, "owned-error"
        metadata = {"request_status": "failed"}
        self.error = "独特的网络失败诊断。"
        self.error_action = "retry"
        if mode == "unknown":
            metadata["request_status"] = "interrupted"
            self._unconfirmed_reply_commits[(cid, message_id)] = {}
            self.error = REPLY_COMMIT_UNCONFIRMED
            self.error_action = "history"
        elif mode == "timeout":
            metadata.update(execution_mode="agent", agent_execution_started=True, agent_error_code="timeout")
            self.error = _ERROR_MESSAGES["timeout"]
            self.error_action = ""
        elif mode == "draining":
            self._requests[(cid, message_id)] = {"phase": "generating_reply", "metadata": {}}
        self.messages = [{"id": message_id, "conversation_id": cid, "role": "user", "source": "user",
            "content": "Owned fixture question", "metadata": metadata}]
        self._recoverable_messages[(cid, message_id)] = deepcopy(self.messages[0])
        self.error_conversation_id = cid
        self.error_message_id = message_id

    def send(self, *args, **kwargs):
        raise AssertionError("NO_REQUESTS_ALLOWED")


class CountingPanel(ChatPanel):
    def __init__(self, *args, **kwargs):
        self.render_depth = 0
        self.max_render_depth = 0
        self.render_count = 0
        super().__init__(*args, **kwargs)

    def _render_messages(self):
        self.render_depth += 1
        self.max_render_depth = max(self.max_render_depth, self.render_depth)
        self.render_count += 1
        try:
            return super()._render_messages()
        finally:
            self.render_depth -= 1


class ErrorGuidanceViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.panels, self.chats = [], []

    def tearDown(self):
        for panel in self.panels:
            self.assertFalse(panel.isVisible())
            panel.deleteLater()
        for chat in self.chats:
            owner = getattr(chat, "_local_draft_owner", None)
            if owner is not None:
                owner.shutdown(2)
                self.assertTrue(owner._stopped.is_set())
            chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def make(self, mode="unknown"):
        chat = GuidanceChat(mode)
        self.chats.append(chat)
        panel = CountingPanel(chat)
        panel.resize(980, 700)
        self.panels.append(panel)
        return chat, panel

    def test_unknown_keeps_global_diagnostic_and_reload_action_without_repeating_inline(self):
        chat, panel = self.make()
        self.assertNotIn(REPLY_COMMIT_UNCONFIRMED, panel.transcript.toPlainText())
        self.assertEqual(panel.error_label.text(), REPLY_COMMIT_UNCONFIRMED)
        self.assertFalse(panel.error_label.isHidden())
        self.assertEqual(panel.retry_button.text(), "重新读取")
        self.assertFalse(panel.retry_button.isHidden())
        self.assertTrue(panel.retry_button.isEnabled())
        self.assertEqual(chat.message_retry_state(chat.current_conversation_id, chat.error_message_id)["available"], False)
        self.assertEqual(panel.max_render_depth, 1)
        self.assertEqual(panel.render_count, 3)

    def test_timeout_keeps_global_reason_caption_and_no_replay_instruction(self):
        chat, panel = self.make("timeout")
        text = panel.transcript.toPlainText()
        self.assertIn("任务超时", text)
        self.assertNotIn(_ERROR_MESSAGES["timeout"], text)
        self.assertIn(AGENT_REPLAY_STATUS, text)
        self.assertEqual(panel.error_label.text(), _ERROR_MESSAGES["timeout"])
        self.assertTrue(panel.retry_button.isHidden())
        self.assertNotIn("aura-retry:", panel.transcript.toHtml())

    def test_ordinary_retry_and_draining_keep_distinct_diagnostic_and_existing_permissions(self):
        for mode in ("ready", "draining"):
            with self.subTest(mode=mode):
                chat, panel = self.make(mode)
                self.assertEqual(panel.error_label.text(), "独特的网络失败诊断。")
                self.assertIn("回复失败", panel.transcript.toPlainText())
                if mode == "ready":
                    self.assertIn("aura-retry:", panel.transcript.toHtml())
                    self.assertTrue(panel.retry_button.isEnabled())
                else:
                    guidance = chat.message_retry_state(chat.current_conversation_id, chat.error_message_id)["status"]
                    self.assertIn(guidance, panel.transcript.toPlainText())
                    self.assertFalse(panel.retry_button.isEnabled())

    def test_error_only_signal_restores_inline_and_identical_scope_does_not_rebuild(self):
        chat, panel = self.make()
        before = panel.render_count
        for _ in range(3):
            chat.error_changed.emit(chat.error)
        self.assertEqual(panel.render_count, before)
        chat.error = chat.error_action = chat.error_conversation_id = chat.error_message_id = ""
        chat.error_changed.emit("")
        self.assertEqual(panel.render_count, before + 1)
        self.assertIn(REPLY_COMMIT_UNCONFIRMED, panel.transcript.toPlainText())
        self.assertTrue(panel.error_label.isHidden())
        self.assertEqual(panel.max_render_depth, 1)

    def test_foreign_cid_message_or_missing_raw_error_never_suppresses_inline(self):
        for field, value in (("error_conversation_id", "other"), ("error_message_id", "other"), ("error", "")):
            with self.subTest(field=field):
                chat, panel = self.make()
                setattr(chat, field, value)
                chat.error_changed.emit(chat.error)
                self.assertIn(REPLY_COMMIT_UNCONFIRMED, panel.transcript.toPlainText())
        chat, panel = self.make()
        panel._rendered_conversation_id = None
        self.assertEqual(panel._inline_error_scope(),
                         (chat.current_conversation_id, chat.error_message_id, chat.error))

    def test_same_text_on_another_message_and_unique_long_diagnostic_are_not_hidden(self):
        chat, panel = self.make()
        second = deepcopy(chat.messages[0])
        second["id"] = "other-owned-error"
        chat.messages.append(second)
        chat._unconfirmed_reply_commits[(chat.current_conversation_id, second["id"])] = {}
        chat.messages_changed.emit()
        self.assertEqual(panel.transcript.toPlainText().count(REPLY_COMMIT_UNCONFIRMED), 1)
        chat.error = "另有独特诊断。" + REPLY_COMMIT_UNCONFIRMED
        chat.error_changed.emit(chat.error)
        self.assertEqual(panel.transcript.toPlainText().count(REPLY_COMMIT_UNCONFIRMED), 2)
        self.assertEqual(panel.error_label.text(), chat.error)

    def test_error_scope_refresh_preserves_draft_and_selection(self):
        chat, panel = self.make()
        draft = "Owned unsent draft with a selected segment"
        panel.input.setPlainText(draft)
        cursor = panel.input.textCursor()
        cursor.setPosition(6)
        cursor.setPosition(17, cursor.MoveMode.KeepAnchor)
        panel.input.setTextCursor(cursor)
        expected = (cursor.position(), cursor.anchor())
        chat.error = chat.error_action = chat.error_conversation_id = chat.error_message_id = ""
        chat.error_changed.emit("")
        self.assertEqual(panel.input.toPlainText(), draft)
        current = panel.input.textCursor()
        self.assertEqual((current.position(), current.anchor()), expected)

    def test_first_assistant_compatibility_render_uses_existing_global_error(self):
        for mode in ("unknown", "timeout"):
            with self.subTest(mode=mode):
                chat = GuidanceChat(mode)
                self.chats.append(chat)
                original_render = ChatPanel._render_messages
                with patch.object(ChatPanel, "_render_messages", autospec=True,
                                  side_effect=original_render) as native_render:
                    panel = AssistantChatPanel(chat)
                    self.panels.append(panel)
                    panel._web_start_timer.stop()
                    self.assertIsNone(panel.web_view)
                    self.assertIsNone(panel._rendered_conversation_id)
                    self.assertEqual(native_render.call_count, 0)
                    panel.compatibility_button.click()
                    self.assertEqual(native_render.call_count, 1)
                self.assertTrue(panel._native_mode)
                self.assertIsNone(panel.web_view)
                self.assertFalse(panel.isVisible())
                self.assertEqual(panel._rendered_conversation_id, chat.current_conversation_id)
                self.assertEqual(panel._rendered_inline_error_scope,
                                 (chat.current_conversation_id, chat.error_message_id, chat.error))
                self.assertEqual(panel.transcript.toPlainText().count(chat.error), 0)
                self.assertEqual(panel.error_label.text().count(chat.error), 1)
                self.assertFalse(panel.error_label.isHidden())
                if mode == "unknown":
                    self.assertEqual(panel.retry_button.text(), "重新读取")
                    self.assertFalse(panel.retry_button.isHidden())
                    self.assertTrue(panel.retry_button.isEnabled())
                else:
                    self.assertIn("任务超时", panel.transcript.toPlainText())
                    self.assertIn(AGENT_REPLAY_STATUS, panel.transcript.toPlainText())
                    self.assertTrue(panel.retry_button.isHidden())


if __name__ == "__main__":
    unittest.main()
