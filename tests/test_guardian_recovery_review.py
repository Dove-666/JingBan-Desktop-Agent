"""Independent recovery ordering with actual SQLite and synthetic proof IO.

The stores below stand in for already authenticated stopped-only evidence; this
suite does not claim to stop or observe an actual Windows worker. Native/App
crash proof is exercised separately. No model, private key or app DB is used.
"""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_pet.agents import job_guardian, job_queue
from desktop_pet.agents.contracts import AgentError, AgentRequest
from desktop_pet.agents.service import AgentService
from desktop_pet.agents.settings import AgentSettings
from tests import test_guardian_queue_review as fixtures


class GuardianRecoveryReviewTests(unittest.TestCase):
    setUp = fixtures.GuardianQueueReviewTests.setUp

    def recover(self, *, close_failure=False, commit_error=None):
        binding = self.queue.bind_guardian_run(self.claim.lease, self.manifest)
        self.queue.recover_after_restart()
        config = self.path.parent / "owned-config"; config.mkdir()
        service = AgentService(checkpoint_path=config / "checkpoints.json")
        self.addCleanup(service.shutdown)
        native = SimpleNamespace(unconfirmed_close=False)
        events = []
        location = SimpleNamespace(_path=config / "host_job_guardian", _native=native,
            _directory=SimpleNamespace(directory=0), close=lambda: events.append("location_closed"))
        manifest = self.manifest
        queue = self.queue
        class SyntheticAuthenticatedStore:
            def __init__(self, *args): pass
            def identity(self): return manifest, binding.manifest_sha256, b"synthetic-only"
            def read_closure(self, **kwargs):
                events.append("closure_authenticated")
                return SimpleNamespace(manifest_sha256=binding.manifest_sha256, run_nonce=binding.run_nonce)
            def consume_closure(self, **kwargs):
                # A separate connection sees only committed state. A marker
                # cannot acknowledge an uncommitted or missing stopped CAS.
                if queue.get(binding.job_id).worker_closed_at is None:
                    raise AssertionError("Evidence consumed before durable DB closure")
                events.append("consumed")
            def close(self):
                events.append("store_closed")
                if close_failure: native.unconfirmed_close = True
        real_commit = self.queue.reconcile_worker_stopped
        def commit(*args, **kwargs):
            if commit_error == "before": raise job_queue.JobQueueError("QUEUE_STORAGE_UNAVAILABLE")
            value = real_commit(*args, **kwargs)
            events.append("db_commit_returned")
            if commit_error == "after": raise job_queue.JobQueueError("QUEUE_STORAGE_UNAVAILABLE")
            return value
        with patch.object(service, "_guardian_storage", return_value=location), \
             patch.object(job_guardian, "PrivateRunStore", SyntheticAuthenticatedStore), \
             patch.object(self.queue, "reconcile_worker_stopped", side_effect=commit):
            service.recover_guardian_jobs(self.queue)
        return service, location, events

    def test_recovery_close_failure_retains_original_authority_after_db_stopped_commit(self):
        service, location, events = self.recover(close_failure=True)
        self.assertIsNotNone(self.queue.get(self.job.job_id).worker_closed_at)
        self.assertLess(events.index("db_commit_returned"), events.index("consumed"))
        self.assertTrue(any(owner._native is location._native for owner in service._unbound_guardian_locations),
            "Failed audited close lost its original strong native owner")
        service.reconcile_worker_closures(timeout_seconds=0)
        self.assertTrue(service._unbound_guardian_locations)
        with self.assertRaises(AgentError) as caught:
            service.execute(AgentSettings(), "synthetic-key", AgentRequest("owned fixture", "agent"),
                {"conversation_id": self.reference.conversation_id})
        self.assertEqual("busy", caught.exception.code)

    def test_failure_before_db_commit_never_consumes_the_authenticated_receipt(self):
        _, _, events = self.recover(commit_error="before")
        self.assertNotIn("consumed", events)
        self.assertIsNone(self.queue.get(self.job.job_id).worker_closed_at)

    def test_error_after_real_db_commit_preserves_stopped_state_without_consuming_or_completing(self):
        _, _, events = self.recover(commit_error="after")
        self.assertNotIn("consumed", events)
        stopped = self.queue.get(self.job.job_id)
        self.assertIsNotNone(stopped.worker_closed_at)
        self.assertNotEqual("completed", stopped.state)
        self.assertIsNone(stopped.reply_id)


if __name__ == "__main__": unittest.main()
