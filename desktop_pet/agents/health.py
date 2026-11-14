"""Explicit, local Agent diagnostics. Importing this module starts no work.

Metadata is not runtime readiness. Even an isolated probe says nothing about
model access, screenshot quality, input accuracy or a user's task succeeding.
Reports contain fixed codes, booleans, counts and validated version strings;
paths, provider values, exception text and child stderr are never exported.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ..bundle_runtime import bundled_node_executable, frozen_workspace_root
from .settings import AGENT_WORKSPACE_ROOT


_VERSION = re.compile(r"v?[0-9]+(?:\.[0-9]+){1,3}(?:[-.](?:alpha|beta|a|b|rc|post|dev)[.-]?[0-9]+)?", re.I)
_COMMAND_CODES = frozenset({"OK", "MISSING", "CANCELLED", "TIMEOUT", "OUTPUT_LIMIT", "START_FAILED", "DEPENDENCY_MISSING"})



@dataclass(frozen=True)
class DoctorRequest:
    workspace: str | Path | None = field(default=None, repr=False)
    backend_python: str | Path | None = field(default=None, repr=False)
    node_path: str | Path | None = field(default=None, repr=False)
    database: str | Path | None = field(default=None, repr=False)
    probe: bool = False
    timeout_seconds: float = 20.0
    tools_enabled: bool = True
    computer_enabled: bool = True
    gui_configured: bool | None = None
    cancel_event: threading.Event | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class HealthCheck:
    id: str
    status: str
    code: str
    summary: str
    recommendation: str
    elapsed_ms: int
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HealthReport:
    version: int
    mode: str
    overall: str
    elapsed_ms: int
    checks: tuple[HealthCheck, ...]
    capabilities: tuple[dict, ...]
    network_requests: bool = False
    model_requests: bool = False
    screenshots_captured: bool = False
    physical_input_executed: bool = False
    production_files_modified: bool = False

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CommandResult:
    code: str
    stdout: bytes = field(default=b"", repr=False)
    returncode: int | None = None


def _safe_version(value):
    return value if isinstance(value, str) and len(value) <= 64 and _VERSION.fullmatch(value) else None


def _local_path(value):
    """Reject UNC/device paths before stat, including Windows paths on POSIX."""
    if value is None:
        return None
    raw = str(value)
    if not raw or any(ord(char) < 32 for char in raw) or raw.startswith(("\\\\", "//")):
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel.GetDriveTypeW.restype = ctypes.c_uint
        if kernel.GetDriveTypeW(path.anchor) == 4:  # DRIVE_REMOTE
            return None
    return path


def _environment(home=None):
    # No API keys, proxy credentials, profile names or model settings enter a child.
    result = {name: value for name, value in os.environ.items()
              if name.casefold() in {"systemroot", "windir", "systemdrive", "pathext"}}
    if home is not None:
        for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR"):
            result[name] = str(home)
    return result


class HealthDependencies:
    """Injectable boundary for tests; constructing it performs no IO."""

    def run(self, kind, command, *, timeout, cancel_event, cwd, environment):
        del kind
        from .service import _ProcessTree, _read_available
        process = tree = None
        output = bytearray()
        deadline = time.monotonic() + max(0.001, timeout)
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=cwd, env=environment, close_fds=True,
                **({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True}))
            tree = _ProcessTree(process)
            while True:
                if cancel_event.is_set():
                    return CommandResult("CANCELLED")
                if time.monotonic() >= deadline:
                    return CommandResult("TIMEOUT")
                chunk = _read_available(process.stdout)
                output.extend(chunk)
                if len(output) > 65536:
                    return CommandResult("OUTPUT_LIMIT")
                if process.poll() is not None:
                    # A descendant may still own the pipe: never block on read
                    # or communicate after the original process exits.
                    while True:
                        chunk = _read_available(process.stdout)
                        if not chunk: break
                        output.extend(chunk)
                        if len(output) > 65536: return CommandResult("OUTPUT_LIMIT")
                    return CommandResult("OK", bytes(output), process.returncode)
                cancel_event.wait(0.02)
        except Exception:
            return CommandResult("START_FAILED")
        finally:
            if tree is not None:
                tree.close()
            elif process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                try:
                    process.wait(timeout=2)
                except Exception:
                    pass
                if process.stdout is not None:
                    process.stdout.close()

    def path_metadata(self, value, *, directory=False):
        path = _local_path(value)
        if path is None:
            return {"selected": value is not None, "local": False, "exists": False, "readable": False, "writable": False}
        try:
            exists = path.is_dir() if directory else path.is_file()
            return {"selected": True, "local": True, "exists": exists,
                    "readable": exists and os.access(path, os.R_OK), "writable": exists and os.access(path, os.W_OK)}
        except Exception:
            return {"selected": True, "local": True, "exists": False, "readable": False, "writable": False}

    def ui_tars_metadata(self, root):
        versions = {}
        for name in ("sdk", "operator-nut-js"):
            path = root / "node_modules" / "@ui-tars" / name / "package.json"
            try:
                if path.stat().st_size <= 131072:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    versions[name] = _safe_version(value.get("version"))
            except Exception:
                versions[name] = None
        return versions

    def windows_metadata(self):
        if sys.platform != "win32":
            return {"supported": False}
        import ctypes
        from ctypes import wintypes
        result = {"supported": True, "session_active": None, "input_default": None,
                  "monitor_count": None, "dpi_awareness": None, "dpi_api_available": False}
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            user = ctypes.WinDLL("user32", use_last_error=True)
            wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
            kernel.GetCurrentProcessId.restype = wintypes.DWORD
            kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            kernel.ProcessIdToSessionId.restype = wintypes.BOOL
            session = wintypes.DWORD()
            if kernel.ProcessIdToSessionId(kernel.GetCurrentProcessId(), ctypes.byref(session)):
                state, length = ctypes.c_void_p(), wintypes.DWORD()
                wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
                wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
                wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
                if session.value == 0:
                    result["session_active"] = False
                elif wts.WTSQuerySessionInformationW(None, session.value, 8, ctypes.byref(state), ctypes.byref(length)):
                    try:
                        if length.value >= ctypes.sizeof(wintypes.DWORD):
                            result["session_active"] = ctypes.cast(state, ctypes.POINTER(wintypes.DWORD)).contents.value == 0
                    finally:
                        wts.WTSFreeMemory(state)
            user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            user.OpenInputDesktop.restype = wintypes.HANDLE
            user.CloseDesktop.argtypes = [wintypes.HANDLE]
            user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            user.GetUserObjectInformationW.restype = wintypes.BOOL
            desktop = user.OpenInputDesktop(0, False, 1)  # Read objects only; never SwitchDesktop.
            if not desktop:
                result["input_default"] = False
            else:
                try:
                    name, needed = ctypes.create_unicode_buffer(256), wintypes.DWORD()
                    if user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
                        result["input_default"] = name.value.casefold() == "default"
                finally:
                    user.CloseDesktop(desktop)
            user.GetSystemMetrics.argtypes = [ctypes.c_int]
            user.GetSystemMetrics.restype = ctypes.c_int
            result["monitor_count"] = user.GetSystemMetrics(80)
            # Query only. Changing DPI awareness here would change the caller's UI.
            user.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
            awareness = user.GetAwarenessFromDpiAwarenessContext(user.GetThreadDpiAwarenessContext())
            result["dpi_api_available"] = awareness in (0, 1, 2)
            result["dpi_awareness"] = awareness if awareness in (0, 1, 2) else None
        except Exception:
            pass
        return result


def collect_agent_health(request=None, *, dependencies=None):
    """Inspect the installed official DSH runtime; probes never call a model."""
    from .service import _BACKEND_ROOT, _WORKER_PATH, _DSH_VERSION, dsh_installation, node_executable
    from .activity import DSH_TOOL_NAMES, COMPUTER_TOOLS
    from ..node_runtime import node_supported
    started = time.monotonic()
    request = DoctorRequest() if request is None else request
    if (not isinstance(request, DoctorRequest) or type(request.probe) is not bool
            or isinstance(request.timeout_seconds, bool) or not isinstance(request.timeout_seconds, (int, float))
            or not math.isfinite(request.timeout_seconds) or not .05 <= request.timeout_seconds <= 120
            or any(type(value) is not bool for value in (request.tools_enabled, request.computer_enabled))):
        raise ValueError("DOCTOR_INVALID_REQUEST")
    dependencies = dependencies or HealthDependencies()
    cancel = request.cancel_event or threading.Event()
    checks = []
    def add(identity, status, code, summary, recommendation="", evidence=None):
        checks.append(HealthCheck(identity, status, code, summary, recommendation,
            max(0, int((time.monotonic() - started) * 1000)), evidence or {}))
    if cancel.is_set():
        add("doctor", "skipped", "DOCTOR_CANCELLED", "诊断已停止")
        return HealthReport(1, "probe" if request.probe else "metadata", "cancelled", 0, tuple(checks), ())
    node = request.node_path or node_executable()
    try: node_info = dependencies.path_metadata(node)
    except Exception: node_info = {}
    node_present = bool(node_info.get("exists") and node_info.get("local") and node_info.get("readable") and node_supported(node))
    add("node", "unverified" if node_present else "blocked", "NODE_METADATA_ONLY" if node_present else "NODE_MISSING",
        "Node.js 运行文件及版本已确认" if node_present else "Node.js 运行环境缺失或版本过旧",
        "" if node_present else "请修复或重新安装应用")
    versions = dsh_installation()
    packages_present = all(value == _DSH_VERSION for value in versions.values())
    add("dsh", "unverified" if packages_present else "blocked", "DSH_METADATA_ONLY" if packages_present else "DSH_MISSING",
        "DeepSeek Harness 安装版本已确认" if packages_present else "DeepSeek Harness 安装不完整",
        "" if packages_present else "请修复或重新安装应用",
        {"packages": {name: value if _safe_version(value) else None for name, value in versions.items()}})
    runner_present = _WORKER_PATH.is_file()
    add("runner", "passed" if runner_present else "blocked", "RUNNER_PRESENT" if runner_present else "RUNNER_MISSING",
        "任务连接组件已找到" if runner_present else "任务连接组件缺失")
    workspace = request.workspace or frozen_workspace_root() or AGENT_WORKSPACE_ROOT
    try: workspace_info = dependencies.path_metadata(workspace, directory=True)
    except Exception: workspace_info = {}
    add("workspace", "passed" if workspace_info.get("writable") else "unverified",
        "WORKSPACE_WRITABLE" if workspace_info.get("writable") else "WORKSPACE_NOT_VERIFIED",
        "任务目录可写" if workspace_info.get("writable") else "任务目录将在首次使用时检查")
    loaded = False
    if request.probe and node_present and packages_present and runner_present:
        with tempfile.TemporaryDirectory(prefix="aura-dsh-doctor-") as temporary:
            remaining = max(.001, request.timeout_seconds - (time.monotonic() - started))
            try:
                result = dependencies.run("dsh_sdk", [str(node), str(_WORKER_PATH), "--doctor"],
                    timeout=remaining, cancel_event=cancel, cwd=str(_BACKEND_ROOT), environment=_environment(temporary))
                data = json.loads(result.stdout) if result.code == "OK" and result.returncode == 0 else {}
                version = _safe_version(data.get("version"))
                loaded = data.get("loaded") is True and data.get("sdk") == "deepseek-harness" and version == _DSH_VERSION
                add("dsh_sdk", "passed" if loaded else "blocked", "DSH_SDK_IMPORTED" if loaded else "DSH_SDK_PROBE_FAILED",
                    "官方 DSH SDK 已加载" if loaded else "官方 DSH SDK 无法加载", evidence={"version": version})
            except Exception:
                add("dsh_sdk", "blocked", "DSH_SDK_PROBE_FAILED", "官方 DSH SDK 无法加载")
    computer_available = request.computer_enabled and sys.platform == "win32"
    if request.computer_enabled:
        try: windows = dependencies.windows_metadata()
        except Exception: windows = {"supported": False}
        computer_available = computer_available and windows.get("supported") is True
        add("desktop", "unverified" if computer_available else "blocked",
            "DESKTOP_METADATA_ONLY" if computer_available else "DESKTOP_UNSUPPORTED",
            "电脑操作由 DSH 工具提供，尚未执行操作" if computer_available else "当前平台不支持电脑操作")
    capabilities = tuple({"name": name, "available": bool(node_present and packages_present and runner_present
        and (computer_available if name in COMPUTER_TOOLS else request.tools_enabled)),
        "reason_code": "" if node_present and packages_present and runner_present else "runtime_missing"}
        for name in DSH_TOOL_NAMES if name != ("bash" if sys.platform == "win32" else "pwsh"))
    overall = "cancelled" if cancel.is_set() else "blocked" if any(item.status == "blocked" for item in checks) else "passed" if loaded else "unverified"
    return HealthReport(1, "probe" if request.probe else "metadata", overall,
        int((time.monotonic() - started) * 1000), tuple(checks), capabilities)
