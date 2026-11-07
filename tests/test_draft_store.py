"""Local text draft CAS, isolated temporary storage; no user data/settings."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet import draft_store as subject


class LocalDraftStoreTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="aura-local-draft-store-")
        self.addCleanup(self.owned.cleanup)
        self.path = Path(self.owned.name)/"archive.drafts.sqlite3"
        self.store = subject.LocalDraftStore(self.path)
        self.cid = str(uuid4())

    def sample(self, text="未发送草稿\n  保留空格。", **kwargs):
        return subject.new_draft(self.cid, text, clock=lambda: 1234.5, **kwargs)

    def test_construction_and_missing_load_do_not_create_a_database_or_thread(self):
        before = {thread.ident for thread in threading.enumerate()}
        with patch.object(subject.sqlite3, "connect", side_effect=AssertionError("No construction I/O")):
            fresh = subject.LocalDraftStore(self.path)
            self.assertEqual((), fresh.load())
            self.assertIsNone(fresh.lookup(self.cid))
        self.assertFalse(self.path.exists())
        self.assertEqual(before, {thread.ident for thread in threading.enumerate()})

    def test_exact_unicode_whitespace_roundtrip_is_not_a_chat_message_or_attachment(self):
        draft = self.sample()
        self.assertTrue(self.store.save(draft))
        self.assertEqual((draft,), subject.LocalDraftStore(self.path).load())
        with closing(sqlite3.connect(self.path)) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in connection.execute("PRAGMA table_info(drafts)")}
        self.assertEqual({"draft_store_meta", "drafts"}, names)
        self.assertEqual({"conversation_id", "epoch", "revision", "generation", "text", "updated_at"}, columns)

    def test_late_old_revision_and_same_revision_different_text_do_not_overwrite(self):
        old = self.sample()
        self.assertTrue(self.store.save(old))
        fresh = replace(old, revision=2, text="更新的草稿")
        self.assertTrue(self.store.save(fresh))
        self.assertFalse(self.store.save(old))
        self.assertFalse(self.store.save(replace(fresh, text="同版本冲突")))
        self.assertTrue(self.store.save(fresh))  # Idempotent exact retry is not a message send.
        self.assertEqual(fresh, self.store.lookup(self.cid))

    def test_two_independent_connections_cas_never_replace_higher_revision(self):
        old = self.sample(); self.assertTrue(self.store.save(old))
        start = threading.Barrier(2)
        def write(revision):
            start.wait(timeout=3)
            return subject.LocalDraftStore(self.path).save(replace(old, revision=revision, text=str(revision)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            low = pool.submit(write, 2); high = pool.submit(write, 3)
            low.result(timeout=4); self.assertTrue(high.result(timeout=4))
        self.assertEqual(3, self.store.lookup(self.cid).revision)

    def test_accepted_send_cleanup_cannot_clear_newer_unsent_text(self):
        sent = self.sample(); self.assertTrue(self.store.save(sent))
        fresh = replace(sent, revision=2, text="下一条尚未发送")
        self.assertTrue(self.store.save(fresh))
        cleared = self.sample("", epoch="f"*32)
        self.assertFalse(self.store.rotate(cleared, expected_epoch=sent.epoch, expected_revision=sent.revision))
        self.assertEqual(fresh, self.store.lookup(self.cid))

    def test_clear_epoch_fence_survives_restart_and_blocks_old_queued_save(self):
        old = self.sample(); self.assertTrue(self.store.save(old))
        empty = self.sample("", epoch="f"*32)
        self.assertTrue(self.store.rotate(empty, expected_epoch=old.epoch))
        restarted = subject.LocalDraftStore(self.path)
        self.assertEqual((), restarted.load())
        self.assertEqual(empty, restarted.lookup(self.cid))
        self.assertFalse(restarted.save(replace(old, revision=9999)))
        new_text = replace(empty, revision=2, text="用户明确输入的新草稿")
        self.assertTrue(restarted.save(new_text))
        self.assertEqual(new_text, restarted.lookup(self.cid))

    def test_clear_failure_restore_uses_new_epoch_and_cannot_overwrite_new_input(self):
        old = self.sample(); self.assertTrue(self.store.save(old))
        empty = self.sample("", epoch="f"*32)
        self.assertTrue(self.store.rotate(empty, expected_epoch=old.epoch))
        restored = replace(empty, revision=2, text=old.text)
        self.assertTrue(self.store.save(restored))
        self.assertFalse(self.store.save(old))
        newer = replace(empty, revision=3, text="回滚期间新输入")
        self.assertTrue(self.store.save(newer))
        self.assertFalse(self.store.save(restored))

    def test_generation_change_needs_explicit_host_epoch_rotation(self):
        old = self.sample(generation=str(uuid4())); self.assertTrue(self.store.save(old))
        new_generation = str(uuid4())
        self.assertFalse(self.store.save(replace(old, generation=new_generation, revision=2)))
        fresh = self.sample("新一代本地草稿", generation=new_generation, epoch="e"*32)
        self.assertTrue(self.store.rotate(fresh, expected_epoch=old.epoch))
        self.assertFalse(self.store.save(replace(old, revision=999)))
        self.assertEqual(fresh, self.store.lookup(self.cid))

    def test_cross_conversation_old_epoch_cannot_remove_other_draft(self):
        first = self.sample(); self.assertTrue(self.store.save(first))
        other = subject.new_draft(str(uuid4()), "另一个会话", clock=lambda: 1235.)
        self.assertTrue(self.store.save(other))
        attempted = replace(other, epoch="f"*32, text="")
        self.assertFalse(self.store.rotate(attempted, expected_epoch=first.epoch))
        self.assertEqual(other, self.store.lookup(other.conversation_id))

    def test_first_accepted_message_anchor_bind_is_explicit_and_cannot_change_established_generation(self):
        empty_anchor = self.sample(); self.assertTrue(self.store.save(empty_anchor))
        anchored = replace(empty_anchor, revision=2, generation=str(uuid4()), text="下一条草稿")
        self.assertFalse(self.store.save(anchored))
        self.assertTrue(self.store.save(anchored, expected_empty_generation=True))
        self.assertFalse(self.store.save(replace(anchored, revision=3, generation=str(uuid4())),
                                         expected_empty_generation=True))
        self.assertFalse(self.store.save(replace(empty_anchor, revision=999)))
        self.assertEqual(anchored, self.store.lookup(self.cid))

    def test_absent_record_rotation_requires_absence_not_an_unrelated_old_ticket(self):
        fresh = self.sample("")
        self.assertFalse(self.store.rotate(fresh, expected_epoch="f"*32))
        self.assertTrue(self.store.rotate(fresh, expected_epoch=None))

    def test_text_total_and_live_count_limits_fail_atomically(self):
        first = self.sample("A"*100); self.assertTrue(self.store.save(first))
        with patch.object(subject, "MAX_TOTAL_TEXT_BYTES", 150):
            with self.assertRaisesRegex(subject.DraftStorageError, "DRAFT_STORAGE_LIMIT"):
                self.store.save(replace(first, revision=2, text="B"*151))
        self.assertEqual(first, self.store.lookup(self.cid))
        with patch.object(subject, "MAX_LIVE_DRAFTS", 1):
            with self.assertRaisesRegex(subject.DraftStorageError, "DRAFT_STORAGE_LIMIT"):
                self.store.save(subject.new_draft(str(uuid4()), "Another draft"))
        self.assertEqual((first,), self.store.load())

    def test_large_invalid_and_control_character_inputs_never_create_storage(self):
        good = self.sample()
        for value in (replace(good, text="字"*(subject.MAX_TEXT_BYTES//3+1)),
                      replace(good, conversation_id=self.cid+"\0"), replace(good, epoch=""),
                      replace(good, revision=True), replace(good, generation="invalid\nidentity"),
                      replace(good, updated_at=float("inf")), replace(good, text="\ud800")):
            with self.subTest(field=str(type(value))), self.assertRaises(subject.DraftStorageError):
                self.store.save(value)
        self.assertFalse(self.path.exists())

    def test_existing_archive_legacy_identity_is_kept_exact_without_uuid_aliasing(self):
        value = subject.new_draft("legacy-conversation", "已有历史会话的草稿", generation="first-message")
        self.assertTrue(self.store.save(value))
        self.assertEqual(value, self.store.lookup("legacy-conversation"))
        self.assertIsNone(self.store.lookup("LEGACY-CONVERSATION"))

    def test_unrelated_sqlite_database_is_not_adopted_or_modified(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE conversations (id TEXT)")
            connection.execute("INSERT INTO conversations VALUES ('SYNTHETIC_PRIVATE_ORIGINAL')")
            connection.commit()
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(subject.DraftStorageError, "DRAFT_STORAGE_UNAVAILABLE") as caught:
            self.store.save(self.sample())
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(digest, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_lock_contention_is_bounded_and_error_does_not_disclose_path_or_text(self):
        original = self.sample(); self.assertTrue(self.store.save(original))
        blocker = sqlite3.connect(self.path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        begun = time.monotonic()
        try:
            with self.assertRaisesRegex(subject.DraftStorageError, "DRAFT_STORAGE_UNAVAILABLE") as caught:
                self.store.save(replace(original, revision=2, text="SYNTHETIC_PRIVATE_UNSAVED"))
        finally: blocker.close()
        self.assertLess(time.monotonic()-begun, 1.5)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertNotIn(str(self.path), str(caught.exception))
        self.assertEqual(original, self.store.lookup(self.cid))

    def test_hardlinked_store_is_not_opened_for_draft_write(self):
        original = self.sample(); self.assertTrue(self.store.save(original))
        target = self.path.with_name("owned-link.sqlite3")
        try: os.link(self.path, target)
        except OSError as error: self.skipTest("Controlled temp hardlink unavailable: "+type(error).__name__)
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(subject.DraftStorageError, "DRAFT_STORAGE_UNAVAILABLE"):
            self.store.save(replace(original, revision=2, text="Do not write through a link"))
        self.assertEqual(digest, hashlib.sha256(self.path.read_bytes()).hexdigest())


if __name__ == "__main__": unittest.main()
