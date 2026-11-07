"""Owned SQLite rollback/readback faults and explicit history recovery."""
import sqlite3
import threading
import unittest
from unittest.mock import patch

from tests import test_archive_mutation_readback as fixture
from tests.test_chat_controller import RecordingClient
from desktop_pet.settings_manager import ApiSettings


class UncertainDeleteRecoveryTests(unittest.TestCase):
    setUpClass = fixture.ArchiveMutationReadbackTests.__dict__["setUpClass"]
    setUp = fixture.ArchiveMutationReadbackTests.setUp
    tearDown = fixture.ArchiveMutationReadbackTests.tearDown
    pump = fixture.ArchiveMutationReadbackTests.pump
    settled = fixture.ArchiveMutationReadbackTests.settled

    def unknown_rolled_back_delete(self, *, wait_all=True):
        actual_connect = sqlite3.connect
        mutation_entries = []
        cid = self.cid

        class RollbackFailure(sqlite3.Connection):
            delete_started = False

            def execute(self, sql, parameters=(), *args, **kwargs):
                if " ".join(sql.upper().split()).startswith("DELETE FROM CONVERSATIONS WHERE ID") and parameters == (cid,):
                    self.delete_started = True
                    mutation_entries.append(threading.get_ident())
                return super().execute(sql, parameters, *args, **kwargs)

            def __exit__(self, kind, value, traceback):
                if self.delete_started and kind is None:
                    self.rollback()
                    raise sqlite3.OperationalError("OWNED_ROLLED_BACK_COMMIT_ERROR")
                return super().__exit__(kind, value, traceback)

        def connect(path, *args, **kwargs):
            if str(path) == str(self.store.path):
                kwargs["factory"] = RollbackFailure
            return actual_connect(path, *args, **kwargs)

        with patch.object(sqlite3, "connect", connect), patch.object(self.store, "get_conversation", side_effect=OSError("OWNED_UNAVAILABLE_READBACK")):
            self.assertTrue(self.chat.delete_conversation(self.cid))
            self.pump(lambda: self.cid not in self.chat._invalidating_conversations)
            if wait_all:
                self.pump(lambda: not self.chat._runner._tasks)
        self.assertEqual(1, len(mutation_entries))
        self.assertNotEqual(threading.get_ident(), mutation_entries[0])
        self.assertIsNotNone(self.store.get_conversation(self.cid))
        self.assertEqual(["OWNED_ORIGINAL"], [row["content"] for row in self.store.load_messages(self.cid)])
        self.assertEqual([], self.chat.messages)
        self.assertIn(self.cid, self.chat._deleted_conversations)
        self.assertTrue(self.chat.error)
        self.assertEqual("conversation_deleted", self.chat.external_import_state(self.cid))
        return mutation_entries

    def test_explicit_fresh_reload_of_surviving_archive_restores_new_message_admission(self):
        mutations = self.unknown_rolled_back_delete()
        self.assertTrue(self.chat.reload_history(self.cid))
        self.pump(lambda: not self.chat.history_loading)
        self.assertEqual(["OWNED_ORIGINAL"], [row["content"] for row in self.chat.messages])
        self.assertEqual("ready", self.chat.external_import_state(self.cid))
        self.assertNotIn(self.cid, self.chat._deleted_conversations)
        client = RecordingClient()
        self.chat._client = client
        with patch.object(self.chat, "_configuration", return_value=(ApiSettings(model="owned-test-model"), "owned-memory-key")), \
             patch.object(self.chat, "_routing_for_turn", return_value=False):
            self.assertTrue(self.chat.send("OWNED_NEW_EXPLICIT_USER_MESSAGE"))
            self.pump(lambda: not self.chat.busy)
            self.pump(lambda: not self.chat._runner._tasks)
        self.assertEqual(1, len(mutations), "Recovery cannot retry the delete")
        self.assertEqual(1, len(client.requests))
        self.assertEqual(1, sum(row["content"] == "OWNED_NEW_EXPLICIT_USER_MESSAGE" for row in self.store.load_messages(self.cid)))

    def test_failed_explicit_reload_cannot_release_unknown_delete_guard(self):
        self.unknown_rolled_back_delete()
        with patch.object(self.store, "get_conversation", side_effect=OSError("OWNED_HISTORY_READ_FAILURE")):
            self.assertTrue(self.chat.reload_history(self.cid))
            self.pump(lambda: not self.chat.history_loading)
        self.assertIn(self.cid, self.chat._deleted_conversations)
        self.assertEqual("conversation_deleted", self.chat.external_import_state(self.cid))
        self.assertEqual([], self.chat.messages)
        self.assertTrue(self.chat.error)

    def test_actual_committed_delete_stays_deleted_after_explicit_reload(self):
        delete = self.store.delete_conversation
        def committed(cid):
            delete(cid)
            raise OSError("OWNED_COMMITTED_ACK_ERROR")
        with patch.object(self.store, "delete_conversation", committed), \
             patch.object(self.store, "load_messages", side_effect=OSError("OWNED_READBACK_FAILURE")):
            self.assertTrue(self.chat.delete_conversation(self.cid))
            self.settled()
        self.assertIsNone(self.store.get_conversation(self.cid))
        self.assertTrue(self.chat.reload_history(self.cid))
        self.pump(lambda: not self.chat.history_loading)
        self.assertIn(self.cid, self.chat._deleted_conversations)
        self.assertEqual("conversation_deleted", self.chat.external_import_state(self.cid))
        self.assertEqual([], self.chat.messages)
        self.assertTrue(self.chat.error)

    def test_generic_snapshot_install_cannot_release_unknown_delete_guard(self):
        self.unknown_rolled_back_delete()
        self.chat._install_snapshot(self.cid, self.store.get_conversation(self.cid), self.store.load_messages(self.cid))
        self.assertIn(self.cid, self.chat._deleted_conversations)
        self.assertEqual("conversation_deleted", self.chat.external_import_state(self.cid))

    def test_history_read_started_before_unknown_delete_cannot_recover_it(self):
        entered, release = threading.Event(), threading.Event()
        read = self.store.load_messages
        first = [True]
        def held(cid):
            rows = read(cid)
            if first[0]:
                first[0] = False
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("Owned pre-delete history not released")
            return rows
        with patch.object(self.store, "load_messages", held):
            try:
                self.assertTrue(self.chat.open_conversation(self.cid))
                self.assertTrue(entered.wait(2))
                self.unknown_rolled_back_delete(wait_all=False)
            finally:
                release.set()
            self.pump(lambda: not self.chat._runner._tasks)
        self.assertEqual([], self.chat.messages)
        self.assertIn(self.cid, self.chat._deleted_conversations)
        self.assertEqual("conversation_deleted", self.chat.external_import_state(self.cid))

    def test_new_actual_delete_invalidates_pending_explicit_recovery_read(self):
        self.unknown_rolled_back_delete()
        entered, release = threading.Event(), threading.Event()
        read = self.store.load_messages
        first = [True]
        def held(cid):
            rows = read(cid)
            if first[0]:
                first[0] = False
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("Owned recovery history not released")
            return rows
        with patch.object(self.store, "load_messages", held):
            try:
                self.assertTrue(self.chat.reload_history(self.cid))
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.chat.delete_conversation(self.cid))
                self.pump(lambda: self.cid not in self.chat._invalidating_conversations)
            finally:
                release.set()
            self.pump(lambda: not self.chat._runner._tasks)
        self.assertIsNone(self.store.get_conversation(self.cid))
        self.assertIn(self.cid, self.chat._deleted_conversations)
        self.assertEqual("conversation_deleted", self.chat.external_import_state(self.cid))
        self.assertNotEqual(self.cid, self.chat.current_conversation_id)
        self.assertEqual([], self.chat.messages)


if __name__ == "__main__":
    unittest.main()
