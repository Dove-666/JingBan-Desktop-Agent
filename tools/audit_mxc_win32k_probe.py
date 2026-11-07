"""Fixed owned-window experiment; does not enable production execution.

The QA kernel proxy only adds the documented process-creation mitigation to
the outer pinned CLI. It does not replace its policy, token, Job, or cleanup.
"""
from __future__ import annotations

import argparse
import ctypes as C
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from tools.audit_mxc_desktop_boundary import _HiddenWindow, FIXED_CODE
from agent_backend.nanobot import mxc_execution as mxc, mxc_native as native
from agent_backend.nanobot.container_execution import HostExecutionScope
from agent_backend.nanobot.safe_files import WorkspaceFiles

_RETAINED = []


class _MitigationKernel:
    def __init__(self, kernel, owner, records):
        self.kernel, self.owner, self.records = kernel, owner, records
        self.policy = C.c_uint64(1 << 28)
        kernel.GetProcessMitigationPolicy.argtypes = [C.c_void_p,C.c_int,C.c_void_p,C.c_size_t]
        kernel.GetProcessMitigationPolicy.restype = C.c_int

    def __getattr__(self, name): return getattr(self.kernel, name)

    def InitializeProcThreadAttributeList(self, storage, count, flags, size):
        return self.kernel.InitializeProcThreadAttributeList(storage, 2, flags, size)

    def UpdateProcThreadAttribute(self, storage, flags, attribute, value, size, previous, returned):
        first = self.kernel.UpdateProcThreadAttribute(storage, flags, attribute, value, size, previous, returned)
        if not first: return first
        return self.kernel.UpdateProcThreadAttribute(storage, 0, 0x00020007,
            C.byref(self.policy), C.sizeof(self.policy), None, None)

    def ResumeThread(self, thread):
        state = C.c_uint32()
        queried = bool(self.kernel.GetProcessMitigationPolicy(self.owner.process,4,C.byref(state),C.sizeof(state)))
        self.records.append({'queried_before_resume':queried,'win32k_disabled':bool(state.value & 1),
            'query_error':0 if queried else C.get_last_error()})
        if not queried or state.value != 1: raise native.NativeFailure('MXC_QA_MITIGATION_UNCONFIRMED')
        return self.kernel.ResumeThread(thread)


CALCULATION = r'''import ctypes as C,decimal,io,json,subprocess,sys,zipfile
k=C.WinDLL('kernel32.dll',use_last_error=True)
k.GetCurrentProcess.restype=C.c_void_p
k.GetProcessMitigationPolicy.argtypes=[C.c_void_p,C.c_int,C.c_void_p,C.c_size_t];k.GetProcessMitigationPolicy.restype=C.c_int
p=C.c_uint32();q=k.GetProcessMitigationPolicy(k.GetCurrentProcess(),4,C.byref(p),4)
child="import ctypes as C,json;k=C.WinDLL('kernel32');k.GetCurrentProcess.restype=C.c_void_p;k.GetProcessMitigationPolicy.argtypes=[C.c_void_p,C.c_int,C.c_void_p,C.c_size_t];v=C.c_uint32();q=k.GetProcessMitigationPolicy(k.GetCurrentProcess(),4,C.byref(v),4);print(json.dumps({'query':bool(q),'win32k_disabled':bool(v.value&1)}))"
result=subprocess.run([sys.executable,'-I','-S','-B','-c',child],capture_output=True,timeout=2,check=False)
b=io.BytesIO()
with zipfile.ZipFile(b,'w') as z:z.writestr('owned.txt','鲸🌊')
with zipfile.ZipFile(io.BytesIO(b.getvalue())) as z:ok=z.read('owned.txt').decode()=='鲸🌊'
print(json.dumps({'query':bool(q),'win32k_disabled':bool(p.value&1),'decimal':str(decimal.Decimal('0.1')+decimal.Decimal('0.2'))=='0.3','zip':ok,'child_exit_zero':result.returncode==0,'child':json.loads(result.stdout) if result.returncode==0 else None}))
'''

