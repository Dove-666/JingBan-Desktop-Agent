"""Independent offline MXC review. No native launch, model or user code runs.

Win32 calls are complete in-memory fakes. Fixture executable files contain
plain synthetic bytes, and are opened only to exercise host pin bookkeeping.
"""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_backend.nanobot import mxc_execution as mxc, mxc_native as native
from agent_backend.nanobot.safe_files import WorkspaceFiles


class Function:
    def __init__(self, callback): self.callback = callback
    def __call__(self, *args): return self.callback(*args)


class FakeKernel:
    """No Win32 function is called. The fake only manipulates ctypes values."""
    def __init__(self):
        self.events, self.info, self.handle_info = [], {}, []
        self.next_handle = 1000
        self.assign = self.contained = True
        self.resume_result = 1
        self.mutate_query = None
        callbacks = {
            "CreateJobObjectW": self.job,
            "SetInformationJobObject": self.set_info,
            "QueryInformationJobObject": self.query,
            "CreatePipe": self.pipe,
            "SetHandleInformation": self.set_handle,
            "CreateFileW": lambda *args: self.handle("stdin"),
            "InitializeProcThreadAttributeList": self.attributes,
            "UpdateProcThreadAttribute": self.allow_handles,
            "DeleteProcThreadAttributeList": lambda *_: self.events.append("delete-attributes"),
            "CreateProcessW": self.create,
            "AssignProcessToJobObject": self.assign_job,
            "IsProcessInJob": self.is_contained,
            "ResumeThread": self.resume,
            "CloseHandle": lambda handle: self.events.append(("close", handle)) or 1,
        }
        for name in ("TerminateJobObject", "WaitForSingleObject", "GetExitCodeProcess", "TerminateProcess", "PeekNamedPipe", "ReadFile"):
            callbacks[name] = lambda *args: 1
        for name, callback in callbacks.items(): setattr(self, name, Function(callback))

    def handle(self, kind):
        self.next_handle += 1
        self.events.append((kind, self.next_handle))
        return self.next_handle

    def job(self, attrs, name):
        assert attrs is None and name is None
        return self.handle("job")

    def set_info(self, job, kind, pointer, size):
        self.events.append(("set", kind))
        self.info[kind] = C.string_at(pointer, size)
        return 1

    def query(self, job, kind, pointer, size, returned):
        self.events.append(("query", kind))
        C.memmove(pointer, self.info[kind], size)
        if self.mutate_query: self.mutate_query(kind, pointer)
        return 1

    def pipe(self, read, write, attrs, size):
        assert attrs._obj.inherit
        read._obj.value, write._obj.value = self.handle("read"), self.handle("write")
        return 1

    def set_handle(self, handle, mask, flags):
        self.handle_info.append((handle.value, mask, flags))
        return 1

    @staticmethod
    def attributes(buffer, count, flags, size):
        if buffer is None: size._obj.value = 128; return 0
        return 1

    def allow_handles(self, buffer, flags, attribute, handles, size, previous, returned):
        self.allowed = tuple(handles)
        self.attribute = attribute
        return 1

    def create(self, executable, command, proc_attrs, thread_attrs, inherit, flags, environment, cwd, startup, info):
        self.events.append("create-suspended")
        self.created = SimpleNamespace(executable=executable, command=command.value, inherit=inherit,
                                       flags=flags, startup=startup._obj, cwd=cwd)
        info._obj.process, info._obj.thread = self.handle("process"), self.handle("thread")
        return 1

    def assign_job(self, job, process):
        self.events.append("assign")
        return self.assign

    def is_contained(self, process, job, contained):
        self.events.append("confirm-membership")
        contained._obj.value = self.contained
        return 1

    def resume(self, thread):
        self.events.append("resume")
        return self.resume_result


