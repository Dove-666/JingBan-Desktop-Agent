"""Contract tests plus opt-in real Windows fixed-payload integration.

Fake transport cases assert host authorization/protocol only. They do not
certify the OS boundary; AURA_RUN_MXC_INTEGRATION=1 runs trusted fixed code.
"""
from __future__ import annotations

from dataclasses import replace
import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_backend.nanobot import mxc_execution as mxc, mxc_native as native
from agent_backend.nanobot.safe_files import WorkspaceFiles


class MxcContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-mxc-contract-")
        self.parent = Path(self.temp.name)
        self.workspace = self.parent / "workspace"
        self.workspace.mkdir()
        self.current = True
        self.scope = mxc.HostExecutionScope(str(uuid4()), str(uuid4()), str(uuid4()), WorkspaceFiles(self.workspace), grant_revision="f" * 64, is_current=lambda: self.current)
        self.runner = mxc.MxcPythonRunner(live_enabled=True, scratch_parent=self.parent)
        self.runner._pins = lambda: None
        self.calls = []
        def transport(argv, **options):
            self.calls.append((argv, options))
            if argv[1:] == ["--version"]:
                return native.NativeResult("MXC_EXITED", 0, mxc.CODEX_VERSION.encode(), limits_verified=True, resumed=True)
            return native.NativeResult("MXC_EXITED", 0, b'{"answer":42}', limits_verified=True, resumed=True)
        self.runner._native_run = transport

    def tearDown(self):
        self.temp.cleanup()

    def test_import_and_disabled_construction_never_touch_native_or_files(self):
        with patch("subprocess.Popen", side_effect=AssertionError("No launch")), patch("tempfile.mkdtemp", side_effect=AssertionError("No scratch")), patch("pathlib.Path.open", side_effect=AssertionError("No IO")):
            importlib.reload(mxc)
            result = mxc.MxcPythonRunner().execute("malformed", scope=None)
            self.assertEqual(result.code, "MXC_DISABLED")
        self.assertFalse(mxc.MxcPythonRunner().capability()["available"])

    def test_explicit_residual_capability_is_not_an_isolation_certificate(self):
        info = self.runner.capability()
        self.assertFalse(info["runtime_verified"])
        self.assertTrue(info["residual_boundaries"]["registry_read"])
        self.assertFalse(info["residual_boundaries"]["hard_disk_quota"])
        self.assertEqual(info["residual_boundaries"]["scratch_quota"], "soft_watch_and_stop")

    def test_scope_rejects_model_dict_and_revocation(self):
        for scope in ({"files": self.scope.files}, None):
            with self.assertRaisesRegex(mxc.MxcExecutionError, "MXC_SCOPE_MISMATCH"):
                self.runner.execute("pass", scope=scope)
        self.current = False
        with self.assertRaisesRegex(mxc.MxcExecutionError, "MXC_SCOPE_REVOKED"):
            self.runner.execute("pass", scope=self.scope)
        self.assertFalse(self.calls)

    def test_workspace_identity_change_during_execution_rejects_result(self):
        original = self.runner._native_run
        replaced = False
        def change(argv, **options):
            nonlocal replaced
            if not replaced:
                self.workspace.rename(self.parent / "old-workspace")
                self.workspace.mkdir()
                replaced = True
            return original(argv, **options)
        self.runner._native_run = change
        result = self.runner.execute("pass", scope=self.scope)
        self.assertEqual(result.code, "MXC_SCOPE_CHANGED")
        self.assertIsNone(result.value)
        self.assertTrue(result.scratch_removed)

    def test_policy_fails_closed_for_nonfinite_boolean_and_resource_values(self):
        for changes in ({"cpu_seconds": float("nan")}, {"timeout_seconds": True}, {"job_memory_mb": 1}, {"process_memory_mb": 1024, "job_memory_mb": 256}, {"process_limit": 0}, {"stdout_limit": -1}):
            with self.assertRaisesRegex(mxc.MxcExecutionError, "MXC_INVALID_POLICY"):
                replace(mxc.MxcPolicy(), **changes).validate()

    def test_configuration_environment_and_fixed_cli_are_narrow_and_no_credentials(self):
        captured = {}
        original = self.runner._native_run
        def inspect(argv, **options):
            config = Path(options["environment"]["CODEX_HOME"]) / "config.toml"
            captured["config"] = config.read_text()
            captured["environment"] = options["environment"]
            if "sandbox" in argv:
                captured["profile"] = next(token for token in argv if token.startswith("permissions."))
                request = Path(argv[-1]).with_name("request.json")
                captured["request"] = json.loads(request.read_bytes())
            return original(argv, **options)
        self.runner._native_run = inspect
        secret = "QA_PRIVATE_PROVIDER_TOKEN"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret, "HTTP_PROXY": secret, "CODEX_HOME": str(self.workspace)}):
            result = self.runner.execute("print(input)", {"quote": "'\"\\中文🐋"}, scope=self.scope)
        self.assertEqual(result.code, "MXC_OK")
        self.assertEqual(captured["request"]["input"]["quote"], "'\"\\中文🐋")
        self.assertNotIn(secret, str(captured))
        config = captured["config"]
        self.assertIn('\":root\"=\"deny\"', captured["profile"])
        self.assertNotIn(":minimal", config + captured["profile"])
        self.assertIn('sandbox = "mxc"', config)
        self.assertIn("enabled=false", captured["profile"])
        self.assertNotIn("permissions.", config)
        self.assertNotIn(str(self.workspace), config)
        argv = self.calls[-1][0]
        self.assertEqual(argv[0], str(mxc.CODEX))
        self.assertIn("--include-managed-config", argv)
        self.assertLess(argv.index("sandbox"), argv.index("--include-managed-config"))
        self.assertLess(argv.index("--include-managed-config"), argv.index("--"))
        self.assertNotIn("-C", argv)
        self.assertIn('project_root_markers=[".aura-mxc-root"]', argv)
        self.assertEqual(argv[-4:-1], ["-I", "-S", "-B"])
        self.assertEqual(list(self.parent.iterdir()), [self.workspace])

    def test_approved_input_is_copied_by_hash_and_original_is_not_exposed(self):
        source = self.workspace / "data.txt"
        source.write_bytes(b"QA-approved-data")
        item = mxc.AuthorizedInput("data.txt", hashlib.sha256(source.read_bytes()).hexdigest(), "data.txt")
        captured = []
        original = self.runner._native_run
        def inspect(argv, **options):
            if "sandbox" in argv:
                input_root = Path(argv[-1]).parent
                captured.append((input_root / "files" / "data.txt").read_bytes())
                self.assertNotIn(str(source), (input_root.parent / "client" / "config.toml").read_text())
            return original(argv, **options)
        self.runner._native_run = inspect
        self.assertEqual(self.runner.execute("pass", scope=self.scope, authorized_inputs=(item,)).code, "MXC_OK")
        self.assertEqual(captured, [b"QA-approved-data"])
        source.write_bytes(b"different")
        self.calls.clear()
        self.assertEqual(self.runner.execute("pass", scope=self.scope, authorized_inputs=(item,)).code, "MXC_INPUT_CHANGED")
        self.assertFalse(self.calls)

    def test_input_aliases_hardlinks_and_unauthorized_paths_never_launch(self):
        source = self.workspace / "data.txt"
        source.write_bytes(b"data")
        digest = hashlib.sha256(b"data").hexdigest()
        outside = self.parent / "outside"
        outside.write_bytes(b"data")
        self.assertEqual(self.runner.execute("pass", scope=self.scope, authorized_inputs=[mxc.AuthorizedInput(str(outside), digest, "outside")]).code, "MXC_INPUT_NOT_AUTHORIZED")
        for item in (mxc.AuthorizedInput("data.txt", digest, "../evil"), mxc.AuthorizedInput("data.txt", digest, "CON"), mxc.AuthorizedInput(str(self.parent / "outside"), digest, "a")):
            self.assertNotEqual(self.runner.execute("pass", scope=self.scope, authorized_inputs=[item]).code, "MXC_OK")
        linked = self.workspace / "linked.txt"
        os.link(source, linked)
        self.assertEqual(self.runner.execute("pass", scope=self.scope, authorized_inputs=[mxc.AuthorizedInput("linked.txt", digest, "b")]).code, "MXC_INPUT_LINK_REJECTED")
        self.assertFalse(self.calls)

    def test_json_output_is_strict_and_errors_cannot_disclose_exception_or_source(self):
        for output in (b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}', b'{} trailing', b'{"x":"\\ud800"}', b'\xff', b''):
            self.runner._native_run = lambda argv, **kw: native.NativeResult("MXC_EXITED", 0, mxc.CODEX_VERSION.encode() if argv[1:] == ["--version"] else output, limits_verified=True)
            result = self.runner.execute("raise Exception('QA_PRIVATE_CODE_SECRET')", scope=self.scope)
            self.assertEqual(result.code, "MXC_OUTPUT_NOT_STRICT_JSON")
            self.assertNotIn("QA_PRIVATE", result.to_json())

    def test_known_secrets_in_output_keys_and_leaves_are_redacted(self):
        self.runner._secrets = ("QA_SECRET",)
        self.runner._native_run = lambda argv, **kw: native.NativeResult("MXC_EXITED", 0, mxc.CODEX_VERSION.encode() if argv[1:] == ["--version"] else b'{"QA_SECRET":["QA_SECRET"]}')
        result = self.runner.execute("pass", scope=self.scope)
        self.assertEqual(result.value, {"[redacted]": ["[redacted]"]})
        self.assertNotIn("QA_SECRET", result.to_json())

    def test_nonzero_payload_and_stderr_are_fixed_failures_no_values(self):
        original = self.runner._native_run
        self.runner._native_run = lambda argv, **kw: original(argv, **kw) if argv[1:] == ["--version"] else native.NativeResult("MXC_EXITED", 1, b'"QA_SECRET"', b"QA_PRIVATE_TRACEBACK")
        result = self.runner.execute("pass", scope=self.scope)
        self.assertEqual(result.code, "MXC_PAYLOAD_FAILED")
        self.assertIsNone(result.value)
        self.assertNotIn("QA_", result.to_json())

    def test_managed_policy_rejection_never_retries_without_policy_or_sandbox(self):
        original = self.runner._native_run
        attempts = []
        def rejected(argv, **options):
            if argv[1:] == ["--version"]:
                return original(argv, **options)
            attempts.append(list(argv))
            return native.NativeResult("MXC_EXITED", 1, b"", b"QA_PRIVATE_MANAGED_POLICY_ERROR",
                                       tree_reaped=True, limits_verified=True)
        self.runner._native_run = rejected
        result = self.runner.execute("print(42)", scope=self.scope)
        self.assertEqual(result.code, "MXC_PAYLOAD_FAILED")
        self.assertIsNone(result.value)
        self.assertEqual(len(attempts), 1)
        self.assertIn("--include-managed-config", attempts[0])
        self.assertIn('windows.sandbox="mxc"', attempts[0])
        self.assertNotIn("QA_PRIVATE", result.to_json())
        self.assertEqual(list(self.parent.iterdir()), [self.workspace])

    def test_runtime_pin_and_version_mismatch_fail_without_payload(self):
        self.runner._pins = lambda: mxc._error("MXC_RUNTIME_PIN_MISMATCH")
        self.assertEqual(self.runner.execute("pass", scope=self.scope).code, "MXC_RUNTIME_PIN_MISMATCH")
        self.assertFalse(self.calls)
        self.runner._pins = lambda: None
        self.runner._native_run = lambda *a, **k: native.NativeResult("MXC_EXITED", 0, b"wrong version")
        self.assertEqual(self.runner.execute("pass", scope=self.scope).code, "MXC_VERSION_MISMATCH")

    def test_worker_style_isolated_import_keeps_scope_type_identity(self):
        import subprocess, sys
        backend = Path(mxc.__file__).parent
        code = "import sys;sys.path.insert(0,sys.argv[1]);import mxc_execution,container_execution;assert mxc_execution.HostExecutionScope is container_execution.HostExecutionScope;assert not mxc_execution.MxcPythonRunner().capability()['available'];print('MXC_IMPORT_OK')"
        result = subprocess.run([sys.executable, "-I", "-c", code, str(backend)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"MXC_IMPORT_OK")

    def test_scratch_scan_does_not_follow_directory_swapped_to_junction(self):
        import _winapi
        from types import SimpleNamespace
        scratch = self.parent / "guarded"; scratch.mkdir()
        child = scratch / "child"; child.mkdir()
        outside = self.parent / "private-scan-target"; outside.mkdir()
        (outside / "private.txt").write_text("QA_METADATA_CANARY")
        info = scratch.stat()
        original = os.scandir
        traversed = []
        class Entries:
            def __init__(self, path): self.source = original(path)
            def __enter__(self): return self
            def __exit__(self, *args): return self.source.__exit__(*args)
            def __iter__(self):
                for entry in self.source:
                    if Path(entry.path) != child:
                        yield entry; continue
                    def swapped(*, follow_symlinks, entry=entry):
                        before = entry.stat(follow_symlinks=follow_symlinks)
                        child.rmdir()
                        _winapi.CreateJunction(str(outside), str(child))
                        return before
                    yield SimpleNamespace(path=entry.path, stat=swapped)
        def scan(path):
            traversed.append(Path(path))
            if Path(path) != scratch:
                raise AssertionError("Host must not enumerate the swapped target")
            return Entries(path)
        with patch.object(native.os, "scandir", side_effect=scan):
            with self.assertRaisesRegex(native.NativeFailure, "MXC_SCRATCH_(?:LINK_REJECTED|MONITOR_UNAVAILABLE)"):
                native.scan_scratch(scratch, expected_identity=(info.st_dev, info.st_ino), maximum_bytes=1024, maximum_entries=16)
        self.assertEqual(traversed, [scratch])
        self.assertEqual((outside / "private.txt").read_text(), "QA_METADATA_CANARY")


@unittest.skipUnless(os.name == "nt" and os.environ.get("AURA_RUN_MXC_INTEGRATION") == "1", "explicit fixed-code Windows MXC integration opt-in")
class MxcFixedIntegration(MxcContracts):
    def setUp(self):
        super().setUp()
        self.runner = mxc.MxcPythonRunner(live_enabled=True, scratch_parent=self.parent)

    # Inherit only fixtures, not fake-transport test cases.
    def test_real_unicode_json_and_empty_ambient_configuration(self):
        result = self.runner.execute("import json; print(json.dumps(input, ensure_ascii=False))", {"quote": "'\"\\鲸鱼🐋", "value": [None, True, 42]}, scope=self.scope)
        self.assertEqual(result.code, "MXC_OK")
        self.assertEqual(result.value["quote"], "'\"\\鲸鱼🐋")
        self.assertTrue(result.limits_verified and result.tree_reaped and result.scratch_removed)

    def test_real_scope_network_and_approved_readonly_copy(self):
        source = self.workspace / "private.txt"
        source.write_bytes(b"QA_SYNTHETIC_APPROVED_COPY")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0)); listener.listen(4)
        port = listener.getsockname()[1]
        code = '''import json,pathlib,socket
result = {}
copy = pathlib.Path(INPUT_DIR)/'approved.txt'
result['copy'] = copy.read_text()
for name,path in [('original',pathlib.Path(input['original'])),('copy',copy)]:
 try:
  path.write_text('forbidden') if name == 'copy' else path.read_text()
  result[name+'_denied'] = False
 except PermissionError: result[name+'_denied'] = True
for name,host,port in [('loopback','127.0.0.1',input['port']),('external','1.1.1.1',443)]:
 try:
  with socket.create_connection((host,port),timeout=1): pass
  result[name+'_denied'] = False
 except OSError as error: result[name+'_denied'] = error.winerror == 10013
print(json.dumps(result))'''
        try:
            result = self.runner.execute(code, {"original": str(source), "port": port}, scope=self.scope, authorized_inputs=[mxc.AuthorizedInput("private.txt", digest, "approved.txt")])
            self.assertEqual(result.code, "MXC_OK")
            self.assertEqual(result.value, {"copy": "QA_SYNTHETIC_APPROVED_COPY", "original_denied": True, "copy_denied": True, "loopback_denied": True, "external_denied": True})
            listener.settimeout(.1)
            with self.assertRaises(TimeoutError): listener.accept()
        finally:
            listener.close()
        self.assertEqual(source.read_bytes(), b"QA_SYNTHETIC_APPROVED_COPY")

    def test_real_memory_commit_limit(self):
        self.runner.policy = replace(mxc.MxcPolicy(), process_memory_mb=128)
        code = "import json\nblocks=[]\ntry:\n for i in range(256): blocks.append(bytearray(4194304))\nexcept MemoryError: print(json.dumps({'memory_denied':True}))"
        result = self.runner.execute(code, scope=self.scope)
        self.assertEqual(result.code, "MXC_OK")
        self.assertEqual(result.value, {"memory_denied": True})

    def test_real_cpu_time_and_wall_time_limits(self):
        self.runner.policy = replace(mxc.MxcPolicy(), cpu_seconds=1, timeout_seconds=12)
        started = time.monotonic()
        result = self.runner.execute("import time\nend=time.monotonic()+15\nwhile time.monotonic()<end: pass", scope=self.scope)
        self.assertEqual(result.code, "MXC_CPU_LIMIT")
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result.tree_reaped and result.scratch_removed)
        self.runner.policy = replace(mxc.MxcPolicy(), timeout_seconds=1)
        result = self.runner.execute("import time; time.sleep(15)", scope=self.scope)
        self.assertEqual(result.code, "MXC_TIMEOUT")
        self.assertTrue(result.tree_reaped)

    def test_real_stdout_stderr_limits_and_strict_protocol(self):
        for code in ("import sys; sys.stdout.write('x'*1048576)", "import sys; sys.stderr.write('x'*1048576)"):
            result = self.runner.execute(code, scope=self.scope)
            self.assertEqual(result.code, "MXC_OUTPUT_LIMIT")
            self.assertIsNone(result.value)
            self.assertTrue(result.tree_reaped)
        self.assertEqual(self.runner.execute("print('not json')", scope=self.scope).code, "MXC_OUTPUT_NOT_STRICT_JSON")

    def test_real_scratch_soft_limit(self):
        self.runner.policy = replace(mxc.MxcPolicy(), scratch_limit=1024)
        code = "from pathlib import Path\nimport time\nPath('qa-data').write_bytes(b'x'*4096)\ntime.sleep(15)"
        result = self.runner.execute(code, scope=self.scope)
        self.assertEqual(result.code, "MXC_SCRATCH_LIMIT")
        self.assertTrue(result.scratch_removed)

    def test_real_process_count_limit_and_background_cleanup(self):
        self.runner.policy = replace(mxc.MxcPolicy(), process_limit=6)
        code = "import json,subprocess,sys\nchildren=[]\ndenied=False\ntry:\n for i in range(32): children.append(subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(2)']))\nexcept OSError: denied=True\nprint(json.dumps({'denied':denied,'spawned':len(children)}))"
        result = self.runner.execute(code, scope=self.scope)
        self.assertEqual(result.code, "MXC_OK")
        self.assertTrue(result.value["denied"])
        self.assertLess(result.value["spawned"], 32)
        self.assertTrue(result.tree_reaped and result.scratch_removed)

    def test_real_unrelated_inheritable_handle_not_visible(self):
        import msvcrt
        canary = self.parent / "host-private.bin"
        canary.write_bytes(b"QA_HOST_HANDLE_ONLY_CANARY")
        descriptor = os.open(canary, os.O_RDONLY | os.O_BINARY)
        os.set_inheritable(descriptor, True)
        try:
            code = "import ctypes as C,json\nk=C.WinDLL('kernel32',use_last_error=True)\nk.ReadFile.argtypes=[C.c_void_p,C.c_void_p,C.c_uint32,C.POINTER(C.c_uint32),C.c_void_p]\nb=C.create_string_buffer(64);n=C.c_uint32()\ntry: ok=k.ReadFile(input['handle'],b,64,C.byref(n),None)\nexcept OSError: ok=False\nprint(json.dumps({'leaked':bool(ok and b.raw[:n.value]==b'QA_HOST_HANDLE_ONLY_CANARY')}))"
            result = self.runner.execute(code, {"handle": msvcrt.get_osfhandle(descriptor)}, scope=self.scope)
            self.assertEqual(result.code, "MXC_OK")
            self.assertEqual(result.value, {"leaked": False})
        finally:
            os.close(descriptor)

    def test_real_owned_config_root_excludes_trusted_parent_overlay(self):
        private = self.parent / "config-private.txt"
        private.write_text("QA_CONFIG_CANARY")
        outer_git = self.parent / ".git"; outer_git.mkdir()
        (outer_git / "HEAD").write_text("ref: refs/heads/qa-parent\n")
        outer_config = self.parent / ".codex"; outer_config.mkdir()
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(4)
        original = self.runner._stage
        remove_boundary = False
        def stage(job, *args):
            original(job, *args)
            config = job.root / "client" / "config.toml"
            config.write_text(config.read_text() + "\n[projects." + json.dumps(str(self.parent)) + ']\ntrust_level="trusted"\n')
            poison = '[permissions.' + job.profile + '.filesystem]\n' + json.dumps(str(private)) + '="read"\n[permissions.' + job.profile + '.network]\nenabled=true\n'
            (outer_config / "config.toml").write_text(poison)
            if remove_boundary:
                (job.root / "input" / ".git" / "HEAD").unlink()
                (job.root / "input" / ".git").rmdir()
                (job.root / "input" / ".aura-mxc-root").unlink()
        self.runner._stage = stage
        code = "import pathlib,json,socket\nresult={}\ntry: pathlib.Path(input['private']).read_text();result['private_denied']=False\nexcept PermissionError: result['private_denied']=True\ntry:\n with socket.create_connection(('127.0.0.1',input['port']),timeout=1): pass\n result['network_denied']=False\nexcept OSError as error: result['network_denied']=error.winerror==10013\nprint(json.dumps(result))"
        try:
            result = self.runner.execute(code, {"private": str(private), "port": listener.getsockname()[1]}, scope=self.scope)
            self.assertEqual(result.code, "MXC_OK")
            self.assertEqual(result.value, {"private_denied": True, "network_denied": True})
            # A control intentionally removes only this synthetic root boundary:
            # the trusted parent's exact matching profile grant is then loaded.
            # This proves the poison is active rather than ignored by trust.
            remove_boundary = True
            original_native = self.runner._native_run
            def without_boundary(argv, **options):
                argv = ['project_root_markers=[".git"]' if token == 'project_root_markers=[".aura-mxc-root"]' else token for token in argv]
                return original_native(argv, **options)
            self.runner._native_run = without_boundary
            control = self.runner.execute(code, {"private": str(private), "port": listener.getsockname()[1]}, scope=self.scope)
            self.assertEqual(control.code, "MXC_OK")
            self.assertEqual(control.value, {"private_denied": False, "network_denied": True})
        finally:
            listener.close()

    def test_real_unconfirmed_cleanup_retains_exact_authority_pins_and_blocks_replay(self):
        created = []
        def factory():
            process = native.WindowsJobProcess()
            process._qa_restore = process.k.TerminateJobObject
            process.k.AssignProcessToJobObject = lambda *args: 0
            process.k.TerminateJobObject = lambda *args: 0
            created.append(process)
            return process
        self.runner._native_run = lambda argv, **kw: native.run(argv, factory=factory, **kw)
        try:
            result = self.runner.execute("print('never executed')", scope=self.scope)
            self.assertEqual(result.code, "MXC_CLEANUP_UNCONFIRMED")
            self.assertFalse(result.tree_reaped)
            self.assertEqual(len(self.runner._pending), 1)
            pending = self.runner._pending[0]
            self.assertTrue(pending.job.root.exists())
            self.assertTrue(pending.pins and all(not stream.closed for stream in pending.pins))
            self.assertFalse(created[0].resumed)
            self.assertTrue(created[0].process and created[0].job)
            self.assertTrue(native.has_pending_cleanup())
            self.assertEqual(self.runner.execute("print('must not replay')", scope=self.scope).code, "MXC_CLEANUP_PENDING")
            self.assertEqual(len(created), 1)
            self.assertFalse(self.runner.reconcile_cleanup(scope=self.scope).tree_reaped)
            self.assertTrue(pending.job.root.exists())
            self.assertTrue(all(not stream.closed for stream in pending.pins))
            import pickle
            with self.assertRaises(TypeError): pickle.dumps(pending.lease)
            created[0].k.TerminateJobObject = created[0]._qa_restore
            reconciled = self.runner.reconcile_cleanup(scope=self.scope)
            self.assertEqual(reconciled.code, "MXC_CLEANUP_RECONCILED")
            self.assertTrue(reconciled.tree_reaped and reconciled.scratch_removed)
            self.assertFalse(native.has_pending_cleanup())
            self.assertTrue(all(stream.closed for stream in pending.pins))
            self.assertFalse(pending.job.root.exists())
            self.assertEqual(len(created), 1)
        finally:
            for process in created:
                process.k.TerminateJobObject = process._qa_restore
            if self.runner._pending:
                self.runner.reconcile_cleanup(scope=self.scope)

    def test_real_async_repeated_stop_and_scope_revocation(self):
        async def stopped():
            staged = []
            original = self.runner._job
            def stage(*args):
                job = original(*args); staged.append(job); return job
            self.runner._job = stage
            code = "from pathlib import Path\nimport time\nPath('started').write_text('QA')\ntime.sleep(15)"
            operation = asyncio.create_task(self.runner.execute_async(code, scope=self.scope, cancel_event=None))
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and not (staged and (staged[0].task / "started").is_file()):
                await asyncio.sleep(.01)
            self.assertTrue(staged and (staged[0].task / "started").is_file())
            started = time.monotonic(); operation.cancel(); await asyncio.sleep(.001); operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(staged[0].root.exists())
            self.runner._job = original
        asyncio.run(stopped())
        staged = []
        original = self.runner._job
        def stage(*args):
            job = original(*args); staged.append(job); return job
        self.runner._job = stage
        result = []
        thread = threading.Thread(target=lambda: result.append(self.runner.execute("from pathlib import Path\nimport time\nPath('started').write_text('QA')\ntime.sleep(15)", scope=self.scope)))
        thread.start()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and not (staged and (staged[0].task / "started").is_file()): time.sleep(.01)
        self.current = False
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0].code, "MXC_SCOPE_REVOKED")
        self.assertTrue(result[0].tree_reaped and result[0].scratch_removed)

    def test_real_cancel_kills_payload_and_descendant(self):
        cancel = threading.Event()
        staged = []
        original = self.runner._job
        def stage(*args):
            job = original(*args); staged.append(job); return job
        self.runner._job = stage
        result = []
        code = "import subprocess,sys,time,json,pathlib,os\np=subprocess.Popen([sys.executable,'-I','-S','-c','import time; time.sleep(15)'])\npathlib.Path('ready.json').write_text(json.dumps([os.getpid(),p.pid]))\ntime.sleep(15)"
        thread = threading.Thread(target=lambda: result.append(self.runner.execute(code, scope=self.scope, cancel_event=cancel)))
        thread.start()
        deadline = time.monotonic() + 6
        ready = None
        while time.monotonic() < deadline:
            if staged and (staged[0].task / "ready.json").is_file():
                ready = json.loads((staged[0].task / "ready.json").read_text()); break
            time.sleep(.01)
        self.assertIsNotNone(ready)
        import ctypes as C
        k = C.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [C.c_uint32, C.c_int, C.c_uint32]; k.OpenProcess.restype = C.c_void_p
        k.WaitForSingleObject.argtypes = [C.c_void_p, C.c_uint32]; k.CloseHandle.argtypes = [C.c_void_p]
        handles = [k.OpenProcess(0x00100000, False, pid) for pid in ready]
        try:
            self.assertTrue(all(handles))
            started = time.monotonic(); cancel.set(); thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(all(k.WaitForSingleObject(handle, 0) == 0 for handle in handles))
            self.assertEqual(result[0].code, "MXC_CANCELLED")
            self.assertTrue(result[0].tree_reaped and result[0].scratch_removed)
        finally:
            cancel.set(); thread.join(5)
            for handle in handles:
                if handle: k.CloseHandle(handle)

    def test_native_assignment_and_resource_failure_never_resume_payload(self):
        marker = self.parent / "never-executed.txt"
        argv = [str(mxc.PYTHON_ROOT / "python.exe"), "-I", "-S", "-c", "from pathlib import Path;Path(" + repr(str(marker)) + ").write_text('BAD')"]
        for method, expected in (("AssignProcessToJobObject", "MXC_JOB_ASSIGN_FAILED"), ("SetInformationJobObject", "MXC_JOB_LIMIT_FAILED")):
            created = []
            def factory():
                process = native.WindowsJobProcess()
                setattr(process.k, method, lambda *args: 0)
                created.append(process)
                return process
            result = native.run(argv, environment={"SystemRoot": str(mxc.SYSTEM32.parent), "SystemDrive": mxc.SYSTEM32.drive}, cwd=self.parent, policy=mxc.MxcPolicy(), cancel_event=threading.Event(), watch=lambda: True, factory=factory)
            self.assertEqual(result.code, expected)
            self.assertFalse(result.resumed)
            self.assertTrue(result.tree_reaped)
            self.assertFalse(marker.exists())
        def mismatch_factory():
            process = native.WindowsJobProcess()
            original = process._query
            def query(kind, value):
                value = original(kind, value)
                if kind == 9: value.basic.active_limit += 1
                return value
            process._query = query
            return process
        result = native.run(argv, environment={"SystemRoot": str(mxc.SYSTEM32.parent), "SystemDrive": mxc.SYSTEM32.drive}, cwd=self.parent, policy=mxc.MxcPolicy(), cancel_event=threading.Event(), watch=lambda: True, factory=mismatch_factory)
        self.assertEqual(result.code, "MXC_JOB_LIMIT_MISMATCH")
        self.assertFalse(result.resumed)
        self.assertTrue(result.tree_reaped)
        self.assertFalse(marker.exists())


# Reusing setUp/tearDown should not run contract cases against a live transport.
for _name in tuple(MxcContracts.__dict__):
    if _name.startswith("test_"):
        setattr(MxcFixedIntegration, _name, None)


if __name__ == "__main__":
    unittest.main()
