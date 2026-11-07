"""Retired unknown-save bookkeeping through explicit Controller history reads.

The existing owned worker fixture supplies opaque execution and closure records.
It exercises real Controller, SQLite and queue paths without model/process IO.
"""
import copy
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import AgentResult
from desktop_pet.conversations import StoreError
from tests.test_chat_commit_ack_recovery import ChatCommitAckRecoveryTests
from tests.test_agent_shutdown_completion import OwnedProcess, OwnedTree


class AgentHistoricalAckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ChatCommitAckRecoveryTests.setUpClass()

    def setUp(self):
        self.fixture = ChatCommitAckRecoveryTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case = self.fixture.case
        self.chat, self.store = self.case.chat, self.case.store
        self.coordinator = self.chat._agent_queue

    def unknown(self, *, status="completed", before=False, artifacts=()):
        committed = [False]
        self.case.outcome = AgentResult("OWNED HISTORICAL ACK RESULT", status=status, artifacts=artifacts)
        original_load = self.store.load_messages

        def load(cid):
            if committed[0]:
                raise StoreError("OWNED_AUTOMATIC_READBACK_UNAVAILABLE")
            return original_load(cid)

        if before:
            def complete(*args, **kwargs):
                committed[0] = True
                raise OSError("OWNED_PRECOMMIT_FAILURE")
            fault = patch.object(self.store, "complete_request", side_effect=complete)
        else:
            fault = self.fixture.commit_fault(after=lambda saved: committed.__setitem__(0, True))
        with fault, patch.object(self.store, "load_messages", side_effect=load):
            self.fixture.start("agent")
            self.fixture.drained()
        self.target = (self.case.cid, self.fixture.user_id)
        self.owner = self.chat._request_owners[self.target]
        self.assertEqual(self.case.job().state, "interrupted")
        self.assertEqual(self.case.job().reason_code, "WORKER_INTERRUPTED")
        self.assertIsNone(self.coordinator._active)
        self.original_rows = self.store.load_messages(self.case.cid)

    def history(self):
        self.assertTrue(self.chat.open_conversation(self.case.cid))
        self.case.wait(lambda: not self.chat.history_loading and not self.chat._runner._tasks)

    def unchanged(self):
        self.assertEqual(self.case.job().state, "interrupted")
        self.assertIsNone(self.case.job().reply_id)
        self.assertEqual(self.case.calls, 1)
        self.assertEqual(self.fixture.delivered, [])
        self.assertIsNone(self.coordinator._active)

    def completed(self):
        self.assertEqual(self.case.job().state, "completed")
        reply = next(row for row in self.original_rows if row["role"] == "assistant")
        self.assertEqual(self.case.job().reply_id, reply["id"])
        self.assertTrue(self.owner["committed"])
        self.assertEqual(self.owner["saved_reply_id"], reply["id"])
        self.assertNotIn(self.target, self.chat._request_owners)
        self.assertEqual(self.case.calls, 1)
        self.assertEqual(self.fixture.delivered, [])
        self.assertIsNone(self.coordinator._active)

    def test_explicit_history_settles_original_completed_job_without_replay(self):
        self.unknown()
        self.history()
        self.completed()
        self.assertEqual(sum(row["role"] == "assistant" for row in self.chat.messages), 1)
        self.assertNotIn(self.target, self.chat._unconfirmed_reply_commits)
        self.assertNotIn(self.target, self.chat._historical_reply_candidates)

    def test_needs_user_never_becomes_completed(self):
        self.unknown(status="needs_user")
        self.history()
        self.unchanged()
        self.assertEqual(next(row for row in self.chat.messages if row["id"] == self.target[1])[
            "metadata"]["execution"]["state"], "needs_user")

    def test_uncommitted_save_never_receives_a_completed_proof(self):
        self.unknown(before=True)
        self.history()
        self.unchanged()

    def test_failed_and_background_reads_do_not_settle(self):
        self.unknown()
        self.chat._install_snapshot(self.case.cid, self.store.get_conversation(self.case.cid), self.original_rows)
        self.unchanged()
        with patch.object(self.store, "load_messages", side_effect=StoreError("OWNED_FAILED_EXPLICIT_READ")):
            self.history()
        self.unchanged()
        self.history()
        self.completed()

    def test_old_load_cannot_settle_after_new_navigation(self):
        self.unknown()
        entered, release = threading.Event(), threading.Event()
        self.case.releases.append(release)
        original = self.store.load_messages

        def hold(cid):
            rows = original(cid)
            entered.set()
            self.assertTrue(release.wait(5))
            return rows

        with patch.object(self.store, "load_messages", side_effect=hold):
            self.assertTrue(self.chat.open_conversation(self.case.cid))
            self.case.wait(entered.is_set)
            self.chat.new_conversation()
            release.set()
            self.case.wait(lambda: not self.chat._runner._tasks)
        self.unchanged()
        self.history()
        self.completed()

    def test_queue_failure_retains_candidate_after_ui_marker_is_released(self):
        self.unknown()
        with patch.object(self.coordinator.queue, "reconcile_completed", side_effect=OSError("OWNED_QUEUE_DOWN")):
            self.history()
        self.unchanged()
        self.assertNotIn(self.target, self.chat._unconfirmed_reply_commits)
        self.assertIn(self.target, self.chat._historical_reply_candidates)
        self.history()
        self.completed()

    def test_queue_commit_ack_failure_is_idempotently_verified_on_next_read(self):
        self.unknown()
        original = self.coordinator.queue.reconcile_completed

        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("OWNED_LEDGER_COMMIT_ACK_LOST")

        with patch.object(self.coordinator.queue, "reconcile_completed", side_effect=lost_ack):
            self.history()
        self.assertEqual(self.case.job().state, "completed")
        self.assertIn(self.target, self.chat._historical_reply_candidates)
        self.history()
        self.completed()
        self.assertNotIn(self.target, self.chat._historical_reply_candidates)

    def test_wrong_archive_fields_reject_even_with_a_complete_user(self):
        self.unknown()
        original = self.store.load_messages
        for field in ("content", "emotion", "user_execution", "reply_execution", "source", "reply_to", "origin",
                      "generation", "duplicate", "user_source", "user_content", "user_role"):
            with self.subTest(field=field):
                def wrong(cid):
                    rows = copy.deepcopy(original(cid))
                    user = next(row for row in rows if row["id"] == self.target[1])
                    reply = next(row for row in rows if row["role"] == "assistant")
                    if field == "content": reply["content"] += " foreign"
                    elif field == "emotion": reply["metadata"]["emotion"] = "sad"
                    elif field == "source": reply["source"] = "remote"
                    elif field == "reply_to": reply["metadata"]["reply_to"] = str(uuid4())
                    elif field == "origin": user["metadata"]["agent_queue_origin"]["request_id"] = str(uuid4())
                    elif field == "generation": rows[0]["id"] = str(uuid4())
                    elif field == "duplicate":
                        duplicate = copy.deepcopy(reply)
                        duplicate["id"] = str(uuid4())
                        rows.append(duplicate)
                    elif field == "user_source": user["source"] = "remote"
                    elif field == "user_content": user["content"] += " changed user"
                    elif field == "user_role": user["role"] = "assistant"
                    else:
                        row = user if field == "user_execution" else reply
                        row["metadata"]["execution"]["started_at"] = "2000-01-01T00:00:00+00:00"
                    return rows
                with patch.object(self.store, "load_messages", side_effect=wrong):
                    self.history()
                self.unchanged()
        self.history()
        self.completed()

    def test_archive_changes_after_explicit_read_are_rechecked_in_verifier(self):
        self.unknown()
        original = self.store.load_messages
        reads = [0]

        def changed(cid):
            reads[0] += 1
            rows = original(cid)
            if reads[0] >= 2:
                next(row for row in rows if row["role"] == "assistant")["content"] += " changed after read"
            return rows

        with patch.object(self.store, "load_messages", side_effect=changed):
            self.history()
        self.assertGreaterEqual(reads[0], 2)
        self.unchanged()
        self.history()
        self.completed()

    def test_owner_and_attempt_replacement_cannot_settle_original_job(self):
        self.unknown()
        self.chat._request_owners[self.target] = dict(self.owner)
        self.history()
        self.unchanged()
        self.chat._request_owners[self.target] = self.owner
        original = self.owner["execution_attempt"]
        self.owner["execution_attempt"] = object()
        self.history()
        self.unchanged()
        self.owner["execution_attempt"] = original
        self.history()
        self.completed()

    def test_revoked_generation_cannot_be_promoted(self):
        self.unknown()
        job = self.case.job()
        self.coordinator.queue.revoke_conversation(self.case.cid, generation=job.generation)
        self.history()
        self.unchanged()

    def test_foreign_or_missing_worker_receipt_cannot_be_replaced_by_closed_at(self):
        self.unknown()
        original = self.case.service.worker_closure
        with patch.object(self.case.service, "worker_closure", return_value=None):
            self.history()
        self.unchanged()
        with patch.object(self.case.service, "validate_worker_closure", return_value=False):
            self.history()
        self.unchanged()
        self.assertIsNotNone(original(self.owner["worker_handle"]))
        self.history()
        self.completed()

    def test_pruned_exact_closure_remains_interrupted(self):
        self.unknown()
        lifecycle = self.case.service._worker_lifecycle
        for index in range(130):
            process, tree = OwnedProcess(), OwnedTree()
            process.pid += index + 1
            handle = lifecycle.register(process, uuid4().hex, self.case.cid)
            lifecycle.attach_tree(handle, tree)
            self.assertIsNotNone(lifecycle.settle(handle, timeout_seconds=0))
        with self.assertRaises(ValueError):
            self.case.service.worker_closure(self.owner["worker_handle"])
        self.history()
        self.unchanged()

    def test_clear_retires_candidate_and_old_bookkeeping(self):
        self.unknown()
        self.chat.clear()
        self.case.wait(lambda: not self.chat._runner._tasks)
        self.assertNotIn(self.target, self.chat._historical_reply_candidates)
        self.history()
        self.unchanged()

    def test_delete_retires_candidate_and_old_bookkeeping(self):
        self.unknown()
        self.assertTrue(self.chat.delete_conversation(self.case.cid))
        self.case.wait(lambda: not self.chat._runner._tasks)
        self.assertNotIn(self.target, self.chat._historical_reply_candidates)
        self.unchanged()

    def test_new_task_authorization_is_still_required_after_historical_settlement(self):
        self.unknown()
        self.case.settings.save_agent_settings(replace(
            self.case.settings.load_agent_settings(), enabled=False))
        self.chat.refresh_agent_settings()
        self.history()
        self.completed()
        response = self.case.rpc("agent.send", {"conversation_id": self.case.cid, "text": "New owned task"})
        self.assertFalse(response["ok"])
        self.assertEqual(self.case.calls, 1)

    def test_only_original_trusted_completed_service_outcome_can_be_recovered(self):
        self.unknown(status="failed")
        self.history()
        self.unchanged()
        self.assertNotIn(self.target, self.chat._historical_reply_candidates)

    def test_candidate_replacement_before_transaction_verifier_rejects_stale_read(self):
        self.unknown()
        original = self.coordinator.queue.reconcile_completed
        candidate = self.chat._historical_reply_candidates[self.target]

        def replace_candidate(*args, **kwargs):
            self.chat._historical_reply_candidates[self.target] = replace(candidate)
            return original(*args, **kwargs)

        with patch.object(self.coordinator.queue, "reconcile_completed", side_effect=replace_candidate):
            self.history()
        self.unchanged()
        self.history()
        self.completed()

    def test_new_owner_before_transaction_verifier_rejects_stale_read(self):
        self.unknown()
        original = self.coordinator.queue.reconcile_completed

        def replace_owner(*args, **kwargs):
            self.chat._request_owners[self.target] = dict(self.owner)
            return original(*args, **kwargs)

        with patch.object(self.coordinator.queue, "reconcile_completed", side_effect=replace_owner):
            self.history()
        self.unchanged()
        self.chat._request_owners[self.target] = self.owner
        self.history()
        self.completed()

    def test_readback_failure_inside_verifier_keeps_candidate_for_next_read(self):
        self.unknown()
        original = self.store.load_messages
        reads = [0]

        def fail_verifier(cid):
            reads[0] += 1
            if reads[0] == 2:
                raise StoreError("OWNED_VERIFIER_READ_FAILED")
            return original(cid)

        with patch.object(self.store, "load_messages", side_effect=fail_verifier):
            self.history()
        self.unchanged()
        self.assertIn(self.target, self.chat._historical_reply_candidates)
        self.history()
        self.completed()

    def test_missing_artifact_cannot_be_overruled_by_saved_reply(self):
        reference = "artifact_" + "b" * 64
        self.unknown(artifacts=({"reference": reference},))
        with patch.object(self.case.service, "artifacts", return_value=[]):
            self.history()
        self.unchanged()
        with patch.object(self.case.service, "artifacts", return_value=[{"reference": reference}]):
            self.history()
        self.completed()
        self.assertEqual(self.case.job().artifact_refs, (reference,))

    def test_historical_bookkeeping_leaves_a_new_active_worker_unchanged(self):
        self.unknown()
        old_job = self.case.job().job_id
        self.case.hold_worker()
        self.case.worker_entered.clear()
        new_ack = self.case.send()
        self.case.wait(self.case.worker_entered.is_set)
        active = self.coordinator._active
        handle, attempt = active.handle, active.attempt
        self.assertEqual(active.claim.job.job_id, new_ack["job_id"])
        self.assertTrue(self.chat.open_conversation(self.case.cid))
        self.case.wait(lambda: self.coordinator.queue.get(old_job).state == "completed")
        self.assertIs(self.coordinator._active, active)
        self.assertIs(active.handle, handle)
        self.assertIs(active.attempt, attempt)
        self.assertFalse(active.request["cancel"].is_set())
        self.assertEqual(self.coordinator.queue.get(new_ack["job_id"]).state, "running")
        self.assertEqual(self.case.calls, 2)
        self.assertEqual(self.fixture.delivered, [])
        self.case.worker_release.set()
        self.fixture.drained()
        self.assertEqual(self.coordinator.queue.get(old_job).state, "completed")
        self.assertEqual(self.coordinator.queue.get(new_ack["job_id"]).state, "completed")

    def test_late_shutdown_prevents_historical_verifier_from_promoting(self):
        self.unknown()
        original = self.coordinator.queue.reconcile_completed
        entered, release = threading.Event(), threading.Event()
        self.case.releases.append(release)

        def held(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        with patch.object(self.coordinator.queue, "reconcile_completed", side_effect=held):
            self.assertTrue(self.chat.open_conversation(self.case.cid))
            self.case.wait(entered.is_set)
            def release_after_shutdown_fence():
                for _ in range(1000):
                    if self.chat._closed:
                        break
                    threading.Event().wait(.005)
                release.set()
            releaser = threading.Thread(target=release_after_shutdown_fence, daemon=True)
            releaser.start()
            self.chat.shutdown()
            releaser.join(6)
            self.assertFalse(releaser.is_alive())
            self.case.wait(lambda: not self.chat._runner._tasks)
        self.unchanged()

    def test_older_stored_message_added_after_history_read_invalidates_generation(self):
        self.unknown()
        original = self.coordinator.queue.reconcile_completed

        def archive_changed(*args, **kwargs):
            self.store.append_message(self.case.cid, "user", "Owned older imported history", "user",
                created_at="2000-01-01T00:00:00+00:00", metadata={"remote_import": True})
            return original(*args, **kwargs)

        with patch.object(self.coordinator.queue, "reconcile_completed", side_effect=archive_changed):
            self.history()
        self.assertNotEqual(self.store.read_conversation_generation(self.case.cid), self.case.job().generation)
        self.unchanged()

    def test_navigation_after_history_acceptance_prevents_late_verifier(self):
        self.unknown()
        original = self.coordinator.queue.reconcile_completed
        entered, release = threading.Event(), threading.Event()
        self.case.releases.append(release)

        def held(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        with patch.object(self.coordinator.queue, "reconcile_completed", side_effect=held):
            self.assertTrue(self.chat.open_conversation(self.case.cid))
            self.case.wait(entered.is_set)
            self.chat.new_conversation()
            release.set()
            self.case.wait(lambda: not self.chat._runner._tasks)
        self.unchanged()
        self.history()
        self.completed()

    def test_already_completed_history_is_confirmed_without_new_ledger_write(self):
        self.unknown()
        original = self.coordinator.queue.reconcile_completed

        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("OWNED_LEDGER_COMMIT_ACK_LOST")

        with patch.object(self.coordinator.queue, "reconcile_completed", side_effect=lost_ack):
            self.history()
        job = self.case.job()
        self.assertEqual(job.state, "completed")
        self.coordinator.queue.revoke_conversation(self.case.cid, generation=job.generation)
        with patch.object(self.coordinator.queue, "reconcile_completed", wraps=original) as publication:
            self.history()
        publication.assert_not_called()
        self.assertEqual(self.case.job().state, "completed")
        self.assertNotIn(self.target, self.chat._historical_reply_candidates)
        self.assertTrue(self.owner["committed"])
        self.assertEqual(self.case.calls, 1)

    def test_revoked_peer_history_does_not_reauthorize_new_tasks(self):
        self.unknown()
        from tests.test_agent_shutdown_completion import PEER
        self.assertTrue(self.case.remote.revoke_device(PEER))
        self.history()
        self.completed()
        response = self.case.rpc("agent.send", {"conversation_id": self.case.cid, "text": "New owned task"})
        self.assertFalse(response["ok"])
        self.assertEqual(self.case.calls, 1)


if __name__ == "__main__":
    unittest.main()
