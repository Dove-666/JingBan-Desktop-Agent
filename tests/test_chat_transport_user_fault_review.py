"""Real Qt controller/store/transport faults using owned localhost responses.

The hidden native composer exercises draft/attachment ownership; it never
activates a window, loads a production profile, or contacts a real provider.
"""
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiClient
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.remote.attachments import ImageInbox
from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.chat_panel import ChatPanel
from tests.mock_api import MockApiServer
from tests.test_application import MemoryCredentialBackend, isolated_settings
from tests.test_chat_stream import SseServer


KEY = "FAKE_OWNED_USER_FAULT_KEY"
DRAFT = "下一条尚未发送的新草稿🐳"


class ChatTransportUserFaultReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.owner = tempfile.TemporaryDirectory(prefix="aura-chat-fault-review-")
        self.root = Path(self.owner.name)
        self.settings = isolated_settings(self.root/"settings.ini", MemoryCredentialBackend())
        self.settings.save_api_key(KEY)
        self.store = ConversationStore(self.root/"archive.sqlite3")
        self.chat = ChatController(self.settings, ApiClient(), store=self.store, metadata_client_factory=lambda: None)
        self.chat.set_image_inbox(ImageInbox(self.root/"inbox"))
        self.panel = ChatPanel(self.chat)
        self.cid = self.chat.current_conversation_id
        self.wait(lambda: not self.chat.history_loading and self.chat._local_draft_owner._loaded.is_set())

    def tearDown(self):
        self.chat.shutdown()
        self.wait(lambda: QThreadPool.globalInstance().activeThreadCount() == 0
                  and self.chat._local_draft_owner._stopped.is_set())
        self.panel.close(); self.panel.deleteLater(); self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.owner.cleanup()

    def wait(self, predicate, timeout=8):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if predicate(): return
            QTest.qWait(10); time.sleep(.002)
        self.assertTrue(predicate(), "Owned Qt/HTTP fixture did not settle")

    def configure(self, server):
        self.settings.save_api_settings(ApiSettings(server.base_url, "owned-fault-fixture", 4))

    def rows(self):
        return self.store.load_messages(self.cid)

    def users(self):
        return [row for row in self.rows() if row["role"] == "user"]

    def sent(self, text="原来的问题"):
        self.panel.input.setPlainText(text)
        self.panel._send()
        self.assertTrue(self.chat.busy)
        self.panel.input.setPlainText(DRAFT)
        self.assertFalse(self.chat.send("不能在忙碌时重复发送"))
        self.wait(lambda: len(self.users()) == 1)
        return self.users()[0]["id"]

    def assert_draft(self, text=DRAFT):
        self.assertEqual(text, self.panel.input.toPlainText())
        owner = self.chat._local_draft_owner
        self.assertEqual(text, owner.snapshot(self.cid).text)
        ticket = owner.flush()
        self.assertTrue(owner.wait(ticket, 2))
        self.assertEqual(text, owner._store.lookup(self.cid).text)

    def assert_failed_once(self, message_id, request_count):
        self.wait(lambda: not self.chat.busy)
        self.assertEqual([message_id], [row["id"] for row in self.users()])
        self.assertIn(self.users()[0]["metadata"]["request_status"], {"failed", "interrupted"})
        self.assertEqual([], [row for row in self.rows() if row["role"] == "assistant"])
        self.assertTrue(self.chat.error)
        for secret in (KEY, "NEVER_PUBLIC_PROVIDER_BODY"):
            self.assertNotIn(secret, self.chat.error)
        self.assert_draft()
        QTest.qWait(150)
        self.assertEqual(1, request_count())

    def assert_retry_success(self, message_id):
        self.wait(lambda: not self.chat.busy)
        self.assertEqual([message_id], [row["id"] for row in self.users()])
        assistants = [row for row in self.rows() if row["role"] == "assistant"]
        self.assertEqual(1, len(assistants))
        self.assertEqual(message_id, assistants[0]["metadata"]["reply_to"])
        self.assertEqual("complete", self.users()[0]["metadata"]["request_status"])
        self.assertFalse(self.chat.error)
        self.assert_draft()

    def stage(self, color):
        path = self.root/(color.lstrip("#")+".png")
        image = QImage(32, 24, QImage.Format.Format_RGB32); image.fill(QColor(color))
        self.assertTrue(image.save(str(path), "PNG"))
        self.assertTrue(self.panel._stage_local_image(str(path), self.cid))
        self.wait(lambda: not self.panel._image_preparing)
        self.assertFalse(self.panel._image_stage_error)
        return self.panel._current_staged_image()["image_id"]

    @staticmethod
    def image_urls(request):
        return [part["image_url"]["url"] for message in request["messages"]
            if isinstance(message.get("content"), list) for part in message["content"] if part.get("type") == "image_url"]

    def test_429_500_503_errors_keep_one_user_and_new_draft_until_explicit_retry(self):
        for status in (429, 500, 503):
            with self.subTest(status=status), MockApiServer() as server:
                self.configure(server)
                # Use an independent fresh conversation for each real HTTP fault.
                if self.rows():
                    self.chat.new_conversation(); self.cid = self.chat.current_conversation_id
                    self.wait(lambda: not self.chat.history_loading)
                server.enqueue("NEVER_PUBLIC_PROVIDER_BODY "+KEY, status=status, delay=.15)
                message_id = self.sent()
                self.assert_failed_once(message_id, lambda: len(server.requests))
                server.enqueue("明确重试后成功")
                self.assertTrue(self.chat.retry(self.cid, message_id))
                self.assertFalse(self.chat.retry(self.cid, message_id))
                self.assert_retry_success(message_id)
                self.assertEqual(2, len(server.requests))

    def test_connection_reset_has_no_auto_replay_and_explicit_retry_reuses_the_same_user(self):
        def plan(handler):
            if len(server.requests) == 1:
                handler.connection.shutdown(socket.SHUT_RDWR)
            else:
                handler.send('{"reply":"断线后明确重试成功","emotion":"happy"}'); handler.done()
        with SseServer(plan) as server:
            self.configure(server)
            message_id = self.sent()
            self.assert_failed_once(message_id, lambda: len(server.requests))
            self.assertTrue(self.chat.retry(self.cid, message_id))
            self.assert_retry_success(message_id)
            self.assertEqual(2, len(server.requests))

    def test_partial_sse_never_becomes_success_or_replays_and_retry_keeps_the_draft(self):
        for graceful in (False, True):
            with self.subTest(graceful=graceful):
                if self.rows():
                    self.chat.new_conversation(); self.cid = self.chat.current_conversation_id
                    self.wait(lambda: not self.chat.history_loading)
                def plan(handler):
                    if len(server.requests) == 1:
                        handler.send('{"reply":"仅收到的半截回复')
                        if graceful:
                            handler.write_chunk(b"data: [DONE]\n\n")
                            handler.wfile.write(b"0\r\n\r\n"); handler.wfile.flush()
                        else:
                            handler.connection.shutdown(socket.SHUT_RDWR)
                    else:
                        handler.send('{"reply":"重试得到完整回复","emotion":"happy"}'); handler.done()
                with SseServer(plan) as server:
                    self.configure(server)
                    message_id = self.sent()
                    self.assert_failed_once(message_id, lambda: len(server.requests))
                    preview = self.chat.streaming_reply
                    self.assertIsNotNone(preview)
                    self.assertEqual("interrupted", preview["state"])
                    self.assertEqual("仅收到的半截回复", preview["text"])
                    self.assertTrue(self.chat.retry(self.cid, message_id))
                    self.assert_retry_success(message_id)
                    self.assertEqual(2, len(server.requests))
                    self.assertIsNone(self.chat.streaming_reply)

    def test_cancelled_image_retry_keeps_original_pixels_and_the_new_unsent_attachment(self):
        def plan(handler):
            if len(server.requests) == 1:
                handler.send('{"reply":"取消前已经收到')
                server.release.wait(6)
                handler.send('迟到的结果不能出现","emotion":"happy"}'); handler.done()
            else:
                handler.send('{"reply":"当前明确请求已完成","emotion":"happy"}'); handler.done()
        with SseServer(plan) as server:
            self.configure(server)
            original_image = self.stage("#1768e8")
            original_url = self.chat._load_chat_image(original_image)
            message_id = self.sent("检查这个原图")
            self.wait(lambda: self.chat.streaming_reply is not None)
            self.assertTrue(self.chat.stop_request(self.cid))
            self.wait(lambda: self.users()[0]["metadata"]["request_status"] == "cancelled")
            self.assertEqual(1, len(server.requests))
            next_image = self.stage("#168c3c")
            self.assertNotEqual(original_image, next_image)
            self.assertTrue(self.chat.retry(self.cid, message_id))
            self.assert_retry_success(message_id)
            self.assertEqual(next_image, self.panel._current_staged_image()["image_id"])
            self.assertEqual(original_image, self.users()[0]["metadata"]["image_id"])
            self.assertEqual([[original_url], [original_url]], [self.image_urls(r) for r in server.requests])
            server.release.set()
            self.wait(lambda: not self.chat._requests)
            self.assertNotIn("迟到的结果不能出现", json.dumps(self.rows(), ensure_ascii=False))
            self.assert_draft()
            next_url = self.chat._load_chat_image(next_image)
            self.panel._send()
            self.wait(lambda: not self.chat.busy)
            self.assertEqual(3, len(server.requests))
            self.assertEqual([next_url], self.image_urls(server.requests[-1]))
            self.assertEqual([original_image, next_image], [r["metadata"]["image_id"] for r in self.users()])
            self.assertEqual(2, len(self.users()))
            self.assertIsNone(self.panel._current_staged_image())

    def test_deleted_failed_attachment_cannot_retry_as_text_or_consume_a_new_staged_image(self):
        with MockApiServer() as server:
            self.configure(server)
            original_image = self.stage("#1768e8")
            server.enqueue("NEVER_PUBLIC_PROVIDER_BODY", status=503, delay=.15)
            message_id = self.sent("原图识别")
            self.assert_failed_once(message_id, lambda: len(server.requests))
            next_image = self.stage("#168c3c")
            self.chat._image_inbox.delete(original_image)
            self.assertTrue(self.chat.retry(self.cid, message_id))
            self.wait(lambda: not self.chat.busy)
            self.assertEqual(1, len(server.requests))
            self.assertTrue(self.chat.error)
            self.assertEqual(next_image, self.panel._current_staged_image()["image_id"])
            self.assertEqual([message_id], [r["id"] for r in self.users()])
            self.assertEqual(original_image, self.users()[0]["metadata"]["image_id"])
            self.assert_draft()


if __name__ == "__main__":
    unittest.main()
