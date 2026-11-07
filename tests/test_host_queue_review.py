"""Independent host queue composition review, with owned synthetic data only.

The tiny closure adapter below proves coordinator identity/state transitions;
it does not claim a Windows process tree was started or actually terminated.
Actual SDK serial/context and native closure checks live in other suites.
"""
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents.contracts import AgentError
from desktop_pet.agents.coordinator import HostAgentCoordinator, CoordinatorError, operation_identity
from desktop_pet.agents.job_queue import JobQueue
from desktop_pet.agents.settings import AgentSettings
from desktop_pet.conversations.store import ConversationStore, StoreError
from desktop_pet.remote.errors import RemoteError
from desktop_pet.remote.settings import RemotePreferences
from desktop_pet.remote.sync import RemoteSyncStore
from desktop_pet.settings_manager import ApiSettings


class _Clock:
    def __init__(self): self.value = 1000.0
    def __call__(self): return self.value


class _OwnedClosureFixture:
    def __init__(self): self.receipts = {}
    def close(self, handle, cid):
        self.receipts[handle] = SimpleNamespace(conversation_id=cid)
    def worker_closure(self, handle): return self.receipts.get(handle)
    def validate_worker_closure(self, handle, receipt): return self.receipts.get(handle) is receipt
    def reconcile_worker_closures(self, **_): return None


