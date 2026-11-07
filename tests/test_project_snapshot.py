"""Owned-file/CAS proofs; fake native transports execute no supplied Python.

Real MXC fixed-payload tests live in a separate opt-in class. A fake native
closure is only a host unit-test control, never evidence for actual isolation.
"""
from dataclasses import replace
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_backend.nanobot import mxc_execution as mxc, mxc_native as native
from agent_backend.nanobot import project_snapshot as project
from agent_backend.nanobot.safe_files import WorkspaceFiles


def digest(raw): return hashlib.sha256(raw).hexdigest()


class ProjectSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-project-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"; self.workspace.mkdir()
        self.scratch = self.root / "scratch"; self.scratch.mkdir()
        self.current, self.goal_current = True, True
        self.lock = threading.RLock()
        self.files = WorkspaceFiles(self.workspace)
        self.scope = mxc.HostExecutionScope(str(uuid4()), str(uuid4()), str(uuid4()), self.files,
            grant_revision="a" * 64, is_current=lambda: self.current)
        self.calls, self.artifact = [], b'{"answer":46,"label":"fixed"}'
        self.fake_result = native.NativeResult("MXC_EXITED", 0, b'{}', limits_verified=True)
        self.runner = mxc.MxcPythonRunner(live_enabled=True, scratch_parent=self.scratch)
        self.runner._pins = lambda: ()
        self.runner._native_run = self.transport
        self.source("src/input.json", b'{"values":[17,29]}')

    def source(self, path, raw):
        target = self.workspace / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
        return project.ProjectInput(path, digest(raw))

    @contextmanager
    def guard(self):
        with self.lock: yield

    def plan(self, inputs=None, output=None, **kwargs):
        return project.ProjectSnapshot.prepare(self.scope,
            inputs or [project.ProjectInput("src/input.json", digest((self.workspace / "src/input.json").read_bytes()))],
            output or project.ProjectOutput("build/result.json", "result.json"),
            goal_revision="b" * 64, goal_is_current=lambda revision: self.goal_current and revision == "b" * 64,
            authority_guard=self.guard, **kwargs)

    def transport(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        if kwargs.get("launch_attempt_callback"): kwargs["launch_attempt_callback"]()
        with kwargs["authority_guard"]():
            self.assertTrue(self.lock._is_owned())
            kwargs["before_resume"]()
        self.assertFalse(self.lock._is_owned(), "Slow IO must stay outside the host authority guard")
        if argv[1:] == ["--version"]:
            return native.NativeResult("MXC_EXITED", 0, mxc.CODEX_VERSION.encode(), limits_verified=True)
        task = kwargs["cwd"].parent / "task"
        output = task / "build/result.json"; output.parent.mkdir(); output.write_bytes(self.artifact)
        return self.fake_result

    def execute(self, plan): return plan.execute(self.runner, "pass")

    def test_nested_snapshots_and_single_artifact_publish_after_scratch_cleanup(self):
        source = self.source("data/鲸🌊.txt", "固定输入🌊".encode())
        plan = self.plan([source])
        original_capture = plan._capture_output
        def capture(path, scope):
            self.assertFalse(self.lock._is_owned())
            staged = path.parent / "input/files/data/鲸🌊.txt"
            self.assertEqual("固定输入🌊".encode(), staged.read_bytes())
            profile = self.calls[-1][self.calls[-1].index("-P") + 1]
            self.assertTrue(profile.startswith("aura-mxc-"))
            self.assertNotIn(str(self.workspace), " ".join(self.calls[-1]))
            return original_capture(path, scope)
        plan._capture_output = capture
        result = self.execute(plan)
        self.assertEqual("MXC_OK", result.code)
        self.assertIs(type(result._capture_receipt), mxc.CaptureReceipt)
        self.assertTrue(result._capture_receipt.matches(self.scope, plan._captured))
        self.assertFalse(result._capture_receipt.matches(self.scope, object()))
        self.assertEqual([], list(self.scratch.iterdir()))
        self.assertFalse((self.workspace / "result.json").exists())
        published = plan.publish()
        self.assertEqual(self.artifact, (self.workspace / "result.json").read_bytes())
        self.assertEqual(digest(self.artifact), published["artifact"]["sha256"])
        self.assertFalse(published["task_verified"])
        self.assertEqual(2, len(self.calls))
        for value in (result._capture_receipt, plan):
            with self.assertRaises(TypeError): pickle.dumps(value)
        self.assertNotIn("capture", result.to_json())
        self.assertNotIn(str(self.workspace), json.dumps(plan.manifest, ensure_ascii=False))
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_CAPTURE_REQUIRED"): plan.publish()
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_ALREADY_EXECUTED"): self.execute(plan)

    def test_plain_result_dict_bool_exit_zero_and_wrong_capture_identity_cannot_publish(self):
        plan = self.plan()
        for forged in (True, {}, mxc.MxcExecutionResult("exited", "MXC_OK", limits_verified=True)):
            plan._receipt = forged
            with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_CAPTURE_REQUIRED"): plan.publish()
        with self.assertRaisesRegex(mxc.MxcExecutionError, "MXC_INVALID_CAPTURE_RECEIPT"):
            mxc.CaptureReceipt(object(), object(), self.scope, None, None, object())
        other = self.plan()
        self.execute(plan)
        other._captured, other._receipt, other._state = plan._captured, plan._receipt, "captured"
        # Exact capture identity plus scope is necessary, and the typed capture
        # itself must belong to this session before publication.
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_CLEANUP_UNCONFIRMED"):
            other.publish()

    def test_traversal_windows_devices_unicode_aliases_and_noncanonical_paths_rejected(self):
        for value in ("../secret", "/absolute", "C:/secret", "src\\input.json", "a//b", "a/./b", "a/../b",
                      "AUX.txt", "nul", "COM¹", "a:ads", "a.", "a ", "a\x00b", "e\u0301.txt", "a\udfff"):
            with self.subTest(kind=repr(value)), self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_INVALID_RELATIVE_PATH"):
                self.plan([project.ProjectInput(value, "a" * 64)])
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_INVALID_AUTHORITY"):
            project.ProjectSnapshot.prepare({}, [], project.ProjectOutput("x", "y"),
                goal_revision="b" * 64, goal_is_current=lambda _: True, authority_guard=self.guard)

    def test_duplicate_case_and_ancestor_input_aliases_rejected(self):
        row = project.ProjectInput("src/input.json", digest((self.workspace / "src/input.json").read_bytes()))
        for other in (row, replace(row, path="SRC/input.json"), replace(row, path="src/input.json/sub")):
            with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_INPUT_ALIAS_REJECTED"):
                self.plan([row, other])

    def test_hardlink_input_and_output_are_rejected(self):
        linked = self.workspace / "linked.json"; os.link(self.workspace / "src/input.json", linked)
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_FILE_ALIAS_REJECTED"):
            self.plan([project.ProjectInput("linked.json", digest(linked.read_bytes()))])
        linked.unlink()
        (self.workspace / "existing.txt").write_bytes(b"prior")
        os.link(self.workspace / "existing.txt", self.workspace / "also.txt")
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_FILE_ALIAS_REJECTED"):
            self.plan(output=project.ProjectOutput("build/result.json", "existing.txt", digest(b"prior")))

    def test_symlink_input_parent_and_output_parent_rejected_even_with_grant(self):
        outside = self.root / "outside"; outside.mkdir(); (outside / "known.txt").write_bytes(b"owned private")
        self.files.add_grant({"path":str(outside),"access":"write","directory":True})
        link = self.workspace / "alias"
        try: link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt": raise
            created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            self.assertEqual(0, created.returncode, "Owned junction construction must succeed for this negative")
        self.addCleanup(lambda: os.rmdir(link))
        for output in (False, True):
            with self.assertRaises(project.ProjectSnapshotError):
                if output: self.plan(output=project.ProjectOutput("build/result.json", "alias/new.txt"))
                else: self.plan([project.ProjectInput("alias/known.txt", digest(b"owned private"))])

    def test_source_hash_limits_and_target_expected_version_checked_before_launch(self):
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_INPUT_CHANGED"):
            self.plan([project.ProjectInput("src/input.json", "0" * 64)])
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_INPUT_UNAVAILABLE"):
            self.plan(limits=project.ProjectLimits(input_bytes=1))
        (self.workspace / "result.json").write_bytes(b"prior")
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_TARGET_EXISTS"): self.plan()
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_TARGET_CHANGED"):
            self.plan(output=project.ProjectOutput("build/result.json", "result.json", "0" * 64))
        self.assertEqual([], self.calls)

    def test_changed_source_after_staging_blocks_resume_and_publish(self):
        plan = self.plan(); original = self.runner._stage
        def changed(job, *args):
            original(job, *args)
            (self.workspace / "src/input.json").write_bytes(b"changed live input")
        self.runner._stage = changed
        result = self.execute(plan)
        self.assertNotEqual("MXC_OK", result.code)
        self.assertEqual([], self.calls)
        self.assertIsNone(result._capture_receipt)
        self.assertFalse((self.workspace / "result.json").exists())

    def test_changed_source_before_actual_resume_blocks_payload(self):
        plan = self.plan(); original = self.runner._native_run
        def changed(argv, **kwargs):
            if argv[1:] != ["--version"]:
                (self.workspace / "src/input.json").write_bytes(b"changed immediately before resume")
            return original(argv, **kwargs)
        self.runner._native_run = changed
        result = self.execute(plan)
        self.assertNotEqual("MXC_OK", result.code)
        self.assertIsNone(result._capture_receipt)
        self.assertIsNone(plan._captured)

    def test_changed_source_after_execution_prevents_publication(self):
        plan = self.plan(); self.execute(plan)
        (self.workspace / "src/input.json").write_bytes(b"host edited while reviewing")
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_INPUT_CHANGED"): plan.publish()
        self.assertFalse((self.workspace / "result.json").exists())
        self.assertEqual(2, len(self.calls))

    def test_goal_scope_and_stop_changes_after_execution_prevent_publication(self):
        for kind in ("goal", "scope", "stop"):
            with self.subTest(kind=kind):
                self.current = self.goal_current = True
                plan = self.plan(); cancel = threading.Event(); plan.execute(self.runner, "pass", cancel_event=cancel)
                if kind == "goal": self.goal_current = False
                elif kind == "scope": self.current = False
                else: cancel.set()
                with self.assertRaises(project.ProjectSnapshotError): plan.publish()
                self.assertFalse((self.workspace / "result.json").exists())

    def test_target_cas_preserves_a_concurrent_edit_and_successful_overwrite_is_atomic(self):
        target = self.workspace / "result.json"; target.write_bytes(b"approved prior")
        output = project.ProjectOutput("build/result.json", "result.json", digest(b"approved prior"))
        plan = self.plan(output=output); self.execute(plan)
        target.write_bytes(b"new independent edit")
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_TARGET_CHANGED"): plan.publish()
        self.assertEqual(b"new independent edit", target.read_bytes())
        plan = self.plan(output=replace(output, expected_sha256=digest(target.read_bytes())))
        self.execute(plan); plan.publish()
        self.assertEqual(self.artifact, target.read_bytes())
        self.assertEqual([], list(self.workspace.glob(".aura-write-*.tmp")))

    def test_last_moment_target_create_is_not_overwritten_by_publish(self):
        plan = self.plan(); self.execute(plan)
        write = self.files.write_bytes
        def raced(*args, **kwargs):
            (self.workspace / "result.json").write_bytes(b"independent create")
            return write(*args, **kwargs)
        with patch.object(self.files, "write_bytes", side_effect=raced), self.assertRaisesRegex(
                project.ProjectPublicationUnknown, "PROJECT_PUBLICATION_UNCONFIRMED"):
            plan.publish()
        self.assertEqual(b"independent create", (self.workspace / "result.json").read_bytes())

    def test_real_atomic_commit_then_lost_ack_is_typed_unknown_and_never_repeated(self):
        plan = self.plan(); self.execute(plan)
        write, calls = self.files.write_bytes, []
        def commit_then_fail(*args, **kwargs):
            calls.append(True); write(*args, **kwargs)
            raise OSError("OWNED_PRIVATE_ACK_ERROR")
        with patch.object(self.files, "write_bytes", side_effect=commit_then_fail):
            with self.assertRaises(project.ProjectPublicationUnknown) as raised:
                plan.publish()
            self.assertEqual("unknown", raised.exception.effect_state)
            self.assertTrue(raised.exception.publication_attempted)
            self.assertTrue(plan.publication_attempted)
            self.assertNotIn("OWNED_PRIVATE", str(raised.exception))
            with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_CAPTURE_REQUIRED"):
                plan.publish()
        self.assertEqual(1, len(calls))
        self.assertEqual(self.artifact, (self.workspace / "result.json").read_bytes())

    def test_freshness_rejection_before_write_is_known_not_attempted(self):
        plan = self.plan(); self.execute(plan); self.goal_current = False
        with patch.object(self.files, "write_bytes", wraps=self.files.write_bytes) as write:
            with self.assertRaises(project.ProjectSnapshotError) as raised: plan.publish()
        self.assertEqual("none", raised.exception.effect_state)
        self.assertFalse(raised.exception.publication_attempted)
        self.assertFalse(plan.publication_attempted)
        write.assert_not_called()

    def test_output_size_nonregular_alias_and_missing_output_never_mint_receipt(self):
        for kind in ("large", "hardlink", "missing"):
            with self.subTest(kind=kind):
                plan = self.plan(limits=project.ProjectLimits(output_bytes=8))
                original = self.runner._native_run
                def altered(argv, **kwargs):
                    result = self.transport(argv, **kwargs)
                    if argv[1:] != ["--version"]:
                        path = kwargs["cwd"].parent / "task/build/result.json"
                        if kind == "hardlink":
                            other = path.with_name("other"); other.write_bytes(b"small")
                            path.unlink(); os.link(other, path)
                        elif kind == "missing": path.unlink()
                    return result
                self.runner._native_run = altered
                result = self.execute(plan)
                self.assertEqual("MXC_OUTPUT_CAPTURE_FAILED", result.code)
                self.assertIsNone(result._capture_receipt)
                with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_CAPTURE_REQUIRED"): plan.publish()
                self.runner._native_run = original

    def test_capture_is_skipped_for_nonzero_invalid_stdout_unverified_limits_or_unclosed_tree(self):
        base = self.fake_result
        for result in (replace(base, exit_code=1), replace(base, stdout=b'{} trailing'),
                       replace(base, limits_verified=False), replace(base, tree_reaped=False),
                       replace(base, resources_closed=False)):
            with self.subTest(kind=(result.exit_code,result.stdout,result.limits_verified,result.tree_reaped,result.resources_closed)):
                plan = self.plan(); self.fake_result = result
                capture = plan._capture_output
                with patch.object(plan, "_capture_output", wraps=capture) as hook:
                    returned = self.execute(plan)
                hook.assert_not_called()
                self.assertIsNone(returned._capture_receipt)
                self.assertIsNone(plan._captured)
                # This fixture has no real native handles. Unknown fake rows
                # remain owned and are never reconciled into a success proof.
                self.runner = mxc.MxcPythonRunner(live_enabled=True, scratch_parent=self.scratch)
                self.runner._pins = lambda: (); self.runner._native_run = self.transport

    def test_pin_or_scratch_cleanup_failure_after_capture_discards_candidate(self):
        for kind in ("pin", "scratch"):
            with self.subTest(kind=kind):
                plan = self.plan()
                if kind == "pin":
                    context = patch.object(self.runner, "_close_pins", return_value=object())
                else:
                    context = patch.object(mxc.shutil, "rmtree", side_effect=OSError("OWNED_PRIVATE_ERROR"))
                with context: result = self.execute(plan)
                self.assertEqual("cleanup_unconfirmed", result.state)
                self.assertIsNone(result._capture_receipt)
                self.assertIsNone(plan._captured)
                self.assertNotIn("OWNED_PRIVATE_ERROR", result.to_json())
                self.runner = mxc.MxcPythonRunner(live_enabled=True, scratch_parent=self.scratch)
                self.runner._pins = lambda: (); self.runner._native_run = self.transport

    def test_stop_after_capture_discards_candidate_and_never_replays(self):
        plan = self.plan(); cancel = threading.Event(); capture = plan._capture_output
        def stopped(*args):
            result = capture(*args); cancel.set(); return result
        plan._capture_output = stopped
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_CANCELLED"):
            plan.execute(self.runner, "pass", cancel_event=cancel)
        self.assertIsNone(plan._captured)
        self.assertIsNone(plan._receipt)
        self.assertEqual(2, len(self.calls))
        self.assertFalse((self.workspace / "result.json").exists())

    def test_global_unknown_appearing_after_capture_never_mints_or_publishes(self):
        plan = self.plan(); pending = [False]
        def close_pins(_): pending[0] = True
        with patch.object(native, "has_pending_cleanup", side_effect=lambda: pending[0]), \
                patch.object(self.runner, "_close_pins", side_effect=close_pins):
            result = self.execute(plan)
        self.assertEqual("cleanup_unconfirmed", result.state)
        self.assertIsNone(result._capture_receipt)
        self.assertIsNone(plan._captured)
        self.assertFalse((self.workspace / "result.json").exists())

    def test_captured_file_close_error_keeps_exact_owner_sticky_in_bounded_child(self):
        # This process must not reset the global cleanup registry. The bounded
        # child proves retention and then exits normally; parent never treats
        # that process exit as a successful cleanup of its failed-close lease.
        script = r'''
import json,sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
sys.path.insert(0,sys.argv[1])
from agent_backend.nanobot import project_snapshot as project,mxc_native as native,mxc_execution as mxc
from agent_backend.nanobot.safe_files import WorkspaceFiles
root=Path(sys.argv[2]);path=root/'capture.bin';path.write_bytes(b'fixed')
original=Path.open;wrappers=[]
class Wrapped:
    calls=0
    def __init__(self,stream):self.stream=stream
    def __getattr__(self,name):return getattr(self.stream,name)
    def close(self):
        self.calls+=1;self.stream.close();raise OSError('OWNED_PRIVATE_CLOSE_ERROR')
def opened(self,*args,**kwargs):
    stream=original(self,*args,**kwargs)
    if self==path:
        wrapper=Wrapped(stream);wrappers.append(wrapper);return wrapper
    return stream
with patch.object(Path,'open',opened):
    try:project.ProjectSnapshot._read_staged(path,32)
    except project.ProjectSnapshotError as error:assert str(error)=='PROJECT_CLEANUP_UNCONFIRMED'
    else:raise AssertionError('close failure accepted')
assert native.has_pending_cleanup() and len(wrappers)==1 and wrappers[0].closed
leases=list(native._pending_cleanup.values())
assert any(lease._process._owners[0] is wrappers[0] for lease in leases)
assert all(native.reconcile_cleanup(lease) is False for lease in leases)
assert wrappers[0].calls==1
workspace=root/'workspace';workspace.mkdir()
scope=mxc.HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),
    grant_revision='a'*64,is_current=lambda:True)
result=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=root).execute('pass',scope=scope)
assert result.code=='MXC_CLEANUP_PENDING' and result._capture_receipt is None
print(json.dumps({'exact_owner_retained':True,'closed_property_not_proof':True,'sticky':True,'next_execution_blocked':True}))
'''
        owned = self.root / "close-child"; owned.mkdir()
        result = subprocess.run([sys.executable, "-I", "-S", "-c", script,
            str(Path(__file__).resolve().parents[1]), str(owned)], capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace")[-1000:])
        self.assertEqual({"exact_owner_retained":True,"closed_property_not_proof":True,
            "sticky":True,"next_execution_blocked":True}, json.loads(result.stdout))
        self.assertFalse(native.has_pending_cleanup(), "Child-owned sticky state must not contaminate host fixture")

    def test_default_disabled_never_stages_or_launches_and_can_not_publish(self):
        plan = self.plan()
        disabled = mxc.MxcPythonRunner(scratch_parent=self.scratch)
        result = plan.execute(disabled, "pass")
        self.assertEqual("MXC_DISABLED", result.code)
        self.assertIsNone(result._capture_receipt)
        self.assertEqual([], list(self.scratch.iterdir()))
        with self.assertRaisesRegex(project.ProjectSnapshotError, "PROJECT_CAPTURE_REQUIRED"): plan.publish()


if __name__ == "__main__": unittest.main()
