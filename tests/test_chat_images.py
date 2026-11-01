"""Real local HTTP proves image references reach the configured chat model."""

import base64
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSettings, QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.api_client import ApiError
from desktop_pet.remote.controller import RemoteController
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
from tests import test_remote_controller as fixtures
from tests.mock_api import MockApiServer
from tests.test_settings import FakeVault


class ChatImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-chat-image-")
        self.path = Path(self.directory.name)
        self.settings = SettingsManager(QSettings(str(self.path / "settings.ini"), QSettings.Format.IniFormat),
                                        CredentialStore(backend=FakeVault()))
        self.chat = ChatController(self.settings)
        self.session = SimpleNamespace(settings=self.settings, chat=self.chat)
        self.remote = RemoteController(self.session, transport_factory=fixtures.FakeTransport)
        fixtures.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.remote.set_enabled(True)
        fixtures.spin_until(lambda: self.remote.status["state"] == "listening")
        pairing = self.remote.begin_pairing()
        self.assertTrue(self.rpc("pair", {"code": pairing["code"], "name": "图片测试手机"})["ok"])
        self.cid = self.chat.current_conversation_id

    def tearDown(self):
        self.remote.shutdown()
        self.chat.shutdown()
        fixtures.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.remote.deleteLater()
        self.chat.deleteLater()
        QApplication.processEvents()
        self.directory.cleanup()

    def rpc(self, method, params=None, request_id=None):
        return self.remote.handle_request(fixtures.PEER,
            {"id": request_id or str(uuid4()), "method": method, "params": params or {}})

    def configure(self, server):
        self.settings.save_api_settings(ApiSettings(server.base_url, "existing-pc-model", 10))
        self.settings.save_api_key("isolated-image-chat-key")

    def upload(self, color="#75b8f4"):
        image = QImage(24, 16, QImage.Format.Format_ARGB32)
        image.fill(QColor(color))
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, "PNG"))
        params = {"filename": "phone.png", "mime_type": "image/png",
                  "data_base64": base64.b64encode(bytes(data)).decode("ascii")}
        response = self.rpc("image.upload", params)
        self.assertTrue(response["ok"], response)
        return response["result"]["id"], params["data_base64"]

    def send(self, server, text, *, image_id=None, cid=None, request_id=None, status=200):
        server.enqueue('{"reply":"看到了图片中的内容。","emotion":"happy"}', status=status)
        params = {"text": text, "conversation_id": cid or self.cid}
        if image_id is not None:
            params["image_id"] = image_id
        response = self.rpc("chat.send", params, request_id)
        self.assertTrue(response["ok"], response)
        fixtures.spin_until(lambda: not self.chat.busy)
        return response, params

    @staticmethod
    def image_bytes(request):
        content = next(message["content"] for message in reversed(request["body"]["messages"])
                       if isinstance(message["content"], list))
        url = next(part["image_url"]["url"] for part in content if part["type"] == "image_url")
        assert url.startswith("data:image/png;base64,")
        return base64.b64decode(url.split(",", 1)[1], validate=True)

    def synced_messages(self):
        return {item["id"]: item["data"] for item in self.remote.sync.snapshot(limit=100)["items"]
                if item["entity"] == "message"}

    def test_uploaded_image_reaches_real_http_and_sync_contains_only_reference(self):
        self.assertIn("chat.image", self.rpc("status")["result"]["capabilities"])
        image_id, encoded = self.upload()
        request_id = str(uuid4())
        with MockApiServer() as server:
            self.configure(server)
            first, params = self.send(server, "这张图是什么颜色？", image_id=image_id, request_id=request_id)
            self.assertFalse(self.chat.error)
            self.assertEqual(len(server.requests), 1)
            payload = server.requests[0]["body"]
            self.assertEqual(payload["model"], "existing-pc-model")
            self.assertEqual(payload["messages"][-1]["content"][0], {"type": "text", "text": params["text"]})
            decoded = self.image_bytes(server.requests[0])
            self.assertEqual(decoded, base64.b64decode(encoded))
            self.assertEqual(QImage.fromData(decoded).pixelColor(0, 0), QColor("#75b8f4"))
            message = self.chat.store.load_messages(self.cid)[0]
            self.assertEqual(message["metadata"]["image_id"], image_id)
            synced = self.synced_messages()[message["id"]]
            self.assertEqual(synced["image_id"], image_id)
            self.assertIsNone(synced["image_error"])
            serialized = json.dumps({"messages": self.chat.store.load_messages(self.cid),
                                     "snapshot": self.remote.sync.snapshot(), "pull": self.remote.sync.pull()})
            for private in (encoded, "data:image", str(self.remote.images.directory), "isolated-image-chat-key"):
                self.assertNotIn(private, serialized)
            self.rpc("image.delete", {"id": image_id})
            duplicate = self.rpc("chat.send", params, request_id)
            self.assertTrue(duplicate["result"]["duplicate"])
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(first["result"]["message_id"], message["id"])

    def test_followup_keeps_same_session_image_and_another_session_stays_text_only(self):
        image_id, encoded = self.upload()
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "先看看图片", image_id=image_id)
            second, _ = self.send(server, "上面这张图里有什么？")
            self.assertEqual(self.image_bytes(server.requests[1]), base64.b64decode(encoded))
            followup = self.synced_messages()[second["result"]["message_id"]]
            self.assertIsNone(followup["image_id"])
            archived = self.chat.store.load_messages(self.cid)[-2]
            self.assertNotIn("image_id", archived["metadata"])
            self.assertEqual(archived["metadata"]["context_image_id"], image_id)
            messages = server.requests[1]["body"]["messages"]
            self.assertEqual(messages[-1]["content"], "上面这张图里有什么？")
            self.assertEqual(messages[-3]["content"][0]["text"], "先看看图片")
            other_cid = str(uuid4())
            self.send(server, "独立会话的纯文字问题", cid=other_cid)
            self.assertIsInstance(server.requests[2]["body"]["messages"][-1]["content"], str)
            self.assertNotIn("image_id", self.chat.store.load_messages(other_cid)[0]["metadata"])
            self.assertNotIn(encoded, json.dumps(server.requests[2]["body"]))

    def test_retry_reuses_original_image_message_id_and_request_id(self):
        original_id, original_data = self.upload()
        other_id, other_data = self.upload("#f47b75")
        request_id = str(uuid4())
        with MockApiServer() as server:
            self.configure(server)
            first, params = self.send(server, "旧图片请求", image_id=original_id, request_id=request_id, status=500)
            original_message_id = first["result"]["message_id"]
            self.assertTrue(self.rpc("chat.send", params, request_id)["result"]["duplicate"])
            changed = self.rpc("chat.send", {**params, "image_id": other_id}, request_id)
            self.assertFalse(changed["ok"])
            self.send(server, "新的图片请求", image_id=other_id)
            server.enqueue("重试原图成功")
            self.assertTrue(self.chat.retry(self.cid, original_message_id))
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual([self.image_bytes(request) for request in server.requests],
                             [base64.b64decode(original_data), base64.b64decode(other_data), base64.b64decode(original_data)])
            users = [message for message in self.chat.store.load_messages(self.cid) if message["role"] == "user"]
            self.assertEqual(len(users), 2)
            self.assertEqual(users[0]["id"], original_message_id)
            self.assertEqual(users[0]["metadata"]["image_id"], original_id)

    def test_unrelated_text_does_not_resend_old_pixels_or_create_an_attachment(self):
        image_id, encoded = self.upload()
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "看这张图", image_id=image_id)
            for text in ("?", "今天心情怎么样", "介绍一下你自己"):
                self.send(server, text)
                self.assertTrue(all(isinstance(message["content"], str)
                                    for message in server.requests[-1]["body"]["messages"]))
                latest = self.chat.store.load_messages(self.cid)[-2]
                self.assertNotIn("image_id", latest["metadata"])
                self.assertIsNone(latest["metadata"]["context_image_id"])
                self.assertNotIn(encoded, json.dumps(server.requests[-1]["body"]))

    def test_reference_retry_keeps_original_historical_image_and_current_text(self):
        first_id, first_data = self.upload()
        next_id, _ = self.upload("#f47b75")
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "原始图片", image_id=first_id)
            failed, _ = self.send(server, "这张图的细节？", status=500)
            failed_id = failed["result"]["message_id"]
            self.send(server, "新的附件", image_id=next_id)
            server.enqueue("原图重试完成")
            self.assertTrue(self.chat.retry(self.cid, failed_id))
            fixtures.spin_until(lambda: not self.chat.busy)
            request = server.requests[-1]
            self.assertEqual(request["body"]["messages"][-1]["content"], "这张图的细节？")
            self.assertEqual(self.image_bytes(request), base64.b64decode(first_data))
            original = next(message for message in self.chat.store.load_messages(self.cid) if message["id"] == failed_id)
            self.assertNotIn("image_id", original["metadata"])
            self.assertEqual(original["metadata"]["context_image_id"], first_id)

    def test_missing_invalid_and_deleted_image_references_are_rejected_before_chat(self):
        for image_id in (None, "../private.png", "F" * 64, 42, {}):
            with self.subTest(image_id=image_id):
                response = self.rpc("chat.send", {"text": "识图", "image_id": image_id})
                self.assertEqual(response["error"]["code"], "invalid_image")
        self.assertEqual(self.rpc("chat.send", {"text": "识图", "image_id": "e" * 64})["error"]["code"],
                         "image_missing")
        image_id, _ = self.upload()
        self.remote.images.delete(image_id)
        self.assertEqual(self.rpc("chat.send", {"text": "识图", "image_id": image_id})["error"]["code"],
                         "image_missing")
        self.assertEqual(self.chat.store.load_messages(self.cid), [])

    def test_deleted_followup_and_retry_fail_clearly_without_text_only_http(self):
        image_id, _ = self.upload()
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "看图", image_id=image_id)
            self.remote.images.delete(image_id)
            response = self.rpc("chat.send", {"text": "上面这张图的细节", "conversation_id": self.cid})
            self.assertTrue(response["ok"])
            fixtures.spin_until(lambda: not self.chat.busy)
            failed_id = response["result"]["message_id"]
            self.assertEqual(self.synced_messages()[failed_id]["image_error"], "image_missing")
            self.assertIn("图片不存在或已删除", self.chat.error)
            self.assertTrue(self.chat.retry(self.cid, failed_id))
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(self.synced_messages()[failed_id]["image_error"], "image_missing")

    def test_corrupt_file_fails_before_http_and_no_local_path_leaks(self):
        image_id, _ = self.upload()
        (self.remote.images.directory / (image_id + ".png")).write_bytes(b"damaged bytes")
        with MockApiServer() as server:
            self.configure(server)
            response = self.rpc("chat.send", {"text": "看图", "image_id": image_id})
            self.assertTrue(response["ok"])
            fixtures.spin_until(lambda: not self.chat.busy)
            message = self.synced_messages()[response["result"]["message_id"]]
            self.assertEqual(message["image_error"], "image_unavailable")
            self.assertEqual(server.requests, [])
            self.assertNotIn(str(self.path), self.chat.error)

    def test_provider_rejection_is_static_sync_error_and_never_falls_back_to_text(self):
        image_id, _ = self.upload()
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue("private provider response with data:image and isolated-image-chat-key", status=400)
            response = self.rpc("chat.send", {"text": "识图", "image_id": image_id})
            self.assertTrue(response["ok"])
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual(len(server.requests), 1)
            message = self.synced_messages()[response["result"]["message_id"]]
            self.assertEqual(message["image_error"], "vision_rejected")
            self.assertIn("图文请求未被模型接口接受", self.chat.error)
            self.assertNotIn("private provider", self.chat.error)
            self.assertNotIn("isolated-image-chat-key", self.chat.error)

    def test_archived_image_is_reused_after_controller_restart(self):
        image_id, encoded = self.upload()
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "记住本会话图片", image_id=image_id)
            self.chat.shutdown()
            fixtures.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            self.chat.deleteLater()
            self.chat = ChatController(self.settings)
            self.chat.set_image_inbox(self.remote.images)
            self.session.chat = self.chat
            fixtures.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            self.send(server, "上面这张图？", cid=self.cid)
            self.assertEqual(self.image_bytes(server.requests[1]), base64.b64decode(encoded))

    def test_preview_getter_verifies_current_message_ownership_before_reading_inbox(self):
        image_id, encoded = self.upload()
        user = self.chat.store.append_message(self.cid, "user", "附图", "user", metadata={"image_id": image_id})
        assistant = self.chat.store.append_message(self.cid, "assistant", "回复", "assistant", metadata={"image_id": image_id})
        other_cid = str(uuid4())
        other = self.chat.store.append_message(other_cid, "user", "其他会话", "user", metadata={"image_id": image_id})
        with patch.object(self.remote.images, "get", wraps=self.remote.images.get) as read:
            for reference in ((other_cid, other["id"], image_id), (self.cid, other["id"], image_id),
                              (self.cid, assistant["id"], image_id), (self.cid, user["id"], "b" * 64),
                              (self.cid, user["id"], "../private.png")):
                with self.subTest(reference=reference), self.assertRaises(ApiError):
                    self.chat.get_message_image(*reference)
            read.assert_not_called()
            self.assertEqual(self.chat.get_message_image(self.cid, user["id"], image_id)["data_base64"], encoded)
            read.assert_called_once_with(image_id)
        self.remote.images.delete(image_id)
        with self.assertRaises(ApiError) as caught:
            self.chat.get_message_image(self.cid, user["id"], image_id)
        self.assertEqual(caught.exception.code, "image_missing")


if __name__ == "__main__":
    unittest.main()
