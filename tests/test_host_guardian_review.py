"""Independent offline negative review of the unregistered guardian prototype.

Fake handles below validate failure ordering, not actual Windows containment.
Real fixed-crash/Job/DPAPI canaries remain a separate producer evidence suite.
"""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import job_guardian as guardian
from desktop_pet.agents.guardian_native import GuardianError, minimal_environment
from desktop_pet.agents.job_queue import HostJobReference


def host_identity():
    ref = HostJobReference(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), "a" * 64, "b" * 64)
    ref.validate()
    manifest = guardian.new_manifest(job_id=str(uuid4()), conversation_id=ref.conversation_id,
        generation=ref.generation, operation_id=ref.idempotency_key, installation_id="c" * 64,
        launch_epoch="d" * 64, process_creation_time=1234)
    return manifest, hashlib.sha256(guardian.canonical(manifest)).hexdigest(), b"S" * 32


class ArmAllocationIndependentReviewTests(unittest.TestCase):
    def allocation_failure(self, fail):
        identity = host_identity()
        class FakeNative:
            def __init__(self):
                self.created, self.closed = [], []
                self.calls = 0
                self.k = SimpleNamespace(GetCurrentProcess=lambda: 900,
                    CreateEventW=lambda *_: 0)
            def in_job(self, process, job): return process == 200 and job == 100
            def creation_time(self, _): return 1234
            def pipe(self):
                self.calls += 1
                if fail == "second_pipe" and self.calls == 2: raise GuardianError("GUARDIAN_PIPE_FAILED")
                values = (self.calls * 10 + 1, self.calls * 10 + 2)
                self.created.extend(values)
                return values
            def check(self, value, code):
                if not value: raise GuardianError(code)
                return value
            def close(self, handle):
                if handle: self.closed.append(handle)
        native = FakeNative()
        store = SimpleNamespace(native=native, identity=lambda: identity)
        root = SimpleNamespace(handle=200, resumed=False)
        with patch.object(guardian, "has_pending_cleanup", return_value=False), \
             self.assertRaises(GuardianError):
            guardian.GuardianSession.arm(store, job=SimpleNamespace(handle=100), root=root)
        self.assertEqual(set(native.created), set(native.closed))
        self.assertTrue(root._guardian_bound)  # Failed one-shot binding cannot be reused.

    def test_second_pipe_failure_releases_already_created_bootstrap_pipe(self):
        self.allocation_failure("second_pipe")

    def test_stop_event_failure_releases_both_successfully_created_pipes(self):
        self.allocation_failure("event")

    def test_manifest_uses_real_host_reference_generation_and_rejects_counter_aliases(self):
        manifest, _, _ = host_identity()
        self.assertIsInstance(manifest["generation"], str)
        for wrong in (True, 1, 0, "0", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(kind=type(wrong).__name__), self.assertRaises(GuardianError):
                guardian.validate_manifest({**manifest, "generation": wrong})

    def test_helper_environment_never_inherits_credentials_profile_or_parent_output_settings(self):
        with patch.dict("os.environ", {"SystemRoot": "C:\\Windows", "API_KEY": "SYNTHETIC_SECRET",
            "OPENAI_API_KEY": "SYNTHETIC_SECRET", "HOME": "private", "USERPROFILE": "private",
            "PYTHONPATH": "untrusted", "HTTP_PROXY": "private", "PYTHONIOENCODING": "private"}, clear=True):
            self.assertEqual({"SystemRoot": "C:\\Windows"}, minimal_environment())


class HelperNoFalseReceiptIndependentReviewTests(unittest.TestCase):
    def exercise(self, *, mutation=None, creation=1234, membership=True, helper_in_job=False,
                 terminated=True, closed=True, wait=0):
        helper = importlib.import_module("desktop_pet.agents.job_guardian_helper")
        manifest, digest, key = host_identity()
        import base64
        bootstrap = {"schema": "aura.guardian_bootstrap.v1", "manifest": dict(manifest),
            "manifest_sha256": digest, "key": base64.b64encode(key).decode("ascii"),
            "private_directory": "HOST_PRIVATE_SYNTHETIC_DIRECTORY"}
        if mutation: mutation(bootstrap)
        actions, written = [], []
        class FakeNative:
            def __init__(self): self.k = SimpleNamespace(GetCurrentProcess=lambda: 900,
                WaitForMultipleObjects=lambda *_: wait, TerminateJobObject=lambda *_: terminated)
            def noninherit(self, handle): actions.append(("noninherit", handle))
            def read_bounded(self, *_args, **_kwargs): return guardian.canonical(bootstrap)
            def creation_time(self, _): return creation
            def in_job(self, process, job=None): return helper_in_job if process == 900 else membership
            def write(self, _handle, raw): actions.append(("armed", json.loads(raw)))
            def close(self, handle): actions.append(("close", handle))
            def check(self, value, code):
                if not value: raise helper.GuardianError(code)
                return value
            def confirm_closed(self, *_args, **_kwargs): return closed
        store = SimpleNamespace(identity=lambda: (manifest, digest, key),
            write_closure=lambda *args, **kwargs: written.append((args, kwargs)), close=lambda: None)
        output, error = io.StringIO(), io.StringIO()
        with patch.object(helper, "Native", return_value=FakeNative()), \
             patch.object(helper, "PrivateRunStore", return_value=store), \
             patch.object(helper.sys, "argv", ["trusted-helper", "11", "12", "13", "14", "15", "16"]), \
             redirect_stdout(output), redirect_stderr(error):
            result = helper.main()
        self.assertEqual("", output.getvalue()); self.assertEqual("", error.getvalue())
        return result, written, actions

    def test_manifest_nonce_cid_generation_or_key_mismatch_never_arms_or_writes_receipt(self):
        mutations = [lambda value: value.update(key="invalid"),
            lambda value: value["manifest"].update(run_nonce="0" * 64),
            lambda value: value.update(manifest_sha256="0" * 64),
            lambda value: value.update(manifest={**value["manifest"], "conversation_id": str(uuid4())}),
            lambda value: value.update(manifest={**value["manifest"], "generation": str(uuid4())})]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                result, written, actions = self.exercise(mutation=mutation)
                self.assertNotEqual(0, result)
                self.assertEqual([], written)
                self.assertFalse(any(action[0] == "armed" for action in actions))

    def test_exact_root_creation_or_job_membership_failure_never_arms(self):
        for values in ({"creation": 9999}, {"membership": False}, {"helper_in_job": True}):
            with self.subTest(values=values):
                result, written, actions = self.exercise(**values)
                self.assertEqual(4, result); self.assertEqual([], written)
                self.assertFalse(any(action[0] == "armed" for action in actions))

    def test_armed_is_not_a_receipt_if_termination_right_wait_or_exact_closure_fails(self):
        for values in ({"terminated": False}, {"closed": False}, {"wait": 0xFFFFFFFF}):
            with self.subTest(values=values):
                result, written, actions = self.exercise(**values)
                self.assertNotEqual(0, result); self.assertEqual([], written)
                self.assertTrue(any(action[0] == "armed" for action in actions))

    def test_both_exact_observations_and_trigger_are_required_for_stopped_only_evidence(self):
        result, written, actions = self.exercise(wait=1)
        self.assertEqual(0, result); self.assertEqual(1, len(written))
        self.assertEqual("stop", written[0][1]["trigger"])
        self.assertEqual([11, 12, 13, 14, 15, 16], [action[1] for action in actions if action[0] == "noninherit"])


@unittest.skipUnless(sys.platform == "win32", "Owned Windows pipe and DPAPI format checks")
class WindowsPipeAndReceiptFormatIndependentReviewTests(unittest.TestCase):
    """Real OS pipes/ACL/DPAPI; synthetic receipt data is NOT tree evidence."""
    def test_full_pipe_without_reader_fails_promptly_in_a_killable_child(self):
        project = Path(__file__).resolve().parents[1]
        source = "\n".join([
            "import json,sys,time",
            f"sys.path.insert(0,{str(project)!r})",
            "from desktop_pet.agents.guardian_native import Native,GuardianError",
            "native=Native(); read,write=native.pipe(); started=time.monotonic(); code=None",
            "try:",
            "    native.write(write,b'X'*1048576)",
            "except GuardianError as exc:",
            "    code=str(exc)",
            "finally:",
            "    native.close(read); native.close(write)",
            "print(json.dumps({'code':code,'elapsed_ms':round((time.monotonic()-started)*1000)}))",
        ])
        result = subprocess.run([sys.executable, "-I", "-u", "-c", source],
            capture_output=True, text=True, timeout=5, env=minimal_environment(), creationflags=0x08000000)
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)
        value = json.loads(result.stdout)
        self.assertIn(value["code"], {"GUARDIAN_WRITE_FAILED", "GUARDIAN_PARTIAL_WRITE"})
        self.assertLess(value["elapsed_ms"], 2000)

    def make_synthetic_receipt_store(self):
        from desktop_pet.agents.guardian_native import Native
        owned = tempfile.TemporaryDirectory(prefix="aura-guardian-format-review-")
        self.addCleanup(owned.cleanup)
        manifest, _, _ = host_identity()
        store = guardian.PrivateRunStore.create(owned.name, manifest, Native())
        self.addCleanup(store.close)
        identity = store.identity()
        # Host-signed data fixture only. No process was started or claimed closed.
        store.write_closure(*identity, trigger="stop")
        self.assertIsNotNone(store.read_closure(expected_manifest=manifest))
        return store, manifest

    def test_current_user_dpapi_key_tampering_cannot_authenticate_a_format_fixture(self):
        store, manifest = self.make_synthetic_receipt_store()
        encrypted = (store.path / "key.dpapi").read_bytes()
        corrupt = bytearray(encrypted); corrupt[len(corrupt) // 2] ^= 1
        (store.path / "key.dpapi").write_bytes(corrupt)
        self.assertIsNone(store.read_closure(expected_manifest=manifest))
        self.assertIsNone(store.consume_closure(expected_manifest=manifest))

    def test_cross_thread_exclusive_consume_accepts_one_authenticated_format_fixture(self):
        from concurrent.futures import ThreadPoolExecutor
        store, manifest = self.make_synthetic_receipt_store()
        barrier = threading.Barrier(4)
        def consume(_): barrier.wait(); return store.consume_closure(expected_manifest=manifest)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(consume, range(4)))
        self.assertEqual(1, sum(value is not None for value in results))
        self.assertIsNone(store.consume_closure(expected_manifest=manifest))


if __name__ == "__main__": unittest.main()
