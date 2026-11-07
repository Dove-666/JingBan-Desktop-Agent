"""Independent production wiring review with synthetic state/fake native IO.

These cases do not launch a worker/model, observe OS closure or test App crash.
Producer owns all guardian/service/queue source; this file is review-only.
"""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import guardian_worker
from desktop_pet.agents import guardian_launch, job_queue, job_guardian
from desktop_pet.agents.contracts import AgentError
from desktop_pet.agents.guardian_native import GuardianError
from desktop_pet.agents.job_guardian import new_manifest
from desktop_pet.agents.service import AgentService
from tests import test_host_queue_review as queue_fixtures
from tests import test_guardian_queue_review as run_fixtures


class GuardianProductionIndependentTests(unittest.TestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="guardian-production-independent-")
        self.addCleanup(owned.cleanup)
        self.root = Path(owned.name)
        self.config = self.root / "appconfig"; self.config.mkdir()
        self.workspace = self.root / "workspace"; self.workspace.mkdir()
        self.service = AgentService(checkpoint_path=self.config / "checkpoints.json")
        self.addCleanup(self.service.shutdown)

    def test_prepare_scope_validation_close_failure_must_retain_exact_audited_owner(self):
        closed = []
        def close(handle): closed.append(handle); return 0
        base = SimpleNamespace(a=object(), c=object(), descriptor=lambda: 123,
            file_info=lambda _: SimpleNamespace(attributes=0x410), owner_only=lambda _: True,
            k=SimpleNamespace(CreateDirectoryW=lambda *_: 1, LocalFree=lambda _: None,
                CreateFileW=lambda *_: 101, CloseHandle=close))
        with patch.object(guardian_worker, "Native", return_value=base), \
             patch.object(guardian_worker._CloseAuditedNative, "descriptor", return_value=123), \
             patch.object(guardian_worker._CloseAuditedNative, "file_info", return_value=SimpleNamespace(attributes=0x410)):
            with self.assertRaisesRegex(GuardianError, "GUARDIAN_PRIVATE_SCOPE_INVALID"):
                self.service._guardian_storage(self.workspace)
        self.assertEqual([101], closed)
        self.assertTrue(self.service.guardian_resources_pending(),
            "Failed PrivateRunStore construction lost the audited close owner before a location was returned")


