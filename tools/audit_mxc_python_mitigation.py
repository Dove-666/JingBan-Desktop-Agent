"""Known nested-Python mitigation probe only; no production enable/config."""
from __future__ import annotations
import ctypes as C
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.audit_mxc_desktop_boundary import _HiddenWindow,FIXED_CODE
from agent_backend.nanobot import mxc_execution as mxc,mxc_native as native
from agent_backend.nanobot.container_execution import HostExecutionScope
from agent_backend.nanobot.safe_files import WorkspaceFiles
_RETAINED=[]

LAUNCH=r'''import ctypes as C,json,os,subprocess,sys
class SI(C.Structure):
    _fields_=[('size',C.c_uint32),('reserved',C.c_wchar_p),('desktop',C.c_wchar_p),('title',C.c_wchar_p),('x',C.c_uint32),('y',C.c_uint32),('width',C.c_uint32),('height',C.c_uint32),('chars_x',C.c_uint32),('chars_y',C.c_uint32),('fill',C.c_uint32),('flags',C.c_uint32),('show',C.c_uint16),('reserved_size',C.c_uint16),('reserved_bytes',C.c_void_p),('stdin',C.c_void_p),('stdout',C.c_void_p),('stderr',C.c_void_p)]
class SX(C.Structure):_fields_=[('base',SI),('attrs',C.c_void_p)]
class PI(C.Structure):_fields_=[('process',C.c_void_p),('thread',C.c_void_p),('pid',C.c_uint32),('tid',C.c_uint32)]
k=C.WinDLL('kernel32.dll',use_last_error=True)
for name,args,result in [
    ('GetCurrentProcess',[],C.c_void_p),
    ('GetProcessMitigationPolicy',[C.c_void_p,C.c_int,C.c_void_p,C.c_size_t],C.c_int),
    ('SetProcessMitigationPolicy',[C.c_int,C.c_void_p,C.c_size_t],C.c_int),
    ('InitializeProcThreadAttributeList',[C.c_void_p,C.c_uint32,C.c_uint32,C.POINTER(C.c_size_t)],C.c_int),
    ('UpdateProcThreadAttribute',[C.c_void_p,C.c_uint32,C.c_size_t,C.c_void_p,C.c_size_t,C.c_void_p,C.c_void_p],C.c_int),
    ('DeleteProcThreadAttributeList',[C.c_void_p],None),
    ('CreateProcessW',[C.c_wchar_p,C.c_wchar_p,C.c_void_p,C.c_void_p,C.c_int,C.c_uint32,C.c_void_p,C.c_wchar_p,C.POINTER(SX),C.POINTER(PI)],C.c_int),
    ('WaitForSingleObject',[C.c_void_p,C.c_uint32],C.c_uint32),
    ('GetExitCodeProcess',[C.c_void_p,C.POINTER(C.c_uint32)],C.c_int),
    ('TerminateProcess',[C.c_void_p,C.c_uint32],C.c_int),
    ('ResumeThread',[C.c_void_p],C.c_uint32),
    ('CloseHandle',[C.c_void_p],C.c_int)]:
    f=getattr(k,name);f.argtypes=args;f.restype=result
def query():
    v=C.c_uint32();C.set_last_error(0);q=k.GetProcessMitigationPolicy(k.GetCurrentProcess(),4,C.byref(v),4)
    return {'query':bool(q),'disabled':bool(v.value&1),'error':0 if q else C.get_last_error()}
def launch(source,mitigation,output_name,executable=None,arguments=None):
    # All source and file names are fixed trusted experiment data.
    path=os.path.abspath(output_name)
    source=('import json\ntry:\n exec('+repr(source)+',globals())\n'
        +'except BaseException as error:\n out={"fixed_probe_error":type(error).__name__ if type(error).__name__ in ("OSError","RuntimeError","NameError","ValueError","SyntaxError","TypeError","AttributeError","KeyError","UnboundLocalError","FileNotFoundError","PermissionError","OverflowError","ImportError","ModuleNotFoundError") else "OTHER","winerror":getattr(error,"winerror",0) or 0}\n'
        +'open('+repr(path)+',"w",encoding="utf-8").write(json.dumps(out))\n')
    executable=executable or sys.executable
    argv=[executable,*(arguments or []),path] if arguments is not None else [executable,'-I','-S','-B','-c',source]
    command=C.create_unicode_buffer(subprocess.list2cmdline(argv))
    size=C.c_size_t();k.InitializeProcThreadAttributeList(None,1,0,C.byref(size));storage=C.create_string_buffer(size.value)
    if not k.InitializeProcThreadAttributeList(storage,1,0,C.byref(size)):raise RuntimeError('FIXED_ATTR_INIT')
    info=PI();outcome={'created':False,'terminated':False,'root_signaled':False,'handles_closed':False}
    try:
        policy=C.c_uint64(mitigation)
        if not k.UpdateProcThreadAttribute(storage,0,0x00020007,C.byref(policy),8,None,None):raise RuntimeError('FIXED_ATTR_POLICY')
        startup=SX();startup.base.size=C.sizeof(startup);startup.attrs=C.cast(storage,C.c_void_p)
        C.set_last_error(0);ok=k.CreateProcessW(executable,command,None,None,False,0x4|0x80000|0x08000000,None,None,C.byref(startup),C.byref(info))
        outcome['created']=bool(ok);outcome['create_error']=0 if ok else C.get_last_error()
        if ok:
            v=C.c_uint32();q=k.GetProcessMitigationPolicy(info.process,4,C.byref(v),4)
            outcome['query_before_resume']=bool(q);outcome['win32k_disabled']=bool(v.value&1)
            if not q or k.ResumeThread(info.thread)!=1:raise RuntimeError('FIXED_QUERY_RESUME')
            if k.WaitForSingleObject(info.process,3000)!=0:
                outcome['terminated']=bool(k.TerminateProcess(info.process,79))
            outcome['root_signaled']=k.WaitForSingleObject(info.process,2000)==0
            rc=C.c_uint32();q=k.GetExitCodeProcess(info.process,C.byref(rc))
            outcome['exit_code']=rc.value if q else None
            if outcome['root_signaled'] and q and rc.value==0:
                outcome['value']=json.loads(open(path,encoding='utf-8').read())
    finally:
        k.DeleteProcThreadAttributeList(storage)
        values=[]
        for h in [info.thread,info.process]:
            if h:values.append(bool(k.CloseHandle(h)))
        outcome['handles_closed']=all(values)
    return outcome
'''

