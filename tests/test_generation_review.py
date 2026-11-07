"""Independent lightweight-generation and late-context-fence regressions."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.conversations.store import ConversationStore, StoreError


class GenerationQueryIndependentReviewTests(unittest.TestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="aura-generation-independent-")
        self.addCleanup(owned.cleanup)
        self.store = ConversationStore(Path(owned.name) / "owned.sqlite3")
        self.cid = str(uuid4())

    def test_timezone_and_insertion_ties_match_full_history_not_uuid_or_iso_lexical_order(self):
        self.store.append_message(self.cid, "user", "Later UTC but earlier ISO text", "user",
            message_id="00000000-0000-0000-0000-000000000001", created_at="2026-11-02T23:45:00-01:00")
        earlier = self.store.append_message(self.cid, "assistant", "Earlier UTC and first inserted at this instant", "assistant",
            message_id="ffffffff-ffff-ffff-ffff-ffffffffffff", created_at="2026-11-03T08:30:00+08:00")
        self.store.append_message(self.cid, "user", "Same UTC, lexically smaller UUID", "user",
            message_id="00000000-0000-0000-0000-000000000002", created_at="2026-11-03T00:30:00Z")
        self.assertEqual(earlier["id"], self.store.load_messages(self.cid)[0]["id"])
        self.assertEqual(earlier["id"], self.store.read_conversation_generation(self.cid))

    def test_generation_does_not_decode_archive_content_metadata_or_screens(self):
        first = self.store.append_message(self.cid, "user", "Synthetic large body " * 1000, "user")
        # A corrupt historical metadata document must not turn an identity-only
        # query into a full history decoder. Actual model preparation still has
        # to reject that corrupt archive; this does not authorize execution.
        with self.store._connection() as connection:
            connection.execute("UPDATE messages SET metadata='not-json' WHERE id=?", (first["id"],))
        original = self.store._connection
        @contextmanager
        def identity_only(**kwargs):
            with original(**kwargs) as connection:
                def authorize(action, table, column, *_):
                    if action == sqlite3.SQLITE_READ and (table == "screen_events" or
                            table == "messages" and column not in {"id", "conversation_id", "created_at", "ROWID"}):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                connection.set_authorizer(authorize)
                yield connection
        with patch.object(self.store, "_connection", side_effect=identity_only):
            self.assertEqual(first["id"], self.store.read_conversation_generation(self.cid))
        with self.assertRaises(StoreError): self.store.load_messages(self.cid)

    def test_empty_clear_new_generation_and_durable_delete_never_restore_old_identity(self):
        first = self.store.append_message(self.cid, "user", "Old identity", "user")
        self.store.clear_messages(self.cid)
        self.assertIsNone(ConversationStore(self.store.path).read_conversation_generation(self.cid))
        fresh = self.store.append_message(self.cid, "user", "Fresh generation", "user")
        self.assertNotEqual(first["id"], fresh["id"])
        self.assertEqual(fresh["id"], self.store.read_conversation_generation(self.cid))
        self.store.delete_conversation(self.cid)
        restarted = ConversationStore(self.store.path)
        self.assertIsNone(restarted.read_conversation_generation(self.cid))
        with self.assertRaises(StoreError): restarted.append_message(self.cid, "user", "No resurrection", "user")

    def test_queue_admission_recheck_and_invalidation_never_materialize_archive(self):
        from tests.test_host_queue_review import HostQueueIndependentReviewTests
        fixture = HostQueueIndependentReviewTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(fixture.store, "load_messages", side_effect=AssertionError("No archive materialization in queue fences")):
            ack = fixture.admit()
            job = fixture.queue.get(ack["job_id"])
            self.assertEqual(fixture.seed["id"], job.generation)
            self.assertTrue(fixture.coordinator._scope(job).authorized)
            fixture.coordinator.invalidate(fixture.cid)
            self.assertEqual("cancelled", fixture.queue.get(ack["job_id"]).state)


class ContextLateGenerationIndependentReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from tests import test_remote_controller as fixture
        fixture.RemoteControllerTests.setUp(self)
        self.cid = str(uuid4())
        self.first = self.chat.store.append_message(self.cid, "user", "Synthetic original history", "user")

    def tearDown(self):
        from tests import test_remote_controller as fixture
        fixture.RemoteControllerTests.tearDown(self)

    def test_context_export_materializes_history_once_and_checks_identity_separately(self):
        with patch.object(self.chat.store, "load_messages", wraps=self.chat.store.load_messages) as history, \
             patch.object(self.chat.store, "read_conversation_generation", wraps=self.chat.store.read_conversation_generation) as generation:
            envelope = self.chat.context_envelope_for(self.cid, policy={"secrets": (), "model_context_tokens": 32000})
        self.assertEqual(1, history.call_count)
        self.assertEqual(1, generation.call_count)
        self.assertEqual(self.first["id"], envelope["generation"])

    def test_clear_and_recreate_during_memory_lookup_fails_late_lightweight_fence(self):
        def changed(*_, **__):
            self.chat.store.clear_messages(self.cid)
            self.chat.store.append_message(self.cid, "user", "Synthetic new generation", "user")
            return []
        self.chat._memory = SimpleNamespace(load_settings=lambda: SimpleNamespace(enabled=True, relevance_threshold=.6), search=changed)
        with self.assertRaises(StoreError):
            self.chat.context_envelope_for(self.cid, policy={"secrets": (), "model_context_tokens": 32000})
        self.chat._memory = None

    def test_empty_generation_still_rejects_durable_conversation_deletion(self):
        self.chat.store.clear_messages(self.cid)
        def deleted(*_, **__): self.chat.store.delete_conversation(self.cid); return []
        self.chat._memory = SimpleNamespace(load_settings=lambda: SimpleNamespace(enabled=True, relevance_threshold=.6), search=deleted)
        with self.assertRaises(StoreError):
            self.chat.context_envelope_for(self.cid, query="Synthetic explicit lookup",
                policy={"secrets": (), "model_context_tokens": 32000})
        self.chat._memory = None


if __name__ == "__main__": unittest.main()
