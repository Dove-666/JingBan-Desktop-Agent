"""Independent SQLite/run-identity review; no worker or model is launched."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import job_queue as queue
from desktop_pet.agents.job_guardian import new_manifest


class GuardianQueueReviewTests(unittest.TestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="guardian-db-independent-")
        self.addCleanup(owned.cleanup)
        self.path = Path(owned.name) / "jobs.sqlite3"
        self.clock = [1000.0]
        self.queue = queue.JobQueue(self.path, clock=lambda: self.clock[0]); self.queue.initialize()
        self.reference = queue.HostJobReference(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), "a"*64, "b"*64)
        job = self.queue.enqueue(self.reference)
        self.claim = self.queue.claim(ttl_seconds=30)
        self.assertEqual(job.job_id, self.claim.job.job_id)
        result = self.queue.begin_dispatch(self.claim.lease, revalidate=lambda job:
            queue.HostDispatchScope(job.conversation_id, job.generation, job.settings_scope, True, job.origin_peer_id))
        self.assertTrue(result.accepted)
        self.job = result.job
        self.manifest = new_manifest(job_id=job.job_id, conversation_id=self.reference.conversation_id,
            generation=self.reference.generation, operation_id=self.reference.idempotency_key,
            user_message_id=self.reference.user_message_id, settings_scope=self.reference.settings_scope,
            origin_peer_id=self.reference.origin_peer_id, installation_id="c"*64,
            launch_epoch="d"*64, process_creation_time=12345)

    @staticmethod
    def stopped(job):
        return queue.WorkerStoppedProof(job.job_id, job.conversation_id, job.generation, job.user_message_id, True)

    def test_every_reference_field_is_bound_before_any_run_is_published(self):
        for field in ("job_id", "conversation_id", "generation", "operation_id", "user_message_id", "settings_scope", "origin_peer_id"):
            with self.subTest(field=field):
                value = "e"*64 if field in {"settings_scope", "origin_peer_id"} else str(uuid4())
                with self.assertRaises(queue.JobQueueError):
                    self.queue.bind_guardian_run(self.claim.lease, {**self.manifest, field: value})
                self.assertIsNone(self.queue.guardian_binding(self.job.job_id))

    def test_one_manifest_is_idempotent_but_new_run_or_creation_identity_cannot_replace_it(self):
        first = self.queue.bind_guardian_run(self.claim.lease, self.manifest)
        self.assertEqual(first.manifest_sha256, self.queue.bind_guardian_run(self.claim.lease, self.manifest).manifest_sha256)
        for field, value in (("run_nonce", "e"*64), ("process_creation_time", 98765), ("launch_epoch", "f"*64)):
            with self.subTest(field=field), self.assertRaisesRegex(queue.JobQueueError, "QUEUE_GUARDIAN_RUN_ALREADY_BOUND"):
                self.queue.bind_guardian_run(self.claim.lease, {**self.manifest, field: value})
        self.assertTrue(self.queue.guardian_binding(self.job.job_id).matches_manifest(self.manifest))

    def test_expired_lease_cannot_bind_even_when_reference_is_otherwise_exact(self):
        self.clock[0] += 31
        with self.assertRaises(queue.JobQueueError): self.queue.bind_guardian_run(self.claim.lease, self.manifest)
        self.assertIsNone(self.queue.guardian_binding(self.job.job_id))

    def test_restart_requires_fresh_db_opaque_binding_and_never_promotes_stopped_to_completed(self):
        old = self.queue.bind_guardian_run(self.claim.lease, self.manifest)
        restarted = queue.JobQueue(self.path, clock=lambda: self.clock[0]); restarted.initialize(); restarted.recover_after_restart()
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_GUARDIAN_BINDING_MISMATCH"):
            restarted.reconcile_worker_stopped(self.job.job_id, verifier=self.stopped, guardian_binding=old)
        self.assertIsNone(restarted.get(self.job.job_id).worker_closed_at)
        fresh = restarted.guardian_binding(self.job.job_id)
        settled = restarted.reconcile_worker_stopped(self.job.job_id, verifier=self.stopped, guardian_binding=fresh)
        self.assertIsNotNone(settled.worker_closed_at)
        self.assertNotEqual("completed", settled.state)
        self.assertIsNone(settled.reply_id)
        self.assertIsNone(restarted.claim())

    def test_error_after_real_commit_never_returns_binding_or_allows_run_identity_replacement(self):
        class AfterCommit(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("owned post-commit uncertainty")
        connect = sqlite3.connect
        def fault(*args, **kwargs): return connect(*args, **kwargs, factory=AfterCommit)
        value = None
        with patch.object(queue.sqlite3, "connect", side_effect=fault):
            with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_STORAGE_UNAVAILABLE"):
                value = self.queue.bind_guardian_run(self.claim.lease, self.manifest)
        self.assertIsNone(value)
        self.assertTrue(self.queue.guardian_binding(self.job.job_id).matches_manifest(self.manifest))
        with self.assertRaisesRegex(queue.JobQueueError, "QUEUE_GUARDIAN_RUN_ALREADY_BOUND"):
            self.queue.bind_guardian_run(self.claim.lease, {**self.manifest, "run_nonce": "e"*64})


if __name__ == "__main__": unittest.main()
