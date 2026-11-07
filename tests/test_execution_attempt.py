"""No-launch proof identity/state negatives; synthetic and owned host data only."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import AgentService, AgentSettings, AgentRequest, AgentError
from desktop_pet.agents.execution_attempt import ExecutionAttempts, HostExecutionAttempt, NoWorkerLaunchReceipt
from tests import test_host_queue_review as queue_fixture


class ExecutionAttemptIdentityTests(unittest.TestCase):
    def test_only_finished_never_attempted_invocation_can_issue_receipt(self):
        registry = ExecutionAttempts()
        attempt = registry.begin(str(uuid4()))
        owner = object()
        self.assertTrue(registry.bind(attempt, owner))
        self.assertFalse(registry.bind(attempt, owner))
        self.assertIsNone(registry.receipt(attempt))
        self.assertFalse(registry.validates(attempt, {"not_launched": True}, owner=owner))
        registry.finish(attempt)
        receipt = registry.receipt(attempt)
        self.assertTrue(registry.validates(attempt, receipt, owner=owner))
        self.assertFalse(registry.validates(attempt, receipt, owner=object()))
        self.assertFalse(registry.valid(attempt))
        self.assertFalse(registry.bind(attempt, object()))
        registry.finish(attempt)
        self.assertIs(receipt, registry.receipt(attempt))
        with self.assertRaises(ValueError): registry.mark_launch_attempted(attempt)

    def test_attempted_launch_is_irreversible_even_without_worker_handle(self):
        registry = ExecutionAttempts()
        attempt = registry.begin(str(uuid4()))
        registry.mark_launch_attempted(attempt)
        with self.assertRaises(ValueError): registry.mark_launch_attempted(attempt)
        self.assertIsNone(registry.receipt(attempt))
        registry.finish(attempt)
        self.assertIsNone(registry.receipt(attempt))
        self.assertFalse(registry.validates(attempt, {"not_launched": True}))

    def test_foreign_cloned_serialized_or_other_attempt_receipt_is_rejected(self):
        registry, foreign = ExecutionAttempts(), ExecutionAttempts()
        attempt = registry.begin(str(uuid4()))
        registry.finish(attempt)
        receipt = registry.receipt(attempt)
        another = registry.begin(attempt.conversation_id)
        registry.finish(another)
        self.assertFalse(registry.validates(another, receipt))
        self.assertFalse(foreign.validates(attempt, receipt))
        self.assertIsNone(registry.receipt({"conversation_id": attempt.conversation_id}))
        clone = object.__new__(HostExecutionAttempt)
        for field in ("_issuer", "_nonce", "_conversation_id"):
            object.__setattr__(clone, field, getattr(attempt, field))
        self.assertFalse(registry.validates(clone, receipt))
        with self.assertRaises(TypeError): HostExecutionAttempt()
        with self.assertRaises(TypeError): NoWorkerLaunchReceipt()
        with self.assertRaises(TypeError): json.dumps(receipt)
        with self.assertRaises(AttributeError): attempt._nonce = "replaced"
        with self.assertRaises(AttributeError): receipt._attempt = another

    def test_terminal_history_is_bounded_but_unfinished_attempts_are_never_pruned(self):
        registry = ExecutionAttempts()
        open_attempts = [registry.begin(str(uuid4())) for _ in range(3)]
        first = None
        for index in range(500):
            handle = registry.begin(str(uuid4()))
            if index == 0: first = handle
            if index % 2: registry.mark_launch_attempted(handle)
            registry.finish(handle)
        self.assertEqual(131, len(registry._attempts))
        self.assertIsNone(registry.receipt(first))
        self.assertTrue(all(registry.valid(handle) for handle in open_attempts))
        self.assertTrue(all(registry.receipt(handle) is None for handle in open_attempts))


class ServiceNoLaunchTests(unittest.TestCase):
    def setUp(self):
        self.service = AgentService()
        self.addCleanup(self.service.shutdown)
        self.settings = replace(AgentSettings(), enabled=True, python_executable=sys.executable)
        self.cid = str(uuid4())
        self.context = {"conversation_id": self.cid,
                        "api": {"base_url": "https://synthetic.invalid/v1", "model": "synthetic-only"},
                        "messages": []}
        self.request = AgentRequest("Synthetic proof-boundary task", "agent")

    def invoke(self, **kwargs):
        return self.service.execute(self.settings, "synthetic-local-key", self.request, self.context, **kwargs)

    def test_actual_missing_interpreter_validation_issues_only_finished_no_launch_proof(self):
        with tempfile.TemporaryDirectory(prefix="no-launch-") as owned:
            self.settings = replace(self.settings, python_executable=str(Path(owned) / "python-missing.exe"))
            handles = []
            def bound(attempt):
                handles.append(attempt)
                self.assertIsNone(self.service.execution_no_launch(attempt))
            with patch("desktop_pet.agents.service.subprocess.Popen", wraps=subprocess.Popen) as popen:
                with self.assertRaises(AgentError) as raised: self.invoke(execution_attempt_callback=bound)
            self.assertEqual("not_configured", raised.exception.code)
            popen.assert_not_called()
            proof = self.service.execution_no_launch(handles[0])
            self.assertTrue(self.service.validate_execution_no_launch(handles[0], proof))
            self.assertFalse(self.service.validate_execution_attempt(handles[0]))

    def test_callback_exception_keeps_original_object_and_no_launch_requires_exit(self):
        original = AgentError("cancelled", "Fixed synthetic cancellation")
        handles = []
        def bound(attempt):
            handles.append(attempt)
            self.assertIsNone(self.service.execution_no_launch(attempt))
            raise original
        with patch("desktop_pet.agents.service.subprocess.Popen", wraps=subprocess.Popen) as popen:
            with self.assertRaises(AgentError) as raised: self.invoke(execution_attempt_callback=bound)
        self.assertIs(original, raised.exception)
        popen.assert_not_called()
        self.assertTrue(self.service.validate_execution_no_launch(handles[0], self.service.execution_no_launch(handles[0])))

    def test_already_cancelled_invocation_keeps_cancel_error_and_can_prove_no_launch(self):
        event = threading.Event(); event.set()
        handles = []
        with patch("desktop_pet.agents.service.subprocess.Popen", wraps=subprocess.Popen) as popen:
            with self.assertRaises(AgentError) as raised:
                self.invoke(cancellation_event=event, execution_attempt_callback=handles.append)
        self.assertEqual("cancelled", raised.exception.code)
        popen.assert_not_called()
        self.assertIsNotNone(self.service.execution_no_launch(handles[0]))

    def test_paused_inflight_validation_cannot_issue_proof_or_free_execution_slot(self):
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        handles, errors = [], []
        original = self.service._context
        def validate(*args):
            entered.set()
            if not release.wait(5): raise AssertionError("Synthetic validation was never released")
            return original(*args)
        def invoke():
            try: self.invoke(cancellation_event=cancelled, execution_attempt_callback=handles.append)
            except BaseException as exc: errors.append(exc)
        with patch.object(self.service, "_context", side_effect=validate), \
             patch("desktop_pet.agents.service.subprocess.Popen", wraps=subprocess.Popen) as popen:
            thread = threading.Thread(target=invoke)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertIsNone(self.service.execution_no_launch(handles[0]))
                self.assertTrue(self.service._execution_lock.locked())
                cancelled.set()
            finally:
                release.set(); thread.join(5)
            self.assertFalse(thread.is_alive())
            popen.assert_not_called()
        self.assertEqual("cancelled", errors[0].code)
        self.assertIsNotNone(self.service.execution_no_launch(handles[0]))

    def test_popen_raised_still_denies_no_launch_and_hides_private_error(self):
        handles = []
        with patch("desktop_pet.agents.service.subprocess.Popen", side_effect=OSError("SYNTHETIC_PRIVATE_ERROR")) as popen:
            with self.assertRaises(AgentError) as raised: self.invoke(execution_attempt_callback=handles.append)
        self.assertEqual(1, popen.call_count)
        self.assertEqual("backend_failed", raised.exception.code)
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertIsNone(self.service.execution_no_launch(handles[0]))

    def test_real_process_returned_before_register_failure_still_denies_no_launch(self):
        handles, processes = [], []
        original = subprocess.Popen
        def owned_launch(*args, **kwargs):
            process = original([sys.executable, "-I", "-u", "-c", "import time; time.sleep(15)"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            processes.append(process)
            return process
        try:
            with patch("desktop_pet.agents.service.subprocess.Popen", side_effect=owned_launch), \
                 patch.object(self.service._worker_lifecycle, "register", side_effect=ValueError("SYNTHETIC_PRIVATE_REGISTER")):
                with self.assertRaises(AgentError): self.invoke(execution_attempt_callback=handles.append)
            self.assertEqual(1, len(processes))
            self.assertIsNone(self.service.execution_no_launch(handles[0]))
        finally:
            for process in processes:
                if process.poll() is None: process.kill()
                process.wait(timeout=5)
                process.stdin.close(); process.stdout.close()

    def test_real_worker_callback_failure_uses_existing_exact_closure_not_no_launch(self):
        attempts, workers = [], []
        original = AgentError("cancelled", "Fixed synthetic lifecycle cancellation")
        def bound(handle): workers.append(handle); raise original
        with self.assertRaises(AgentError) as raised:
            self.invoke(execution_attempt_callback=attempts.append, worker_lifecycle_callback=bound)
        self.assertIs(original, raised.exception)
        self.assertEqual(1, len(workers))
        self.assertIsNone(self.service.execution_no_launch(attempts[0]))
        receipt = self.service.worker_closure(workers[0])
        self.assertTrue(self.service.validate_worker_closure(workers[0], receipt))

    def test_no_launch_receipt_cannot_discard_another_unresolved_registry(self):
        self.service._processes[1234567] = (threading.Event(), None)
        handles = []
        try:
            with self.assertRaises(AgentError) as raised: self.invoke(execution_attempt_callback=handles.append)
            self.assertEqual("busy", raised.exception.code)
            self.assertIsNotNone(self.service.execution_no_launch(handles[0]))
            self.assertIn(1234567, self.service._processes)
        finally:
            self.service._processes.clear()


class CoordinatorNoLaunchTests(unittest.TestCase):
    make_coordinator = queue_fixture.HostQueueIndependentReviewTests.make_coordinator
    started = queue_fixture.HostQueueIndependentReviewTests.started
    admit = queue_fixture.HostQueueIndependentReviewTests.admit

    def setUp(self):
        queue_fixture.HostQueueIndependentReviewTests.setUp(self)
        self.service = AgentService()
        self.coordinator.service = self.service
        self.addCleanup(self.service.shutdown)

    def begin(self):
        ack = self.admit()
        self.coordinator.pump()
        context, request, _ = self.starts[-1]
        self.store.set_message_status(self.cid, ack["message_id"], "pending")
        with self.coordinator.reserve_guard(request, context, prepared=self.policy):
            self.store.reserve_agent_execution(self.cid, ack["message_id"])
        self.coordinator.service_entered(context)
        return ack, context, request

    def fail_before_launch(self, context, request):
        settings = replace(self.policy["agent"], python_executable=str(self.root / "python-missing.exe"))
        with self.assertRaises(AgentError):
            self.service.execute(settings, self.policy["key"], AgentRequest("Synthetic owned task", "agent"),
                {"api": self.policy["api"], "conversation_id": self.cid, "messages": []},
                execution_attempt_callback=lambda attempt: self.coordinator.execution_attempt_bound(request, context, attempt))

    def test_exact_no_launch_proof_releases_only_as_interrupted_never_completed(self):
        ack, context, request = self.begin()
        self.fail_before_launch(context, request)
        # Even hostile self-reported completion and a matching archive reply
        # cannot convert absence of an actual worker into a completed job.
        reply = self.store.append_message(self.cid, "assistant", "Synthetic untrusted claim", "assistant",
            metadata={"reply_to": ack["message_id"]})
        request.update(result_state="completed", saved_reply_id=reply["id"])
        self.coordinator.finished(context, request, {})
        job = self.queue.get(ack["job_id"])
        self.assertEqual("interrupted", job.state)
        self.assertIsNotNone(job.worker_closed_at)
        self.assertIsNone(job.reply_id)
        self.assertFalse(self.coordinator.effect_barrier())

    def test_finished_same_cid_attempt_and_forged_dictionary_cannot_bind_next_job(self):
        first, context, request = self.begin()
        self.fail_before_launch(context, request)
        old_attempt = context.attempt
        self.coordinator.finished(context, request, {})
        second, context, request = self.begin()
        for rejected in (old_attempt, {"conversation_id": self.cid, "finished": True, "launch_attempted": False}):
            with self.assertRaises(AgentError): self.coordinator.execution_attempt_bound(request, context, rejected)
        self.assertNotIn("execution_attempt", request)
        # A stale receipt cannot stand in for this job's missing attempt owner.
        context.attempt = old_attempt
        request["execution_attempt"] = old_attempt
        request["execution_attempt_owner"] = object()
        self.coordinator.finished(context, request, {})
        self.clock.value += 31
        self.coordinator.pump()
        self.assertEqual("interrupted", self.queue.get(first["job_id"]).state)
        self.assertEqual("unknown", self.queue.get(second["job_id"]).state)
        self.assertIsNone(self.queue.get(second["job_id"]).worker_closed_at)
        self.assertTrue(self.coordinator.effect_barrier())


if __name__ == "__main__": unittest.main()
