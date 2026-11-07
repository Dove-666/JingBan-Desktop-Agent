"""Actual RPC/service prelaunch failure and subsequent fresh-task recovery.

No fake worker handle or native process result is provided. A copied interpreter is
removed after durable admission, so the real service's filesystem validation
fails before any native launch call. A new task after repair must use the real isolated
SDK, while the original failed request is never retried or marked completed.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from PySide6.QtCore import QThreadPool

from tests import test_remote_agent_queue as fixture
from tests.test_remote_controller import spin_until
from desktop_pet.agents.guardian_native import OwnedProcess


@unittest.skipUnless(fixture.PYTHON.is_file(), "Isolated SDK interpreter unavailable")
class PrelaunchFailureReviewTests(unittest.TestCase):
    setUpClass = classmethod(fixture.RemoteAgentQueueTests.setUpClass.__func__)
    setUp = fixture.RemoteAgentQueueTests.setUp
    tearDown = fixture.RemoteAgentQueueTests.tearDown
    rpc = fixture.RemoteAgentQueueTests.rpc
    configure = fixture.RemoteAgentQueueTests.configure
    send = fixture.RemoteAgentQueueTests.send
    job = fixture.RemoteAgentQueueTests.job

    def test_actual_validation_failure_without_any_launch_must_not_hold_unknown_slot_forever(self):
        source_names = ("desktop_pet/agents/coordinator.py", "desktop_pet/agents/service.py",
                        "desktop_pet/agents/execution_attempt.py", "desktop_pet/chat_controller.py",
                        "desktop_pet/remote/controller.py")
        def source_hashes():
            return {name: hashlib.sha256((fixture.REPO / name).read_bytes()).hexdigest() for name in source_names}
        before = source_hashes()
        model = self.configure([("write_file", {"path": "b.txt", "content": "B"}),
            ("read_file", {"path": "b.txt"}),
            ("finish_task", {"status": "completed", "summary": fixture.BREPLY}), "Unchecked rewrite"])
        copied = self.workspace / "python-owned.exe"
        shutil.copyfile(fixture.PYTHON, copied)
        settings = replace(self.settings.load_agent_settings(), python_executable=str(copied))
        self.settings.save_agent_settings(settings)
        self.chat.refresh_agent_settings()
        self.assertTrue(self.rpc("status")["result"]["agent_available"])
        coordinator = self.chat._agent_queue
        clock = [1000.0]
        coordinator.queue._clock = lambda: clock[0]
        self.chat._set_busy(True)
        first = self.send(fixture.A)
        self.assertEqual("queued", self.job(first).state)
        copied.unlink()  # Only this test-owned copy is removed.
        service = self.chat._agent_service
        native_create=OwnedProcess.create
        with patch.object(service, "execute", wraps=service.execute) as actual_execute, \
             patch("desktop_pet.agents.service.subprocess.Popen", wraps=subprocess.Popen) as actual_popen, \
             patch.object(OwnedProcess,"create",wraps=native_create) as actual_native:
            self.chat._set_busy(False)
            spin_until(lambda: actual_execute.call_count > 0, timeout=8)
            spin_until(lambda: not self.chat.busy and not QThreadPool.globalInstance().activeThreadCount(), timeout=8)
            self.assertEqual(1, actual_execute.call_count)
            self.assertEqual(0, actual_popen.call_count)
            self.assertEqual(0, actual_native.call_count)
            self.assertIsNone(coordinator._active)
            first_job = self.job(first)
            self.assertEqual("interrupted", first_job.state)
            self.assertIsNotNone(first_job.worker_closed_at)
            self.assertIsNone(first_job.reply_id)
            self.assertFalse(coordinator.effect_barrier())
            row = self.chat.store.read_message(self.cid, first["message_id"])
            self.assertEqual("failed", row["metadata"]["request_status"])
            self.assertEqual("not_configured", row["metadata"]["agent_error_code"])
            # Repair configuration to the actual runnable isolated SDK. This
            # authorizes only a newly submitted request, not replay of the old.
            self.settings.save_agent_settings(replace(settings, python_executable=str(fixture.PYTHON)))
            self.chat.refresh_agent_settings()
            self.assertTrue(self.rpc("status")["result"]["agent_available"])
            second = self.send(fixture.B)
            spin_until(lambda: self.job(second).state == "completed", timeout=15)
            spin_until(lambda: not self.chat.busy and not QThreadPool.globalInstance().activeThreadCount(), timeout=8)
            self.assertEqual(2, actual_execute.call_count)
            self.assertEqual(0, actual_popen.call_count)
            roots=[call for call in actual_native.call_args_list if Path(call.args[1][3]).name=="worker.py"]
            self.assertEqual(len(roots),1)
            self.assertEqual(4, len(model.planner_calls))
            self.assertFalse((self.workspace / "a.txt").exists())
            self.assertEqual("B", (self.workspace / "b.txt").read_text())
            self.assertEqual(fixture.BREPLY, self.chat.store.load_messages(self.cid)[-1]["content"])
            first_job, second_job = self.job(first), self.job(second)
            self.assertEqual("interrupted", first_job.state)
            self.assertIsNone(first_job.reply_id)
            self.assertIsNotNone(second_job.worker_closed_at)
            after = source_hashes()
            report = {"version": 1, "validated": True,
                "scenario": "actual_rpc_ack_then_owned_python_disappears_before_service_validation",
                "actual_service_calls": actual_execute.call_count,
                "popen_attempts": actual_popen.call_count,
                "native_root_attempts": len(roots), "native_total_attempts": actual_native.call_count,
                "planner_requests": len(model.planner_calls),
                "first_status": first_job.state, "first_worker_closed": first_job.worker_closed_at is not None,
                "second_status": second_job.state,
                "repaired_readiness": True, "unknown_barrier": coordinator.effect_barrier(),
                "source_hash_scope": "on_disk_at_test_entry_and_exit_not_a_loaded_binary_attestation",
                "source_before": before, "source_after": after, "invalidated": before != after}
            target = fixture.REPO / "artifacts/agent-queue-prelaunch-native-v1-recovery.json"
            target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            self.assertFalse(coordinator.effect_barrier())
            self.assertFalse(report["invalidated"], "Producer source changed during this verification")

    def test_actual_rpc_native_attempt_error_preserves_unknown_barrier_and_never_replays(self):
        model = self.configure([])
        coordinator = self.chat._agent_queue
        clock = [1000.0]
        coordinator.queue._clock = lambda: clock[0]
        with patch.object(OwnedProcess,"create",side_effect=OSError("SYNTHETIC_PRIVATE_LAUNCH_ERROR")) as native:
            first = self.send(fixture.A)
            spin_until(lambda: native.call_count == 1, timeout=8)
            spin_until(lambda: not self.chat.busy and not QThreadPool.globalInstance().activeThreadCount(), timeout=8)
            context = coordinator._active
            self.assertIsNotNone(context)
            self.assertIsNotNone(context.attempt)
            self.assertIsNone(context.handle)
            self.assertIsNone(self.chat._agent_service.execution_no_launch(context.attempt))
            clock[0] += 31
            self.chat._pump_agent_queue()
            second = self.send(fixture.B)
            self.chat._pump_agent_queue()
            self.assertEqual("unknown", self.job(first).state)
            self.assertIsNone(self.job(first).worker_closed_at)
            self.assertEqual("queued", self.job(second).state)
            self.assertTrue(coordinator.effect_barrier())
            self.assertEqual(1, native.call_count)
            self.assertEqual([], model.planner_calls)
            self.assertFalse((self.workspace / "a.txt").exists())


if __name__ == "__main__": unittest.main()
