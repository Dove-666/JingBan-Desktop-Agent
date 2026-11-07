"""Stdlib Win32 primitives for an isolated, opt-in owned Job guardian.

No process, file or OS policy changes happen on import. This module does not
accept PID/name-based lifetime evidence and is not registered with the agent.
"""
from __future__ import annotations

import ctypes as C
from functools import cmp_to_key
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

_cleanup_lock = threading.Lock()
_pending_launch_cleanup = {}


class GuardianError(ValueError):
    """Only fixed public failure codes; never raw Win32 error strings."""


class SecurityAttributes(C.Structure):
    _fields_ = [("size", C.c_uint32), ("descriptor", C.c_void_p), ("inherit", C.c_int)]


class StartupInfo(C.Structure):
    _fields_ = [("size", C.c_uint32), ("reserved", C.c_wchar_p), ("desktop", C.c_wchar_p),
        ("title", C.c_wchar_p), ("x", C.c_uint32), ("y", C.c_uint32), ("width", C.c_uint32),
        ("height", C.c_uint32), ("chars_x", C.c_uint32), ("chars_y", C.c_uint32),
        ("fill", C.c_uint32), ("flags", C.c_uint32), ("show", C.c_uint16),
        ("reserved_size", C.c_uint16), ("reserved_bytes", C.c_void_p),
        ("stdin", C.c_void_p), ("stdout", C.c_void_p), ("stderr", C.c_void_p)]


class StartupInfoEx(C.Structure):
    _fields_ = [("base", StartupInfo), ("attributes", C.c_void_p)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", C.c_void_p), ("thread", C.c_void_p), ("pid", C.c_uint32), ("tid", C.c_uint32)]


class BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", C.c_uint32),
        ("min_ws", C.c_size_t), ("max_ws", C.c_size_t), ("active_limit", C.c_uint32),
        ("affinity", C.c_size_t), ("priority", C.c_uint32), ("scheduling", C.c_uint32)]


class IoCounters(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]


class ExtendedLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", C.c_size_t),
        ("job_memory", C.c_size_t), ("peak_process_memory", C.c_size_t), ("peak_job_memory", C.c_size_t)]


class Accounting(C.Structure):
    _fields_ = [("user", C.c_int64), ("kernel", C.c_int64), ("period_user", C.c_int64),
        ("period_kernel", C.c_int64), ("faults", C.c_uint32), ("total", C.c_uint32),
        ("active", C.c_uint32), ("terminated", C.c_uint32)]


class Blob(C.Structure):
    _fields_ = [("size", C.c_uint32), ("data", C.c_void_p)]


class FileInfo(C.Structure):
    # FILETIME is DWORD-aligned, not UINT64-aligned.
    _pack_ = 4
    _fields_ = [("attributes", C.c_uint32), ("creation", C.c_uint64), ("access", C.c_uint64),
        ("write", C.c_uint64), ("volume", C.c_uint32), ("size_hi", C.c_uint32),
        ("size_lo", C.c_uint32), ("links", C.c_uint32), ("index_hi", C.c_uint32), ("index_lo", C.c_uint32)]


