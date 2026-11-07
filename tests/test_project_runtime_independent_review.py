"""Independent runtime ownership/publication controls, no code/model execution.

Native transport is a labelled trusted fake writing fixed temporary bytes. It
does not validate MXC/Windows isolation, GUI restrictions or machine enablement.
"""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

# Match the actual -I worker's standalone import graph; capabilities from a
# second package module are deliberately not substituted into its harness.
from agent_backend.nanobot.tests import test_worker as sdk_fixture
import project_runtime as subject
import mxc_execution as mxc
import mxc_native as native
from safe_files import WorkspaceFiles
from task_harness import TaskHarness, HarnessError
from goal_evidence import GoalEvidence
from desktop_pet.agents import execution_runtime as host


class ProjectRuntimeIndependentReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="project-independent-review-")
        self.root = Path(self.owned.name)
        self.workspace = self.root/"workspace"; self.workspace.mkdir()
        (self.workspace/"source.txt").write_bytes(b"2 3")
        self.files = WorkspaceFiles(self.workspace)
        self.revision, self.action = "owned-revision", "b"*64
        self.authorized, self.cancel, self.guard = True, threading.Event(), threading.RLock()
        self.entered, self.release = threading.Event(), threading.Event(); self.release.set()
        self.hold, self.calls, self.runtimes = False, [], []
        self.authority = {"version": 1, "conversation_id": str(uuid4()), "generation": str(uuid4()),
            "request_id": str(uuid4()), "settings_scope": "e"*64}
        with patch.object(host, "load_execution_runtime", return_value=host._fixed_configuration({"engine": "mxc", "enabled": True})):
            self.snapshot = host.capture_execution_runtime()
        self.registry = set()
        self.patchers = [patch.object(subject, "_OWNERS", self.registry),
            patch.object(host, "capture_execution_runtime", side_effect=lambda: self.snapshot),
            patch.object(mxc.MxcPythonRunner, "_pins", return_value=()),
            patch.object(native, "run", side_effect=self.transport)]
        for patcher in self.patchers: patcher.start()

    async def asyncTearDown(self):
        self.release.set()
        for runtime in self.runtimes:
            runtime.invalidate()
            for entry in runtime._entries.values():
                work = entry.work
                if work is not None:
                    for _ in range(500):
                        if not work.thread.is_alive(): break
                        await asyncio.sleep(.005)
                    self.assertFalse(work.thread.is_alive(), "A controlled real thread remains live")
            try: await runtime.aclose()
            except subject.ProjectRuntimeError:
                self.assertTrue(runtime._has_unknown(), "Only deliberately sticky uncertainty is tolerated in teardown")
        for patcher in reversed(self.patchers): patcher.stop()
        self.owned.cleanup()

    def make(self):
        runtime = subject.ProjectRuntime(files=self.files, authority=self.authority,
            configuration=self.snapshot.configuration, configuration_identity=self.snapshot.identity,
            current_goal_revision=lambda: self.revision, authorization_current=lambda: self.authorized,
            parent_cancellation=self.cancel, call_identity=lambda: self.action, authority_guard=lambda: self.guard)
        self.runtimes.append(runtime)
        return runtime

    def args(self):
        return {"code": "pass", "inputs": [{"path": "source.txt", "expected_sha256": hashlib.sha256(b"2 3").hexdigest()}],
            "output": {"path": "result.txt", "target": "result.txt"}}

    def transport(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        kwargs["launch_attempt_callback"]()
        with kwargs["authority_guard"](): kwargs["before_resume"]()
        if argv[1:] == ["--version"]:
            return native.NativeResult("MXC_EXITED", 0, mxc.CODEX_VERSION.encode(), limits_verified=True)
        self.entered.set()
        if self.hold and not self.release.wait(3.): raise RuntimeError("Controlled transport was not released")
        if kwargs["cancel_event"].is_set():
            return native.NativeResult("MXC_CANCELLED", 0, b"{}", limits_verified=True)
        (kwargs["cwd"].parent/"task/result.txt").write_bytes(b"5")
        return native.NativeResult("MXC_EXITED", 0,
            b'{"written":"MODEL_STDOUT_IS_NOT_A_HOST_RECEIPT","sha256":"0000"}', limits_verified=True)

    async def until_entered(self):
        for _ in range(400):
            if self.entered.is_set(): return
            await asyncio.sleep(.005)
        self.fail("Controlled transport did not enter")

    async def test_repeated_stop_keeps_exact_real_thread_until_noncooperative_transport_exits(self):
        runtime = self.make(); self.hold = True; self.release.clear()
        task = asyncio.create_task(runtime.compute_project_artifact(**self.args()))
        await self.until_entered()
        entry, = runtime._entries.values()
        exact = entry.work.thread
        task.cancel(); await asyncio.sleep(.01); task.cancel(); await asyncio.sleep(.01)
        self.assertFalse(task.done())
        self.assertTrue(exact.is_alive())
        self.assertIn(runtime, self.registry)
        with self.assertRaisesRegex(subject.ProjectRuntimeError, "PROJECT_EXECUTION_ACTIVE"):
            runtime.assert_parent_ready()
        self.release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(exact.is_alive())
        self.assertTrue(entry.work.closed)
        self.assertFalse((self.workspace/"result.txt").exists())

    async def test_cancelled_aclose_cannot_return_or_drop_owner_before_actual_compute_thread_exit(self):
        runtime = self.make(); self.hold = True; self.release.clear()
        compute = asyncio.create_task(runtime.compute_project_artifact(**self.args()))
        await self.until_entered()
        closing = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(.01); closing.cancel(); await asyncio.sleep(.01); closing.cancel()
        self.assertFalse(closing.done())
        self.assertIn(runtime, self.registry)
        self.release.set()
        await asyncio.gather(compute, return_exceptions=True)
        with self.assertRaises(asyncio.CancelledError): await closing
        self.assertNotIn(runtime, self.registry)
        self.assertFalse((self.workspace/"result.txt").exists())

    async def test_thread_start_error_after_actual_start_retains_exact_owner_and_blocks_new_runtime(self):
        runtime = self.make(); self.hold = True; self.release.clear()
        start = threading.Thread.start
        def started_then_failed(thread):
            result = start(thread)
            if thread.name == "aura-owned-project": raise OSError("PRIVATE_START_ACK_ERROR")
            return result
        with patch.object(threading.Thread, "start", started_then_failed):
            with self.assertRaisesRegex(subject.ProjectRuntimeError, "PROJECT_EXECUTION_CLEANUP_UNCONFIRMED") as caught:
                await runtime.compute_project_artifact(**self.args())
        self.assertEqual("unknown", caught.exception.effect_state)
        self.assertFalse(caught.exception.safe_no_publication)
        entry, = runtime._entries.values()
        self.assertTrue(entry.work.unknown)
        self.assertIn(runtime, self.registry)
        with self.assertRaisesRegex(subject.ProjectRuntimeError, "PROJECT_EXECUTION_CLEANUP_UNCONFIRMED"):
            self.make()
        runtime.invalidate(); self.release.set()
        for _ in range(400):
            if not entry.work.thread.is_alive(): break
            await asyncio.sleep(.005)
        self.assertFalse(entry.work.thread.is_alive())
        self.assertFalse((self.workspace/"result.txt").exists())
        self.assertTrue(runtime._has_unknown(), "Thread existence/exit cannot erase the lost start acknowledgement")

    async def test_join_error_after_real_join_is_sticky_unknown_not_safe_fallback(self):
        runtime = self.make()
        join = threading.Thread.join
        def joined_then_failed(thread, *args, **kwargs):
            result = join(thread, *args, **kwargs)
            if thread.name == "aura-owned-project": raise OSError("PRIVATE_JOIN_ACK_ERROR")
            return result
        with patch.object(threading.Thread, "join", joined_then_failed):
            with self.assertRaisesRegex(subject.ProjectRuntimeError, "PROJECT_EXECUTION_CLEANUP_UNCONFIRMED") as caught:
                await runtime.compute_project_artifact(**self.args())
        self.assertEqual("unknown", caught.exception.effect_state)
        self.assertFalse(caught.exception.safe_no_publication)
        entry, = runtime._entries.values()
        self.assertFalse(entry.work.thread.is_alive())
        self.assertTrue(entry.work.unknown)
        with self.assertRaisesRegex(subject.ProjectRuntimeError, "PROJECT_EXECUTION_CLEANUP_UNCONFIRMED"):
            runtime.assert_parent_ready()

    async def test_source_replaced_while_native_transport_waits_never_publishes(self):
        runtime = self.make(); self.hold = True; self.release.clear()
        task = asyncio.create_task(runtime.compute_project_artifact(**self.args()))
        await self.until_entered()
        (self.workspace/"source.txt").write_bytes(b"100 200")
        self.release.set()
        with self.assertRaises(subject.ProjectRuntimeError): await task
        self.assertFalse((self.workspace/"result.txt").exists())
        self.assertFalse(runtime._has_unknown())

    async def test_target_cas_conflict_preserves_external_bytes_and_never_claims_publication(self):
        runtime = self.make(); self.hold = True; self.release.clear()
        task = asyncio.create_task(runtime.compute_project_artifact(**self.args()))
        await self.until_entered()
        (self.workspace/"result.txt").write_bytes(b"OWNED_EXTERNAL_WRITER")
        self.release.set()
        with self.assertRaises(subject.ProjectRuntimeError): await task
        self.assertEqual(b"OWNED_EXTERNAL_WRITER", (self.workspace/"result.txt").read_bytes())
        entry, = runtime._entries.values()
        self.assertIsNone(entry.result)
        self.assertFalse(runtime._has_unknown())

    async def test_initial_scope_conflict_is_known_no_publication_and_normal_file_helper_stays_usable(self):
        self.files.add_grant({"path": str(Path(subject.__file__).resolve()), "access": "read", "directory": False})
        runtime = self.make()
        with self.assertRaisesRegex(subject.ProjectRuntimeError, "PROJECT_PRIVATE_SCOPE_CONFLICT") as caught:
            await runtime.compute_project_artifact(**self.args())
        self.assertTrue(caught.exception.safe_no_publication)
        self.assertEqual("none", caught.exception.effect_state)
        self.assertEqual(0, len(self.calls))
        self.files.write_bytes("ordinary.txt", b"Independent normal file route")
        self.assertEqual(b"Independent normal file route", (self.workspace/"ordinary.txt").read_bytes())

    async def test_genuine_publication_is_projected_to_plain_text_after_host_evidence_validation(self):
        runtime = self.make()
        result = await runtime.compute_project_artifact(**self.args())
        self.assertIs(type(result), subject.ProjectArtifactResult)
        metadata = subject.published_artifact(result)
        self.assertEqual(hashlib.sha256(b"5").hexdigest(), metadata["sha256"])
        self.assertFalse(metadata["task_verified"])
        self.assertNotIn("MODEL_STDOUT", str(result))
        harness = TaskHarness(self.workspace, files=self.files)
        harness.record_success("compute_project_artifact", self.args(), result)
        self.assertIn("result.txt", harness.writes)
        # A legacy harness without a goal contract intentionally does not
        # collect observations. Exercise the actual evidence boundary directly.
        harness.goal_evidence.observe("compute_project_artifact", self.args(), result)
        observation, = harness.goal_evidence.snapshot()["observations"]
        self.assertIs(type(observation["observation"]["result"]), str)
        with self.assertRaises(TypeError): copy.deepcopy(result)
        for invalid in (str(result), json.loads(result)):
            self.assertIsNone(subject.published_artifact(invalid))
            with self.assertRaisesRegex(ValueError, "PROJECT_PUBLICATION_RECEIPT_REQUIRED"):
                GoalEvidence().observe("compute_project_artifact", self.args(), invalid)

    async def test_plain_or_foreign_str_cannot_borrow_issuer_to_mint_goal_write(self):
        runtime = self.make(); result = await runtime.compute_project_artifact(**self.args())
        class ForeignText(str): pass
        foreign = ForeignText(str(result)); foreign._issuer = result._issuer; foreign._encoded = result._encoded
        for invalid in (foreign, str(result), json.loads(result)):
            self.assertIsNone(subject.published_artifact(invalid))
            harness = TaskHarness(self.workspace, files=self.files)
            with self.assertRaisesRegex(HarnessError, "PROJECT_PUBLICATION_RECEIPT_REQUIRED"):
                harness.record_success("compute_project_artifact", self.args(), invalid)
            self.assertFalse(harness.writes)
        with self.assertRaisesRegex(TypeError, "PROJECT_PUBLICATION_RECEIPT_REQUIRED"):
            subject.ProjectArtifactResult(object(), json.loads(result))

    async def test_duplicate_same_call_returns_only_same_host_receipt_and_does_not_execute_again(self):
        runtime = self.make(); first = await runtime.compute_project_artifact(**self.args())
        calls = len(self.calls)
        second = await runtime.compute_project_artifact(**self.args())
        self.assertIs(first, second)
        self.assertEqual(calls, len(self.calls))
        with self.assertRaisesRegex(subject.ProjectRuntimeError, "PROJECT_CALL_IDENTITY_CONFLICT"):
            await runtime.compute_project_artifact(**{**self.args(), "code": "pass\n# distinct intent"})
        self.assertEqual(calls, len(self.calls))


if __name__ == "__main__": unittest.main()
