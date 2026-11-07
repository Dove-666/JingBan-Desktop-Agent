"""Saved Agent replies and exact worker closure across host shutdown.

Only the worker body and transport are owned in-memory adapters. Controller,
queue, persistence, execution-attempt identity and closure receipts are real.
No process, provider, screen, or operating-system credential access is used.
"""

from dataclasses import replace
import io
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.agents import AgentResult, AgentService
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.remote.controller import RemoteController
from desktop_pet.settings_manager import ApiSettings
from tests.test_application import MemoryCredentialBackend, isolated_settings


PEER = "a" * 64
REPLY = "Owned fixture output was created and independently read back."


class NoProvider:
    def complete(self, *args, **kwargs):
        raise AssertionError("NO_PROVIDER_ALLOWED")

    def complete_context(self, *args, **kwargs):
        raise AssertionError("NO_PROVIDER_ALLOWED")


class OwnedTransport:
    def __init__(self, secret_hex, on_request, on_status):
        self.request = on_request
        self.on_status = on_status

    def start(self):
        self.on_status({"state": "listening", "endpoint_id": "c" * 64, "ticket": "owned"})

    def stop(self):
        pass


class OwnedProcess:
    def __init__(self):
        self.pid = 71920
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()

    def wait(self, timeout):
        return 0

    def poll(self):
        return 0

    def kill(self):
        pass


class OwnedTree:
    def __init__(self):
        self.confirmed = True

    def close(self):
        pass

    def confirm_closed(self, *, timeout_seconds):
        return self.confirmed


class FalsePredicate:
    def __init__(self):
        self.calls = 0

    def __bool__(self):
        return False

    def __call__(self, cid):
        self.calls += 1
        return False


class AgentShutdownCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        workspace_root = Path(__file__).resolve().parents[1] / "agent_workspace"
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-completion-owned-", dir=workspace_root)
        self.root = Path(self.temporary.name)
        self.releases = []
        self.addCleanup(self.cleanup)
        self.settings = isolated_settings(self.root / "settings.ini", MemoryCredentialBackend())
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "owned-no-provider"))
        self.settings.save_api_key("OWNED_COMPLETION_SYNTHETIC")
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.settings.save_agent_settings(replace(self.settings.load_agent_settings(),
            enabled=True, computer_enabled=False, workspace=str(workspace), timeout_seconds=20))
        self.service = AgentService(checkpoint_path=self.root / "checkpoint.sqlite3")
        self.store = ConversationStore(self.root / "chat.sqlite3")
        self.chat = ChatController(self.settings, NoProvider(), store=self.store,
            agent_service=self.service, metadata_client_factory=lambda: None)
        self.remote = RemoteController(SimpleNamespace(settings=self.settings, chat=self.chat),
            transport_factory=OwnedTransport)
        self.wait(lambda: not self.chat._runner._tasks)
        self.assertTrue(self.remote.set_enabled(True))
        self.wait(lambda: self.remote.status["state"] == "listening")
        pairing = self.remote.begin_pairing()
        self.assertTrue(self.rpc("pair", {"code": pairing["code"], "name": "Owned phone"})["ok"])
        self.cid = self.chat.current_conversation_id
        self.calls = 0
        self.handles = []
        self.trees = []
        self.outcome = AgentResult(REPLY, status="completed")
        self.worker_entered = threading.Event()
        self.worker_release = None
        self.tree_confirmed = True
        self.worker_patch = patch.object(self.service, "_execute_impl", side_effect=self.worker)
        self.worker_patch.start()

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(5)
            time.sleep(.002)
        self.assertTrue(predicate(), "OWNED_COMPLETION_TIMED_OUT")

    def cleanup(self):
        for release in self.releases:
            release.set()
        for tree in getattr(self, "trees", []):
            tree.confirmed = True
        chat, remote = getattr(self, "chat", None), getattr(self, "remote", None)
        if remote is not None:
            remote.shutdown()
        if chat is not None:
            chat.shutdown()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(4000))
        if hasattr(self, "service"):
            self.service.reconcile_worker_closures(timeout_seconds=0)
        if hasattr(self, "worker_patch"):
            self.worker_patch.stop()
        for obj in (remote, chat):
            if obj is not None:
                obj.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def rpc(self, method, params=None):
        return self.remote._transport.request(PEER,
            {"id": str(uuid4()), "method": method, "params": params or {}})

    def worker(self, settings, key, request, context, cancel, progress, event,
               interaction, image_resolver, diagnostic, bound, dispatch_guard, *,
               execution_attempt, guardian_launch=None):
        self.calls += 1
        self.service._execution_attempts.mark_launch_attempted(execution_attempt)
        process, tree = OwnedProcess(), OwnedTree()
        tree.confirmed = self.tree_confirmed
        handle = self.service._worker_lifecycle.register(process, uuid4().hex, context["conversation_id"])
        self.service._worker_lifecycle.attach_tree(handle, tree)
        self.service._worker_handles[process.pid] = handle
        self.service._processes[process.pid] = (cancel, tree)
        self.handles.append(handle)
        self.trees.append(tree)
        bound(handle)
        try:
            with dispatch_guard(handle):
                self.service._worker_lifecycle.mark_dispatched(handle)
                (self.root / "result.txt").write_bytes(b"OWNED RESULT")
            self.assertEqual((self.root / "result.txt").read_bytes(), b"OWNED RESULT")
            self.worker_entered.set()
            if self.worker_release is not None:
                self.assertTrue(self.worker_release.wait(6), "OWNED_WORKER_HELD")
            return self.outcome
        finally:
            self.service._settle_worker(handle, timeout_seconds=0)

    def hold_worker(self):
        self.worker_release = threading.Event()
        self.releases.append(self.worker_release)

    def send(self):
        response = self.rpc("agent.send", {"conversation_id": self.cid,
            "text": "Create and verify the owned fixture output."})
        self.assertTrue(response["ok"], response)
        self.ack = response["result"]
        return self.ack

    def job(self):
        return self.chat._agent_queue.queue.get(self.ack["job_id"])

    def wait_physical(self):
        self.wait(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.app.processEvents()

    def wait_gui(self):
        self.wait(lambda: not self.chat.busy and not self.chat._requests and not self.chat._runner._tasks)

    def hold_commit(self):
        committed, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.store.complete_request
        def complete(*args, **kwargs):
            result = original(*args, **kwargs)
            committed.set()
            self.assertTrue(release.wait(6), "OWNED_COMMIT_HELD")
            return result
        return committed, release, patch.object(self.store, "complete_request", side_effect=complete)

    def test_saved_committed_completion_survives_host_shutdown(self):
        committed, release, hold = self.hold_commit()
        with hold:
            self.send()
            self.wait(committed.is_set)
            self.assertEqual(self.store.load_messages(self.cid)[-1]["content"], REPLY)
            self.assertTrue(self.service.validate_worker_closure(self.handles[0], self.service.worker_closure(self.handles[0])))
            self.chat.shutdown()
            release.set()
            self.wait_physical()
        job = self.job()
        rows = self.store.load_messages(self.cid)
        self.assertEqual(job.state, "completed")
        self.assertEqual(job.reply_id, rows[-1]["id"])
        self.assertIsNotNone(job.worker_closed_at)
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.calls, 1)

    def test_shutdown_before_commit_does_not_save_or_complete_a_late_result(self):
        self.hold_worker()
        self.send()
        self.wait(self.worker_entered.is_set)
        self.chat.shutdown()
        self.worker_release.set()
        self.wait_physical()
        self.assertNotEqual(self.job().state, "completed")
        self.assertIsNone(self.job().reply_id)
        self.assertIsNotNone(self.job().worker_closed_at)
        self.assertFalse(any(row["role"] == "assistant" for row in self.store.load_messages(self.cid)))

    def test_stop_winning_before_commit_blocks_late_completion_and_retry(self):
        self.hold_worker()
        self.send()
        self.wait(self.worker_entered.is_set)
        self.assertTrue(self.chat.stop_request(self.cid))
        self.worker_release.set()
        self.wait_gui()
        self.assertEqual(self.job().state, "cancelled")
        self.assertIsNone(self.job().reply_id)
        self.assertFalse(self.chat.message_retry_state(self.cid, self.ack["message_id"])["available"])
        self.assertFalse(any(row["role"] == "assistant" for row in self.store.load_messages(self.cid)))

    def test_too_late_stop_does_not_cancel_a_committed_reply(self):
        committed, release, hold = self.hold_commit()
        with hold:
            self.send()
            self.wait(committed.is_set)
            self.assertFalse(self.chat.stop_request(self.cid))
            release.set()
            self.wait_gui()
        self.assertEqual(self.job().state, "completed")
        self.assertEqual(len(self.store.load_messages(self.cid)), 2)

    def test_missing_saved_reply_does_not_satisfy_completion(self):
        committed, release, hold = self.hold_commit()
        with hold:
            self.send()
            self.wait(committed.is_set)
            reply_id = self.store.load_messages(self.cid)[-1]["id"]
            with self.store._connection() as connection:
                connection.execute("DELETE FROM messages WHERE id=?", (reply_id,))
            self.chat.shutdown()
            release.set()
            self.wait_physical()
        self.assertNotEqual(self.job().state, "completed")
        self.assertIsNone(self.job().reply_id)

    def test_clear_delete_changed_generation_and_invalidation_flag_stay_blocked(self):
        for change in ("clear", "delete", "generation", "invalidating"):
            with self.subTest(change=change):
                # Each subcase gets a fresh actual host and queue.
                fixture = AgentShutdownCompletionTests("runTest")
                fixture.setUp()
                try:
                    committed, release, hold = fixture.hold_commit()
                    with hold:
                        fixture.send()
                        fixture.wait(committed.is_set)
                        if change == "clear":
                            fixture.store.clear_messages(fixture.cid)
                        elif change == "delete":
                            fixture.store.delete_conversation(fixture.cid)
                        elif change == "generation":
                            fixture.store.append_message(fixture.cid, "user", "Owned changed generation", "user",
                                created_at="2000-01-01T00:00:00+00:00")
                        else:
                            fixture.chat._invalidating_conversations.add(fixture.cid)
                        fixture.chat.shutdown()
                        release.set()
                        fixture.wait_physical()
                    self.assertNotEqual(fixture.job().state, "completed")
                    self.assertIsNone(fixture.job().reply_id)
                finally:
                    fixture.doCleanups()

    def test_unconfirmed_worker_closure_cannot_complete_or_release_effect_barrier(self):
        self.tree_confirmed = False
        self.send()
        self.wait_gui()
        self.assertIsNone(self.service.worker_closure(self.handles[0]))
        self.assertNotEqual(self.job().state, "completed")
        self.assertIsNone(self.job().worker_closed_at)
        self.assertIsNotNone(self.chat._agent_queue._active)
        second = self.rpc("agent.send", {"conversation_id": self.cid,
            "text": "A new owned task must wait for exact closure."})
        self.assertTrue(second["ok"], second)
        self.chat._pump_agent_queue()
        self.assertEqual(self.chat._agent_queue.queue.get(second["result"]["job_id"]).state, "queued")
        self.assertEqual(self.calls, 1)
        self.assertIsNone(self.job().reply_id)

    def test_completion_predicate_exception_keeps_existing_failed_barrier(self):
        committed, release, hold = self.hold_commit()
        with hold:
            self.send()
            self.wait(committed.is_set)
            def unavailable(cid):
                raise RuntimeError("OWNED_PRIVATE_PREDICATE_DETAIL")
            self.chat._agent_queue.completion_invalidated = unavailable
            release.set()
            self.wait_gui()
        self.assertTrue(self.chat._agent_queue._failed)
        self.assertTrue(self.chat._agent_queue.effect_barrier())
        self.assertNotEqual(self.job().state, "completed")
        self.assertIsNone(self.job().reply_id)
        self.assertEqual(self.store.load_messages(self.cid)[-1]["content"], REPLY)

    def test_falsey_explicit_completion_predicate_is_not_replaced_by_default(self):
        from desktop_pet.agents.coordinator import HostAgentCoordinator
        original = self.chat._agent_queue
        predicate = FalsePredicate()
        coordinator = HostAgentCoordinator(original.queue, self.store, self.service,
            configuration=original.configuration, invalidated=original.invalidated,
            start=original.start, idle=original.idle, completion_invalidated=predicate)
        self.assertIs(coordinator.completion_invalidated, predicate)


if __name__ == "__main__":
    unittest.main()
