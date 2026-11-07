"""Independent no-launch negatives and one real SDK cancellation composition."""
from dataclasses import replace
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import AgentError, AgentRequest, AgentService, AgentSettings
from desktop_pet.agents.execution_attempt import ExecutionAttempts, NoWorkerLaunchReceipt


class NoLaunchIdentityIndependentReviewTests(unittest.TestCase):
    def test_cloned_receipt_cannot_be_substituted_for_issued_receipt_or_other_attempt(self):
        registry = ExecutionAttempts()
        attempt = registry.begin(str(uuid4())); owner = object()
        self.assertTrue(registry.bind(attempt, owner)); registry.finish(attempt)
        real = registry.receipt(attempt)
        clone = object.__new__(NoWorkerLaunchReceipt)
        for name in ("_issuer", "_attempt"): object.__setattr__(clone, name, getattr(real, name))
        self.assertFalse(registry.validates(attempt, clone, owner=owner))
        another = registry.begin(attempt.conversation_id); registry.bind(another, object()); registry.finish(another)
        self.assertFalse(registry.validates(another, real, owner=owner))
        self.assertTrue(registry.validates(attempt, real, owner=owner))

    def test_racing_finish_and_launch_commit_cannot_produce_a_receipt_and_successful_commit(self):
        for _ in range(20):
            registry = ExecutionAttempts(); attempt = registry.begin(str(uuid4()))
            barrier = threading.Barrier(2); committed = []
            def launch():
                barrier.wait()
                try: registry.mark_launch_attempted(attempt); committed.append(True)
                except ValueError: committed.append(False)
            thread = threading.Thread(target=launch); thread.start()
            barrier.wait(); registry.finish(attempt); thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual([False] if registry.receipt(attempt) is not None else [True], committed)

    def test_final_seal_failure_preserves_original_exception_and_cannot_issue_no_launch(self):
        service = AgentService(); self.addCleanup(service.shutdown)
        handles = []
        original = AgentError("cancelled", "Synthetic original callback cancellation")
        def callback(attempt): handles.append(attempt); raise original
        with patch.object(service._execution_attempts, "finish", side_effect=RuntimeError("Synthetic seal unavailable")), \
             patch("desktop_pet.agents.service.subprocess.Popen", wraps=subprocess.Popen) as popen:
            with self.assertRaises(AgentError) as caught:
                service.execute(replace(AgentSettings(), python_executable=sys.executable), "synthetic-key",
                    AgentRequest("Synthetic cancelled invocation", "agent"), {"conversation_id": str(uuid4())},
                    execution_attempt_callback=callback)
        self.assertIs(original, caught.exception)
        popen.assert_not_called()
        self.assertIsNone(service.execution_no_launch(handles[0]))

    def test_terminal_bound_keeps_all_active_identities_including_an_attempted_unfinished_call(self):
        registry = ExecutionAttempts()
        active = registry.begin(str(uuid4())); launched = registry.begin(str(uuid4()))
        registry.mark_launch_attempted(launched)
        for _ in range(150): registry.finish(registry.begin(str(uuid4())))
        self.assertTrue(registry.valid(active)); self.assertTrue(registry.valid(launched))
        self.assertIsNone(registry.receipt(active)); self.assertIsNone(registry.receipt(launched))
        registry.finish(launched)
        self.assertIsNone(registry.receipt(launched))
        registry.finish(active)
        self.assertIsNotNone(registry.receipt(active))


class NoLaunchSdkCancellationIndependentReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_host_queue_review import HostQueueSdkCompositionReviewTests
        HostQueueSdkCompositionReviewTests.setUpClass.__func__(cls)

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

    def test_cancel_before_launch_waits_for_finished_invocation_then_next_cid_really_executes(self):
        from tests.test_remote_controller import spin_until
        from PySide6.QtCore import QThreadPool
        reply = "Created no-launch-next.txt with actual B."
        model = self.configure([("write_file", {"path": "no-launch-next.txt", "content": "B"}),
            ("read_file", {"path": "no-launch-next.txt"}),
            ("finish_task", {"status": "completed", "summary": reply}), "Unchecked final"])
        entered, release = threading.Event(), threading.Event()
        service = self.chat._agent_service; original = service._context
        def validation(*args, **kwargs):
            if args[0].get("conversation_id") == self.cid:
                entered.set()
                if not release.wait(10): raise AssertionError("Owned prelaunch validation was not released")
            return original(*args, **kwargs)
        with patch.object(service, "_context", side_effect=validation), \
             patch("desktop_pet.agents.service.subprocess.Popen", wraps=subprocess.Popen) as actual_launch:
            first = self.send('Create no-launch-a.txt containing exactly "A".')
            try:
                spin_until(entered.is_set, timeout=8)
                context = self.chat._agent_queue._active
                self.assertIsNotNone(context.attempt)
                self.assertIsNone(service.execution_no_launch(context.attempt))
                other = str(uuid4())
                response = self.rpc("agent.send", {"conversation_id": other,
                    "text": 'Create no-launch-next.txt containing exactly "B".'})
                self.assertTrue(response["ok"], response); second = response["result"]
                cancellation = self.rpc("agent.cancel", {"conversation_id": self.cid})["result"]
                self.assertTrue(cancellation["active_cancelled"])
                self.assertEqual("cancel_requested", self.job(first).state)
                self.assertIsNone(self.job(first).worker_closed_at)
                self.assertEqual("queued", self.job(second).state)
                self.assertEqual(0, actual_launch.call_count)
                release.set()
                spin_until(lambda: self.job(first).state == "cancelled" and self.job(second).state == "completed", timeout=18)
                spin_until(lambda: not self.chat.busy and not QThreadPool.globalInstance().activeThreadCount(), timeout=8)
                self.assertEqual(1, actual_launch.call_count)
                self.assertIsNone(context.handle)
                self.assertIsNotNone(service.execution_no_launch(context.attempt))
                self.assertIsNotNone(self.job(first).worker_closed_at)
                self.assertIsNone(self.job(first).reply_id)
                self.assertEqual(4, len(model.planner_calls))
                self.assertFalse((self.workspace / "no-launch-a.txt").exists())
                self.assertEqual("B", (self.workspace / "no-launch-next.txt").read_text())
                self.assertEqual(reply, self.chat.store.load_messages(other)[-1]["content"])
                self.assertFalse(self.chat._agent_queue.effect_barrier())
            finally: release.set()


if __name__ == "__main__": unittest.main()
