"""Trusted MXC broker: model code runs only in a Win32k-disabled second stage.

The original MXC token, filesystem/network restrictions and native Job remain
in effect. This process never reads/evaluates the model request. Its only child
is the same host-pinned Python and staged worker, with three duplicated stdio
handles and a creation-time mitigation queried while the child is suspended.
The outer host owns Stop/EOF/authorization and publication; this process does
not claim to hold that host's cross-process authority lock around Resume.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

BOUNDARY = "win32k_spawn_v1"
FAILED = 78
UNCLOSED = 79


class WindowsBroker:
    def __init__(self):
        # ctypes cannot initialize under Win32k ON on the pinned runtime. Only
        # this trusted first stage imports it; the model worker never needs it.
        import ctypes as C
        self.C = C
        class StartupInfo(C.Structure):
            _fields_ = [("size", C.c_uint32), ("reserved", C.c_wchar_p), ("desktop", C.c_wchar_p),
                ("title", C.c_wchar_p), ("x", C.c_uint32), ("y", C.c_uint32), ("width", C.c_uint32),
                ("height", C.c_uint32), ("chars_x", C.c_uint32), ("chars_y", C.c_uint32),
                ("fill", C.c_uint32), ("flags", C.c_uint32), ("show", C.c_uint16),
                ("reserved_size", C.c_uint16), ("reserved_bytes", C.c_void_p),
                ("stdin", C.c_void_p), ("stdout", C.c_void_p), ("stderr", C.c_void_p)]
        class StartupEx(C.Structure):
            _fields_ = [("base", StartupInfo), ("attributes", C.c_void_p)]
        class ProcessInfo(C.Structure):
            _fields_ = [("process", C.c_void_p), ("thread", C.c_void_p),
                ("pid", C.c_uint32), ("tid", C.c_uint32)]
        self.StartupEx, self.ProcessInfo = StartupEx, ProcessInfo
        self.k = C.WinDLL("kernel32.dll", use_last_error=True)
        functions = {
            "GetCurrentProcess": ([], C.c_void_p), "GetStdHandle": ([C.c_uint32], C.c_void_p),
            "DuplicateHandle": ([C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p), C.c_uint32, C.c_int, C.c_uint32], C.c_int),
            "CloseHandle": ([C.c_void_p], C.c_int),
            "InitializeProcThreadAttributeList": ([C.c_void_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_size_t)], C.c_int),
            "UpdateProcThreadAttribute": ([C.c_void_p, C.c_uint32, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p], C.c_int),
            "DeleteProcThreadAttributeList": ([C.c_void_p], None),
            "CreateProcessW": ([C.c_wchar_p, C.c_wchar_p, C.c_void_p, C.c_void_p, C.c_int, C.c_uint32,
                C.c_void_p, C.c_wchar_p, C.POINTER(StartupEx), C.POINTER(ProcessInfo)], C.c_int),
            "QueryFullProcessImageNameW": ([C.c_void_p, C.c_uint32, C.c_wchar_p, C.POINTER(C.c_uint32)], C.c_int),
            "GetProcessMitigationPolicy": ([C.c_void_p, C.c_int, C.c_void_p, C.c_size_t], C.c_int),
            "IsProcessInJob": ([C.c_void_p, C.c_void_p, C.POINTER(C.c_int)], C.c_int),
            "ResumeThread": ([C.c_void_p], C.c_uint32),
            "WaitForSingleObject": ([C.c_void_p, C.c_uint32], C.c_uint32),
            "GetExitCodeProcess": ([C.c_void_p, C.POINTER(C.c_uint32)], C.c_int),
            "TerminateProcess": ([C.c_void_p, C.c_uint32], C.c_int),
        }
        for name, (arguments, result) in functions.items():
            function = getattr(self.k, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def require(value):
        if not value:
            raise ValueError("MXC_WIN32K_BROKER_FAILED")

    def run(self, executable, worker):
        C, k = self.C, self.k
        duplicate_handles = []
        info = self.ProcessInfo()
        attributes = None
        initialized = False
        signaled = False
        exit_code = FAILED
        clean = True
        try:
            # The enclosing exact Job identity stays owned by the outer host.
            # This observes membership rather than minting a new containment.
            in_job = C.c_int()
            self.require(k.IsProcessInJob(k.GetCurrentProcess(), None, C.byref(in_job)) and in_job.value)
            for standard in (0xfffffff6, 0xfffffff5, 0xfffffff4):
                source = k.GetStdHandle(standard)
                self.require(source and source != C.c_void_p(-1).value)
                copied = C.c_void_p()
                try:
                    ok = k.DuplicateHandle(k.GetCurrentProcess(), source, k.GetCurrentProcess(),
                        C.byref(copied), 0, True, 2)  # DUPLICATE_SAME_ACCESS.
                finally:
                    if copied.value: duplicate_handles.append(copied.value)
                self.require(ok and copied.value)
            size = C.c_size_t()
            k.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
            self.require(0 < size.value <= 65536)
            attributes = C.create_string_buffer(size.value)
            self.require(k.InitializeProcThreadAttributeList(attributes, 2, 0, C.byref(size)))
            initialized = True
            handles = (C.c_void_p * 3)(*duplicate_handles)
            mitigation = C.c_uint64(1 << 28)
            self.require(k.UpdateProcThreadAttribute(attributes, 0, 0x00020002,
                handles, C.sizeof(handles), None, None))
            self.require(k.UpdateProcThreadAttribute(attributes, 0, 0x00020007,
                C.byref(mitigation), C.sizeof(mitigation), None, None))
            startup = self.StartupEx()
            startup.base.size = C.sizeof(startup)
            startup.base.flags = 0x100
            startup.base.stdin, startup.base.stdout, startup.base.stderr = duplicate_handles
            startup.attributes = C.cast(attributes, C.c_void_p)
            command = C.create_unicode_buffer(subprocess.list2cmdline(
                [str(executable), "-I", "-S", "-B", str(worker)]))
            # No shell, breakaway, arbitrary runtime or model argument exists.
            created = k.CreateProcessW(str(executable), command, None, None, True,
                0x4 | 0x80000 | 0x08000000, None, str(worker.parent), C.byref(startup), C.byref(info))
            self.require(created and info.process and info.thread)
            policy = C.c_uint32()
            self.require(k.GetProcessMitigationPolicy(info.process, 4, C.byref(policy), 4) and policy.value & 1)
            in_job = C.c_int()
            self.require(k.IsProcessInJob(info.process, None, C.byref(in_job)) and in_job.value)
            image = C.create_unicode_buffer(32768)
            image_size = C.c_uint32(len(image))
            self.require(k.QueryFullProcessImageNameW(info.process, 0, image, C.byref(image_size)))
            self.require(os.path.normcase(image.value) == os.path.normcase(str(executable)))
            self.require(k.ResumeThread(info.thread) == 1)
            self.require(k.WaitForSingleObject(info.process, 0xffffffff) == 0)
            signaled = True
            actual = C.c_uint32()
            self.require(k.GetExitCodeProcess(info.process, C.byref(actual)))
            exit_code = int(actual.value)
        except BaseException:
            exit_code = FAILED
        finally:
            if info.process and not signaled:
                try:
                    if k.WaitForSingleObject(info.process, 0) != 0:
                        clean = bool(k.TerminateProcess(info.process, FAILED)) and clean
                    signaled = k.WaitForSingleObject(info.process, 2000) == 0
                    clean = signaled and clean
                except BaseException:
                    clean = False
            for handle in (info.thread, info.process, *duplicate_handles):
                if handle:
                    try: clean = bool(k.CloseHandle(handle)) and clean
                    except BaseException: clean = False
            if initialized:
                try: k.DeleteProcThreadAttributeList(attributes)
                except BaseException: clean = False
        return exit_code if clean else UNCLOSED


def main():
    try:
        if os.name != "nt": return FAILED
        parent = Path(__file__).resolve().parent
        configuration = json.loads(parent.joinpath("broker.json").read_bytes())
        if (type(configuration) is not dict or set(configuration) != {"version", "desktop_boundary", "python_executable", "python_sha256"}
                or type(configuration["version"]) is not int or configuration["version"] != 1
                or configuration["desktop_boundary"] != BOUNDARY):
            return FAILED
        executable = Path(configuration["python_executable"])
        if (not executable.is_absolute() or executable != executable.resolve()
                or os.path.normcase(str(executable)) != os.path.normcase(str(Path(sys.executable).resolve()))
                or hashlib.sha256(executable.read_bytes()).hexdigest() != configuration["python_sha256"]):
            return FAILED
        worker = parent / "worker.py"
        if not worker.is_file() or worker.is_symlink(): return FAILED
        return WindowsBroker().run(executable, worker)
    except BaseException:
        return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
