"""Generation identity stays chronological without loading archive bodies."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from desktop_pet.conversations.store import ConversationStore, StoreError


class ConversationGenerationTests(unittest.TestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="aura-generation-query-")
        self.addCleanup(owned.cleanup)
        self.store = ConversationStore(Path(owned.name) / "archive.sqlite3")
        self.cid = str(uuid4())

    def test_missing_and_empty_conversations_have_no_generation(self):
        self.assertIsNone(self.store.read_conversation_generation(self.cid))
        self.assertIsNone(self.store.get_conversation(self.cid))
        self.store.append_message(self.cid, "user", "Owned initial turn", "user")
        self.store.clear_messages(self.cid)
        self.assertIsNotNone(self.store.get_conversation(self.cid))
        self.assertIsNone(self.store.read_conversation_generation(self.cid))

    def test_offset_normalization_and_equal_instant_rowid_match_archive_order(self):
        # ISO lexical order, UUID order and insertion order differ here.
        self.store.append_message(self.cid, "user", "Later in UTC", "user",
            message_id="00000000-0000-0000-0000-000000000001", created_at="2026-11-03T00:30:00+00:00")
        first = self.store.append_message(self.cid, "user", "Earlier UTC, first tie", "user",
            message_id="ffffffff-ffff-ffff-ffff-ffffffffffff", created_at="2026-11-03T08:00:00+08:00")
        self.store.append_message(self.cid, "assistant", "Same instant, later row", "assistant",
            message_id="00000000-0000-0000-0000-000000000002", created_at="2026-11-03T00:00:00Z")
        self.assertEqual(self.store.load_messages(self.cid)[0]["id"], first["id"])
        self.assertEqual(self.store.read_conversation_generation(self.cid), first["id"])
        restarted = ConversationStore(self.store.path)
        self.assertEqual(restarted.read_conversation_generation(self.cid), first["id"])

    def test_earlier_import_changes_identity_and_clear_never_keeps_old_generation(self):
        self.store.append_message(self.cid, "user", "Current first", "user", created_at="2026-11-03T03:00:00Z")
        imported = self.store.append_message(self.cid, "user", "Earlier imported turn", "user",
            created_at="2026-11-02T03:00:00Z")
        self.assertEqual(self.store.read_conversation_generation(self.cid), imported["id"])
        self.store.clear_messages(self.cid)
        self.assertIsNone(self.store.read_conversation_generation(self.cid))
        fresh = self.store.append_message(self.cid, "user", "Fresh after clear", "user")
        self.assertEqual(self.store.read_conversation_generation(self.cid), fresh["id"])
        self.store.delete_conversation(self.cid)
        self.assertIsNone(self.store.read_conversation_generation(self.cid))
        with self.assertRaises(StoreError):
            self.store.append_message(self.cid, "user", "Must not resurrect", "user")

    def test_other_conversations_cannot_supply_generation(self):
        other = str(uuid4())
        self.store.append_message(other, "user", "Different owner", "user", created_at="2000-01-01T00:00:00Z")
        first = self.store.append_message(self.cid, "user", "Current owner", "user")
        self.assertEqual(self.store.read_conversation_generation(self.cid), first["id"])

    def test_sql_authorizer_can_deny_all_body_metadata_and_screen_reads(self):
        first = self.store.append_message(self.cid, "user", "Q" * 24000, "user", metadata={"owned": "M" * 4096})
        original_connection = self.store._connection
        from contextlib import contextmanager
        @contextmanager
        def guarded_connection(**kwargs):
            with original_connection(**kwargs) as connection:
                def authorize(action, table, column, *_):
                    if action == sqlite3.SQLITE_READ and (table == "screen_events" or
                            table == "messages" and column in {"content", "metadata", "screen_event_id"}):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                connection.set_authorizer(authorize)
                yield connection
        self.store._connection = guarded_connection
        self.assertEqual(self.store.read_conversation_generation(self.cid), first["id"])
        # The old full-archive route really needs the forbidden columns.
        with self.assertRaises(StoreError):
            self.store.load_messages(self.cid)


if __name__ == "__main__":
    unittest.main()
