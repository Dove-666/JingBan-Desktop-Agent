"""Independent export boundary checks using only synthetic temporary archives."""

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

from desktop_pet.conversations import ConversationStore
from desktop_pet.conversations.export import ExportError, export_conversation_file


class ExportEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-export-edge-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ConversationStore(self.root / "archive.sqlite3")
        self.cid = str(uuid4())

    def export_markdown_html(self):
        path = self.root / "result.md"
        export_conversation_file(self.store.path, self.cid, path)
        document = QTextDocument()
        document.setMarkdown(path.read_text(encoding="utf-8"))
        return document.toHtml()

    def test_image_prefix_cannot_be_activated_by_backslash_parity(self):
        for count in range(7):
            self.store.append_message(self.cid, "user", "\\" * count + "![合成图](https://example.invalid/tracker.png)", "user")
        self.assertNotIn("<img ", self.export_markdown_html())

    def test_title_and_summary_do_not_activate_markdown_images(self):
        self.store.append_message(self.cid, "user", "合成消息", "user")
        self.store.update_metadata(self.cid,
                                   title="标题 ![图](https://example.invalid/title.png)",
                                   summary="概况 ![图](https://example.invalid/summary.png)")
        self.assertNotIn("<img ", self.export_markdown_html())

    def test_screen_context_fields_do_not_activate_markdown_images(self):
        self.store.append_screen_comment(self.cid, "合成屏幕评论", event_id="synthetic-event",
                                         app_name="![图](https://example.invalid/app.png)",
                                         context_summary="![图](https://example.invalid/context.png)",
                                         event_type="![图](https://example.invalid/type.png)")
        self.assertNotIn("<img ", self.export_markdown_html())

    def test_dynamic_ids_and_times_cannot_escape_into_raw_html(self):
        self.store.append_message(self.cid, "user", "合成消息", "user",
                                  message_id='message`<img src="https://example.invalid/id.png">',
                                  created_at='2026-10-30 <img src="https://example.invalid/time.png">')
        self.store.append_screen_comment(self.cid, "合成屏幕评论",
                                         event_id='event`<img src="https://example.invalid/event.png">',
                                         app_name="Synthetic.exe", context_summary="合成情境", event_type="coding",
                                         observed_at='2026-10-30 <img src="https://example.invalid/observed.png">')
        self.assertNotIn("<img ", self.export_markdown_html())
        # Qt's Markdown renderer is stricter about raw inline HTML than some
        # other viewers; the exported source itself must also escape it.
        self.assertNotIn("<img ", (self.root / "result.md").read_text(encoding="utf-8"))

    def test_inline_image_uri_continuations_are_not_exported_as_raw_image_bytes(self):
        self.store.append_message(self.cid, "user", "![图](data:image/png;base64,\nSYNTHETIC_IMAGE_BASE64_BYTES)", "user")
        path = self.root / "result.json"
        export_conversation_file(self.store.path, self.cid, path, "json")
        content = json.loads(path.read_text(encoding="utf-8"))["messages"][0]["content"]
        self.assertNotIn("SYNTHETIC_IMAGE_BASE64_BYTES", content)

    def test_database_named_with_export_extension_is_never_replaced(self):
        store = ConversationStore(self.root / "archive.json")
        store.append_message(self.cid, "user", "数据库本体必须保留。", "user")
        with self.assertRaises(ExportError):
            export_conversation_file(store.path, self.cid, store.path, "json")
        self.assertEqual(store.load_messages(self.cid)[0]["content"], "数据库本体必须保留。")
        self.assertEqual(list(self.root.glob(".aura-export-*")), [])

    def test_incomplete_proactive_event_is_rejected_before_output(self):
        self.store.append_message(self.cid, "assistant", "缺关联的旧记录", "assistant")
        connection = sqlite3.connect(self.store.path)
        try:
            with connection:
                connection.execute("PRAGMA ignore_check_constraints=ON")
                connection.execute("UPDATE messages SET source='proactive_screen' WHERE conversation_id=?", (self.cid,))
        finally:
            connection.close()
        destination = self.root / "incomplete.json"
        with self.assertRaises(ExportError):
            export_conversation_file(self.store.path, self.cid, destination, "json")
        self.assertFalse(destination.exists())

    def test_fsync_failure_preserves_destination_and_removes_temporary_history(self):
        self.store.append_message(self.cid, "user", "必须保留完整记录", "user")
        destination = self.root / "safe.json"
        destination.write_text("existing-destination", encoding="utf-8")
        with patch("desktop_pet.conversations.export.os.fsync", side_effect=OSError("synthetic private text")):
            with self.assertRaises(ExportError) as caught:
                export_conversation_file(self.store.path, self.cid, destination, "json")
        self.assertNotIn("synthetic private text", str(caught.exception))
        self.assertEqual(destination.read_text(encoding="utf-8"), "existing-destination")
        self.assertEqual(list(self.root.glob(".aura-export-*")), [])

    def test_illegal_windows_destinations_leave_no_temporary_files(self):
        self.store.append_message(self.cid, "user", "合成记录", "user")
        if os.name != "nt":
            self.assertTrue(self.store.path.is_file())
            return
        for name in ("invalid:name.json", "invalid?name.json", "invalid|name.json"):
            with self.subTest(name=name):
                with self.assertRaises(ExportError):
                    export_conversation_file(self.store.path, self.cid, self.root / name, "json")
                self.assertEqual(list(self.root.glob(".aura-export-*")), [])


if __name__ == "__main__":
    unittest.main()