class GuardianLaunchAuthorityIndependentTests(unittest.TestCase):
    make_coordinator = queue_fixtures.HostQueueIndependentReviewTests.make_coordinator
    started = queue_fixtures.HostQueueIndependentReviewTests.started
    admit = queue_fixtures.HostQueueIndependentReviewTests.admit

    def setUp(self):
        queue_fixtures.HostQueueIndependentReviewTests.setUp(self)
        self.service = AgentService(checkpoint_path=self.root / "private/checkpoints.json")
        self.addCleanup(self.service.shutdown)
        self.coordinator.service = self.service
        ack = self.admit(); self.coordinator.pump()
        self.context, self.request, _ = self.starts[-1]
        self.store.set_message_status(self.cid, ack["message_id"], "pending")
        self.store.reserve_agent_execution(self.cid, ack["message_id"])
        self.attempt = self.service._execution_attempts.begin(self.cid)
        self.addCleanup(self.service._execution_attempts.finish, self.attempt)
        self.coordinator.execution_attempt_bound(self.request, self.context, self.attempt)
        self.launch = self.coordinator.guardian_launch(self.request, self.context)

    def manifest(self):
        reference = self.launch.reference
        return new_manifest(job_id=self.launch.job_id, conversation_id=reference.conversation_id,
            generation=reference.generation, operation_id=reference.idempotency_key,
            user_message_id=reference.user_message_id, settings_scope=reference.settings_scope,
            origin_peer_id=reference.origin_peer_id, installation_id="c" * 64,
            launch_epoch="d" * 64, process_creation_time=12345)

    def test_remote_capability_is_opaque_and_direct_call_cannot_mint_it(self):
        self.assertTrue(guardian_launch.valid(self.launch))
        self.assertEqual(self.peer, self.launch.reference.origin_peer_id)
        self.assertIsNone(self.coordinator.guardian_launch(self.request, None))
        for fake in ({"origin_peer_id": self.peer}, True, object()):
            self.assertFalse(guardian_launch.valid(fake))
        with self.assertRaises(TypeError): guardian_launch.HostGuardianLaunch()
        self.assertTrue(self.launch.validate_attempt(self.attempt))

    def test_foreign_or_finished_execution_attempt_cannot_use_an_old_launch_capability(self):
        foreign = self.service._execution_attempts.begin(str(uuid4()))
        self.addCleanup(self.service._execution_attempts.finish, foreign)
        with self.assertRaises(AgentError): self.launch.validate_attempt(foreign)
        self.service._execution_attempts.finish(self.attempt)
        with self.assertRaises(AgentError): self.launch.validate_attempt(self.attempt)

    def test_revoke_and_same_peer_repair_does_not_restore_old_launch_or_manifest_commit(self):
        self.preferences.revoke(self.peer)
        self.preferences.add_device(self.peer, "Synthetic repaired phone", "synthetic-time")
        with self.assertRaises(AgentError): self.launch.validate_attempt(self.attempt)
        with self.assertRaises(AgentError): self.launch.commit_manifest(self.manifest())
        self.assertIsNone(self.queue.guardian_binding(self.launch.job_id))

    def test_expired_lease_is_rechecked_without_recovery_scan_at_attempt_and_commit(self):
        self.clock.value += 31
        with self.assertRaises(AgentError): self.launch.validate_attempt(self.attempt)
        with self.assertRaises(AgentError): self.launch.commit_manifest(self.manifest())
        self.assertIsNone(self.queue.guardian_binding(self.launch.job_id))

    def test_changed_provider_scope_prevents_using_the_already_issued_capability(self):
        self.policy["key"] = "DIFFERENT_SYNTHETIC_PRIVATE_KEY"
        with self.assertRaises(AgentError): self.launch.validate_attempt(self.attempt)
        with self.assertRaises(AgentError): self.launch.commit_manifest(self.manifest())

    def test_cancel_after_commit_ack_fault_preserves_db_binding_but_rejects_resume_authority(self):
        original = self.queue.bind_guardian_run
        manifest = self.manifest()
        def committed_then_cancel(*args, **kwargs):
            original(*args, **kwargs)
            self.request["cancel"].set()
            raise job_queue.JobQueueError("QUEUE_STORAGE_UNAVAILABLE")
        with patch.object(self.queue, "bind_guardian_run", side_effect=committed_then_cancel):
            with self.assertRaises(AgentError): self.launch.commit_manifest(manifest)
        self.assertTrue(self.queue.guardian_binding(self.launch.job_id).matches_manifest(manifest))
        with self.assertRaises(AgentError): self.launch.validate_attempt(self.attempt)

    def test_lease_expiry_during_commit_ack_loss_cannot_be_rescued_by_same_run_readback(self):
        original = self.queue.bind_guardian_run
        manifest = self.manifest()
        def committed_then_expire(*args, **kwargs):
            original(*args, **kwargs)
            self.clock.value += 31
            raise job_queue.JobQueueError("QUEUE_STORAGE_UNAVAILABLE")
        with patch.object(self.queue, "bind_guardian_run", side_effect=committed_then_expire):
            with self.assertRaises(AgentError): self.launch.commit_manifest(manifest)
        self.assertTrue(self.queue.guardian_binding(self.launch.job_id).matches_manifest(manifest))

    def test_new_generation_and_message_identity_cannot_reuse_old_capability(self):
        self.store.clear_messages(self.cid)
        self.store.append_message(self.cid, "user", "New generation", "user")
        with self.assertRaises(AgentError): self.launch.validate_attempt(self.attempt)
        with self.assertRaises(AgentError): self.launch.commit_manifest(self.manifest())

    def test_other_cid_manifest_is_never_bound_by_a_valid_capability(self):
        manifest = {**self.manifest(), "conversation_id": str(uuid4())}
        with self.assertRaises(AgentError): self.launch.commit_manifest(manifest)
        self.assertIsNone(self.queue.guardian_binding(self.launch.job_id))


