"""Owned hidden image imports preserve static storage causes and draft fences."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from desktop_pet.api_client import ApiError
from desktop_pet.remote.attachments import ImageInbox
from desktop_pet.remote.errors import RemoteError
from tests import test_chat_image_data_stage as staging
from tests import test_remote_controller as fixtures


FULL = "图片存储空间已满（64 MiB），本次图片未添加，文字已保留。"
IO = "无法读取或保存图片，请检查本地可用空间和读写权限后重试；文字已保留。"


class ChatImageStorageErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        staging.ChatImageDataStageTests.setUpClass.__func__(cls)

    setUp = staging.ChatImageDataStageTests.setUp
    tearDown = staging.ChatImageDataStageTests.tearDown
    rpc = staging.ChatImageDataStageTests.rpc
    configure = staging.ChatImageDataStageTests.configure
    payload = staging.ChatImageDataStageTests.payload
    stage = staging.ChatImageDataStageTests.stage
    finish = staging.ChatImageDataStageTests.finish
    set_draft = staging.ChatImageDataStageTests.set_draft
    observations = []

    def check_storage_failure(self, origin, expected_code, expected_text):
        payload = self.payload()
        selected = self.path / "valid-storage-fixture.png"
        selected.write_bytes(base64.b64decode(payload["dataBase64"], validate=True))
        self.set_draft("保留本次尚未发送的文字")
        function = (lambda: self.chat.import_local_image(str(selected))) if origin == "picker" else (
            lambda: self.chat.import_image_data(payload["filename"], payload["mimeType"], payload["dataBase64"]))
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(ApiError) as raised:
                pool.submit(function).result(timeout=10)
        accepted = self.panel._stage_local_image(str(selected), self.cid) if origin == "picker" else self.stage(payload)
        self.assertTrue(accepted)
        self.finish()
        public = self.panel.web_state()["imageError"]
        self.observations.append({"origin": origin, "controller_code": raised.exception.code,
                                  "public_error": public, "expected_code": expected_code,
                                  "expected_error": expected_text})
        self.assertIsNone(self.panel._current_staged_image())
        self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, "保留本次尚未发送的文字")
        self.assertEqual(public, expected_text)
        self.assertEqual(raised.exception.code, expected_code)

    def test_quota_reports_storage_full_in_picker_and_gesture(self):
        with patch("desktop_pet.remote.attachments.MAX_TOTAL_BYTES", 0):
            for origin in ("picker", "gesture"):
                with self.subTest(origin=origin):
                    self.check_storage_failure(origin, "image_storage_full", FULL)

    def test_real_directory_io_reports_storage_error_in_picker_and_gesture(self):
        blocked = self.path / "owned-blocked-inbox"
        blocked.write_bytes(b"Owned fixture makes directory creation fail")
        self.chat.set_image_inbox(ImageInbox(blocked))
        for origin in ("picker", "gesture"):
            with self.subTest(origin=origin):
                self.check_storage_failure(origin, "image_storage_error", IO)

    def test_trusted_storage_error_and_sqlite_failure_have_only_fixed_io_text(self):
        for failure in (RemoteError("storage_error", "PRIVATE_OWNED_REMOTE_MESSAGE"),
                        sqlite3.OperationalError("PRIVATE_OWNED_SQLITE_PATH")):
            with self.subTest(kind=type(failure).__name__), patch.object(self.chat._image_inbox, "upload", side_effect=failure):
                self.check_storage_failure("gesture", "image_storage_error", IO)
                serialized = json.dumps(self.panel.web_state())
                self.assertNotIn("PRIVATE_OWNED", serialized)

    def test_untrusted_exception_code_cannot_forge_storage_full(self):
        class UntrustedFailure(ValueError):
            code = "storage_full"
        with patch.object(self.chat._image_inbox, "upload", side_effect=UntrustedFailure("PRIVATE_OWNED_UNTRUSTED")):
            self.check_storage_failure("gesture", "image_unavailable", staging.IMAGE_STAGE_ERROR)

    def test_late_storage_failure_after_clear_is_discarded_and_new_draft_kept(self):
        entered = threading.Event()

        def held_failure(*_args):
            entered.set()
            self.release.wait(4)
            raise ApiError("image_storage_full", "PRIVATE_OWNED_EXCEPTION_MESSAGE")

        with patch.object(self.chat, "import_image_data", held_failure):
            self.assertTrue(self.stage(self.payload()))
            fixtures.spin_until(entered.is_set)
            self.chat.clear()
            fixtures.spin_until(lambda: not self.chat.history_loading)
            self.set_draft("清空后的新文字")
            self.finish()
        self.assertEqual(self.panel.web_state()["imageError"], "")
        self.assertIsNone(self.panel._current_staged_image())
        self.assertEqual(self.panel._draft_owner.snapshot(self.cid).text, "清空后的新文字")

    def test_unknown_api_error_message_is_not_forwarded_as_storage_guidance(self):
        with patch.object(self.chat, "import_image_data", side_effect=ApiError("PRIVATE_OWNED_CODE", "PRIVATE_OWNED_MESSAGE")):
            self.assertTrue(self.stage(self.payload()))
            self.finish()
        self.assertEqual(self.panel.web_state()["imageError"], staging.IMAGE_STAGE_ERROR)
        self.assertNotIn("PRIVATE_OWNED", json.dumps(self.panel.web_state()))


if __name__ == "__main__":
    unittest.main()
