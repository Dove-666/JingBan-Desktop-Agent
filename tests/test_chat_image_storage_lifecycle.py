"""Hidden actual Controller/SQLite with held in-memory work, never a provider."""
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QTest, QSignalSpy

from desktop_pet.api_client import ApiClient, ApiError, ChatReply
from desktop_pet.agents import AgentSettings
from desktop_pet.agents.coordinator import CoordinatorError
from desktop_pet.image_storage import ImageStorageGate, ImageStorageGateError
from tests import test_chat_controller as support
from tests.test_agent_chat_routing import FakeAgent


class CountedSettings(support.MemorySettings):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.agent = replace(AgentSettings(), enabled=False)

    def load_api_settings(self):
        self.reads.append("settings")
        return super().load_api_settings()

    def get_api_key(self):
        self.reads.append("key")
        return super().get_api_key()

    def load_agent_settings(self):
        return self.agent


class HeldClient(ApiClient):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()
        self.calls = 0
        self.failure = None

    def complete_context(self, settings, key, messages, **options):
        self.calls += 1
        self.entered.set()
        try:
            if not self.release.wait(5):
                raise AssertionError("OWNED_CLIENT_NOT_RELEASED")
            if self.failure is not None:
                raise self.failure
            return ChatReply("owned final reply", "happy")
        finally:
            self.done.set()

    def shutdown(self):
        # Deliberately require explicit release: close is not proof of drain.
        super().shutdown()


