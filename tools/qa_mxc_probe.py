"""Fixed, synthetic Windows MXC canaries. Never runs model-generated code.

All generated state belongs to one QA folder. The report records scopes, hashes,
booleans and fixed result codes, never raw CLI logs or process environments.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time


REPO = Path(__file__).resolve().parents[1]
QA = REPO / "artifacts" / "qa-mxc-probe-20261103"


def _codex_probe_executable():
    """Use one fixed build beneath the current user's local application data."""
    value = os.environ.get("LOCALAPPDATA")
    local = Path(value) if value and Path(value).is_absolute() else Path.home() / "AppData" / "Local"
    return local / "OpenAI" / "Codex" / "bin" / "22cfb1d302613eae" / "codex.exe"


CODEX = _codex_probe_executable()
RUNTIME = Path(sys.base_prefix).resolve()
PYTHON = RUNTIME / "python.exe"
SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"

CANARY = r'''
import json, os, pathlib, socket, sys
task = pathlib.Path(__file__).resolve().parent
private = task.parent / 'private' / 'marker.txt'
result = {'fixed_code': 'QA_SCOPE_CANARY'}
out = task / 'workspace-result.txt'
out.write_text('QA_WORKSPACE_ONLY', encoding='utf-8')
result['workspace_write'] = out.read_text(encoding='utf-8') == 'QA_WORKSPACE_ONLY'
for op in ('read', 'write'):
    try:
        if op == 'read': private.read_text(encoding='utf-8')
        else: private.write_text('QA_FORBIDDEN_MUTATION', encoding='utf-8')
        result['private_' + op + '_denied'] = False
    except OSError as error:
        result['private_' + op + '_denied'] = isinstance(error, PermissionError) or getattr(error, 'winerror', None) == 5
        result['private_' + op + '_code'] = int(getattr(error, 'winerror', None) or error.errno or 0)
for name, host, port in [('loopback', '127.0.0.1', int(sys.argv[1])), ('external', '1.1.1.1', 443)]:
    try:
        with socket.create_connection((host, port), timeout=1.5): pass
        result[name + '_denied'] = False
    except OSError as error:
        result[name + '_denied'] = True
        result[name + '_code'] = int(getattr(error, 'winerror', None) or error.errno or 0)
print(json.dumps(result, sort_keys=True), flush=True)
'''.lstrip()

CANCEL_CHILD = r'''
import time
end = time.monotonic() + 15
while time.monotonic() < end: time.sleep(.05)
'''.lstrip()

CANCEL_PARENT = r'''
import json, os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-I', '-S', str(pathlib.Path(__file__).with_name('cancel_child.py'))], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(json.dumps({'fixed_code': 'QA_CANCEL_READY', 'payload_pid': os.getpid(), 'child_pid': child.pid}), flush=True)
end = time.monotonic() + 15
while time.monotonic() < end: time.sleep(.05)
child.wait(timeout=1)
'''.lstrip()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hashed_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def toml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def classify(stderr: bytes) -> str:
    value = stderr.decode("utf-8", "replace")
    if re.search(r"(?:unknown|undefined).*(?:permission|profile)|(?:permission|profile).*(?:not found|does not exist|unknown)", value, re.I):
        return "PROFILE_NOT_FOUND"
    if re.search(r"mxc.*(?:unavailable|not available|not supported|unsupported|requires)|(?:unavailable|not supported|unsupported).*mxc", value, re.I):
        return "MXC_UNAVAILABLE"
    if re.search(r"(?:administrator|elevat|UAC|sandbox setup)", value, re.I):
        return "SETUP_OR_ELEVATION_REQUIRED"
    if re.search(r"config|parse|toml|permission profile", value, re.I):
        return "CONFIG_OR_POLICY_REJECTED"
    return "EXECUTION_REJECTED"


def run_bounded(args: list[str], env: dict[str, str], timeout: float = 12) -> tuple[int, bytes, bytes]:
    process = subprocess.Popen(args, env=env, cwd=QA, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate(timeout=3)
        return -999, out[:8192], err[:8192]
    return process.returncode, out[:8192], err[:8192]


def parse_fixed(stdout: bytes, code: str) -> dict[str, object] | None:
    for line in stdout.decode("utf-8", "replace").splitlines():
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("fixed_code") == code:
            return obj
    return None


def process_handle(pid: int) -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    return int(kernel.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, pid) or 0)


