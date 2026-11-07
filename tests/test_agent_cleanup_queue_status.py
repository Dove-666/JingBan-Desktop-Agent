"""A retained local worker keeps queued requests intact until exact closure.

All persistence and host lifecycle decisions are real. Processes and trees are
finite in-memory adapters. No credentials, providers or native launches exist.
"""

from dataclasses import replace
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import AgentService, AgentSettings
from desktop_pet.agents.coordinator import HostAgentCoordinator
from desktop_pet.agents.job_queue import JobQueue
from desktop_pet.conversations import ConversationStore
from desktop_pet.remote.controller import RemoteController
from desktop_pet.settings_manager import ApiSettings


class OwnedProcess:
    pid = 890013

    def __init__(self):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()

    def wait(self, timeout):
        return 0

    def kill(self):
        pass


class OwnedTree:
    def __init__(self):
        self.confirmed = False
        self.close_calls = 0

    def close(self):
        self.close_calls += 1

    def confirm_closed(self, *, timeout_seconds):
        return self.confirmed


class AgentCleanupQueueStatusTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[1] / "agent_workspace"
        self.temporary = tempfile.TemporaryDirectory(prefix="cleanup-queue-owned-", dir=workspace)
        self.root = Path(self.temporary.name)
        self.assertTrue(self.root.resolve().is_relative_to(workspace.resolve()))
        self.store = ConversationStore(self.root / "chat.sqlite3")
        self.queue = JobQueue(self.root / "jobs.sqlite3")
        self.service = AgentService()
        self.started = []
        self.idle = True
        self.policy = {"api": ApiSettings("http://127.0.0.1:1/v1", "owned-no-provider"),
            "key": "OWNED_CONFIGURATION_VALUE", "gui_key": "",
            "agent": replace(AgentSettings(), workspace=str(self.root))}
        self.coordinator = HostAgentCoordinator(self.queue, self.store, self.service,
            configuration=lambda: self.policy, invalidated=lambda _cid: False,
            start=lambda context, message, policy: self.started.append((context, message, policy)),
            idle=lambda: self.idle, allow_new=lambda _cid: True)
        self.assertTrue(self.coordinator.initialize())
        self.coordinator.bind_authority(lambda _peer, create=False: "b" * 64)

    def tearDown(self):
        if hasattr(self, "tree"):
            self.tree.confirmed = True
        self.service.reconcile_worker_closures(timeout_seconds=0)
        self.service.shutdown()
        self.coordinator.shutdown()
        self.temporary.cleanup()

    def pending_local_worker(self):
        self.process = OwnedProcess()
        self.tree = OwnedTree()
        self.handle = self.service._worker_lifecycle.register(self.process, uuid4().hex, str(uuid4()))
        self.service._worker_lifecycle.attach_tree(self.handle, self.tree)
        self.service._worker_lifecycle.mark_dispatched(self.handle)
        self.service._worker_handles[self.process.pid] = self.handle
        self.service._processes[self.process.pid] = (threading.Event(), self.tree)
        self.assertIsNone(self.service._settle_worker(self.handle, timeout_seconds=0))

    def admit(self):
        return self.coordinator.admit("a" * 64, str(uuid4()), str(uuid4()), "Inspect the owned fixture.")

    def test_retained_local_worker_preserves_queued_identity_until_exact_closure(self):
        self.pending_local_worker()
        ack = self.admit()
        message_before = self.store.read_message(ack["conversation_id"], ack["message_id"])
        for _ in range(4):
            self.coordinator.pump()
            self.assertEqual(self.queue.get(ack["job_id"]).state, "queued")
            self.assertEqual(self.started, [])
            self.assertEqual(self.store.read_message(ack["conversation_id"], ack["message_id"]), message_before)
            self.assertTrue(self.coordinator.recovery_pending)
            self.assertIsNone(self.service.worker_closure(self.handle))
        self.tree.confirmed = True
        self.coordinator.pump()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0][0].claim.job.job_id, ack["job_id"])
        self.assertEqual(self.started[0][1]["id"], ack["message_id"])
        receipt = self.service.worker_closure(self.handle)
        self.assertIsNotNone(receipt)
        self.assertTrue(self.service.validate_worker_closure(self.handle, receipt))
        self.assertFalse(self.service.execution_resources_pending())
        self.coordinator.pump()
        self.assertEqual(len(self.started), 1)

    def test_active_local_request_is_not_cancelled_by_cleanup_poll(self):
        self.pending_local_worker()
        ack = self.admit()
        self.idle = False
        close_calls = self.tree.close_calls
        with patch.object(self.service, "reconcile_worker_closures", wraps=self.service.reconcile_worker_closures) as reconcile:
            self.coordinator.pump()
            self.assertTrue(self.coordinator.recovery_pending)
            self.assertEqual(reconcile.call_count, 0)
        self.assertEqual(self.tree.close_calls, close_calls)
        self.assertEqual(self.queue.get(ack["job_id"]).state, "queued")
        self.assertEqual(self.started, [])

    def test_legacy_service_without_optional_resource_query_keeps_dispatch_compatibility(self):
        self.coordinator.service = object()
        ack = self.admit()
        self.coordinator.pump()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0][0].claim.job.job_id, ack["job_id"])

    def test_resource_query_failure_waits_without_consuming_the_queued_request(self):
        ack = self.admit()
        with patch.object(self.service, "execution_resources_pending", side_effect=RuntimeError("OWNED_STATUS_UNKNOWN")):
            self.coordinator.pump()
            self.assertEqual(self.started, [])
            self.assertEqual(self.queue.get(ack["job_id"]).state, "queued")
            self.assertTrue(self.coordinator.recovery_pending)

    def remote_status(self, service, cid=None):
        chat = SimpleNamespace(_agent_service=service,
            agent_readiness=lambda: {"available": True, "reason": "", "tools": []},
            agent_running_conversation_id=cid)
        return RemoteController._agent_status(SimpleNamespace(session=SimpleNamespace(chat=chat)))

    def test_remote_status_keeps_capability_availability_and_no_running_cid_during_cleanup(self):
        self.pending_local_worker()
        status = self.remote_status(self.service)
        self.assertTrue(status["agent_available"])
        self.assertTrue(status["agent_busy"])
        self.assertIsNone(status["agent_conversation_id"])
        cid = str(uuid4())
        self.assertEqual(self.remote_status(self.service, cid)["agent_conversation_id"], cid)
        self.tree.confirmed = True
        self.service.reconcile_worker_closures(timeout_seconds=0)
        self.assertFalse(self.remote_status(self.service)["agent_busy"])

    def test_remote_status_missing_query_is_legacy_idle_and_invalid_query_is_busy(self):
        self.assertFalse(self.remote_status(object())["agent_busy"])
        for value in (None, 0, "unknown"):
            with self.subTest(value=value):
                self.assertTrue(self.remote_status(SimpleNamespace(execution_resources_pending=lambda: value))["agent_busy"])
        with patch.object(self.service, "execution_resources_pending", side_effect=RuntimeError("OWNED_STATUS_UNKNOWN")):
            self.assertTrue(self.remote_status(self.service)["agent_busy"])


if __name__ == "__main__":
    unittest.main()