class HostQueueIndependentReviewTests(unittest.TestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="aura-host-queue-review-")
        self.addCleanup(owned.cleanup)
        self.root = Path(owned.name)
        self.clock = _Clock()
        self.store = ConversationStore(self.root / "chat.sqlite3")
        self.queue = JobQueue(self.root / "jobs.sqlite3", clock=self.clock)
        self.peer = "a" * 64
        settings = SimpleNamespace(config_path=self.root / "owned.ini", _credentials=None)
        self.preferences = RemotePreferences(settings)
        self.preferences.add_device(self.peer, "Synthetic phone", "synthetic-time")
        self.preferences.set_enabled(True)
        self.policy = {"api": ApiSettings("https://synthetic.invalid/v1", "synthetic-model", 10),
                       "key": "SYNTHETIC_REVIEW_KEY", "gui_key": "",
                       "agent": replace(AgentSettings(), enabled=True)}
        self.invalid = set()
        self.starts = []
        self.service = _OwnedClosureFixture()
        self.coordinator = self.make_coordinator()
        self.cid = str(uuid4())
        self.seed = self.store.append_message(self.cid, "user", "Synthetic previous message", "user")

    def make_coordinator(self):
        coordinator = HostAgentCoordinator(self.queue, self.store, self.service,
            configuration=lambda: self.policy, invalidated=lambda cid: cid in self.invalid,
            idle=lambda: True, allow_new=lambda _: True, start=self.started)
        coordinator.bind_authority(lambda peer, create=False:
            self.preferences.agent_authorization(peer, create=create)
            if self.preferences.enabled and self.preferences.authorized(peer) else None)
        self.assertTrue(coordinator.initialize())
        return coordinator

    def started(self, context, message, policy):
        request = {"conversation_id": message["conversation_id"], "message_id": message["id"],
                   "cancel": threading.Event()}
        self.coordinator.attach_request(context, request)
        self.starts.append((context, request, policy))

    def admit(self, text="Synthetic authorized task", request=None, cid=None, peer=None):
        return self.coordinator.admit(peer or self.peer, request or str(uuid4()), cid or self.cid, text)

    def running(self):
        ack = self.admit()
        self.coordinator.pump()
        context, request, _ = self.starts[-1]
        self.store.set_message_status(self.cid, ack["message_id"], "pending")
        self.store.reserve_agent_execution(self.cid, ack["message_id"])
        handle = object()
        self.coordinator.worker_bound(request, context, handle)
        self.coordinator.service_entered(context)
        return ack, context, request, handle

    def save_complete(self, ack, request):
        reply = self.store.complete_request(self.cid, ack["message_id"], "Synthetic saved final reply")
        request.update(result_state="completed", saved_reply_id=reply["id"])
        return reply

    def test_host_transport_peer_and_pair_incarnation_remain_immutable_across_retries(self):
        request = str(uuid4())
        first = self.admit(request=request)
        with patch.object(self.queue, "enqueue", wraps=self.queue.enqueue) as enqueue:
            self.assertEqual(first, self.admit(request=request))
        self.assertEqual(enqueue.call_count, 1)
        self.preferences.revoke(self.peer)
        self.preferences.add_device(self.peer, "Synthetic repair", "synthetic-time")
        self.assertIsNone(self.coordinator.recover_ack(self.peer, request))
        with self.assertRaisesRegex(CoordinatorError, "QUEUE_REQUEST_CONFLICT"):
            self.admit(request=request)
        self.coordinator.pump()
        self.assertEqual("blocked", self.queue.get(first["job_id"]).state)
        self.assertEqual([], self.starts)
        self.assertNotIn("agent_execution_started", self.store.read_message(self.cid, first["message_id"])["metadata"])

    def test_unpaired_origin_cannot_enroll_and_public_ack_contains_no_authority_nonce(self):
        with self.assertRaisesRegex(CoordinatorError, "QUEUE_AUTHORIZATION_REVOKED"):
            self.admit(peer="b" * 64)
        self.assertEqual((), self.queue.list_jobs())
        ack = self.admit()
        self.assertEqual({"accepted", "conversation_id", "message_id", "job_id", "queue_state",
                          "execution_mode", "reply_delivery"}, set(ack))
        row = self.queue.get(ack["job_id"])
        self.assertEqual(self.peer, row.origin_peer_id)
        self.assertEqual(self.seed["id"], row.generation)
        with sqlite3.connect(self.root / "jobs.sqlite3") as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn(self.policy["key"], dump)
        self.assertNotIn("Synthetic authorized task", dump)

    def test_settings_change_or_unavailable_policy_blocks_dispatch_without_reenrolling(self):
        for change in ("route", "key", "unavailable"):
            with self.subTest(change=change):
                original = self.policy
                ack = self.admit()
                if change == "unavailable": self.policy = None
                else:
                    self.policy = dict(original)
                    if change == "route": self.policy["api"] = replace(original["api"], model="changed-model")
                    else: self.policy["key"] = "SYNTHETIC_CHANGED_KEY"
                self.coordinator.pump()
                self.assertEqual("blocked", self.queue.get(ack["job_id"]).state)
                self.assertEqual([], self.starts)
                self.policy = original

    def test_ambiguous_enrollment_commit_recovers_exact_job_without_second_message(self):
        request = str(uuid4())
        original = self.queue.enqueue
        def commit_then_fail(reference):
            original(reference)
            raise RuntimeError("Synthetic acknowledgement loss")
        with patch.object(self.queue, "enqueue", side_effect=commit_then_fail):
            ack = self.admit(request=request)
        self.assertEqual(ack, self.coordinator.recover_ack(self.peer, request))
        self.assertEqual(ack, self.admit(request=request))
        self.assertEqual(1, len(self.queue.list_jobs()))
        self.assertEqual(2, len(self.store.load_messages(self.cid)))

    def test_failed_enrollment_cannot_undo_saved_failure_into_automatic_execution(self):
        request = str(uuid4())
        with patch.object(self.queue, "enqueue", side_effect=OSError("Synthetic before-commit failure")):
            with self.assertRaisesRegex(CoordinatorError, "QUEUE_ENROLLMENT_FAILED"):
                self.admit(request=request)
        message = self.store.read_message(self.cid, operation_identity(self.peer, request))
        self.assertEqual("failed", message["metadata"]["request_status"])
        ack = self.admit(request=request)
        self.coordinator.pump()
        self.assertEqual("blocked", self.queue.get(ack["job_id"]).state)
        self.assertEqual([], self.starts)

    def test_unconfirmed_enrollment_lookup_fails_closed_for_all_agent_effects(self):
        with patch.object(self.queue, "enqueue", side_effect=OSError("Synthetic uncertain write")), \
             patch.object(self.queue, "lookup", side_effect=OSError("Synthetic unreadable ledger")):
            with self.assertRaisesRegex(CoordinatorError, "QUEUE_ENROLLMENT_UNCONFIRMED"):
                self.admit()
        self.assertTrue(self.coordinator.effect_barrier())
        with self.assertRaisesRegex(CoordinatorError, "QUEUE_UNAVAILABLE"): self.admit()
        self.assertEqual([], self.starts)

    def test_worker_checkpoint_rechecks_live_pair_authority_after_reservation(self):
        ack = self.admit()
        self.coordinator.pump()
        context, request, _ = self.starts[-1]
        self.store.set_message_status(self.cid, ack["message_id"], "pending")
        with self.coordinator.reserve_guard(request, context, prepared=self.policy): pass
        self.store.reserve_agent_execution(self.cid, ack["message_id"])
        handle = object()
        self.coordinator.worker_bound(request, context, handle)
        self.coordinator.service_entered(context)
        with self.coordinator.dispatch_guard(request, context, handle): pass
        self.preferences.revoke(self.peer)
        self.preferences.add_device(self.peer, "Synthetic repair", "synthetic-time")
        with self.assertRaises(AgentError):
            with self.coordinator.dispatch_guard(request, context, handle):
                self.fail("A new pairing cannot authorize the old request")
        self.assertEqual("running", self.queue.get(ack["job_id"]).state)

    def test_prepared_settings_must_match_cached_host_route_before_reserving(self):
        ack = self.admit()
        self.coordinator.pump()
        context, request, _ = self.starts[-1]
        stale = dict(self.policy, api=replace(self.policy["api"], model="synthetic-stale-prepared-model"))
        with self.assertRaises(AgentError):
            with self.coordinator.reserve_guard(request, context, prepared=stale):
                self.fail("A stale prepared model route must not reserve side effects")
        self.assertNotIn("agent_execution_started", self.store.read_message(self.cid, ack["message_id"])["metadata"])

    def test_clear_revokes_old_generation_without_poisoning_new_generation(self):
        old = self.admit()
        self.coordinator.invalidate(self.cid)
        self.store.clear_messages(self.cid)
        fresh = self.admit("Synthetic task in a genuinely fresh generation")
        self.assertNotEqual(self.queue.get(old["job_id"]).generation, self.queue.get(fresh["job_id"]).generation)
        self.coordinator.pump()
        self.assertEqual("cancelled", self.queue.get(old["job_id"]).state)
        self.assertEqual("running", self.queue.get(fresh["job_id"]).state)
        self.assertEqual(1, len(self.starts))

    def test_deleted_conversation_tombstone_cannot_be_resurrected_by_admission(self):
        ack = self.admit()
        self.coordinator.invalidate(self.cid)
        self.store.delete_conversation(self.cid)
        with self.assertRaises((StoreError, CoordinatorError)):
            self.admit("Synthetic attempted resurrection")
        self.assertIsNone(self.store.get_conversation(self.cid))
        self.assertEqual("cancelled", self.queue.get(ack["job_id"]).state)
        self.assertEqual([], self.starts)

    def test_partial_saved_text_never_counts_as_completed_even_after_exact_fixture_closure(self):
        ack, context, request, handle = self.running()
        partial = self.store.append_message(self.cid, "assistant", "Synthetic partial", "assistant",
            metadata={"reply_to": ack["message_id"]})
        request.update(result_state="partial", saved_reply_id=partial["id"])
        self.service.close(handle, self.cid)
        self.coordinator.finished(context, request, {"reply": "Synthetic partial"})
        snapshot = self.queue.get(ack["job_id"])
        self.assertEqual("interrupted", snapshot.state)
        self.assertIsNone(snapshot.reply_id)
        self.assertIsNotNone(snapshot.worker_closed_at)

    def test_complete_reply_waits_for_exact_handle_closure_and_stale_callback_cannot_finish_next_job(self):
        ack, context, request, handle = self.running()
        reply = self.save_complete(ack, request)
        self.coordinator.finished(context, request, {})
        self.assertEqual("running", self.queue.get(ack["job_id"]).state)
        self.service.close(object(), self.cid)  # Another handle is never authority.
        self.coordinator.pump()
        self.assertEqual("running", self.queue.get(ack["job_id"]).state)
        self.service.close(handle, self.cid)
        self.coordinator.pump()
        self.assertEqual("completed", self.queue.get(ack["job_id"]).state)
        self.assertEqual(reply["id"], self.queue.get(ack["job_id"]).reply_id)
        next_ack = self.admit("Synthetic next task")
        self.coordinator.pump()
        self.coordinator.finished(context, request, {})
        self.assertEqual("running", self.queue.get(next_ack["job_id"]).state)
        self.assertEqual(2, len(self.starts))

    def test_claimed_or_running_restart_is_never_automatically_replayed(self):
        ack, context, request, _ = self.running()
        self.coordinator = self.make_coordinator()
        self.assertEqual("unknown", self.queue.get(ack["job_id"]).state)
        self.assertTrue(self.coordinator.effect_barrier())
        later = self.admit("Synthetic later queued task")
        self.coordinator.pump()
        self.coordinator.finished(context, request, {})  # A stale host callback cannot release restart barrier.
        self.assertEqual("unknown", self.queue.get(ack["job_id"]).state)
        self.assertEqual("queued", self.queue.get(later["job_id"]).state)
        self.assertEqual(1, len(self.starts))

    def test_cancellation_commit_order_and_fixture_closure_remain_distinct(self):
        ack, context, request, handle = self.running()
        self.assertEqual({"cancelled": False, "active_cancelled": False, "queued_cancelled_count": 0},
                         self.coordinator.cancel(self.cid, lambda _: False))
        self.assertEqual("running", self.queue.get(ack["job_id"]).state)
        def accepted_stop(_): request["cancel"].set(); return True
        self.assertEqual({"cancelled": True, "active_cancelled": True, "queued_cancelled_count": 0},
                         self.coordinator.cancel(self.cid, accepted_stop))
        self.assertEqual("cancel_requested", self.queue.get(ack["job_id"]).state)
        self.coordinator.finished(context, request, {})
        self.assertEqual("cancel_requested", self.queue.get(ack["job_id"]).state)
        self.service.close(handle, self.cid)
        self.coordinator.pump()
        self.assertEqual("cancelled", self.queue.get(ack["job_id"]).state)

    def test_phone_import_cannot_invent_saved_reply_for_existing_host_agent_user(self):
        ack = self.admit()
        original = self.store.read_message(self.cid, ack["message_id"])
        params = {"conversation_id": self.cid, "messages": [
            {"id": original["id"], "role": "user", "content": original["content"], "created_at": original["created_at"]},
            {"id": str(uuid4()), "role": "assistant", "content": "Synthetic forged complete reply",
             "created_at": original["created_at"], "reply_to": original["id"]}]}
        with self.assertRaises(RemoteError) as raised:
            RemoteSyncStore(self.store).import_exchange(self.peer, str(uuid4()), params)
        self.assertEqual("import_conflict", raised.exception.code)
        self.assertEqual(original, self.store.read_message(self.cid, ack["message_id"]))
        self.assertEqual(2, len(self.store.load_messages(self.cid)))

    def test_more_than_500_terminal_rows_cannot_hide_a_queued_task_from_cancel(self):
        for _ in range(501):
            ack = self.admit()
            self.queue.request_cancel(ack["job_id"])
        target = self.admit("Synthetic queued task after long terminal history")
        self.assertEqual({"cancelled": True, "active_cancelled": False, "queued_cancelled_count": 1},
                         self.coordinator.cancel(self.cid, lambda _: self.fail("No live task to stop")))
        self.assertEqual("cancelled", self.queue.get(target["job_id"]).state)
        self.assertEqual("cancelled", self.store.read_message(self.cid, target["message_id"])["metadata"]["request_status"])
        self.coordinator.pump()
        self.assertEqual([], self.starts)

    def test_expired_lease_late_final_reply_cannot_be_committed_as_fresh_completion(self):
        ack, context, request, handle = self.running()
        self.save_complete(ack, request)
        self.clock.value += 31
        self.queue.recover_expired()
        self.service.close(handle, self.cid)
        self.coordinator.finished(context, request, {})
        snapshot = self.queue.get(ack["job_id"])
        self.assertEqual("unknown", snapshot.state)
        self.assertIsNone(snapshot.reply_id)
        self.assertIsNotNone(snapshot.worker_closed_at)

    def test_first_worker_dispatch_must_not_accept_an_already_expired_host_lease(self):
        ack = self.admit()
        self.coordinator.pump()
        context, request, _ = self.starts[-1]
        self.store.set_message_status(self.cid, ack["message_id"], "pending")
        with self.coordinator.reserve_guard(request, context, prepared=self.policy): pass
        self.store.reserve_agent_execution(self.cid, ack["message_id"])
        handle = object()
        self.coordinator.worker_bound(request, context, handle)
        self.clock.value += 31  # No recover call: checkpoint itself must fence freshness.
        with self.assertRaises(AgentError):
            with self.coordinator.dispatch_guard(request, context, handle):
                pass


