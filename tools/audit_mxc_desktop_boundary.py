"""One fixed MXC probe against one owned hidden STATIC window only.

Never enumerate user windows, activate anything, send physical input or inspect
clipboard contents. Clipboard probing only opens then immediately closes the
gate. No production opt-in, model code or OS policy is changed.
"""
from __future__ import annotations

import argparse
import ctypes as C
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
_RETAINED = []

FIXED_CODE = r'''import ctypes as C,hashlib,json
user=C.WinDLL('user32.dll',use_last_error=True)
user.GetWindowThreadProcessId.argtypes=[C.c_void_p,C.POINTER(C.c_uint32)];user.GetWindowThreadProcessId.restype=C.c_uint32
user.GetWindowTextW.argtypes=[C.c_void_p,C.c_wchar_p,C.c_int];user.GetWindowTextW.restype=C.c_int
user.SendMessageTimeoutW.argtypes=[C.c_void_p,C.c_uint32,C.c_size_t,C.c_ssize_t,C.c_uint32,C.c_uint32,C.POINTER(C.c_size_t)]
user.SendMessageTimeoutW.restype=C.c_ssize_t
user.OpenClipboard.argtypes=[C.c_void_p];user.OpenClipboard.restype=C.c_int
user.CloseClipboard.argtypes=[];user.CloseClipboard.restype=C.c_int
out={'target_identity_visible':False,'window_read_attempted':False,'owned_title_hash_match':False,
    'window_read_error':0,'window_set_attempted':False,'window_set_returned':False,'window_set_error':0,
    'window_message_result':False,'clipboard_tested':False,'clipboard_opened':False,
    'clipboard_open_error':0,'clipboard_closed':True,'clipboard_close_error':0}
hwnd=input['hwnd']
assert type(hwnd) is int and hwnd>0 and hwnd!=0xffff
pid=C.c_uint32();C.set_last_error(0)
tid=user.GetWindowThreadProcessId(hwnd,C.byref(pid))
out['target_identity_visible']=bool(tid==input['tid'] and pid.value==input['pid'])
out['target_identity_error']=0 if tid else C.get_last_error()
if out['target_identity_visible']:
    text=C.create_unicode_buffer(128);C.set_last_error(0)
    count=user.GetWindowTextW(hwnd,text,128)
    out['window_read_attempted']=True
    out['window_read_error']=0 if count else C.get_last_error()
    out['owned_title_hash_match']=bool(count and hashlib.sha256(text.value.encode('utf-8')).hexdigest()==input['title_sha256'])
    replacement=C.create_unicode_buffer(input['replacement']);answer=C.c_size_t();C.set_last_error(0)
    sent=user.SendMessageTimeoutW(hwnd,0x000c,0,C.cast(replacement,C.c_void_p).value,0x23,500,C.byref(answer))
    out['window_set_attempted']=True;out['window_set_returned']=bool(sent)
    out['window_set_error']=0 if sent else C.get_last_error();out['window_message_result']=bool(answer.value)
if input['clipboard_probe']:
    out['clipboard_tested']=True;C.set_last_error(0)
    opened=user.OpenClipboard(None);out['clipboard_opened']=bool(opened)
    out['clipboard_open_error']=0 if opened else C.get_last_error()
    if opened:
        C.set_last_error(0)
        closed=user.CloseClipboard();out['clipboard_closed']=bool(closed)
        out['clipboard_close_error']=0 if closed else C.get_last_error()
print(json.dumps(out,sort_keys=True))
'''


def _user_api():
    user=C.WinDLL('user32.dll',use_last_error=True)
    user.CreateWindowExW.argtypes=[C.c_uint32,C.c_wchar_p,C.c_wchar_p,C.c_uint32,*([C.c_int]*4),C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p]
    user.CreateWindowExW.restype=C.c_void_p
    user.DestroyWindow.argtypes=[C.c_void_p];user.DestroyWindow.restype=C.c_int
    user.IsWindowVisible.argtypes=[C.c_void_p];user.IsWindowVisible.restype=C.c_int
    user.GetWindowThreadProcessId.argtypes=[C.c_void_p,C.POINTER(C.c_uint32)];user.GetWindowThreadProcessId.restype=C.c_uint32
    user.GetWindowTextW.argtypes=[C.c_void_p,C.c_wchar_p,C.c_int];user.GetWindowTextW.restype=C.c_int
    user.SendMessageTimeoutW.argtypes=[C.c_void_p,C.c_uint32,C.c_size_t,C.c_ssize_t,C.c_uint32,C.c_uint32,C.POINTER(C.c_size_t)]
    user.SendMessageTimeoutW.restype=C.c_ssize_t
    user.OpenClipboard.argtypes=[C.c_void_p];user.OpenClipboard.restype=C.c_int
    user.CloseClipboard.argtypes=[];user.CloseClipboard.restype=C.c_int
    return user


