"""Native picker/staging writes shared image references, never raw chat data."""

import json
import threading
import unittest
from unittest.mock import patch

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.chat_panel import ChatPanel
from tests import test_chat_images as image_fixtures
from tests import test_remote_controller as fixtures
from tests.mock_api import MockApiServer


class ChatImageStageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        image_fixtures.ChatImageTests.setUp(self)
        self.panel = ChatPanel(self.chat)
        self.folder = self.path / "PRIVATE_DIRECTORY"
        self.folder.mkdir()
        self.image_path = self.folder / "public.png"
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(QColor("#75b8f4"))
        self.assertTrue(image.save(str(self.image_path), "PNG"))

    def tearDown(self):
        fixtures.spin_until(lambda: not self.panel._image_preparing)
        self.panel.hide()
        self.panel.deleteLater()
        image_fixtures.ChatImageTests.tearDown(self)

    rpc = image_fixtures.ChatImageTests.rpc
    configure = image_fixtures.ChatImageTests.configure

    def select(self):
        with patch("desktop_pet.ui.chat_panel.QFileDialog.getOpenFileName", return_value=(str(self.image_path), "")):
            self.assertTrue(self.panel._select_image())
        fixtures.spin_until(lambda: not self.panel._image_preparing)
        self.assertEqual(self.panel._image_stage_error, "")
        return self.panel._current_staged_image()

    def test_native_picker_import_send_and_phone_sync_use_the_same_inbox_reference(self):
        staged = self.select()
        self.assertEqual(staged["filename"], "public.png")
        self.assertNotIn("PRIVATE_DIRECTORY", json.dumps(staged))
        self.assertEqual(self.rpc("image.get", {"id": staged["image_id"]})["result"]["width"], 64)
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue("看到了蓝色图片")
            self.panel.input.setPlainText("请检查这个附件")
            self.panel._send()
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertIsNone(self.panel._current_staged_image())
            self.assertEqual(self.panel.input.toPlainText(), "")
            content = server.requests[0]["body"]["messages"][-1]["content"]
            self.assertEqual(content[0]["text"], "请检查这个附件")
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
            user = self.chat.store.load_messages(self.cid)[0]
            self.assertEqual(user["metadata"]["image_id"], staged["image_id"])
            synced = next(item["data"] for item in self.remote.sync.snapshot()["items"]
                          if item["entity"] == "message" and item["id"] == user["id"])
            self.assertEqual(synced["image_id"], staged["image_id"])
            self.assertNotIn("data:image", json.dumps(self.chat.store.load_messages(self.cid)))

    def test_image_only_send_is_explicit_and_selection_itself_never_calls_model(self):
        with MockApiServer() as server:
            self.configure(server)
            self.select()
            self.assertEqual(server.requests, [])
            self.assertTrue(self.panel.send_button.isEnabled())
            server.enqueue("图片单独发送已读取")
            self.panel._send()
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(server.requests[0]["body"]["messages"][-1]["content"][0]["text"], "请描述这张图片。")

    def test_removal_preserves_text_and_other_conversation_stage_is_not_sent(self):
        self.chat.store.append_message(self.cid, "assistant", "用于保存测试会话", "assistant")
        staged = self.select()
        self.panel.input.setPlainText("仍未发送的文字")
        original = self.chat.current_conversation_id
        self.chat.new_conversation()
        fixtures.spin_until(lambda: not self.chat.history_loading)
        self.assertIsNone(self.panel._current_staged_image())
        self.assertIn(original, self.panel._staged_images)
        self.chat.open_conversation(original)
        fixtures.spin_until(lambda: not self.chat.history_loading)
        self.assertEqual(self.panel._current_staged_image()["image_id"], staged["image_id"])
        self.assertTrue(self.panel._remove_staged_image())
        self.assertEqual(self.panel.input.toPlainText(), "仍未发送的文字")
        self.assertEqual(len(self.chat.store.load_messages(original)), 1)

    def test_invalid_file_reports_only_static_error_and_does_not_keep_old_attachment(self):
        self.select()
        self.image_path.write_bytes(b"not an image")
        self.assertTrue(self.panel._stage_local_image(str(self.image_path), self.cid))
        fixtures.spin_until(lambda: not self.panel._image_preparing)
        self.assertIsNone(self.panel._current_staged_image())
        self.assertIn("无法准备图片", self.panel._image_stage_error)
        self.assertNotIn(str(self.image_path), self.panel._image_stage_error)

    def test_conversation_invalidation_discards_a_late_import_result(self):
        entered, release = threading.Event(), threading.Event()
        original = self.chat.import_local_image

        def delayed(path):
            entered.set()
            release.wait(3)
            return original(path)

        with patch.object(self.chat, "import_local_image", delayed):
            self.assertTrue(self.panel._stage_local_image(str(self.image_path), self.cid))
            fixtures.spin_until(entered.is_set)
            self.chat.conversation_invalidated.emit(self.cid)
            release.set()
            fixtures.spin_until(lambda: not self.panel._image_stage_runner._tasks)
        self.assertIsNone(self.panel._current_staged_image())
        self.assertFalse(self.panel._image_preparing)


if __name__ == "__main__":
    unittest.main()
