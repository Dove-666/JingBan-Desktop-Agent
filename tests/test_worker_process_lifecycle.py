"""Exact host closure proof with fake owned handles, never real user processes."""
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents.process_lifecycle import WorkerLifecycle, WorkerRunHandle, WorkerClosureReceipt
from desktop_pet.agents import AgentService, AgentRequest, AgentSettings, AgentError, AgentResult
from desktop_pet.agents import service as host


class Process:
    def __init__(self, waits=(0,), pid=12345):
        self.waits, self.pid, self.kills, self.wait_calls = list(waits), pid, 0, 0
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
    def kill(self): self.kills += 1
    def poll(self): return None
    def wait(self, timeout):
        self.wait_calls += 1
        result = self.waits.pop(0) if len(self.waits) > 1 else self.waits[0]
        if isinstance(result, BaseException): raise result
        return result


class Tree:
    def __init__(self, confirmed=True): self.confirmed, self.closes = confirmed, 0
    def close(self): self.closes += 1
    def confirm_closed(self, *, timeout_seconds): return self.confirmed


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self): self.lifecycle, self.cid = WorkerLifecycle(), str(uuid4())

    def bind(self, process=None, tree=None, *, dispatched=True):
        process = process or Process()
        handle = self.lifecycle.register(process, "a" * 32, self.cid)
        if tree is not None: self.lifecycle.attach_tree(handle, tree)
        if dispatched: self.lifecycle.mark_dispatched(handle)
        return handle, process

    def test_close_called_or_reaped_parent_cannot_replace_tree_confirmation(self):
        tree = Tree(False)
        handle, process = self.bind(tree=tree)
        self.lifecycle.request_stop(handle)
        self.assertIsNone(self.lifecycle.receipt(handle))
        self.assertIsNone(self.lifecycle.settle(handle, timeout_seconds=0))
        self.assertEqual(process.wait_calls, 1)
        self.assertGreater(tree.closes, 0)
        tree.confirmed = True
        receipt = self.lifecycle.settle(handle, timeout_seconds=0)
        self.assertTrue(self.lifecycle.validates(handle, receipt))
        self.assertTrue(receipt.worker_closed and receipt.reaped)

    def test_timeout_and_wait_oserror_keep_tracking_until_exact_later_reap(self):
        for error in (subprocess.TimeoutExpired("owned", 0), OSError("PRIVATE")):
            process = Process((error, 0))
            handle, _ = self.bind(process, Tree())
            self.assertIsNone(self.lifecycle.settle(handle, timeout_seconds=0))
            self.assertIsNone(self.lifecycle.receipt(handle))
            receipt = self.lifecycle.settle(handle, timeout_seconds=0)
            self.assertTrue(self.lifecycle.validates(handle, receipt))

    def test_anonymous_predispatch_reap_only_cannot_cover_a_dispatched_uncontained_worker(self):
        handle, _ = self.bind(dispatched=False)
        self.assertIsNotNone(self.lifecycle.settle(handle, timeout_seconds=0))
        handle, _ = self.bind(dispatched=True)
        self.assertIsNone(self.lifecycle.settle(handle, timeout_seconds=0))

    def test_empty_job_cannot_prove_external_computer_operation_stopped(self):
        handle, _ = self.bind(tree=Tree(), dispatched=False)
        self.lifecycle.require_external_cleanup(handle)
        self.lifecycle.mark_dispatched(handle)
        self.assertIsNone(self.lifecycle.settle(handle, timeout_seconds=0))
        self.lifecycle.confirm_external_cleanup(handle)
        self.assertIsNotNone(self.lifecycle.settle(handle, timeout_seconds=0))

    def test_uncommitted_worker_does_not_need_external_cleanup_ack(self):
        handle, _ = self.bind(tree=Tree(), dispatched=False)
        self.lifecycle.require_external_cleanup(handle)
        self.assertIsNotNone(self.lifecycle.settle(handle, timeout_seconds=0))

    def test_receipt_and_run_handle_cannot_be_json_or_foreign_identity(self):
        handle, _ = self.bind(tree=Tree())
        receipt = self.lifecycle.settle(handle, timeout_seconds=0)
        self.assertEqual(receipt.run_id, "a" * 32)
        self.assertEqual(receipt.conversation_id, self.cid)
        with self.assertRaises(TypeError): WorkerRunHandle()
        with self.assertRaises(TypeError): WorkerClosureReceipt()
        with self.assertRaises(TypeError): json.dumps(receipt)
        with self.assertRaises(AttributeError): handle._run_id = "changed"
        with self.assertRaises(AttributeError): receipt._handle = None
        with self.assertRaises(ValueError): self.lifecycle.receipt({"worker_closed": True})
        self.assertFalse(WorkerLifecycle().validates(handle, receipt))
        same_pid, _ = self.bind(Process(pid=12345), Tree())
        self.assertFalse(self.lifecycle.validates(same_pid, receipt))

    def test_receipt_is_idempotent_and_boolean_wait_is_not_reaping_proof(self):
        handle, process = self.bind(tree=Tree())
        receipt = self.lifecycle.settle(handle, timeout_seconds=0)
        calls = process.wait_calls
        self.assertIs(self.lifecycle.settle(handle, timeout_seconds=0), receipt)
        self.assertEqual(process.wait_calls, calls)
        handle, _ = self.bind(Process((True,)), Tree())
        self.assertIsNone(self.lifecycle.settle(handle, timeout_seconds=0))

    def test_service_registry_survives_cleanup_failure_and_stop_is_only_a_request(self):
        service = AgentService()
        process, tree, cancel = Process((subprocess.TimeoutExpired("owned", 0), 0)), Tree(), threading.Event()
        handle = service._worker_lifecycle.register(process, "b" * 32, self.cid)
        service._worker_lifecycle.attach_tree(handle, tree)
        service._worker_lifecycle.mark_dispatched(handle)
        service._worker_handles[process.pid] = handle
        service._processes[process.pid] = (cancel, tree)
        self.assertEqual(service.reconcile_worker_closures(timeout_seconds=0), ())
        self.assertEqual(process.wait_calls, 0)
        self.assertEqual(tree.closes, 0)
        service.cancel_request(cancel)
        self.assertTrue(cancel.is_set())
        self.assertIsNone(service.worker_closure(handle))
        self.assertIsNone(service._settle_worker(handle, timeout_seconds=0))
        self.assertIn(process.pid, service._processes)
        with self.assertRaises(AgentError) as raised:
            service.execute(AgentSettings(), "unused", AgentRequest("Owned task", "agent"),
                            {"api": {"base_url": "http://127.0.0.1:1/v1", "model": "owned-offline-fixture"}})
        self.assertEqual(raised.exception.code, "busy")
        receipts = service.reconcile_worker_closures(timeout_seconds=0)
        self.assertEqual(len(receipts), 1)
        self.assertTrue(service.validate_worker_closure(handle, receipts[0]))
        self.assertEqual(service._processes, {})
        self.assertTrue(process.stdin.closed and process.stdout.closed)
        service.shutdown()

    def test_wrapper_downgrades_returned_completion_after_finally_cleanup_failed(self):
        service, bound = AgentService(), []
        original = AgentResult("Verified content.", status="completed", artifacts=({"reference": "owned-artifact"},))
        def implementation(*args, **kwargs):
            service._execution_attempts.mark_launch_attempted(kwargs["execution_attempt"])
            process, tree, cancel = Process(), Tree(False), threading.Event()
            handle = service._worker_lifecycle.register(process, "c" * 32, self.cid)
            service._worker_lifecycle.attach_tree(handle, tree)
            service._worker_lifecycle.mark_dispatched(handle)
            service._worker_handles[process.pid] = handle
            service._processes[process.pid] = (cancel, tree)
            args[-2](handle)
            self.assertIsNone(service._settle_worker(handle, timeout_seconds=0))
            return original
        with patch.object(service, "_execute_impl", side_effect=implementation):
            result = service.execute(AgentSettings(), "unused", AgentRequest("Owned task", "agent"), {},
                                     worker_lifecycle_callback=bound.append)
        self.assertEqual(result.status, "needs_user")
        self.assertEqual(result.artifacts, original.artifacts)
        self.assertIn("后台工具仍在结束中", result.reply)
        self.assertTrue(result.reply.startswith(original.reply))
        self.assertIsNone(result.acceptance)
        self.assertIsNone(service.worker_closure(bound[0]))
        service.shutdown()

    def test_original_exception_is_not_replaced_by_cleanup_status(self):
        service = AgentService()
        error = AgentError("provider_auth_failed", "Fixed safe error")
        with patch.object(service, "_execute_impl", side_effect=error):
            with self.assertRaises(AgentError) as raised:
                service.execute(AgentSettings(), "unused", AgentRequest("Owned task", "agent"), {})
        self.assertIs(raised.exception, error)