def process_active(handle: int) -> bool:
    if not handle:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    code = ctypes.c_uint32()
    return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)


def stop_handle(handle: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    if process_active(handle):
        kernel.TerminateProcess(handle, 77)
        kernel.WaitForSingleObject(handle, 3000)
    stopped = not process_active(handle)
    kernel.CloseHandle(handle)
    return stopped


def owned_descendant_ids(root_pid: int) -> set[int]:
    # Process metadata only; never reads command lines, environments or tokens.
    class Entry(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("usage", ctypes.c_uint32), ("pid", ctypes.c_uint32), ("heap", ctypes.c_size_t), ("module", ctypes.c_uint32), ("threads", ctypes.c_uint32), ("ppid", ctypes.c_uint32), ("priority", ctypes.c_long), ("flags", ctypes.c_uint32), ("exe", ctypes.c_wchar * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(Entry)]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return set()
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        parents: dict[int, int] = {}
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            parents[entry.pid] = entry.ppid
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        owned = {root_pid}
        while True:
            expanded = owned | {pid for pid, parent in parents.items() if parent in owned}
            if expanded == owned:
                return owned - {root_pid}
            owned = expanded
    finally:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(snapshot)


class _JobBasicLimits(ctypes.Structure):
    _fields_ = [("per_process_time", ctypes.c_int64), ("per_job_time", ctypes.c_int64), ("flags", ctypes.c_uint32), ("min_working_set", ctypes.c_size_t), ("max_working_set", ctypes.c_size_t), ("active_process_limit", ctypes.c_uint32), ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32), ("scheduling", ctypes.c_uint32)]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("read_count", "write_count", "other_count", "read_bytes", "write_bytes", "other_bytes")]


