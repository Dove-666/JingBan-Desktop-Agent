"""Actual Qt host crash + exact native stop + real SDK/loopback next-task proof.

This is a controlled production-code host, not the user's running App, a real
phone transport, cloud model, physical desktop or a task-quality benchmark.
"""
from __future__ import annotations

import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from agent_backend.nanobot.tests.test_goal_contract_worker import SeparateGoalModel
from desktop_pet.agents.guardian_native import Native


@unittest.skipUnless(sys.platform == "win32", "actual Windows App/Job/DPAPI proof")
class GuardianProductionCrashTests(unittest.TestCase):
    def exact(self, native, pid, creation):
        native.k.OpenProcess.argtypes = [C.c_uint32, C.c_int, C.c_uint32]
        native.k.OpenProcess.restype = C.c_void_p
        handle = native.k.OpenProcess(0x100000 | 0x1000, False, pid)
        self.assertTrue(handle)
        self.assertEqual(native.creation_time(handle), creation)
        return handle

    def start_app(self, mode, config, workspace, model, *, previous=None):
        args = [sys.executable, "-I", "-u", str(REPO / "tools/qa_guardian_queue_app.py"), "--mode", mode,
            "--config", str(config), "--workspace", str(workspace), "--endpoint", model.base_url]
        if previous:
            args += ["--cid", previous["conversation_id"], "--old-job", previous["job_id"]]
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", close_fds=True, creationflags=0x08000000)
        frames = queue.Queue()
        def read():
            for line in process.stdout:
                frames.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()
        return process, frames

    def test_actual_app_crash_recovers_only_stopped_then_different_queued_task_completes(self):
        from tools.freeze_agent_benchmark_sources import sources
        before = sources(REPO)
        native = Native()
        (REPO / "agent_workspace").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="guardian-crash-task-", dir=REPO / "agent_workspace") as task, \
             tempfile.TemporaryDirectory(prefix="guardian-crash-appconfig-") as config:
            workspace = Path(task)
            entered, release = threading.Event(), threading.Event()
            def pending(_):
                entered.set()
                if not release.wait(25): raise AssertionError("Fixed app crash fixture was not released")
                return "The old unfinished task must not be replayed."
            final = "Created and read b.txt with content B."
            model = SeparateGoalModel([("write_file", {"path": "a.txt", "content": "A"}),
                ("read_file", {"path": "a.txt"}), pending,
                ("write_file", {"path": "b.txt", "content": "B"}),
                ("read_file", {"path": "b.txt"}),
                ("finish_task", {"status": "completed", "summary": final}), final], judge_mode="optimistic")
            apps, handles = [], []
            control = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(60)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=0x08000000)
            report = None
            try:
                first, frames = self.start_app("first", config, workspace, model)
                apps.append(first)
                running = frames.get(timeout=20)
                self.assertEqual(running["phase"], "running")
                self.assertTrue(running["manifest_v2"])
                self.assertTrue(running["queue_binding_matches"])
                self.assertTrue(running["same_lifecycle_object"])
                root = self.exact(native, running["root_pid"], running["root_creation_time"])
                guardian = self.exact(native, running["guardian_pid"], running["guardian_creation_time"])
                handles.extend([root, guardian])
                self.assertTrue(entered.wait(15))
                self.assertEqual((workspace / "a.txt").read_text(), "A")
                version = (workspace / "a.txt").stat().st_mtime_ns
                first.terminate()  # Abrupt: no host finally or shutdown runs.
                first.wait(5)
                self.assertEqual(native.k.WaitForSingleObject(guardian, 7000), 0)
                self.assertTrue(native.signaled(root))
                self.assertIsNone(control.poll(), "Guardian stopped an unrelated owned control")
                release.set()
                second, restored_frames = self.start_app("restart", config, workspace, model, previous=running)
                apps.append(second)
                restored = restored_frames.get(timeout=20)
                self.assertEqual(restored, {"phase": "restored", "old_state": "unknown", "old_worker_closed": True,
                    "old_reply_absent": True, "barrier": False})
                completed = restored_frames.get(timeout=35)
                self.assertEqual(completed["phase"], "completed")
                self.assertTrue(all(completed[key] for key in ("new_job_different", "new_worker_closed", "new_bound",
                    "old_worker_closed", "old_reply_absent", "a_exists", "b_exists")))
                self.assertEqual(completed["old_state"], "unknown")
                self.assertEqual(completed["new_state"], "completed")
                self.assertFalse(completed["barrier"])
                self.assertEqual((workspace / "a.txt").stat().st_mtime_ns, version)
                second.wait(10)
                self.assertEqual(second.returncode, 0)
                self.assertEqual(len(model.planner_calls), 7)
                self.assertEqual(len(model.judge_calls), 1)
                self.assertEqual(model.errors, [])
                self.assertEqual(sources(REPO), before)
                report = {"schema": "aura.remote_guardian_actual_crash.v1", "passed": True,
                    "actual_qt_controller_and_service": True, "real_sdk_loopback_only": True,
                    "authenticated_remote_handler": True, "real_phone_transport": False,
                    "abrupt_host_exit": True, "exact_root_and_guardian_handles_signaled": True,
                    "unrelated_control_alive": True, "manifest_v2_db_binding": True,
                    "old_job_stopped_not_completed": True, "old_file_version_unchanged": True,
                    "new_different_job_completed": True, "planner_requests": 7, "judge_requests": 1,
                    "source_unchanged": True, "source_files": len(before),
                    "source_manifest_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                    "production_user_app_or_credentials_touched": False, "loaded_dependency_attestation": False}
            finally:
                release.set()
                for process in apps:
                    if process.poll() is None: process.terminate()
                    process.wait(5)
                    process.stdout.close()
                    error = process.stderr.read()
                    process.stderr.close()
                    self.assertEqual(error, "")
                if control.poll() is None: control.terminate()
                control.wait(5)
                for handle in handles: native.close(handle)
                model.close()
            target = REPO / "artifacts/guardian-production-v1-actual-crash.json"
            target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__": unittest.main()