class MxcNativeReviewTests(unittest.TestCase):
    def process(self, kernel=None):
        kernel = kernel or FakeKernel()
        with patch.object(native.C, "WinDLL", return_value=kernel):
            process = native.WindowsJobProcess()
        return process, kernel

    @unittest.skipUnless(C.sizeof(C.c_void_p) == 8, "pinned runtime is Windows x64")
    def test_x64_abi_sizes_offsets_and_non_breakaway_job_flags(self):
        expected = {native.BasicLimits: 64, native.ExtendedLimits: 144, native.CpuRate: 8,
                    native.Accounting: 48, native.SecurityAttributes: 24, native.StartupInfo: 104,
                    native.StartupInfoEx: 112, native.ProcessInfo: 24}
        for structure, size in expected.items(): self.assertEqual(size, C.sizeof(structure), structure.__name__)
        self.assertEqual(16, native.BasicLimits.flags.offset)
        self.assertEqual(48, native.BasicLimits.affinity.offset)
        self.assertEqual(80, native.StartupInfo.stdin.offset)
        self.assertEqual(104, native.StartupInfoEx.attributes.offset)
        self.assertEqual(0x230C, native.WindowsJobProcess.FLAGS)
        self.assertFalse(native.WindowsJobProcess.FLAGS & (0x800 | 0x1000))

    def test_limits_membership_and_exact_inherited_handles_are_confirmed_before_resume(self):
        process, kernel = self.process()
        process.configure(mxc.MxcPolicy())
        pipes = process.launch([r"C:\fixed\codex.exe", "--fixed", 'quoted " Unicode 鲸'],
                               {"PATH": r"C:\fixed", "SystemRoot": r"C:\Windows"}, r"C:\owned")
        self.assertTrue(process.verified and process.resumed)
        events = kernel.events
        self.assertLess(events.index(("query", 6)), events.index("create-suspended"))
        self.assertLess(events.index("create-suspended"), events.index("assign"))
        self.assertLess(events.index("assign"), events.index("confirm-membership"))
        self.assertLess(events.index("confirm-membership"), events.index("resume"))
        self.assertEqual(0x00020002, kernel.attribute)
        startup = kernel.created.startup.base
        self.assertEqual((startup.stdin, startup.stdout, startup.stderr), kernel.allowed)
        self.assertEqual(3, len(set(kernel.allowed)))
        self.assertFalse(set(pipes) & set(kernel.allowed))
        self.assertEqual({(value, 1, 0) for value in pipes}, set(kernel.handle_info))
        self.assertTrue(kernel.created.flags & 0x4)
        self.assertTrue(kernel.created.flags & 0x80000)
        self.assertTrue(kernel.created.flags & 0x400)
        self.assertEqual(C.sizeof(native.StartupInfoEx), startup.size)

    def test_unconfirmed_limits_or_membership_never_resume(self):
        kernel = FakeKernel()
        kernel.mutate_query = lambda kind, pointer: setattr(pointer._obj.basic, "flags", pointer._obj.basic.flags | 0x800) if kind == 9 else None
        process, kernel = self.process(kernel)
        with self.assertRaisesRegex(native.NativeFailure, "MXC_JOB_LIMIT_MISMATCH"):
            process.configure(mxc.MxcPolicy())
        self.assertFalse(process.verified)
        self.assertNotIn("resume", kernel.events)
        for field in ("assign", "contained"):
            process, kernel = self.process()
            process.configure(mxc.MxcPolicy())
            setattr(kernel, field, False)
            with self.assertRaises(native.NativeFailure):
                process.launch([r"C:\fixed\codex.exe"], {}, r"C:\owned")
            self.assertFalse(process.resumed)
            self.assertNotIn("resume", kernel.events)


class MxcPinsReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-mxc-pin-review-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex.exe"; self.codex.write_bytes(b"Synthetic pin fixture, never executed")
        self.python_root = self.root / "python"; self.python_root.mkdir()
        self.python = self.python_root / "python.exe"; self.python.write_bytes(b"Synthetic Python pin fixture, never executed")
        self.system = self.root / "system32"; self.system.mkdir()
        self.opened, self.options = [], []

    def kernel(self):
        def open_fixture(path, access, share, attributes, disposition, flags, template):
            self.options.append((access, share, disposition, flags))
            fd = os.open(path, os.O_RDONLY | os.O_BINARY)
            self.opened.append(fd)
            return fd
        return SimpleNamespace(CreateFileW=Function(open_fixture), CloseHandle=Function(os.close))

    def pins(self, *, bad_hash=False, fdopen=None):
        import msvcrt
        patches = [patch.object(mxc.C if hasattr(mxc, "C") else C, "WinDLL", return_value=self.kernel()),
                   patch.object(mxc, "CODEX", self.codex), patch.object(mxc, "PYTHON_ROOT", self.python_root),
                   patch.object(mxc, "SYSTEM32", self.system),
                   patch.object(mxc, "CODEX_SHA256", hashlib.sha256(self.codex.read_bytes()).hexdigest()),
                   patch.object(mxc, "PYTHON_SHA256", "0" * 64 if bad_hash else hashlib.sha256(self.python.read_bytes()).hexdigest()),
                   patch.object(msvcrt, "open_osfhandle", side_effect=lambda handle, flags: handle)]
        if fdopen is not None: patches.append(patch.object(mxc.os, "fdopen", side_effect=fdopen))
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in patches: stack.enter_context(item)
            return mxc.MxcPythonRunner._pins()

    def assert_closed(self):
        for fd in self.opened:
            with self.assertRaises(OSError): os.fstat(fd)

    def tearDown(self):
        for fd in self.opened:
            try: os.close(fd)
            except OSError: pass

    def test_checked_images_remain_open_read_only_without_write_or_delete_sharing(self):
        pins = self.pins()
        try:
            self.assertEqual(2, len(pins)); self.assertTrue(all(not stream.closed for stream in pins))
            self.assertEqual([(0x80000000, 1, 3, 0x00200000)] * 2, self.options)
        finally:
            for stream in pins: stream.close()
        self.assert_closed()

    def test_pin_hash_mismatch_closes_all_already_opened_runtime_descriptors(self):
        with self.assertRaisesRegex(mxc.MxcExecutionError, "MXC_RUNTIME_PIN_MISMATCH"):
            self.pins(bad_hash=True)
        self.assert_closed()

    def test_fdopen_failure_does_not_leak_the_newly_adopted_pin_descriptor(self):
        with self.assertRaises(OSError): self.pins(fdopen=OSError("Synthetic wrapper failure"))
        self.assert_closed()


