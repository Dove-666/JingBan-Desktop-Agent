"""Host-approved fixed canaries: own spawned memory and one own credential only.

No process or credential enumeration exists. No real credential or other process
is targeted. Values, PIDs, addresses and target names are never written to the
report. The backend remains disabled; no model code or engine is enabled.
"""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
_RETAINED_OWNERS = []

NATIVE_CREDENTIAL = '''
class FileTime(C.Structure):
    _fields_=[('low',C.c_uint32),('high',C.c_uint32)]
class Credential(C.Structure):
    _fields_=[('Flags',C.c_uint32),('Type',C.c_uint32),('TargetName',C.c_wchar_p),
        ('Comment',C.c_wchar_p),('LastWritten',FileTime),('CredentialBlobSize',C.c_uint32),
        ('CredentialBlob',C.POINTER(C.c_ubyte)),('Persist',C.c_uint32),('AttributeCount',C.c_uint32),
        ('Attributes',C.c_void_p),('TargetAlias',C.c_wchar_p),('UserName',C.c_wchar_p)]
advapi=C.WinDLL('advapi32.dll',use_last_error=True)
advapi.CredReadW.argtypes=[C.c_wchar_p,C.c_uint32,C.c_uint32,C.POINTER(C.POINTER(Credential))]
advapi.CredReadW.restype=C.c_int
advapi.CredFree.argtypes=[C.c_void_p];advapi.CredFree.restype=None
'''

FIXED_MEMORY_CANARY = '''import ctypes,json,os,sys,threading
marker=bytes.fromhex(sys.argv[1]);buffer=ctypes.create_string_buffer(marker)
timer=threading.Timer(15,lambda:os._exit(0));timer.daemon=True;timer.start()
print(json.dumps({'pid':os.getpid(),'address':ctypes.addressof(buffer),'size':len(marker)}),flush=True)
sys.stdin.buffer.read(1)
'''

