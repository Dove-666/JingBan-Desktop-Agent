"""SQLite-backed protocol guarantees, privacy, pagination and durable admission."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.conversations.store import ConversationStore, REMOTE_SYNC_SCHEMA_VERSION, SCHEMA_VERSION, StoreError
from desktop_pet.remote.sync import RemoteSyncStore, SyncError, SyncResetRequired, sanitize_message


class RemoteSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.store = ConversationStore(self.path)
        self.sync = RemoteSyncStore(self.store)
        self.cid = str(uuid4())

    def _all_snapshot(self, limit=2):
        page = self.sync.snapshot(limit=limit)
        watermark, items = page["watermark"], list(page["items"])
        while page["has_more"]:
            page = self.sync.snapshot(limit=limit, cursor=page["next_cursor"])
            self.assertEqual(page["watermark"], watermark)
            items.extend(page["items"])
        self.assertEqual(page["next_cursor"], "")
        return items, watermark

    def _all_changes(self, since=0, limit=2):
        changes = []
        while True:
            page = self.sync.pull(since=since, limit=limit)
            changes.extend(page["changes"])
            since = page["next_since"]
            if not page["has_more"]:
                return changes, since

    def test_additive_migration_keeps_archive_schema_and_preexisting_history(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            ConversationStore._migrate_v1(connection)
            ConversationStore._migrate_v2(connection)
            ConversationStore._migrate_v3(connection)
            connection.execute("PRAGMA user_version = 3")
            connection.execute("""INSERT INTO conversations(id,title,created_at,updated_at)
                VALUES (?, 'before migration', '2026', '2026')""", (self.cid,))
            connection.execute("""INSERT INTO messages(id,conversation_id,role,content,created_at,source)
                VALUES ('legacy', ?, 'user', 'old text', '2026', 'user')""", (self.cid,))
        items, watermark = self._all_snapshot()
        self.assertEqual(watermark, 0)
        self.assertEqual(len(items), 2)
        self.assertEqual(next(i["data"]["content"] for i in items if i["entity"] == "message"), "old text")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(connection.execute("SELECT version FROM remote_sync_schema").fetchone()[0],
                             REMOTE_SYNC_SCHEMA_VERSION)
        self.store.rename_conversation(self.cid, "after migration")
        self.assertEqual(self.sync.pull()["changes"][0]["data"]["title"], "after migration")

    def test_snapshot_public_fields_exclude_metadata_local_paths_and_private_summaries(self):
        user = self.store.append_message(self.cid, "user", "allowed message text", "user",
            metadata={"api_key": "SECRET_API_KEY", "image_path": "C:/private/screen.png",
                      "request_status": "pending", "origin": "remote"})
        reply = self.store.complete_request(self.cid, user["id"], "allowed reply", emotion="happy")
        self.store.update_metadata(self.cid, summary="PRIVATE_INTERNAL_SUMMARY")
        self.store.append_screen_comment(self.cid, "allowed visible comment", event_id=str(uuid4()),
            app_name="PRIVATE_WINDOW_TITLE", context_summary="PRIVATE_OBSERVATION_SUMMARY", event_type="media",
            metadata={"image": "C:/private/frame.jpg", "secret": "SECRET_CAPTURE"}, source_kind="camera")
        items, _ = self._all_snapshot()
        payload = json.dumps(items)
        for excluded in ("SECRET_API_KEY", "C:/private", "PRIVATE_INTERNAL_SUMMARY", "PRIVATE_WINDOW_TITLE",
                         "PRIVATE_OBSERVATION_SUMMARY", "SECRET_CAPTURE", '"metadata"'):
            self.assertNotIn(excluded, payload)
        messages = {item["id"]: item["data"] for item in items if item["entity"] == "message"}
        self.assertEqual(messages[user["id"]]["request_status"], "complete")
        self.assertEqual(messages[reply["id"]]["reply_to"], user["id"])
        self.assertIn("allowed visible comment", payload)
        event = next(item["data"] for item in items if item["entity"] == "screen_event")
        self.assertEqual(set(event), {"id", "conversation_id", "observed_at", "source_kind"})

    def test_sanitize_message_never_copies_unrecognized_metadata(self):
        reply_id = str(uuid4())
        message = sanitize_message({"id": str(uuid4()), "content": "hello", "metadata": {
            "request_status": "pending", "reply_to": reply_id, "api_key": "SECRET", "image": "private.png"},
            "screen_event": {"app_name": "private"}})
        self.assertEqual(message["reply_to"], reply_id)
        self.assertEqual(message["request_status"], "pending")
        self.assertNotIn("metadata", message)
        self.assertNotIn("SECRET", json.dumps(message))
        invalid = sanitize_message({"metadata": {"request_status": "SECRET", "reply_to": "C:/private.png"}})
        self.assertIsNone(invalid["request_status"])
        self.assertIsNone(invalid["reply_to"])

    def test_reply_to_is_only_same_conversation_user_id(self):
        other = str(uuid4())
        user = self.store.append_message(other, "user", "other", "user")
        self.store.append_message(self.cid, "assistant", "reply", "assistant",
                                  metadata={"reply_to": user["id"], "request_status": "api-key-secret"})
        items, _ = self._all_snapshot()
        reply = next(i["data"] for i in items if i["entity"] == "message" and i["data"]["content"] == "reply")
        self.assertIsNone(reply["reply_to"])
        self.assertIsNone(reply["request_status"])

    def test_snapshot_pages_are_stable_and_changes_after_watermark_catch_up(self):
        originals = [self.store.append_message(self.cid, "user", f"before-{i}", "user") for i in range(7)]
        first = self.sync.snapshot(limit=2)
        self.store.clear_messages(self.cid)
        later = self.store.append_message(self.cid, "user", "after snapshot", "user")
        self.store.rename_conversation(self.cid, "new title")
        items = list(first["items"])
        cursor = first["next_cursor"]
        while cursor:
            page = self.sync.snapshot(limit=2, cursor=cursor)
            self.assertEqual(page["watermark"], first["watermark"])
            items.extend(page["items"])
            cursor = page["next_cursor"]
        original_ids = {m["id"] for m in originals}
        self.assertEqual({i["id"] for i in items if i["entity"] == "message"}, original_ids)
        self.assertNotIn(later["id"], {i["id"] for i in items})
        cache = {(i["entity"], i["id"]): i["data"] for i in items}
        changes, cursor = self._all_changes(first["watermark"], limit=1)
        self.assertTrue(changes)
        self.assertEqual(len({change["seq"] for change in changes}), len(changes))
        for change in changes:
            key = change["entity"], change["id"]
            if change["deleted"]:
                cache.pop(key, None)
            else:
                cache[key] = change["data"]
        self.assertEqual({key[1] for key in cache if key[0] == "message"}, {later["id"]})
        self.assertEqual(cache[("conversation", self.cid)]["title"], "new title")
        self.assertEqual(self.sync.pull(cursor)["changes"], [])

    def test_journal_payload_is_immutable_and_deleted_messages_have_tombstones(self):
        message = self.store.append_message(self.cid, "user", "original", "user",
                                            metadata={"request_status": "pending"})
        self.store.set_message_status(self.cid, message["id"], "failed")
        self.store.clear_messages(self.cid)
        changes, _ = self._all_changes()
        message_changes = [c for c in changes if c["entity"] == "message" and c["id"] == message["id"]]
        self.assertEqual([c["data"].get("request_status") for c in message_changes], ["pending", "failed", None])
        self.assertEqual(message_changes[-1]["data"], {})
        self.assertTrue(message_changes[-1]["deleted"])
        with self.assertRaises(StoreError):
            self.store.append_message(self.cid, "user", "late resend", "user", message_id=message["id"])
        self.assertEqual(self.store.load_messages(self.cid), [])
        self.store.append_message(self.cid, "user", "new turn with new UUID", "user")
        self.store.delete_conversation(self.cid)
        with self.assertRaises(StoreError):
            self.store.append_message(self.cid, "user", "late resurrection", "user")

    def test_cascading_delete_logs_event_message_and_conversation_without_private_data(self):
        message = self.store.append_screen_comment(self.cid, "comment", event_id="event", app_name="private",
            context_summary="context", event_type="error", metadata={"private": "SECRET"})
        _, since = self._all_changes()
        self.store.delete_conversation(self.cid)
        changes, _ = self._all_changes(since)
        deleted = {(c["entity"], c["id"]) for c in changes if c["deleted"]}
        self.assertIn(("conversation", self.cid), deleted)
        self.assertIn(("message", message["id"]), deleted)
        self.assertIn(("screen_event", "event"), deleted)
        self.assertNotIn("SECRET", json.dumps(changes))

    def test_failed_local_transaction_rolls_back_the_journal(self):
        self.store.append_message(self.cid, "user", "existing", "user", message_id="duplicate")
        _, before = self._all_changes()
        other = str(uuid4())
        with self.assertRaises(StoreError):
            self.store.append_screen_comment(other, "failed", event_id="bad", app_name="app",
                context_summary="summary", event_type="error", message_id="duplicate")
        self.assertEqual(self.sync.pull(before)["changes"], [])

    def test_pagination_validation_and_no_unbounded_page(self):
        for limit in (0, 101, -1, True, "2"):
            with self.assertRaises(SyncError):
                self.sync.snapshot(limit=limit)
            with self.assertRaises(SyncError):
                self.sync.pull(limit=limit)
        for since in (-1, True, "0", 2**63):
            with self.assertRaises(SyncError):
                self.sync.pull(since=since)
        self.sync.snapshot()
        with self.assertRaises(SyncError):
            self.sync.pull(since=1)
        for cursor in ("broken", "../file:1", str(uuid4()) + ":-1", None):
            with self.assertRaises(SyncError):
                self.sync.snapshot(cursor=cursor)

    def test_snapshot_cache_is_bounded_expiring_and_retryable(self):
        self.store.append_message(self.cid, "user", "hello", "user")
        first = self.sync.snapshot(limit=1)
        replay = self.sync.snapshot(limit=1, cursor=first["next_cursor"])
        self.assertEqual(replay, self.sync.snapshot(limit=1, cursor=first["next_cursor"]))
        for _ in range(self.sync.MAX_SNAPSHOTS):
            self.sync.snapshot(limit=1)
        with self.assertRaises(SyncError):
            self.sync.snapshot(cursor=first["next_cursor"])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertLessEqual(connection.execute("SELECT count(*) FROM remote_sync_snapshots").fetchone()[0],
                                 self.sync.MAX_SNAPSHOTS)
            connection.execute("UPDATE remote_sync_snapshots SET created_at = 0")
        recent = self.sync.snapshot(limit=1)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM remote_sync_snapshots").fetchone()[0], 1)
        with patch("desktop_pet.remote.sync.time.time", return_value=time.time() + 1000):
            with self.assertRaises(SyncError):
                self.sync.snapshot(cursor=recent["next_cursor"])

    def test_snapshot_capacity_error_is_atomic(self):
        self.store.append_message(self.cid, "user", "hello", "user")
        with patch.object(self.sync, "MAX_SNAPSHOT_ITEMS", 1):
            with self.assertRaises(SyncError):
                self.sync.snapshot()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM remote_sync_snapshots").fetchone()[0], 0)
        with patch.object(self.sync, "MAX_SNAPSHOT_BYTES", 1):
            with self.assertRaises(SyncError):
                self.sync.snapshot()

    def test_operation_reservation_survives_restart_and_completed_ack_replay(self):
        payload = {"action": "chat.send", "text": "hello", "message_id": str(uuid4())}
        first = self.sync.reserve_operation("phone", "request-1", payload)
        self.assertTrue(first["accepted"])
        restarted = RemoteSyncStore(ConversationStore(self.path))
        duplicate = restarted.reserve_operation("phone", "request-1", payload)
        self.assertFalse(duplicate["accepted"])
        self.assertEqual(duplicate["state"], "pending")
        # A pending request is not automatically retried, even after restart.
        self.assertEqual(restarted.get_operation("phone", "request-1")["state"], "pending")
        result = {"ok": True, "message_id": payload["message_id"]}
        completed = restarted.complete_operation("phone", "request-1", result)
        self.assertEqual(completed["state"], "complete")
        self.assertEqual(completed, restarted.complete_operation("phone", "request-1", result))
        self.assertEqual(restarted.reserve_operation("phone", "request-1", payload)["result"], result)
        self.assertIsNone(restarted.get_operation("phone", "missing"))

    def test_operation_ids_are_bound_to_payload_and_result_and_expiry(self):
        self.sync.reserve_operation("phone", "one", {"action": "pet.expression", "value": "happy"})
        with self.assertRaises(SyncError):
            self.sync.reserve_operation("phone", "one", {"action": "pet.expression", "value": "angry"})
        self.sync.complete_operation("phone", "one", {"ok": True})
        with self.assertRaises(SyncError):
            self.sync.complete_operation("phone", "one", {"ok": False})
        with self.assertRaises(SyncError):
            self.sync.complete_operation("phone", "missing", {})
        expires = time.time() + 30
        self.sync.reserve_operation("phone", "two", {}, expires_at=expires, ephemeral=True)
        with self.assertRaises(SyncError):
            self.sync.reserve_operation("phone", "two", {}, expires_at=expires + 1, ephemeral=True)
        with self.assertRaises(SyncError):
            self.sync.reserve_operation("phone", "transient-without-expiry", {}, ephemeral=True)

    def test_expired_commands_are_never_admitted_or_completed(self):
        expired = self.sync.reserve_operation("phone", "old", {"action": "pet.expression"},
            expires_at=time.time() - 1, ephemeral=True)
        self.assertFalse(expired["accepted"])
        self.assertEqual(expired["state"], "expired")
        with self.assertRaises(SyncError):
            self.sync.complete_operation("phone", "old", {})
        deadline = time.time() + 1
        self.sync.reserve_operation("phone", "crashed", {}, expires_at=deadline, ephemeral=True)
        with patch("desktop_pet.remote.sync.time.time", return_value=deadline + 1):
            self.assertEqual(self.sync.reserve_operation("phone", "crashed", {},
                expires_at=deadline, ephemeral=True)["state"], "expired")

    def test_concurrent_operation_reservations_have_one_winner(self):
        self.store.initialize()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.sync.reserve_operation("phone", "same", {"text": "hello"}),
                                        range(12)))
        self.assertEqual(sum(result["accepted"] for result in results), 1)

    def test_bounded_journal_requires_snapshot_for_old_cursor_without_losing_history(self):
        with patch("desktop_pet.conversations.store.REMOTE_SYNC_MAX_CHANGES", 8), \
                patch("desktop_pet.conversations.store.REMOTE_SYNC_PRUNE_BATCH", 2):
            for i in range(12):
                self.store.append_message(self.cid, "user", f"message-{i}", "user")
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.row_factory = sqlite3.Row
                state = connection.execute("SELECT * FROM remote_sync_state").fetchone()
                count, size = connection.execute(
                    "SELECT count(*), coalesce(sum(length(CAST(payload AS BLOB))),0) FROM remote_sync_changes").fetchone()
                self.assertEqual(state["entry_count"], count)
                self.assertEqual(state["payload_bytes"], size)
                self.assertLessEqual(count, 8)
                self.assertGreater(state["floor_seq"], 0)
                self.assertEqual(state["high_seq"], connection.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name='remote_sync_changes'").fetchone()[0])
            with self.assertRaises(SyncResetRequired) as caught:
                self.sync.pull(0)
            self.assertEqual(caught.exception.code, "sync_reset_required")
            with self.assertRaises(SyncResetRequired):
                self.sync.pull(state["high_seq"] + 1)
            items, watermark = self._all_snapshot(limit=3)
            self.assertEqual(len([item for item in items if item["entity"] == "message"]), 12)
            later = self.store.append_message(self.cid, "user", "after bounded snapshot", "user")
            changes, next_since = self._all_changes(since=watermark, limit=1)
            self.assertIn(later["id"], {item["id"] for item in changes if item["entity"] == "message"})
            self.assertEqual(self.sync.pull(next_since)["changes"], [])
            self.assertEqual(len(self.store.load_messages(self.cid)), 13)

    def test_snapshot_that_falls_behind_journal_floor_requires_reset(self):
        with patch("desktop_pet.conversations.store.REMOTE_SYNC_MAX_CHANGES", 8), \
                patch("desktop_pet.conversations.store.REMOTE_SYNC_PRUNE_BATCH", 2):
            self.store.append_message(self.cid, "user", "before", "user")
            first = self.sync.snapshot(limit=1)
            self.assertTrue(first["next_cursor"])
            for i in range(12):
                self.store.append_message(self.cid, "user", f"later-{i}", "user")
            with self.assertRaises(SyncResetRequired):
                self.sync.snapshot(cursor=first["next_cursor"])
            with self.assertRaises(SyncResetRequired):
                self.sync.pull(first["watermark"])
            fresh, watermark = self._all_snapshot()
            self.assertEqual(len([i for i in fresh if i["entity"] == "message"]), 13)
            self.assertEqual(self.sync.pull(watermark)["changes"], [])

    def test_payload_budget_is_bounded_and_tombstones_outlive_compaction(self):
        with patch("desktop_pet.conversations.store.REMOTE_SYNC_MAX_CHANGE_BYTES", 500):
            old = self.store.append_message(self.cid, "user", "large message " * 100, "user")
            self.store.clear_messages(self.cid)
            for i in range(4):
                self.store.append_message(self.cid, "user", "another large message " * 100, "user")
            with closing(sqlite3.connect(self.path)) as connection, connection:
                size = connection.execute("SELECT payload_bytes FROM remote_sync_state").fetchone()[0]
                self.assertLessEqual(size, 500)
                self.assertGreater(connection.execute("SELECT count(*) FROM remote_sync_tombstones").fetchone()[0], 0)
            with self.assertRaises(StoreError):
                self.store.append_message(self.cid, "user", "old resend", "user", message_id=old["id"])
            self.assertEqual(len(self.store.load_messages(self.cid)), 4)

    def test_high_watermark_never_retreats_when_journal_is_completely_compacted(self):
        with patch("desktop_pet.conversations.store.REMOTE_SYNC_MAX_CHANGE_BYTES", 0):
            self.store.append_message(self.cid, "user", "kept in source history", "user")
            with closing(sqlite3.connect(self.path)) as connection, connection:
                count = connection.execute("SELECT count(*) FROM remote_sync_changes").fetchone()[0]
                floor, high = connection.execute("SELECT floor_seq, high_seq FROM remote_sync_state").fetchone()
            self.assertEqual(count, 0)
            self.assertGreater(high, 0)
            self.assertEqual(floor, high)
            self.assertEqual(self.sync.pull(high)["next_since"], high)
            self.assertEqual(self.sync.snapshot()["watermark"], high)
            with self.assertRaises(SyncResetRequired):
                self.sync.pull(0)
        self.store.append_message(self.cid, "user", "new turn", "user")
        changes, next_since = self._all_changes(high)
        self.assertTrue(all(item["seq"] > high for item in changes))
        self.assertGreater(next_since, high)
        self.assertEqual(len(self.store.load_messages(self.cid)), 2)

    def test_upgrade_old_sync_schema_preserves_sequence_and_discards_only_derived_cache(self):
        self.store.append_message(self.cid, "user", "preserved history", "user")
        snapshot = self.sync.snapshot(limit=1)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            high = connection.execute("SELECT high_seq FROM remote_sync_state").fetchone()[0]
            connection.execute("DROP TRIGGER remote_sync_journal_added")
            connection.execute("DROP TRIGGER remote_sync_journal_removed")
            connection.execute("DROP TABLE remote_sync_state")
            connection.execute("UPDATE remote_sync_schema SET version = 1")
        restarted = ConversationStore(self.path)
        sync = RemoteSyncStore(restarted)
        restarted.initialize()
        with self.assertRaises(SyncResetRequired):
            sync.snapshot(cursor=snapshot["next_cursor"])
        self.assertEqual(sync.snapshot()["watermark"], high)
        self.assertEqual(restarted.load_messages(self.cid)[0]["content"], "preserved history")
        self.assertTrue(sync.pull(0)["changes"])


if __name__ == "__main__":
    unittest.main()