class HostQueueQtCacheReviewTests(unittest.TestCase):
    """Actual ChatController guards consume a UI-published immutable snapshot."""
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from tests import test_chat_images as fixture
        fixture.ChatImageTests.setUp(self)
        self.settings.save_api_settings(ApiSettings("https://synthetic.invalid/v1", "synthetic-cache-model", 10))
        self.settings.save_api_key("SYNTHETIC_GUARD_CACHE_KEY")
        self.settings.save_agent_settings(replace(self.settings.load_agent_settings(), enabled=True))
        self.chat.refresh_agent_settings()
        self.assertIsNotNone(self.chat._agent_queue_policy)

    def tearDown(self):
        from tests import test_chat_images as fixture
        fixture.ChatImageTests.tearDown(self)

    def rpc(self, *args, **kwargs):
        from tests import test_chat_images as fixture
        return fixture.ChatImageTests.rpc(self, *args, **kwargs)

    def test_background_reserve_and_dispatch_never_read_qsettings_or_vault(self):
        from contextlib import ExitStack
        request = {"conversation_id": self.cid, "message_id": str(uuid4()), "cancel": threading.Event()}
        errors = []
        coordinator = self.chat._agent_queue
        prepared = dict(self.chat._agent_queue_policy)
        def worker():
            try:
                with coordinator.reserve_guard(request, prepared=prepared): pass
                handle = object()
                coordinator.worker_bound(request, None, handle)
                with coordinator.dispatch_guard(request, None, handle): pass
            except BaseException as exc: errors.append(exc)
        with ExitStack() as stack:
            for method in ("load_api_settings", "get_api_key", "load_agent_settings", "get_agent_gui_api_key"):
                stack.enter_context(patch.object(self.settings, method, side_effect=AssertionError("No Qt/vault reads from guard thread")))
            thread = threading.Thread(target=worker)
            thread.start(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)

    def test_idle_uses_no_periodic_queue_timer(self):
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        self.assertIsNone(self.chat._agent_queue._active)
        self.assertFalse(self.chat._agent_queue_timer.isActive())


