"""Owned temporary SQLite only; queue persistence never grants execution."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.conversations.store import ConversationStore, StoreError, remote_sync_projection
from desktop_pet.remote.sync import sanitize_message


class QueuedAgentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="queued-agent-store-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "archive.sqlite3"
        self.store = ConversationStore(self.path)
        self.cid, self.mid, self.op = (str(uuid4()) for _ in range(3))
        self.store.append_message(self.cid, "user", "Existing owned conversation", "user")
        self.options = {"origin_peer_id": "a" * 64, "operation_id": self.op,
                        "authorization_scope": "b" * 64, "settings_scope": "c" * 64}

    def save(self, **changes):
        options = dict(self.options); options.update(changes)
        return self.store.save_queued_agent_message(self.cid, self.mid, "Owned queued task", **options)

    def test_first_write_metadata_and_body_are_atomic_and_restart_preserves_queue(self):
        row = self.save(image_id="f" * 64, created_at="2026-11-03T10:00:00+00:00")
        self.assertEqual(row["role"], "user"); self.assertEqual(row["source"], "user")
        self.assertEqual(row["content"], "Owned queued task")
        self.assertEqual(row["metadata"], {"request_status": "queued", "execution_mode": "agent", "image_id": "f" * 64,
            "agent_queue_origin": {"version": 1, "peer_id": "a" * 64, "request_id": self.op,
                "authorization_scope": "b" * 64, "settings_scope": "c" * 64}})
        restarted = ConversationStore(self.path)
        self.assertEqual(restarted.read_message(self.cid, self.mid), row)
        recovered, reply = restarted.reconcile_request(self.cid, self.mid)
        self.assertEqual(recovered, row); self.assertIsNone(reply)
        self.assertEqual(self.store.get_conversation(self.cid)["message_count"], 2)

    def test_identical_retries_are_idempotent_and_never_downgrade_newer_status(self):
        original = self.save()
        for status in ("started", "pending", "complete", "cancelled"):
            with sqlite3.connect(self.path) as connection:
                connection.execute("UPDATE messages SET metadata=json_set(metadata,'$.request_status',?,'$.host_note','newer') WHERE id=?", (status, self.mid))
            current = self.store.read_message(self.cid, self.mid)
            retried = self.save(created_at="2030-01-01T00:00:00+00:00")
            self.assertEqual(retried, current)
            self.assertEqual(retried["created_at"], original["created_at"])
            self.assertEqual(retried["metadata"]["request_status"], status)
            self.assertEqual(retried["metadata"]["host_note"], "newer")
        self.assertEqual(self.store.get_conversation(self.cid)["message_count"], 2)

    def test_concurrent_duplicate_writes_have_one_row_and_one_queue_insert(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.save(), range(16)))
        self.assertTrue(all(row == rows[0] for row in rows))
        self.assertEqual(self.store.get_conversation(self.cid)["message_count"], 2)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM remote_sync_changes WHERE entity='message' AND entity_id=? AND deleted=0", (self.mid,)).fetchone()[0], 1)

    def test_conflicting_content_origin_image_or_conversation_never_overwrites(self):
        original = self.save(image_id="f" * 64)
        for changes in ({"operation_id": str(uuid4())}, {"origin_peer_id": "d" * 64}, {"authorization_scope": "e" * 64}, {"settings_scope": "d" * 64}, {"image_id": None}, {"image_id": "d" * 64}):
            with self.subTest(changes=tuple(changes)):
                options = dict(self.options, image_id="f" * 64); options.update(changes)
                with self.assertRaisesRegex(StoreError, "排队消息身份或内容发生冲突"):
                    self.store.save_queued_agent_message(self.cid, self.mid, "Owned queued task", **options)
        with self.assertRaises(StoreError):
            self.store.save_queued_agent_message(self.cid, self.mid, "Different private content", **dict(self.options, image_id="f" * 64))
        other = str(uuid4()); self.store.append_message(other, "user", "Other conversation", "user")
        with self.assertRaises(StoreError):
            self.store.save_queued_agent_message(other, self.mid, "Owned queued task", **dict(self.options, image_id="f" * 64))
        self.assertIsNone(self.store.read_message(other, self.mid))
        self.assertEqual(self.store.read_message(self.cid, self.mid), original)

    def test_existing_nonqueue_row_cannot_acquire_private_origin(self):
        row = self.store.append_message(self.cid, "user", "Owned queued task", "user", message_id=self.mid)
        with self.assertRaises(StoreError): self.save()
        self.assertEqual(self.store.read_message(self.cid, self.mid), row)

    def test_unknown_conversation_and_deleted_identifiers_are_not_created_or_resurrected(self):
        unknown = str(uuid4())
        with self.assertRaises(StoreError):
            self.store.save_queued_agent_message(unknown, self.mid, "Owned queued task", **self.options)
        self.assertIsNone(self.store.get_conversation(unknown))
        self.save(); self.store.clear_messages(self.cid)
        with self.assertRaises(StoreError): self.save()
        self.assertIsNone(self.store.read_message(self.cid, self.mid))
        self.assertEqual(self.store.load_messages(self.cid), [])
        self.store.delete_conversation(self.cid)
        with self.assertRaises(StoreError): self.save()
        self.assertIsNone(self.store.get_conversation(self.cid))

    def test_uuid_hash_and_utf8_validation_are_fixed_errors(self):
        invalid_ids = (None, "not-a-uuid", self.mid.upper(), "00000000-0000-0000-0000-000000000000")
        for value in invalid_ids:
            with self.subTest(identity=value):
                with self.assertRaises(StoreError): self.store.read_message(self.cid, value)
                with self.assertRaises(StoreError):
                    self.store.save_queued_agent_message(self.cid, value, "Private payload", **self.options)
                with self.assertRaises(StoreError): self.save(operation_id=value)
        for key in ("origin_peer_id", "authorization_scope", "settings_scope", "image_id"):
            for bad in ("A" * 64, "g" * 64, "a" * 63, True):
                with self.subTest(field=key):
                    with self.assertRaises(StoreError): self.save(**{key: bad})
        with self.assertRaises(StoreError):
            self.store.save_queued_agent_message(self.cid, self.mid, "\ud800", **self.options)
        self.assertIsNone(self.store.read_message(self.cid, self.mid))

    def test_image_request_preserves_original_nonempty_body_and_missing_read(self):
        with self.assertRaises(StoreError):
            self.store.save_queued_agent_message(self.cid, self.mid, "", image_id="f" * 64, **self.options)
        row = self.store.save_queued_agent_message(self.cid, self.mid, "请查看图片", image_id="f" * 64, **self.options)
        self.assertEqual(row["content"], "请查看图片")
        self.assertIsNone(self.store.read_message(self.cid, str(uuid4())))
        self.assertEqual(self.store.read_message(self.cid, self.mid), row)

    def test_failed_transaction_cannot_leave_body_metadata_or_sync_journal(self):
        with sqlite3.connect(self.path) as connection:
            before = connection.execute("SELECT COUNT(*) FROM remote_sync_changes").fetchone()[0]
        with patch.object(self.store, "_touch", side_effect=StoreError("固定测试中断")):
            with self.assertRaises(StoreError): self.save()
        self.assertIsNone(self.store.read_message(self.cid, self.mid))
        self.assertEqual(self.store.get_conversation(self.cid)["message_count"], 1)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM remote_sync_changes").fetchone()[0], before)

    def test_public_snapshot_journal_and_sanitizer_never_export_private_origin(self):
        row = self.save(image_id="f" * 64)
        with sqlite3.connect(self.path) as connection:
            projected = json.loads(connection.execute("SELECT " + remote_sync_projection("message") + " FROM messages WHERE id=?", (self.mid,)).fetchone()[0])
            journal = json.loads(connection.execute("SELECT payload FROM remote_sync_changes WHERE entity='message' AND entity_id=? ORDER BY seq DESC LIMIT 1", (self.mid,)).fetchone()[0])
        for public in (projected, journal, sanitize_message(row)):
            raw = json.dumps(public)
            self.assertNotIn("agent_queue_origin", raw)
            self.assertNotIn("authorization_scope", raw)
            self.assertNotIn("settings_scope", raw)
            self.assertNotIn("a" * 64, raw)
            self.assertNotIn("b" * 64, raw)
            self.assertNotIn("c" * 64, raw)
            self.assertNotIn(self.op, raw)
            self.assertEqual(public["image_id"], "f" * 64)


if __name__ == "__main__":
    unittest.main()
