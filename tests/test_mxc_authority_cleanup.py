"""MXC pre-Resume and exact cleanup contracts. Fake kernels never run code."""
import ctypes as C
from contextlib import contextmanager
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

from agent_backend.nanobot import mxc_execution as mxc, mxc_native as native
from agent_backend.nanobot.safe_files import WorkspaceFiles
from tests.test_mxc_backend_review import FakeKernel, Function


class MxcAuthorityCleanupTests(unittest.TestCase):
    def setUp(self): self.owned = []

    def process(self, kernel=None):
        kernel = kernel or FakeKernel()
        with patch.object(native.C, "WinDLL", return_value=kernel):
            process = native.WindowsJobProcess()
        self.owned.append(process)
        return process, kernel

    def test_stop_received_during_suspended_creation_never_resumes_payload(self):
        cancel = threading.Event(); kernel = FakeKernel()
        create = kernel.CreateProcessW.callback
        def stop_during_create(*arguments):
            result = create(*arguments)
            cancel.set()
            return result
        kernel.CreateProcessW = Function(stop_during_create)
        process, kernel = self.process(kernel)
        process.terminate_and_reap = lambda: True
        process.read_available = lambda _: b""
        process.accounting = lambda: SimpleNamespace(user_time=0, active_processes=0)
        result = native._run([r"C:\owned\fixed.exe"], environment={"SystemRoot": r"C:\Windows"},
            cwd=Path(r"E:\owned"), policy=mxc.MxcPolicy(), cancel_event=cancel,
            watch=lambda: not cancel.is_set(), factory=lambda: process)
        self.assertEqual("MXC_CANCELLED", result.code)
        self.assertFalse(result.resumed, "Stop arrived before Resume, so even a transient payload dispatch is forbidden")
        self.assertNotIn("resume", kernel.events)

    def test_failed_handle_closes_retain_original_owner_and_globally_block_new_work(self):
        report = _fixture("close_failure")
        self.assertTrue(report["original_handles_retained"])
        self.assertTrue(report["global_gate"])
        self.assertTrue(report["retry_remains_unknown"])
        self.assertTrue(report["no_second_close_attempt"])

    def native(self, process, *, cancel=None, watch=lambda: True, **options):
        process.terminate_and_reap = lambda: True
        process.read_available = lambda _: b""
        process.accounting = lambda: SimpleNamespace(user_time=0, active_processes=0)
        process.exited = lambda: True
        return native._run([r"C:\owned\fixed.exe"], environment={"SystemRoot": r"C:\Windows"},
            cwd=Path(r"E:\owned"), policy=mxc.MxcPolicy(), cancel_event=cancel or threading.Event(),
            watch=watch, factory=lambda: process, **options)

    def test_revocation_during_native_create_is_sampled_again_before_resume(self):
        process, kernel = self.process(); current = [True]
        create = kernel.CreateProcessW.callback
        def revoke(*arguments):
            result = create(*arguments); current[0] = False; return result
        kernel.CreateProcessW = Function(revoke)
        result = self.native(process, watch=lambda: current[0])
        self.assertEqual("MXC_SCOPE_REVOKED", result.code)
        self.assertFalse(result.resumed)
        self.assertNotIn("resume", kernel.events)

    def test_shared_guard_surrounds_fresh_callback_and_resume_and_attempt_precedes_create(self):
        process, kernel = self.process(); lock = threading.RLock(); order=[]
        @contextmanager
        def guarded():
            with lock:
                order.append("guard-enter"); yield; order.append("guard-exit")
        def fresh(): self.assertTrue(lock._is_owned()); order.append("fresh")
        resume = kernel.ResumeThread.callback
        def resumed(*arguments):
            self.assertTrue(lock._is_owned()); order.append("resume"); return resume(*arguments)
        create = kernel.CreateProcessW.callback
        def created(*arguments): order.append("create"); return create(*arguments)
        kernel.ResumeThread = Function(resumed); kernel.CreateProcessW = Function(created)
        result = self.native(process, authority_guard=guarded, before_resume=fresh,
            launch_attempt_callback=lambda:order.append("attempt"))
        self.assertEqual("MXC_EXITED", result.code)
        self.assertEqual(["attempt","create","guard-enter","fresh","resume","guard-exit"], order)
        self.assertTrue(result.resources_closed)

    def test_hook_exception_is_fixed_and_never_resumes_or_exposes_exception_text(self):
        for name in ("before_resume", "launch_attempt_callback"):
            process, kernel = self.process()
            def deny(): raise ValueError("SYNTHETIC_PRIVATE_HOOK_DETAIL")
            result = self.native(process, **{name: deny})
            self.assertEqual("MXC_RESUME_AUTHORITY_FAILED" if name=="before_resume" else "MXC_LAUNCH_AUTHORITY_FAILED", result.code)
            self.assertFalse(result.resumed)
            self.assertNotIn("resume", kernel.events)
            if name=="launch_attempt_callback":self.assertNotIn("create-suspended", kernel.events)
            self.assertNotIn(b"PRIVATE", result.stdout+result.stderr)

    def test_dictionary_is_not_a_host_authority_callback_and_does_not_construct_native_owner(self):
        for name in ("authority_guard", "before_resume", "launch_attempt_callback"):
            with patch.object(native, "WindowsJobProcess", side_effect=AssertionError("No native owner")):
                result = native._run([], environment={}, cwd=Path.cwd(), policy=mxc.MxcPolicy(),
                    cancel_event=threading.Event(), watch=lambda:True, factory=lambda:self.fail("No native owner"),
                    **{name:{"authorized":True}})
            self.assertEqual("MXC_INVALID_AUTHORITY", result.code)

    def test_create_exception_after_returned_handles_keeps_suspended_root_owned(self):
        process,kernel=self.process();process.configure(mxc.MxcPolicy())
        create=kernel.CreateProcessW.callback
        def ambiguous(*args):create(*args);raise RuntimeError("SYNTHETIC_CREATE_ACK_FAILURE")
        kernel.CreateProcessW=Function(ambiguous)
        with self.assertRaisesRegex(RuntimeError,"SYNTHETIC_CREATE_ACK_FAILURE"):
            process.launch([r"C:\owned\fixed.exe"],{},r"E:\owned")
        self.assertTrue(process.process and process.thread)
        self.assertFalse(process.resumed)
        self.assertNotIn("resume",kernel.events)
        self.assertTrue(process.close())

    def test_pin_close_raising_after_closed_true_is_permanent_and_scratch_is_retained(self):
        report = _fixture("pin_close_failure")
        for key in ("tree_reaped", "resources_unknown", "scratch_retained", "pin_wrapper_retained",
                    "global_gate", "new_runner_blocked", "reconcile_stays_unknown", "no_second_pin_close"):
            self.assertTrue(report[key], key)

    def test_monitor_directory_close_failure_retains_original_api_and_handle_permanently(self):
        report = _fixture("monitor_close_failure")
        for key in ("close_failed", "owner_retained", "global_gate", "reconcile_stays_unknown", "no_second_close_attempt"):
            self.assertTrue(report[key], key)


