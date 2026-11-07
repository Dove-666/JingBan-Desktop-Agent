"""Independent dispatch ordering checks; all Win32 calls are in-memory fakes."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from agent_backend.nanobot import mxc_execution as mxc, mxc_native as native
from tests.test_mxc_backend_review import FakeKernel, Function


class MxcDispatchIndependentReviewTests(unittest.TestCase):
    def invoke(self, *, kernel=None, **kwargs):
        kernel = kernel or FakeKernel()
        with patch.object(native.C, "WinDLL", return_value=kernel):
            process = native.WindowsJobProcess()
        process.terminate_and_reap = lambda: True
        process.read_available = lambda _: b""
        process.accounting = lambda: SimpleNamespace(user_time=0, active_processes=0)
        process.exited = lambda: True
        return native.run([r"C:\owned\fixed.exe"], environment={"SystemRoot": r"C:\Windows"},
            cwd=Path(r"E:\owned"), policy=mxc.MxcPolicy(), cancel_event=threading.Event(),
            watch=lambda: True, factory=lambda: process, **kwargs), kernel

    def test_real_second_thread_cannot_revoke_inside_shared_resume_guard(self):
        lock = threading.RLock()
        attempted, revoked = threading.Event(), threading.Event()
        order, threads = [], []
        kernel = FakeKernel()
        original_resume = kernel.ResumeThread.callback
        def revoke():
            attempted.set()
            with lock:
                order.append("revoked"); revoked.set()
        def resume(*args):
            thread = threading.Thread(target=revoke); threads.append(thread); thread.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(revoked.wait(.03))
            order.append("resume")
            return original_resume(*args)
        kernel.ResumeThread = Function(resume)
        try:
            result, _ = self.invoke(kernel=kernel, authority_guard=lambda: lock)
        finally:
            for thread in threads: thread.join(2)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(["resume", "revoked"], order)
        self.assertTrue(result.resumed and result.tree_reaped and result.resources_closed)
        self.assertEqual(1, kernel.events.count("resume"))

    def test_cancelled_error_in_guard_entry_is_fixed_and_never_resumes(self):
        @contextmanager
        def refused():
            raise asyncio.CancelledError("SYNTHETIC_PRIVATE_DETAIL")
            yield
        result, kernel = self.invoke(authority_guard=refused)
        self.assertEqual("MXC_RESUME_AUTHORITY_FAILED", result.code)
        self.assertFalse(result.resumed)
        self.assertNotIn("resume", kernel.events)
        self.assertTrue(result.tree_reaped and result.resources_closed)
        self.assertNotIn(b"PRIVATE_DETAIL", result.stdout + result.stderr)

    def test_failed_attempt_record_prevents_even_suspended_creation(self):
        def refused(): raise RuntimeError("SYNTHETIC_PRIVATE_DETAIL")
        result, kernel = self.invoke(launch_attempt_callback=refused)
        self.assertEqual("MXC_LAUNCH_AUTHORITY_FAILED", result.code)
        self.assertFalse(result.resumed)
        self.assertNotIn("create-suspended", kernel.events)
        self.assertNotIn("resume", kernel.events)
        self.assertTrue(result.tree_reaped and result.resources_closed)


if __name__ == "__main__": unittest.main()