class _JobLimits(ctypes.Structure):
    _fields_ = [("basic", _JobBasicLimits), ("io", _JobIoCounters), ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


def create_host_job() -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    handle = int(kernel.CreateJobObjectW(None, None) or 0)
    if not handle:
        raise OSError("QA_JOB_CREATE_FAILED")
    limits = _JobLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, owned processes only.
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        close_job(handle)
        raise OSError("QA_JOB_POLICY_FAILED")
    return handle


def close_job(handle: int) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle(handle)


def in_job(process_handle_value: int, job: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    result = ctypes.c_int()
    return bool(kernel.IsProcessInJob(process_handle_value, job, ctypes.byref(result)) and result.value)


def cancel_probe(command: list[str], env: dict[str, str], *, host_job: bool = False) -> dict[str, object]:
    job = create_host_job() if host_job else 0
    process = subprocess.Popen(command, env=env, cwd=QA, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if job:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if not kernel.AssignProcessToJobObject(job, int(process._handle)):
            process.kill()
            process.communicate(timeout=3)
            close_job(job)
            return {"fixed_code": "HOST_JOB_ASSIGN_FAILED", "payload_ready": False}
    buffers = {"out": bytearray(), "err": bytearray()}
    ready = threading.Event()

    def collect(stream: object, name: str) -> None:
        while True:
            chunk = stream.read(1)
            if not chunk:
                return
            if len(buffers[name]) < 8192:
                buffers[name].extend(chunk)
            if name == "out" and b"\n" in buffers[name] and parse_fixed(bytes(buffers[name]), "QA_CANCEL_READY"):
                ready.set()

    readers = [threading.Thread(target=collect, args=(process.stdout, "out"), daemon=True), threading.Thread(target=collect, args=(process.stderr, "err"), daemon=True)]
    for reader in readers:
        reader.start()
    handles: list[int] = []
    extra_handles: list[int] = []
    result: dict[str, object] = {"fixed_code": "CANCEL_NOT_READY", "payload_ready": False, "host_kill_on_close_job": host_job}
    try:
        if not ready.wait(8):
            return result
        message = parse_fixed(bytes(buffers["out"]), "QA_CANCEL_READY") or {}
        for key in ("payload_pid", "child_pid"):
            pid = message.get(key)
            if type(pid) is not int or pid <= 0:
                return result
            handles.append(process_handle(pid))
        if not all(handles) or not all(process_active(handle) for handle in handles):
            return result
        observed_ids = owned_descendant_ids(process.pid)
        expected_ids = {message["payload_pid"], message["child_pid"]}
        result["owned_process_tree_observed"] = expected_ids <= observed_ids
        if not result["owned_process_tree_observed"]:
            result["fixed_code"] = "OWNED_PROCESS_TREE_NOT_CONFIRMED"
            return result
        extra_handles = [handle for pid in observed_ids - expected_ids if (handle := process_handle(pid))]
        result["payload_ready"] = True
        if job:
            result["payload_in_host_job"] = in_job(handles[0], job)
            result["descendant_in_host_job"] = in_job(handles[1], job)
            if not result["payload_in_host_job"] or not result["descendant_in_host_job"]:
                result["fixed_code"] = "HOST_JOB_CONTAINMENT_NOT_CONFIRMED"
                return result
            close_job(job)
            job = 0
        else:
            process.terminate()
        process.wait(timeout=3)
        deadline = time.monotonic() + 2
        while any(process_active(handle) for handle in handles) and time.monotonic() < deadline:
            time.sleep(.025)
        result.update({"fixed_code": "CANCEL_OBSERVED", "payload_exited": not process_active(handles[0]), "descendant_exited": not process_active(handles[1])})
        result["all_observed_owned_processes_exited"] = not any(process_active(handle) for handle in handles + extra_handles)
        return result
    finally:
        if job:
            close_job(job)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        result["owned_processes_cleaned"] = all([stop_handle(handle) for handle in handles + extra_handles])
        for reader in readers:
            reader.join(timeout=1)
        result["stdout_within_limit"] = len(buffers["out"]) < 8192
        result["stderr_sha256"] = digest(bytes(buffers["err"]))


def main() -> int:
    if sys.platform != "win32" or not CODEX.is_file() or not PYTHON.is_file():
        raise SystemExit("FIXED_RUNTIME_UNAVAILABLE")
    QA.mkdir(parents=True, exist_ok=True)
    task = QA / "task"
    private = QA / "private"
    config_home = QA / "config-home"
    for path in (task, private, config_home, task / "temp", task / "home", task / "appdata", task / "localappdata"):
        path.mkdir(exist_ok=True)
    marker = private / "marker.txt"
    marker.write_text("QA_PRIVATE_SYNTHETIC_CANARY", encoding="utf-8")
    scripts = {"canary.py": CANARY, "cancel_parent.py": CANCEL_PARENT, "cancel_child.py": CANCEL_CHILD}
    for name, source in scripts.items():
        (task / name).write_text(source, encoding="utf-8")
    config = "\n".join([
        'default_permissions = "aura-qa"',
        '[windows]', 'sandbox = "mxc"',
        '[permissions.aura-qa.filesystem]', '":root" = "deny"',
        f'{toml_string(SYSTEM32)} = "read"',
        f'{toml_string(RUNTIME)} = "read"',
        f'{toml_string(task)} = "write"',
        '[permissions.aura-qa.network]', 'enabled = false', '',
    ])
    (config_home / "config.toml").write_text(config, encoding="utf-8")
    env = {"SystemRoot": str(SYSTEM32.parent), "SystemDrive": SYSTEM32.drive, "WINDIR": str(SYSTEM32.parent), "COMSPEC": str(SYSTEM32 / "cmd.exe"), "PATH": os.pathsep.join((str(RUNTIME), str(SYSTEM32))), "CODEX_HOME": str(config_home), "TEMP": str(task / "temp"), "TMP": str(task / "temp"), "USERPROFILE": str(task / "home"), "APPDATA": str(task / "appdata"), "LOCALAPPDATA": str(task / "localappdata"), "PYTHONIOENCODING": "utf-8"}
    base = [str(CODEX), "-c", 'windows.sandbox="mxc"', "sandbox", "-P", "aura-qa", "-C", str(task)]
    report: dict[str, object] = {
        "fixed_code": "PROBE_STARTED", "model_invoked": False, "global_config_modified": False,
        "explicit_mxc": True, "legacy_fallback_allowed": False, "managed_cloud_config_requested": False,
        "scope": {"qa_root": str(QA), "readonly_roots": [str(SYSTEM32), str(RUNTIME)], "readwrite_roots": [str(task)], "default_filesystem": "deny", "network_enabled": False, "minimal_platform_roots_enabled": False},
        "known_residual_boundary": "REGISTRY_READ_CAPABILITY_NOT_CREDENTIAL_ISOLATION",
        "hashes": {"probe_source": hashed_file(Path(__file__)), "codex": hashed_file(CODEX), "python": hashed_file(PYTHON), "config": digest(config.encode()), **{name: digest(source.encode()) for name, source in scripts.items()}},
    }
    report_path = QA / "report.json"

    def save(code: str) -> int:
        generated_tmp = (config_home / "tmp").resolve()
        if generated_tmp.is_relative_to(QA.resolve()) and generated_tmp.name == "tmp" and generated_tmp.is_dir():
            shutil.rmtree(generated_tmp)
        report["fixed_code"] = code
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"fixed_code": code, "scope": str(report_path)}, ensure_ascii=False))
        return 0 if code == "FIXED_CANARIES_PASSED_WITH_HOST_JOB" else 2

    # A deliberately absent profile must be rejected before the payload runs.
    invalid = base.copy()
    invalid[invalid.index("aura-qa")] = "qa-profile-does-not-exist"
    rc, out, err = run_bounded(invalid + ["--", str(PYTHON), "-I", "-S", "-c", "print('QA_UNEXPECTED_EXECUTION')"], env)
    report["invalid_profile_rejection_code"] = classify(err)
    report["invalid_profile_rejected"] = rc != 0 and b"QA_UNEXPECTED_EXECUTION" not in out and report["invalid_profile_rejection_code"] == "PROFILE_NOT_FOUND"
    report["invalid_profile_stderr_sha256"] = digest(err)
    if not report["invalid_profile_rejected"]:
        return save("INVALID_PROFILE_GATE_FAILED")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        accepted, _ = listener.accept()
        accepted.close()
    report["host_loopback_control_connected"] = True
    try:
        try:
            with socket.create_connection(("1.1.1.1", 443), timeout=2):
                pass
            report["host_external_control_connected"] = True
        except OSError:
            report["host_external_control_connected"] = False
        rc, out, err = run_bounded(base + ["--", str(PYTHON), "-I", "-S", str(task / "canary.py"), str(port)], env)
        report["canary_stderr_sha256"] = digest(err)
        payload = parse_fixed(out, "QA_SCOPE_CANARY")
        if rc != 0 or payload is None:
            report["payload_executed"] = payload is not None
            report["rejection_code"] = classify(err)
            return save("MXC_PROBE_BLOCKED")
        report["payload_executed"] = True
        report["canary"] = payload
        report["private_marker_unchanged"] = marker.read_text(encoding="utf-8") == "QA_PRIVATE_SYNTHETIC_CANARY"
        listener.settimeout(.1)
        try:
            accepted, _ = listener.accept()
            accepted.close()
            report["loopback_server_saw_sandbox_connection"] = True
        except TimeoutError:
            report["loopback_server_saw_sandbox_connection"] = False
        passed = all(payload.get(key) is True for key in ("workspace_write", "private_read_denied", "private_write_denied", "loopback_denied", "external_denied")) and report["private_marker_unchanged"] and not report["loopback_server_saw_sandbox_connection"]
        if not passed:
            return save("SCOPE_OR_NETWORK_CANARY_FAILED")
        cancel_command = base + ["--", str(PYTHON), "-I", "-S", str(task / "cancel_parent.py")]
        report["bare_cli_cancel"] = cancel_probe(cancel_command, env)
        report["host_job_cancel"] = cancel_probe(cancel_command, env, host_job=True)
        cancelled = report["host_job_cancel"]
        if not all(cancelled.get(key) is True for key in ("payload_ready", "payload_exited", "descendant_exited", "stdout_within_limit")):
            return save("CANCEL_CANARY_FAILED")
        if not report["host_external_control_connected"]:
            return save("EXTERNAL_NETWORK_CONTROL_UNVERIFIED")
        return save("FIXED_CANARIES_PASSED_WITH_HOST_JOB")
    finally:
        listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
