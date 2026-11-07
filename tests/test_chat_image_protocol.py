"""Image wire validation, safe sync projection and additive schema upgrades."""

import base64
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from desktop_pet.api_client import ApiClient, ApiError
from desktop_pet.conversations.store import ConversationStore, REMOTE_SYNC_SCHEMA_VERSION
from desktop_pet.remote.sync import RemoteSyncStore, SyncResetRequired, sanitize_message
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer


class ImageApiContractTests(unittest.TestCase):
    @staticmethod
    def messages(url="data:image/png;base64,QUJD", role="user", text="图片问题"):
        return [{"role": role, "content": [{"type": "text", "text": text},
                 {"type": "image_url", "image_url": {"url": url}}]}]

    def test_image_bytes_are_unchanged_when_key_also_occurs_in_base64(self):
        key = "iVBOR"
        url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
        original = self.messages(url=url, text="问题 " + key)
        with MockApiServer() as server:
            server.enqueue("收到图片")
            ApiClient().complete_context(ApiSettings(server.base_url, "test-model"), key, original)
            sent = server.requests[0]["body"]["messages"][0]["content"]
            self.assertNotIn(key, sent[0]["text"])
            self.assertEqual(sent[1]["image_url"]["url"], url)
            self.assertIn(key, original[0]["content"][0]["text"])

    def test_rejected_images_and_explicit_refusal_never_retry_as_text(self):
        key = "private-test-key"
        with MockApiServer() as server:
            for index, status in enumerate((400, 415, 422)):
                server.enqueue("provider private details " + key, status=status)
                with self.subTest(status=status), self.assertRaises(ApiError) as caught:
                    ApiClient().complete_context(ApiSettings(server.base_url, "test-model"), key, self.messages())
                self.assertEqual(caught.exception.code, "vision_rejected")
                self.assertIn("确认模型支持图片并检查接口配置", str(caught.exception))
                self.assertNotIn(key, str(caught.exception))
                self.assertEqual(len(server.requests), index + 1)
            server.enqueue(body={"id": "refused", "object": "chat.completion", "created": 0,
                "model": "test-model", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": None, "refusal": "private refusal " + key}}]})
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(ApiSettings(server.base_url, "test-model"), key, self.messages())
            self.assertEqual(caught.exception.code, "vision_rejected")
            self.assertEqual(len(server.requests), 4)
            self.assertNotIn("private refusal", str(caught.exception))

    def test_external_images_and_non_user_images_are_refused_before_http(self):
        for role, url in (("user", "file:///private/photo.png"), ("user", "https://example.com/photo.png"),
                          ("assistant", "data:image/png;base64,QUJD")):
            with self.subTest(role=role, url=url), self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(ApiSettings("http://127.0.0.1:1/v1", "test-model"), "key",
                                             self.messages(url=url, role=role))
            self.assertEqual(caught.exception.code, "invalid_messages")


class ImageSyncContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-image-sync-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.store = ConversationStore(self.path)
        self.sync = RemoteSyncStore(self.store)
        self.cid = str(uuid4())

    def test_image_fields_are_strictly_allowlisted_in_snapshot_journal_and_helper(self):
        user = self.store.append_message(self.cid, "user", "图片问题", "user", metadata={
            "image_id": "a" * 64, "request_status": "failed", "image_error_code": "vision_rejected",
            "image_url": "data:image/png;base64,PRIVATE_IMAGE", "image_path": "C:/private/image.png"})
        self.store.append_message(self.cid, "assistant", "回答", "assistant", metadata={
            "image_id": "a" * 64, "request_status": "failed", "image_error_code": "image_missing"})
        bad = self.store.append_message(self.cid, "user", "非法引用", "user", metadata={
            "image_id": "C:/private/image.png", "request_status": "failed", "image_error_code": "PRIVATE_PROVIDER_BODY"})
        for page, field in ((self.sync.snapshot(), "items"), (self.sync.pull(), "changes")):
            messages = {item["id"]: item["data"] for item in page[field] if item["entity"] == "message"}
            self.assertEqual(messages[user["id"]]["image_id"], "a" * 64)
            self.assertEqual(messages[user["id"]]["image_error"], "vision_rejected")
            self.assertIsNone(messages[bad["id"]]["image_id"])
            self.assertIsNone(messages[bad["id"]]["image_error"])
            assistant = next(item for item in messages.values() if item["role"] == "assistant")
            self.assertIsNone(assistant["image_id"])
            self.assertIsNone(assistant["image_error"])
            for private in ("PRIVATE_IMAGE", "C:/private", "PRIVATE_PROVIDER_BODY", "data:image"):
                self.assertNotIn(private, json.dumps(page))
        valid = sanitize_message(user)
        self.assertEqual(valid["image_id"], "a" * 64)
        self.assertEqual(valid["image_error"], "vision_rejected")
        invalid = sanitize_message({"role": "user", "metadata": {"image_id": {}, "image_error_code": []}})
        self.assertIsNone(invalid["image_id"])
        self.assertIsNone(invalid["image_error"])

    def test_v2_upgrade_refreshes_message_triggers_without_replaying_operations(self):
        user = self.store.append_message(self.cid, "user", "保留的图片问题", "user", metadata={"image_id": "a" * 64})
        operation_id = str(uuid4())
        self.sync.reserve_operation("phone", operation_id, {"method": "chat.send", "image_id": "a" * 64})
        original_operation = self.sync.get_operation("phone", operation_id)
        snapshot = self.sync.snapshot(limit=1)
        with self.store._connection() as db:
            high = db.execute("SELECT high_seq FROM remote_sync_state").fetchone()[0]
            original_journal = [tuple(row) for row in db.execute("SELECT * FROM remote_sync_changes ORDER BY seq")]
            original_operations = [tuple(row) for row in db.execute("SELECT * FROM remote_sync_operations ORDER BY rowid")]
            db.execute("UPDATE remote_sync_schema SET version = 2")
            for operation in ("insert", "update"):
                db.execute(f"DROP TRIGGER remote_sync_message_{operation}")
                db.execute(f"""CREATE TRIGGER remote_sync_message_{operation} AFTER {operation} ON messages
                    BEGIN INSERT INTO remote_sync_changes(entity,entity_id,conversation_id,deleted,payload)
                    VALUES ('message',NEW.id,NEW.conversation_id,0,json_object('id',NEW.id)); END""")
        restarted = ConversationStore(self.path)
        sync = RemoteSyncStore(restarted)
        restarted.initialize()
        # v9/v10 append the current conversation's title-provenance projection
        # for already-caught-up peers. This is not a replay of an operation or
        # a rewrite of the immutable pre-upgrade journal.
        upgraded_high = sync.snapshot()["watermark"]
        self.assertEqual(upgraded_high, high + 1)
        refresh = sync.pull(high)["changes"]
        self.assertEqual(len(refresh), 1)
        self.assertEqual((refresh[0]["entity"], refresh[0]["id"]), ("conversation", self.cid))
        self.assertIs(refresh[0]["data"]["title_manual"], False)
        self.assertEqual(sync.get_operation("phone", operation_id), original_operation)
        with self.assertRaises(SyncResetRequired):
            sync.snapshot(cursor=snapshot["next_cursor"])
        with restarted._connection() as db:
            self.assertEqual(db.execute("SELECT version FROM remote_sync_schema").fetchone()[0], REMOTE_SYNC_SCHEMA_VERSION)
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM remote_sync_changes WHERE seq <= ? ORDER BY seq", (high,))], original_journal)
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM remote_sync_operations ORDER BY rowid")], original_operations)
            db.execute("UPDATE messages SET metadata = json_set(metadata, '$.image_id', ?) WHERE id = ?",
                       ("b" * 64, user["id"]))
        changes = sync.pull(upgraded_high)["changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["data"]["image_id"], "b" * 64)
        self.assertEqual(restarted.load_messages(self.cid)[0]["content"], "保留的图片问题")


if __name__ == "__main__":
    unittest.main()
