"""Read-only Service resource snapshots with owned in-memory resources only."""

from __future__ import annotations

import io
import threading
import unittest
from unittest.mock import Mock

from desktop_pet.agents.execution_attempt import ExecutionAttempts
from desktop_pet.agents.process_lifecycle import WorkerLifecycle
from desktop_pet.agents.service import AgentService


class OwnedProcess:
    """No Popen, OS handle, thread, file path or real process operation."""
    pid = 1729

    def __init__(self):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.kills = self.waits = 0

    def kill(self):
        self.kills += 1

    def wait(self, timeout):
        self.waits += 1
        return 0


class OwnedTree:
    def __init__(self, confirmed):
        self.confirmed, self.closes = confirmed, 0

    def close(self):
        self.closes += 1

    def confirm_closed(self, *, timeout_seconds):
        return self.confirmed


class OwnedWriter:
    def __init__(self):
        self.joined = False
        self.calls = 0

    def close(self, **kwargs):
        self.calls += 1
        return self.joined


class ServiceExecutionResourcesTests(unittest.TestCase):
    def setUp(self):
        # Bypass construction so these tests never open stores or consult the
        # real guardian registry. The production predicate is bound unchanged.
        self.service = object.__new__(AgentService)
        for field in ("_lock", "_execution_lock"):
            setattr(self.service, field, threading.Lock())
        for field in ("_processes", "_worker_handles", "_worker_input_writers", "_worker_cancel_senders"):
            setattr(self.service, field, {})
        self.service._worker_cleanup_pending = set()
        self.service._active_control = None
        self.service._worker_lifecycle = WorkerLifecycle()
        self.service._execution_attempts = ExecutionAttempts()
        self.service.guardian_resources_pending = Mock(return_value=False)

    def bind_owned_worker(self, *, confirmed=False, writer=None):
        process, tree = OwnedProcess(), OwnedTree(confirmed)
        handle = self.service._worker_lifecycle.register(process, "owned-run", None)
        self.service._worker_lifecycle.attach_tree(handle, tree)
        self.service._worker_lifecycle.mark_dispatched(handle)
        self.service._processes[process.pid] = (threading.Event(), tree)
        self.service._worker_handles[process.pid] = handle
        if writer is not None:
            self.service._worker_input_writers[handle] = writer
        return handle, process, tree

    def test_idle_has_no_resources(self):
        self.assertIs(self.service.execution_resources_pending(), False)
        self.service.guardian_resources_pending.assert_called_once_with()

    def test_each_local_resource_blocks_independently_without_guardian(self):
        for field, value in (
            ("_processes", {1: object()}),
            ("_worker_handles", {1: object()}),
            ("_worker_cleanup_pending", {object()}),
            ("_worker_input_writers", {object(): object()}),
            ("_active_control", {}),  # Empty dict is still an active control.
        ):
            with self.subTest(field=field):
                original = getattr(self.service, field)
                setattr(self.service, field, value)
                try:
                    self.assertIs(self.service.execution_resources_pending(), True)
                finally:
                    setattr(self.service, field, original)
        self.service.guardian_resources_pending.assert_not_called()

    def test_prelaunch_execution_lock_blocks_before_any_handle_exists(self):
        self.assertTrue(self.service._execution_lock.acquire(blocking=False))
        try:
            self.assertIs(self.service.execution_resources_pending(), True)
            self.service.guardian_resources_pending.assert_not_called()
        finally:
            self.service._execution_lock.release()
        self.assertIs(self.service.execution_resources_pending(), False)

    def test_guardian_true_blocks(self):
        self.service.guardian_resources_pending.return_value = True
        self.assertIs(self.service.execution_resources_pending(), True)

    def test_guardian_exception_is_unknown_and_blocks_without_leaking(self):
        self.service.guardian_resources_pending.side_effect = RuntimeError("owned-private-detail")
        self.assertIs(self.service.execution_resources_pending(), True)

    def test_guardian_non_boolean_is_unknown_even_when_falsey(self):
        for result in (None, 0, 1, "", "false", [], {}, object()):
            with self.subTest(kind=type(result).__name__):
                self.service.guardian_resources_pending.return_value = result
                self.assertIs(self.service.execution_resources_pending(), True)

    def test_guardian_callback_runs_outside_service_lock(self):
        acquired = []

        def guardian():
            unlocked = self.service._lock.acquire(blocking=False)
            acquired.append(unlocked)
            if unlocked:
                self.service._lock.release()
            return False

        self.service.guardian_resources_pending.side_effect = guardian
        self.assertIs(self.service.execution_resources_pending(), False)
        self.assertEqual(acquired, [True])

    def test_predicate_preserves_local_state_and_lock_ownership(self):
        original = {name: getattr(self.service, name) for name in (
            "_processes", "_worker_handles", "_worker_cleanup_pending",
            "_worker_input_writers", "_active_control", "_lock", "_execution_lock")}
        for _ in range(3):
            self.assertIs(self.service.execution_resources_pending(), False)
        self.assertEqual({name: getattr(self.service, name) for name in original}, original)
        for lock in (self.service._lock, self.service._execution_lock):
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()

    def test_snapshot_does_not_reconcile_stop_poll_join_or_close_owned_resources(self):
        handle, process, tree = self.bind_owned_worker(writer=OwnedWriter())
        self.service._worker_cleanup_pending.add(handle)
        writer = self.service._worker_input_writers[handle]
        for name in ("reconcile_worker_closures", "_settle_worker", "cancel_request", "shutdown"):
            setattr(self.service, name, Mock(side_effect=AssertionError("snapshot performed resource action")))
        self.assertIs(self.service.execution_resources_pending(), True)
        self.assertEqual((process.kills, process.waits, tree.closes, writer.calls), (0, 0, 0, 0))
        self.assertIsNone(self.service.worker_closure(handle))
        for name in ("reconcile_worker_closures", "_settle_worker", "cancel_request", "shutdown"):
            getattr(self.service, name).assert_not_called()

    def test_populated_snapshot_preserves_every_retained_entry_and_container(self):
        handle, writer = object(), object()
        self.service._processes[1] = (object(), object())
        self.service._worker_handles[1] = handle
        self.service._worker_cleanup_pending.add(handle)
        self.service._worker_input_writers[handle] = writer
        self.service._active_control = {"task_id": "owned-task"}
        names = ("_processes", "_worker_handles", "_worker_cleanup_pending",
                 "_worker_input_writers", "_active_control")
        original = {name: getattr(self.service, name) for name in names}
        contents = {name: value.copy() for name, value in original.items()}
        for _ in range(3):
            self.assertIs(self.service.execution_resources_pending(), True)
        for name, container in original.items():
            self.assertIs(getattr(self.service, name), container)
            self.assertEqual(container, contents[name])
        self.service.guardian_resources_pending.assert_not_called()

    def test_unknown_tree_closure_stays_busy_then_actual_fake_settlement_releases(self):
        handle, process, tree = self.bind_owned_worker()
        self.assertIsNone(self.service._settle_worker(handle, timeout_seconds=0))
        self.assertIn(handle, self.service._worker_cleanup_pending)
        self.assertIs(self.service.execution_resources_pending(), True)
        tree.confirmed = True
        receipt = self.service._settle_worker(handle, timeout_seconds=0)
        self.assertTrue(self.service.validate_worker_closure(handle, receipt))
        self.assertFalse(self.service._processes)
        self.assertFalse(self.service._worker_handles)
        self.assertFalse(self.service._worker_cleanup_pending)
        self.assertTrue(process.stdin.closed and process.stdout.closed)
        self.assertIs(self.service.execution_resources_pending(), False)
        self.assertTrue(self.service._worker_lifecycle._runs, "closed receipt is retained, not live")

    def test_unjoined_writer_stays_busy_then_actual_fake_settlement_releases(self):
        writer = OwnedWriter()
        handle, process, _ = self.bind_owned_worker(confirmed=True, writer=writer)
        self.assertIsNone(self.service._settle_worker(handle, timeout_seconds=0))
        self.assertIn(handle, self.service._worker_input_writers)
        self.assertIs(self.service.execution_resources_pending(), True)
        writer.joined = True
        receipt = self.service._settle_worker(handle, timeout_seconds=0)
        self.assertTrue(self.service.validate_worker_closure(handle, receipt))
        self.assertFalse(self.service._worker_input_writers)
        self.assertIs(self.service.execution_resources_pending(), False)

    def test_long_lived_terminal_history_does_not_make_idle_service_busy(self):
        for i in range(140):
            process = OwnedProcess()
            handle = self.service._worker_lifecycle.register(process, f"owned-history-{i}", None)
            self.assertIsNotNone(self.service._worker_lifecycle.settle(handle, timeout_seconds=0))
            process.stdin.close()
            process.stdout.close()
            attempt = self.service._execution_attempts.begin(None)
            self.service._execution_attempts.finish(attempt)
        # WorkerLifecycle prunes completed history during register, before the
        # newly registered run settles. The retained history is what matters
        # here; its exact pruning bound is a separate lifecycle contract.
        self.assertGreaterEqual(len(self.service._worker_lifecycle._runs), 128)
        self.assertEqual(len(self.service._execution_attempts._attempts), 128)
        self.service._sessions = {"owned": ["retained text"]}
        self.service._runtime_health = {"owned-interpreter": True}
        self.service._closed = threading.Event()
        self.service._closed.set()  # Shutdown requested is not itself a live resource.
        self.assertIs(self.service.execution_resources_pending(), False)

    def test_retained_closed_client_objects_are_not_execution_resource_flags(self):
        # AgentService does not own these Controller fields. Their existence
        # must never be mistaken for an outstanding task/closure obligation.
        closed_client = type("OwnedClosedClient", (), {"closed": True})()
        self.service._client = closed_client
        self.service._metadata_clients = {closed_client}
        self.assertIs(self.service.execution_resources_pending(), False)


if __name__ == "__main__":
    unittest.main()
