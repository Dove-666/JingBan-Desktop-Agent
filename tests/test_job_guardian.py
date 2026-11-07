"""Independent fixed Win32 crash canaries for the unregistered guardian.

PIDs only acquire handles while known live, then creation times are checked.
All closure assertions use retained exact process handles plus authenticated
Job-empty evidence. No model, actual App, queue, documents or network is used.
"""
from __future__ import annotations

import ctypes as C
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from desktop_pet.agents.guardian_native import Native, GuardianError
from desktop_pet.agents.job_guardian import PrivateRunStore, new_manifest, canonical, decode

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tools" / "qa_job_guardian_fixture.py"
MEASUREMENTS = {"helper_working_set_bytes": [], "guardian_in_external_job": []}


class MemoryCounters(C.Structure):
    _fields_ = [("size", C.c_uint32), ("faults", C.c_uint32), *[
        (name, C.c_size_t) for name in ("peak_working_set", "working_set", "peak_paged", "paged",
                                      "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]]


class RecordTests(unittest.TestCase):
    def test_strict_identity_and_bounded_canonical_records(self):
        manifest = new_manifest(job_id=str(uuid4()), conversation_id=str(uuid4()), generation=str(uuid4()),
            operation_id=str(uuid4()), installation_id="a" * 64, launch_epoch="b" * 64,
            process_creation_time=1234)
        self.assertEqual(decode(canonical(manifest)), manifest)
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{ "x":1}', b'[]', b'{}\n', b'x' * 4097):
            with self.subTest(case=len(raw)), self.assertRaises(GuardianError):
                decode(raw)
        for generation in (1, True, None, -1, 2**64, "", "not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            with self.assertRaises(GuardianError):
                new_manifest(job_id=str(uuid4()), conversation_id=str(uuid4()), generation=generation,
                    operation_id=str(uuid4()), installation_id="a" * 64, launch_epoch="b" * 64,
                    process_creation_time=1234)


@unittest.skipUnless(sys.platform == "win32", "real Windows handle/Job/DPAPI proof")
class GuardianCrashTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-guardian-fixed-owned-")
        self.root = Path(self.temporary.name)
        self.native = Native()
        self.handles, self.parents, self.stores = [], [], []
        self.guardians = {}
        self.native.k.OpenProcess.argtypes = [C.c_uint32, C.c_int, C.c_uint32]
        self.native.k.OpenProcess.restype = C.c_void_p
        self.native.k.GetExitCodeProcess.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
        self.native.k.GetExitCodeProcess.restype = C.c_int

    def tearDown(self):
        for parent in self.parents:
            if parent.poll() is None:
                parent.terminate()
            parent.wait(timeout=5)
            parent.stdin.close()
            parent.stdout.close()
            self.assertEqual(parent.stderr.read(), "")
            parent.stderr.close()
        for store in self.stores:
            store.close()
        for handle in self.handles:
            # Exact trusted fixture handles only. Even a failed test leaves no
            # waiting descendant; the fixture also self-expires at 15/20s.
            if not self.native.signaled(handle):
                self.native.k.TerminateProcess(handle, 77)
            self.native.k.WaitForSingleObject(handle, 2000)
            self.native.close(handle)
        self.temporary.cleanup()

    def exact(self, pid, creation):
        handle = self.native.k.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        self.assertTrue(handle, "Known live fixture process could not be opened")
        self.handles.append(handle)
        self.assertEqual(self.native.creation_time(handle), creation)
        return handle

    def start(self, mode="running"):
        private = self.root / ("private-" + str(len(self.parents)))
        private.mkdir()
        parent = subprocess.Popen([sys.executable, "-I", "-u", str(FIXTURE), "parent", "--root", str(private), "--mode", mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", close_fds=True, creationflags=0x08000000)
        self.parents.append(parent)
        frames = queue.Queue()
        threading.Thread(target=lambda: frames.put(parent.stdout.readline()), daemon=True).start()
        line = frames.get(timeout=10)
        self.assertTrue(line, "Fixed parent exited before its rendezvous")
        value = json.loads(line)
        self.assertNotIn("fixture-worker-output", line)
        root = self.exact(value["root_pid"], value["root_creation_time"])
        guardian = child = None
        if "guardian_pid" in value:
            guardian = self.exact(value["guardian_pid"], value["guardian_creation_time"])
            MEASUREMENTS["guardian_in_external_job"].append(value["guardian_in_external_job"])
            get_memory = self.native.k.K32GetProcessMemoryInfo
            get_memory.argtypes = [C.c_void_p, C.POINTER(MemoryCounters), C.c_uint32]
            get_memory.restype = C.c_int
            memory = MemoryCounters()
            memory.size = C.sizeof(memory)
            if get_memory(guardian, C.byref(memory), memory.size):
                MEASUREMENTS["helper_working_set_bytes"].append(memory.working_set)
        if "grandchild_pid" in value:
            child = self.exact(value["grandchild_pid"], value["grandchild_creation_time"])
        store = None
        if "manifest" in value:
            store = PrivateRunStore(private / value["manifest"]["run_nonce"], self.native)
            self.stores.append(store)
            self.guardians[value["manifest"]["run_nonce"]] = guardian
        return parent, value, root, guardian, child, store

    def wait_evidence(self, store, manifest, timeout=7):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = store.read_closure(expected_manifest=manifest)
            if result is not None:
                return result
            time.sleep(.02)
        code = C.c_uint32()
        guardian = self.guardians[manifest["run_nonce"]]
        if guardian:
            self.native.k.GetExitCodeProcess(guardian, C.byref(code))
        self.fail(json.dumps({"failure": "NO_AUTHENTICATED_CLOSURE", "helper_exit_code": code.value,
            "receipt_exists": (store.path / "receipt.json").exists(), "temporary_exists": (store.path / "receipt.tmp").exists()}))

    def test_parent_abrupt_exit_closes_root_and_grandchild_with_exact_evidence(self):
        parent, value, root, guardian, child, store = self.start()
        self.assertFalse(self.native.signaled(root))
        self.assertFalse(self.native.signaled(child))
        self.assertFalse(value["canary_inherited"])
        self.assertFalse(value["ambient_event_signaled"])
        self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))
        parent.terminate()
        parent.wait(5)
        result = self.wait_evidence(store, value["manifest"])
        self.assertTrue(result.worker_stopped)
        self.assertEqual(result.trigger, "app_exit")
        self.assertTrue(self.native.signaled(root))
        self.assertTrue(self.native.signaled(child))
        self.assertEqual(self.native.k.WaitForSingleObject(guardian, 5000), 0)
        raw = store._read("receipt.json")
        self.assertLessEqual(len(raw), 4096)
        self.assertNotIn(b"fixture-worker-output", raw)
        self.assertNotIn(b"private-", raw)
        self.assertIsNotNone(store.consume_closure(expected_manifest=value["manifest"]))
        self.assertIsNone(store.consume_closure(expected_manifest=value["manifest"]))

    def test_explicit_stop_proves_same_tree_without_app_exit(self):
        parent, value, root, guardian, child, store = self.start()
        parent.stdin.write("STOP\n")
        parent.stdin.flush()
        result = self.wait_evidence(store, value["manifest"])
        self.assertEqual(result.trigger, "stop")
        self.assertTrue(self.native.signaled(root))
        self.assertTrue(self.native.signaled(child))
        parent.wait(6)
        self.assertEqual(self.native.k.WaitForSingleObject(guardian, 5000), 0)

    def test_root_exit_is_not_tree_closure_while_grandchild_lives(self):
        parent, value, root, guardian, child, store = self.start("root_exit")
        self.assertEqual(self.native.k.WaitForSingleObject(root, 2000), 0)
        self.assertFalse(self.native.signaled(child))
        self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))
        parent.terminate()
        parent.wait(5)
        self.wait_evidence(store, value["manifest"])
        self.assertTrue(self.native.signaled(child))

    def test_crash_before_armed_retains_unknown_even_when_job_kill_closes_root(self):
        for mode in ("before_manifest", "before_arm"):
            with self.subTest(mode=mode):
                parent, value, root, guardian, child, store = self.start(mode)
                self.assertIsNone(guardian)
                self.assertIsNone(child)
                parent.terminate()
                parent.wait(5)
                self.assertEqual(self.native.k.WaitForSingleObject(root, 3000), 0)
                if store is not None:
                    self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))

    def test_armed_before_resume_parent_crash_proves_suspended_worker_stopped(self):
        parent, value, root, guardian, child, store = self.start("before_resume")
        self.assertIsNone(child)
        self.assertFalse(self.native.signaled(root))
        parent.terminate()
        parent.wait(5)
        self.wait_evidence(store, value["manifest"])
        self.assertTrue(self.native.signaled(root))

    def test_guardian_killed_and_app_exit_never_manufactures_receipt(self):
        parent, value, root, guardian, child, store = self.start()
        self.native.check(self.native.k.TerminateProcess(guardian, 77), "FIXTURE_STOP_FAILED")
        self.assertEqual(self.native.k.WaitForSingleObject(guardian, 3000), 0)
        self.assertFalse(self.native.signaled(child))
        parent.terminate()
        parent.wait(5)
        self.assertEqual(self.native.k.WaitForSingleObject(child, 3000), 0)
        self.assertEqual(self.native.k.WaitForSingleObject(root, 3000), 0)
        self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))

    def test_receipt_identity_forgery_partial_and_hardlink_are_rejected(self):
        parent, value, root, guardian, child, store = self.start()
        parent.terminate()
        parent.wait(5)
        self.wait_evidence(store, value["manifest"])
        for field, replacement in (("conversation_id", str(uuid4())), ("generation", str(uuid4())),
                ("run_nonce", "0" * 64), ("operation_id", str(uuid4())), ("launch_epoch", "3" * 64)):
            with self.subTest(field=field):
                self.assertIsNone(store.read_closure(expected_manifest={**value["manifest"], field: replacement}))
        path = store.path / "receipt.json"
        original = path.read_bytes()
        forged = json.loads(original)
        forged["trigger"] = "stop"
        path.write_bytes(canonical(forged))
        self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))
        path.write_bytes(original[:len(original) // 2])
        self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))
        path.write_bytes(original)
        alias = store.path / "hardlink-fixture"
        os.link(path, alias)
        self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))
        alias.unlink()
        self.assertIsNotNone(store.read_closure(expected_manifest=value["manifest"]))

    def test_missing_query_right_fails_armed_and_keeps_worker_suspended(self):
        parent, value, root, guardian, child, store = self.start("query_denied")
        self.assertEqual(value["phase"], "arm_failed")
        self.assertFalse(value["root_resumed"])
        self.assertIsNone(guardian)
        self.assertIsNone(child)
        self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))

    def test_query_only_job_handle_cannot_mint_receipt_on_failed_terminate(self):
        parent, value, root, guardian, child, store = self.start("terminate_denied")
        parent.stdin.write("STOP_KEEP\n")
        parent.stdin.flush()
        self.assertEqual(self.native.k.WaitForSingleObject(guardian, 6000), 0)
        code = C.c_uint32()
        self.native.k.GetExitCodeProcess(guardian, C.byref(code))
        self.assertEqual(code.value, 25)
        self.assertFalse(self.native.signaled(child))
        self.assertFalse(self.native.signaled(root))
        self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))
        parent.terminate()
        parent.wait(5)
        self.assertEqual(self.native.k.WaitForSingleObject(child, 3000), 0)
        self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))

    def test_failed_atomic_publish_is_unknown_even_when_actual_tree_closed(self):
        parent, value, root, guardian, child, store = self.start()
        store._write_new("receipt.json", b"{}")
        parent.terminate()
        parent.wait(5)
        self.assertEqual(self.native.k.WaitForSingleObject(guardian, 6000), 0)
        code = C.c_uint32()
        self.native.k.GetExitCodeProcess(guardian, C.byref(code))
        self.assertEqual(code.value, 23)
        self.assertTrue(self.native.signaled(child))
        self.assertTrue(self.native.signaled(root))
        self.assertTrue((store.path / "receipt.tmp").exists())
        self.assertIsNone(store.read_closure(expected_manifest=value["manifest"]))

    def test_full_private_pipe_write_without_any_reader_never_waits_for_buffer_space(self):
        read, write = self.native.pipe()
        try:
            started = time.monotonic()
            with self.assertRaises(GuardianError) as failure:
                self.native.write(write, b"F" * (8 * 1024 * 1024))
            self.assertIn(failure.exception.args[0], {"GUARDIAN_PARTIAL_WRITE", "GUARDIAN_WRITE_FAILED"})
            self.assertLess(time.monotonic() - started, .5)
        finally:
            self.native.close(read)
            self.native.close(write)


if __name__ == "__main__":
    unittest.main()