ENTRY_EXPERIMENT = r'''import ctypes as C,contextlib,decimal,io,json,subprocess,sys,zipfile
k=C.WinDLL('kernel32.dll',use_last_error=True)
k.GetCurrentProcess.restype=C.c_void_p
k.GetProcessMitigationPolicy.argtypes=[C.c_void_p,C.c_int,C.c_void_p,C.c_size_t];k.GetProcessMitigationPolicy.restype=C.c_int
k.SetProcessMitigationPolicy.argtypes=[C.c_int,C.c_void_p,C.c_size_t];k.SetProcessMitigationPolicy.restype=C.c_int
def query():
    v=C.c_uint32();C.set_last_error(0);q=k.GetProcessMitigationPolicy(k.GetCurrentProcess(),4,C.byref(v),4)
    return {'query':bool(q),'win32k_disabled':bool(v.value&1),'error':0 if q else C.get_last_error()}
out={'before':query()}
v=C.c_uint32(1);C.set_last_error(0);ok=k.SetProcessMitigationPolicy(4,C.byref(v),4)
out['enable']={'returned':bool(ok),'error':0 if ok else C.get_last_error()};out['after_enable']=query()
v=C.c_uint32(0);C.set_last_error(0);ok=k.SetProcessMitigationPolicy(4,C.byref(v),4)
out['disable_attempt']={'returned':bool(ok),'error':0 if ok else C.get_last_error()};out['after_disable_attempt']=query()
def observe():
    text=io.StringIO()
    try:
        with contextlib.redirect_stdout(text):exec(compile(input['observation_code'],'<fixed-owned-window>','exec'),{'input':input['window']})
        return {'completed':True,'value':json.loads(text.getvalue())}
    except OSError as e:return {'completed':False,'winerror':e.winerror or 0}
out['own_window']=observe()
child=r"""import ctypes as C,contextlib,io,json
k=C.WinDLL('kernel32.dll',use_last_error=True);k.GetCurrentProcess.restype=C.c_void_p
k.GetProcessMitigationPolicy.argtypes=[C.c_void_p,C.c_int,C.c_void_p,C.c_size_t]
v=C.c_uint32();q=k.GetProcessMitigationPolicy(k.GetCurrentProcess(),4,C.byref(v),4)
out={'query':bool(q),'win32k_disabled':bool(v.value&1)}
text=io.StringIO()
try:
    with contextlib.redirect_stdout(text):exec(compile(DATA['code'],'<fixed-owned-child>','exec'),{'input':DATA['window']})
    out['observation']={'completed':True,'value':json.loads(text.getvalue())}
except OSError as e:out['observation']={'completed':False,'winerror':e.winerror or 0}
print(json.dumps(out))
"""
child='DATA='+repr({'code':input['observation_code'],'window':input['window']})+'\n'+child
r=subprocess.run([sys.executable,'-I','-S','-B','-c',child],capture_output=True,timeout=3,check=False)
out['child']={'exit_zero':r.returncode==0,'exit_code':r.returncode,'value':json.loads(r.stdout) if r.returncode==0 else None}
b=io.BytesIO()
with zipfile.ZipFile(b,'w') as z:z.writestr('owned.txt','鲸🌊')
with zipfile.ZipFile(io.BytesIO(b.getvalue())) as z:out['zip']=z.read('owned.txt').decode()=='鲸🌊'
out['decimal']=str(decimal.Decimal('0.1')+decimal.Decimal('0.2'))=='0.3'
print(json.dumps(out))
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--fixed-win32k-probe',action='store_true')
    parser.add_argument('--fixed-entry-probe',action='store_true')
    args=parser.parse_args()
    if not args.fixed_win32k_probe and not args.fixed_entry_probe:
        print(json.dumps({'code':'FIXED_WIN32K_PROBE_NOT_REQUESTED'}));return 0
    paths=['tools/audit_mxc_win32k_probe.py','agent_backend/nanobot/mxc_native.py',
        'agent_backend/nanobot/mxc_execution.py','agent_backend/nanobot/mxc_entry.py']
    hashes=lambda:{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
    before=hashes()
    report={'schema':'aura.qa.win32k_creation_probe.v1','fixed_trusted_payload_only':True,
        'production_enabled':False,'original_negative_report_modified':False,'source_sha256':before,
        'outer_queries':[],'native_invocations':[],'desktop_boundary_proven':False}
    parent=ROOT/'agent_workspace';parent.mkdir(exist_ok=True)
    owner=tempfile.TemporaryDirectory(prefix='mxc-win32k-owned-',dir=parent)
    root=Path(owner.name);workspace=root/'workspace';scratch=root/'scratch';workspace.mkdir();scratch.mkdir()
    runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=scratch,
        policy=replace(mxc.MxcPolicy(),timeout_seconds=8,cpu_seconds=3,stdout_limit=16384))
    def factory():
        process=native.WindowsJobProcess();process.k=_MitigationKernel(process.k,process,report['outer_queries']);return process
    def run(*args,**kwargs):
        value=native.run(*args,**kwargs,factory=factory)
        report['native_invocations'].append({'code':value.code,'exit_code':value.exit_code,
            'tree_reaped':value.tree_reaped,'resources_closed':value.resources_closed,
            'limits_verified':value.limits_verified,'resumed':value.resumed,
            'stdout_bytes':len(value.stdout),'stderr_bytes':len(value.stderr)})
        return value
    if not args.fixed_entry_probe:runner._native_run=run
    window=None
    try:
        scope=HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),
            grant_revision=hashlib.sha256(b'fixed-win32k-probe').hexdigest(),is_current=lambda:True)
        if args.fixed_entry_probe:
            title='AURA_QA_WIN32K_'+secrets.token_hex(16)
            window=_HiddenWindow(title);window.start()
            title_hash=hashlib.sha256(title.encode()).hexdigest()
            report['host_read_control']=window.read_hash()==title_hash
            report['host_write_control']=window.set_text(title) and window.read_hash()==title_hash
            data={'observation_code':FIXED_CODE,'window':{'hwnd':window.hwnd,'pid':window.pid,'tid':window.tid,
                'title_sha256':title_hash,'replacement':'AURA_QA_'+secrets.token_hex(16),'clipboard_probe':False}}
            # Same exact native ownership/cleanup; no creation mitigation in
            # this separate experiment. Only the fixed Python sets its flag.
            def ordinary_run(*args,**kwargs):
                value=native.run(*args,**kwargs)
                report['native_invocations'].append({'code':value.code,'exit_code':value.exit_code,
                    'tree_reaped':value.tree_reaped,'resources_closed':value.resources_closed,
                    'limits_verified':value.limits_verified,'resumed':value.resumed})
                return value
            runner._native_run=ordinary_run
            value=runner.execute(ENTRY_EXPERIMENT,data,scope=scope)
        else:value=runner.execute(CALCULATION,scope=scope)
        report['calculation']=json.loads(value.to_json())
        if value.code=='MXC_OK' and not args.fixed_entry_probe:
            title='AURA_QA_WIN32K_'+secrets.token_hex(16)
            window=_HiddenWindow(title);window.start()
            title_hash=hashlib.sha256(title.encode()).hexdigest()
            report['host_read_control']=window.read_hash()==title_hash
            value=runner.execute(FIXED_CODE,{'hwnd':window.hwnd,'pid':window.pid,'tid':window.tid,
                'title_sha256':title_hash,'replacement':'AURA_QA_'+secrets.token_hex(16),'clipboard_probe':False},scope=scope)
            report['owned_window_result']=json.loads(value.to_json())
    finally:
        report['window_closed']=window is None or window.close()
        report['native_pending_cleanup']=native.has_pending_cleanup()
        clean=bool(report['window_closed'] and not report['native_pending_cleanup']
            and all(i['tree_reaped'] and i['resources_closed'] for i in report['native_invocations']))
        if clean:
            owner.cleanup();report['owned_directory_removed']=True
        else:
            _RETAINED.append((owner,runner,window));report['owned_directory_removed']=False
    report['exit_sha256']=hashes();report['source_unchanged']=before==report['exit_sha256']
    destination=ROOT/('artifacts/mxc-win32k-entry-probe.json' if args.fixed_entry_probe else 'artifacts/mxc-win32k-creation-probe.json')
    destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'calculation_code':report['calculation']['code'],
        'calculation_value':report['calculation'].get('value'),'native_invocations':report['native_invocations'],
        'native_pending_cleanup':report['native_pending_cleanup'],'source_unchanged':report['source_unchanged']}))
    return 0


if __name__=='__main__':raise SystemExit(main())