def _fixture(name, *, actual=False):
    parent=Path(__file__).resolve().parents[1]/"agent_workspace";parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mxc-sticky-fixture-",dir=parent) as owned:
        interpreter=str(mxc.PYTHON_ROOT/"python.exe") if actual else sys.executable
        source=Path(__file__).resolve().parents[1]
        launch="import sys,runpy;sys.path.insert(0,"+repr(str(source))+");runpy.run_module('tests.test_mxc_authority_cleanup',run_name='__main__')"
        environment={"SystemRoot":str(mxc.SYSTEM32.parent),"WINDIR":str(mxc.SYSTEM32.parent),
            "PATH":str(mxc.PYTHON_ROOT)+os.pathsep+str(mxc.SYSTEM32)}
        process=subprocess.run([interpreter,"-I","-S","-c",launch,"--fixture",name,owned],
            cwd=source,env=environment,capture_output=True,timeout=12,
            **({"creationflags":subprocess.CREATE_NO_WINDOW} if sys.platform=="win32" else {}))
        if process.returncode!=0:raise AssertionError("Owned MXC fixture failed without a successful protocol result")
        if len(process.stdout)>16384 or len(process.stderr)>16384:raise AssertionError("Owned fixture output exceeded its fixed budget")
        return json.loads(process.stdout)


def _close_fixture():
    kernel=FakeKernel()
    with patch.object(native.C,"WinDLL",return_value=kernel): process=native.WindowsJobProcess()
    process.handles=[101,102];process.thread=103;process.process=104;process.job=105
    closes=[]
    def fail(handle): closes.append(handle);C.set_last_error(5);return 0
    kernel.CloseHandle=Function(fail)
    process.close()
    retained=(process.handles==[101,102] and (process.thread,process.process,process.job)==(103,104,105))
    count=len(closes)
    kernel.CloseHandle=Function(lambda handle:closes.append(handle) or 1)
    lease=getattr(process,"_cleanup_lease",None)
    unknown=(lease is not None and native.reconcile_cleanup(lease) is False and process.close() is False)
    print(json.dumps({"original_handles_retained":retained,"global_gate":native.has_pending_cleanup(),
        "retry_remains_unknown":unknown,"no_second_close_attempt":len(closes)==count}))


