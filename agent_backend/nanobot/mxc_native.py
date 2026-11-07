"""Local Win32 launcher for the optional MXC backend. No work on import.

The suspended thread is resumed only after queried job limits and containment
are confirmed. No shell, inherited host environment, or unrelated handles.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from contextlib import nullcontext
import ctypes as C
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time


class NativeFailure(ValueError):
    pass


class BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", C.c_uint32), ("min_ws", C.c_size_t), ("max_ws", C.c_size_t), ("active_limit", C.c_uint32), ("affinity", C.c_size_t), ("priority", C.c_uint32), ("scheduling", C.c_uint32)]


class IoCounters(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", C.c_size_t), ("job_memory", C.c_size_t), ("peak_process_memory", C.c_size_t), ("peak_job_memory", C.c_size_t)]


class CpuRate(C.Structure):
    _fields_ = [("flags", C.c_uint32), ("rate", C.c_uint32)]


class Accounting(C.Structure):
    _fields_ = [("user_time", C.c_int64), ("kernel_time", C.c_int64), ("period_user", C.c_int64), ("period_kernel", C.c_int64), ("faults", C.c_uint32), ("total_processes", C.c_uint32), ("active_processes", C.c_uint32), ("terminated_processes", C.c_uint32)]


class SecurityAttributes(C.Structure):
    _fields_ = [("size", C.c_uint32), ("descriptor", C.c_void_p), ("inherit", C.c_int)]


class StartupInfo(C.Structure):
    _fields_ = [("size", C.c_uint32), ("reserved", C.c_wchar_p), ("desktop", C.c_wchar_p), ("title", C.c_wchar_p), ("x", C.c_uint32), ("y", C.c_uint32), ("width", C.c_uint32), ("height", C.c_uint32), ("chars_x", C.c_uint32), ("chars_y", C.c_uint32), ("fill", C.c_uint32), ("flags", C.c_uint32), ("show", C.c_uint16), ("reserved_size", C.c_uint16), ("reserved_bytes", C.c_void_p), ("stdin", C.c_void_p), ("stdout", C.c_void_p), ("stderr", C.c_void_p)]


class StartupInfoEx(C.Structure):
    _fields_ = [("base", StartupInfo), ("attributes", C.c_void_p)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", C.c_void_p), ("thread", C.c_void_p), ("pid", C.c_uint32), ("tid", C.c_uint32)]


@dataclass(frozen=True)
class NativeResult:
    code: str
    exit_code: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    tree_reaped: bool = True
    limits_verified: bool = False
    resumed: bool = False
    job_user_100ns: int = 0
    peak_job_memory: int = 0
    _cleanup_lease: object = field(default=None, repr=False, compare=False)
    resources_closed: bool = True


_cleanup_lock = threading.RLock()
_execution_lock = threading.RLock()
_pending_cleanup = {}


class _CleanupLease:
    """Non-serializable exact-handle authority, strongly retained on the host."""
    def __init__(self, process, *, tree_reaped=False):
        self._process = process
        self._tree_reaped = tree_reaped
        self._closed = False

    def __repr__(self):
        return "_CleanupLease([host-owned])"

    def __reduce__(self):
        raise TypeError("MXC_CLEANUP_AUTHORITY_NOT_SERIALIZABLE")


def has_pending_cleanup():
    with _cleanup_lock:
        return bool(_pending_cleanup)


def _retain_cleanup(process, *, tree_reaped=False):
    with _cleanup_lock:
        lease = getattr(process, "_cleanup_lease", None)
        if not isinstance(lease, _CleanupLease) or lease._process is not process:
            lease = _CleanupLease(process, tree_reaped=tree_reaped)
            process._cleanup_lease = lease
        lease._tree_reaped = lease._tree_reaped or tree_reaped
        _pending_cleanup[id(lease)] = lease
        return lease


class _StickyResourceOwner:
    """A failed close has lost certainty. Never retry a possibly reused number."""
    _close_unknown = True
    def __init__(self, *owners): self._owners = owners
    def terminate_and_reap(self): return True  # This owner never creates a process.
    def close(self): return False


def retain_failed_close(*owners):
    """Host-only strong ownership of exact wrapper/handle state after failure."""
    return _retain_cleanup(_StickyResourceOwner(*owners), tree_reaped=True)


def reconcile_cleanup(lease):
    with _execution_lock, _cleanup_lock:
        if not isinstance(lease, _CleanupLease) or _pending_cleanup.get(id(lease)) is not lease:
            return False
        if getattr(lease._process, "_close_unknown", False):
            return False
        try:
            if not lease._tree_reaped:
                if lease._process.terminate_and_reap() is not True:
                    return False
                lease._tree_reaped = True
            if lease._process.close() is not True:
                return False
        except Exception:
            return False
        lease._closed = True
        del _pending_cleanup[id(lease)]
        return True


def scan_scratch(root, *, expected_identity, maximum_bytes, maximum_entries):
    """Soft quota only. Hold non-reparse ancestor directories while scanning.

    Denying write/delete sharing also prevents turning a checked directory into
    a junction after the check. A concurrent incompatible directory handle is
    a reason to stop monitoring, never to follow an unstable path.
    """
    if sys.platform != "win32":
        raise NativeFailure("MXC_WINDOWS_REQUIRED")
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p]
    kernel.CreateFileW.restype = C.c_void_p
    kernel.CloseHandle.argtypes = [C.c_void_p]
    kernel.GetFileInformationByHandleEx.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32]
    class AttributeTag(C.Structure):
        _fields_ = [("attributes", C.c_uint32), ("tag", C.c_uint32)]
    used, count = 0, 0
    def visit(path, initial=False):
        nonlocal used, count
        handle = kernel.CreateFileW(str(path), 0x81, 1, None, 3, 0x02200000, None)
        if not handle or handle == C.c_void_p(-1).value:
            raise NativeFailure("MXC_SCRATCH_MONITOR_UNAVAILABLE")
        try:
            attrs = AttributeTag()
            if not kernel.GetFileInformationByHandleEx(handle, 9, C.byref(attrs), C.sizeof(attrs)):
                raise NativeFailure("MXC_SCRATCH_MONITOR_UNAVAILABLE")
            if attrs.attributes & 0x400 or not attrs.attributes & 0x10:
                raise NativeFailure("MXC_SCRATCH_LINK_REJECTED")
            info = path.stat(follow_symlinks=False)
            if initial and (info.st_dev, info.st_ino) != expected_identity:
                raise NativeFailure("MXC_SCRATCH_CHANGED")
            with os.scandir(path) as entries:
                for child in entries:
                    info = child.stat(follow_symlinks=False)
                    count += 1
                    if count > maximum_entries:
                        raise NativeFailure("MXC_SCRATCH_LIMIT")
                    if info.st_file_attributes & 0x400 or stat.S_ISLNK(info.st_mode):
                        raise NativeFailure("MXC_SCRATCH_LINK_REJECTED")
                    if stat.S_ISDIR(info.st_mode):
                        visit(Path(child.path))
                    else:
                        used += info.st_size
                    if used > maximum_bytes:
                        raise NativeFailure("MXC_SCRATCH_LIMIT")
        except OSError:
            raise NativeFailure("MXC_SCRATCH_MONITOR_UNAVAILABLE") from None
        finally:
            # Close failure is not proof of release; preserve the original
            # API object and exact handle even if a later scalar is reusable.
            try: closed = kernel.CloseHandle(handle) != 0
            except BaseException: closed = False
            if not closed:
                lease = retain_failed_close(kernel, handle)
                failure = NativeFailure("MXC_RESOURCE_CLOSE_UNCONFIRMED")
                failure._cleanup_lease = lease
                if sys.exc_info()[0] is None: raise failure
    visit(Path(root), True)
    return True


class WindowsJobProcess:
    """Owned process tree, injectable only by host-side tests."""
    FLAGS = 0x2000 | 0x0004 | 0x0008 | 0x0100 | 0x0200

    def __init__(self):
        if sys.platform != "win32":
            raise NativeFailure("MXC_WINDOWS_REQUIRED")
        self.k = C.WinDLL("kernel32", use_last_error=True)
        specs = {
            "CloseHandle": ([C.c_void_p], C.c_int),
            "CreateJobObjectW": ([C.c_void_p, C.c_wchar_p], C.c_void_p),
            "SetInformationJobObject": ([C.c_void_p, C.c_int, C.c_void_p, C.c_uint32], C.c_int),
            "QueryInformationJobObject": ([C.c_void_p, C.c_int, C.c_void_p, C.c_uint32, C.c_void_p], C.c_int),
            "TerminateJobObject": ([C.c_void_p, C.c_uint32], C.c_int),
            "AssignProcessToJobObject": ([C.c_void_p, C.c_void_p], C.c_int),
            "IsProcessInJob": ([C.c_void_p, C.c_void_p, C.POINTER(C.c_int)], C.c_int),
            "ResumeThread": ([C.c_void_p], C.c_uint32),
            "WaitForSingleObject": ([C.c_void_p, C.c_uint32], C.c_uint32),
            "GetExitCodeProcess": ([C.c_void_p, C.POINTER(C.c_uint32)], C.c_int),
            "TerminateProcess": ([C.c_void_p, C.c_uint32], C.c_int),
            "CreatePipe": ([C.POINTER(C.c_void_p), C.POINTER(C.c_void_p), C.POINTER(SecurityAttributes), C.c_uint32], C.c_int),
            "SetHandleInformation": ([C.c_void_p, C.c_uint32, C.c_uint32], C.c_int),
            "CreateFileW": ([C.c_wchar_p, C.c_uint32, C.c_uint32, C.POINTER(SecurityAttributes), C.c_uint32, C.c_uint32, C.c_void_p], C.c_void_p),
            "PeekNamedPipe": ([C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32), C.c_void_p], C.c_int),
            "ReadFile": ([C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p], C.c_int),
            "InitializeProcThreadAttributeList": ([C.c_void_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_size_t)], C.c_int),
            "UpdateProcThreadAttribute": ([C.c_void_p, C.c_uint32, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p], C.c_int),
            "DeleteProcThreadAttributeList": ([C.c_void_p], None),
            "CreateProcessW": ([C.c_wchar_p, C.c_wchar_p, C.c_void_p, C.c_void_p, C.c_int, C.c_uint32, C.c_void_p, C.c_wchar_p, C.POINTER(StartupInfoEx), C.POINTER(ProcessInfo)], C.c_int),
        }
        for name, (args, result) in specs.items():
            method = getattr(self.k, name)
            method.argtypes, method.restype = args, result
        self.job = self.process = self.thread = 0
        self.handles = []
        self.resumed = self.verified = False
        self._failed_close_handles = set()
        self._close_unknown = False
        self._cleanup_lease = None

    @staticmethod
    def _check(value, code):
        if not value:
            raise NativeFailure(code)

    def _query(self, kind, value):
        if not self.job:
            raise NativeFailure("MXC_JOB_NOT_CREATED")
        self._check(self.k.QueryInformationJobObject(self.job, kind, C.byref(value), C.sizeof(value), None), "MXC_JOB_QUERY_FAILED")
        return value

    def configure(self, policy):
        self.job = self.k.CreateJobObjectW(None, None)
        self._check(self.job, "MXC_JOB_CREATE_FAILED")
        limits = ExtendedLimits()
        limits.basic.flags = self.FLAGS
        limits.basic.job_time = int(policy.cpu_seconds * 10_000_000)
        limits.basic.active_limit = policy.process_limit
        limits.process_memory = policy.process_memory_mb * 1024 * 1024
        limits.job_memory = policy.job_memory_mb * 1024 * 1024
        self._check(self.k.SetInformationJobObject(self.job, 9, C.byref(limits), C.sizeof(limits)), "MXC_JOB_LIMIT_FAILED")
        rate = CpuRate(5, policy.cpu_rate_percent * 100)
        self._check(self.k.SetInformationJobObject(self.job, 15, C.byref(rate), C.sizeof(rate)), "MXC_CPU_RATE_FAILED")
        action = C.c_uint32(0)  # JOB_OBJECT_TERMINATE_AT_END_OF_JOB.
        self._check(self.k.SetInformationJobObject(self.job, 6, C.byref(action), C.sizeof(action)), "MXC_CPU_ACTION_FAILED")
        actual, actual_rate = self._query(9, ExtendedLimits()), self._query(15, CpuRate())
        actual_action = self._query(6, C.c_uint32())
        if (actual.basic.flags != self.FLAGS or actual.basic.job_time != limits.basic.job_time or actual.basic.active_limit != policy.process_limit or actual.process_memory != limits.process_memory or actual.job_memory != limits.job_memory or actual_rate.flags != 5 or actual_rate.rate != rate.rate or actual_action.value != 0):
            raise NativeFailure("MXC_JOB_LIMIT_MISMATCH")
        self.verified = True

    def _pipe(self):
        read, write = C.c_void_p(), C.c_void_p()
        attrs = SecurityAttributes(C.sizeof(SecurityAttributes), None, True)
        self._check(self.k.CreatePipe(C.byref(read), C.byref(write), C.byref(attrs), 0), "MXC_PIPE_FAILED")
        self.handles.extend([read.value, write.value])
        self._check(self.k.SetHandleInformation(read, 1, 0), "MXC_PIPE_FAILED")
        return read.value, write.value

    def launch(self, argv, environment, cwd, *, cancel_event=None, before_resume=None,
               authority_guard=None, launch_attempt_callback=None):
        if not self.verified:
            raise NativeFailure("MXC_JOB_NOT_VERIFIED")
        out_read, out_write = self._pipe()
        err_read, err_write = self._pipe()
        attrs = SecurityAttributes(C.sizeof(SecurityAttributes), None, True)
        null = self.k.CreateFileW("NUL", 0x80000000, 3, C.byref(attrs), 3, 0, None)
        if not null or null == C.c_void_p(-1).value:
            raise NativeFailure("MXC_STDIN_FAILED")
        self.handles.append(null)
        size = C.c_size_t()
        self.k.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
        storage = C.create_string_buffer(size.value)
        self._check(self.k.InitializeProcThreadAttributeList(storage, 1, 0, C.byref(size)), "MXC_HANDLE_POLICY_FAILED")
        try:
            inherited = (C.c_void_p * 3)(null, out_write, err_write)
            self._check(self.k.UpdateProcThreadAttribute(storage, 0, 0x00020002, inherited, C.sizeof(inherited), None, None), "MXC_HANDLE_POLICY_FAILED")
            startup = StartupInfoEx()
            startup.base.size = C.sizeof(startup)
            startup.base.flags = 0x100  # STARTF_USESTDHANDLES.
            startup.base.stdin, startup.base.stdout, startup.base.stderr = null, out_write, err_write
            startup.attributes = C.cast(storage, C.c_void_p)
            info = ProcessInfo()
            env_block = C.create_unicode_buffer("\0".join(f"{key}={environment[key]}" for key in sorted(environment, key=str.casefold)) + "\0\0")
            command = C.create_unicode_buffer(subprocess.list2cmdline(argv))
            if launch_attempt_callback is not None:
                try: launch_attempt_callback()
                except BaseException: raise NativeFailure("MXC_LAUNCH_AUTHORITY_FAILED") from None
            if cancel_event is not None and cancel_event.is_set(): raise NativeFailure("MXC_CANCELLED")
            try:
                created = self.k.CreateProcessW(argv[0], command, None, None, True, 0x4 | 0x400 | 0x80000 | 0x08000000, env_block, str(cwd), C.byref(startup), C.byref(info))
            finally:
                # A host exception after native creation cannot discard the
                # still-suspended root's exact returned handles.
                self.process, self.thread = info.process, info.thread
            self._check(created, "MXC_CREATE_FAILED")
            self._check(self.k.AssignProcessToJobObject(self.job, self.process), "MXC_JOB_ASSIGN_FAILED")
            contained = C.c_int()
            self._check(self.k.IsProcessInJob(self.process, self.job, C.byref(contained)) and contained.value, "MXC_JOB_ASSIGN_UNCONFIRMED")
            # A supplied host CM must be the SAME guard used by its real grant,
            # goal and Stop mutations. No CM means only callback-time freshness.
            try:
                with authority_guard() if authority_guard is not None else nullcontext():
                    if cancel_event is not None and cancel_event.is_set(): raise NativeFailure("MXC_CANCELLED")
                    if before_resume is not None: before_resume()
                    if cancel_event is not None and cancel_event.is_set(): raise NativeFailure("MXC_CANCELLED")
                    if has_pending_cleanup(): raise NativeFailure("MXC_CLEANUP_PENDING")
                    if self.k.ResumeThread(self.thread) != 1: raise NativeFailure("MXC_RESUME_FAILED")
                    self.resumed = True
            except NativeFailure: raise
            except BaseException: raise NativeFailure("MXC_RESUME_AUTHORITY_FAILED") from None
        finally:
            self.k.DeleteProcThreadAttributeList(storage)
            primary_failure = sys.exc_info()[0] is not None
            closed = True
            for handle in (null, out_write, err_write):
                if not self._close_handle(handle): closed = False
            if not closed and not primary_failure:
                raise NativeFailure("MXC_RESOURCE_CLOSE_UNCONFIRMED")
        return out_read, err_read

    def read_available(self, handle):
        size = C.c_uint32()
        if not self.k.PeekNamedPipe(handle, None, 0, None, C.byref(size), None) or not size.value:
            return b""
        buffer = C.create_string_buffer(min(size.value, 65536))
        count = C.c_uint32()
        if not self.k.ReadFile(handle, buffer, len(buffer), C.byref(count), None):
            return b""
        return buffer.raw[:count.value]

    def exited(self):
        return self.k.WaitForSingleObject(self.process, 0) == 0

    def accounting(self):
        return self._query(1, Accounting())

    def terminate_and_reap(self):
        if self.job:
            if not self.k.TerminateJobObject(self.job, 77):
                return False
        if self.process:
            # Also covers assignment failure while the process is suspended.
            self.k.TerminateProcess(self.process, 77)
            if self.k.WaitForSingleObject(self.process, 2000) != 0:
                return False
        end = time.monotonic() + 2
        while self.job:
            try:
                if self.accounting().active_processes == 0:
                    return True
            except NativeFailure:
                return False
            if time.monotonic() >= end:
                return False
            time.sleep(.01)
        return True

    def _close_handle(self, handle):
        if not handle: return True
        if handle in self._failed_close_handles: return False
        try: closed = self.k.CloseHandle(handle) != 0
        except BaseException: closed = False
        if not closed:
            self._failed_close_handles.add(handle)
            self._close_unknown = True
            _retain_cleanup(self)
            return False
        if handle in self.handles: self.handles.remove(handle)
        for name in ("thread", "process", "job"):
            if getattr(self, name) == handle: setattr(self, name, 0)
        return True

    def close(self):
        for handle in [*self.handles, self.thread, self.process, self.job]:
            self._close_handle(handle)
        if self._close_unknown:
            _retain_cleanup(self)
            return False
        return True


def run(argv, *, environment, cwd, policy, cancel_event, watch, factory=WindowsJobProcess,
        authority_guard=None, before_resume=None, launch_attempt_callback=None):
    started = time.monotonic()
    while not _execution_lock.acquire(timeout=.02):
        if cancel_event.is_set():
            return NativeResult("MXC_CANCELLED")
        if time.monotonic() - started >= policy.timeout_seconds:
            return NativeResult("MXC_TIMEOUT")
    try:
        if has_pending_cleanup():
            return NativeResult("MXC_CLEANUP_PENDING", tree_reaped=False, resources_closed=False)
        remaining = policy.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            return NativeResult("MXC_TIMEOUT")
        return _run(argv, environment=environment, cwd=cwd, policy=replace(policy, timeout_seconds=remaining), cancel_event=cancel_event, watch=watch, factory=factory,
            authority_guard=authority_guard, before_resume=before_resume, launch_attempt_callback=launch_attempt_callback)
    finally:
        _execution_lock.release()


def _run(argv, *, environment, cwd, policy, cancel_event, watch, factory,
         authority_guard=None, before_resume=None, launch_attempt_callback=None):
    """No IO before explicit host invocation; no retry or fallback."""
    if sys.platform != "win32":
        return NativeResult("MXC_WINDOWS_REQUIRED")
    if any(value is not None and not callable(value) for value in (authority_guard,before_resume,launch_attempt_callback)):
        return NativeResult("MXC_INVALID_AUTHORITY")
    with _cleanup_lock: prior_leases = set(_pending_cleanup)
    process, code, out, err, exit_code, failed_lease = None, "MXC_NATIVE_FAILED", bytearray(), bytearray(), None, None
    reaped, verified, resumed, cpu, memory = True, False, False, 0, 0
    resources_closed = True
    try:
        process = factory()
        process.configure(policy)
        if cancel_event.is_set():
            code = "MXC_CANCELLED"
        elif not watch():
            code = "MXC_SCOPE_REVOKED"
        else:
            def resume_current():
                if cancel_event.is_set(): raise NativeFailure("MXC_CANCELLED")
                if not watch(): raise NativeFailure("MXC_SCOPE_REVOKED")
                if before_resume is not None: before_resume()
                if cancel_event.is_set(): raise NativeFailure("MXC_CANCELLED")
            pipes = process.launch(argv, environment, cwd, cancel_event=cancel_event, before_resume=resume_current,
                authority_guard=authority_guard, launch_attempt_callback=launch_attempt_callback)
            deadline = time.monotonic() + policy.timeout_seconds
            while True:
                for index, target, maximum in ((0, out, policy.stdout_limit), (1, err, policy.stderr_limit)):
                    raw = process.read_available(pipes[index])
                    if len(target) + len(raw) > maximum:
                        raise NativeFailure("MXC_OUTPUT_LIMIT")
                    target.extend(raw)
                if cancel_event.is_set():
                    raise NativeFailure("MXC_CANCELLED")
                if not watch():
                    raise NativeFailure("MXC_SCOPE_REVOKED")
                if process.exited():
                    # Drain pipe bytes already written before reporting success.
                    for index, target, maximum in ((0, out, policy.stdout_limit), (1, err, policy.stderr_limit)):
                        while raw := process.read_available(pipes[index]):
                            if len(target) + len(raw) > maximum:
                                raise NativeFailure("MXC_OUTPUT_LIMIT")
                            target.extend(raw)
                    value = C.c_uint32()
                    if not process.k.GetExitCodeProcess(process.process, C.byref(value)):
                        raise NativeFailure("MXC_EXIT_UNCONFIRMED")
                    exit_code, code = value.value, "MXC_EXITED"
                    break
                if time.monotonic() >= deadline:
                    raise NativeFailure("MXC_TIMEOUT")
                time.sleep(.01)
    except NativeFailure as failure:
        code = str(failure)
        failed_lease = getattr(failure, "_cleanup_lease", None)
    except Exception:
        code = "MXC_NATIVE_FAILED"
    finally:
        if process is not None:
            verified, resumed = process.verified, process.resumed
            try:
                cpu = process.accounting().user_time
                memory = process._query(9, ExtendedLimits()).peak_job_memory
            except Exception:
                pass
            try:
                reaped = process.terminate_and_reap()
            except Exception:
                reaped = False
            if reaped:
                try: resources_closed = process.close() is True
                except BaseException:
                    process._close_unknown = True
                    resources_closed = False
        # A monitor close can fail while another fixed error is already being
        # propagated. It still needs an owned lease and conservative retention.
        with _cleanup_lock:
            if failed_lease is None:
                failed_lease = next((lease for key, lease in _pending_cleanup.items() if key not in prior_leases), None)
    if not reaped or not resources_closed or failed_lease is not None:
        lease = _retain_cleanup(process, tree_reaped=reaped) if process is not None and (not reaped or not resources_closed) else failed_lease
        return NativeResult("MXC_CLEANUP_UNCONFIRMED", tree_reaped=reaped, limits_verified=verified, resumed=resumed,
            resources_closed=False, _cleanup_lease=lease)
    if code == "MXC_EXITED" and exit_code != 0 and cpu >= int(policy.cpu_seconds * 10_000_000):
        code = "MXC_CPU_LIMIT"
    return NativeResult(code, exit_code, bytes(out), bytes(err), reaped, verified, resumed, cpu, memory)