class MxcProtocolReviewTests(unittest.TestCase):
    def test_strict_json_rejects_nested_duplicates_nonfinite_and_unicode_errors(self):
        for raw in (b'{"a":{"same":1,"same":2}}', b'{"a":1e999}', b'{"a":NaN}', b'"\\udfff"', b'\xff', b'{} extra'):
            with self.subTest(size=len(raw)), self.assertRaises(Exception): mxc._strict_json(raw)
        self.assertEqual({"鲸": [None, True, 42]}, mxc._strict_json('{"鲸":[null,true,42]}'.encode()))

    def test_secret_redaction_collision_fails_instead_of_overwriting_object_key(self):
        runner = mxc.MxcPythonRunner(known_secrets=("SYNTHETIC_A", "SYNTHETIC_B"))
        with self.assertRaisesRegex(mxc.MxcExecutionError, "MXC_OUTPUT_REDACTION_CONFLICT"):
            runner._clean_value({"SYNTHETIC_A": 1, "SYNTHETIC_B": 2})
        value = runner._clean_value({"safe": ["SYNTHETIC_A", {"nested": "SYNTHETIC_B"}]})
        self.assertNotIn("SYNTHETIC_", json.dumps(value))


class MxcInputOwnershipReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-mxc-owned-review-")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.workspace = self.parent / "workspace"; self.workspace.mkdir()
        self.scope = mxc.HostExecutionScope(str(uuid4()), str(uuid4()), str(uuid4()), WorkspaceFiles(self.workspace),
            grant_revision="f" * 64, is_current=lambda: True)
        self.runner = mxc.MxcPythonRunner(live_enabled=True, scratch_parent=self.parent)
        self.job = self.runner._job(self.scope, self.runner._scope(self.scope))

    def test_only_exact_approved_copy_is_staged_and_changed_hash_or_hardlink_fails(self):
        source = self.workspace / "approved.txt"; source.write_bytes(b"synthetic approved bytes")
        unapproved = self.workspace / "not-approved.txt"; unapproved.write_bytes(b"synthetic unapproved bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.runner._stage(self.job, mxc._payload("pass", None), [mxc.AuthorizedInput("approved.txt", digest, "copy.txt")])
        inputs = self.job.root / "input" / "files"
        self.assertEqual(["copy.txt"], [path.name for path in inputs.iterdir()])
        self.assertEqual(source.read_bytes(), (inputs / "copy.txt").read_bytes())
        self.assertNotIn(str(self.workspace), (self.job.root / "client" / "config.toml").read_text())
        source.write_bytes(b"changed after approval")
        with self.assertRaisesRegex(mxc.MxcExecutionError, "MXC_INPUT_CHANGED"):
            self.runner._stage(self.job, b"{}", [mxc.AuthorizedInput("approved.txt", digest, "new.txt")])
        source.write_bytes(b"synthetic approved bytes")
        linked = self.workspace / "linked.txt"; os.link(source, linked)
        with self.assertRaisesRegex(mxc.MxcExecutionError, "MXC_INPUT_LINK_REJECTED"):
            self.runner._stage(self.job, b"{}", [mxc.AuthorizedInput("linked.txt", digest, "linked.txt")])

    def test_owner_nonce_and_root_identity_are_both_required(self):
        self.assertEqual(self.job.root, self.runner._owned(self.job))
        marker = self.job.root / "owner.json"
        marker.write_text(json.dumps({"nonce": "0" * 64, "scope": self.scope.identity}))
        with self.assertRaisesRegex(mxc.MxcExecutionError, "MXC_SCRATCH_CHANGED"):
            self.runner._owned(self.job)
        marker.write_text(json.dumps({"nonce": self.job.nonce, "scope": self.scope.identity}))
        self.job.root.rename(self.parent / "old-owned-root")
        self.job.root.mkdir()
        marker.write_text(json.dumps({"nonce": self.job.nonce, "scope": self.scope.identity}))
        with self.assertRaisesRegex(mxc.MxcExecutionError, "MXC_SCRATCH_CHANGED"):
            self.runner._owned(self.job)

    def test_successful_fake_native_result_never_exposes_stderr_or_known_secret(self):
        self.runner._pins = lambda: ()
        self.runner._secrets = ("SYNTHETIC_PRIVATE_KEY",)
        self.runner._native_run = lambda argv, **kw: native.NativeResult("MXC_EXITED", 0,
            mxc.CODEX_VERSION.encode() if argv[1:] == ["--version"] else b'{"reply":"SYNTHETIC_PRIVATE_KEY"}',
            b"RAW_PRIVATE_TRACEBACK_AND_AUTH_HEADER", limits_verified=True)
        with patch.object(native, "has_pending_cleanup", return_value=False):
            result = self.runner.execute("pass", scope=self.scope)
        self.assertEqual("MXC_OK", result.code)
        self.assertEqual({"reply": "[redacted]"}, result.value)
        self.assertFalse(result.task_verified)
        self.assertNotIn("PRIVATE", result.to_json())


if __name__ == "__main__":
    unittest.main()
