"""Hidden production panel/controller/inbox; owned images and localhost HTTP only."""
import base64
import json
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QByteArray, QBuffer, QEvent, QIODevice, QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.assistant_panel import AssistantChatPanel
from desktop_pet.ui.chat_panel import MAX_STAGE_IMAGE_BASE64, IMAGE_STAGE_ERROR
from tests import test_chat_images as images
from tests import test_remote_controller as fixtures
from tests.mock_api import MockApiServer


class ChatImageDataStageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    rpc = images.ChatImageTests.rpc
    configure = images.ChatImageTests.configure

    def setUp(self):
        images.ChatImageTests.setUp(self)
        self.panel = AssistantChatPanel(self.chat)
        self.release = threading.Event()
        self.main_thread = threading.get_ident()

    def tearDown(self):
        self.release.set()
        fixtures.spin_until(lambda: not self.panel._image_stage_runner._tasks
                           and QThreadPool.globalInstance().activeThreadCount() == 0)
        for timer in (self.panel._web_publish_timer, self.panel._web_stream_timer,
                      self.panel._web_start_timer, self.panel._web_timeout):
            timer.stop()
        self.assertFalse(self.panel.isVisible())
        self.assertIsNone(self.panel.web_view)
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        images.ChatImageTests.tearDown(self)

    def payload(self, *, format="PNG", **changes):
        image = QImage(24, 16, QImage.Format.Format_ARGB32)
        image.fill(QColor("#75b8f4"))
        output = QByteArray()
        buffer = QBuffer(output)
        self.assertTrue(buffer.open(QIODevice.OpenModeFlag.WriteOnly))
        self.assertTrue(image.save(buffer, format))
        mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[format]
        cid = self.chat.current_conversation_id
        value = {"conversationId": cid, "filename": "pasted." + format.lower(), "mimeType": mime,
                 "dataBase64": base64.b64encode(bytes(output)).decode("ascii"),
                 "draftEpoch": self.panel._draft_owner.snapshot(cid).epoch,
                 "draftDocument": self.panel._web_document_token}
        return {**value, **changes}

    def stage(self, payload):
        return json.loads(self.panel.bridge.dispatch("stageImageData", json.dumps(payload)))["ok"]

    def finish(self):
        self.release.set()
        fixtures.spin_until(lambda: not self.panel._image_preparing and not self.panel._image_stage_runner._tasks)

    def set_draft(self, text):
        self.assertTrue(self.panel.dispatch_web_action("draft", {
            "conversationId": self.chat.current_conversation_id, "text": text}))

    def test_accept_publishes_busy_before_ack_and_worker_imports_without_automatic_send(self):
        payload = self.payload()
        self.set_draft("保留尚未发送的文字")
        entered = threading.Event()
        worker_threads = []
        importer = self.chat.import_image_data
        snapshots = []
        self.panel.bridge.stateChanged.connect(lambda raw: snapshots.append(json.loads(raw)))

        def held(*args):
            worker_threads.append(threading.get_ident())
            entered.set()
            self.release.wait(4)
            return importer(*args)

        with MockApiServer() as server, patch.object(self.chat, "import_image_data", held):
            self.configure(server)
            self.assertTrue(self.panel.web_state()["canStageImageData"])
            self.assertTrue(self.stage(payload))
            self.assertTrue(snapshots, "Busy state must be emitted before accepted ACK returns")
            self.assertTrue(snapshots[-1]["imagePreparing"])
            self.assertTrue(snapshots[-1]["imageImportBusy"])
            fixtures.spin_until(entered.is_set)
            self.assertEqual(server.requests, [])
            self.finish()
            self.assertNotEqual(worker_threads, [self.main_thread])
            staged = self.panel._current_staged_image()
            self.assertEqual((staged["width"], staged["height"]), (24, 16))
            self.assertTrue(staged["thumbnail"]["src"].startswith("data:image/png;base64,"))
            self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, "保留尚未发送的文字")
            self.assertEqual(server.requests, [])
            self.assertNotIn("dataBase64", json.dumps(self.panel.web_state()))
            server.enqueue("已读取粘贴附件")
            self.assertTrue(self.panel.dispatch_web_action("send", {
                "conversationId": self.cid, "text": "保留尚未发送的文字"}))
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(images.ChatImageTests.image_bytes(server.requests[0]),
                             base64.b64decode(payload["dataBase64"]))
            self.assertIsNone(self.panel._current_staged_image())

    def test_actual_png_jpeg_webp_use_inbox_validation_and_basename_labels(self):
        for format in ("PNG", "JPEG", "WEBP"):
            with self.subTest(format=format):
                self.assertTrue(self.stage(self.payload(format=format, filename="C:/PRIVATE_LABEL/可爱图片." + format.lower())))
                self.finish()
                staged = self.panel._current_staged_image()
                self.assertEqual((staged["width"], staged["height"]), (24, 16))
                self.assertNotIn("PRIVATE_LABEL", json.dumps(staged))
                self.assertNotIn("/", staged["filename"])

    def test_raw_payload_cap_is_enforced_before_json_parsing(self):
        with patch("desktop_pet.ui.assistant_panel.json.loads", side_effect=AssertionError("PARSED_OVERSIZED_PAYLOAD")) as decode, \
                patch.object(self.chat, "import_image_data") as importer:
            response = self.panel.bridge.dispatch("stageImageData", " " * (MAX_STAGE_IMAGE_BASE64 + 2049))
            decode.assert_not_called()
            importer.assert_not_called()
        self.assertFalse(json.loads(response)["ok"])

    def test_deeply_nested_under_cap_payload_is_safely_rejected(self):
        payload = '{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}'
        with patch.object(self.chat, "import_image_data") as importer:
            self.assertFalse(json.loads(self.panel.bridge.dispatch("stageImageData", payload))["ok"])
            importer.assert_not_called()
        self.assertFalse(self.panel._image_preparing)

    def test_bad_schema_authority_and_admission_never_import_or_modify_text(self):
        self.set_draft("新草稿必须保留")
        valid = self.payload()
        cases = [{**valid, "path": "C:/PRIVATE_CANARY.png"}, {**valid, "url": "https://invalid.example/x.png"},
                 {**valid, "conversationId": "other"}, {**valid, "draftEpoch": "old-epoch"},
                 {**valid, "draftDocument": "old-document"}, {k: v for k, v in valid.items() if k != "mimeType"},
                 {**valid, "filename": "bad\nname.png"}, {**valid, "filename": "x" * 161},
                 {**valid, "mimeType": "image/svg+xml"}, {**valid, "dataBase64": ""},
                 {**valid, "dataBase64": "A" * (MAX_STAGE_IMAGE_BASE64 + 1)},
                 {**valid, "dataBase64": "data:image/png;base64," + valid["dataBase64"]},
                 {**valid, "dataBase64": "file:///PRIVATE_CANARY.png"}]
        with patch.object(self.chat, "import_image_data") as importer:
            for value in cases:
                with self.subTest(fields=set(value), encoded_size=len(value.get("dataBase64", ""))):
                    self.assertFalse(self.stage(value))
            importer.assert_not_called()
        self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, "新草稿必须保留")
        self.assertFalse(self.panel._image_preparing)
        self.assertIsNone(self.panel._current_staged_image())

    def test_busy_loading_and_legacy_capability_reject_without_replacing_valid_stage(self):
        self.assertTrue(self.stage(self.payload()))
        self.finish()
        previous = self.panel._current_staged_image()
        self.set_draft("忙碌或不支持时保留文字")
        valid = self.payload()
        with patch.object(self.chat, "import_image_data") as importer:
            with patch.object(self.chat, "_busy", True):
                self.assertFalse(self.stage(valid))
            with patch.object(self.chat, "_history_loading", True):
                self.assertFalse(self.stage(valid))
            importer.assert_not_called()
        with patch.object(self.chat, "import_image_data", None):
            self.assertFalse(self.panel.web_state()["canStageImageData"])
            self.assertFalse(self.stage(valid))
        self.assertEqual(self.panel._current_staged_image(), previous)
        self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, "忙碌或不支持时保留文字")

    def test_decoding_real_mime_and_decoded_size_fail_in_worker_with_only_static_error(self):
        valid = self.payload()
        for changes in ({"dataBase64": "PRIVATE_SYNTHETIC_INVALID_BASE64"},
                        {"mimeType": "image/jpeg"},
                        {"dataBase64": base64.b64encode(b"<svg>PRIVATE_SYNTHETIC</svg>").decode()},
                        {"dataBase64": base64.b64encode(b"x" * (4 * 1024 * 1024 + 1)).decode()}):
            with self.subTest(kind=tuple(changes)):
                self.set_draft("失败后仍可编辑")
                self.assertTrue(self.stage({**valid, **changes}))
                self.finish()
                self.assertEqual(self.panel._image_stage_error, IMAGE_STAGE_ERROR)
                self.assertIsNone(self.panel._current_staged_image())
                self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, "失败后仍可编辑")
                self.assertNotIn("PRIVATE_SYNTHETIC", json.dumps(self.panel.web_state()))

    def test_stale_clear_epoch_and_reload_document_cannot_revive_attachment(self):
        old = self.payload()
        self.chat.clear()
        fixtures.spin_until(lambda: not self.chat.history_loading)
        self.set_draft("清空后新文字")
        with patch.object(self.chat, "import_image_data") as importer:
            self.assertFalse(self.stage(old))
            importer.assert_not_called()
        self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, "清空后新文字")
        old = self.payload()
        with patch.object(self.panel, "_ensure_web"):
            self.panel._reload_web()
        with patch.object(self.chat, "import_image_data") as importer:
            self.assertFalse(self.stage(old))
            importer.assert_not_called()
        self.assertIsNone(self.panel._current_staged_image())
        self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, "清空后新文字")

    def test_single_worker_keeps_other_conversation_send_available_and_late_origin_stage(self):
        self.chat.store.append_message(self.cid, "assistant", "保存自有会话", "assistant")
        entered = threading.Event()
        importer = self.chat.import_image_data
        calls = []

        def held(*args):
            calls.append(args[0]); entered.set(); self.release.wait(4)
            return importer(*args)

        with patch.object(self.chat, "import_image_data", held), MockApiServer() as server:
            self.configure(server)
            self.assertTrue(self.stage(self.payload()))
            fixtures.spin_until(entered.is_set)
            self.chat.new_conversation()
            fixtures.spin_until(lambda: not self.chat.history_loading)
            other = self.chat.current_conversation_id
            self.assertNotEqual(other, self.cid)
            self.assertFalse(self.panel.web_state()["imagePreparing"])
            self.assertTrue(self.panel.web_state()["imageImportBusy"])
            self.assertFalse(self.stage(self.payload()))
            server.enqueue("B 会话正常回复")
            self.assertTrue(self.panel.dispatch_web_action("send", {"conversationId": other, "text": "B 的新文字"}))
            fixtures.spin_until(lambda: not self.chat.busy)
            self.finish()
            self.chat.open_conversation(self.cid)
            fixtures.spin_until(lambda: not self.chat.history_loading)
            self.assertIsNotNone(self.panel._current_staged_image())
            self.assertEqual(len(calls), 1)

    def test_accepted_import_clear_discards_late_bytes_and_preserves_new_draft(self):
        entered = threading.Event()
        importer = self.chat.import_image_data

        def held(*args):
            entered.set(); self.release.wait(4)
            return importer(*args)

        with patch.object(self.chat, "import_image_data", held):
            self.assertTrue(self.stage(self.payload()))
            fixtures.spin_until(entered.is_set)
            self.chat.clear()
            fixtures.spin_until(lambda: not self.chat.history_loading)
            self.set_draft("新的一轮文字")
            self.assertTrue(self.panel._image_preparing)
            self.finish()
        self.assertIsNone(self.panel._current_staged_image())
        self.assertEqual(self.panel._image_stage_error, "")
        self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, "新的一轮文字")

    def test_accepted_native_import_survives_web_reload_without_reaccepting_old_reader(self):
        entered = threading.Event()
        importer = self.chat.import_image_data
        old = self.payload()
        calls = []

        def held(*args):
            calls.append(args[0]); entered.set(); self.release.wait(4)
            return importer(*args)

        with patch.object(self.chat, "import_image_data", held):
            self.assertTrue(self.stage(old))
            fixtures.spin_until(entered.is_set)
            with patch.object(self.panel, "_ensure_web"):
                self.panel._reload_web()
            self.set_draft("重载后继续编辑")
            self.assertFalse(self.stage(old))
            self.finish()
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(self.panel._current_staged_image())
        self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, "重载后继续编辑")

    def test_accepted_import_deleted_origin_cannot_attach_to_new_conversation(self):
        self.chat.store.append_message(self.cid, "assistant", "保存待删除的自有会话", "assistant")
        entered = threading.Event()
        importer = self.chat.import_image_data

        def held(*args):
            entered.set(); self.release.wait(4)
            return importer(*args)

        with patch.object(self.chat, "import_image_data", held):
            self.assertTrue(self.stage(self.payload()))
            fixtures.spin_until(entered.is_set)
            self.assertTrue(self.chat.delete_conversation(self.cid))
            fixtures.spin_until(lambda: not self.chat.history_loading and self.chat.current_conversation_id != self.cid)
            self.set_draft("删除后新会话文字")
            new_cid = self.chat.current_conversation_id
            self.finish()
        self.assertNotIn(self.cid, self.panel._staged_images)
        self.assertIsNone(self.panel._current_staged_image())
        self.assertEqual(self.panel._draft_owner.snapshot(new_cid).text, "删除后新会话文字")


if __name__ == "__main__":
    unittest.main()
