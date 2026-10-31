"""Selection source cards in the shipped WebEngine page, using isolated data."""

import json
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.selection.context import SelectionContext
from tests.test_application import MemoryCredentialBackend, isolated_settings
from tests.test_assistant_ui import click_dom, eval_js, make_panel, wait_js, wait_until
from tests.test_selection_application import RecordingClient


class SelectionWebUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-selection-web-ui-")
        self.path = Path(self.temp.name)
        self.settings = isolated_settings(self.path / "settings.ini", MemoryCredentialBackend())
        self.store = ConversationStore(self.path / "archive.sqlite3")
        self.client = RecordingClient()
        self.chat = ChatController(self.settings, self.client, store=self.store)
        self.panel = None
        wait_until(lambda: not self.chat._runner._tasks)
        self.conversation_id = self.chat.current_conversation_id
        self.source = SelectionContext(
            text="<img src=x onerror='window.__selectionXss=1'>\n合成选区" + "字符" * 1600,
            title="合成页面 <script>window.__selectionXss=2</script>",
            url="https://example.invalid/article?session=discard",
            page_text="PAGE_BODY_NOT_FOR_WEB_SNAPSHOT\n" + "合成有限正文" * 1200,
            notes=("<svg onload='window.__selectionXss=3'>未执行内容</svg>",),
            captured_at="2026-10-31T12:00:00+08:00",
        )
        self.user = self.store.append_message(
            self.conversation_id, "user", "请解释这段合成选区。", "user",
            metadata={"request_status": "complete", "selection_context": self.source.to_dict()},
        )
        self.store.append_message(self.conversation_id, "assistant", "这是合成回答。", "assistant")
        self.title = "只有标题 <img src=x>"
        self.summary = "SIDEBAR_SUMMARY_MUST_STAY_HIDDEN"
        self.store.rename_conversation(self.conversation_id, self.title)
        self.store.update_metadata(self.conversation_id, summary=self.summary)
        self.assertTrue(self.chat.open_conversation(self.conversation_id))
        wait_until(lambda: not self.chat.history_loading and not self.chat._runner._tasks)
        self.panel = make_panel(self.chat)
        wait_js(self.panel, "document.querySelectorAll('[data-testid=selection-source]').length === 1")

    def tearDown(self):
        self.chat.shutdown()
        wait_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        if self.panel is not None:
            self.panel.hide()
            self.panel.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def test_bridge_exposes_bounded_preview_and_count_without_republishing_full_page(self):
        state = self.panel.web_state()
        user = next(item for item in state["messages"] if item["role"] == "user")
        source = user["metadata"]["selection_context"]
        self.assertEqual(source["text"], self.source.text[:2000])
        self.assertEqual(source["title"], self.source.title)
        self.assertEqual(source["page_characters"], len(self.source.page_text))
        self.assertEqual(source["notes"], list(self.source.notes))
        self.assertNotIn("page_text", source)
        self.assertNotIn("PAGE_BODY_NOT_FOR_WEB_SNAPSHOT", json.dumps(state))
        archived = self.store.load_messages(self.conversation_id)[0]["metadata"]["selection_context"]
        self.assertEqual(archived["text"], self.source.text)
        self.assertEqual(archived["page_text"], self.source.page_text)
        self.assertEqual(self.client.requests, [])

    def test_packaged_page_expands_user_source_and_escapes_html_in_all_source_fields(self):
        self.assertFalse(eval_js(self.panel, "document.querySelector('.selection-source').open"))
        click_dom(self.panel, ".selection-source summary")
        self.assertTrue(eval_js(self.panel, "document.querySelector('.selection-source').open"))
        self.assertEqual(eval_js(self.panel, "document.querySelector('.selection-source p').textContent"),
                         self.source.text[:2000])
        self.assertIn(self.source.title,
                      eval_js(self.panel, "document.querySelector('.selection-source summary').textContent"))
        self.assertIn(self.source.notes[0],
                      eval_js(self.panel, "document.querySelector('.selection-source').textContent"))
        # The trusted source-link control contains its own SVG icon. Reject
        # executable markup/handlers from the quoted source, not that UI icon.
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.selection-source img, .selection-source script, .selection-source iframe, .selection-source [onload], .selection-source [onerror]').length"), 0)
        self.assertIsNone(eval_js(self.panel, "window.__selectionXss ?? null"))
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.message-assistant .selection-source').length"), 0)
        click_dom(self.panel, ".selection-source summary")
        self.assertFalse(eval_js(self.panel, "document.querySelector('.selection-source').open"))
        self.assertEqual(self.client.requests, [])

    def test_history_remains_titles_only_and_source_survives_frontend_reload(self):
        wait_js(self.panel, "document.querySelectorAll('.conversation-link').length === 1")
        self.assertEqual(eval_js(self.panel, "document.querySelector('.conversation-link').textContent"), self.title)
        self.assertEqual(eval_js(self.panel, "document.querySelector('.conversation-link').title"), self.title)
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.conversation-time, .conversation-summary, .conversation-list time, .conversation-list img').length"), 0)
        self.assertNotIn(self.summary, eval_js(self.panel, "document.querySelector('.conversation-list').textContent"))
        self.panel._reload_web()
        wait_until(lambda: self.panel._frontend_ready)
        wait_js(self.panel, "document.querySelectorAll('[data-testid=selection-source]').length === 1")
        self.assertEqual(eval_js(self.panel, "document.querySelector('.conversation-link').textContent"), self.title)
        click_dom(self.panel, ".selection-source summary")
        self.assertEqual(eval_js(self.panel, "document.querySelector('.selection-source p').textContent"), self.source.text[:2000])
        self.assertEqual(self.chat.current_conversation_id, self.conversation_id)
        self.assertEqual(len(self.store.load_messages(self.conversation_id)), 2)
        self.assertEqual(self.client.requests, [])


if __name__ == "__main__":
    unittest.main()