class HostQueueSdkCompositionReviewTests(unittest.TestCase):
    """One real isolated SDK/loopback case; no external model or real user data."""
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from agent_backend.nanobot.tests.test_worker import PYTHON
        if not PYTHON.is_file(): raise unittest.SkipTest("Isolated SDK unavailable")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from tests import test_remote_agent_queue as fixture
        fixture.RemoteAgentQueueTests.setUp(self)

    def tearDown(self):
        from tests import test_remote_agent_queue as fixture
        fixture.RemoteAgentQueueTests.tearDown(self)

    def rpc(self, *args, **kwargs):
        from tests import test_chat_images as fixture
        return fixture.ChatImageTests.rpc(self, *args, **kwargs)

    def configure(self, *args, **kwargs):
        from tests import test_remote_agent_queue as fixture
        return fixture.RemoteAgentQueueTests.configure(self, *args, **kwargs)

    def send(self, *args, **kwargs):
        from tests import test_remote_agent_queue as fixture
        return fixture.RemoteAgentQueueTests.send(self, *args, **kwargs)

    def job(self, ack): return self.chat._agent_queue.queue.get(ack["job_id"])

    def idle(self):
        from tests import test_remote_agent_queue as fixture
        fixture.RemoteAgentQueueTests.idle(self)

    def test_same_conversation_steer_is_not_a_job_and_other_conversation_waits_for_real_closure(self):
        from tests.test_remote_controller import spin_until
        entered, release = threading.Event(), threading.Event()
        def blocked_first(_):
            entered.set()
            if not release.wait(10): raise AssertionError("Synthetic planner was not released")
            return ("write_file", {"path": "late-a.txt", "content": "A"})
        reply = "Created separate-b.txt after the first task was actually stopped."
        model = self.configure([blocked_first,
            ("write_file", {"path": "separate-b.txt", "content": "B"}),
            ("read_file", {"path": "separate-b.txt"}),
            ("finish_task", {"status": "completed", "summary": reply}), "Unchecked rewrite"])
        selected = self.chat.current_conversation_id
        first = self.send('Create late-a.txt containing exactly "A".')
        try:
            spin_until(entered.is_set, timeout=10)
            status = self.rpc("status")["result"]
            self.assertTrue(status["agent_busy"])
            self.assertTrue(status["agent_available"])
            self.assertIn("agent.queue.v1", status["capabilities"])
            steering = "Synthetic status supplement; preserve the existing requested task."
            response = self.rpc("agent.steer", {"conversation_id": self.cid, "text": steering})
            self.assertTrue(response["ok"], response)
            self.assertTrue(response["result"]["accepted"])
            self.assertEqual(1, len(self.chat._agent_queue.queue.list_jobs(conversation_id=self.cid)))
            other = str(uuid4())
            second = self.rpc("agent.send", {"conversation_id": other,
                "text": 'Create separate-b.txt containing exactly "B".'})
            self.assertTrue(second["ok"], second)
            ack = second["result"]
            self.assertEqual("queued", self.job(ack).state)
            self.assertEqual(1, len(model.planner_calls))
            cancellation = self.rpc("agent.cancel", {"conversation_id": self.cid})["result"]
            self.assertTrue(cancellation["active_cancelled"])
            self.assertEqual(0, cancellation["queued_cancelled_count"])
            release.set()
            try:
                spin_until(lambda: self.job(first).state == "cancelled" and self.job(ack).state == "completed", timeout=20)
            except AssertionError:
                first_job, second_job = self.job(first), self.job(ack)
                self.fail(str({"first": (first_job.state, first_job.reason_code, first_job.worker_closed_at is not None),
                    "second": (second_job.state, second_job.reason_code, second_job.worker_closed_at is not None),
                    "planner_calls": len(model.planner_calls), "fixture_errors": model.errors,
                    "second_rows": [(row["role"], row["metadata"].get("agent_error_code"), row["content"])
                                    for row in self.chat.store.load_messages(other)]}))
            self.idle()
            self.assertIsNotNone(self.job(first).worker_closed_at)
            self.assertIsNotNone(self.job(ack).worker_closed_at)
            self.assertFalse((self.workspace / "late-a.txt").exists())
            self.assertEqual("B", (self.workspace / "separate-b.txt").read_text())
            self.assertEqual(reply, self.chat.store.load_messages(other)[-1]["content"])
            self.assertEqual(selected, self.chat.current_conversation_id)
            rows = self.chat.store.load_messages(self.cid)
            self.assertTrue(any(row["content"] == steering and row["metadata"].get("agent_steering") is True for row in rows))
            self.assertEqual(5, len(model.planner_calls))
        finally: release.set()


if __name__ == "__main__": unittest.main()