class Native:
    def __init__(self):
        if sys.platform != "win32":
            raise GuardianError("GUARDIAN_WINDOWS_REQUIRED")
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.a = C.WinDLL("advapi32", use_last_error=True)
        self.c = C.WinDLL("crypt32", use_last_error=True)
        self._set(self.k, {
            "GetCurrentProcess": ([], C.c_void_p), "CloseHandle": ([C.c_void_p], C.c_int),
            "CompareStringOrdinal": ([C.c_wchar_p, C.c_int, C.c_wchar_p, C.c_int, C.c_int], C.c_int),
            "LocalFree": ([C.c_void_p], C.c_void_p),
            "DuplicateHandle": ([C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p), C.c_uint32, C.c_int, C.c_uint32], C.c_int),
            "SetHandleInformation": ([C.c_void_p, C.c_uint32, C.c_uint32], C.c_int),
            "SetNamedPipeHandleState": ([C.c_void_p, C.POINTER(C.c_uint32), C.c_void_p, C.c_void_p], C.c_int),
            "GetNamedPipeHandleStateW": ([C.c_void_p, C.POINTER(C.c_uint32), C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint32], C.c_int),
            "CreateEventW": ([C.c_void_p, C.c_int, C.c_int, C.c_wchar_p], C.c_void_p),
            "SetEvent": ([C.c_void_p], C.c_int),
            "WaitForSingleObject": ([C.c_void_p, C.c_uint32], C.c_uint32),
            "WaitForMultipleObjects": ([C.c_uint32, C.POINTER(C.c_void_p), C.c_int, C.c_uint32], C.c_uint32),
            "GetProcessTimes": ([C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p], C.c_int),
            "CreateJobObjectW": ([C.c_void_p, C.c_wchar_p], C.c_void_p),
            "SetInformationJobObject": ([C.c_void_p, C.c_int, C.c_void_p, C.c_uint32], C.c_int),
            "QueryInformationJobObject": ([C.c_void_p, C.c_int, C.c_void_p, C.c_uint32, C.c_void_p], C.c_int),
            "AssignProcessToJobObject": ([C.c_void_p, C.c_void_p], C.c_int),
            "IsProcessInJob": ([C.c_void_p, C.c_void_p, C.POINTER(C.c_int)], C.c_int),
            "TerminateJobObject": ([C.c_void_p, C.c_uint32], C.c_int),
            "TerminateProcess": ([C.c_void_p, C.c_uint32], C.c_int),
            "ResumeThread": ([C.c_void_p], C.c_uint32),
            "CreatePipe": ([C.POINTER(C.c_void_p), C.POINTER(C.c_void_p), C.POINTER(SecurityAttributes), C.c_uint32], C.c_int),
            "PeekNamedPipe": ([C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32), C.c_void_p], C.c_int),
            "ReadFile": ([C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p], C.c_int),
            "WriteFile": ([C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p], C.c_int),
            "FlushFileBuffers": ([C.c_void_p], C.c_int),
            "CreateFileW": ([C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p], C.c_void_p),
            "CreateDirectoryW": ([C.c_wchar_p, C.c_void_p], C.c_int),
            "GetFileInformationByHandle": ([C.c_void_p, C.POINTER(FileInfo)], C.c_int),
            "MoveFileExW": ([C.c_wchar_p, C.c_wchar_p, C.c_uint32], C.c_int),
            "InitializeProcThreadAttributeList": ([C.c_void_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_size_t)], C.c_int),
            "UpdateProcThreadAttribute": ([C.c_void_p, C.c_uint32, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p], C.c_int),
            "DeleteProcThreadAttributeList": ([C.c_void_p], None),
            "CreateProcessW": ([C.c_wchar_p, C.c_wchar_p, C.c_void_p, C.c_void_p, C.c_int, C.c_uint32, C.c_void_p, C.c_wchar_p, C.POINTER(StartupInfoEx), C.POINTER(ProcessInfo)], C.c_int),
        })
        self._set(self.a, {
            "OpenProcessToken": ([C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p)], C.c_int),
            "GetTokenInformation": ([C.c_void_p, C.c_int, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)], C.c_int),
            "ConvertSidToStringSidW": ([C.c_void_p, C.POINTER(C.c_wchar_p)], C.c_int),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": ([C.c_wchar_p, C.c_uint32, C.POINTER(C.c_void_p), C.c_void_p], C.c_int),
            "GetSecurityInfo": ([C.c_void_p, C.c_int, C.c_uint32, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)], C.c_uint32),
            "ConvertSecurityDescriptorToStringSecurityDescriptorW": ([C.c_void_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_wchar_p), C.c_void_p], C.c_int),
        })
        for name in ("CryptProtectData", "CryptUnprotectData"):
            self._set(self.c, {name: ([C.POINTER(Blob), C.c_void_p, C.POINTER(Blob), C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(Blob)], C.c_int)})

    @staticmethod
    def _set(library, specifications):
        for name, (args, result) in specifications.items():
            method = getattr(library, name)
            method.argtypes, method.restype = args, result

    @staticmethod
    def check(value, code):
        if not value:
            raise GuardianError(code)
        return value

    def close(self, handle):
        if handle:
            self.k.CloseHandle(handle)

    def duplicate(self, handle, access, *, inheritable=False):
        result = C.c_void_p()
        current = self.k.GetCurrentProcess()
        self.check(self.k.DuplicateHandle(current, handle, current, C.byref(result), access, inheritable, 0), "GUARDIAN_DUPLICATE_FAILED")
        return result.value

    def noninherit(self, handle):
        self.check(self.k.SetHandleInformation(handle, 1, 0), "GUARDIAN_HANDLE_POLICY_FAILED")

    def pipe(self):
        read, write = C.c_void_p(), C.c_void_p()
        attrs = SecurityAttributes(C.sizeof(SecurityAttributes), None, False)
        self.check(self.k.CreatePipe(C.byref(read), C.byref(write), C.byref(attrs), 0), "GUARDIAN_PIPE_FAILED")
        try:
            self.noninherit(read.value)
            self.noninherit(write.value)
            # A single bounded, nonblocking handshake write. Partial/zero
            # writes fail closed and are never concatenated/retried. This is
            # not an asynchronous I/O implementation or a background writer.
            mode, actual = C.c_uint32(1), C.c_uint32()
            self.check(self.k.SetNamedPipeHandleState(write.value, C.byref(mode), None, None), "GUARDIAN_PIPE_POLICY_FAILED")
            self.check(self.k.GetNamedPipeHandleStateW(write.value, C.byref(actual), None, None, None, None, 0), "GUARDIAN_PIPE_POLICY_FAILED")
            if actual.value != 1:
                raise GuardianError("GUARDIAN_PIPE_POLICY_UNCONFIRMED")
        except BaseException:
            self.close(read.value)
            self.close(write.value)
            raise
        return read.value, write.value

    def write(self, handle, value):
        count = C.c_uint32()
        self.check(self.k.WriteFile(handle, value, len(value), C.byref(count), None), "GUARDIAN_WRITE_FAILED")
        if count.value != len(value):
            raise GuardianError("GUARDIAN_PARTIAL_WRITE")

    def read_bounded(self, handle, *, maximum, timeout):
        end = time.monotonic() + timeout
        result = bytearray()
        while time.monotonic() < end:
            size = C.c_uint32()
            if not self.k.PeekNamedPipe(handle, None, 0, None, C.byref(size), None):
                raise GuardianError("GUARDIAN_PIPE_CLOSED")
            if size.value:
                buffer = C.create_string_buffer(min(size.value, maximum + 1))
                count = C.c_uint32()
                self.check(self.k.ReadFile(handle, buffer, len(buffer), C.byref(count), None), "GUARDIAN_READ_FAILED")
                result.extend(buffer.raw[:count.value])
                if len(result) > maximum:
                    raise GuardianError("GUARDIAN_INPUT_LIMIT")
                if result.endswith(b"\n"):
                    return bytes(result[:-1])
            time.sleep(.01)
        raise GuardianError("GUARDIAN_HANDSHAKE_TIMEOUT")

    def signaled(self, handle):
        value = self.k.WaitForSingleObject(handle, 0)
        if value not in {0, 258}:
            raise GuardianError("GUARDIAN_WAIT_FAILED")
        return value == 0

    def creation_time(self, handle):
        values = [C.c_uint64() for _ in range(4)]
        self.check(self.k.GetProcessTimes(handle, *(C.byref(item) for item in values)), "GUARDIAN_PROCESS_IDENTITY_FAILED")
        return values[0].value

    def in_job(self, process, job=None):
        found = C.c_int()
        self.check(self.k.IsProcessInJob(process, job, C.byref(found)), "GUARDIAN_MEMBERSHIP_QUERY_FAILED")
        return bool(found.value)

    def job_empty(self, job):
        value = Accounting()
        self.check(self.k.QueryInformationJobObject(job, 1, C.byref(value), C.sizeof(value), None), "GUARDIAN_JOB_QUERY_FAILED")
        return value.active == 0

    def confirm_closed(self, job, root, *, timeout=3):
        end = time.monotonic() + timeout
        while True:
            if self.job_empty(job) and self.signaled(root):
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(.01)

    def current_sid(self):
        token = C.c_void_p()
        self.check(self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, C.byref(token)), "GUARDIAN_OWNER_UNAVAILABLE")
        try:
            size = C.c_uint32()
            self.a.GetTokenInformation(token, 1, None, 0, C.byref(size))
            self.check(0 < size.value <= 4096, "GUARDIAN_OWNER_UNAVAILABLE")
            buffer = C.create_string_buffer(size.value)
            self.check(self.a.GetTokenInformation(token, 1, buffer, size, C.byref(size)), "GUARDIAN_OWNER_UNAVAILABLE")
            sid = C.cast(buffer, C.POINTER(C.c_void_p))[0]
            text = C.c_wchar_p()
            self.check(self.a.ConvertSidToStringSidW(sid, C.byref(text)), "GUARDIAN_OWNER_UNAVAILABLE")
            try:
                return text.value
            finally:
                self.k.LocalFree(C.cast(text, C.c_void_p))
        finally:
            self.close(token)

    def descriptor(self):
        sid = self.current_sid()
        sddl = "O:" + sid + "D:P(A;;FA;;;" + sid + ")"
        descriptor = C.c_void_p()
        self.check(self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(descriptor), None), "GUARDIAN_ACL_UNAVAILABLE")
        return descriptor

    def owner_only(self, handle):
        descriptor, text = C.c_void_p(), C.c_wchar_p()
        if self.a.GetSecurityInfo(handle, 1, 5, None, None, None, None, C.byref(descriptor)) != 0:
            raise GuardianError("GUARDIAN_ACL_QUERY_FAILED")
        try:
            self.check(self.a.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 5, C.byref(text), None), "GUARDIAN_ACL_QUERY_FAILED")
            sid = self.current_sid()
            expected = "O:" + sid + "D:P(A;;FA;;;" + sid + ")"
            return text.value == expected
        finally:
            self.k.LocalFree(C.cast(text, C.c_void_p))
            self.k.LocalFree(descriptor)

    def protected_key(self, value, *, decrypt=False):
        buffer = C.create_string_buffer(value)
        source, result = Blob(len(value), C.cast(buffer, C.c_void_p)), Blob()
        method = self.c.CryptUnprotectData if decrypt else self.c.CryptProtectData
        self.check(method(C.byref(source), None, None, None, None, 1, C.byref(result)), "GUARDIAN_KEY_UNAVAILABLE")
        try:
            self.check(0 < result.size <= 4096, "GUARDIAN_KEY_LIMIT")
            return C.string_at(result.data, result.size)
        finally:
            self.k.LocalFree(result.data)

    def file_info(self, handle):
        value = FileInfo()
        self.check(self.k.GetFileInformationByHandle(handle, C.byref(value)), "GUARDIAN_FILE_QUERY_FAILED")
        return value