def _pin_fixture(root):
    workspace=root/"workspace";workspace.mkdir()
    scope=mxc.HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),grant_revision='a'*64,is_current=lambda:True)
    class Pin:
        closed=False
        calls=0
        def fileno(self):return 37
        def close(self):self.calls+=1;self.closed=True;raise OSError('fixed synthetic close failure')
    pin=Pin();runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=root)
    runner._pins=lambda:(pin,)
    runner._native_run=lambda argv,**kwargs:native.NativeResult('MXC_EXITED',0,mxc.CODEX_VERSION.encode() if argv[1:]==['--version'] else b'{"owned":true}')
    result=runner.execute('print("never executed by fake")',scope=scope)
    pending=runner._pending[0]
    before=pin.calls
    after=runner.reconcile_cleanup(scope=scope)
    other=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=root)
    blocked=other.execute('must not replay',scope=scope)
    print(json.dumps({'tree_reaped':result.tree_reaped,'resources_unknown':not result.resources_closed,
        'scratch_retained':pending.job.root.is_dir(),'pin_wrapper_retained':pin in pending.pins and pin.closed,
        'global_gate':native.has_pending_cleanup(),'new_runner_blocked':blocked.code=='MXC_CLEANUP_PENDING',
        'reconcile_stays_unknown':after.code=='MXC_CLEANUP_UNCONFIRMED','no_second_pin_close':pin.calls==before}))


def _monitor_fixture(root):
    calls=[]
    class API:
        def __init__(self,callback):self.callback=callback
        def __call__(self,*args):return self.callback(*args)
    kernel=SimpleNamespace(CreateFileW=API(lambda *_:111),
        GetFileInformationByHandleEx=API(lambda handle,kind,pointer,*_:setattr(pointer._obj,'attributes',0x10) or 1),
        CloseHandle=API(lambda handle:calls.append(handle) or 0))
    before=root.stat();failed=False
    with patch.object(native.C,'WinDLL',return_value=kernel):
        try:native.scan_scratch(root,expected_identity=(before.st_dev,before.st_ino),maximum_bytes=1024,maximum_entries=8)
        except native.NativeFailure:failed=True
    lease=next(iter(native._pending_cleanup.values()))
    count=len(calls);kernel.CloseHandle=API(lambda handle:calls.append(handle) or 1)
    reconciled=native.reconcile_cleanup(lease)
    print(json.dumps({'close_failed':failed,'owner_retained':lease._process._owners==(kernel,111),
        'global_gate':native.has_pending_cleanup(),'reconcile_stays_unknown':reconciled is False,'no_second_close_attempt':len(calls)==count}))


@unittest.skipUnless(os.environ.get("AURA_RUN_MXC_AUTHORITY_INTEGRATION")=="1"
    and sys.platform=="win32" and mxc.CODEX.is_file() and (mxc.PYTHON_ROOT/"python.exe").is_file(),
    "Explicit fixed trusted native MXC opt-in is required")
