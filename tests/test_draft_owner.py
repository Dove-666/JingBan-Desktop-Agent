"""Shared draft owner lifecycle with controlled archive identities/temp SQLite."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet import draft_owner as subject
from desktop_pet.draft_store import LocalDraftStore, new_draft


class LocalDraftOwnerTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="aura-draft-owner-")
        self.addCleanup(self.owned.cleanup)
        self.path = Path(self.owned.name)/"drafts.sqlite3"
        self.store = LocalDraftStore(self.path)
        self.cid, self.generation = str(uuid4()), str(uuid4())
        self.identities = {self.cid: subject.ArchiveDraftIdentity(True, self.generation)}
        self.owners, self.releases = [], []
        self.addCleanup(self.close_owners)

    def close_owners(self):
        for release in self.releases: release.set()
        for owner in self.owners:
            owner.shutdown(2.)
            self.assertTrue(owner._stopped.is_set(), "A real draft writer outlived its controlled fixture")

    def reader(self, cid):
        return self.identities.get(cid, subject.ArchiveDraftIdentity(False, ""))

    def owner(self, **kwargs):
        owner = subject.LocalDraftOwner(self.store, identity_reader=kwargs.pop("identity_reader", self.reader), **kwargs)
        self.owners.append(owner)
        return owner

    def ready(self, owner): self.assertTrue(owner._loaded.wait(2.))
    def flush(self, owner): self.assertTrue(owner.wait(owner.flush(), 2.), owner.last_error)

    def test_edits_do_memory_work_only_then_flush_restores_exact_text_after_restart(self):
        owner = self.owner(); self.ready(owner)
        before = owner.snapshot(self.cid)
        with patch.object(self.store, "save", side_effect=AssertionError("No keypress I/O")), \
             patch.object(self.store, "rotate", side_effect=AssertionError("No keypress I/O")):
            updated = owner.set_text(self.cid, "中文草稿\n  尚未发送", epoch=before.epoch)
        self.assertEqual(before.revision+1, updated.revision)
        self.flush(owner); self.assertTrue(owner.shutdown(1.))
        restored = self.owner(); self.ready(restored)
        self.assertEqual("中文草稿\n  尚未发送", restored.snapshot(self.cid).text)
        self.assertEqual(self.generation, restored.snapshot(self.cid).generation)

    def test_late_disk_load_cannot_overwrite_new_user_input_or_change_its_editor_epoch(self):
        self.assertTrue(self.store.save(new_draft(self.cid, "磁盘旧草稿", generation=self.generation)))
        entered, release = threading.Event(), threading.Event(); self.releases.append(release)
        def held(cid): entered.set(); release.wait(3.); return self.reader(cid)
        owner = self.owner(identity_reader=held)
        self.assertTrue(entered.wait(2.))
        typed = owner.set_text(self.cid, "加载期间的新输入")
        release.set(); self.ready(owner)
        self.assertEqual(typed.epoch, owner.snapshot(self.cid).epoch)
        self.assertEqual(typed.text, owner.snapshot(self.cid).text)
        self.flush(owner)
        self.assertEqual(typed.text, self.store.lookup(self.cid).text)

    def test_accepted_send_clear_is_exact_revision_cas_and_rejected_send_changes_nothing(self):
        owner = self.owner(); self.ready(owner)
        sent = owner.set_text(self.cid, "本次准备发送")
        newer = owner.set_text(self.cid, "下一条尚未发送")
        self.assertFalse(owner.accepted(self.cid, epoch=sent.epoch, revision=sent.revision))
        self.assertEqual(newer, owner.snapshot(self.cid))
        # A rejected controller call simply does not call accepted().
        self.flush(owner)
        self.assertEqual(newer.text, self.store.lookup(self.cid).text)

    def test_old_debounce_job_waiting_in_identity_read_cannot_write_sent_text_after_acceptance(self):
        held = [False]
        entered, release = threading.Event(), threading.Event(); self.releases.append(release)
        def reader(cid):
            if held[0]: entered.set(); release.wait(3.)
            return self.reader(cid)
        owner = self.owner(identity_reader=reader); self.ready(owner)
        old = owner.set_text(self.cid, "已接受的旧输入")
        held[0] = True; old_write = owner.flush(); self.assertTrue(entered.wait(2.))
        self.assertTrue(owner.accepted(self.cid, epoch=old.epoch, revision=old.revision))
        release.set(); self.assertTrue(owner.wait(old_write, 2.))
        self.flush(owner)
        self.assertEqual("", self.store.lookup(self.cid).text)

    def test_first_accepted_message_identity_can_bind_without_changing_editor_epoch(self):
        self.identities[self.cid] = subject.ArchiveDraftIdentity(False, "")
        owner = self.owner(); self.ready(owner)
        sent = owner.set_text(self.cid, "首次发送")
        self.flush(owner)
        self.assertTrue(owner.accepted(self.cid, epoch=sent.epoch, revision=sent.revision))
        self.identities[self.cid] = subject.ArchiveDraftIdentity(True, self.generation)
        newer = owner.set_text(self.cid, "首次消息后新草稿")
        self.flush(owner)
        self.assertEqual(newer.epoch, owner.snapshot(self.cid).epoch)
        self.assertEqual(self.generation, self.store.lookup(self.cid).generation)
        self.assertEqual(newer.text, self.store.lookup(self.cid).text)

    def test_invalidation_barrier_is_durable_before_archive_clear_and_old_epoch_cannot_reenter(self):
        owner = self.owner(); self.ready(owner)
        old = owner.set_text(self.cid, "用户清空前的输入"); self.flush(owner)
        ticket = owner.begin_invalidation(self.cid)
        self.assertTrue(owner.wait_invalidation(ticket, 2.))
        self.assertEqual("", self.store.lookup(self.cid).text)
        self.assertIsNone(owner.set_text(self.cid, "迟到Web旧输入", epoch=old.epoch))
        self.identities[self.cid] = subject.ArchiveDraftIdentity(True, "")
        self.assertTrue(owner.finish_invalidation(ticket, success=True))
        self.flush(owner); self.assertTrue(owner.shutdown(1.))
        restored = self.owner(); self.ready(restored)
        self.assertEqual("", restored.snapshot(self.cid).text)

    def test_late_first_message_archive_commit_is_bound_on_shutdown_without_another_keypress(self):
        self.identities[self.cid] = subject.ArchiveDraftIdentity(False, "")
        owner = self.owner(); self.ready(owner)
        sent = owner.set_text(self.cid, "首次已提交但尚未入archive")
        self.flush(owner)
        self.assertTrue(owner.accepted(self.cid, epoch=sent.epoch, revision=sent.revision))
        next_draft = owner.set_text(self.cid, "马上输入的下一条草稿")
        self.flush(owner)
        self.assertEqual("", self.store.lookup(self.cid).generation)
        # Main controller has now drained its accepted write; no further edit.
        self.identities[self.cid] = subject.ArchiveDraftIdentity(True, self.generation)
        self.assertTrue(owner.shutdown(2.))
        self.assertEqual(self.generation, self.store.lookup(self.cid).generation)
        restored = self.owner(); self.ready(restored)
        self.assertEqual(next_draft.text, restored.snapshot(self.cid).text)
        self.assertEqual(next_draft.epoch, restored.snapshot(self.cid).epoch)

    def test_first_proactive_message_binds_runtime_observed_empty_anchor_without_a_user_send(self):
        self.identities[self.cid] = subject.ArchiveDraftIdentity(False, "")
        owner = self.owner(); self.ready(owner)
        draft = owner.set_text(self.cid, "主动评论出现前已经输入的草稿")
        self.flush(owner)
        self.identities[self.cid] = subject.ArchiveDraftIdentity(True, self.generation)
        self.assertTrue(owner.wait(owner.archive_changed(), 2.))
        self.assertEqual(self.generation, self.store.lookup(self.cid).generation)
        self.assertTrue(owner.shutdown(1.))
        restarted = self.owner(); self.ready(restarted)
        self.assertEqual(draft.text, restarted.snapshot(self.cid).text)
        self.assertEqual(draft.epoch, restarted.snapshot(self.cid).epoch)

    def test_failed_clear_restores_old_text_only_if_user_has_not_typed_newer_text(self):
        for newer_text in ("", "清空失败期间的新输入"):
            with self.subTest(newer_text=bool(newer_text)):
                owner = self.owner(); self.ready(owner)
                old = owner.set_text(self.cid, "清空之前保留的草稿"); self.flush(owner)
                ticket = owner.begin_invalidation(self.cid)
                self.assertTrue(owner.wait_invalidation(ticket, 2.))
                if newer_text: owner.set_text(self.cid, newer_text)
                self.assertTrue(owner.finish_invalidation(ticket, success=False))
                self.assertEqual(newer_text or old.text, owner.snapshot(self.cid).text)
                self.assertNotEqual(old.epoch, owner.snapshot(self.cid).epoch)
                self.flush(owner); self.assertTrue(owner.shutdown(1.))

    def test_failed_invalidation_never_claims_durable_clear_and_memory_can_be_restored(self):
        owner = self.owner(); self.ready(owner)
        old = owner.set_text(self.cid, "存储失败仍保留"); self.flush(owner)
        with patch.object(self.store, "rotate", side_effect=OSError("SYNTHETIC_PRIVATE_IO_FAILURE")):
            ticket = owner.begin_invalidation(self.cid)
            self.assertFalse(owner.wait_invalidation(ticket, 2.))
        self.assertTrue(owner.finish_invalidation(ticket, success=False))
        self.assertEqual(old.text, owner.snapshot(self.cid).text)
        self.flush(owner)
        self.assertEqual(old.text, self.store.lookup(self.cid).text)

    def test_failed_clear_of_unarchived_draft_restores_when_full_before_identity_is_unchanged(self):
        self.identities[self.cid] = subject.ArchiveDraftIdentity(False, "")
        owner = self.owner(); self.ready(owner)
        old = owner.set_text(self.cid, "未归档新会话的草稿"); self.flush(owner)
        ticket = owner.begin_invalidation(self.cid)
        self.assertTrue(owner.wait_invalidation(ticket, 2.))
        self.assertTrue(owner.finish_invalidation(ticket, success=False))
        self.assertEqual(old.text, owner.snapshot(self.cid).text)
        self.flush(owner)

    def test_commit_then_error_or_unreadable_identity_does_not_restore_old_text(self):
        for outcome in ("clear_committed", "delete_committed", "identity_unavailable"):
            with self.subTest(outcome=outcome):
                self.identities[self.cid] = subject.ArchiveDraftIdentity(True, self.generation)
                owner = self.owner(); self.ready(owner)
                old = owner.set_text(self.cid, "不应在已提交清空后复活的旧草稿"); self.flush(owner)
                ticket = owner.begin_invalidation(self.cid, kind="delete" if outcome == "delete_committed" else "clear")
                self.assertTrue(owner.wait_invalidation(ticket, 2.))
                if outcome == "identity_unavailable":
                    with patch.object(owner, "_identity", side_effect=OSError("PRIVATE_IDENTITY_FAILURE")):
                        self.assertFalse(owner.finish_invalidation(ticket, success=False))
                else:
                    self.identities[self.cid] = subject.ArchiveDraftIdentity(outcome == "clear_committed", "",
                                                                          outcome == "delete_committed")
                    self.assertFalse(owner.finish_invalidation(ticket, success=False))
                self.assertEqual("", owner.snapshot(self.cid).text)
                self.assertEqual("", self.store.lookup(self.cid).text)
                self.assertEqual(old.text, owner._invalidations[self.cid]._previous.text,
                                 "Uncertain rollback keeps a private memory copy, not a visible old draft")
                self.assertEqual("DRAFT_ROLLBACK_UNCONFIRMED", owner.last_error)
                self.assertTrue(owner.shutdown(1.))

    def test_generation_change_and_tombstone_never_hydrate_or_overwrite_stale_text(self):
        old = new_draft(self.cid, "旧一代输入", generation=self.generation)
        self.assertTrue(self.store.save(old))
        self.identities[self.cid] = subject.ArchiveDraftIdentity(True, str(uuid4()))
        owner = self.owner(); self.ready(owner)
        self.assertEqual("", owner.snapshot(self.cid).text)
        other = str(uuid4())
        self.assertTrue(self.store.save(new_draft(other, "已删除未归档草稿")))
        self.identities[other] = subject.ArchiveDraftIdentity(False, "", True)
        restarted = self.owner(); self.ready(restarted)
        self.assertIsNone(restarted.startup_candidate(str(uuid4())))

    def test_latest_unarchived_candidate_is_host_only_one_shot_and_can_be_revoked(self):
        first, latest = str(uuid4()), str(uuid4())
        self.assertTrue(self.store.save(new_draft(first, "第一份未归档", clock=lambda: 100.)))
        self.assertTrue(self.store.save(new_draft(latest, "最近未归档", clock=lambda: 200.)))
        owner = self.owner(); self.ready(owner)
        candidate = owner.startup_candidate(str(uuid4()))
        self.assertEqual(latest, candidate.conversation_id)
        self.assertIsNone(owner.startup_candidate(str(uuid4())))
        foreign = self.owner(); self.ready(foreign)
        self.assertFalse(foreign.activate_candidate(candidate))
        owner.revoke_candidate(latest)
        self.assertFalse(owner.activate_candidate(candidate))

    def test_new_input_even_if_erased_prevents_surprise_startup_draft_adoption(self):
        self.assertTrue(self.store.save(new_draft(str(uuid4()), "旧空白会话草稿")))
        owner = self.owner(); self.ready(owner)
        blank = str(uuid4())
        owner.set_text(blank, "用户已经开始了新会话")
        owner.set_text(blank, "")
        self.assertIsNone(owner.startup_candidate(blank))

    def test_candidate_already_offered_cannot_activate_after_new_input_was_erased(self):
        self.assertTrue(self.store.save(new_draft(str(uuid4()), "先前未发出的输入")))
        owner = self.owner(); self.ready(owner)
        blank = str(uuid4())
        candidate = owner.startup_candidate(blank)
        self.assertIsNotNone(candidate)
        owner.set_text(blank, "用户新输入")
        owner.set_text(blank, "")
        self.assertFalse(owner.activate_candidate(candidate))

    def test_candidate_is_revoked_if_conversation_became_archived_after_initial_load(self):
        old = str(uuid4())
        self.assertTrue(self.store.save(new_draft(old, "先前未发出的输入")))
        owner = self.owner(); self.ready(owner)
        candidate = owner.startup_candidate(str(uuid4()))
        self.identities[old] = subject.ArchiveDraftIdentity(True, str(uuid4()))
        self.assertTrue(owner.wait(owner.archive_changed(), 2.))
        self.assertFalse(owner.activate_candidate(candidate))

    def test_unknown_writer_join_is_bounded_and_not_reported_as_saved(self):
        held = [False]
        entered, release = threading.Event(), threading.Event(); self.releases.append(release)
        def reader(cid):
            if held[0]: entered.set(); release.wait(3.)
            return self.reader(cid)
        owner = self.owner(identity_reader=reader); self.ready(owner)
        owner.set_text(self.cid, "阻塞期间保留在内存")
        held[0] = True; owner.flush(); self.assertTrue(entered.wait(2.))
        begun = time.monotonic()
        self.assertFalse(owner.shutdown(.02))
        self.assertLess(time.monotonic()-begun, .25)
        self.assertTrue(owner._thread.is_alive())
        self.assertEqual("阻塞期间保留在内存", owner.snapshot(self.cid).text)
        release.set(); self.assertTrue(owner.shutdown(2.))

    def test_cross_owner_or_cross_ticket_cannot_confirm_other_clear(self):
        owner = self.owner(); self.ready(owner)
        ticket = owner.begin_invalidation(self.cid)
        foreign = self.owner(); self.ready(foreign)
        self.assertFalse(foreign.wait_invalidation(ticket, .01))
        self.assertFalse(foreign.finish_invalidation(ticket, success=True))

    def test_oversize_text_stays_in_memory_but_flush_truthfully_reports_failure(self):
        owner = self.owner(); self.ready(owner)
        text = "x"*(subject.MAX_TEXT_BYTES+1)
        owner.set_text(self.cid, text)
        self.assertFalse(owner.wait(owner.flush(), 2.))
        self.assertEqual(text, owner.snapshot(self.cid).text)
        self.assertEqual("DRAFT_INPUT_LIMIT", owner.last_error)


if __name__ == "__main__": unittest.main()
