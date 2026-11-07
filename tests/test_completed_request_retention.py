"""Completed chat requests release their private trace and key-refresh closures."""
import gc
import unittest
import weakref

from tests.test_chat_commit_ack_recovery import ChatCommitAckRecoveryTests


class CompletedRequestRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ChatCommitAckRecoveryTests.setUpClass()

    def setUp(self):
        self.case = ChatCommitAckRecoveryTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_successful_turns_release_private_requests_and_keep_archive(self):
        chat, store = self.case.chat, self.case.store
        traces = []
        self.case.adapter.before_return = lambda: traces.append(weakref.ref(chat._active_request["trace"]))
        for turn in range(6):
            self.assertTrue(chat.send(f"Owned completed retention turn {turn}."))
            self.case.drained()
        gc.collect()
        self.assertEqual(chat._requests, {})
        self.assertEqual(chat._request_owners, {})
        self.assertTrue(all(reference() is None for reference in traces))
        rows = store.load_messages(self.case.case.cid)
        self.assertEqual(sum(row["role"] == "assistant" for row in rows), 6)
        self.assertTrue(chat.new_conversation())
        self.case.case.wait(lambda: not chat._runner._tasks)
        self.assertTrue(chat.open_conversation(self.case.case.cid))
        self.case.case.wait(lambda: not chat.history_loading and not chat._runner._tasks)
        self.assertEqual(sum(row["role"] == "assistant" for row in chat.messages), 6)
        self.assertTrue(all(row.get("metadata", {}).get("execution", {}).get("state") == "completed"
                            for row in chat.messages if row["role"] == "user"))

    def test_successful_commit_ack_readback_releases_request(self):
        with self.case.commit_fault():
            self.case.start()
            self.case.drained()
        self.case.assert_restored()
        self.assertEqual(self.case.chat._request_owners, {})

    def test_unconfirmed_commit_keeps_original_owner_and_retry_guard(self):
        from unittest.mock import patch
        from desktop_pet.conversations.store import StoreError

        committed = [False]
        original_load = self.case.store.load_messages

        def read(conversation_id):
            if committed[0]:
                raise StoreError("OWNED_RETENTION_READBACK_UNAVAILABLE")
            return original_load(conversation_id)

        with self.case.commit_fault(after=lambda saved: committed.__setitem__(0, True)), \
                patch.object(self.case.store, "load_messages", side_effect=read):
            self.case.start()
            self.case.drained()
        target = (self.case.case.cid, self.case.user_id)
        self.assertIn(target, self.case.chat._request_owners)
        self.assertIn(target, self.case.chat._unconfirmed_reply_commits)
        self.assertFalse(self.case.chat.retry(*target))

    def test_precommit_failure_releases_private_owner_and_public_retry_succeeds(self):
        requests = []
        self.case.adapter.before_return = lambda: requests.append(self.case.chat._active_request)
        with self.case.commit_fault(before=True):
            self.case.start()
            self.case.drained()
        target = (self.case.case.cid, self.case.user_id)
        self.assertNotIn(target, self.case.chat._request_owners)
        self.assertFalse(requests[0]["committed"])
        self.assertTrue(self.case.chat.message_retry_state(*target)["available"])
        self.assertTrue(self.case.chat.retry(*target))
        self.case.drained()
        self.assertEqual(len(self.case.rows()[1]), 1)
        self.assertEqual(self.case.adapter.calls, 2)
        self.assertEqual(self.case.chat._request_owners, {})
        self.assertFalse(self.case.chat.message_retry_state(*target)["available"])

    def test_saved_reply_history_read_failure_releases_owner_and_reopens(self):
        from unittest.mock import patch
        from desktop_pet.conversations.store import StoreError

        chat, store = self.case.chat, self.case.store
        original_complete = store.complete_request
        original_get = store.get_conversation
        saved = [False]

        def complete(*args, **kwargs):
            reply = original_complete(*args, **kwargs)
            saved[0] = True
            return reply

        def get(conversation_id):
            if saved[0]:
                raise StoreError("OWNED_RETENTION_HISTORY_READ_FAILURE")
            return original_get(conversation_id)

        with patch.object(store, "complete_request", side_effect=complete), \
                patch.object(store, "get_conversation", side_effect=get):
            self.case.start()
            self.case.drained()
        self.assertEqual(chat.error_action, "history")
        self.assertEqual(chat._request_owners, {})
        self.assertTrue(chat.open_conversation(self.case.case.cid))
        self.case.case.wait(lambda: not chat.history_loading and not chat._runner._tasks)
        self.assertEqual(sum(row["role"] == "assistant" for row in chat.messages), 1)
        self.assertEqual(self.case.adapter.calls, 1)

    def test_saved_partial_with_unknown_closure_retains_its_coordinator_binding(self):
        fixture = self.case.case
        fixture.tree_confirmed = False
        self.case.start("agent")
        self.case.drained()
        context = self.case.chat._agent_queue._active
        self.assertIsNotNone(context)
        self.assertTrue(context.request["committed"])
        self.assertEqual(self.case.chat._request_owners, {})
        self.assertIsNone(fixture.service.worker_closure(fixture.handles[0]))
        self.assertIsNone(fixture.job().worker_closed_at)
        fixture.trees[0].confirmed = True
        fixture.service.reconcile_worker_closures(timeout_seconds=0)
        self.case.chat._pump_agent_queue()
        self.assertIsNone(self.case.chat._agent_queue._active)
        self.assertIsNotNone(fixture.job().worker_closed_at)
        self.assertNotEqual(fixture.job().state, "completed")
        self.assertEqual(fixture.calls, 1)
        self.assertEqual(len(self.case.rows()[1]), 1)
