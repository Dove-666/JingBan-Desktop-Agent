"""Search hit navigation and long-history recovery without production data."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_session_ui import SessionChat


class HistoryNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-history-navigation-")
        base = Path(self.temp.name)
        self.store = ConversationStore(base / "fixture.sqlite3")
        settings = isolated_settings(base / "fixture.ini", MemoryCredentialBackend())
        self.chat = ChatController(settings, store=self.store)
        self.panels = []
    def tearDown(self):
        self.chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for panel in self.panels:
            panel.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()
    def search(self, term):
        self.chat.search_conversations(term)
        spin_until(lambda: self.chat._listed_query == term and not self.chat._runner._tasks)
    def open(self, cid, *args, **kwargs):
        self.assertTrue(self.chat.open_conversation(cid, *args, **kwargs))
        spin_until(lambda: not self.chat.history_loading)

    def test_history_body_search_opens_at_real_hit_far_above_latest_messages(self):
        for index in range(80):
            self.store.append_message("one", "user", "只有很早的消息命中" if index == 4 else f"其他记录 {index}",
                                      "user", message_id=f"m-{index}")
        self.search("只有很早的消息命中")
        focus = QSignalSpy(self.chat.message_focus_requested)
        self.open("one")
        self.assertEqual(len(self.chat.messages), 80)
        self.assertEqual(focus.count(), 1)
        self.assertEqual(focus.at(0)[0], "m-4")

    def test_latest_literal_body_hit_uses_real_timestamp_order_and_stays_in_its_session(self):
        self.store.append_message("one", "user", "任务100%_x", "user", message_id="old-hit",
                                  created_at="2026-10-01T08:00:00+08:00")
        self.store.append_message("one", "assistant", "任务100%_x", "assistant", message_id="new-hit",
                                  created_at="2026-10-01T01:00:00Z")
        self.store.append_message("two", "user", "任务100%_x", "user", message_id="other-session")
        self.search("100%_x")
        focus = QSignalSpy(self.chat.message_focus_requested)
        self.open("one")
        self.assertEqual(focus.at(0)[0], "new-hit")
        self.assertNotIn("other-session", [item["id"] for item in self.chat.messages])

    def test_explicit_link_focus_takes_precedence_and_cleared_search_does_not_auto_focus(self):
        self.store.append_message("one", "user", "正文目标", "user", message_id="hit")
        self.store.append_message("one", "assistant", "显式气泡目标", "assistant", message_id="linked")
        self.search("正文目标")
        focus = QSignalSpy(self.chat.message_focus_requested)
        self.open("one", focus_message_id="linked")
        self.assertEqual(focus.at(0)[0], "linked")
        self.search("")
        self.open("one")
        self.assertEqual(focus.count(), 1)

    def test_title_only_search_has_no_arbitrary_message_focus(self):
        self.store.append_message("one", "user", "其他正文", "user", message_id="u")
        self.store.rename_conversation("one", "只在标题命中")
        self.search("只在标题命中")
        focus = QSignalSpy(self.chat.message_focus_requested)
        self.open("one")
        self.assertEqual(focus.count(), 0)

    def test_hidden_native_transcript_is_deferred_until_compatibility_mode_and_keeps_focus_draft(self):
        fake = SessionChat()
        panel = AssistantChatPanel(fake)
        self.panels.append(panel)
        fake.messages = [{"id": f"long-{index}", "conversation_id": "current", "role": "assistant",
                          "content": "合成历史内容。" * 30} for index in range(1000)]
        fake.messages[3].update(source="proactive_camera", screen_event={"context_summary": "确切的摄像头情境"})
        panel.dispatch_web_action("draft", {"conversationId": "current", "text": "网页未发出的草稿"})
        with patch.object(panel.transcript, "setHtml", wraps=panel.transcript.setHtml) as render:
            fake.messages_changed.emit()
            fake.message_focus_requested.emit("long-3")
            self.assertEqual(render.call_count, 0)
            self.assertEqual(len(panel.web_state()["messages"]), 1000)
            self.assertEqual(len(json.loads(panel.bridge.getState())["messages"]), 1000)
            panel._publish_web_state()
            self.assertEqual(render.call_count, 0)
            self.assertEqual(panel._focus_request["id"], "long-3")
            panel._activate_native()
            self.assertEqual(render.call_count, 1)
            self.assertIn("合成历史内容", panel.transcript.toPlainText())
            self.assertIn("确切的摄像头情境", panel.event_details.text())
            self.assertIsNone(panel._pending_focus)
            self.assertEqual(panel.input.toPlainText(), "网页未发出的草稿")
            with patch.object(panel, "_ensure_web"):
                panel._reload_web()
            fake.messages.append({"id": "later", "role": "assistant", "content": "后来保存的消息"})
            fake.messages_changed.emit()
            self.assertEqual(render.call_count, 1)
            panel._activate_native()
            self.assertEqual(render.call_count, 2)
            self.assertIn("后来保存的消息", panel.transcript.toPlainText())


if __name__ == "__main__":
    unittest.main()