class ChatImageStorageLifecycleTests(unittest.TestCase):
    setUpClass = classmethod(support.ChatControllerTests.setUpClass.__func__)
    spin_until = support.ChatControllerTests.spin_until

    def setUp(self):
        self.controllers = []
        self.clients = []
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for client in self.clients:
            client.release.set()
        for controller in self.controllers:
            controller.shutdown()
        self.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        QTest.qWait(10)

    def make_chat(self, *, agent=None):
        settings, client = CountedSettings(), HeldClient()
        if agent is not None:
            settings.agent = replace(settings.agent, enabled=True)
            self.release_events.append(agent.release)
        chat = support.ChatController(settings, client, metadata_client_factory=lambda: None, agent_service=agent)
        self.controllers.append(chat)
        self.clients.append(client)
        self.spin_until(lambda: not chat._history_loading and not chat._runner._tasks)
        # Before-only compatibility permits the old Controller to demonstrate
        # that it ignored a real gate held by a future maintenance caller.
        if not hasattr(chat, "image_storage_gate"):
            chat.image_storage_gate = ImageStorageGate()
        settings.reads.clear()
        return chat, settings, client

    def hold_maintenance(self, chat):
        gate = chat.image_storage_gate
        owner = gate.create_owner()
        return gate, gate.maintenance(owner)

    def assert_blocked(self, gate):
        with self.assertRaises(ImageStorageGateError) as caught:
            gate.maintenance(gate.create_owner())
        self.assertEqual(caught.exception.code, "IMAGE_STORAGE_IN_USE")

    def test_maintenance_send_rejects_before_configuration_or_optimistic_message(self):
        chat, settings, client = self.make_chat()
        gate, maintenance = self.hold_maintenance(chat)
        before = chat.messages
        try:
            accepted = chat.send("owned unsent text")
            self.assertFalse(accepted)
            self.assertEqual(settings.reads, [])
            self.assertEqual(chat.messages, before)
            self.assertEqual(client.calls, 0)
            self.assertEqual(chat.error, "图片存储正在维护，请稍后重试。")
        finally:
            gate.release(maintenance)

    def test_maintenance_retry_rejects_before_configuration_or_status_change(self):
        chat, settings, client = self.make_chat()
        client.failure = ApiError("network", "owned network failure")
        client.release.set()
        self.assertTrue(chat.send("owned retry message"))
        self.spin_until(lambda: not chat.busy and not chat._requests)
        before = chat.messages
        settings.reads.clear()
        gate, maintenance = self.hold_maintenance(chat)
        try:
            self.assertFalse(chat.retry())
            self.assertEqual(settings.reads, [])
            self.assertEqual(chat.messages, before)
            self.assertEqual(client.calls, 1)
        finally:
            gate.release(maintenance)

    def test_remote_admission_rejects_before_policy_or_archive(self):
        chat, settings, client = self.make_chat()
        gate, maintenance = self.hold_maintenance(chat)
        try:
            with self.assertRaises(CoordinatorError) as caught:
                chat.admit_remote_agent("a" * 64, str(uuid4()), chat.current_conversation_id, "owned queued task")
            self.assertEqual(caught.exception.code, "QUEUE_BUSY")
            self.assertEqual(settings.reads, [])
            self.assertEqual(chat.messages, [])
            self.assertEqual(client.calls, 0)
        finally:
            gate.release(maintenance)

    def test_maintenance_pump_does_not_capture_policy_or_claim(self):
        chat, settings, client = self.make_chat()
        gate, maintenance = self.hold_maintenance(chat)
        try:
            with patch.object(chat._agent_queue, "pump", wraps=chat._agent_queue.pump) as pump:
                chat._pump_agent_queue()
            self.assertEqual(pump.call_count, 0)
            self.assertEqual(settings.reads, [])
            self.assertEqual(client.calls, 0)
        finally:
            gate.release(maintenance)

    def test_stop_and_idle_busy_flag_do_not_release_held_physical_request(self):
        chat, _settings, client = self.make_chat()
        self.assertTrue(chat.send("owned held request"))
        self.spin_until(client.entered.is_set)
        self.assertTrue(chat.stop_request())
        self.assertFalse(chat.busy)
        self.assert_blocked(chat.image_storage_gate)
        client.release.set()
        self.spin_until(lambda: not chat._requests and not chat._pending_cancel_count.get(chat.current_conversation_id))
        self.spin_until(lambda: chat.image_storage_gate.snapshot()["activity_count"] == 0)
        self.assertFalse(any(message["role"] == "assistant" for message in chat.messages))

    def test_physical_return_keeps_blocker_until_actual_queued_ui_completion(self):
        chat, _settings, client = self.make_chat()
        self.assertTrue(chat.send("owned callback gap"))
        self.spin_until(client.entered.is_set)
        client.release.set()
        # No Qt event pump: wait for the physical function and TaskRunner drain.
        self.assertTrue(QThreadPool.globalInstance().waitForDone(4000))
        self.assertTrue(client.done.is_set())
        self.assertTrue(chat._requests)
        self.assert_blocked(chat.image_storage_gate)
        self.spin_until(lambda: not chat._requests)
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)

    def test_shutdown_idle_notifies_and_running_shutdown_waits_for_real_finally(self):
        chat, _settings, client = self.make_chat()
        signal = getattr(chat, "image_storage_changed", None)
        self.assertIsNotNone(signal)
        changed = QSignalSpy(signal)
        self.assertTrue(chat.send("owned shutdown request"))
        self.spin_until(client.entered.is_set)
        before = changed.count()
        chat.shutdown()
        self.assertGreater(changed.count(), before)
        self.assert_blocked(chat.image_storage_gate)
        client.release.set()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(4000))
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)

    def test_normal_reply_releases_after_ui_and_property_is_readonly(self):
        chat, _settings, client = self.make_chat()
        gate = chat.image_storage_gate
        with self.assertRaises(AttributeError):
            chat.image_storage_gate = ImageStorageGate()
        self.assertIs(chat.image_storage_gate, gate)
        self.assertTrue(chat.send("owned normal message"))
        self.spin_until(client.entered.is_set)
        self.assert_blocked(gate)
        client.release.set()
        self.spin_until(lambda: not chat._requests and not chat.busy)
        self.assertEqual(gate.snapshot()["activity_count"], 0)
        self.assertEqual([row["role"] for row in chat.store.load_messages(chat.current_conversation_id)],
                         ["user", "assistant"])
        self.assertEqual(client.calls, 1)
        self.assertEqual(chat._request_owners, {})  # completed requests release private trace state
        maintenance = gate.maintenance(gate.create_owner())
        gate.release(maintenance)

    def test_idle_pump_does_not_emit_image_storage_or_agent_work_notifications(self):
        chat, _settings, _client = self.make_chat()
        changed = QSignalSpy(chat.image_storage_changed)
        work = QSignalSpy(chat.work_active_changed)
        for _ in range(4):
            chat._pump_agent_queue()
        self.assertEqual(changed.count(), 0)
        self.assertEqual(work.count(), 0)
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)

    def test_idle_shutdown_always_notifies(self):
        chat, _settings, _client = self.make_chat()
        changed = QSignalSpy(chat.image_storage_changed)
        self.assertFalse(chat.busy)
        chat.shutdown()
        self.assertEqual(changed.count(), 1)
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)

    def test_closed_remote_admission_does_not_read_configuration(self):
        chat, settings, client = self.make_chat()
        chat.shutdown()
        settings.reads.clear()
        with self.assertRaises(CoordinatorError) as caught:
            chat.admit_remote_agent("a" * 64, str(uuid4()), chat.current_conversation_id, "owned closed request")
        self.assertEqual(caught.exception.code, "QUEUE_UNAVAILABLE")
        self.assertEqual(settings.reads, [])
        self.assertEqual(client.calls, 0)

    def test_rejected_input_and_missing_configuration_do_not_leave_admission_tokens(self):
        chat, settings, _client = self.make_chat()
        for text in ("", "x" * 8001):
            self.assertFalse(chat.send(text))
            self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)
        settings.key = ""
        self.assertFalse(chat.send("owned missing configuration"))
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)

    def test_submit_none_unwinds_callback_lifetime_and_keeps_one_retryable_user(self):
        chat, _settings, client = self.make_chat()
        with patch.object(chat._runner, "submit", return_value=None):
            self.assertTrue(chat.send("owned not scheduled"))
        self.assert_blocked(chat.image_storage_gate)  # queued failure acknowledgement
        self.spin_until(lambda: not chat._requests and not chat.busy)
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)
        self.assertEqual(client.calls, 0)
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(chat.messages[0]["metadata"]["request_status"], "failed")
        client.release.set()
        self.assertTrue(chat.retry())
        self.spin_until(lambda: not chat._requests)
        self.assertEqual([row["role"] for row in chat.store.load_messages(chat.current_conversation_id)],
                         ["user", "assistant"])
        self.assertEqual(client.calls, 1)

    def test_actual_task_start_raise_before_entry_cancels_late_runnable_without_api(self):
        chat, _settings, client = self.make_chat()
        held = []
        def reject(worker):
            held.append(worker)
            raise RuntimeError("SYNTHETIC_PRIVATE_START_ERROR")
        with patch.object(QThreadPool.globalInstance(), "start", side_effect=reject):
            self.assertTrue(chat.send("owned rejected start"))
        self.spin_until(lambda: not chat._requests and not chat.busy)
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)
        self.assertEqual(client.calls, 0)
        self.assertNotIn("SYNTHETIC_PRIVATE", chat.error)
        # TaskRunner conservatively retains a rejected runnable. If Qt had
        # queued it before throwing, its real late entry must perform no work.
        self.assertEqual(len(held), 1)
        held[0].run()
        self.spin_until(lambda: not chat._runner._tasks)
        self.assertEqual(client.calls, 0)
        self.assertEqual(chat.store.load_messages(chat.current_conversation_id), [])

    def test_actual_task_start_raise_after_entry_waits_for_real_result_and_does_not_replay(self):
        chat, _settings, client = self.make_chat()
        pool = QThreadPool.globalInstance()
        original = pool.start
        def accepted_then_raise(worker):
            original(worker)
            if not client.entered.wait(3):
                raise AssertionError("OWNED_WORK_NOT_ENTERED")
            raise RuntimeError("SYNTHETIC_PRIVATE_AFTER_ACCEPT")
        with patch.object(pool, "start", side_effect=accepted_then_raise):
            self.assertTrue(chat.send("owned accepted start exception"))
        self.assertTrue(chat._requests)
        self.assert_blocked(chat.image_storage_gate)
        self.assertEqual(client.calls, 1)
        client.release.set()
        self.spin_until(lambda: not chat._requests and not chat.busy)
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)
        self.assertEqual(client.calls, 1)
        self.assertNotIn("SYNTHETIC_PRIVATE", chat.error)
        self.assertFalse(any(row["role"] == "assistant" for row in chat.messages))

    def test_accepted_but_not_yet_entered_request_archives_on_shutdown(self):
        chat, _settings, client = self.make_chat()
        pool, held = QThreadPool.globalInstance(), []
        original = pool.start
        with patch.object(pool, "start", side_effect=lambda worker: held.append(worker)):
            self.assertTrue(chat.send("owned accepted pending archive"))
        self.assertEqual(client.calls, 0)
        chat.shutdown()
        self.assert_blocked(chat.image_storage_gate)
        self.assertEqual(len(held), 1)
        original(held[0])
        self.assertTrue(pool.waitForDone(4000))
        messages = chat.store.load_messages(chat.current_conversation_id)
        self.assertEqual([row["content"] for row in messages], ["owned accepted pending archive"])
        self.assertEqual(messages[0]["metadata"]["request_status"], "cancelled")
        self.assertEqual(client.calls, 0)
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)

    def test_cancel_archive_write_outlives_primary_reply_worker(self):
        chat, _settings, client = self.make_chat()
        self.assertTrue(chat.send("owned stop with held archive"))
        self.spin_until(client.entered.is_set)
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original = chat.store.set_message_status
        def save(cid, mid, status, **options):
            if status == "cancelled" and threading.current_thread().name != "MainThread":
                if not entered.is_set():
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("OWNED_CANCEL_WRITE_NOT_RELEASED")
            return original(cid, mid, status, **options)
        with patch.object(chat.store, "set_message_status", side_effect=save):
            self.assertTrue(chat.stop_request())
            self.spin_until(entered.is_set)
            client.release.set()
            self.spin_until(client.done.is_set)
            self.assert_blocked(chat.image_storage_gate)
            release.set()
            self.spin_until(lambda: not chat._requests and not chat._pending_cancel_count.get(chat.current_conversation_id))
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)

    def test_worker_exception_keeps_ui_token_until_failure_callback(self):
        chat, _settings, _client = self.make_chat()
        results = []
        def fail():
            raise OSError("SYNTHETIC_PRIVATE_WORK_ERROR")
        self.assertTrue(chat._submit_image_storage_task(fail, lambda value, error: results.append((value, error))))
        self.assertTrue(QThreadPool.globalInstance().waitForDone(4000))
        self.assert_blocked(chat.image_storage_gate)
        self.spin_until(lambda: bool(results))
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)
        self.assertNotIn("SYNTHETIC_PRIVATE", results[0][1])

    def test_normal_direct_agent_uses_same_lifetime_without_primary_api_call(self):
        agent = FakeAgent()
        agent.hold = True
        chat, _settings, client = self.make_chat(agent=agent)
        with patch.object(chat, "agent_readiness", return_value={"available": True}):
            self.assertTrue(chat.send("owned direct agent task", agent_mode=True))
        self.spin_until(agent.started.is_set)
        self.assert_blocked(chat.image_storage_gate)
        self.assertEqual(client.calls, 0)
        agent.release.set()
        self.spin_until(lambda: not chat._requests)
        self.assertEqual(chat.image_storage_gate.snapshot()["activity_count"], 0)
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(len(chat.messages), 2)

    def test_real_queue_remains_queued_during_maintenance_then_hands_off_without_gap(self):
        agent = FakeAgent()
        agent.hold = True
        chat, _settings, client = self.make_chat(agent=agent)
        chat.bind_remote_agent_authority(lambda peer, create=False: "b" * 64)
        ack = chat.admit_remote_agent("a" * 64, str(uuid4()), chat.current_conversation_id, "owned queue task")
        gate, maintenance = self.hold_maintenance(chat)
        try:
            chat._pump_agent_queue()
            self.assertEqual(chat._agent_queue.queue.get(ack["job_id"]).state, "queued")
            self.assertEqual(agent.calls, [])
        finally:
            gate.release(maintenance)
        start = chat._agent_queue.start
        handoff = []
        def begin(*args):
            self.assert_blocked(gate)  # pump token, before request token exists
            result = start(*args)
            handoff.append(gate.snapshot()["activity_count"])
            return result
        with patch.object(chat._agent_queue, "start", side_effect=begin):
            chat._pump_agent_queue()
        self.spin_until(agent.started.is_set)
        self.assertTrue(handoff and handoff[0] >= 3)
        self.assert_blocked(gate)
        agent.release.set()
        self.spin_until(lambda: not chat._requests)
        self.assertEqual(gate.snapshot()["activity_count"], 0)
        self.assertEqual(client.calls, 0)
        self.assertEqual(len(agent.calls), 1)
        # FakeAgent launches no process and supplies no Service closure proof.
        # The queue therefore stays conservative; gate idle is not that proof.
        self.assertTrue(chat._agent_queue.queue.has_image_maintenance_blocker())


if __name__ == "__main__":
    unittest.main()
