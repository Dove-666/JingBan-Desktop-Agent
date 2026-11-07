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
from .settings import AGENT_WORKSPACE_ROOT, DEFAULT_PYTHON


_VERSION = re.compile(r"v?[0-9]+(?:\.[0-9]+){1,3}(?:[.-]?(?:a|b|rc|post|dev)[0-9]+)?", re.I)
_COMMAND_CODES = frozenset({"OK", "MISSING", "CANCELLED", "TIMEOUT", "OUTPUT_LIMIT", "START_FAILED", "DEPENDENCY_MISSING"})
_DEPENDENCIES = {"nanobot": "nanobot-ai", "quickjs": "quickjs-ng", "pypdf": "pypdf",
                 "docx": "python-docx", "pptx": "python-pptx", "openpyxl": "openpyxl"}


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


_METADATA_SCRIPT = r'''
import importlib.util, importlib.metadata, json, sys
names = {"nanobot":"nanobot-ai","quickjs":"quickjs-ng","pypdf":"pypdf","docx":"python-docx","pptx":"python-pptx","openpyxl":"openpyxl"}
result = {"python": ".".join(map(str, sys.version_info[:3])), "packages": {}}
for module, distribution in names.items():
    try:
        present = importlib.util.find_spec(module) is not None
        version = importlib.metadata.version(distribution) if present else None
    except Exception:
        present, version = False, None
    result["packages"][module] = {"present": present, "version": version}
print(json.dumps(result))
'''

_PROBE_PREFIX = r'''
import contextlib, io, json, os, pathlib, sys
root = pathlib.Path.cwd().resolve()
class Discard:
    def write(self, text): return len(text)
    def flush(self): pass
def fixture_path(path):
    return isinstance(path,(str,bytes,os.PathLike)) and pathlib.Path(os.fsdecode(path)).resolve().is_relative_to(root)
def audit(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen","os.system","os.startfile","os.posix_spawn"}:
        raise PermissionError("DOCTOR_OFFLINE_PROBE")
    if event == "open":
        path, mode, flags = args
        writing = any(flag in str(mode) for flag in "wax+") or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
        if writing and isinstance(path,(str,bytes,os.PathLike)):
            if not fixture_path(path):
                raise PermissionError("DOCTOR_FIXTURE_ONLY")
    if event in {"os.mkdir","os.remove","os.unlink","os.rmdir","os.rename","os.replace"}:
        paths = args[:2] if event in {"os.rename","os.replace"} else args[:1]
        if any(not fixture_path(path) for path in paths):
            raise PermissionError("DOCTOR_FIXTURE_ONLY")
sys.addaudithook(audit)
result = {"passed": False}
try:
    with contextlib.redirect_stdout(Discard()), contextlib.redirect_stderr(Discard()):
'''
_PROBE_SUFFIX = r'''
except BaseException:
    result = {"passed": False}
print(json.dumps(result))
'''


def _probe_script(body):
    return _PROBE_PREFIX + "\n".join("        " + line for line in body.strip().splitlines()) + "\n" + _PROBE_SUFFIX


_SDK_PROBE = _probe_script('''
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.registry import ToolRegistry
fixture = root / "doctor-fixture.txt"
fixture.write_text("AURA_ISOLATED_DOCTOR_FIXTURE", encoding="utf-8")
registry = ToolRegistry()
registry.register(ReadFileTool(workspace=root, allowed_dir=root, restrict_to_workspace=True))
read = registry.get("read_file").execute(path=str(fixture))
try:
    read.send(None)
except StopIteration as completed:
    reply = completed.value
else:
    read.close()
    result = {"passed": False, "scope_unverified": True}
if not result.get("scope_unverified"):
    result = {"passed": "AURA_ISOLATED_DOCTOR_FIXTURE" in str(reply), "sdk_tool_readback": True}
''')

_VM_PROBE = _probe_script('''
import quickjs
vm = quickjs.Context()
vm.set_memory_limit(8 * 1024 * 1024)
vm.set_time_limit(1)
result = {"passed": vm.eval("[1,2,3].reduce((a,b)=>a+b,0)") == 6, "pure_computation": True}
''')