class OwnedJobAccountingTests(unittest.TestCase):
    def tree(self, *, active=0, query=True, close=True):
        class Kernel:
            def __init__(self): self.terminated, self.closed = [], []
            def TerminateJobObject(self, job, code): self.terminated.append(job); return True
            def QueryInformationJobObject(self, job, kind, pointer, size, returned):
                pointer._obj.ActiveProcesses = active
                return query
            def CloseHandle(self, job): self.closed.append(job); return close
        tree = object.__new__(host._ProcessTree)
        tree.process, tree._job, tree._kernel = Process(), 777, Kernel()
        tree._close_lock, tree._confirmed_closed = threading.Lock(), False
        return tree

    def test_termination_request_retains_exact_job_until_active_count_is_zero(self):
        tree = self.tree()
        with patch.object(host.sys, "platform", "win32"):
            tree.close()
            self.assertEqual(tree._job, 777)
            self.assertEqual(tree._kernel.closed, [])
            self.assertEqual(tree._kernel.terminated, [777])
            self.assertTrue(tree.confirm_closed(timeout_seconds=0))
            self.assertIsNone(tree._job)
            self.assertEqual(tree._kernel.closed, [777])
            kills = tree.process.kills
            tree.close()
            self.assertEqual(tree.process.kills, kills)

    def test_live_owned_child_failed_query_or_failed_close_cannot_mint_confirmation(self):
        for values in ({"active": 1}, {"query": False}, {"close": False}):
            tree = self.tree(**values)
            with patch.object(host.sys, "platform", "win32"):
                self.assertFalse(tree.confirm_closed(timeout_seconds=0))
                self.assertEqual(tree._job, 777)
                self.assertFalse(tree._confirmed_closed)


