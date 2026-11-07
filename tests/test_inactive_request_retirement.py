"""Quiescent private requests retire while public retries and unknown proofs remain."""
from contextlib import contextmanager
import gc
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4
import weakref

from desktop_pet.api_client import ApiError
from desktop_pet.conversations import StoreError
from tests.test_chat_commit_ack_recovery import ChatCommitAckRecoveryTests


class InactiveRequestRetirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ChatCommitAckRecoveryTests.setUpClass()

    def setUp(self):
        self.case = ChatCommitAckRecoveryTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.chat, self.store = self.case.chat, self.case.store

    def history(self):
        self.assertTrue(self.chat.open_conversation(self.case.case.cid))
        self.case.case.wait(lambda: not self.chat.history_loading and not self.chat._runner._tasks)

    def failed(self):
        captured = []

        def fail():
            captured.append(self.chat._active_request)
            raise ApiError("network", "OWNED_INACTIVE_NETWORK_FAILURE")
        self.case.adapter.before_return = fail
        self.assertTrue(self.chat.send("普通闲聊，记录一个固定失败。"))
        self.case.drained()
        self.case.adapter.before_return = None
        return captured[0]

    def hold(self):
        entered, release = threading.Event(), threading.Event()
        self.case.case.releases.append(release)

        def wait():
            entered.set()
            if not release.wait(5):
                raise AssertionError("OWNED_INACTIVE_ADAPTER_TIMEOUT")
        self.case.adapter.before_return = wait
        self.assertTrue(self.chat.send("普通闲聊，等待固定回复。"))
        request = self.chat._active_request
        self.case.case.wait(entered.is_set)
        return request, release

    @contextmanager
    def held_cancel_write(self, *, fail=False):
        entered, release = threading.Event(), threading.Event()
        self.case.case.releases.append(release)
        original = self.chat._submit_image_storage_task

        def submit(function, callback, **kwargs):
            if function.__name__ == "persist_cancelled":
                def held():
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("OWNED_CANCEL_WRITE_TIMEOUT")
                    if fail:
                        raise StoreError("OWNED_CANCEL_WRITE_FAILED")
                    return function()
                return original(held, callback, **kwargs)
            return original(function, callback, **kwargs)
        with patch.object(self.chat, "_submit_image_storage_task", side_effect=submit):
            yield entered, release

    def unknown(self, mode="ordinary"):
        committed = [False]
        original = self.store.load_messages

        def load(cid):
            if committed[0]:
                raise StoreError("OWNED_INACTIVE_ACK_READBACK_UNAVAILABLE")
            return original(cid)
        with self.case.commit_fault(after=lambda saved: committed.__setitem__(0, True)), \
             patch.object(self.store, "load_messages", side_effect=load):
            self.case.start(mode)
            self.case.drained()
        target = (self.case.case.cid, self.case.user_id)
        return target, self.chat._request_owners[target]

    def test_six_failed_turns_release_original_traces_and_keep_public_retries(self):
        traces, functions, targets = [], [], []

        def fail():
            request = self.chat._active_request
            targets.append((request["conversation_id"], request["message_id"]))
            traces.append(weakref.ref(request["trace"]))
            functions.append(weakref.ref(request["refresh_trace_keys"]))
            raise ApiError("network", "OWNED_SIX_FAILED_TURNS")
        self.case.adapter.before_return = fail
        for turn in range(6):
            self.assertTrue(self.chat.send("固定失败的普通闲聊 " + str(turn)))
            self.case.drained()
        gc.collect()
        self.assertEqual(self.chat._request_owners, {})
        self.assertTrue(all(reference() is None for reference in (*traces, *functions)))
        self.assertEqual(len(self.store.load_messages(self.case.case.cid)), 6)
        self.assertTrue(all(self.chat.message_retry_state(*target)["available"] for target in targets))

    def test_three_stopped_turns_release_private_objects_after_actual_drain(self):
        traces, functions, targets = [], [], []
        for turn in range(3):
            entered, release = threading.Event(), threading.Event()
            self.case.case.releases.append(release)

            def hold(entered=entered, release=release):
                request = self.chat._active_request
                targets.append((request["conversation_id"], request["message_id"]))
                traces.append(weakref.ref(request["trace"]))
                functions.append(weakref.ref(request["refresh_trace_keys"]))
                entered.set()
                if not release.wait(5):
                    raise AssertionError("OWNED_THREE_STOPS_TIMEOUT")
            self.case.adapter.before_return = hold
            self.assertTrue(self.chat.send("固定停止的普通闲聊 " + str(turn)))
            self.case.case.wait(entered.is_set)
            self.assertTrue(self.chat.stop_request())
            release.set()
            self.case.drained()
        gc.collect()
        self.assertEqual(self.chat._request_owners, {})
        self.assertTrue(all(reference() is None for reference in (*traces, *functions)))
        self.assertTrue(all(self.chat.message_retry_state(*target)["available"] for target in targets))
        self.assertTrue(all(row["metadata"]["request_status"] == "cancelled"
                            for row in self.store.load_messages(self.case.case.cid)))

    def test_cancel_write_finishing_first_cannot_retire_a_live_main_request(self):
        request, release = self.hold()
        target = (request["conversation_id"], request["message_id"])
        self.assertTrue(self.chat.stop_request())
        self.case.case.wait(lambda: self.chat._pending_cancel_count.get(target[0], 0) == 0)
        self.assertIs(self.chat._requests.get(target), request)
        self.assertIs(self.chat._request_owners.get(target), request)
        release.set()
        self.case.drained()
        self.assertNotIn(target, self.chat._request_owners)

    def test_main_finishing_first_retains_owner_until_cancel_write_callback(self):
        with self.held_cancel_write() as (write_entered, write_release):
            request, main_release = self.hold()
            target = (request["conversation_id"], request["message_id"])
            self.assertTrue(self.chat.stop_request())
            self.case.case.wait(write_entered.is_set)
            main_release.set()
            self.case.case.wait(lambda: target not in self.chat._requests)
            self.assertEqual(self.chat._pending_cancel_count[target[0]], 1)
            self.assertIs(self.chat._request_owners.get(target), request)
            write_release.set()
            self.case.drained()
        self.assertNotIn(target, self.chat._request_owners)
        self.assertEqual(self.chat._pending_cancel_count[target[0]], 0)

    def test_cancel_write_error_is_reported_before_owner_retirement(self):
        feedback = []
        with self.held_cancel_write(fail=True) as (write_entered, write_release):
            request, main_release = self.hold()
            target = (request["conversation_id"], request["message_id"])

            def error_changed(message):
                if "停止状态未能保存" in message:
                    feedback.append(self.chat._request_owners.get(target) is request)
            self.chat.error_changed.connect(error_changed)
            self.assertTrue(self.chat.stop_request())
            self.case.case.wait(write_entered.is_set)
            main_release.set()
            self.case.case.wait(lambda: target not in self.chat._requests)
            write_release.set()
            self.case.drained()
        self.assertEqual(feedback, [True])
        self.assertEqual(self.chat.error_action, "history")
        self.assertNotIn(target, self.chat._request_owners)
        self.assertTrue(self.chat.message_retry_state(*target)["available"])

    def test_unknown_save_stays_owned_through_failed_read_then_retires_after_fresh_read(self):
        target, owner = self.unknown()
        self.chat._retire_inactive_request_owners(target[0])
        self.assertIs(self.chat._request_owners.get(target), owner)
        self.assertFalse(self.chat.retry(*target))
        with patch.object(self.store, "load_messages", side_effect=StoreError("OWNED_FAILED_FRESH_READ")):
            self.history()
        self.assertIs(self.chat._request_owners.get(target), owner)
        self.assertIn(target, self.chat._unconfirmed_reply_commits)
        self.history()
        self.assertNotIn(target, self.chat._unconfirmed_reply_commits)
        self.assertNotIn(target, self.chat._request_owners)
        self.assertEqual(len(self.case.rows()[1]), 1)
        self.assertEqual(self.case.adapter.calls, 1)

    def test_candidate_keeps_owner_after_ledger_failure_and_retires_only_on_confirmation(self):
        target, owner = self.unknown("agent")
        candidate = self.chat._historical_reply_candidates[target]
        with patch.object(self.chat._agent_queue.queue, "reconcile_completed", side_effect=OSError("OWNED_LEDGER_DOWN")):
            self.history()
        self.assertNotIn(target, self.chat._unconfirmed_reply_commits)
        self.assertIs(self.chat._historical_reply_candidates.get(target), candidate)
        self.assertIs(self.chat._request_owners.get(target), owner)
        self.assertFalse(owner["committed"])
        self.history()
        self.assertNotIn(target, self.chat._historical_reply_candidates)
        self.assertNotIn(target, self.chat._request_owners)
        self.assertTrue(owner["committed"])
        self.assertEqual(self.case.case.job().state, "completed")
        self.assertEqual(owner["saved_reply_id"], self.case.case.job().reply_id)
        self.assertEqual(self.case.case.calls, 1)

    def test_ledger_commit_ack_loss_keeps_candidate_until_idempotent_confirmation(self):
        target, owner = self.unknown("agent")
        original = self.chat._agent_queue.queue.reconcile_completed

        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("OWNED_RETIREMENT_LEDGER_ACK_LOST")
        with patch.object(self.chat._agent_queue.queue, "reconcile_completed", side_effect=lost_ack):
            self.history()
        saved_job = self.case.case.job()
        self.assertEqual(saved_job.state, "completed")
        self.assertIs(self.chat._request_owners.get(target), owner)
        self.assertIn(target, self.chat._historical_reply_candidates)
        self.history()
        self.assertNotIn(target, self.chat._request_owners)
        self.assertTrue(owner["committed"])
        self.assertEqual(self.case.case.job(), saved_job)

    def test_public_retry_uses_a_new_request_after_failed_owner_has_retired(self):
        request = self.failed()
        target = (request["conversation_id"], request["message_id"])
        self.assertNotIn(target, self.chat._request_owners)
        self.assertTrue(self.chat.retry(*target))
        self.assertIsNot(self.chat._active_request, request)
        self.case.drained()
        rows = self.store.load_messages(target[0])
        self.assertEqual(sum(row["role"] == "user" for row in rows), 1)
        self.assertEqual(sum(row["role"] == "assistant" for row in rows), 1)
        self.assertFalse(self.chat.message_retry_state(*target)["available"])
        self.assertEqual(self.chat._request_owners, {})

    def test_cid_sweep_keeps_a_new_live_request_and_other_cid_owners(self):
        old = self.failed()
        old_target = (old["conversation_id"], old["message_id"])
        other_cid = str(uuid4())
        self.chat._request_owners[(other_cid, old_target[1])] = old
        request, release = self.hold()
        target = (request["conversation_id"], request["message_id"])
        self.chat._retire_inactive_request_owners(target[0])
        self.assertIs(self.chat._request_owners.get(target), request)
        self.assertIs(self.chat._request_owners.get((other_cid, old_target[1])), old)
        release.set()
        self.case.drained()
        self.assertNotIn(target, self.chat._request_owners)

    def test_sweep_cannot_remove_a_replaced_owner_from_its_captured_snapshot(self):
        old = self.failed()
        target = (old["conversation_id"], old["message_id"])
        replacement = dict(old)

        class ReplacedOwners(dict):
            def items(self):
                snapshot = tuple(super().items())
                self[target] = replacement
                return snapshot
        self.chat._request_owners = ReplacedOwners({target: old})
        self.chat._retire_inactive_request_owners(target[0])
        self.assertIs(self.chat._request_owners.get(target), replacement)
        self.chat._request_owners = dict(self.chat._request_owners)


if __name__ == "__main__":
    unittest.main()
