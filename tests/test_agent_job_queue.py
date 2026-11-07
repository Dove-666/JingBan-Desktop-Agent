"""Pure host ledger tests. No workers, prompts, models or application DBs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import importlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import job_queue as queue


PRIVATE = "Authorization Bearer sk-SYNTHETIC_SECRET C:\\Users\\PrivateName\\RealDocumentTitle.docx"


class Clock:
    def __init__(self): self.value = 1000.0; self.lock = threading.Lock()
    def __call__(self):
        with self.lock: return self.value
    def advance(self, value):
        with self.lock: self.value += value


class AgentJobQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-job-queue-test-")
        self.path = Path(self.directory.name) / "queue.sqlite3"
        self.clock = Clock(); self.queue = queue.JobQueue(self.path, clock=self.clock); self.queue.initialize()

    def tearDown(self): self.directory.cleanup()

    def reference(self, **values):
        data = dict(conversation_id=str(uuid4()), generation=str(uuid4()), user_message_id=str(uuid4()),
                    idempotency_key=str(uuid4()), settings_scope="a" * 64)
        data.update(values); return queue.HostJobReference(**data)

    @staticmethod
    def revalidate(job):
        return queue.HostDispatchScope(job.conversation_id, job.generation, job.settings_scope, True)

    def start(self, reference=None, ttl=30):
        job = self.queue.enqueue(reference or self.reference()); claimed = self.queue.claim(ttl_seconds=ttl)
        self.assertEqual(claimed.job.job_id, job.job_id)
        decision = self.queue.begin_dispatch(claimed.lease, revalidate=self.revalidate)
        self.assertTrue(decision.accepted)
        return decision.job, claimed.lease

    @staticmethod
    def saved(job, reply_id):
        return queue.SavedReplyProof(job.job_id, job.conversation_id, job.generation, job.user_message_id,
            reply_id, True, True, ("artifact_" + "f" * 64,))

    @staticmethod
    def stopped(job):
        return queue.WorkerStoppedProof(job.job_id, job.conversation_id, job.generation, job.user_message_id, True)

    def test_import_and_construction_do_not_open_database_or_start_threads(self):
        with patch("sqlite3.connect", side_effect=AssertionError("No IO on import/construction")), \
             patch("threading.Thread.start", side_effect=AssertionError("No scheduler thread")):
            importlib.reload(queue)
            unopened = queue.JobQueue(Path(self.directory.name) / "not-created.sqlite3")
        self.assertFalse((Path(self.directory.name) / "not-created.sqlite3").exists())
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_NOT_INITIALIZED"):
            unopened.list_jobs()

    def test_enqueue_is_idempotent_even_after_terminal_and_does_not_replace_settings_snapshot(self):
        reference = self.reference(); first = self.queue.enqueue(reference)
        self.assertEqual(first, self.queue.enqueue(reference))
        changed = self.queue.enqueue(replace(reference, settings_scope="b" * 64))
        self.assertEqual(changed.job_id, first.job_id); self.assertEqual(changed.settings_scope, "a" * 64)
        self.queue.request_cancel(first.job_id)
        self.assertEqual(self.queue.enqueue(reference).state, "cancelled")
        self.assertIsNone(self.queue.claim())
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_IDEMPOTENCY_CONFLICT"):
            self.queue.enqueue(replace(reference, conversation_id=str(uuid4())))
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_MESSAGE_ALREADY_QUEUED"):
            self.queue.enqueue(replace(reference, idempotency_key=str(uuid4())))

    def test_authenticated_origin_cannot_be_changed_by_retry_or_dispatch_scope(self):
        reference = self.reference(origin_peer_id="b" * 64)
        job = self.queue.enqueue(reference)
        self.assertEqual(job.origin_peer_id, "b" * 64)
        self.assertEqual(self.queue.enqueue(reference), job)
        for peer in (None, "c" * 64):
            with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_IDEMPOTENCY_CONFLICT"):
                self.queue.enqueue(replace(reference, origin_peer_id=peer))
        lease = self.queue.claim().lease
        # A generic local authorized=True does not prove the paired origin is
        # still authorized. The host must explicitly bind its fresh recheck.
        decision = self.queue.begin_dispatch(lease, revalidate=self.revalidate)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "QUEUE_SCOPE_RECHECK_FAILED")
        reference = self.reference(origin_peer_id="b" * 64)
        self.queue.enqueue(reference)
        lease = self.queue.claim().lease
        decision = self.queue.begin_dispatch(lease, revalidate=lambda row: queue.HostDispatchScope(
            row.conversation_id, row.generation, row.settings_scope, True, "b" * 64))
        self.assertTrue(decision.accepted)

    def test_invalid_remote_origin_rejected_before_persistence(self):
        for peer in ("", "B" * 64, "c" * 63, [], True, PRIVATE):
            with self.subTest(peer_type=type(peer).__name__), self.assertRaisesRegex(queue.JobQueueError, "QUEUE_INVALID_REFERENCE"):
                self.queue.enqueue(self.reference(origin_peer_id=peer))
        self.assertEqual(self.queue.list_jobs(), ())

    def test_lookup_preserves_terminal_identity_and_never_enqueues_or_resumes(self):
        reference = self.reference(origin_peer_id="b" * 64)
        self.assertIsNone(self.queue.lookup(reference.idempotency_key))
        self.assertIsNone(self.queue.lookup_message(reference.conversation_id, reference.user_message_id))
        job = self.queue.enqueue(reference)
        self.assertEqual(self.queue.lookup(reference.idempotency_key), job)
        self.assertEqual(self.queue.lookup_message(reference.conversation_id, reference.user_message_id), job)
        self.queue.request_cancel(job.job_id)
        self.assertEqual(self.queue.lookup(reference.idempotency_key).state, "cancelled")
        self.assertEqual(self.queue.lookup_message(reference.conversation_id, reference.user_message_id).state, "cancelled")
        self.assertIsNone(self.queue.lookup_message(str(uuid4()), reference.user_message_id))
        self.assertIsNone(self.queue.claim())
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_INVALID_REFERENCE"):
            self.queue.lookup("not a UUID")

    def test_global_recovery_barrier_does_not_depend_on_paginated_terminal_history(self):
        for _ in range(505):
            job = self.queue.enqueue(self.reference())
            self.queue.request_cancel(job.job_id)
        self.assertFalse(self.queue.has_unclosed_worker())
        job, _ = self.start()
        self.queue.recover_after_restart()
        self.assertEqual(self.queue.get(job.job_id).state, "unknown")
        self.assertTrue(self.queue.has_unclosed_worker())
        self.queue.reconcile_worker_stopped(job.job_id, verifier=self.stopped)
        self.assertFalse(self.queue.has_unclosed_worker())

    def test_bulk_queued_cancel_changes_all_matching_rows_once_and_keeps_active_other_cid(self):
        active, lease = self.start()
        first = self.queue.enqueue(self.reference(conversation_id=active.conversation_id))
        second = self.queue.enqueue(self.reference(conversation_id=active.conversation_id))
        other = self.queue.enqueue(self.reference())
        changed = self.queue.cancel_queued(active.conversation_id)
        self.assertEqual([row.job_id for row in changed], [first.job_id, second.job_id])
        self.assertTrue(all(row.state == "cancelled" and row.worker_closed_at is not None for row in changed))
        self.assertEqual(self.queue.get(active.job_id).state, "running")
        self.assertEqual(self.queue.get(other.job_id).state, "queued")
        self.assertEqual(self.queue.cancel_queued(active.conversation_id), ())
        self.assertEqual(self.queue.heartbeat(lease).state, "running")

    def test_v1_migration_preserves_reference_and_unknown_worker_without_inventing_peer(self):
        job, lease = self.start()
        # The pre-provenance schema is otherwise identical. Its active lease
        # and exact unresolved state must survive this additive migration.
        with sqlite3.connect(self.path) as db:
            old = db.execute("SELECT state,lease_hash,lease_expires_at FROM jobs").fetchone()
            db.execute("DROP TABLE guardian_runs")  # Absent in the real v1 schema.
            db.execute("ALTER TABLE jobs DROP COLUMN origin_peer_id")
            db.execute("PRAGMA user_version=1")
        db.close()
        upgraded = queue.JobQueue(self.path, clock=self.clock)
        upgraded.initialize()
        row = upgraded.get(job.job_id)
        self.assertIsNone(row.origin_peer_id)
        self.assertEqual(row.state, "running")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT state,lease_hash,lease_expires_at FROM jobs").fetchone(), old)
        db.close()
        upgraded.recover_after_restart()
        self.assertEqual(upgraded.get(job.job_id).state, "unknown")
        self.assertIsNone(upgraded.claim())
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_LEASE_MISMATCH"):
            upgraded.heartbeat(lease)

    def test_concurrent_claim_has_one_winner_and_stores_only_hashed_lease(self):
        job = self.queue.enqueue(self.reference()); barrier = threading.Barrier(8)
        def claim(_): barrier.wait(); return self.queue.claim()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        winners = [value for value in results if value is not None]
        self.assertEqual(len(winners), 1); winner = winners[0]
        self.assertEqual(winner.job.job_id, job.job_id)
        with sqlite3.connect(self.path) as db:
            digest = db.execute("SELECT lease_hash FROM jobs").fetchone()[0]
        self.assertEqual(digest, hashlib.sha256(winner.lease.token.encode()).hexdigest())
        self.assertNotIn(winner.lease.token, self.path.read_bytes().decode("latin1"))
        self.assertNotIn(winner.lease.token, repr(winner.lease))

    def test_fifo_uses_insertion_order_when_clock_has_equal_or_backward_values(self):
        first = self.queue.enqueue(self.reference())
        self.clock.advance(-10)
        second = self.queue.enqueue(self.reference())
        self.assertEqual([job.job_id for job in self.queue.list_jobs()], [first.job_id, second.job_id])
        self.assertEqual(self.queue.claim().job.job_id, first.job_id)

    def test_dispatch_rechecks_host_generation_permissions_and_settings_before_running(self):
        for change, reason in ((dict(generation=str(uuid4())), "GENERATION_CHANGED"),
            (dict(settings_scope="b" * 64), "SETTINGS_CHANGED"), (dict(authorized=False), "SCOPE_RECHECK_FAILED")):
            with self.subTest(reason=reason):
                job = self.queue.enqueue(self.reference()); claimed = self.queue.claim()
                decision = self.queue.begin_dispatch(claimed.lease, revalidate=lambda row: replace(self.revalidate(row), **change))
                self.assertFalse(decision.accepted); self.assertEqual(decision.job.state, "blocked")
                self.assertEqual(decision.job.reason_code, reason)
                self.assertIsNone(decision.job.started_at)

    def test_dispatch_callback_cannot_outlive_lease_and_still_authorize_launch(self):
        job = self.queue.enqueue(self.reference()); claim = self.queue.claim(ttl_seconds=1)
        def slow(job): self.clock.advance(2); return self.revalidate(job)
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_LEASE_EXPIRED"):
            self.queue.begin_dispatch(claim.lease, revalidate=slow)
        self.assertEqual(self.queue.get(job.job_id).state, "claimed")
        self.queue.recover_expired()
        self.assertEqual(self.queue.get(job.job_id).state, "interrupted")
        self.assertIsNone(self.queue.claim())

    def test_expired_running_lease_becomes_unknown_and_blocks_new_work_until_host_stop_proof(self):
        job, lease = self.start(ttl=1)
        next_job = self.queue.enqueue(self.reference()); self.clock.advance(2)
        self.assertEqual(self.queue.recover_expired(), 1)
        self.assertEqual(self.queue.get(job.job_id).state, "unknown")
        self.assertIsNone(self.queue.claim())
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_LEASE_MISMATCH"):
            self.queue.host_acknowledge_stopped(lease, outcome="cancelled", worker_stopped=True)
        self.queue.reconcile_worker_stopped(job.job_id, verifier=self.stopped)
        self.assertEqual(self.queue.claim().job.job_id, next_job.job_id)

    def test_restart_recovers_claimed_running_and_awaiting_without_automatic_replay(self):
        for phase in ("claimed", "running", "awaiting_user"):
            path = Path(self.directory.name) / (phase + ".sqlite3")
            ledger = queue.JobQueue(path, clock=self.clock); ledger.initialize()
            reference = self.reference(); job = ledger.enqueue(reference); claim = ledger.claim()
            if phase != "claimed": ledger.begin_dispatch(claim.lease, revalidate=self.revalidate)
            if phase == "awaiting_user": ledger.set_awaiting_user(claim.lease)
            restarted = queue.JobQueue(path, clock=self.clock); restarted.initialize()
            self.assertEqual(restarted.recover_after_restart(), 1)
            self.assertEqual(restarted.get(job.job_id).state, "interrupted" if phase == "claimed" else "unknown")
            self.assertIsNone(restarted.claim())
            self.assertEqual(restarted.enqueue(reference).job_id, job.job_id)
            with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_LEASE_MISMATCH"):
                restarted.heartbeat(claim.lease)

    def test_running_cancel_remains_requested_until_worker_has_really_closed(self):
        job, lease = self.start()
        self.queue.set_awaiting_user(lease)
        requested = self.queue.request_cancel(job.job_id)
        self.assertEqual(requested.state, "cancel_requested"); self.assertIsNone(requested.worker_closed_at)
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_WORKER_STOP_UNCONFIRMED"):
            self.queue.host_acknowledge_stopped(lease, outcome="cancelled", worker_stopped=False)
        self.assertEqual(self.queue.get(job.job_id).state, "cancel_requested")
        stopped = self.queue.host_acknowledge_stopped(lease, outcome="cancelled", worker_stopped=True)
        self.assertEqual(stopped.state, "cancelled"); self.assertIsNotNone(stopped.worker_closed_at)

    def test_cancel_dispatch_race_has_no_cancelled_then_running_transition(self):
        job = self.queue.enqueue(self.reference()); claimed = self.queue.claim(); barrier = threading.Barrier(2)
        def dispatch():
            barrier.wait()
            try: return self.queue.begin_dispatch(claimed.lease, revalidate=self.revalidate)
            except queue.JobQueueError as error: return str(error)
        def cancel(): barrier.wait(); return self.queue.request_cancel(job.job_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(dispatch), pool.submit(cancel)
            first.result(); second.result()
        self.assertEqual(self.queue.get(job.job_id).state, "cancel_requested")
        self.assertIsNone(self.queue.get(job.job_id).worker_closed_at)

    def test_completion_needs_saved_complete_bound_reply_and_worker_closed_proof(self):
        job, lease = self.start(); reply = str(uuid4())
        for proof in (None, replace(self.saved(job, reply), complete=False), replace(self.saved(job, reply), worker_closed=False),
            replace(self.saved(job, reply), conversation_id=str(uuid4())), replace(self.saved(job, reply), user_message_id=str(uuid4()))):
            with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_PROOF_REJECTED"):
                self.queue.reconcile_completed(job.job_id, reply, lease=lease, verifier=lambda *_: proof)
        finished = self.queue.reconcile_completed(job.job_id, reply, lease=lease, verifier=self.saved)
        self.assertEqual(finished.state, "completed"); self.assertEqual(finished.reply_id, reply)
        self.assertEqual(finished.artifact_refs, ("artifact_" + "f" * 64,))
        self.assertEqual(self.queue.reconcile_completed(job.job_id, reply, verifier=lambda *_: None), finished)

    def test_completion_verifier_expiration_rejects_old_lease_and_explicit_host_reconciliation_can_recover_saved_reply(self):
        job, lease = self.start(ttl=1); reply = str(uuid4())
        def slow(job, reply): self.clock.advance(2); return self.saved(job, reply)
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_LEASE_EXPIRED"):
            self.queue.reconcile_completed(job.job_id, reply, lease=lease, verifier=slow)
        self.queue.recover_expired()
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_LEASE_MISMATCH"):
            self.queue.reconcile_completed(job.job_id, reply, lease=lease, verifier=self.saved)
        self.assertEqual(self.queue.reconcile_completed(job.job_id, reply, verifier=self.saved).state, "completed")

    def test_foreign_or_old_lease_cannot_heartbeat_close_or_complete(self):
        job, lease = self.start(); wrong = queue.JobLease(job.job_id, "f" * 64)
        for action in (lambda: self.queue.heartbeat(wrong),
            lambda: self.queue.host_acknowledge_stopped(wrong, outcome="failed", worker_stopped=True),
            lambda: self.queue.reconcile_completed(job.job_id, str(uuid4()), lease=wrong, verifier=self.saved)):
            with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_LEASE_MISMATCH"): action()
        self.assertEqual(self.queue.get(job.job_id).state, "running")

    def test_conversation_generation_clear_revokes_pending_and_requests_running_cancel(self):
        reference = self.reference(); running, lease = self.start(reference)
        queued = self.queue.enqueue(self.reference(conversation_id=reference.conversation_id, generation=reference.generation))
        self.queue.revoke_conversation(reference.conversation_id, generation=reference.generation)
        self.assertEqual(self.queue.get(running.job_id).state, "cancel_requested")
        self.assertEqual(self.queue.get(queued.job_id).state, "cancelled")
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_SCOPE_REVOKED"):
            self.queue.enqueue(self.reference(conversation_id=reference.conversation_id, generation=reference.generation))
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_SCOPE_REVOKED"):
            self.queue.reconcile_completed(running.job_id, str(uuid4()), lease=lease, verifier=self.saved)
        new = self.queue.enqueue(self.reference(conversation_id=reference.conversation_id))
        self.assertIsNone(self.queue.claim())
        self.queue.host_acknowledge_stopped(lease, outcome="cancelled", worker_stopped=True)
        self.assertEqual(self.queue.claim().job.job_id, new.job_id)

    def test_full_conversation_revoke_also_blocks_future_generations(self):
        reference = self.reference(); self.queue.enqueue(reference)
        self.queue.revoke_conversation(reference.conversation_id)
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_SCOPE_REVOKED"):
            self.queue.enqueue(self.reference(conversation_id=reference.conversation_id))

    def test_content_paths_credentials_raw_errors_and_lease_tokens_are_never_persisted(self):
        for field in ("conversation_id", "generation", "user_message_id", "idempotency_key", "settings_scope"):
            with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_INVALID_REFERENCE"):
                self.queue.enqueue(replace(self.reference(), **{field: PRIVATE}))
        job, lease = self.start()
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_WORKER_STOP_UNCONFIRMED"):
            self.queue.host_acknowledge_stopped(lease, outcome=PRIVATE, worker_stopped=True)
        reply = str(uuid4())
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_INVALID_ARTIFACT_REFERENCE"):
            self.queue.reconcile_completed(job.job_id, reply, lease=lease, verifier=lambda row, reply: replace(self.saved(row, reply), artifact_refs=(PRIVATE,)))
        with sqlite3.connect(self.path) as db:
            dump = "\n".join(db.iterdump())
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        for forbidden in (PRIVATE, "PrivateName", "RealDocumentTitle", "SYNTHETIC_SECRET", lease.token): self.assertNotIn(forbidden, dump)
        self.assertFalse(columns & {"prompt", "content", "api_key", "path", "raw_error", "lease_token"})

    def test_existing_unrelated_database_is_not_migrated_and_errors_hide_paths(self):
        other = Path(self.directory.name) / "unrelated.sqlite3"
        with sqlite3.connect(other) as db: db.execute("CREATE TABLE user_data(value TEXT)")
        before = other.read_bytes()
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_SCHEMA_MISMATCH"):
            queue.JobQueue(other).initialize()
        self.assertEqual(other.read_bytes(), before)
        self.path.unlink()
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_STORAGE_UNAVAILABLE"):
            self.queue.list_jobs()


if __name__ == "__main__": unittest.main()
