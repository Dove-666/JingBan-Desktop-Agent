"""Explicit fixed QA for the production two-stage MXC broker.

No model calls, production opt-in, credential reads or user windows are used.
The compiler and payloads are fixed; the only desktop target is our hidden
synthetic window. Reports are new files, preserving all previous failures.
"""
from __future__ import annotations

import ctypes as C
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import tempfile
import threading
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent_backend.nanobot import mxc_execution as mxc, mxc_native as native
from agent_backend.nanobot.container_execution import AuthorizedInput, HostExecutionScope
from agent_backend.nanobot.safe_files import WorkspaceFiles
from tools.audit_mxc_desktop_boundary import _HiddenWindow
from tools.audit_mxc_native_mitigation import COMPILER, COMPILER_SHA256, SOURCE, decode_exit, decode_observation, pe_imports

_RETAINED = []
PATHS = ('tools/audit_mxc_broker.py', 'tools/audit_mxc_native_mitigation.py',
    'tools/audit_mxc_desktop_boundary.py', 'tools/fixtures/mxc_win32k_probe.c',
    'tools/fixtures/mxc_broker_access_probe.c',
    'agent_backend/nanobot/mxc_native.py', 'agent_backend/nanobot/mxc_execution.py',
    'agent_backend/nanobot/mxc_entry.py', 'agent_backend/nanobot/mxc_worker.py')

CHILD = '''import decimal,io,json,pathlib,subprocess,sys,zipfile
out={'decimal':str(decimal.Decimal('0.1')+decimal.Decimal('0.2'))=='0.3'}
buffer=io.BytesIO()
with zipfile.ZipFile(buffer,'w') as z:z.writestr('owned.txt','鲸🌊')
with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as z:out['zip']=z.read('owned.txt').decode()=='鲸🌊'
try:
 import ctypes
 out['ctypes_imported']=True
except (ImportError,OSError):out['ctypes_imported']=False
r=subprocess.run([sys.executable,'-I','-S','-B','-c','from decimal import Decimal;assert Decimal("0.1")+Decimal("0.2")==Decimal("0.3")'],capture_output=True,timeout=2,check=False)
out['ordinary_subprocess_exit_zero']=r.returncode==0
pathlib.Path('fixed-grandchild.json').write_text(json.dumps(out),encoding='utf-8')
'''

PAYLOAD = '''import json,os,pathlib,subprocess
for key in ('hwnd','pid','tid','title'):os.environ['AURA_QA_OWNED_'+key.upper()]=str(input[key])
os.environ['AURA_QA_OBSERVATION_NONCE']=str(input['nonce'])
r=subprocess.run([os.path.join(INPUT_DIR,'fixed.exe'),os.path.join(INPUT_DIR,'fixed_child.py')],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,check=False)
out={'helper_exit':r.returncode,'observation_hex':pathlib.Path('owned-native-observe.bin').read_bytes().hex(),
 'grandchild':json.loads(pathlib.Path('fixed-grandchild.json').read_text(encoding='utf-8'))}
print(json.dumps(out,ensure_ascii=False))
'''

# A trusted QA-only replacement entry delegates to the unmodified staged
# production broker. It makes one real query while the real child is suspended,
# records only a Boolean and then returns failure. No payload instruction runs.
FAULT_ENTRY = '''import ctypes as C,importlib.util,json,pathlib,sys
p=pathlib.Path(__file__).with_name('production_broker.py')
s=importlib.util.spec_from_file_location('owned_production_broker',p)
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
Original=m.WindowsBroker
class Deny(Original):
 def __init__(self):
  super().__init__();original=self.k.GetProcessMitigationPolicy
  def deny(process,kind,policy,size):
   actual=bool(original(process,kind,policy,size))
   pathlib.Path(__file__).parent.parent.joinpath('task','fault-policy.json').write_text(json.dumps({'actual_query_succeeded':actual,'actual_win32k_on':bool(policy._obj.value&1)}),encoding='utf-8')
   return 0
  self.k.GetProcessMitigationPolicy=deny
m.WindowsBroker=Deny
raise SystemExit(m.main())
'''

STOP_PAYLOAD = '''import json,os,pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-I','-S','-B','-c','import time;time.sleep(15)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
pathlib.Path('stop-ready.json').write_text(json.dumps({'worker':os.getpid(),'child':child.pid}),encoding='utf-8')
time.sleep(15)
child.wait()
print('{}')
'''