class _HiddenWindow:
    def __init__(self,title):
        self.user=_user_api();self.title=title
        self.hwnd,self.pid,self.tid=None,None,None
        self.ready,self.stop=threading.Event(),threading.Event()
        self.destroyed,self.failed=False,False
        self.thread=threading.Thread(target=self._run,name='aura-owned-hidden-canary',daemon=False)

    def _run(self):
        class Point(C.Structure):_fields_=[('x',C.c_int32),('y',C.c_int32)]
        class Message(C.Structure):
            _fields_=[('hwnd',C.c_void_p),('message',C.c_uint32),('wparam',C.c_size_t),('lparam',C.c_ssize_t),
                ('time',C.c_uint32),('point',Point),('private',C.c_uint32)]
        kernel=C.WinDLL('kernel32.dll',use_last_error=True)
        kernel.GetCurrentThreadId.restype=C.c_uint32
        kernel.GetModuleHandleW.argtypes=[C.c_wchar_p];kernel.GetModuleHandleW.restype=C.c_void_p
        self.user.PeekMessageW.argtypes=[C.POINTER(Message),C.c_void_p,C.c_uint32,C.c_uint32,C.c_uint32]
        self.user.PeekMessageW.restype=C.c_int
        self.user.TranslateMessage.argtypes=[C.POINTER(Message)];self.user.TranslateMessage.restype=C.c_int
        self.user.DispatchMessageW.argtypes=[C.POINTER(Message)];self.user.DispatchMessageW.restype=C.c_ssize_t
        try:
            self.pid,self.tid=os.getpid(),kernel.GetCurrentThreadId()
            # Hidden, no-activate tool window with a caption. GetWindowText for
            # another process is specifically documented to read captions.
            self.hwnd=self.user.CreateWindowExW(0x08000080,'STATIC',self.title,0x80c00000,
                -32000,-32000,1,1,None,None,kernel.GetModuleHandleW(None),None)
            if not self.hwnd or self.hwnd==0xffff or not self.owned():raise ValueError()
            self.ready.set();deadline=time.monotonic()+15
            while not self.stop.is_set() and time.monotonic()<deadline:
                message=Message()
                for _ in range(64):
                    if not self.user.PeekMessageW(C.byref(message),self.hwnd,0,0,1):break
                    self.user.TranslateMessage(C.byref(message));self.user.DispatchMessageW(C.byref(message))
                self.stop.wait(.005)
        except BaseException:self.failed=True;self.ready.set()
        finally:
            if self.hwnd and self.owned():self.destroyed=bool(self.user.DestroyWindow(self.hwnd))

    def owned(self):
        if not self.hwnd:return False
        pid=C.c_uint32();tid=self.user.GetWindowThreadProcessId(self.hwnd,C.byref(pid))
        return tid==self.tid and pid.value==self.pid

    def start(self):
        self.thread.start()
        if not self.ready.wait(2) or self.failed or not self.owned():raise ValueError('FIXED_DESKTOP_CANARY_CREATE_FAILED')
        if self.user.IsWindowVisible(self.hwnd):raise ValueError('FIXED_DESKTOP_CANARY_NOT_HIDDEN')

    def read_hash(self):
        if not self.owned():raise ValueError('FIXED_DESKTOP_CANARY_IDENTITY_CHANGED')
        text=C.create_unicode_buffer(128)
        if not self.user.GetWindowTextW(self.hwnd,text,128):raise ValueError('FIXED_DESKTOP_HOST_READ_FAILED')
        return hashlib.sha256(text.value.encode('utf-8')).hexdigest()

    def set_text(self,value):
        if not self.owned():raise ValueError('FIXED_DESKTOP_CANARY_IDENTITY_CHANGED')
        text=C.create_unicode_buffer(value);answer=C.c_size_t();C.set_last_error(0)
        return bool(self.user.SendMessageTimeoutW(self.hwnd,0x000c,0,C.cast(text,C.c_void_p).value,0x23,500,C.byref(answer)))

    def close(self):
        self.stop.set();self.thread.join(2)
        return not self.thread.is_alive() and self.destroyed and not self.failed


