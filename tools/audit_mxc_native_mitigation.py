"""Compile one fixed no-CRT C canary with a pinned owned compiler, then probe.

No compiler/helper runs without the explicit flag and an independently
verified compiler hash. Nothing here enables production or accepts model code.
"""
from __future__ import annotations
from dataclasses import replace
import hashlib
import inspect
import json
import os
from pathlib import Path
import secrets
import struct
import sys
import tempfile
import threading
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.audit_mxc_desktop_boundary import _HiddenWindow,FIXED_CODE
from tools.audit_mxc_python_mitigation import LAUNCH
from agent_backend.nanobot import mxc_execution as mxc,mxc_native as native
from agent_backend.nanobot.container_execution import AuthorizedInput,HostExecutionScope
from agent_backend.nanobot.safe_files import WorkspaceFiles

COMPILER=Path('E:/Tools/AURA/zig-0.14.1-native-qa/zig-x86_64-windows-0.14.1/zig.exe')
# Official archive SHA and this exact extracted executable were rechecked in
# artifacts/native-compiler-qa-version-recheck.json. The earlier failed version
# acquisition report is retained; its original failure cause is unknown.
COMPILER_SHA256='1ddf230367e07738c4a769eae66c1db7469e37bd520e81c86356453d0db2b9fd'
SOURCE=ROOT/'tools/fixtures/mxc_win32k_probe.c'
_RETAINED=[]


def fixed_python_definition():
    """Encode the fixed host pin as one quote-free UTF-16 compiler argv item."""
    executable = str(mxc.PYTHON_ROOT / 'python.exe')
    if any(char in executable for char in ('\x00', '\r', '\n')):
        raise ValueError('INVALID_FIXED_QA_PYTHON_PATH')
    encoded = executable.encode('utf-16-le')
    units = [int.from_bytes(encoded[index:index + 2], 'little') for index in range(0, len(encoded), 2)]
    return '-DAURA_QA_PYTHON_EXE={' + ','.join(str(unit) for unit in (*units, 0)) + '}'


BITS=('self_query_ok','self_disabled','zero_clear_returned','after_query_ok','after_disabled',
    'attribute_ok','child_created','child_query_ok','child_disabled','child_resumed','child_signaled',
    'child_exit_zero','child_thread_closed','child_root_closed','input_valid')
IMPORTS=frozenset(('GetCommandLineW','GetCurrentProcess','GetProcessMitigationPolicy',
    'SetProcessMitigationPolicy','SetLastError','GetLastError','InitializeProcThreadAttributeList',
    'UpdateProcThreadAttribute','DeleteProcThreadAttributeList','CreateProcessW','ResumeThread',
    'WaitForSingleObject','GetExitCodeProcess','TerminateProcess','CloseHandle','ExitProcess',
    'LoadLibraryW','GetProcAddress','FreeLibrary','GetEnvironmentVariableW','CreateFileW','WriteFile'))


def decode_observation(raw,nonce):
    if type(raw) is not bytes or len(raw)!=64 or type(nonce) is not int or not 1<=nonce<=0xffffffff:
        raise ValueError('FIXED_NATIVE_INVALID_OBSERVATION')
    values=struct.unpack('<16I',raw)
    if values[:3]!=(0x41555241,1,nonce) or values[15]!=0 or any(values[i] not in (0,1) for i in (3,4,5,7,8,9,10,11,13)):
        raise ValueError('FIXED_NATIVE_INVALID_OBSERVATION')
    result=dict(zip(('input_valid','module_load_attempted','module_loaded','module_load_error',
        'fixed_procedures_loaded','exact_target_identity','caption_read_attempted','caption_nonempty',
        'caption_matches_expected','caption_read_error','module_closed','identity_error'),values[3:15]))
    for key in ('input_valid','module_load_attempted','module_loaded','fixed_procedures_loaded','exact_target_identity',
            'caption_read_attempted','caption_nonempty','caption_matches_expected','module_closed'):result[key]=bool(result[key])
    return {**result,'invocation_nonce_matched':True}