FIXED_MXC_PROBE = '''import ctypes as C,hashlib,json
''' + NATIVE_CREDENTIAL + '''
kernel=C.WinDLL('kernel32.dll',use_last_error=True)
kernel.OpenProcess.argtypes=[C.c_uint32,C.c_int,C.c_uint32];kernel.OpenProcess.restype=C.c_void_p
kernel.CloseHandle.argtypes=[C.c_void_p];kernel.CloseHandle.restype=C.c_int
kernel.GetProcessTimes.argtypes=[C.c_void_p,*([C.POINTER(FileTime)]*4)];kernel.GetProcessTimes.restype=C.c_int
kernel.ReadProcessMemory.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]
kernel.ReadProcessMemory.restype=C.c_int
out={'open_owned_process':False,'owned_creation_time_matches':False,'read_owned_memory':False,
     'owned_memory_hash_matches':False,'owned_process_handle_closed':True}
handle=kernel.OpenProcess(0x1000|0x0010,False,input['pid'])
out['open_owned_process']=bool(handle);out['open_owned_process_winerror']=0 if handle else C.get_last_error()
if handle:
    try:
        stamps=[FileTime() for _ in range(4)]
        queried=kernel.GetProcessTimes(handle,*[C.byref(item) for item in stamps])
        created=(stamps[0].high<<32)|stamps[0].low
        out['owned_creation_time_matches']=bool(queried and created==input['creation_time'])
        if out['owned_creation_time_matches']:
            buffer=C.create_string_buffer(input['size']);count=C.c_size_t()
            ok=kernel.ReadProcessMemory(handle,C.c_void_p(input['address']),buffer,input['size'],C.byref(count))
            out['read_owned_memory']=bool(ok and count.value==input['size'])
            out['read_owned_memory_winerror']=0 if ok else C.get_last_error()
            if out['read_owned_memory']:
                out['owned_memory_hash_matches']=hashlib.sha256(buffer.raw[:count.value]).hexdigest()==input['memory_sha256']
    finally: out['owned_process_handle_closed']=bool(kernel.CloseHandle(handle))
credential=C.POINTER(Credential)()
ok=advapi.CredReadW(input['credential_target'],1,0,C.byref(credential))
out['read_owned_generic_credential']=bool(ok)
out['read_owned_generic_credential_winerror']=0 if ok else C.get_last_error()
out['owned_credential_hash_matches']=False
if ok:
    try:
        item=credential.contents
        if item.Type==1 and item.CredentialBlobSize==32:
            raw=C.string_at(item.CredentialBlob,item.CredentialBlobSize)
            out['owned_credential_hash_matches']=hashlib.sha256(raw).hexdigest()==input['credential_sha256']
    finally: advapi.CredFree(credential)
print(json.dumps(out,sort_keys=True))
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--fixed-sensitive-canaries',action='store_true')
    args=parser.parse_args()
    if not args.fixed_sensitive_canaries:
        print(json.dumps({'code':'FIXED_CANARIES_NOT_REQUESTED','model_code_enabled':False}));return 0
    from agent_backend.nanobot import mxc_execution as mxc
    from agent_backend.nanobot.container_execution import HostExecutionScope
    from agent_backend.nanobot.safe_files import WorkspaceFiles
    exec(NATIVE_CREDENTIAL,globals())
    advapi.CredWriteW.argtypes=[C.POINTER(Credential),C.c_uint32];advapi.CredWriteW.restype=C.c_int
    advapi.CredDeleteW.argtypes=[C.c_wchar_p,C.c_uint32,C.c_uint32];advapi.CredDeleteW.restype=C.c_int
    parent=ROOT/'agent_workspace';parent.mkdir(exist_ok=True)
    owner=tempfile.TemporaryDirectory(prefix='mxc-sensitive-owned-',dir=parent)
    root=Path(owner.name);workspace=root/'workspace';scratch=root/'scratch'
    workspace.mkdir();scratch.mkdir()
    marker,blob=secrets.token_bytes(32),secrets.token_bytes(32)
    target='AURA_QA_'+uuid4().hex
    process=None;created=False;tree_clean=False;process_clean=False;credential_absent=False
    runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=scratch)
    scope=HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),
        grant_revision=hashlib.sha256(b'owned-sensitive-canary-no-added-grants').hexdigest(),is_current=lambda:True)
    report={'version':1,'fixed_trusted_code':True,'model_code_enabled':False,'model_calls':False,
        'process_enumeration':False,'credential_enumeration':False,'other_process_targeted':False,
        'existing_credential_targeted':False,'engine_or_vm_started':False,'image_pulled':False,
        'owned_memory_sha256':hashlib.sha256(marker).hexdigest(),'owned_credential_sha256':hashlib.sha256(blob).hexdigest(),
        'owned_target_sha256':hashlib.sha256(target.encode()).hexdigest(),
        'backend_default_enabled':mxc.MxcPythonRunner().live_enabled,
        'all_process_memory_isolation_proven':False,'all_credential_isolation_proven':False,
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ('agent_backend/nanobot/mxc_execution.py','agent_backend/nanobot/mxc_native.py','agent_backend/nanobot/mxc_entry.py')},
        'runtime_pins':{'codex_sha256':mxc.CODEX_SHA256,'python_sha256':mxc.PYTHON_SHA256}}
    try:
        if hashlib.sha256((mxc.PYTHON_ROOT/'python.exe').read_bytes()).hexdigest()!=mxc.PYTHON_SHA256:
            raise ValueError('FIXED_CANARY_RUNTIME_PIN_MISMATCH')
        pointer=C.POINTER(Credential)()
        present=advapi.CredReadW(target,1,0,C.byref(pointer))
        if present:
            advapi.CredFree(pointer);raise ValueError('FIXED_CANARY_TARGET_COLLISION')
        if C.get_last_error()!=1168:raise ValueError('FIXED_CANARY_CREDENTIAL_VIEW_UNAVAILABLE')
        raw=(C.c_ubyte*len(blob)).from_buffer_copy(blob)
        item=Credential();item.Type=1;item.TargetName=target;item.Comment='Owned synthetic boundary probe'
        item.CredentialBlobSize=len(blob);item.CredentialBlob=C.cast(raw,C.POINTER(C.c_ubyte))
        item.Persist=1;item.UserName='AURA_QA_Synthetic'
        if not advapi.CredWriteW(C.byref(item),0):raise ValueError('FIXED_CANARY_CREDENTIAL_CREATE_FAILED')
        created=True
        positive=C.POINTER(Credential)()
        if not advapi.CredReadW(target,1,0,C.byref(positive)):
            raise ValueError('FIXED_CANARY_HOST_CREDENTIAL_CONTROL_FAILED')
        try:
            saved=positive.contents
            matches=(saved.Type==1 and saved.CredentialBlobSize==32
                and hashlib.sha256(C.string_at(saved.CredentialBlob,32)).hexdigest()==report['owned_credential_sha256'])
            report['host_owned_credential_positive_control']=matches
            if not matches:raise ValueError('FIXED_CANARY_HOST_CREDENTIAL_CONTROL_FAILED')
        finally:advapi.CredFree(positive)
        env={'SystemRoot':str(mxc.SYSTEM32.parent),'WINDIR':str(mxc.SYSTEM32.parent),
            'PATH':str(mxc.PYTHON_ROOT)+os.pathsep+str(mxc.SYSTEM32)}
        process=subprocess.Popen([str(mxc.PYTHON_ROOT/'python.exe'),'-I','-S','-B','-c',FIXED_MEMORY_CANARY,marker.hex()],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,cwd=root,
            close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW)
        lines=[]
        reader=threading.Thread(target=lambda:lines.append(process.stdout.readline(4096)),daemon=True)
        reader.start();reader.join(3)
        if reader.is_alive() or not lines or process.poll() is not None:raise ValueError('FIXED_CANARY_START_FAILED')
        info=json.loads(lines[0])
        if set(info)!={'pid','address','size'} or info['pid']!=process.pid or info['size']!=32 or type(info['address']) is not int or info['address']<=0:
            raise ValueError('FIXED_CANARY_IDENTITY_INVALID')
        kernel=C.WinDLL('kernel32.dll',use_last_error=True)
        kernel.GetProcessTimes.argtypes=[C.c_void_p,*([C.POINTER(FileTime)]*4)];kernel.GetProcessTimes.restype=C.c_int
        stamps=[FileTime() for _ in range(4)]
        if not kernel.GetProcessTimes(int(process._handle),*[C.byref(value) for value in stamps]):
            raise ValueError('FIXED_CANARY_IDENTITY_UNAVAILABLE')
        info['creation_time']=(stamps[0].high<<32)|stamps[0].low
        kernel.ReadProcessMemory.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]
        kernel.ReadProcessMemory.restype=C.c_int
        positive=C.create_string_buffer(32);count=C.c_size_t()
        matched=bool(kernel.ReadProcessMemory(int(process._handle),C.c_void_p(info['address']),positive,32,C.byref(count))
            and count.value==32 and hashlib.sha256(positive.raw).hexdigest()==report['owned_memory_sha256'])
        report['host_owned_memory_positive_control']=matched
        if not matched:raise ValueError('FIXED_CANARY_HOST_MEMORY_CONTROL_FAILED')
        info.update(credential_target=target,memory_sha256=report['owned_memory_sha256'],credential_sha256=report['owned_credential_sha256'])
        result=runner.execute(FIXED_MXC_PROBE,info,scope=scope)
        report['result']=json.loads(result.to_json())
        tree_clean=result.tree_reaped and result.scratch_removed and result.limits_verified
    except Exception as error:
        code=str(error)
        report['failure_code']=code if code.startswith(('MXC_','FIXED_CANARY_')) and len(code)<128 and code.replace('_','').isalnum() else 'FIXED_CANARY_FAILED'
    finally:
        if process is not None:
            try:
                if process.poll() is None:process.stdin.write(b'\n');process.stdin.flush()
                process.stdin.close();process.wait(timeout=3)
            except Exception:
                try:process.kill();process.wait(timeout=3)
                except Exception:pass
            process_clean=process.poll() is not None
            if process_clean:
                try:process.stdout.close()
                except OSError:process_clean=False
        report['own_canary_process_reaped']=process_clean
        if created:
            report['own_credential_delete_called']=True
            deleted=advapi.CredDeleteW(target,1,0)
            report['own_credential_delete_succeeded']=bool(deleted)
            pointer=C.POINTER(Credential)()
            present=advapi.CredReadW(target,1,0,C.byref(pointer))
            if present:advapi.CredFree(pointer)
            credential_absent=not present and C.get_last_error()==1168
        report['own_credential_absent_verified']=credential_absent
        if tree_clean and process_clean and credential_absent:
            owner.cleanup();report['owned_directory_removed']=True
        else:
            _RETAINED_OWNERS.append((owner,process,runner,scope));report['owned_directory_removed']=False
    (ROOT/'artifacts/mxc-sensitive-fixed-canary.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))
    return 0 if tree_clean and process_clean and credential_absent and report.get('result',{}).get('code')=='MXC_OK' else 1


if __name__=='__main__':raise SystemExit(main())
