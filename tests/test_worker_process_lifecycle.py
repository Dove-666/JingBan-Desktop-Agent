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
from agent_backend.nanobot.acceptance_report import checked_assessment
from desktop_pet.agents import service as host
from desktop_pet.agents import execution_runtime, office_runtime, settings as settings_module


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
        self.assertIn("未完成", result.reply)
        self.assertTrue(result.reply.startswith(original.reply))
        self.assertFalse(checked_assessment(result.acceptance)["passed"])
        self.assertIn("WORKER_CLEANUP_UNCONFIRMED", result.acceptance["issue_codes"])
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
    def test_terminal_storage_cannot_delay_stop_or_publish_early_closure(self):
        for error in ("timeout", "backend_failed"):
            for storage in ("journal", "checkpoint"):
                for confirmed in (True, False):
                    with self.subTest(error=error, storage=storage, confirmed=confirmed):
                        self.check_blocked_storage(error, storage, confirmed)

    def check_blocked_storage(self, error, storage, confirmed):
        # All process/Job operations use these in-memory endpoints. The real
        # public execute, input watchdog, lifecycle and owned SQLite stores run.
        clock = SimpleNamespace(now=100.0)
        process = Process()
        committed = threading.Event()

        class Input(io.BytesIO):
            def write(self, value):
                count = super().write(value)
                if bytes(value) == b"\n":
                    committed.set()
                    if error == "timeout":
                        clock.now += 6.0
                return count

        process.stdin = Input()
        process.poll = lambda: 0 if error == "backend_failed" and committed.is_set() else None
        tree = Tree(confirmed)
        entered, release = threading.Event(), threading.Event()
        ready_sent = False
        terminal, outcomes, handles = {}, {}, []

        def read_available(pipe):
            nonlocal ready_sent
            self.assertIs(pipe, process.stdout)
            if not ready_sent:
                ready_sent = True
                return b'{"type":"ready","protocol":1,"sdk":"nanobot-ai","sdk_version":"0.4.0"}\n'
            return b""

        def hold(data):
            terminal.update(data)
            entered.set()
            if not release.wait(10):
                raise AssertionError("OWNED_TERMINAL_STORAGE_GATE_NOT_RELEASED")

        with tempfile.TemporaryDirectory(prefix="aura-terminal-storage-") as temporary, ExitStack() as patches:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            interpreter = root / "python-owned.exe"
            interpreter.write_bytes(b"OWNED_FAKE_NOT_EXECUTED")
            service = AgentService(checkpoint_path=root / "checkpoint.sqlite3")
            original_append = service._journal.append
            original_save = service._checkpoint_store.save

            def append(conversation_id, task_id, kind, data, **kwargs):
                if storage == "journal" and kind == "task_finished":
                    hold(data)
                return original_append(conversation_id, task_id, kind, data, **kwargs)

            def save(*args, **kwargs):
                if storage == "checkpoint" and kwargs.get("status") != "running":
                    hold({"status": kwargs["status"]})
                return original_save(*args, **kwargs)

            service._journal.append = append
            service._checkpoint_store.save = save
            patches.enter_context(patch.object(settings_module, "AGENT_WORKSPACE_ROOT", root))
            patches.enter_context(patch.object(office_runtime, "load_office_runtime", return_value=None))
            patches.enter_context(patch.object(execution_runtime, "load_execution_runtime", return_value=None))
            patches.enter_context(patch.object(host, "time", SimpleNamespace(
                monotonic=lambda: clock.now, time=time.time, sleep=time.sleep)))
            launch = patches.enter_context(patch.object(host.subprocess, "Popen", return_value=process))
            patches.enter_context(patch.object(host, "_ProcessTree", return_value=tree))
            patches.enter_context(patch.object(host, "_read_available", side_effect=read_available))
            stop = patches.enter_context(patch.object(service._worker_lifecycle, "request_stop",
                wraps=service._worker_lifecycle.request_stop))
            settle = patches.enter_context(patch.object(service, "_settle_worker", wraps=service._settle_worker))
            settings = AgentSettings(python_executable=str(interpreter), workspace=str(workspace),
                tools_enabled=False, computer_enabled=False, timeout_seconds=5)
            context = {"api": {"base_url": "http://127.0.0.1:1/v1", "model": "owned-no-provider"},
                "messages": [], "conversation_id": str(uuid4())}
            cancel = threading.Event()

            def execute():
                try:
                    service.execute(settings, "owned-dummy-never-sent", AgentRequest("Owned task", "agent"),
                        context, cancellation_event=cancel, worker_lifecycle_callback=handles.append)
                except BaseException as exc:
                    outcomes["exception"] = exc

            thread = threading.Thread(target=execute, name="owned-terminal-storage-proof", daemon=True)
            try:
                thread.start()
                self.assertTrue(entered.wait(5), repr(outcomes))
                self.assertTrue(committed.is_set())
                self.assertEqual(len(handles), 1)
                handle = handles[0]
                expected_status = "interrupted" if error == "backend_failed" and storage == "checkpoint" else error
                self.assertEqual(terminal["status"], expected_status)
                stop.assert_called_once_with(handle)
                settle.assert_not_called()
                self.assertEqual(tree.closes, 1)
                self.assertEqual(process.wait_calls, 0)
                self.assertIsNone(service.worker_closure(handle))
                self.assertTrue(service._execution_lock.locked())
                self.assertTrue(thread.is_alive())
                self.assertIn(process.pid, service._processes)
                self.assertIs(service._worker_handles[process.pid], handle)
                self.assertTrue(service.execution_resources_pending())
                self.assertFalse(cancel.is_set())
            finally:
                release.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                # Record unknown closure before this owned fixture's final
                # retirement. No production process or resource is removed.
                try:
                    if handles:
                        handle = handles[0]
                        receipt = service.worker_closure(handle)
                        self.assertIsInstance(outcomes.get("exception"), AgentError)
                        self.assertEqual(outcomes["exception"].code, error)
                        self.assertFalse(service._execution_lock.locked())
                        self.assertEqual(receipt is not None, confirmed)
                        self.assertEqual(handle in service._worker_cleanup_pending, not confirmed)
                        self.assertEqual(process.pid in service._processes, not confirmed)
                        if receipt is not None:
                            self.assertTrue(service.validate_worker_closure(handle, receipt))
                finally:
                    tree.confirmed = True
                    service.reconcile_worker_closures(timeout_seconds=0)
                    service.shutdown()
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0][0], str(interpreter))
            self.assertFalse(service.execution_resources_pending())


if __name__ == "__main__": unittest.main()
