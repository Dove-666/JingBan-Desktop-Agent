"""Mixed visual/manual turns through the shipped web UI and temporary SQLite.

The client returns deterministic in-memory replies. No real API, credential vault,
screen reader or camera is opened. Visual timestamps intentionally use +08:00
while the ordinary chat controller creates UTC timestamps, as in production.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ChatReply
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from tests.test_assistant_ui import click_dom, eval_js, make_panel, set_dom_text, wait_js, wait_until
from tests.test_chat_controller import MemorySettings, RecordingClient


class VisualChatWebPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-visual-chat-dom-")
        self.store = ConversationStore(Path(self.directory.name) / "isolated.sqlite3")
        self.client = RecordingClient()
        self.client.reply = ChatReply("这是手动问题对应的合成回答。", "neutral")
        self.chat = ChatController(MemorySettings(), self.client, store=self.store)
        self.panel = None

    def tearDown(self):
        self.chat.shutdown()
        wait_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        if self.panel is not None:
            self.panel.hide()
            self.panel.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def visual_comment(self, kind, text):
        reference = self.chat.record_screen_comment(
            self.chat.current_conversation_id, text, event_id=str(uuid4()),
            app_name="合成摄像头" if kind == "camera" else "合成屏幕", context_summary="合成视觉情境",
            event_type="test_context", source_kind=kind,
            observed_at=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="microseconds"),
        )
        self.assertIsNotNone(reference)
        wait_until(lambda: self.chat._pending_screen_count.get(self.chat.current_conversation_id, 0) == 0)
        wait_until(lambda: not self.chat._runner._tasks)
        return reference["message_id"]

    def dom_messages(self):
        return eval_js(self.panel, "[...document.querySelectorAll('[data-message-id]')].map(node=>({id:node.dataset.messageId,text:node.querySelector('.markdown').textContent}))")

    def assert_dom_order(self, expected_ids):
        wait_js(self.panel, f"document.querySelectorAll('[data-message-id]').length === {len(expected_ids)}")
        self.assertEqual([item["id"] for item in self.dom_messages()], expected_ids)

    def test_visual_comment_then_manual_turn_stays_at_latest_end(self):
        visual_id = self.visual_comment("screen", "先出现的屏幕评论。")
        self.panel = make_panel(self.chat)
        question = "随后在当前白色窗口手动输入的问题。"
        set_dom_text(self.panel, "#message-input", question)
        click_dom(self.panel, "[data-testid=send-button]")
        wait_until(lambda: not self.chat.busy and bool(self.client.requests))
        wait_until(lambda: not self.chat._runner._tasks)
        saved = self.store.load_messages(self.chat.current_conversation_id)
        user = next(item for item in saved if item["role"] == "user")
        reply = next(item for item in saved if item["source"] == "assistant")
        expected = [visual_id, user["id"], reply["id"]]
        self.assertEqual([item["id"] for item in saved], expected)
        self.assert_dom_order(expected)
        self.assertEqual([item["text"] for item in self.dom_messages()],
                         ["先出现的屏幕评论。", question, "这是手动问题对应的合成回答。"])
        wait_js(self.panel, "document.querySelector('#message-input').value === ''")

    def test_later_camera_comment_and_reload_keep_complete_manual_turn(self):
        first_visual = self.visual_comment("screen", "第一条合成视觉评论。")
        self.panel = make_panel(self.chat)
        original_id = self.chat.current_conversation_id
        set_dom_text(self.panel, "#message-input", "视觉评论之后的手动消息需要保留。")
        click_dom(self.panel, "[data-testid=send-button]")
        wait_until(lambda: not self.chat.busy and bool(self.client.requests))
        wait_until(lambda: not self.chat._runner._tasks)
        manual = self.store.load_messages(original_id)
        user_id = next(item["id"] for item in manual if item["role"] == "user")
        reply_id = next(item["id"] for item in manual if item["source"] == "assistant")
        camera_id = self.visual_comment("camera", "后续摄像头评论不应覆盖手动问答。")
        expected = [first_visual, user_id, reply_id, camera_id]
        self.assert_dom_order(expected)
        self.panel._reload_web()
        wait_until(lambda: self.panel._frontend_ready)
        self.assert_dom_order(expected)
        self.chat.new_conversation()
        wait_js(self.panel, "document.querySelectorAll('[data-message-id]').length === 0")
        self.assertTrue(self.chat.open_conversation(original_id))
        wait_until(lambda: not self.chat.history_loading)
        self.assert_dom_order(expected)
        self.assertEqual(len(self.store.load_messages(original_id)), 4)


if __name__ == "__main__":
    unittest.main()
