"""Independent manager edges using root's owned actual Controller/Inbox fixture."""
from contextlib import closing
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QThreadPool
from desktop_pet.conversations.store import StoreError
from desktop_pet.image_storage import ImageStorageGateError
from tests import test_image_storage_manager as fixture
from tests.test_assistant_ui import wait_until


class IndependentImageManagerReviewTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ImageStorageManagerTests.setUpClass.__func__)
    setUp = fixture.ImageStorageManagerTests.setUp
    tearDown = fixture.ImageStorageManagerTests.tearDown
    result = fixture.ImageStorageManagerTests.result
    review = fixture.ImageStorageManagerTests.review

    def test_same_count_different_message_reference_requires_new_confirmation(self):
        cid = self.chat.current_conversation_id
        first = self.chat.store.append_message(cid, "user", "owned first message", "user",
                                               metadata={"image_id": self.image_id})
        second = self.chat.store.append_message(cid, "user", "owned other message", "user")
        ticket = self.review()
        before = self.chat.store.image_reference_summary([self.image_id])
        with closing(sqlite3.connect(self.chat.store.path)) as connection:
            with connection:
                connection.execute("UPDATE messages SET metadata='{}' WHERE id=?", (first["id"],))
                connection.execute("UPDATE messages SET metadata=? WHERE id=?",
                                   (json.dumps({"image_id": self.image_id}), second["id"]))
        self.assertEqual(self.chat.store.image_reference_summary([self.image_id]), before)
        with patch.object(self.inbox, "delete", wraps=self.inbox.delete) as delete:
            value = self.result(lambda callback: self.manager.delete(ticket, callback))
        self.assertEqual(value.get("code"), "changed")
        self.assertEqual(delete.call_count, 0)

    def test_host_shutdown_while_delete_running_retains_maintenance_until_real_io_returns(self):
        ticket = self.review()
        entered, release = threading.Event(), threading.Event()
        self.events.append(release)
        received = []
        actual_delete = self.inbox.delete
        def held(image_id):
            entered.set()
            if not release.wait(4):
                raise AssertionError("OWNED_DELETE_NOT_RELEASED")
            return actual_delete(image_id)
        with patch.object(self.inbox, "delete", held):
            self.assertTrue(self.manager.delete(ticket, lambda result, error: received.append(result)))
            wait_until(entered.is_set)
            self.chat.shutdown()
            self.assertTrue(self.chat.image_storage_gate.snapshot()["maintenance_active"])
            with self.assertRaises(ImageStorageGateError):
                self.chat.image_storage_gate.activity(self.chat.image_storage_gate.create_owner())
            self.assertEqual(received, [])
            release.set()
            self.assertTrue(QThreadPool.globalInstance().waitForDone(4000))
        self.assertFalse(self.chat.image_storage_gate.snapshot()["maintenance_active"])
        self.assertEqual(self.inbox.usage()["count"], 0)
        self.app.processEvents()
        self.assertEqual(received, [])

    def test_committed_delete_with_failed_ack_invalidates_and_cannot_replay(self):
        ticket = self.review()
        actual_delete = self.inbox.delete
        def committed_then_raise(image_id):
            actual_delete(image_id)
            raise OSError("OWNED_PRIVATE_COMMIT_ACK_DETAIL")
        with patch.object(self.inbox, "delete", side_effect=committed_then_raise) as delete:
            value = self.result(lambda callback: self.manager.delete(ticket, callback))
        self.assertEqual(value.get("code"), "delete")
        self.assertNotIn("OWNED_PRIVATE", str(value))
        self.assertEqual(delete.call_count, 1)
        self.assertEqual(self.inbox.usage()["count"], 0)
        self.assertEqual(self.chat.image_storage_epoch, 1)
        self.assertFalse(self.chat.image_storage_gate.snapshot()["maintenance_active"])
        with patch.object(self.inbox, "delete", side_effect=AssertionError("NO_REPLAY")):
            self.assertEqual(self.result(lambda callback: self.manager.delete(ticket, callback))["code"], "changed")

    def test_reference_read_failure_mints_no_ticket_and_input_failures_do_no_io(self):
        with patch.object(self.chat.store, "image_reference_summary", side_effect=StoreError("OWNED_PRIVATE_QUERY_DETAIL")), \
             patch.object(self.chat.store, "image_reference_snapshot", side_effect=StoreError("OWNED_PRIVATE_QUERY_DETAIL")):
            value = self.result(lambda callback: self.manager.review_delete(self.image_id, callback))
            page = self.result(lambda callback: self.manager.load_page(0, callback))
        self.assertEqual(value.get("code"), "storage")
        self.assertEqual(page.get("code"), "storage")
        self.assertEqual(len(self.manager._tickets), 0)
        self.assertNotIn("OWNED_PRIVATE", str((value, page)))
        with patch.object(self.inbox, "metadata", side_effect=AssertionError("NO_INVALID_METADATA_READ")), \
             patch.object(self.inbox, "delete", side_effect=AssertionError("NO_INVALID_DELETE")), \
             patch.object(self.inbox, "list", side_effect=AssertionError("NO_INVALID_LIST")):
            self.assertFalse(self.result(lambda callback: self.manager.review_delete([], callback))["ok"])
            self.assertFalse(self.result(lambda callback: self.manager.delete({}, callback))["ok"])
            self.assertFalse(self.result(lambda callback: self.manager.load_page(True, callback))["ok"])


if __name__ == "__main__":
    unittest.main()