class MxcFixedAuthorityIntegration(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/"agent_workspace";parent.mkdir(exist_ok=True)
        self.owner=tempfile.TemporaryDirectory(prefix="mxc-fixed-authority-",dir=parent)
        self.root=Path(self.owner.name);self.workspace=self.root/"workspace";self.workspace.mkdir()
        self.current=True;self.lock=threading.RLock()
        self.scope=mxc.HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(self.workspace),
            grant_revision="a"*64,is_current=lambda:self.current)
        self.runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=self.root)

    def tearDown(self):
        if native.has_pending_cleanup():
            raise AssertionError("The ordinary fixed authority fixture must not leave resources pending")
        self.owner.cleanup()

    def test_actual_stop_or_scope_revoke_in_pre_resume_hook_never_runs_payload_marker(self):
        for stop in (True,False):
            with self.subTest(stop=stop):
                self.current=True;cancel=threading.Event();calls=[];observed=[];attempts=[]
                original=self.runner._native_run
                def transport(argv,**kwargs):
                    result=original(argv,**kwargs)
                    if "sandbox" in argv:
                        observed.append((result,(Path(argv[-1]).parent.parent/"task"/"MUST_NOT_RUN").exists()))
                    return result
                self.runner._native_run=transport
                def before(scope):
                    self.assertIs(scope,self.scope);self.assertTrue(self.lock._is_owned());calls.append(True)
                    if len(calls)==2:
                        if stop:cancel.set()
                        else:self.current=False
                try:
                    result=self.runner.execute("from pathlib import Path;Path('MUST_NOT_RUN').write_text('bad');print('{}')",
                        scope=self.scope,cancel_event=cancel,authority_guard=lambda:self.lock,before_resume=before,
                        launch_attempt_callback=lambda:attempts.append(True))
                finally:self.runner._native_run=original
                self.assertEqual("MXC_CANCELLED" if stop else "MXC_SCOPE_REVOKED",result.code)
                self.assertEqual(2,len(calls));self.assertEqual(2,len(attempts));self.assertEqual(1,len(observed))
                self.assertFalse(observed[0][0].resumed)
                self.assertFalse(observed[0][1])
                self.assertTrue(result.tree_reaped and result.resources_closed and result.scratch_removed)
                self.assertEqual([self.workspace],list(self.root.iterdir()))

    def test_actual_authorized_fixed_json_keeps_shared_guard_and_attempt_callbacks(self):
        calls=[];attempts=[]
        def before(scope):
            self.assertIs(scope,self.scope);self.assertTrue(self.lock._is_owned());calls.append(True)
        result=self.runner.execute("import json;print(json.dumps({'owned':True,'text':'鲸🐋'}))",scope=self.scope,
            authority_guard=lambda:self.lock,before_resume=before,launch_attempt_callback=lambda:attempts.append(True))
        self.assertEqual("MXC_OK",result.code)
        self.assertEqual({"owned":True,"text":"鲸🐋"},result.value)
        self.assertEqual((2,2),(len(calls),len(attempts)))
        self.assertTrue(result.tree_reaped and result.resources_closed and result.scratch_removed and result.limits_verified)
        self.assertFalse(mxc.MxcPythonRunner().live_enabled)

    def test_actual_failed_job_handle_close_retains_pins_and_never_retries_that_handle(self):
        report=_fixture("real_job_close_failure",actual=True)
        for key in ("tree_reaped","resources_unknown","limits_verified","scratch_retained","pins_retained",
                    "original_job_handle_retained","global_gate","new_runner_blocked","reconcile_stays_unknown",
                    "no_second_close_attempt","not_resumed_again"):
            self.assertTrue(report[key],key)


def _real_close_fixture(root):
    workspace=root/"workspace";workspace.mkdir()
    scope=mxc.HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),
        grant_revision='a'*64,is_current=lambda:True)
    runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=root)
    created=[];attempts=[]
    def factory():
        process=native.WindowsJobProcess();close=process.k.CloseHandle
        def fail_owned_job(handle):
            if handle==process.job:
                attempts.append(True);C.set_last_error(5);return 0
            return close(handle)
        process.k.CloseHandle=fail_owned_job
        process._qa_restore_close=close;created.append(process)
        return process
    runner._native_run=lambda argv,**kwargs:native.run(argv,factory=factory,**kwargs)
    result=runner.execute("print('{}')",scope=scope)
    pending=runner._pending[0];count=len(attempts)
    for process in created:process.k.CloseHandle=process._qa_restore_close
    reconciled=runner.reconcile_cleanup(scope=scope)
    other=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=root)
    blocked=other.execute("must not replay",scope=scope)
    print(json.dumps({'tree_reaped':result.tree_reaped,'resources_unknown':not result.resources_closed,
        'limits_verified':bool(created and created[0].verified),'scratch_retained':pending.job.root.is_dir(),
        'pins_retained':bool(pending.pins) and all(not stream.closed for stream in pending.pins),
        'original_job_handle_retained':bool(created and created[0].job),'global_gate':native.has_pending_cleanup(),
        'new_runner_blocked':blocked.code=='MXC_CLEANUP_PENDING','reconcile_stays_unknown':reconciled.code=='MXC_CLEANUP_UNCONFIRMED',
        'no_second_close_attempt':len(attempts)==count,'not_resumed_again':len(created)==1}))


if __name__ == "__main__":
    if len(sys.argv)==4 and sys.argv[1]=="--fixture":
        fixture,root=sys.argv[2],Path(sys.argv[3])
        if fixture=="close_failure":_close_fixture()
        elif fixture=="pin_close_failure":_pin_fixture(root)
        elif fixture=="monitor_close_failure":_monitor_fixture(root)
        elif fixture=="real_job_close_failure":_real_close_fixture(root)
        else:raise SystemExit(2)
    else: unittest.main()