def decode_exit(code):
    if type(code) is not int or not 0<=code<=0xffffffff or code&0x8000:
        raise ValueError('FIXED_NATIVE_INVALID_EXIT_BITS')
    return {**{name:bool(code&(1<<i)) for i,name in enumerate(BITS)},'zero_clear_winerror':code>>16}


def pe_imports(raw):
    """Bounded PE32+ import inspection; never maps or executes image bytes."""
    def require(ok):
        if not ok:raise ValueError('FIXED_NATIVE_INVALID_PE')
    def take(offset,size):
        require(type(offset) is int and offset>=0 and 0<=size<=65536 and offset+size<=len(raw))
        return raw[offset:offset+size]
    def u16(at):return struct.unpack('<H',take(at,2))[0]
    def u32(at):return struct.unpack('<I',take(at,4))[0]
    def u64(at):return struct.unpack('<Q',take(at,8))[0]
    require(type(raw) is bytes and 256<=len(raw)<=2*1024*1024 and take(0,2)==b'MZ')
    pe=u32(60);require(take(pe,4)==b'PE\0\0' and u16(pe+4)==0x8664)
    count=u16(pe+6);optional=pe+24;size=u16(pe+20)
    require(1<=count<=16 and size>=240 and u16(optional)==0x20b and u16(optional+68)==3)
    require(u32(optional+108)>=16)
    # No CLR, TLS callbacks, delayed import path or hidden extra DLL loader.
    for directory in (9,13,14):require(u64(optional+112+directory*8)==0)
    sections=[]
    for i in range(count):
        at=optional+size+i*40
        sections.append((u32(at+12),u32(at+8),u32(at+20),u32(at+16)))
    def offset(rva,size=1):
        matches=[p+(rva-v) for v,virtual,p,n in sections if v<=rva and rva+size<=v+n]
        require(len(matches)==1);take(matches[0],size);return matches[0]
    def cstring(rva,maximum=256):
        at=offset(rva);value=take(at,min(maximum,len(raw)-at));end=value.find(b'\0')
        require(0<end<maximum)
        try:return value[:end].decode('ascii','strict')
        except UnicodeError:raise ValueError('FIXED_NATIVE_INVALID_PE') from None
    rva,length=struct.unpack('<II',take(optional+120,8));require(rva and 20<=length<=4096)
    dlls={};terminated=False
    for i in range(min(32,length//20)):
        row=take(offset(rva+i*20,20),20)
        if row==b'\0'*20:terminated=True;break
        original,_,_,name,thunk=struct.unpack('<IIIII',row)
        dll=cstring(name).casefold();require(dll not in dlls)
        names=[]
        for j in range(128):
            entry=u64(offset((original or thunk)+j*8,8))
            if not entry:break
            require(not entry&(1<<63));names.append(cstring(entry+2))
        else:raise ValueError('FIXED_NATIVE_INVALID_PE')
        require(names and len(set(names))==len(names));dlls[dll]=sorted(names)
    require(terminated and set(dlls)=={'kernel32.dll'} and set(dlls['kernel32.dll'])<=IMPORTS)
    require('ExitProcess' in dlls['kernel32.dll'])
    return dlls


CHILD_PREFIX=r'''import decimal,io,json,pathlib,subprocess,sys,zipfile
out={'decimal':str(decimal.Decimal('0.1')+decimal.Decimal('0.2'))=='0.3'}
buffer=io.BytesIO()
with zipfile.ZipFile(buffer,'w') as z:z.writestr('owned.txt','鲸🌊')
with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as z:out['zip']=z.read('owned.txt').decode()=='鲸🌊'
try:
 import ctypes as C,contextlib,hashlib
 out['ctypes_imported']=True
 k=C.WinDLL('kernel32.dll',use_last_error=True);k.GetCurrentProcess.restype=C.c_void_p
 k.GetProcessMitigationPolicy.argtypes=[C.c_void_p,C.c_int,C.c_void_p,C.c_size_t]
 v=C.c_uint32();q=k.GetProcessMitigationPolicy(k.GetCurrentProcess(),4,C.byref(v),4)
 out['mitigation_query']=bool(q);out['win32k_disabled']=bool(v.value&1)
 text=io.StringIO()
 with contextlib.redirect_stdout(text):exec(compile(DATA['observe'],'<fixed-owned-window>','exec'),{'input':DATA['window']})
 out['observation']=json.loads(text.getvalue())
except ImportError:out['ctypes_imported']=False
except OSError as error:out['winerror']=error.winerror or 0
r=subprocess.run([sys.executable,'-I','-S','-B','-c','from decimal import Decimal;assert Decimal("0.1")+Decimal("0.2")==Decimal("0.3")'],capture_output=True,timeout=2,check=False)
out['ordinary_subprocess_exit_zero']=r.returncode==0
pathlib.Path('fixed-grandchild.json').write_text(json.dumps(out),encoding='utf-8')
'''


def main():
    arguments=sys.argv[1:]
    if not arguments or arguments[0]!='--fixed-native-mitigation':
        print(json.dumps({'code':'FIXED_NATIVE_MITIGATION_NOT_REQUESTED'}));return 0
    if not COMPILER_SHA256 or not COMPILER.is_file() or hashlib.sha256(COMPILER.read_bytes()).hexdigest()!=COMPILER_SHA256:
        raise ValueError('FIXED_NATIVE_COMPILER_NOT_VERIFIED')
    if len(arguments)==1:report_output=ROOT/'artifacts/mxc-fixed-native-mitigation-probe.json'
    elif len(arguments)==3 and arguments[1]=='--output':report_output=Path(arguments[2]).resolve()
    else:raise ValueError('FIXED_NATIVE_OUTPUT_ARGUMENTS_INVALID')
    if not report_output.is_relative_to((ROOT/'artifacts').resolve()) or report_output.exists():
        raise ValueError('FIXED_NATIVE_OUTPUT_MUST_BE_NEW_ARTIFACT')
    paths=['tools/audit_mxc_native_mitigation.py','tools/fixtures/mxc_win32k_probe.c',
        'tools/audit_mxc_python_mitigation.py','tools/audit_mxc_desktop_boundary.py',
        'agent_backend/nanobot/mxc_native.py','agent_backend/nanobot/mxc_execution.py','agent_backend/nanobot/mxc_entry.py']
    hashes=lambda:{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
    before=hashes();records=[];compiler_records=[];window=None;runner=None
    report={'schema':'aura.qa.fixed_native_mitigation.v1','production_enabled':False,'fixed_trusted_payload_only':True,
        'source_sha256':before,'compiler_sha256':COMPILER_SHA256,'compiler_version':'0.14.1',
        'native_invocations':records,'trusted_compiler_invocations':compiler_records,'desktop_boundary_proven':False,
        'runtime_scope_expanded':False,'interactive_display_switched':False,'global_acl_changed':False}
    owner=tempfile.TemporaryDirectory(prefix='mxc-native-ui-owned-',dir=ROOT/'agent_workspace');root=Path(owner.name)
    workspace=root/'workspace';scratch=root/'scratch';workspace.mkdir();scratch.mkdir()
    env={'SystemRoot':os.environ['SystemRoot'],'WINDIR':os.environ['SystemRoot'],'PATH':str(mxc.SYSTEM32),
        'TEMP':str(root),'TMP':str(root),'ZIG_GLOBAL_CACHE_DIR':str(root/'global-cache'),
        'ZIG_LOCAL_CACHE_DIR':str(root/'local-cache')}
    compile_policy=replace(mxc.MxcPolicy(),process_memory_mb=1024,job_memory_mb=2048,cpu_rate_percent=75,
        cpu_seconds=90,timeout_seconds=120,stdout_limit=16384,stderr_limit=16384)
    def compile_run(argv):
        r=native.run(argv,environment=env,cwd=root,policy=compile_policy,cancel_event=threading.Event(),watch=lambda:True)
        compiler_records.append({'code':r.code,'exit_code':r.exit_code,'tree_reaped':r.tree_reaped,
            'resources_closed':r.resources_closed,'limits_verified':r.limits_verified,
            'stderr_bytes':len(r.stderr),'stderr_sha256':hashlib.sha256(r.stderr).hexdigest()})
        if r.code!='MXC_EXITED' or r.exit_code!=0 or not r.tree_reaped or not r.resources_closed:
            raise ValueError('FIXED_NATIVE_COMPILER_FAILED')
        return r
    try:
        version=compile_run([str(COMPILER),'version'])
        if version.stdout.strip()!=b'0.14.1':raise ValueError('FIXED_NATIVE_COMPILER_VERSION_MISMATCH')
        for parser in (True,False):
            output=workspace/('parser.exe' if parser else 'fixed.exe')
            args=[str(COMPILER),'cc','-target','x86_64-windows-gnu','-ffreestanding','-fno-stack-protector',
                '-fno-builtin','-nostdlib','-Os','-Wl,--entry=mainCRTStartup','-Wl,--subsystem,console',
                '-isystem',str(COMPILER.parent/'lib/libc/include/any-windows-any'),
                fixed_python_definition(), *( ['-DAURA_CANARY_PARSER_TEST'] if parser else [] ),str(SOURCE),'-lkernel32','-o',str(output)]
            compile_run(args)
            report['parser_imports' if parser else 'helper_imports']=pe_imports(output.read_bytes())
            if parser:
                compile_run([str(output)]);report['actual_c_parser_14_cases_passed']=True
            else:report['helper_sha256']=hashlib.sha256(output.read_bytes()).hexdigest()
        title='AURA_QA_NATIVE_'+secrets.token_hex(16);window=_HiddenWindow(title);window.start()
        title_hash=hashlib.sha256(title.encode()).hexdigest()
        report['host_read_control']=window.read_hash()==title_hash
        control='AURA_QA_NATIVE_CONTROL_'+secrets.token_hex(16)
        report['host_write_control']=window.set_text(control) and window.read_hash()==hashlib.sha256(control.encode()).hexdigest()
        report['host_title_restored']=window.set_text(title) and window.read_hash()==title_hash
        if not all(report[k] for k in ('host_read_control','host_write_control','host_title_restored')):
            raise ValueError('FIXED_NATIVE_HOST_CONTROL_FAILED')
        data={'observe':FIXED_CODE,'window':{'hwnd':window.hwnd,'pid':window.pid,'tid':window.tid,
            'title_sha256':title_hash,'replacement':'AURA_QA_'+secrets.token_hex(16),'clipboard_probe':False}}
        script=workspace/'fixed_child.py';script.write_text('DATA='+repr(data)+'\n'+CHILD_PREFIX,encoding='utf-8')
        inputs=[AuthorizedInput(str(p),hashlib.sha256(p.read_bytes()).hexdigest(),p.name)
            for p in (workspace/'fixed.exe',script)]
        runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=scratch,
            policy=replace(mxc.MxcPolicy(),timeout_seconds=12,cpu_seconds=5,stdout_limit=16384))
        def run(*args,**kwargs):
            r=native.run(*args,**kwargs);records.append({'code':r.code,'exit_code':r.exit_code,
                'tree_reaped':r.tree_reaped,'resources_closed':r.resources_closed,'limits_verified':r.limits_verified});return r
        runner._native_run=run
        scope=HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),
            grant_revision=hashlib.sha256(b'fixed-native-win32k').hexdigest(),is_current=lambda:window.owned())
        target={'hwnd':window.hwnd,'pid':window.pid,'tid':window.tid,'title':title}
        baseline_nonce=secrets.randbelow(0xffffffff)+1;mitigated_nonce=secrets.randbelow(0xffffffff)+1
        code=LAUNCH+'\nimport struct\n'+inspect.getsource(decode_observation)
        code+='TARGET='+repr(target)+'\n'
        code+='''
for key in ('hwnd','pid','tid','title'):os.environ['AURA_QA_OWNED_'+key.upper()]=str(TARGET[key])
def probe_helper(policy,nonce):
 os.environ['AURA_QA_OBSERVATION_NONCE']=str(nonce)
 observed='owned-native-observe.bin'
 if os.path.lexists(observed):raise ValueError('FIXED_NATIVE_STALE_OBSERVATION')
 helper=launch('',policy,os.path.join(INPUT_DIR,'fixed_child.py'),os.path.join(INPUT_DIR,'fixed.exe'),[])
 with open(observed,'rb') as stream:raw=stream.read(65)
 observation=decode_observation(raw,nonce)
 os.remove(observed)
 return {'helper':helper,'observation':observation}
'''
        code+='out={"baseline":probe_helper(0,'+str(baseline_nonce)+'),"mitigated":probe_helper(1<<28,'+str(mitigated_nonce)+')}\n'
        code+='p="fixed-grandchild.json"\nout["grandchild"]=json.loads(open(p,encoding="utf-8").read(16384)) if os.path.isfile(p) else None\nprint(json.dumps(out))\n'
        result=runner.execute(code,scope=scope,authorized_inputs=inputs);report['result']=json.loads(result.to_json())
        if result.code=='MXC_OK' and type(result.value) is dict:
            helper=result.value.get('mitigated',{}).get('helper',{})
            if helper.get('root_signaled') and helper.get('handles_closed') and type(helper.get('exit_code')) is int:
                report['native_helper_flags']=decode_exit(helper['exit_code'])
            report['host_final_caption_unchanged']=window.read_hash()==title_hash
    except ValueError as error:
        text=str(error);report['failure_code']=text if text.startswith('FIXED_NATIVE_') and len(text)<96 else 'FIXED_NATIVE_PROBE_FAILED'
    finally:
        report['window_closed']=window is None or window.close()
        report['native_pending_cleanup']=native.has_pending_cleanup()
        clean=report['window_closed'] and not native.has_pending_cleanup() and all(r['tree_reaped'] and r['resources_closed'] for r in [*records,*compiler_records])
        if clean:owner.cleanup();report['owned_directory_removed']=True
        else:
            owner._finalizer.detach();_RETAINED.append((owner,runner,window));report['owned_directory_removed']=False
    report['exit_sha256']=hashes();report['source_unchanged']=before==report['exit_sha256']
    report['compiler_sha256_after']=hashlib.sha256(COMPILER.read_bytes()).hexdigest()
    report['compiler_unchanged']=report['compiler_sha256_after']==COMPILER_SHA256
    value=report.get('result',{}).get('value',{})
    positive=value.get('baseline',{}).get('observation',{}) if type(value) is dict else {}
    negative=value.get('mitigated',{}).get('observation',{}) if type(value) is dict else {}
    flags=report.get('native_helper_flags',{})
    report['fixed_owned_caption_path_blocked']=bool(report.get('source_unchanged') and report['compiler_unchanged']
        and report.get('owned_directory_removed') and not report['native_pending_cleanup']
        and report.get('host_final_caption_unchanged') and positive.get('input_valid') is True
        and positive.get('exact_target_identity') is True and positive.get('caption_matches_expected') is True
        and positive.get('module_closed') is True and negative.get('input_valid') is True
        and negative.get('module_load_attempted') is True and negative.get('module_loaded') is False
        and negative.get('module_load_error')==1114 and negative.get('caption_read_attempted') is False
        and flags.get('self_disabled') is True and flags.get('after_disabled') is True
        and flags.get('zero_clear_returned') is False and flags.get('child_disabled') is True
        and flags.get('child_signaled') is True and flags.get('child_root_closed') is True
        and flags.get('child_thread_closed') is True)
    with report_output.open('x',encoding='utf-8') as stream:stream.write(json.dumps(report,indent=2))
    print(json.dumps(report));return 0


if __name__=='__main__':raise SystemExit(main())