def _clipboard_gate(user):
    C.set_last_error(0);opened=bool(user.OpenClipboard(None));error=0 if opened else C.get_last_error()
    closed=True;close_error=0
    if opened:
        C.set_last_error(0);closed=bool(user.CloseClipboard());close_error=0 if closed else C.get_last_error()
    return {'opened':opened,'closed':closed,'open_error':error,'close_error':close_error}


def _classification(value,host_window_changed):
    detected=bool(value.get('owned_title_hash_match') or host_window_changed or value.get('clipboard_opened'))
    possible_write=bool(value.get('window_set_returned') and not host_window_changed)
    return {'access_detected':detected,'possible_window_write':possible_write,
        'bootstrap_blocked':detected or possible_write,
        'code':'OWNED_DESKTOP_ACCESS_DETECTED' if detected else 'OWNED_DESKTOP_WRITE_UNVERIFIED' if possible_write
               else 'OWNED_DESKTOP_CANARY_NO_ACCESS_OBSERVED'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--fixed-desktop-canary',action='store_true');args=parser.parse_args()
    if not args.fixed_desktop_canary:
        print(json.dumps({'code':'FIXED_DESKTOP_CANARY_NOT_REQUESTED','model_code_enabled':False}));return 0
    from agent_backend.nanobot import mxc_execution as mxc,mxc_native
    from agent_backend.nanobot.container_execution import HostExecutionScope
    from agent_backend.nanobot.safe_files import WorkspaceFiles
    from desktop_pet.agents.execution_runtime import capture_execution_runtime
    paths=['tools/audit_mxc_desktop_boundary.py','tests/test_mxc_desktop_boundary.py',
        'agent_backend/nanobot/mxc_execution.py','agent_backend/nanobot/mxc_native.py','agent_backend/nanobot/mxc_entry.py']
    def hashes():return {path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in paths}
    source=hashes();title='AURA_QA_HIDDEN_'+secrets.token_hex(16);replacement='AURA_QA_CHANGED_'+secrets.token_hex(16)
    parent=ROOT/'agent_workspace';parent.mkdir(exist_ok=True)
    owner=tempfile.TemporaryDirectory(prefix='mxc-desktop-owned-',dir=parent);root=Path(owner.name)
    workspace=root/'workspace';scratch=root/'scratch';workspace.mkdir();scratch.mkdir()
    window=None;runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=scratch,
        policy=replace(mxc.MxcPolicy(),timeout_seconds=8,cpu_seconds=3,stdout_limit=16384))
    snapshot=capture_execution_runtime();tree_closed=False;window_closed=False
    report={'schema':'aura.qa.mxc_owned_desktop_canary.v1','fixed_trusted_payload_only':True,'model_code_enabled':False,
        'model_calls':False,'machine_enable_file_created':False,'machine_runtime_enabled':bool(snapshot.configuration and snapshot.configuration['enabled']),
        'other_windows_targeted':False,'window_enumeration':False,'physical_input':False,'screen_capture':False,
        'clipboard_contents_read':False,'clipboard_contents_written':False,'clipboard_probe_scope':'open_close_gate_only',
        'all_desktop_isolation_proven':False,'launch_context':'standalone_owned_QA_host_not_full_production_nested_parent',
        'owned_title_sha256':hashlib.sha256(title.encode()).hexdigest(),'owned_replacement_sha256':hashlib.sha256(replacement.encode()).hexdigest(),
        'source_sha256':source,'runtime_pins':{'codex_sha256':mxc.CODEX_SHA256,'codex_version':mxc.CODEX_VERSION,'python_sha256':mxc.PYTHON_SHA256},
        'primary_sources':['https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowtextw',
            'https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw',
            'https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openclipboard']}
    try:
        window=_HiddenWindow(title);window.start()
        report['host_window_hidden']=not bool(window.user.IsWindowVisible(window.hwnd))
        report['host_owned_identity_confirmed']=window.owned()
        read=window.read_hash()==report['owned_title_sha256']
        control='AURA_QA_CONTROL_'+secrets.token_hex(16)
        write=window.set_text(control) and window.read_hash()==hashlib.sha256(control.encode()).hexdigest()
        restored=window.set_text(title) and window.read_hash()==report['owned_title_sha256']
        report['host_read_positive_control'],report['host_write_positive_control'],report['host_title_restored']=read,write,restored
        if not (read and write and restored):raise ValueError('FIXED_DESKTOP_HOST_CONTROL_FAILED')
        before=_clipboard_gate(window.user);report['host_clipboard_before']=before
        if before['opened'] and not before['closed']:raise ValueError('FIXED_DESKTOP_HOST_CLIPBOARD_CLOSE_FAILED')
        scope=HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),
            grant_revision=hashlib.sha256(b'owned-hidden-window-only').hexdigest(),is_current=lambda:window.owned() and not window.stop.is_set())
        result=runner.execute(FIXED_CODE,{'hwnd':window.hwnd,'pid':window.pid,'tid':window.tid,
            'title_sha256':report['owned_title_sha256'],'replacement':replacement,
            'clipboard_probe':before['opened'] and before['closed']},scope=scope)
        report['result']=json.loads(result.to_json())
        tree_closed=(result.code=='MXC_OK' and result.tree_reaped is True and result.resources_closed is True
            and result.scratch_removed is True and result.limits_verified is True and not mxc_native.has_pending_cleanup())
        if not tree_closed:raise ValueError('FIXED_DESKTOP_NATIVE_CLOSURE_UNCONFIRMED')
        changed=window.read_hash()==report['owned_replacement_sha256']
        report['host_canary_title_changed']=changed
        report['host_clipboard_after']=_clipboard_gate(window.user)
        report.update(_classification(result.value,changed))
        report['clipboard_content_access']='NOT_TESTED'
        calibrated=bool(before['opened'] and before['closed'] and report['host_clipboard_after']['opened']
                        and report['host_clipboard_after']['closed'])
        report['clipboard_gate_calibration_complete']=calibrated
        report['clipboard_gate_conclusion']=('OPEN_SUCCEEDED_CONTENT_NOT_TESTED' if result.value.get('clipboard_opened')
            else 'OPEN_DENIED_WITH_HOST_CONTROLS' if calibrated else 'NOT_ESTABLISHED')
        if not calibrated:report['bootstrap_blocked']=True
    except Exception as error:
        text=str(error)
        report['failure_code']=text if text.startswith(('MXC_','FIXED_DESKTOP_')) and len(text)<96 and text.replace('_','').isalnum() else 'FIXED_DESKTOP_CANARY_FAILED'
        report['bootstrap_blocked']=True
    finally:
        if window is not None:
            try:window_closed=window.close()
            except Exception:window_closed=False
        report['owned_window_destroyed_and_thread_joined']=window_closed
        report['native_tree_and_resources_closed']=tree_closed
        report['native_pending_cleanup']=mxc_native.has_pending_cleanup()
        if tree_closed and window_closed:
            try:
                owner.cleanup();report['owned_directory_removed']=True
            except Exception:
                _RETAINED.append((owner,window,runner));report['owned_directory_removed']=False
        else:_RETAINED.append((owner,window,runner));report['owned_directory_removed']=False
    after=hashes();report['source_unchanged']=source==after;report['exit_sha256']=after
    report['probe_execution_valid']=bool(tree_closed and window_closed and report['owned_directory_removed'] and source==after
        and 'failure_code' not in report)
    if not report['probe_execution_valid']:report['bootstrap_blocked']=True
    (ROOT/'artifacts/mxc-owned-desktop-fixed-canary.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({key:report.get(key) for key in ('code','failure_code','probe_execution_valid','access_detected',
        'bootstrap_blocked','host_canary_title_changed','owned_window_destroyed_and_thread_joined','native_tree_and_resources_closed','source_unchanged')}))
    return 2 if report.get('access_detected') else 0 if report['probe_execution_valid'] and not report.get('bootstrap_blocked') else 1


if __name__=='__main__':raise SystemExit(main())
