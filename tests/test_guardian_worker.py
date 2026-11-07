"""Native suspended adapter compatibility; no production service or model."""
from __future__ import annotations

import ctypes as C
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents.guardian_native import GuardianError, Native
from desktop_pet.agents.guardian_worker import (GuardianWorker, PrivateGuardianLocation,
    consume_restarted_stopped, pending_cleanup_count, reconcile_cleanup)
from desktop_pet.agents.process_lifecycle import WorkerLifecycle
from desktop_pet.agents.execution_attempt import ExecutionAttempts
from desktop_pet.agents.job_queue import HostJobReference
from desktop_pet.agents.staged_input import StagedRunInput, StagedInputError

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tools" / "qa_guardian_worker_fixture.py"
from tools.qa_guardian_worker_fixture import read_frame


@unittest.skipUnless(sys.platform == "win32", "native Windows suspended worker/Job proof")
class GuardianWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-native-worker-owned-")
        self.base = Path(self.temporary.name)
        self.config, self.workspace = self.base / "appconfig", self.base / "workspace"
        self.config.mkdir(); self.workspace.mkdir()
        self.native = Native()
        self.location = PrivateGuardianLocation.prepare(self.config, model_scopes=(self.workspace,), native=self.native)
        self.lifecycle = WorkerLifecycle()
        self.processes, self.parents, self.retained = [], [], []

    def tearDown(self):
        for parent in self.parents:
            if parent.poll() is None: parent.terminate()
            parent.wait(5)
            parent.stdin.close(); parent.stdout.close()
            self.assertEqual(parent.stderr.read(), "")
            parent.stderr.close()
        for process in self.processes:
            self.assertTrue(process.cleanup(timeout_seconds=3))
        self.assertTrue(reconcile_cleanup(timeout_seconds=1))
        self.assertEqual(pending_cleanup_count(), 0)
        for handle in self.retained:
            if not self.native.signaled(handle): self.native.k.TerminateProcess(handle, 77)
            self.native.k.WaitForSingleObject(handle, 2000)
            self.native.close(handle)
        self.location.close()
        self.temporary.cleanup()

    def create(self, *, no_read=False, callback=None, stderr_to_null=False, stderr_bytes=0, job_active_limit=8):
        reference = HostJobReference(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), "a" * 64)
        attempts = ExecutionAttempts()
        attempt = attempts.begin(reference.conversation_id)
        phases, handles = [], []
        marker = self.workspace / (str(uuid4()) + ".txt")
        def registered(process):
            self.processes.append(process)
            handle = self.lifecycle.register(process, str(uuid4()), reference.conversation_id)
            self.lifecycle.attach_tree(handle, process.tree)
            handles.append(handle)
            if callback is not None: callback(process)
        argv = [sys.executable, "-I", "-u", str(FIXTURE), "worker", "--marker", str(marker)]
        if no_read: argv.append("--no-read")
        if stderr_bytes: argv.extend(["--stderr-bytes", str(stderr_bytes)])
        process = GuardianWorker.create(argv, cwd=REPO, location=self.location, job_id=str(uuid4()), reference=reference,
            installation_id="b" * 64, launch_epoch="c" * 64,
            launch_attempt_callback=lambda: attempts.mark_launch_attempted(attempt),
            lifecycle_callback=registered, phase_callback=phases.append,
            stderr_to_null=stderr_to_null, job_active_limit=job_active_limit)
        return process, handles[0], marker, phases, attempts, attempt

    def exact(self, pid, creation):
        self.native.k.OpenProcess.argtypes = [C.c_uint32, C.c_int, C.c_uint32]
        self.native.k.OpenProcess.restype = C.c_void_p
        handle = self.native.k.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        self.assertTrue(handle)
        self.assertEqual(self.native.creation_time(handle), creation)
        self.retained.append(handle)
        return handle

    def test_ready_stage_guarded_lf_unicode_stdio_and_same_lifecycle_identity(self):
        process, handle, marker, phases, attempts, attempt = self.create()
        self.assertNotIsInstance(process, subprocess.Popen)
        self.assertIs(handle._process, process)
        self.assertFalse(process._root.resumed)
        self.assertFalse(marker.exists())
        self.assertEqual(process.read_available(), b"")
        with self.assertRaises(subprocess.TimeoutExpired): process.wait(timeout=0)
        self.assertLess(phases.index("LAUNCH_ATTEMPT_MARKED"), phases.index("ROOT_CREATED_SUSPENDED"))
        self.assertLess(phases.index("LIFECYCLE_BOUND"), phases.index("JOB_BOUND"))
        self.assertLess(phases.index("JOB_BOUND"), phases.index("MANIFEST_DURABLE"))
        self.assertLess(phases.index("MANIFEST_DURABLE"), phases.index("GUARDIAN_ARMED"))
        process.resume()
        self.assertEqual(read_frame(process), {"type": "ready", "protocol": 1, "sdk": "nanobot-ai", "sdk_version": "0.4.0"})
        event = threading.Event()
        writer = StagedRunInput(process.stdin, stop_owned=lambda: self.lifecycle.request_stop(handle), cancellation_event=event)
        process.bind_writer(writer); writer.start()
        writer.stage(json.dumps({"fixture": "trusted-fixed-worker", "text": "中文 🌊 😀", "spawn_child": False}, ensure_ascii=False).encode("utf-8"))
        self.assertFalse(marker.exists())
        self.assertEqual(process.read_available(), b"")
        with threading.Lock():
            self.assertFalse(event.is_set())
            process.validate_private_scope()
            self.lifecycle.mark_dispatched(handle)
            writer.commit()
        self.assertTrue(writer.close())
        result = read_frame(process)
        self.assertEqual(result, {"type": "result", "text": "中文 🌊 😀", "canary_inherited": False})
        self.assertEqual(marker.read_text(), "DISPATCHED")
        self.assertEqual(process.read_available(process.stderr), b"fixed stderr synthetic marker\r\n")
        receipt = self.lifecycle.settle(handle, timeout_seconds=3)
        self.assertTrue(self.lifecycle.validates(handle, receipt))
        self.assertIs(handle._process, process)
        self.assertTrue(all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr)))
        self.assertIsInstance(process.poll(), int)
        attempts.finish(attempt)
        self.assertIsNone(attempts.receipt(attempt), "Native creation cannot become a no-launch receipt")
        proof = consume_restarted_stopped(self.location, process.manifest)
        self.assertIsNotNone(proof)
        self.assertTrue(proof.worker_stopped)
        self.assertIsNone(consume_restarted_stopped(self.location, process.manifest))

    def test_cancelled_guard_before_lf_never_dispatches_and_closes_exact_worker(self):
        process, handle, marker, _, _, _ = self.create()
        process.resume(); read_frame(process)
        event = threading.Event()
        writer = StagedRunInput(process.stdin, stop_owned=lambda: self.lifecycle.request_stop(handle), cancellation_event=event)
        process.bind_writer(writer); writer.start()
        writer.stage(b'{"fixture":"trusted-fixed-worker","text":"fixed","spawn_child":false}')
        event.set()
        with threading.Lock(), self.assertRaises(StagedInputError): writer.commit()
        receipt = self.lifecycle.settle(handle, timeout_seconds=3)
        self.assertTrue(self.lifecycle.validates(handle, receipt))
        self.assertFalse(marker.exists())

    def test_nonreading_worker_large_stage_watchdog_stops_job_and_unblocks_writer(self):
        process, handle, marker, _, _, _ = self.create(no_read=True)
        process.resume(); read_frame(process)
        writer = StagedRunInput(process.stdin, stop_owned=lambda: self.lifecycle.request_stop(handle),
            cancellation_event=threading.Event(), stage_timeout=.15)
        process.bind_writer(writer); writer.start()
        started = time.monotonic()
        with self.assertRaises(StagedInputError): writer.stage(b"x" * (8 * 1024 * 1024))
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(writer.close())
        self.assertTrue(self.lifecycle.validates(handle, self.lifecycle.settle(handle, timeout_seconds=3)))
        self.assertFalse(marker.exists())

    def test_original_lifecycle_callback_exception_is_not_replaced_by_cleanup(self):
        primary = RuntimeError("fixed host lifecycle exception")
        def failed(_): raise primary
        with self.assertRaises(RuntimeError) as caught:
            self.create(callback=failed)
        self.assertIs(caught.exception, primary)
        process = self.processes[-1]
        self.assertFalse(process._root.resumed)
        self.assertTrue(process._confirmed_closed)
        self.assertEqual(pending_cleanup_count(), 0)

    def test_any_unconfirmed_writer_guardian_job_or_pipe_retains_exact_owner_and_blocks_new_launch(self):
        for boundary in ("writer", "guardian", "job", "pipe"):
            with self.subTest(boundary=boundary):
                process, _, _, _, _, _ = self.create()
                original_job, original_root = process._job.handle, process._root.handle
                if boundary == "writer":
                    writer = type("OwnedUncooperativeWriter", (), {"close": lambda self, **_: False})()
                    process.bind_writer(writer)
                    blocker = patch.object(writer, "close", return_value=False)
                elif boundary == "guardian":
                    original_wait = process.native.k.WaitForSingleObject
                    guardian_handle = process.guardian.process.handle
                    blocker = patch.object(process.native.k, "WaitForSingleObject",
                        side_effect=lambda handle, timeout: 258 if handle == guardian_handle else original_wait(handle, timeout))
                elif boundary == "job":
                    blocker = patch.object(process.native, "job_empty", return_value=False)
                else:
                    blocker = patch.object(process.stdout, "close_checked", return_value=False)
                with blocker:
                    self.assertFalse(process.cleanup(timeout_seconds=.15))
                    self.assertEqual(pending_cleanup_count(), 1)
                    self.assertEqual(process._job.handle, original_job)
                    self.assertEqual(process._root.handle, original_root)
                    with self.assertRaises(GuardianError) as busy: self.create()
                    self.assertEqual(busy.exception.args[0], "GUARDIAN_WORKER_BUSY")
                if boundary == "writer":
                    writer.close = lambda **_: True
                self.assertTrue(reconcile_cleanup(timeout_seconds=2))
                self.assertTrue(process._confirmed_closed)

    def test_private_storage_rejects_workspace_and_authorized_ancestor_overlap(self):
        for scope in (self.config, self.base, self.config / "host_job_guardian" / "future-model-grant"):
            with self.subTest(scope=scope.name), self.assertRaises(GuardianError):
                PrivateGuardianLocation.prepare(self.config, model_scopes=(scope,), native=self.native)

    def test_fresh_host_grant_callback_rejects_late_private_parent_at_resume_and_lf_guard(self):
        current = [self.workspace]
        old = self.location
        self.location = PrivateGuardianLocation.prepare(self.config, model_scopes=lambda: tuple(current), native=self.native)
        old.close()
        process, handle, marker, _, _, _ = self.create()
        current.append(self.config)
        with self.assertRaises(GuardianError) as blocked: process.resume()
        self.assertEqual(blocked.exception.args[0], "GUARDIAN_STORAGE_SCOPE_OVERLAP")
        self.assertFalse(process._root.resumed)
        current.pop()
        process.resume(); read_frame(process)
        writer = StagedRunInput(process.stdin, stop_owned=lambda: self.lifecycle.request_stop(handle), cancellation_event=threading.Event())
        process.bind_writer(writer); writer.start()
        writer.stage(b'{"fixture":"trusted-fixed-worker","text":"fixed","spawn_child":false}')
        with threading.Lock():
            current.append(self.base)  # Newly authorized ancestor contains AppConfig.
            with self.assertRaises(GuardianError) as blocked: process.validate_private_scope()
            self.assertEqual(blocked.exception.args[0], "GUARDIAN_STORAGE_SCOPE_OVERLAP")
            # No mark_dispatched/commit after failed fresh exclusion check.
        self.assertFalse(marker.exists())
        self.assertTrue(self.lifecycle.validates(handle, self.lifecycle.settle(handle, timeout_seconds=3)))

    def test_model_dict_cannot_supply_host_scope_provider(self):
        with self.assertRaises(GuardianError):
            PrivateGuardianLocation.prepare(self.config, model_scopes={"workspace": str(self.workspace)}, native=self.native)

    def test_descriptor_conversion_failure_preserves_original_error_and_closes_owned_fd(self):
        primary = RuntimeError("fixed FileIO construction failure")
        import desktop_pet.agents.guardian_worker as module
        with patch.object(module.io, "FileIO", side_effect=primary):
            with self.assertRaises(RuntimeError) as caught:
                self.create()
            self.assertIs(caught.exception, primary)
            self.assertEqual(pending_cleanup_count(), 0)
        self.assertTrue(reconcile_cleanup(timeout_seconds=2))
        self.assertEqual(pending_cleanup_count(), 0)

    def test_close_exception_after_closed_property_retains_registry_and_reconcile_cannot_erase_unknown(self):
        # A sticky unknown cannot be cleared by a test-only reset in the live
        # host. Use a bounded isolated trusted fixture; process exit releases
        # its already-stopped exact native resources after assertions.
        result = subprocess.run([sys.executable, "-I", "-u", str(FIXTURE), "close_fault", "--config", str(self.config),
            "--workspace", str(self.workspace)], capture_output=True, text=True, encoding="utf-8", timeout=10,
            creationflags=0x08000000)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"first_confirmed": False, "second_confirmed": False,
            "reconcile_confirmed": False, "registry_busy": True, "pending_count": 1, "kept_exact_identity": True})

    def test_guardian_failure_does_not_block_live_exact_cleanup_but_missing_receipt_stays_unknown_on_restart(self):
        process, handle, marker, _, _, _ = self.create()
        guardian = process.guardian.process.handle
        self.native.check(self.native.k.TerminateProcess(guardian, 77), "FIXTURE_STOP_FAILED")
        self.assertEqual(self.native.k.WaitForSingleObject(guardian, 3000), 0)
        receipt = self.lifecycle.settle(handle, timeout_seconds=3)
        self.assertTrue(self.lifecycle.validates(handle, receipt))
        self.assertFalse(marker.exists())
        self.assertIsNone(consume_restarted_stopped(self.location, process.manifest))

    def test_production_stderr_null_does_not_block_large_output_before_ready(self):
        process, handle, marker, _, _, _ = self.create(stderr_to_null=True, stderr_bytes=8 * 1024 * 1024, job_active_limit=None)
        self.assertIsNone(process.stderr)
        from desktop_pet.agents.guardian_native import ExtendedLimits
        limits = ExtendedLimits()
        self.assertTrue(process.native.k.QueryInformationJobObject(process._job.handle, 9, C.byref(limits), C.sizeof(limits), None))
        self.assertEqual(limits.basic.flags, 0x2000)
        self.assertEqual(limits.basic.active_limit, 0)
        process.resume()
        self.assertEqual(read_frame(process)["type"], "ready")
        process.stdin.write(b'{"fixture":"trusted-fixed-worker","text":"ok","spawn_child":false}\n')
        process.stdin.flush()
        self.assertEqual(read_frame(process)["text"], "ok")
        self.assertTrue(marker.exists())
        self.assertTrue(self.lifecycle.validates(handle, self.lifecycle.settle(handle, timeout_seconds=3)))

    def test_undrained_stderr_pipe_control_blocks_ready_until_owned_job_stop(self):
        process, handle, marker, _, _, _ = self.create(stderr_bytes=8 * 1024 * 1024)
        process.resume()
        with self.assertRaisesRegex(ValueError, "FIXTURE_FRAME_TIMEOUT"):
            read_frame(process, timeout=.15)
        self.assertFalse(marker.exists())
        self.assertTrue(self.lifecycle.validates(handle, self.lifecycle.settle(handle, timeout_seconds=3)))

    def start_parent(self, *, pause="staged"):
        frames = queue.Queue()
        parent = subprocess.Popen([sys.executable, "-I", "-u", str(FIXTURE), "parent", "--config", str(self.config),
            "--workspace", str(self.workspace), "--pause", pause], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", close_fds=True, creationflags=0x08000000)
        self.parents.append(parent)
        def read():
            for line in parent.stdout: frames.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()
        first = frames.get(timeout=10)
        root = self.exact(first["root_pid"], first["root_creation_time"])
        guardian = self.exact(first["guardian_pid"], first["guardian_creation_time"])
        return parent, frames, first, root, guardian

    def test_real_parent_staged_commit_and_stop_uses_native_object_without_ambient_handle_leak(self):
        parent, frames, first, root, guardian = self.start_parent()
        self.assertEqual(first["phase"], "staged")
        self.assertFalse(first["marker_exists"])
        self.assertTrue(first["same_lifecycle_object"])
        self.assertEqual(first["launch_attempt_count"], 1)
        parent.stdin.write("COMMIT\n"); parent.stdin.flush()
        committed = frames.get(timeout=5)
        self.assertEqual(committed["result"]["text"], "Unicode 🌊 中文")
        self.assertFalse(committed["result"]["canary_inherited"])
        self.assertFalse(committed["ambient_event_signaled"])
        child = self.exact(committed["result"]["child_pid"], committed["result"]["child_creation_time"])
        parent.stdin.write("STOP\n"); parent.stdin.flush()
        closed = frames.get(timeout=7)
        self.assertTrue(closed["closure_valid"])
        self.assertTrue(closed["durable_stopped"])
        self.assertTrue(closed["stdio_closed"])
        self.assertTrue(closed["same_lifecycle_object"])
        self.assertEqual(closed["pending_count"], 0)
        parent.wait(5)
        self.assertTrue(self.native.signaled(root)); self.assertTrue(self.native.signaled(child))
        self.assertEqual(self.native.k.WaitForSingleObject(guardian, 5000), 0)

    def test_parent_abrupt_exit_before_dispatch_and_after_commit_restore_stopped_only(self):
        for pause, commit in (("armed", False), ("staged", False), ("staged", True)):
            with self.subTest(pause=pause, commit=commit):
                parent, frames, first, root, guardian = self.start_parent(pause=pause)
                child = None
                if commit:
                    parent.stdin.write("COMMIT\n"); parent.stdin.flush()
                    committed = frames.get(timeout=5)
                    child = self.exact(committed["result"]["child_pid"], committed["result"]["child_creation_time"])
                self.assertIsNone(consume_restarted_stopped(self.location, first["manifest"]))
                parent.terminate(); parent.wait(5)
                self.assertEqual(self.native.k.WaitForSingleObject(guardian, 6000), 0)
                proof = consume_restarted_stopped(self.location, first["manifest"])
                self.assertIsNotNone(proof)
                self.assertTrue(proof.worker_stopped)
                self.assertEqual(proof.trigger, "app_exit")
                self.assertFalse(hasattr(proof, "completed"))
                self.assertTrue(self.native.signaled(root))
                if child is not None: self.assertTrue(self.native.signaled(child))
                if not commit: self.assertFalse((self.workspace / "dispatch-marker.txt").exists())


if __name__ == "__main__": unittest.main()