ACCESS_PAYLOAD = '''import json,os,pathlib,subprocess
os.environ['AURA_QA_BROKER_PID']=str(os.getppid())
os.environ['AURA_QA_BROKER_NONCE']=str(input['nonce'])
r=subprocess.run([os.path.join(INPUT_DIR,'access.exe')],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3,check=False)
print(json.dumps({'returncode':r.returncode,'observation_hex':pathlib.Path('owned-broker-access.bin').read_bytes().hex()}))
'''


def fingerprint():
    return {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in PATHS}


def projection(result):
    return {key: getattr(result, key) for key in ('code', 'exit_code', 'tree_reaped', 'resources_closed', 'limits_verified', 'resumed')}


def result_projection(result):
    return json.loads(result.to_json())


def require(value, code):
    if not value:
        raise ValueError(code)


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if not arguments or arguments[0] != '--fixed-production-broker':
        print(json.dumps({'code': 'FIXED_PRODUCTION_BROKER_NOT_REQUESTED'}))
        return 0
    fixed_compatibility_only = arguments[1:2] == ['--allow-known-boundary-fixed-only']
    if fixed_compatibility_only:
        arguments.pop(1)
    require(len(arguments) == 3 and arguments[1] == '--output', 'FIXED_BROKER_ARGUMENTS_INVALID')
    output = Path(arguments[2]).resolve()
    require(output.is_relative_to((ROOT/'artifacts').resolve()) and not output.exists(), 'FIXED_BROKER_REPORT_MUST_BE_NEW')
    require(COMPILER.is_file() and hashlib.sha256(COMPILER.read_bytes()).hexdigest() == COMPILER_SHA256,
        'FIXED_BROKER_COMPILER_UNVERIFIED')
    before = fingerprint()
    records, compiler_records = [], []
    report = {'schema': 'aura.qa.production_mxc_broker.v1', 'fixed_trusted_payload_only': True,
        'model_invoked': False, 'production_enabled': False, 'source_sha256': before,
        'allow_known_boundary_fixed_only': fixed_compatibility_only,
        'compiler_sha256': COMPILER_SHA256, 'native_invocations': records, 'compiler_invocations': compiler_records}
    owner = tempfile.TemporaryDirectory(prefix='mxc-broker-fixed-', dir=ROOT/'agent_workspace')
    root = Path(owner.name).resolve()
    workspace, scratch = root/'workspace', root/'scratch'
    workspace.mkdir(); scratch.mkdir()
    window, runner = None, None
    kernel = C.WinDLL('kernel32.dll', use_last_error=True)
    kernel.OpenProcess.argtypes = [C.c_uint32, C.c_int, C.c_uint32]; kernel.OpenProcess.restype = C.c_void_p
    kernel.IsProcessInJob.argtypes = [C.c_void_p, C.c_void_p, C.POINTER(C.c_int)]
    kernel.WaitForSingleObject.argtypes = [C.c_void_p, C.c_uint32]
    kernel.CloseHandle.argtypes = [C.c_void_p]
    retained_handles = []
    runs_closed = True

    def run_native(argv, **options):
        nonlocal runs_closed
        result = native.run(argv, **options)
        records.append(projection(result))
        runs_closed = runs_closed and result.tree_reaped is True and result.resources_closed is True
        return result

    def closed(result):
        require(result.tree_reaped is True and result.resources_closed is True and result.scratch_removed is True
            and result.limits_verified is True and not native.has_pending_cleanup(), 'FIXED_BROKER_RESOURCES_UNCONFIRMED')
        require(list(scratch.iterdir()) == [], 'FIXED_BROKER_SCRATCH_NOT_EMPTY')

    try:
        env = {'SystemRoot': os.environ['SystemRoot'], 'WINDIR': os.environ['SystemRoot'],
            'PATH': str(mxc.SYSTEM32), 'TEMP': str(root), 'TMP': str(root),
            'ZIG_GLOBAL_CACHE_DIR': str(root/'global-cache'), 'ZIG_LOCAL_CACHE_DIR': str(root/'local-cache')}
        compile_policy = replace(mxc.MxcPolicy(), process_memory_mb=1024, job_memory_mb=2048,
            cpu_rate_percent=75, cpu_seconds=90, timeout_seconds=120, stdout_limit=16384, stderr_limit=16384)
        def compile_run(argv):
            nonlocal runs_closed
            r = native.run(argv, environment=env, cwd=root, policy=compile_policy,
                cancel_event=threading.Event(), watch=lambda: True)
            compiler_records.append(projection(r))
            runs_closed = runs_closed and r.tree_reaped is True and r.resources_closed is True
            require(r.code == 'MXC_EXITED' and r.exit_code == 0 and r.tree_reaped and r.resources_closed,
                'FIXED_BROKER_COMPILER_FAILED')
            return r
        require(compile_run([str(COMPILER), 'version']).stdout.strip() == b'0.14.1', 'FIXED_BROKER_COMPILER_VERSION')
        helper = workspace/'fixed.exe'
        compile_run([str(COMPILER), 'cc', '-target', 'x86_64-windows-gnu', '-ffreestanding', '-fno-stack-protector',
            '-fno-builtin', '-nostdlib', '-Os', '-Wl,--entry=mainCRTStartup', '-Wl,--subsystem,console',
            '-isystem', str(COMPILER.parent/'lib/libc/include/any-windows-any'), str(SOURCE), '-lkernel32', '-o', str(helper)])
        report['helper_imports'] = pe_imports(helper.read_bytes())
        report['helper_sha256'] = hashlib.sha256(helper.read_bytes()).hexdigest()
        access_helper = workspace/'access.exe'
        compile_run([str(COMPILER), 'cc', '-target', 'x86_64-windows-gnu', '-ffreestanding', '-fno-stack-protector',
            '-fno-builtin', '-nostdlib', '-Os', '-Wl,--entry=mainCRTStartup', '-Wl,--subsystem,console',
            '-isystem', str(COMPILER.parent/'lib/libc/include/any-windows-any'),
            str(ROOT/'tools/fixtures/mxc_broker_access_probe.c'), '-lkernel32', '-o', str(access_helper)])
        report['access_helper_sha256'] = hashlib.sha256(access_helper.read_bytes()).hexdigest()
        script = workspace/'fixed_child.py'; script.write_text(CHILD, encoding='utf-8')
        title = 'AURA_QA_BROKER_' + secrets.token_hex(16)
        window = _HiddenWindow(title); window.start()
        title_hash = hashlib.sha256(title.encode()).hexdigest()
        require(window.read_hash() == title_hash, 'FIXED_BROKER_WINDOW_READ_CONTROL')
        changed = 'AURA_QA_CONTROL_' + secrets.token_hex(16)
        require(window.set_text(changed) and window.read_hash() == hashlib.sha256(changed.encode()).hexdigest(),
            'FIXED_BROKER_WINDOW_WRITE_CONTROL')
        require(window.set_text(title) and window.read_hash() == title_hash, 'FIXED_BROKER_WINDOW_RESTORE')
        target = {'hwnd': window.hwnd, 'pid': window.pid, 'tid': window.tid, 'title': title}
        control_nonce = secrets.randbelow(0xffffffff)+1
        control_env = dict(env, **{'AURA_QA_OWNED_'+k.upper(): str(v) for k, v in target.items()},
            AURA_QA_OBSERVATION_NONCE=str(control_nonce))
        control = native.run([str(helper), str(script)], environment=control_env, cwd=root,
            policy=replace(mxc.MxcPolicy(), timeout_seconds=5), cancel_event=threading.Event(), watch=lambda: True)
        records.append(projection(control)); runs_closed &= control.tree_reaped and control.resources_closed
        control_observation = decode_observation((root/'owned-native-observe.bin').read_bytes(), control_nonce)
        require(control_observation['caption_matches_expected'] and control_observation['exact_target_identity'],
            'FIXED_BROKER_WINDOW_POSITIVE_CONTROL_FAILED')
        report['host_owned_caption_control'] = control_observation
        scope = HostExecutionScope(str(uuid4()), str(uuid4()), str(uuid4()), WorkspaceFiles(workspace),
            grant_revision=hashlib.sha256(b'fixed-production-broker').hexdigest(), is_current=lambda: window.owned())
        runner = mxc.MxcPythonRunner(live_enabled=True, scratch_parent=scratch,
            policy=replace(mxc.MxcPolicy(), timeout_seconds=12, cpu_seconds=5, stdout_limit=16384))
        runner._native_run = run_native
        inputs = [AuthorizedInput(str(p), hashlib.sha256(p.read_bytes()).hexdigest(), p.name) for p in (helper, script)]

        nonce = secrets.randbelow(0xffffffff)+1
        result = runner.execute(ACCESS_PAYLOAD, {'nonce': nonce}, scope=scope,
            authorized_inputs=[AuthorizedInput(str(access_helper), hashlib.sha256(access_helper.read_bytes()).hexdigest(), 'access.exe')])
        report['broker_access_result'] = result_projection(result)
        closed(result); require(result.code == 'MXC_OK' and result.value['returncode'] == 0,
            'FIXED_BROKER_ACCESS_PROBE_FAILED')
        raw = bytes.fromhex(result.value['observation_hex'])
        require(len(raw) == 80, 'FIXED_BROKER_ACCESS_OBSERVATION_INVALID')
        values = struct.unpack('<20I', raw)
        require(values[:3] == (0x41555241, 1, nonce) and values[3:5] == (1, 1),
            'FIXED_BROKER_ACCESS_OBSERVATION_INVALID')
        observations = {}
        for i, name in enumerate(('create_process', 'vm_write_operation', 'dup_handle', 'terminate', 'write_dac')):
            opened, error, handle_closed = values[5+i*3:8+i*3]
            require(opened in (0, 1) and handle_closed == 1, 'FIXED_BROKER_ACCESS_HANDLE_UNCONFIRMED')
            observations[name] = {'opened': bool(opened), 'winerror': error, 'handle_closed': True}
        report['exact_owned_broker_access'] = observations
        report['security_gate_passed'] = (not any(v['opened'] for v in observations.values())
            and all(v['winerror'] == 5 for v in observations.values()))
        if not report['security_gate_passed']:
            report['security_failure_code'] = 'FIXED_BROKER_ANCESTOR_DANGEROUS_ACCESS'
        require(report['security_gate_passed'] or fixed_compatibility_only, 'FIXED_BROKER_ANCESTOR_DANGEROUS_ACCESS')

        nonce = secrets.randbelow(0xffffffff)+1
        result = runner.execute(PAYLOAD, dict(target, nonce=nonce), scope=scope, authorized_inputs=inputs)
        report['production_result'] = result_projection(result)
        closed(result); require(result.code == 'MXC_OK', 'FIXED_BROKER_PRODUCTION_PAYLOAD_FAILED')
        flags = decode_exit(result.value['helper_exit'])
        observation = decode_observation(bytes.fromhex(result.value['observation_hex']), nonce)
        report['production_helper_flags'] = flags; report['production_observation'] = observation
        require(all(flags[k] for k in ('self_query_ok', 'self_disabled', 'after_query_ok', 'after_disabled',
            'child_created', 'child_query_ok', 'child_disabled', 'child_resumed', 'child_signaled',
            'child_exit_zero', 'child_thread_closed', 'child_root_closed', 'input_valid'))
            and flags['zero_clear_returned'] is False, 'FIXED_BROKER_INHERITANCE_FAILED')
        require(observation['input_valid'] and observation['module_load_attempted'] and not observation['module_loaded']
            and observation['module_load_error'] == 1114 and not observation['caption_read_attempted'],
            'FIXED_BROKER_OWNED_WINDOW_NOT_BLOCKED')
        require(result.value['grandchild'] == {'decimal': True, 'zip': True, 'ctypes_imported': False,
            'ordinary_subprocess_exit_zero': True}, 'FIXED_BROKER_CHILD_COMPUTATION_FAILED')

        fault = {}
        def stage_fault(files, actual_scope):
            require(actual_scope is scope, 'FIXED_BROKER_FAULT_SCOPE')
            entry = files.parent/'entry.py'; entry.rename(entry.with_name('production_broker.py'))
            entry.write_text(FAULT_ENTRY, encoding='utf-8')
        def fault_run(argv, **options):
            r = run_native(argv, **options)
            if 'sandbox' in argv:
                task = Path(argv[-1]).parent.parent/'task'
                fault.update(json.loads((task/'fault-policy.json').read_bytes()))
                fault.update(payload_marker_absent=not (task/'MUST_NOT_RUN').exists(), broker_exit=r.exit_code)
            return r
        runner._native_run = fault_run
        result = runner.execute("from pathlib import Path;Path('MUST_NOT_RUN').write_text('bad');print('{}')",
            scope=scope, stage_inputs=stage_fault)
        closed(result); require(result.code == 'MXC_PAYLOAD_FAILED', 'FIXED_BROKER_FAULT_NOT_REJECTED')
        require(fault == {'actual_query_succeeded': True, 'actual_win32k_on': True,
            'payload_marker_absent': True, 'broker_exit': 78}, 'FIXED_BROKER_PRE_RESUME_FAILURE')
        report['actual_pre_resume_fault'] = fault

        cancel = threading.Event(); stop = {}; processes = []
        def factory():
            process = native.WindowsJobProcess(); processes.append(process); return process
        def stop_run(argv, **options):
            original_watch = options['watch']
            def watch():
                valid = original_watch()
                ready = Path(options['cwd']).parent/'task'/'stop-ready.json'
                if 'sandbox' in argv and ready.is_file() and not retained_handles:
                    data = json.loads(ready.read_bytes())
                    require(set(data) == {'worker', 'child'} and all(type(v) is int and v > 0 for v in data.values()),
                        'FIXED_BROKER_STOP_INVALID_PID')
                    for name in ('worker', 'child'):
                        handle = kernel.OpenProcess(0x1000 | 0x100000, False, data[name])
                        require(handle, 'FIXED_BROKER_STOP_PROCESS_UNAVAILABLE'); retained_handles.append(handle)
                        in_job = C.c_int()
                        require(kernel.IsProcessInJob(handle, processes[-1].job, C.byref(in_job)) and in_job.value,
                            'FIXED_BROKER_STOP_PROCESS_NOT_OWNED')
                        require(kernel.WaitForSingleObject(handle, 0) == 258, 'FIXED_BROKER_STOP_PROCESS_NOT_RUNNING')
                        stop[name+'_running_in_exact_job'] = True
                    cancel.set()
                return valid
            options['watch'] = watch
            r = native.run(argv, factory=factory, **options)
            records.append(projection(r))
            return r
        runner._native_run = stop_run
        result = runner.execute(STOP_PAYLOAD, scope=scope, cancel_event=cancel)
        closed(result); require(result.code == 'MXC_CANCELLED' and len(retained_handles) == 2,
            'FIXED_BROKER_STOP_NOT_OBSERVED')
        for name, handle in zip(('worker', 'child'), retained_handles):
            stop[name+'_exited'] = kernel.WaitForSingleObject(handle, 0) == 0
        require(all(stop.values()), 'FIXED_BROKER_STOP_TREE_SURVIVED')
        report['actual_stop'] = stop

        runner._native_run = run_native
        runner.policy = replace(runner.policy, stdout_limit=128)
        result = runner.execute("print('x'*1000)", scope=scope)
        closed(result); report['actual_output_limit'] = result_projection(result)
        require(result.code == 'MXC_OUTPUT_LIMIT', 'FIXED_BROKER_OUTPUT_LIMIT_NOT_ENFORCED')
        report['host_caption_unchanged'] = window.read_hash() == title_hash
        require(report['host_caption_unchanged'], 'FIXED_BROKER_HOST_CAPTION_CHANGED')
        report['checks_passed'] = True
    except BaseException as error:
        text = str(error)
        report['failure_code'] = text if text.startswith('FIXED_BROKER_') and len(text) < 100 else type(error).__name__
        report['checks_passed'] = False
    finally:
        handles_closed = True
        for handle in retained_handles:
            handles_closed = bool(kernel.CloseHandle(handle)) and handles_closed
        report['observation_handles_closed'] = handles_closed
        report['window_closed'] = window is None or window.close()
        report['native_cleanup_pending'] = native.has_pending_cleanup()
        clean = (handles_closed and report['window_closed'] and runs_closed and not native.has_pending_cleanup()
            and all(r['tree_reaped'] is True and r['resources_closed'] is True for r in records+compiler_records))
        if runner is not None and runner._pending:
            clean = False
        require(root.parent == (ROOT/'agent_workspace').resolve(), 'FIXED_BROKER_CLEANUP_SCOPE_INVALID')
        if clean:
            owner.cleanup(); report['owned_directory_removed'] = True
        else:
            owner._finalizer.detach(); _RETAINED.append((owner, runner, window, retained_handles))
            report['owned_directory_removed'] = False
    report['exit_sha256'] = fingerprint(); report['source_unchanged'] = before == report['exit_sha256']
    report['compiler_unchanged'] = hashlib.sha256(COMPILER.read_bytes()).hexdigest() == COMPILER_SHA256
    report['fixed_compatibility_passed'] = (report.get('checks_passed') is True and report['source_unchanged']
        and report['compiler_unchanged'] and report['owned_directory_removed'] and not report['native_cleanup_pending'])
    report['passed'] = report['fixed_compatibility_passed'] and report.get('security_gate_passed') is True
    with output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'passed': report['passed'], 'fixed_compatibility_passed': report['fixed_compatibility_passed'],
        'security_gate_passed': report.get('security_gate_passed'), 'report': str(output),
        'failure': report.get('failure_code') or report.get('security_failure_code')}, ensure_ascii=False))
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