OBSERVE=r'''import contextlib,io
out={'before':query()}
v=C.c_uint32(0);C.set_last_error(0);ok=k.SetProcessMitigationPolicy(4,C.byref(v),4)
out['clear']={'returned':bool(ok),'error':0 if ok else C.get_last_error()};out['after_clear']=query()
text=io.StringIO()
try:
    with contextlib.redirect_stdout(text):exec(compile(DATA['observe'],'<fixed-window>','exec'),{'input':DATA['window']})
    out['observation']={'completed':True,'value':json.loads(text.getvalue())}
except OSError as e:out['observation']={'completed':False,'winerror':e.winerror or 0}
'''


def main():
    if sys.argv[1:]!=['--fixed-python-mitigation']:
        print(json.dumps({'code':'FIXED_PYTHON_MITIGATION_NOT_REQUESTED'}));return 0
    paths=['tools/audit_mxc_python_mitigation.py','agent_backend/nanobot/mxc_native.py',
        'agent_backend/nanobot/mxc_execution.py','agent_backend/nanobot/mxc_entry.py']
    hashes=lambda:{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
    before=hashes();records=[];window=None
    parent=ROOT/'agent_workspace';parent.mkdir(exist_ok=True)
    owner=tempfile.TemporaryDirectory(prefix='mxc-python-mitigation-',dir=parent);root=Path(owner.name)
    workspace=root/'workspace';scratch=root/'scratch';workspace.mkdir();scratch.mkdir()
    runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=scratch,
        policy=replace(mxc.MxcPolicy(),timeout_seconds=12,cpu_seconds=4,stdout_limit=16384))
    def run(*args,**kwargs):
        r=native.run(*args,**kwargs);records.append({'code':r.code,'exit_code':r.exit_code,'tree_reaped':r.tree_reaped,
            'resources_closed':r.resources_closed,'limits_verified':r.limits_verified});return r
    runner._native_run=run
    report={'schema':'aura.qa.nested_python_mitigation.v1','fixed_trusted_payload_only':True,'production_enabled':False,
        'source_sha256':before,'native_invocations':records,'all_desktop_isolation_proven':False}
    try:
        title='AURA_QA_MITIGATION_'+secrets.token_hex(16);window=_HiddenWindow(title);window.start()
        title_hash=hashlib.sha256(title.encode()).hexdigest()
        report['host_positive_read']=window.read_hash()==title_hash
        report['host_positive_write']=window.set_text(title) and window.read_hash()==title_hash
        data={'observe':FIXED_CODE,'window':{'hwnd':window.hwnd,'pid':window.pid,'tid':window.tid,
            'title_sha256':title_hash,'replacement':'AURA_QA_'+secrets.token_hex(16),'clipboard_probe':False}}
        observation='DATA='+repr(data)+'\n'+LAUNCH+'\n'+OBSERVE
        # The mitigated Python tries an ordinary child and an explicit OFF
        # child; no other process or window is inspected.
        child=observation+'\nprint(json.dumps(out))\n'
        middle=('import decimal,io,json,subprocess,sys,zipfile\n'
            +'out={"decimal":str(decimal.Decimal("0.1")+decimal.Decimal("0.2"))=="0.3"}\n'
            +'buf=io.BytesIO()\nwith zipfile.ZipFile(buf,"w") as z:z.writestr("fixed.txt","鲸🌊")\n'
            +'with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:out["zip"]=z.read("fixed.txt").decode()=="鲸🌊"\n'
            +'try:\n import ctypes\n out["ctypes_imported"]=True\nexcept ImportError:\n out["ctypes_imported"]=False\n'
            +'r=subprocess.run([sys.executable,"-I","-S","-B","-c",'+repr(child)+'],capture_output=True,timeout=2,check=False)\n'
            +'out["plain_child"]={"exit_zero":r.returncode==0,"exit_code":r.returncode,"value":json.loads(r.stdout) if r.returncode==0 else None}\n')
        code=LAUNCH+'\nout=launch('+repr(middle)+',1<<28,"mitigated-owned.json")\nprint(json.dumps(out))\n'
        scope=HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),
            grant_revision=hashlib.sha256(b'fixed-nested-mitigation').hexdigest(),is_current=lambda:window.owned())
        result=runner.execute(code,scope=scope);report['result']=json.loads(result.to_json())
    finally:
        report['window_closed']=window is None or window.close()
        report['native_pending_cleanup']=native.has_pending_cleanup()
        clean=report['window_closed'] and not report['native_pending_cleanup'] and all(r['tree_reaped'] and r['resources_closed'] for r in records)
        if clean:owner.cleanup();report['owned_directory_removed']=True
        else:_RETAINED.append((owner,runner,window));report['owned_directory_removed']=False
    report['exit_sha256']=hashes();report['source_unchanged']=before==report['exit_sha256']
    (ROOT/'artifacts/mxc-python-creation-mitigation-probe.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'result':report['result'],'source_unchanged':report['source_unchanged'],
        'native_pending_cleanup':report['native_pending_cleanup'],'owned_directory_removed':report['owned_directory_removed']}))
    return 0


if __name__=='__main__':raise SystemExit(main())