_DOCUMENT_PROBE = _probe_script('''
from docx import Document
from pptx import Presentation
from openpyxl import Workbook, load_workbook
from pypdf import PdfReader, PdfWriter
import sqlite3
word = root / "doctor-fixture.docx"
doc = Document(); doc.add_paragraph("AURA_FIXTURE"); doc.save(word)
word_ok = Document(word).paragraphs[0].text == "AURA_FIXTURE"
slides = root / "doctor-fixture.pptx"
deck = Presentation(); deck.slides.add_slide(deck.slide_layouts[6]); deck.save(slides)
slides_ok = len(Presentation(slides).slides) == 1
sheet = root / "doctor-fixture.xlsx"
book = Workbook(); book.active["A1"] = 42; book.save(sheet); book.close()
check = load_workbook(sheet, read_only=True); sheet_ok = check.active["A1"].value == 42; check.close()
pdf = root / "doctor-fixture.pdf"
writer = PdfWriter(); writer.add_blank_page(width=100,height=100)
with pdf.open("wb") as target: writer.write(target)
pdf_ok = len(PdfReader(pdf).pages) == 1
with sqlite3.connect(root / "doctor-fixture.sqlite3") as database:
    db_ok = database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
result = {"passed": all((word_ok,slides_ok,sheet_ok,pdf_ok,db_ok)), "fixture_formats": 5}
''')

_NODE_PROBE = r'''
const write = process.stdout.write.bind(process.stdout);
process.stdout.write=()=>true;
for(const key of ['log','info','warn','error','debug']) console[key]=()=>{};
const blocked=()=>{throw new Error('DOCTOR_OFFLINE_PROBE')};
globalThis.fetch=blocked;
for(const name of ['node:net','node:tls','node:http','node:https','node:child_process']) {
 const module=(await import(name)).default;
 for(const key of ['connect','createConnection','request','get','spawn','spawnSync','exec','execSync','execFile','execFileSync','fork'])
   if(typeof module[key]==='function') module[key]=blocked;
 if(module.Socket?.prototype) module.Socket.prototype.connect=blocked;
}
(await import('node:module')).syncBuiltinESMExports();
try {
 const {pathToFileURL}=await import('node:url');
 const {loadRuntime}=await import(pathToFileURL(process.argv[1]).href);
 const runtime=await loadRuntime();
 write(JSON.stringify({passed:typeof runtime.GUIAgent==='function'&&typeof runtime.NutJSOperator==='function',constructors_only:true})+'\n');
} catch {write('{"passed":false}\n');process.exitCode=1;}
'''