class GuardianRestartIdentityIndependentTests(unittest.TestCase):
    setUp = run_fixtures.GuardianQueueReviewTests.setUp

    def recover(self, *, manifest=None, receipt="valid"):
        binding = self.queue.bind_guardian_run(self.claim.lease, self.manifest)
        self.queue.recover_after_restart()
        config = self.path.parent / "owned-private"; config.mkdir()
        service = AgentService(checkpoint_path=config / "checkpoints.json")
        self.addCleanup(service.shutdown)
        location = SimpleNamespace(_path=config / "host_job_guardian",
            _native=SimpleNamespace(unconfirmed_close=False), _directory=SimpleNamespace(directory=0), close=lambda: None)
        observations = []
        current_manifest = self.manifest if manifest is None else manifest
        class SyntheticAuthenticatedStore:
            def __init__(self, *args): observations.append("opened")
            def identity(self): return current_manifest, binding.manifest_sha256, b"SYNTHETIC_ONLY"
            def read_closure(self, **kwargs):
                observations.append("read")
                if receipt == "absent": return None
                if receipt == "model_dict": return {"manifest_sha256": binding.manifest_sha256, "run_nonce": binding.run_nonce}
                return SimpleNamespace(manifest_sha256="f" * 64 if receipt == "wrong_digest" else binding.manifest_sha256,
                    run_nonce="e" * 64 if receipt == "wrong_nonce" else binding.run_nonce)
            def consume_closure(self, **kwargs): observations.append("consumed")
            def close(self): observations.append("closed")
        with patch.object(service, "_guardian_storage", return_value=location), \
             patch.object(job_guardian, "PrivateRunStore", SyntheticAuthenticatedStore):
            service.recover_guardian_jobs(self.queue)
            # Recovery is idempotent. Closed rows disappear from the next scan;
            # unverified rows can be read again but are never consumed.
            service.recover_guardian_jobs(self.queue)
        return self.queue.get(binding.job_id), observations

    def test_missing_authenticated_receipt_never_clears_the_unknown_barrier(self):
        job, observations = self.recover(receipt="absent")
        self.assertIsNone(job.worker_closed_at)
        self.assertNotIn("consumed", observations)
        self.assertTrue(self.queue.has_unclosed_worker())

    def test_different_run_nonce_cannot_clear_the_bound_job(self):
        job, observations = self.recover(receipt="wrong_nonce")
        self.assertIsNone(job.worker_closed_at)
        self.assertNotIn("consumed", observations)

    def test_old_or_foreign_manifest_digest_cannot_clear_the_bound_job(self):
        job, observations = self.recover(receipt="wrong_digest")
        self.assertIsNone(job.worker_closed_at)
        self.assertNotIn("consumed", observations)

    def test_model_dictionary_is_not_an_authenticated_receipt(self):
        job, observations = self.recover(receipt="model_dict")
        self.assertIsNone(job.worker_closed_at)
        self.assertNotIn("consumed", observations)

    def test_other_conversation_manifest_is_rejected_before_reading_receipt(self):
        job, observations = self.recover(manifest={**self.manifest, "conversation_id": str(uuid4())})
        self.assertIsNone(job.worker_closed_at)
        self.assertNotIn("read", observations)
        self.assertNotIn("consumed", observations)

    def test_stopped_only_recovery_is_once_and_never_completion_or_automatic_replay(self):
        job, observations = self.recover()
        self.assertIsNotNone(job.worker_closed_at)
        self.assertEqual(1, observations.count("consumed"))
        self.assertEqual(1, observations.count("read"))
        self.assertNotEqual("completed", job.state)
        self.assertIsNone(job.reply_id)
        self.assertIsNone(self.queue.claim())



if __name__ == "__main__": unittest.main()
