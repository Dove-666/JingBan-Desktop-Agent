"""Historical saved-success settlement without renewing executable authority."""

from dataclasses import replace
import hashlib
import json
import sqlite3
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents.artifacts import ArtifactStore
from desktop_pet.agents.coordinator import CoordinatorError
from desktop_pet.agents.job_queue import JobLease, JobQueueError
from desktop_pet.conversations.store import StoreError
from agent_backend.nanobot.safe_files import WorkspaceFiles
from tests import test_agent_shutdown_completion as fixture


class AgentExpiredCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AgentShutdownCompletionTests.setUpClass()

    def setUp(self):
        self.host = fixture.AgentShutdownCompletionTests("runTest")
        self.host.setUp()
        self.addCleanup(self.cleanup)
        self.clock = [time.time()]
        self.host.chat._agent_queue.queue._clock = lambda: self.clock[0]

    def cleanup(self):
        self.assertTrue(self.host.doCleanups())

    def register_worker_delivery(self):
        host = self.host
        host.service._artifact_store = ArtifactStore(host.root / "artifacts")
        original = host.worker
        def deliver(*args, **kwargs):
            outcome = original(*args, **kwargs)
            files = WorkspaceFiles(host.root)
            digest = hashlib.sha256((host.root / "result.txt").read_bytes()).hexdigest()
            metadata = host.service._artifact_store.register(host.cid,
                {"path": "result.txt", "passed": True, "sha256": digest, "scope": "owned_readback"}, files)
            return replace(outcome, artifacts=(metadata,))
        host.worker_patch.stop()
        host.worker_patch = patch.object(host.service, "_execute_impl", side_effect=deliver)
        host.worker_patch.start()

    def test_expired_committed_reply_and_real_delivery_settle_without_replaying(self):
        host = self.host
        self.register_worker_delivery()
        committed, release, hold = host.hold_commit()
        with hold:
            host.send()
            host.wait(committed.is_set)
            context = host.chat._agent_queue._active
            self.assertTrue(host.service.validate_worker_closure(host.handles[0], host.service.worker_closure(host.handles[0])))
            host.chat.shutdown()
            self.clock[0] += 31
            release.set()
            host.wait_physical()
        job = host.job()
        rows = host.store.load_messages(host.cid)
        self.assertEqual(job.state, "completed")
        self.assertEqual(job.reply_id, rows[-1]["id"])
        self.assertIsNotNone(job.worker_closed_at)
        self.assertEqual(len(job.artifact_refs), 1)
        metadata, content = host.service.artifact_snapshot(host.cid, job.artifact_refs[0])
        self.assertEqual(content, b"OWNED RESULT")
        self.assertEqual(metadata["sha256"], hashlib.sha256(content).hexdigest())
        host.chat._agent_queue.finished(context, context.request, context.result)
        self.assertEqual(host.job(), job)
        self.assertEqual(len(host.store.load_messages(host.cid)), 2)
        self.assertEqual(host.calls, 1)
        with self.assertRaises(CoordinatorError):
            host.chat.admit_remote_agent(fixture.PEER, str(uuid4()), host.cid, "No new closed-host effects")

    def test_already_expired_unknown_can_settle_only_the_same_committed_context(self):
        host = self.host
        committed, release, hold = host.hold_commit()
        with hold:
            host.send()
            host.wait(committed.is_set)
            self.clock[0] += 31
            host.chat._agent_queue.queue.recover_expired()
            self.assertEqual(host.job().state, "unknown")
            release.set()
            host.wait_gui()
        self.assertEqual(host.job().state, "completed")
        self.assertEqual(host.calls, 1)

    def test_active_lease_mismatch_and_fake_reason_cannot_become_history_authority(self):
        host = self.host
        committed, release, hold = host.hold_commit()
        attempts = []
        queue = host.chat._agent_queue.queue
        original = queue.reconcile_completed
        def record(*args, **kwargs):
            attempts.append(kwargs.get("lease"))
            return original(*args, **kwargs)
        with hold, patch.object(queue, "reconcile_completed", side_effect=record):
            host.send()
            host.wait(committed.is_set)
            context = host.chat._agent_queue._active
            context.claim = replace(context.claim, lease=JobLease(host.ack["job_id"], "f" * 64))
            with queue._connection() as connection:
                connection.execute("UPDATE jobs SET reason_code='LEASE_EXPIRED' WHERE job_id=?", (host.ack["job_id"],))
            release.set()
            host.wait_gui()
        self.assertNotEqual(host.job().state, "completed")
        self.assertTrue(attempts)
        self.assertTrue(all(lease is not None for lease in attempts))
        self.assertEqual(host.calls, 1)

    def test_persistent_generation_revocation_rejects_historical_success(self):
        host = self.host
        committed, release, hold = host.hold_commit()
        with hold:
            host.send()
            host.wait(committed.is_set)
            host.chat._agent_queue.queue.revoke_conversation(host.cid, generation=host.job().generation)
            self.clock[0] += 31
            release.set()
            host.wait_gui()
        self.assertNotEqual(host.job().state, "completed")
        self.assertIsNone(host.job().reply_id)
        self.assertEqual(len(host.store.load_messages(host.cid)), 2)

    def test_historical_verifier_rechecks_reply_binding_after_the_expired_lease_error(self):
        host = self.host
        queue = host.chat._agent_queue.queue
        original = queue.reconcile_completed
        changed = []
        def change_after_expiry(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except JobQueueError as error:
                if str(error) == "QUEUE_LEASE_EXPIRED" and not changed:
                    reply = host.store.load_messages(host.cid)[-1]
                    metadata = dict(reply["metadata"], reply_to=str(uuid4()))
                    with host.store._connection() as connection:
                        connection.execute("UPDATE messages SET metadata=? WHERE id=?",
                            (json.dumps(metadata), reply["id"]))
                    changed.append(True)
                raise
        committed, release, hold = host.hold_commit()
        with hold, patch.object(queue, "reconcile_completed", side_effect=change_after_expiry):
            host.send()
            host.wait(committed.is_set)
            self.clock[0] += 31
            release.set()
            host.wait_gui()
        self.assertTrue(changed)
        self.assertNotEqual(host.job().state, "completed")
        self.assertIsNone(host.job().reply_id)

    def test_missing_delivery_after_expiry_cannot_use_cached_artifact_evidence(self):
        host = self.host
        self.register_worker_delivery()
        queue = host.chat._agent_queue.queue
        original = queue.reconcile_completed
        removed = []
        def remove_after_expiry(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except JobQueueError as error:
                if str(error) == "QUEUE_LEASE_EXPIRED" and not removed:
                    host.service._artifact_store.clear(host.cid)
                    removed.append(True)
                raise
        committed, release, hold = host.hold_commit()
        with hold, patch.object(queue, "reconcile_completed", side_effect=remove_after_expiry):
            host.send()
            host.wait(committed.is_set)
            self.clock[0] += 31
            release.set()
            host.wait_gui()
        self.assertTrue(removed)
        self.assertNotEqual(host.job().state, "completed")

    def test_missing_exact_closure_retains_unknown_and_prevents_next_dispatch(self):
        host = self.host
        host.tree_confirmed = False
        host.send()
        host.wait_gui()
        self.clock[0] += 31
        host.chat._pump_agent_queue()
        self.assertEqual(host.job().state, "unknown")
        self.assertIsNone(host.job().worker_closed_at)
        self.assertIsNone(host.job().reply_id)
        second = host.rpc("agent.send", {"conversation_id": host.cid, "text": "Wait for exact prior closure"})
        self.assertTrue(second["ok"], second)
        host.chat._pump_agent_queue()
        self.assertEqual(host.calls, 1)
        self.assertEqual(host.chat._agent_queue.queue.get(second["result"]["job_id"]).state, "queued")

    def test_store_failure_after_expiry_cannot_be_reported_as_saved_success(self):
        host = self.host
        queue = host.chat._agent_queue.queue
        original = queue.reconcile_completed
        failed = []
        original_read = host.store.read_message
        def storage_failure(*args, **kwargs):
            if failed:
                raise StoreError("Owned metadata read unavailable")
            return original_read(*args, **kwargs)
        def fail_after_expiry(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except JobQueueError as error:
                if str(error) == "QUEUE_LEASE_EXPIRED":
                    failed.append(True)
                raise
        committed, release, hold = host.hold_commit()
        with hold, patch.object(queue, "reconcile_completed", side_effect=fail_after_expiry), \
                patch.object(host.store, "read_message", side_effect=storage_failure):
            host.send()
            host.wait(committed.is_set)
            self.clock[0] += 31
            release.set()
            host.wait_gui()
        self.assertTrue(failed)
        self.assertNotEqual(host.job().state, "completed")
        self.assertIsNone(host.job().reply_id)

    def test_historical_verifier_runs_inside_the_ledger_write_transaction(self):
        host = self.host
        queue = host.chat._agent_queue.queue
        original = queue.reconcile_completed
        observations = []
        def observe(*args, **kwargs):
            if kwargs.get("lease") is None:
                verify = kwargs["verifier"]
                def checked(current, reply_id):
                    connection = sqlite3.connect(str(queue._path), timeout=0, isolation_level=None)
                    try:
                        with self.assertRaises(sqlite3.OperationalError):
                            connection.execute("BEGIN IMMEDIATE")
                    finally:
                        connection.close()
                    observations.append((current.state, current.reason_code, current.lease_expires_at))
                    return verify(current, reply_id)
                kwargs["verifier"] = checked
            return original(*args, **kwargs)
        committed, release, hold = host.hold_commit()
        with hold, patch.object(queue, "reconcile_completed", side_effect=observe):
            host.send()
            host.wait(committed.is_set)
            self.clock[0] += 31
            release.set()
            host.wait_gui()
        self.assertEqual(observations, [("unknown", "LEASE_EXPIRED", None)])
        self.assertEqual(host.job().state, "completed")

    def test_nonexpired_completion_retains_the_existing_lease_path(self):
        host = self.host
        queue = host.chat._agent_queue.queue
        original = queue.reconcile_completed
        leases = []
        def observe(*args, **kwargs):
            leases.append(kwargs.get("lease"))
            return original(*args, **kwargs)
        committed, release, hold = host.hold_commit()
        with hold, patch.object(queue, "reconcile_completed", side_effect=observe):
            host.send()
            host.wait(committed.is_set)
            self.clock[0] += 1
            release.set()
            host.wait_gui()
        self.assertEqual(len(leases), 1)
        self.assertIsNotNone(leases[0])
        self.assertEqual(host.job().state, "completed")

    def test_a_fake_historical_reason_without_elapsed_lease_is_rejected(self):
        host = self.host
        queue = host.chat._agent_queue.queue
        committed, release, hold = host.hold_commit()
        with hold:
            host.send()
            host.wait(committed.is_set)
            # An adversarial owned ledger row cannot invent elapsed time.
            with queue._connection() as connection:
                connection.execute("""UPDATE jobs SET state='unknown',reason_code='LEASE_EXPIRED',
                    lease_hash=NULL,lease_expires_at=NULL,finished_at=? WHERE job_id=?""",
                    (round(self.clock[0] * 1000), host.ack["job_id"]))
            release.set()
            host.wait_gui()
        self.assertNotEqual(host.job().state, "completed")
        self.assertIsNone(host.job().reply_id)
        self.assertTrue(host.chat._agent_queue._failed)

    def test_context_replaced_before_the_history_verifier_cannot_settle_old_success(self):
        host = self.host
        coordinator = host.chat._agent_queue
        queue = coordinator.queue
        original = queue.reconcile_completed
        replaced = []
        def replace_context(*args, **kwargs):
            if kwargs.get("lease") is None:
                coordinator._active = None
                replaced.append(True)
            return original(*args, **kwargs)
        committed, release, hold = host.hold_commit()
        with hold, patch.object(queue, "reconcile_completed", side_effect=replace_context):
            host.send()
            host.wait(committed.is_set)
            self.clock[0] += 31
            release.set()
            host.wait_gui()
        self.assertTrue(replaced)
        self.assertNotEqual(host.job().state, "completed")
        self.assertIsNone(host.job().reply_id)
        self.assertEqual(host.calls, 1)


if __name__ == "__main__":
    unittest.main()
