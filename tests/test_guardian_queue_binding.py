"""Pure temporary SQLite run bindings. No process/model closure is claimed."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import job_queue as queue
from desktop_pet.agents.job_guardian import canonical, new_manifest


class GuardianQueueBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-guardian-binding-owned-")
        self.path = Path(self.temp.name) / "queue.db"
        self.queue = queue.JobQueue(self.path)
        self.queue.initialize()

    def tearDown(self): self.temp.cleanup()

    def start(self):
        reference = queue.HostJobReference(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), "a" * 64, "b" * 64)
        job = self.queue.enqueue(reference)
        claim = self.queue.claim()
        decision = self.queue.begin_dispatch(claim.lease, revalidate=lambda row: queue.HostDispatchScope(
            row.conversation_id, row.generation, row.settings_scope, True, row.origin_peer_id))
        self.assertTrue(decision.accepted)
        manifest = new_manifest(job_id=job.job_id, conversation_id=reference.conversation_id,
            generation=reference.generation, operation_id=reference.idempotency_key,
            user_message_id=reference.user_message_id, settings_scope=reference.settings_scope,
            origin_peer_id=reference.origin_peer_id, installation_id="c" * 64, launch_epoch="d" * 64,
            process_creation_time=12345)
        return job, claim.lease, reference, manifest

    def test_binding_is_full_immutable_identity_committed_once_and_idempotent(self):
        job, lease, reference, manifest = self.start()
        binding = self.queue.bind_guardian_run(lease, manifest)
        self.assertIsInstance(binding, queue.GuardianRunBinding)
        self.assertEqual(binding.reference, reference)
        self.assertEqual(binding.job_id, job.job_id)
        self.assertEqual(binding.manifest_sha256, hashlib.sha256(canonical(manifest)).hexdigest())
        self.assertTrue(binding.matches_manifest(manifest))
        again = self.queue.bind_guardian_run(lease, manifest)
        self.assertEqual(again.manifest_sha256, binding.manifest_sha256)
        with self.assertRaises(queue.JobQueueError):
            self.queue.bind_guardian_run(lease, {**manifest, "run_nonce": "e" * 64})
        self.assertTrue(self.queue.guardian_binding(job.job_id).matches_manifest(manifest))
        with self.assertRaises(TypeError): queue.GuardianRunBinding()
        with self.assertRaises(AttributeError): binding._run_nonce = "f" * 64

    def test_each_reference_mismatch_rejected_without_persisting_any_binding(self):
        job, lease, _, manifest = self.start()
        for field, value in (("job_id", str(uuid4())), ("conversation_id", str(uuid4())),
                ("generation", str(uuid4())), ("user_message_id", str(uuid4())),
                ("operation_id", str(uuid4())), ("settings_scope", "e" * 64), ("origin_peer_id", "f" * 64)):
            with self.subTest(field=field), self.assertRaises(queue.JobQueueError):
                self.queue.bind_guardian_run(lease, {**manifest, field: value})
            self.assertIsNone(self.queue.guardian_binding(job.job_id))

    def test_claimed_or_expired_lease_and_v1_manifest_cannot_bind(self):
        job, lease, _, manifest = self.start()
        old = {key: value for key, value in manifest.items() if key not in {"user_message_id", "settings_scope", "origin_peer_id"}}
        old["schema"] = "aura.host_job_manifest.v1"
        with self.assertRaises(queue.JobQueueError): self.queue.bind_guardian_run(lease, old)
        with self.assertRaises(queue.JobQueueError): self.queue.bind_guardian_run(queue.JobLease(job.job_id, "0" * 64), manifest)
        self.queue.recover_after_restart()
        with self.assertRaises(queue.JobQueueError): self.queue.bind_guardian_run(lease, manifest)

    def test_recovery_rechecks_binding_in_transaction_and_only_marks_worker_stopped(self):
        job, lease, _, manifest = self.start()
        binding = self.queue.bind_guardian_run(lease, manifest)
        self.queue.recover_after_restart()
        self.assertTrue(self.queue.has_unclosed_worker())
        proof = lambda row: queue.WorkerStoppedProof(row.job_id, row.conversation_id, row.generation, row.user_message_id, True)
        with self.assertRaises(queue.JobQueueError):
            self.queue.reconcile_worker_stopped(job.job_id, guardian_binding={"run_nonce": binding.run_nonce}, verifier=proof)
        another = queue.JobQueue(self.path); another.initialize()
        with self.assertRaises(queue.JobQueueError):
            another.reconcile_worker_stopped(job.job_id, guardian_binding=binding, verifier=proof)
        fresh = another.guardian_binding(job.job_id)
        current = another.reconcile_worker_stopped(job.job_id, guardian_binding=fresh, verifier=proof)
        self.assertEqual(current.state, "unknown")
        self.assertIsNotNone(current.worker_closed_at)
        self.assertIsNone(current.reply_id)
        self.assertFalse(another.has_unclosed_worker())
        self.assertIsNone(another.claim(), "The original task was not replayed")

    def test_post_commit_error_retains_exact_binding_for_readback_without_new_run(self):
        job, lease, _, manifest = self.start()
        original = self.queue._connection
        @contextmanager
        def ambiguous(**kwargs):
            with original(**kwargs) as db:
                yield db
            raise queue.JobQueueError("QUEUE_STORAGE_UNAVAILABLE")
        with patch.object(self.queue, "_connection", ambiguous), self.assertRaises(queue.JobQueueError):
            self.queue.bind_guardian_run(lease, manifest)
        self.assertTrue(self.queue.guardian_binding(job.job_id).matches_manifest(manifest))

    def test_v2_database_upgrade_preserves_old_unknown_without_fabricating_binding(self):
        job, _, _, _ = self.start()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE guardian_runs")
            db.execute("PRAGMA user_version=2")
        restored = queue.JobQueue(self.path); restored.initialize(); restored.recover_after_restart()
        self.assertEqual(restored.get(job.job_id).state, "unknown")
        self.assertIsNone(restored.guardian_binding(job.job_id))
        self.assertEqual(restored.unclosed_guardian_bindings(), ())
        self.assertTrue(restored.has_unclosed_worker())


if __name__ == "__main__": unittest.main()