def collect_agent_health(request=None, *, dependencies=None):
    """Run explicitly, off the GUI thread. No credentials are accepted or read."""
    started = time.monotonic()
    request = DoctorRequest() if request is None else request
    if not isinstance(request, DoctorRequest) or type(request.probe) is not bool or \
        isinstance(request.timeout_seconds, bool) or not isinstance(request.timeout_seconds, (int, float)) or \
        not math.isfinite(request.timeout_seconds) or not 0.05 <= request.timeout_seconds <= 120 or \
        any(type(value) is not bool for value in (request.tools_enabled, request.computer_enabled)) or \
        request.gui_configured is not None and type(request.gui_configured) is not bool:
        raise ValueError("DOCTOR_INVALID_REQUEST")
    dependencies = dependencies or HealthDependencies()
    cancel = request.cancel_event or threading.Event()
    if cancel.is_set():
        return HealthReport(1, "probe" if request.probe else "metadata", "cancelled", 0,
            (HealthCheck("doctor", "skipped", "DOCTOR_CANCELLED", "诊断已停止，未启动探测", "", 0),), ())
    deadline = started + request.timeout_seconds
    root = Path(__file__).absolute().parents[2]
    python = request.backend_python or DEFAULT_PYTHON
    node = request.node_path or bundled_node_executable() or shutil.which("node")
    workspace = request.workspace or frozen_workspace_root() or AGENT_WORKSPACE_ROOT
    checks = []

    def add(identity, status, code, summary, recommendation="", evidence=None, at=None):
        checks.append(HealthCheck(identity, status, code, summary, recommendation,
            max(0, int((time.monotonic() - (at or started)) * 1000)), evidence or {}))

    def command(kind, argv, *, cwd=root, home=None):
        if cancel.is_set(): return CommandResult("CANCELLED")
        remaining = deadline - time.monotonic()
        if remaining <= 0: return CommandResult("TIMEOUT")
        try:
            result = dependencies.run(kind, argv, timeout=remaining, cancel_event=cancel, cwd=str(cwd), environment=_environment(home))
        except Exception:
            return CommandResult("START_FAILED")
        if not isinstance(result, CommandResult) or result.code not in _COMMAND_CODES or not isinstance(result.stdout, bytes) or len(result.stdout) > 65536:
            return CommandResult("START_FAILED")
        return result

    def path_info(path, *, directory=False):
        try:
            value = dependencies.path_metadata(path, directory=directory)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {"selected": path is not None}

    def object_result(result):
        if result.code != "OK" or result.returncode != 0: return {}
        try:
            value = json.loads(result.stdout)
            return value if isinstance(value, dict) else {}
        except Exception: return {}

    at = time.monotonic()
    metadata = path_info(python)
    python_result = command("python_metadata", [str(python), "-I", "-B", "-c", _METADATA_SCRIPT]) if metadata.get("exists") and metadata.get("local") else CommandResult("MISSING")
    values = object_result(python_result)
    python_version = _safe_version(values.get("python"))
    python_parts = re.match(r"v?(\d+)\.(\d+)", python_version or "")
    python_ok = python_parts is not None and tuple(map(int, python_parts.groups())) >= (3, 11)
    python_code = ("PYTHON_METADATA_OK" if python_ok else "PYTHON_VERSION_UNSUPPORTED" if python_version else
        "PYTHON_METADATA_INVALID" if python_result.code == "OK" and python_result.returncode == 0 else
        "PYTHON_PROCESS_FAILED" if python_result.code == "OK" else "PYTHON_" + python_result.code)
    add("backend_python", "pass" if python_ok else "blocked", python_code,
        "独立 Python 版本已核对" if python_ok else "独立 Python 尚不可用或版本不受支持", "请检查后端解释器；后端需要 Python 3.11 或更高版本。" if not python_ok else "",
        {"version": python_version, "runtime_scope": "version_and_package_metadata", "sdk_loaded": False}, at)
    packages = values.get("packages") if isinstance(values.get("packages"), dict) else {}
    present = {}
    for module, distribution in _DEPENDENCIES.items():
        package = packages.get(module) if isinstance(packages.get(module), dict) else {}
        version = _safe_version(package.get("version"))
        present[module] = package.get("present") is True and version is not None
        if module in ("nanobot", "quickjs"):
            add(module, "unverified" if present[module] else "blocked", ("NANOBOT" if module == "nanobot" else "QUICKJS") + ("_METADATA_ONLY" if present[module] else "_MISSING"),
                "任务 SDK 依赖元数据已找到" if module == "nanobot" and present[module] else "JavaScript VM 依赖元数据已找到" if present[module] else "任务依赖尚未就绪",
                "可使用 --probe 检查隔离运行；包存在不代表任务可执行。" if present[module] else "请使用后端依赖清单修复独立环境。",
                {"package": distribution, "version": version, "runtime_verified": False}, at)
    documents_ok = all(present[name] for name in ("docx", "pptx", "openpyxl", "pypdf"))
    add("documents", "unverified" if documents_ok else "blocked", "DOCUMENT_METADATA_ONLY" if documents_ok else "DOCUMENT_DEPENDENCY_MISSING",
        "文档依赖元数据已找到" if documents_ok else "文档处理依赖不完整", "可使用 --probe 验证独立样例文件。" if documents_ok else "请修复后端文档依赖后重试。",
        {"packages": {name: bool(present[name]) for name in ("docx", "pptx", "openpyxl", "pypdf")},
         "versions": {name: _safe_version(packages.get(name, {}).get("version")) if isinstance(packages.get(name), dict) else None
                      for name in ("docx", "pptx", "openpyxl", "pypdf")}, "runtime_verified": False}, at)

    at = time.monotonic()
    node_meta = path_info(node)
    node_result = command("node_version", [str(node), "--version"]) if node_meta.get("exists") and node_meta.get("local") else CommandResult("MISSING")
    node_version = _safe_version(node_result.stdout.decode("ascii", "ignore").strip()) if node_result.code == "OK" and node_result.returncode == 0 else None
    node_ok = node_version is not None and int(node_version.lstrip("v").split(".")[0]) >= 22
    node_code = ("NODE_VERSION_OK" if node_ok else "NODE_VERSION_UNSUPPORTED" if node_version else
        "NODE_VERSION_INVALID" if node_result.code == "OK" and node_result.returncode == 0 else
        "NODE_PROCESS_FAILED" if node_result.code == "OK" else "NODE_" + node_result.code)
    add("node", "pass" if node_ok else "blocked", node_code,
        "Node.js 版本已核对" if node_ok else "电脑操作所需 Node.js 尚未就绪", "请安装 Node.js 22 或更高版本并重启桌宠。" if not node_ok else "",
        {"version": node_version, "runtime_scope": "version_command"}, at)
    ui_root = root / "agent_backend" / "ui_tars"
    try: ui_versions = dependencies.ui_tars_metadata(ui_root)
    except Exception: ui_versions = {}
    if not isinstance(ui_versions, dict): ui_versions = {}
    ui_versions = {name: _safe_version(ui_versions.get(name)) for name in ("sdk", "operator-nut-js")}
    ui_ok = all(ui_versions.values())
    add("ui_tars", "unverified" if ui_ok else "blocked", "UI_TARS_METADATA_ONLY" if ui_ok else "UI_TARS_DEPENDENCY_MISSING",
        "UI-TARS 包元数据已找到" if ui_ok else "UI-TARS 本地依赖不完整", "可使用 --probe 验证模块加载；不会截图或操作鼠标键盘。" if ui_ok else "请修复本地 UI-TARS 依赖。",
        {"versions": ui_versions, "runtime_verified": False}, at)

    at = time.monotonic()
    try: windows = dependencies.windows_metadata()
    except Exception: windows = {"supported": sys.platform == "win32"}
    if not isinstance(windows, dict): windows = {"supported": sys.platform == "win32"}
    supported = windows.get("supported") is True
    active = windows.get("session_active") is True
    desktop = windows.get("input_default") is True
    count = windows.get("monitor_count") if type(windows.get("monitor_count")) is int and 0 <= windows["monitor_count"] <= 128 else None
    dpi = windows.get("dpi_awareness") if type(windows.get("dpi_awareness")) is int and windows["dpi_awareness"] in (0, 1, 2) else None
    add("windows_session", "pass" if active else "blocked" if supported else "unsupported", "WINDOWS_SESSION_ACTIVE" if active else "WINDOWS_SESSION_UNAVAILABLE" if supported else "WINDOWS_REQUIRED",
        "当前 Windows 交互会话可用" if active else "当前交互会话尚不可确认", "请恢复并解锁 Windows 桌面后重试。" if not active else "",
        {"windows": supported, "session_active": active, "screenshots_captured": False}, at)
    add("input_desktop", "pass" if desktop and active else "blocked" if supported else "unsupported", "INPUT_DESKTOP_DEFAULT" if desktop and active else "INPUT_DESKTOP_UNAVAILABLE",
        "当前输入桌面为普通桌面" if desktop and active else "普通输入桌面尚不可用", "锁屏、安全桌面或断开的会话需要在电脑上处理。" if not desktop or not active else "",
        {"default_desktop": desktop, "input_executed": False}, at)
    add("display", "unverified" if count == 1 and dpi is not None else "warning", "SINGLE_DISPLAY_METADATA_ONLY" if count == 1 and dpi is not None else "MULTI_DISPLAY_UNSUPPORTED" if count and count > 1 else "DISPLAY_METADATA_UNAVAILABLE",
        "单屏和 DPI 元数据已核对" if count == 1 and dpi is not None else "显示环境超出已核对范围", "截图和输入坐标仍需实际任务核验；当前桥接以单屏为支持范围。",
        {"monitor_count": count, "caller_dpi_awareness": dpi, "coordinate_runtime_verified": False}, at)

    for identity, path, directory in (("workspace", workspace, True), ("database", request.database, False)):
        at = time.monotonic(); info = path_info(path, directory=directory)
        selected = info.get("selected") is True
        accessible = info.get("local") is True and info.get("exists") is True and info.get("readable") is True
        add(identity, "unverified" if accessible else "skipped" if not selected else "blocked", identity.upper() + ("_METADATA_ONLY" if accessible else "_NOT_SELECTED" if not selected else "_UNAVAILABLE"),
            "工作目录元数据可访问" if identity == "workspace" and accessible else "数据库元数据可访问" if accessible else "未指定数据库；未读取聊天记录" if not selected else "本地存储路径尚不可访问",
            "未写入目标位置；存在和访问标志不代表实际读写或数据库结构健康。" if accessible else "请选择可访问的本地位置；诊断不会自动创建或修复。",
            {"selected": selected, "exists": info.get("exists") is True, "readable_metadata": info.get("readable") is True,
             "writable_metadata": info.get("writable") is True, "contents_read": False, "runtime_verified": False}, at)

    if request.probe and not cancel.is_set():
        # The only writes happen below, in a new directory owned by this call.
        with tempfile.TemporaryDirectory(prefix="aura-doctor-") as fixture:
            for identity, available, script, argv, summary in (
                ("sdk_probe", python_ok and present["nanobot"], _SDK_PROBE, None, "SDK 已完成受控文本回读；未测试事件循环和模型执行链"),
                ("vm_probe", python_ok and present["quickjs"], _VM_PROBE, None, "JavaScript VM 已完成无宿主绑定的纯计算"),
                ("document_probe", python_ok and documents_ok, _DOCUMENT_PROBE, None, "文档与 SQLite 已完成独立样例回读"),
                ("ui_tars_probe", node_ok and ui_ok, None, [str(node), "--input-type=module", "-e", _NODE_PROBE, str(ui_root / "sdk-adapter.mjs")], "UI-TARS 模块和构造器已加载；未截图或输入"),
            ):
                if cancel.is_set(): break
                at = time.monotonic()
                result = command(identity, argv or [str(python), "-I", "-B", "-c", script], cwd=fixture, home=fixture) if available else CommandResult("DEPENDENCY_MISSING")
                data = object_result(result)
                passed = data.get("passed") is True
                scope_unverified = identity == "sdk_probe" and data.get("scope_unverified") is True
                code = identity.upper() + ("_ASYNC_UNVERIFIED" if scope_unverified else "_PASSED" if passed else "_" + result.code if result.code != "OK" else "_FAILED")
                add(identity, "pass" if passed else "unverified" if scope_unverified else "blocked", code,
                    summary if passed else "当前 SDK 工具需要事件循环，离线样例未覆盖该路径" if scope_unverified else "隔离运行探测未通过",
                    "请用实际只读任务核验 SDK 事件循环执行链。" if scope_unverified else "请检查对应依赖；诊断未自动修复或调用模型。" if not passed else "",
                    {"fixture_only": True, "runtime_scope": identity, "probe_passed": passed, "network_requests": False,
                     "screenshots_captured": False, "physical_input_executed": False,
                     **({"sdk_read_fixture_verified": passed, "asyncio_runner_verified": False, "model_loop_verified": False}
                        if identity == "sdk_probe" else {})}, at)

    from agent_backend.nanobot.tool_catalog import CATALOG
    capabilities = []
    for name, spec in CATALOG.items():
        allowed = spec.available(tools_enabled=request.tools_enabled, computer_enabled=request.computer_enabled,
            gui_configured=request.gui_configured is True)
        requires_backend = python_ok and present["nanobot"]
        blocked = not requires_backend or spec.requires_gui and not (node_ok and ui_ok and active and desktop and count == 1)
        capabilities.append({"name": name, "declared_enabled": bool(allowed), "state": "disabled" if not allowed else "dependency_blocked" if blocked else "unverified",
                             "runtime_ready": None, "model_access_checked": False})
    cancelled = cancel.is_set()
    if cancelled:
        add("doctor", "skipped", "DOCTOR_CANCELLED", "诊断已停止，未继续启动探测")
    overall = ("cancelled" if cancelled else "blocked" if any(check.status == "blocked" for check in checks) else
        "degraded" if any(check.status in ("warning", "unsupported") for check in checks) else
        "probe_partial" if any(check.id.endswith("_probe") and check.status == "unverified" for check in checks) else
        "probe_passed" if request.probe else "metadata_only")
    return HealthReport(1, "probe" if request.probe else "metadata", overall,
        max(0, int((time.monotonic() - started) * 1000)), tuple(checks), tuple(capabilities))