class TerminalStorageStopTests(unittest.TestCase):
    """A blocked native-session metadata write cannot prevent process Stop."""
    def test_native_session_storage_cannot_delay_stop_or_publish_early_closure(self):
        from tests.test_dsh_host import DshHostTests
        DshHostTests.setUp(self)
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        handles, failures = [], []
        original = self.service._session_store.save
        def save(*args):
            entered.set()
            if not release.wait(10): raise AssertionError("Owned storage fixture was not released")
            return original(*args)
        def run():
            try:
                DshHostTests.run_task(self, "cancel", cancellation_event=cancelled,
                                     worker_lifecycle_callback=handles.append)
            except AgentError as exc: failures.append(exc.code)
        try:
            with patch.object(self.service._session_store, "save", side_effect=save):
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(entered.wait(10))
                    self.assertEqual(len(handles), 1)
                    self.service.cancel_request(cancelled)
                    deadline = time.monotonic() + 3
                    while handles[0]._process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertIsNotNone(handles[0]._process.poll())
                    self.assertIsNone(self.service.worker_closure(handles[0]))
                    self.assertTrue(self.service.execution_resources_pending())
                finally:
                    release.set()
                    thread.join(10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, ["cancelled"])
                receipt = self.service.worker_closure(handles[0])
                self.assertTrue(self.service.validate_worker_closure(handles[0], receipt))
                self.assertFalse(self.service.execution_resources_pending())
        finally:
            release.set()
            DshHostTests.tearDown(self)


if __name__ == "__main__": unittest.main()
