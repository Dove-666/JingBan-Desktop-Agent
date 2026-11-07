"""One owned noninteractive station/desktop experiment, no global UI changes.

Only a separate helper calls SetProcessWindowStation. The interactive host's
station, thread desktop and every existing object ACL remain untouched.
"""
from __future__ import annotations
import ctypes as C
from dataclasses import replace
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

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.audit_mxc_desktop_boundary import _HiddenWindow,FIXED_CODE
from agent_backend.nanobot import mxc_execution as mxc,mxc_native as native
from agent_backend.nanobot.container_execution import HostExecutionScope
from agent_backend.nanobot.safe_files import WorkspaceFiles
_RETAINED=[]


def _station_helper(station_name,desktop_name):
    user=C.WinDLL('user32.dll',use_last_error=True)
    for name,args,result in [
        ('GetProcessWindowStation',[],C.c_void_p),
        ('CreateWindowStationW',[C.c_wchar_p,C.c_uint32,C.c_uint32,C.c_void_p],C.c_void_p),
        ('SetProcessWindowStation',[C.c_void_p],C.c_int),
        ('CreateDesktopW',[C.c_wchar_p,C.c_wchar_p,C.c_void_p,C.c_uint32,C.c_uint32,C.c_void_p],C.c_void_p),
        ('GetUserObjectInformationW',[C.c_void_p,C.c_int,C.c_void_p,C.c_uint32,C.POINTER(C.c_uint32)],C.c_int),
        ('GetUserObjectSecurity',[C.c_void_p,C.POINTER(C.c_uint32),C.c_void_p,C.c_uint32,C.POINTER(C.c_uint32)],C.c_int),
        ('CloseDesktop',[C.c_void_p],C.c_int),('CloseWindowStation',[C.c_void_p],C.c_int)]:
        f=getattr(user,name);f.argtypes=args;f.restype=result
    old=user.GetProcessWindowStation();station=desktop=None
    result={'created':False,'ready':False,'restored':False,'desktop_closed':True,'station_closed':True,'error':0,
        'explicit_private_dacl':False,'actual_dacl_matches':False,'security_buffers_freed':True}
    adv=C.WinDLL('advapi32.dll',use_last_error=True);kernel=C.WinDLL('kernel32.dll',use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[C.c_wchar_p,C.c_uint32,C.POINTER(C.c_void_p),C.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=C.c_int
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes=[C.c_void_p,C.c_uint32,C.c_uint32,C.POINTER(C.c_void_p),C.c_void_p]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype=C.c_int
    kernel.LocalFree.argtypes=[C.c_void_p];kernel.LocalFree.restype=C.c_void_p
    sd=C.c_void_p();sddl=C.c_void_p()
    try:
        # Protected ACL grants only LocalSystem and the exact object's owner.
        # No Everyone/Users/ALL_APPLICATION_PACKAGES ACE or global ACL edit.
        policy='D:P(A;;GA;;;SY)(A;;GA;;;OW)'
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(policy,1,C.byref(sd),None):
            result['error']=C.get_last_error();return result
        attrs=native.SecurityAttributes(C.sizeof(native.SecurityAttributes),sd,0)
        result['explicit_private_dacl']=True
        C.set_last_error(0)
        station=user.CreateWindowStationW(station_name,1,0x37f,C.byref(attrs))
        if not station:result['error']=C.get_last_error();return result
        result['created']=True
        needed=C.c_uint32();security=C.c_uint32(4)
        user.GetUserObjectSecurity(station,C.byref(security),None,0,C.byref(needed))
        if not 0<needed.value<8192:result['error']=13;return result
        actual=C.create_string_buffer(needed.value)
        if not user.GetUserObjectSecurity(station,C.byref(security),actual,len(actual),C.byref(needed)):
            result['error']=C.get_last_error();return result
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(actual,1,4,C.byref(sddl),None):
            result['error']=C.get_last_error();return result
        result['actual_dacl_matches']=C.wstring_at(sddl)==policy
        if not result['actual_dacl_matches']:result['error']=13;return result
        name=C.create_unicode_buffer(512);size=C.c_uint32()
        if not user.GetUserObjectInformationW(station,2,name,C.sizeof(name),C.byref(size)):
            result['error']=C.get_last_error();return result
        if not user.SetProcessWindowStation(station):result['error']=C.get_last_error();return result
        desktop=user.CreateDesktopW(desktop_name,None,None,0,0x1ff,None)
        if not desktop:result['error']=C.get_last_error();return result
        if not user.SetProcessWindowStation(old):result['error']=C.get_last_error();return result
        result['restored']=True;result['ready']=True
        print(json.dumps({'ready':True,'station_name':name.value,'actual_dacl_matches':True}),flush=True)
        stop=threading.Event()
        def read():sys.stdin.buffer.read(1);stop.set()
        threading.Thread(target=read,daemon=True).start();stop.wait(15)
    finally:
        if station and not result['restored']:result['restored']=bool(user.SetProcessWindowStation(old))
        if desktop:result['desktop_closed']=bool(user.CloseDesktop(desktop))
        if station:result['station_closed']=bool(user.CloseWindowStation(station))
        for memory in (sddl,sd):
            if memory and kernel.LocalFree(memory):result['security_buffers_freed']=False
    return result


CODE=r'''import ctypes as C,contextlib,hashlib,io,json
u=C.WinDLL('user32.dll',use_last_error=True)
u.GetProcessWindowStation.argtypes=[];u.GetProcessWindowStation.restype=C.c_void_p
u.GetUserObjectInformationW.argtypes=[C.c_void_p,C.c_int,C.c_void_p,C.c_uint32,C.POINTER(C.c_uint32)];u.GetUserObjectInformationW.restype=C.c_int
u.OpenWindowStationW.argtypes=[C.c_wchar_p,C.c_int,C.c_uint32];u.OpenWindowStationW.restype=C.c_void_p
u.SetProcessWindowStation.argtypes=[C.c_void_p];u.SetProcessWindowStation.restype=C.c_int
u.CloseWindowStation.argtypes=[C.c_void_p];u.CloseWindowStation.restype=C.c_int
old=u.GetProcessWindowStation();name=C.create_unicode_buffer(256);used=C.c_uint32()
q=u.GetUserObjectInformationW(old,2,name,C.sizeof(name),C.byref(used))
out={'station_query':bool(q),'private_station_matches':bool(q and hashlib.sha256(name.value.encode()).hexdigest()==input['station_sha256'])}
def observe():
    text=io.StringIO()
    with contextlib.redirect_stdout(text):exec(compile(input['observe'],'<fixed-owned-window>','exec'),{'input':input['window']})
    return json.loads(text.getvalue())
out['before_reconnect']=observe()
# No window enumeration or display switch. Only this untrusted process's
# station is reassigned, with a read-only handle; then exactly restored.
C.set_last_error(0);target=u.OpenWindowStationW('WinSta0',False,0x3)
out['interactive_station_read_opened']=bool(target);out['open_error']=0 if target else C.get_last_error()
out['reconnected']=False;out['restored']=True;out['target_handle_closed']=True
if target:
    try:
        C.set_last_error(0);out['reconnected']=bool(u.SetProcessWindowStation(target));out['reconnect_error']=0 if out['reconnected'] else C.get_last_error()
        if out['reconnected']:out['after_reconnect']=observe()
    finally:
        if out['reconnected']:out['restored']=bool(u.SetProcessWindowStation(old))
        out['target_handle_closed']=bool(u.CloseWindowStation(target))
print(json.dumps(out))
'''


class _DesktopKernel:
    def __init__(self,kernel,desktop):self.kernel,self.desktop=kernel,desktop
    def __getattr__(self,name):return getattr(self.kernel,name)
    def CreateProcessW(self,*args):
        startup=args[8]._obj
        if startup.base.desktop is not None:raise native.NativeFailure('FIXED_DESKTOP_ALREADY_SET')
        startup.base.desktop=self.desktop
        return self.kernel.CreateProcessW(*args)


def main():
    if len(sys.argv)==4 and sys.argv[1]=='--owned-station-helper':
        result=_station_helper(None if sys.argv[2]=='--anonymous' else sys.argv[2],sys.argv[3]);print(json.dumps(result),flush=True);return 0
    if sys.argv[1:] not in (['--fixed-private-station'],['--fixed-anonymous-station']):
        print(json.dumps({'code':'FIXED_PRIVATE_STATION_NOT_REQUESTED'}));return 0
    paths=['tools/audit_mxc_private_station.py','agent_backend/nanobot/mxc_native.py',
        'agent_backend/nanobot/mxc_execution.py','agent_backend/nanobot/mxc_entry.py']
    hashes=lambda:{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
    before=hashes();records=[];window=None;helper=None
    report={'schema':'aura.qa.owned_private_station.v1','fixed_trusted_payload_only':True,'production_enabled':False,
        'global_acl_changed':False,'interactive_display_switched':False,'user_windows_enumerated':False,
        'source_sha256':before,'native_invocations':records,'desktop_boundary_proven':False}
    station='AURA_QA_'+secrets.token_hex(16);desktop='owned_'+secrets.token_hex(16)
    anonymous=sys.argv[1:] == ['--fixed-anonymous-station']
    owner=tempfile.TemporaryDirectory(prefix='mxc-station-owned-',dir=ROOT/'agent_workspace');root=Path(owner.name)
    workspace=root/'workspace';scratch=root/'scratch';workspace.mkdir();scratch.mkdir()
    runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=scratch,
        policy=replace(mxc.MxcPolicy(),timeout_seconds=8,cpu_seconds=3,stdout_limit=16384))
    def factory():
        p=native.WindowsJobProcess();p.k=_DesktopKernel(p.k,station+'\\'+desktop);return p
    def run(*args,**kwargs):
        r=native.run(*args,**kwargs,factory=factory);records.append({'code':r.code,'exit_code':r.exit_code,
            'tree_reaped':r.tree_reaped,'resources_closed':r.resources_closed,'limits_verified':r.limits_verified});return r
    runner._native_run=run
    try:
        helper=subprocess.Popen([sys.executable,'-I','-S','-B',str(Path(__file__).resolve()),'--owned-station-helper','--anonymous' if anonymous else station,desktop],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=0x08000000,
            env={'SystemRoot':os.environ['SystemRoot'],'TEMP':str(root),'TMP':str(root)})
        # Owned bounded helper only. Its fixed deadline is 15 seconds.
        first=json.loads(helper.stdout.readline(2048))
        actual_station=first.pop('station_name',None);report['helper_first']=first
        if not first.get('ready'):raise ValueError('FIXED_PRIVATE_STATION_NOT_AVAILABLE')
        if type(actual_station) is not str or not actual_station or '\\' in actual_station:raise ValueError('FIXED_PRIVATE_STATION_NOT_AVAILABLE')
        station=actual_station
        title='AURA_QA_STATION_'+secrets.token_hex(16);window=_HiddenWindow(title);window.start()
        h=hashlib.sha256(title.encode()).hexdigest()
        report['host_read_positive']=window.read_hash()==h
        report['host_write_positive']=window.set_text(title) and window.read_hash()==h
        scope=HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),
            grant_revision=hashlib.sha256(b'fixed-private-station').hexdigest(),is_current=lambda:window.owned())
        data={'station_sha256':hashlib.sha256(station.encode()).hexdigest(),'observe':FIXED_CODE,
            'window':{'hwnd':window.hwnd,'pid':window.pid,'tid':window.tid,'title_sha256':h,
                'replacement':'AURA_QA_'+secrets.token_hex(16),'clipboard_probe':False}}
        result=runner.execute(CODE,data,scope=scope);report['result']=json.loads(result.to_json())
    except ValueError as e:
        report['failure_code']=str(e) if str(e)=='FIXED_PRIVATE_STATION_NOT_AVAILABLE' else 'FIXED_PRIVATE_STATION_FAILED'
    finally:
        report['window_closed']=window is None or window.close()
        if helper is not None:
            helper.stdin.close()
            try:
                helper.wait(17);last=helper.stdout.readline(2048)
                report['helper_closure']=json.loads(last) if last else report.get('helper_first',{})
                report['helper_exit_zero']=helper.returncode==0
            except subprocess.TimeoutExpired:
                helper.kill();helper.wait(2);report['helper_exit_zero']=False
            finally:helper.stdout.close()
        report['native_pending_cleanup']=native.has_pending_cleanup()
        clean=bool(report['window_closed'] and not native.has_pending_cleanup()
            and all(r['tree_reaped'] and r['resources_closed'] for r in records))
        if clean:owner.cleanup();report['owned_directory_removed']=True
        else:_RETAINED.append((owner,runner,window,helper));report['owned_directory_removed']=False
    report['exit_sha256']=hashes();report['source_unchanged']=before==report['exit_sha256']
    destination=ROOT/('artifacts/mxc-owned-private-station-anonymous-probe.json' if anonymous else 'artifacts/mxc-owned-private-station-probe.json')
    destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report));return 0


if __name__=='__main__':raise SystemExit(main())
