"""Owned save-confirmation failures through the actual Controller entry points."""
import copy
import threading
import unittest
from unittest.mock import patch

from desktop_pet.agents import AgentResult
from desktop_pet.api_client import ChatReply
from desktop_pet.conversations.store import StoreError
from tests import test_agent_shutdown_completion as owned


class FixedReply(owned.NoProvider):
    def __init__(self):
        self.calls = 0
        self.before_return = None

    def complete_context(self, *args, **kwargs):
        self.calls += 1
        if self.before_return is not None:
            self.before_return()
        return ChatReply("OWNED ACK RECOVERY ANSWER", "neutral")

    def complete(self, *args, **kwargs):
        return self.complete_context(*args, **kwargs)


class ChatCommitAckRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        owned.AgentShutdownCompletionTests.setUpClass()

    def setUp(self):
        self.case = owned.AgentShutdownCompletionTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.chat = self.case.chat
        self.store = self.case.store
        self.adapter = FixedReply()
        self.chat._client = self.adapter
        self.delivered = []
        self.chat.reply_received.connect(lambda *args: self.delivered.append(args))

    def start(self, mode="ordinary"):
        if mode == "agent":
            self.user_id = self.case.send()["message_id"]
        else:
            self.assertTrue(self.chat.send("Owned ordinary task for commit acknowledgement check."))
            self.user_id = self.chat.messages[-1]["id"]

    def drained(self):
        self.case.wait_gui()
        self.case.wait_physical()

    def rows(self):
        rows = self.store.load_messages(self.case.cid)
        user = next(row for row in rows if row["id"] == self.user_id)
        replies = [row for row in rows if row["role"] == "assistant"]
        return user, replies

    def commit_fault(self, *, before=False, after=None):
        original = self.store.complete_request

        def injected(*args, **kwargs):
            if not before:
                saved = original(*args, **kwargs)
                if after is not None:
                    after(saved)
            raise OSError("OWNED_COMMIT_ACK_FAILURE")

        return patch.object(self.store, "complete_request", side_effect=injected)

    def assert_restored(self, mode="ordinary", state="completed"):
        user, replies = self.rows()
        self.assertEqual(len(replies), 1)
        self.assertEqual(user["metadata"]["request_status"], "complete")
        self.assertEqual(user["metadata"]["execution"]["state"], state)
        self.assertEqual(replies[0]["metadata"]["execution"]["state"], state)
        self.assertEqual(sum(row["role"] == "assistant" for row in self.chat.messages), 1)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.delivered[0][2]["message_id"], replies[0]["id"])
        self.assertFalse(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
        self.assertEqual(self.adapter.calls, 0 if mode == "agent" else 1)
        self.assertEqual(self.case.calls, 1 if mode == "agent" else 0)
        if mode == "agent" and state == "completed":
            self.assertEqual(self.case.job().state, "completed")
            self.assertEqual(self.case.job().reply_id, replies[0]["id"])

    def test_ordinary_successful_save_failed_ack_is_immediately_visible(self):
        with self.commit_fault():
            self.start()
            self.drained()
        self.assert_restored()

    def test_queued_agent_successful_save_failed_ack_retires_the_same_job(self):
        with self.commit_fault():
            self.start("agent")
            self.drained()
        self.assert_restored("agent")

    def test_needs_user_outcome_keeps_its_original_semantics(self):
        self.case.outcome = AgentResult("OWNED PARTIAL RESULT", status="needs_user")
        with self.commit_fault():
            self.start("agent")
            self.drained()
        self.assert_restored("agent", "needs_user")
        self.assertNotEqual(self.case.job().state, "completed")

    def test_precommit_failure_has_no_saved_reply(self):
        with self.commit_fault(before=True):
            self.start()
            self.drained()
        user, replies = self.rows()
        self.assertEqual(replies, [])
        self.assertEqual(user["metadata"]["request_status"], "failed")
        self.assertTrue(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.adapter.calls, 1)

    def test_unknown_readback_preserves_archive_and_requires_fresh_history(self):
        committed = [False]
        original_load = self.store.load_messages

        def read(cid):
            if committed[0]:
                raise StoreError("OWNED_READBACK_UNAVAILABLE")
            return original_load(cid)

        with self.commit_fault(after=lambda saved: committed.__setitem__(0, True)), \
                patch.object(self.store, "load_messages", side_effect=read):
            self.start()
            self.drained()
        user, replies = self.rows()
        self.assertEqual(user["metadata"]["request_status"], "complete")
        self.assertEqual(user["metadata"]["execution"]["state"], "completed")
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.chat.error_action, "history")
        self.assertFalse(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
        self.assertFalse(self.chat.retry(self.case.cid, self.user_id))
        self.assertEqual(self.adapter.calls, 1)
        with patch.object(self.store, "get_conversation", side_effect=StoreError("OWNED_HISTORY_UNAVAILABLE")):
            self.assertTrue(self.chat.open_conversation(self.case.cid))
            self.case.wait(lambda: not self.chat.history_loading)
        self.assertFalse(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
        self.assertTrue(self.chat.open_conversation(self.case.cid))
        self.case.wait(lambda: not self.chat.history_loading)
        self.assertEqual(sum(row["role"] == "assistant" for row in self.chat.messages), 1)
        self.assertFalse(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
        self.assertEqual(self.delivered, [])

    def test_nonmatching_snapshot_is_not_adopted_or_written(self):
        committed = [False]
        original_load = self.store.load_messages

        def read(cid):
            rows = original_load(cid)
            if committed[0]:
                rows = copy.deepcopy(rows)
                next(row for row in rows if row["role"] == "assistant")["metadata"]["execution"]["started_at"] = "2000-01-01T00:00:00+00:00"
            return rows

        with self.commit_fault(after=lambda saved: committed.__setitem__(0, True)), \
                patch.object(self.store, "load_messages", side_effect=read):
            self.start()
            self.drained()
        user, replies = self.rows()
        self.assertEqual(user["metadata"]["request_status"], "complete")
        self.assertEqual(user["metadata"]["execution"]["state"], "completed")
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.chat.error_action, "history")
        self.assertFalse(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
        self.assertEqual(self.delivered, [])

    def test_stop_during_committed_save_and_readback_is_too_late(self):
        entered, release = threading.Event(), threading.Event()
        self.case.releases.append(release)

        def hold(saved):
            entered.set()
            self.assertTrue(release.wait(5))

        with self.commit_fault(after=hold):
            self.start()
            self.case.wait(entered.is_set)
            self.assertFalse(self.chat.stop_request(self.case.cid))
            release.set()
            self.drained()
        self.assert_restored()

    def test_stop_before_commit_keeps_cancelled_result(self):
        self.case.hold_worker()
        with self.commit_fault() as publication:
            self.start("agent")
            self.case.wait(self.case.worker_entered.is_set)
            self.assertTrue(self.chat.stop_request(self.case.cid))
            self.case.worker_release.set()
            self.drained()
        self.assertEqual(publication.call_count, 0)
        user, replies = self.rows()
        self.assertEqual(replies, [])
        self.assertEqual(user["metadata"]["request_status"], "cancelled")
        self.assertNotEqual(self.case.job().state, "completed")

    def test_shutdown_after_saved_reply_can_acknowledge_the_existing_result(self):
        entered, release = threading.Event(), threading.Event()
        self.case.releases.append(release)

        def hold(saved):
            entered.set()
            self.assertTrue(release.wait(5))

        with self.commit_fault(after=hold):
            self.start("agent")
            self.case.wait(entered.is_set)
            self.chat.shutdown()
            release.set()
            self.case.wait_physical()
        user, replies = self.rows()
        self.assertEqual(user["metadata"]["request_status"], "complete")
        self.assertEqual(user["metadata"]["execution"]["state"], "completed")
        self.assertEqual(self.case.job().state, "completed")
        self.assertEqual(self.case.job().reply_id, replies[0]["id"])
        self.assertEqual(self.case.calls, 1)
        self.assertEqual(self.delivered, [])

    def test_healthy_return_adds_no_recovery_read(self):
        returned, reads = [False], []
        original_complete, original_load = self.store.complete_request, self.store.load_messages

        def complete(*args, **kwargs):
            result = original_complete(*args, **kwargs)
            returned[0] = True
            return result

        def load(cid):
            if returned[0]:
                reads.append(cid)
            return original_load(cid)

        with patch.object(self.store, "complete_request", side_effect=complete), \
                patch.object(self.store, "load_messages", side_effect=load):
            self.start()
            self.drained()
        # The frozen original performs two reads after a healthy save, including
        # its existing metadata follow-up. Recovery must not add another read.
        self.assertEqual(reads, [self.case.cid, self.case.cid])
        self.assert_restored()

    def test_unknown_guard_rejects_late_trace_and_background_snapshot(self):
        committed = [False]
        original_load = self.store.load_messages

        def read(cid):
            if committed[0]:
                raise StoreError("OWNED_READBACK_UNAVAILABLE")
            return original_load(cid)

        with self.commit_fault(after=lambda saved: committed.__setitem__(0, True)), \
                patch.object(self.store, "load_messages", side_effect=read):
            self.start()
            self.drained()
        archived = self.store.load_messages(self.case.cid)
        user = next(row for row in archived if row["id"] == self.user_id)
        self.chat._execution_changed.emit(self.case.cid, self.user_id, user["metadata"]["execution"])
        self.case.app.processEvents()
        self.chat._install_snapshot(self.case.cid, self.store.get_conversation(self.case.cid), archived)
        visible = next(row for row in self.chat.messages if row["id"] == self.user_id)
        self.assertEqual(visible["metadata"]["request_status"], "interrupted")
        self.assertEqual(visible["metadata"]["execution"]["state"], "interrupted")
        self.assertFalse(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
        self.assertFalse(self.chat.retry(self.case.cid, self.user_id))
        self.assertTrue(self.chat.open_conversation(self.case.cid))
        self.case.wait(lambda: not self.chat.history_loading)
        visible = next(row for row in self.chat.messages if row["id"] == self.user_id)
        self.assertEqual(visible["metadata"]["execution"]["state"], "completed")
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.delivered, [])

    def test_unknown_missing_reply_allows_retry_after_confirmed_history_read(self):
        attempted = [False]
        original_load = self.store.load_messages

        def complete(*args, **kwargs):
            attempted[0] = True
            raise OSError("OWNED_PRECOMMIT_FAILURE")

        def read(cid):
            if attempted[0]:
                raise StoreError("OWNED_READBACK_UNAVAILABLE")
            return original_load(cid)

        with patch.object(self.store, "complete_request", side_effect=complete), \
                patch.object(self.store, "load_messages", side_effect=read):
            self.start()
            self.drained()
        self.assertEqual(self.rows()[1], [])
        self.assertFalse(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
        self.assertFalse(self.chat.retry(self.case.cid, self.user_id))
        self.assertTrue(self.chat.open_conversation(self.case.cid))
        self.case.wait(lambda: not self.chat.history_loading)
        self.assertTrue(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
        self.assertTrue(self.chat.retry(self.case.cid, self.user_id))
        self.drained()
        self.assertEqual(self.adapter.calls, 2)
        self.assertEqual(len(self.rows()[1]), 1)
        self.assertEqual(len(self.delivered), 1)

    def test_changed_content_cannot_be_accepted_as_this_attempt(self):
        committed = [False]
        original_load = self.store.load_messages

        def read(cid):
            rows = original_load(cid)
            if committed[0]:
                next(row for row in rows if row["role"] == "assistant")["content"] = "OTHER OWNED CONTENT"
            return rows

        with self.commit_fault(after=lambda saved: committed.__setitem__(0, True)), \
                patch.object(self.store, "load_messages", side_effect=read):
            self.start()
            self.drained()
        self.assertEqual(self.chat.error_action, "history")
        self.assertEqual(self.rows()[0]["metadata"]["request_status"], "complete")
        self.assertEqual(self.delivered, [])

    def test_deleted_generation_cannot_restore_a_saved_old_reply(self):
        with self.commit_fault(after=lambda saved: self.store.clear_messages(self.case.cid)):
            self.start()
            self.drained()
        self.assertEqual(self.store.load_messages(self.case.cid), [])
        self.assertEqual(self.chat.error_action, "history")
        self.assertEqual(self.delivered, [])
        self.assertTrue(self.chat.open_conversation(self.case.cid))
        self.case.wait(lambda: not self.chat.history_loading)
        self.assertEqual(self.chat.messages, [])

    def test_domain_invalidation_preserves_archive_and_blocks_adoption(self):
        with self.commit_fault(after=lambda saved: self.chat._invalidating_conversations.add(self.case.cid)):
            self.start()
            self.drained()
        user, replies = self.rows()
        self.assertEqual(user["metadata"]["request_status"], "complete")
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.delivered, [])
        self.chat._invalidating_conversations.discard(self.case.cid)
        self.assertTrue(self.chat.open_conversation(self.case.cid))
        self.case.wait(lambda: not self.chat.history_loading)
        self.assertEqual(sum(row["role"] == "assistant" for row in self.chat.messages), 1)

    def test_confirmed_clear_retires_the_unknown_message_guard(self):
        committed = [False]
        original_load = self.store.load_messages

        def read(cid):
            if committed[0]:
                raise StoreError("OWNED_READBACK_UNAVAILABLE")
            return original_load(cid)

        with self.commit_fault(after=lambda saved: committed.__setitem__(0, True)), \
                patch.object(self.store, "load_messages", side_effect=read):
            self.start()
            self.drained()
        self.assertFalse(self.chat.retry(self.case.cid, self.user_id))
        self.chat.clear()
        self.case.wait(lambda: not self.chat.history_loading and not self.chat._runner._tasks)
        self.assertEqual(self.store.load_messages(self.case.cid), [])
        self.assertEqual(self.chat.message_retry_state(self.case.cid, self.user_id)["status"], "")
        self.assertTrue(self.chat.send("Owned ordinary task after confirmed clear."))
        self.drained()
        self.assertEqual(self.adapter.calls, 2)

    def test_history_started_before_unknown_result_cannot_release_its_guard(self):
        provider_entered, provider_release = threading.Event(), threading.Event()
        history_captured, history_release = threading.Event(), threading.Event()
        self.case.releases.extend((provider_release, history_release))
        provider_thread, committed = [None], [False]
        original_load = self.store.load_messages

        def hold_provider():
            provider_thread[0] = threading.get_ident()
            provider_entered.set()
            self.assertTrue(provider_release.wait(5))

        def read(cid):
            if threading.get_ident() == provider_thread[0] and committed[0]:
                raise StoreError("OWNED_READBACK_UNAVAILABLE")
            rows = original_load(cid)
            if (provider_thread[0] is not None and threading.get_ident() != provider_thread[0]
                    and not history_captured.is_set()):
                history_captured.set()
                self.assertTrue(history_release.wait(5))
            return rows

        self.adapter.before_return = hold_provider
        with self.commit_fault(after=lambda saved: committed.__setitem__(0, True)), \
                patch.object(self.store, "load_messages", side_effect=read):
            self.start()
            self.case.wait(provider_entered.is_set)
            self.assertTrue(self.chat.open_conversation(self.case.cid))
            self.case.wait(history_captured.is_set)
            provider_release.set()
            self.case.wait(lambda: not self.chat.busy and not self.chat._requests)
            self.assertFalse(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
            history_release.set()
            self.case.wait(lambda: not self.chat.history_loading and not self.chat._runner._tasks)
        visible = next(row for row in self.chat.messages if row["id"] == self.user_id)
        self.assertEqual(visible["metadata"]["execution"]["state"], "interrupted")
        self.assertFalse(self.chat.message_retry_state(self.case.cid, self.user_id)["available"])
        self.assertTrue(self.chat.open_conversation(self.case.cid))
        self.case.wait(lambda: not self.chat.history_loading)
        visible = next(row for row in self.chat.messages if row["id"] == self.user_id)
        self.assertEqual(visible["metadata"]["execution"]["state"], "completed")
        self.assertEqual(self.adapter.calls, 1)

    def test_confirmed_delete_retires_the_unknown_message_guard(self):
        committed = [False]
        original_load = self.store.load_messages

        def read(cid):
            if committed[0]:
                raise StoreError("OWNED_READBACK_UNAVAILABLE")
            return original_load(cid)

        with self.commit_fault(after=lambda saved: committed.__setitem__(0, True)), \
                patch.object(self.store, "load_messages", side_effect=read):
            self.start()
            self.drained()
        cid = self.case.cid
        self.assertTrue(self.chat.delete_conversation(cid))
        self.case.wait(lambda: not self.chat._runner._tasks)
        self.assertIsNone(self.store.get_conversation(cid))
        self.assertEqual(self.chat.message_retry_state(cid, self.user_id)["status"], "")
        self.assertNotEqual(self.chat.current_conversation_id, cid)