def minimal_environment():
    # The runtime is explicit and isolated; no user/profile/home/config/proxy
    # environment is inherited. Python's Windows DLLs only need SystemRoot.
    return {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}


def environment_block(environment, *, compare=None):
    """Bounded immutable host snapshot, never ambient inheritance or model IPC.

    Windows drive-directory entries are the only permitted '=' names. Values
    may contain Unicode and '='; embedded NULs and lone surrogates are rejected.
    """
    try:
        if type(environment) is not dict or len(environment) > 2048:
            raise ValueError()
        snapshot = dict(environment)
        for name, value in snapshot.items():
            if not isinstance(name, str) or not name or not isinstance(value, str) or "\0" in name or "\0" in value:
                raise ValueError()
            if "=" in name and not (len(name) == 3 and name[0] == "=" and name[1] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" and name[2] == ":"):
                raise ValueError()
            name.encode("utf-16-le", "strict")
            value.encode("utf-16-le", "strict")
        comparator = compare or (lambda left, right: (left.casefold() > right.casefold()) - (left.casefold() < right.casefold()))
        names = sorted(snapshot, key=cmp_to_key(comparator))
        if any(comparator(left, right) == 0 for left, right in zip(names, names[1:])):
            raise ValueError()
        block = "\0".join(name + "=" + snapshot[name] for name in names) + "\0\0"
        if len(block.encode("utf-16-le", "strict")) > 1024 * 1024:
            raise ValueError()
        return block
    except GuardianError:
        raise
    except Exception:
        raise GuardianError("GUARDIAN_ENVIRONMENT_INVALID") from None


class OwnedProcess:
    """Exact CreateProcess handle, with a resumable initial thread retained."""
    def __init__(self, native, info):
        self.native, self.handle, self.thread = native, info.process, info.thread
        self.pid = info.pid  # Diagnostic/fixture rendezvous only, never closure.
        self.resumed = False

    @classmethod
    def create(cls, native, argv, *, cwd, inherited, stdio, suspended=True, environment=None):
        if (not argv or not Path(argv[0]).is_absolute() or len(set(inherited)) != len(inherited)
                or any(type(item) is not int or item <= 0 for item in inherited)
                or not set(stdio) <= set(inherited)):
            raise GuardianError("GUARDIAN_LAUNCH_INVALID")
        size = C.c_size_t()
        native.k.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
        storage = C.create_string_buffer(size.value)
        native.check(native.k.InitializeProcThreadAttributeList(storage, 1, 0, C.byref(size)), "GUARDIAN_HANDLE_POLICY_FAILED")
        result = None
        failure = None
        reset_failed = False
        try:
            for handle in inherited:
                native.check(native.k.SetHandleInformation(handle, 1, 1), "GUARDIAN_HANDLE_POLICY_FAILED")
            handles = (C.c_void_p * len(inherited))(*inherited)
            native.check(native.k.UpdateProcThreadAttribute(storage, 0, 0x00020002, handles, C.sizeof(handles), None, None), "GUARDIAN_HANDLE_POLICY_FAILED")
            startup = StartupInfoEx()
            startup.base.size, startup.base.flags = C.sizeof(startup), 0x100
            startup.base.stdin, startup.base.stdout, startup.base.stderr = stdio
            startup.attributes = C.cast(storage, C.c_void_p)
            def compare(left, right):
                result = native.k.CompareStringOrdinal(left, -1, right, -1, True)
                if result not in {1, 2, 3}:
                    raise GuardianError("GUARDIAN_ENVIRONMENT_INVALID")
                return result - 2
            text = environment_block(minimal_environment() if environment is None else environment, compare=compare)
            units = len(text.encode("utf-16-le")) // 2 if C.sizeof(C.c_wchar) == 2 else len(text)
            block = C.create_unicode_buffer(text, units)
            info = ProcessInfo()
            flags = 0x400 | 0x80000 | 0x08000000 | (0x4 if suspended else 0)
            native.check(native.k.CreateProcessW(argv[0], C.create_unicode_buffer(subprocess.list2cmdline(argv)),
                None, None, True, flags, block, str(cwd), C.byref(startup), C.byref(info)), "GUARDIAN_CREATE_FAILED")
            result = cls(native, info)
            result.resumed = not suspended
        except BaseException as exc:
            failure = exc
        finally:
            native.k.DeleteProcThreadAttributeList(storage)
            for handle in inherited:
                if not native.k.SetHandleInformation(handle, 1, 0):
                    reset_failed = True
        if failure is not None or reset_failed:
            if result is not None:
                retain_failed_launch(result)
            if failure is not None:
                raise failure
            raise GuardianError("GUARDIAN_HANDLE_POLICY_FAILED")
        return result

    def resume(self):
        if self.resumed or self.native.k.ResumeThread(self.thread) != 1:
            raise GuardianError("GUARDIAN_RESUME_FAILED")
        self.resumed = True

    def terminate(self):
        self.native.check(self.native.k.TerminateProcess(self.handle, 77), "GUARDIAN_PROCESS_STOP_FAILED")

    def close(self):
        self.native.close(self.thread)
        self.native.close(self.handle)
        self.thread = self.handle = 0


def retain_failed_launch(process):
    """Keep exact handles when Stop/query is unconfirmed; no missing-PID proof."""
    try:
        process.native.k.TerminateProcess(process.handle, 77)
        if process.native.k.WaitForSingleObject(process.handle, 2000) == 0:
            process.close()
            return True
    except Exception:
        pass
    with _cleanup_lock:
        _pending_launch_cleanup[id(process)] = process
    return False


def has_pending_cleanup():
    with _cleanup_lock:
        return bool(_pending_launch_cleanup)


def reconcile_cleanup():
    """Explicit trusted-host cleanup only. Retain every unconfirmed handle."""
    with _cleanup_lock:
        candidates = list(_pending_launch_cleanup.items())
    for identity, process in candidates:
        try:
            process.native.k.TerminateProcess(process.handle, 77)
            if process.native.k.WaitForSingleObject(process.handle, 2000) != 0:
                continue
            process.close()
        except Exception:
            continue
        with _cleanup_lock:
            _pending_launch_cleanup.pop(identity, None)
    return not has_pending_cleanup()


class OwnedJob:
    """Unnamed no-breakaway Job; separate from the guardian/App process."""
    def __init__(self, native, *, active_limit=8):
        if active_limit is not None and (type(active_limit) is not int or not 1 <= active_limit <= 256):
            raise GuardianError("GUARDIAN_JOB_POLICY_INVALID")
        self.native = native
        self.handle = native.check(native.k.CreateJobObjectW(None, None), "GUARDIAN_JOB_CREATE_FAILED")
        try:
            value = ExtendedLimits()
            value.basic.flags = 0x2000 | (0x8 if active_limit is not None else 0)
            value.basic.active_limit = active_limit or 0
            native.check(native.k.SetInformationJobObject(self.handle, 9, C.byref(value), C.sizeof(value)), "GUARDIAN_JOB_LIMIT_FAILED")
            actual = ExtendedLimits()
            native.check(native.k.QueryInformationJobObject(self.handle, 9, C.byref(actual), C.sizeof(actual), None), "GUARDIAN_JOB_QUERY_FAILED")
            if actual.basic.flags != value.basic.flags or actual.basic.active_limit != (active_limit or 0):
                raise GuardianError("GUARDIAN_JOB_LIMIT_MISMATCH")
        except BaseException:
            native.close(self.handle)
            self.handle = 0
            raise

    def assign(self, process):
        if process.resumed:
            raise GuardianError("GUARDIAN_ROOT_NOT_SUSPENDED")
        try:
            self.native.check(self.native.k.AssignProcessToJobObject(self.handle, process.handle), "GUARDIAN_JOB_ASSIGN_FAILED")
            if not self.native.in_job(process.handle, self.handle):
                raise GuardianError("GUARDIAN_JOB_ASSIGN_UNCONFIRMED")
        except BaseException:
            retain_failed_launch(process)
            raise

    def terminate(self):
        self.native.check(self.native.k.TerminateJobObject(self.handle, 77), "GUARDIAN_JOB_STOP_FAILED")

    def close(self):
        self.native.close(self.handle)
        self.handle = 0
