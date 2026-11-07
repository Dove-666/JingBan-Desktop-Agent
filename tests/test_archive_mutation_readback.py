"""Owned real SQLite/controller fixtures for uncertain archive mutation ACKs."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from tests.test_application import isolated_settings, MemoryCredentialBackend


class ArchiveMutationReadbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def pump(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents(); time.sleep(.002)
        self.assertTrue(predicate())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="archive-readback-review-")
        root = Path(self.temp.name)
        self.store = ConversationStore(root / "archive.sqlite3")
        self.cid = str(uuid4())
        self.store.append_message(self.cid, "user", "OWNED_ORIGINAL", "user", message_id=str(uuid4()))
        settings = isolated_settings(root / "settings.ini", MemoryCredentialBackend())
        with patch("desktop_pet.chat_controller.capture_execution_runtime", side_effect=AssertionError("No machine execution config")), \
             patch("desktop_pet.chat_controller.capture_office_runtime", side_effect=AssertionError("No machine office config")):
            self.chat = ChatController(settings, store=self.store, metadata_client_factory=lambda: None)
        self.chat.open_conversation(self.cid)
        self.pump(lambda: not self.chat.history_loading)
        self.pump(lambda: not self.chat._runner._tasks)

    def tearDown(self):
        self.chat.shutdown()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(3000))
        self.chat.deleteLater(); self.app.processEvents()
        self.temp.cleanup()

    def settled(self):
        self.pump(lambda: self.cid not in self.chat._invalidating_conversations)
        self.pump(lambda: not self.chat._runner._tasks)

    def test_failed_clear_displays_fresh_surviving_rows_not_the_saved_ui_copy(self):
        gui = threading.get_ident(); reads = []
        actual_get, actual_load = self.store.get_conversation, self.store.load_messages
        def get(cid): reads.append(threading.get_ident()); return actual_get(cid)
        def load(cid): reads.append(threading.get_ident()); return actual_load(cid)
        def rejected(cid):
            self.store.append_message(cid, "assistant", "OWNED_NEW_DISK_ROW", "assistant", message_id=str(uuid4()))
            raise OSError("PRIVATE_ACK_PATH_MUST_NOT_BE_PROJECTED")
        with patch.object(self.store, "clear_messages", side_effect=rejected) as mutation, patch.object(self.store, "get_conversation", get), \
             patch.object(self.store, "load_messages", load):
            self.chat.clear(); self.settled()
        self.assertEqual(1, mutation.call_count)
        self.assertEqual(["OWNED_ORIGINAL", "OWNED_NEW_DISK_ROW"], [row["content"] for row in self.chat.messages])
        self.assertTrue(reads and all(thread != gui for thread in reads))
        self.assertTrue(self.chat.error)
        self.assertNotIn("PRIVATE_ACK_PATH", self.chat.error)

    def test_committed_clear_with_unreadable_archive_has_empty_unknown_ui(self):
        clear = self.store.clear_messages
        def committed(cid): clear(cid); raise OSError("PRIVATE_COMMIT_ACK_FAILURE")
        with patch.object(self.store, "clear_messages", committed), \
             patch.object(self.store, "get_conversation", side_effect=OSError("PRIVATE_READBACK_FAILURE")):
            self.chat.clear(); self.settled()
        self.assertEqual([], self.store.load_messages(self.cid))
        self.assertEqual([], self.chat.messages)
        self.assertIsNone(self.chat._history_restore_view)
        self.assertTrue(self.chat.error)
        self.assertNotIn("PRIVATE", self.chat.error)
        self.assertNotIn(self.cid, self.chat._retry_messages)

    def test_unreadable_delete_keeps_conservative_deleted_guard_and_no_old_rows(self):
        delete = self.store.delete_conversation
        def committed(cid): delete(cid); raise OSError("PRIVATE_DELETE_ACK_FAILURE")
        with patch.object(self.store, "delete_conversation", committed), \
             patch.object(self.store, "load_messages", side_effect=OSError("PRIVATE_READBACK_FAILURE")):
            self.assertTrue(self.chat.delete_conversation(self.cid)); self.settled()
        self.assertIsNone(self.store.get_conversation(self.cid))
        self.assertEqual([], self.chat.messages)
        self.assertIn(self.cid, self.chat._deleted_conversations)
        self.assertTrue(self.chat.error)

    def test_rejected_clear_with_failed_readback_stays_empty_until_explicit_read(self):
        with patch.object(self.store, "clear_messages", side_effect=OSError("PRIVATE_PRECOMMIT_FAILURE")), \
             patch.object(self.store, "get_conversation", side_effect=OSError("PRIVATE_READBACK_FAILURE")):
            self.chat.clear(); self.settled()
        self.assertEqual(["OWNED_ORIGINAL"], [row["content"] for row in self.store.load_messages(self.cid)])
        self.assertEqual([], self.chat.messages)
        self.assertTrue(self.chat.error)
        self.assertTrue(self.chat.reload_history(self.cid))
        self.pump(lambda: not self.chat.history_loading)
        self.assertEqual(["OWNED_ORIGINAL"], [row["content"] for row in self.chat.messages])

    def test_delete_rejected_before_commit_restores_only_actual_surviving_view(self):
        with patch.object(self.store, "delete_conversation", side_effect=OSError("PRIVATE_PRECOMMIT_FAILURE")):
            self.assertTrue(self.chat.delete_conversation(self.cid)); self.settled()
        self.assertEqual(["OWNED_ORIGINAL"], [row["content"] for row in self.chat.messages])
        self.assertNotIn(self.cid, self.chat._deleted_conversations)
        self.assertTrue(self.chat.error)

    def test_confirmed_clear_invalidates_precommit_same_cid_history_snapshot(self):
        clear_entered, clear_release, read_entered, read_release = (threading.Event() for _ in range(4))
        clear, load = self.store.clear_messages, self.store.load_messages
        def held_clear(cid):
            clear_entered.set()
            if not clear_release.wait(3): raise RuntimeError("Owned clear fixture not released")
            return clear(cid)
        def held_read(cid):
            rows = load(cid)
            read_entered.set()
            if not read_release.wait(3): raise RuntimeError("Owned history fixture not released")
            return rows
        with patch.object(self.store, "clear_messages", held_clear), patch.object(self.store, "load_messages", held_read):
            try:
                self.chat.clear(); self.assertTrue(clear_entered.wait(2))
                self.assertTrue(self.chat.open_conversation(self.cid)); self.assertTrue(read_entered.wait(2))
                clear_release.set()
                self.pump(lambda: self.cid not in self.chat._invalidating_conversations)
                read_release.set(); self.pump(lambda: not self.chat.history_loading)
                self.pump(lambda: not self.chat._runner._tasks)
            finally: clear_release.set(); read_release.set()
        self.assertEqual([], self.store.load_messages(self.cid))
        self.assertEqual([], self.chat.messages, "A late successful read cannot re-install pre-clear rows")


if __name__ == "__main__": unittest.main()
