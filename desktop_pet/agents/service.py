"""On-demand host for the official DeepSeek Harness Node runtime.

The harness owns planning, tools and native sessions. This host owns user
requests, credentials, process lifetime and the desktop/mobile presentation.
"""
from __future__ import annotations
from dataclasses import asdict, is_dataclass, replace
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import shutil
import subprocess
import sys
import secrets as host_secrets
import threading
import time
from uuid import UUID, uuid4

from .contracts import AgentError, AgentRequest, AgentResult, AgentValidationError
from .settings import AGENT_WORKSPACE_ROOT, AgentSettings, shared_api_settings, validate_api
from ..bundle_runtime import bundled_node_executable, bundled_python_executable, frozen_workspace_root
from ..node_runtime import node_supported
from ..reasoning import ReasoningError, resolve_reasoning, validate_selection
from .activity import TOOL_LABELS, ACTION_LABELS, COMPUTER_TOOLS, DSH_TOOL_NAMES, public_activity
from .dsh_storage import DshSessionStore, session_scope, valid_session_id
from .identities import conversation_uuid
from .interactions import InteractionBroker, checked_interaction
from .journal import ExecutionJournal
from .artifacts import ArtifactStore
from .process_lifecycle import WorkerLifecycle
from .execution_attempt import ExecutionAttempts
from .staged_input import StagedRunInput, StagedInputError
from ..redaction import redact_text

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_ROOT = _ROOT / "agent_backend" / "deepseek_harness"
_WORKER_PATH = _BACKEND_ROOT / "worker.mjs"
_DSH_VERSION = "0.2.2-rc.3"
_MAX_LINE = 1048576
_GRACEFUL_STOP_SECONDS = 45.0
_EMOTIONS = {"neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talking"}


def _workspace_root():
    return frozen_workspace_root() or AGENT_WORKSPACE_ROOT


def node_executable():
    bundled = bundled_node_executable()
    if bundled is not None:
        return Path(bundled) if node_supported(bundled) else None
    runtime = _ROOT / "runtime" / "node" / "node.exe"
    if runtime.is_file():
        return runtime if node_supported(runtime) else None
    found = shutil.which("node")
    return Path(found) if found and node_supported(found) else None


def dsh_installation():
    """Static local package metadata only; never contacts a provider."""
    versions = {}
    for name in ("dsh", "dsh-sdk-client"):
        path = _BACKEND_ROOT / "node_modules" / "@deepseek-ai" / name / "package.json"
        try:
            if path.stat().st_size > 131072:
                raise ValueError()
            package = json.loads(path.read_text(encoding="utf-8"))
            versions[name] = package.get("version") if package.get("name") == "@deepseek-ai/" + name else None
        except (OSError, ValueError, TypeError):
            versions[name] = None
    return versions


def worker_environment(home):
    environment = {name: value for name, value in os.environ.items()
                   if not re.search(r"API_?KEY|TOKEN|SECRET|PASSWORD|OPENAI|ANTHROPIC|DEEPSEEK|DSH|NANOBOT|PYTHONPATH|NODE_OPTIONS|NODE_PATH",
                                    name, re.IGNORECASE)}
    environment.update({"DSH_HOME": str(home), "NO_COLOR": "1"})
    if getattr(sys, "frozen", False):
        paths = [str(path.parent) for path in (bundled_python_executable(), bundled_node_executable()) if path is not None]
        inherited = next((value for name, value in environment.items() if name.casefold() == "path"), "")
        for name in tuple(environment):
            if name.casefold() == "path": del environment[name]
        environment["PATH"] = os.pathsep.join([*paths, *([inherited] if inherited else [])])
    return environment


def current_image(value):
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) > 16000000
            or not re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+", value)):
        raise ValueError("INVALID_CURRENT_IMAGE")
    return value


PROGRESS_MESSAGES = {
    "starting": "正在启动任务助手…", "planning": "正在规划任务…",
    "tool": "正在使用任务工具…", "computer": "正在操作电脑…",
    "verifying": "正在核验任务结果…",
    "finishing": "正在整理任务结果…", "completed": "任务已完成。",
}
_ERROR_MESSAGES = {
    "office_runtime_invalid": "文档转换组件的本机配置无效，请修复配置后再试。",
    "vision_rejected": "图文请求未被模型接口接受，请确认模型支持图片并检查接口配置。",
    "disabled": "任务助手尚未启用，请在设置中启用后再试。",
    "kind_disabled": "这类任务尚未启用，请先检查任务助手设置。",
    "not_configured": "任务助手尚未配置完成，请检查 DeepSeek Harness 运行环境和模型设置。",
    "missing_backend": "未找到任务助手后端，请先完成后端安装。",
    "missing_sdk": "任务助手依赖尚未安装，请先完成 DeepSeek Harness 安装。",
    "sdk_unlimited_unsupported": "当前任务 SDK 与不限步数模式不兼容，任务未执行；请检查本地后端版本。",
    "gui_not_configured": "模型 API 尚未配置完成，请在「模型与 API」中填写地址、模型名称和密钥。",
    "cancelled": "任务已停止；已经完成的操作会保留，未自动重试。",
    "timeout": "任务超时，本次等待已结束。已完成的操作会保留，本次未自动重试。",
    "protocol": "任务助手返回格式无效，已停止且未自动重试。",
    "backend_failed": "任务助手未能完成这次操作；请检查当前结果，再发送新的任务消息。",
    "agent_failed": "任务执行链中断；请检查已完成的操作，再发送新的任务消息。",
    "worker_startup_timeout": "任务后台尚未准备好接收请求，本次启动已停止。",
    "worker_input_stage_timeout": "任务后台接收请求超时，本次未提交完整任务；请检查后台状态。",
    "worker_input_commit_timeout": "任务启动提交超时，后台已停止；本次不会自动重试。",
    "worker_input_write_failed": "任务启动通信中断，后台已停止；本次不会自动重试。",
    "empty_reply": "任务助手没有返回结果说明；请检查已完成的操作，再发送新的任务消息。",
    "computer_not_used": "任务助手没有执行电脑操作；请明确要操作的应用和目标后发送新消息。",
    "gui_runtime_missing": "任务运行环境缺失或版本过旧；请修复或重新安装应用。",
    "gui_start_failed": "电脑操作后台启动失败；请修复 DeepSeek Harness 运行环境。",
    "gui_exited": "电脑操作后台意外退出；请检查当前结果，再发送新的任务消息。",
    "gui_protocol": "电脑操作后台返回格式无效，任务已停止；请检查当前结果。",
    "gui_failed": "电脑操作未能完成；请检查当前画面，再发送更明确的任务消息。",
    "gui_sdk_unavailable": "电脑操作组件尚未就绪；请修复 DeepSeek Harness 和 Windows 操作组件。",
    "gui_unsupported_platform": "当前电脑操作组件只支持 Windows。",
    "gui_step_limit": "电脑操作已达到本次步数上限，仍未确认完成；请检查画面，再发送更具体的任务。",
    "gui_screenshot_failed": "无法获取桌面画面，电脑操作已停止；请确认桌面会话可用。",
    "gui_desktop_locked": "电脑已锁屏或正在显示安全桌面；请在电脑上解锁或完成安全提示后再发送任务。",
    "gui_desktop_session_unavailable": "电脑桌面会话已断开或不可用；请恢复 Windows 桌面后再发送任务。",
    "gui_screenshot_display_unavailable": "目标显示器不可用或已发生切换；电脑操作已停止，请检查显示器后再发送任务。",
    "gui_screenshot_black_frame": "截图返回近乎全黑的不可用画面，电脑操作已停止；请恢复可见桌面后再发送任务。",
    "gui_model_failed": "电脑操作模型请求失败；请检查模型服务、密钥，以及模型是否支持看图和电脑操作。",
    "gui_context_length_exceeded": "电脑操作上下文已达到模型服务的实际容量；请新建任务或选择容量更大的模型。",
    "model_context_exceeded": "任务上下文已达到模型服务的实际容量；请新建对话或选择容量更大的模型。",
    "gui_action_failed": "鼠标或键盘操作执行失败，任务已停止；请检查当前画面。",
    "gui_unsupported_action": "模型返回了尚不支持的电脑动作，任务已停止；请检查当前画面。",
    "gui_sdk_failed": "电脑操作组件未能完成任务；请检查画面，再发送新的任务消息。",
    "gui_invalid_request": "电脑操作配置无效；请检查模型地址和名称，重新保存后重试。",
    "gui_invalid_provider": "电脑操作模型配置不兼容；请重新保存配置后重试。",
    "gui_busy": "上一项电脑操作尚未结束；请稍后发送新任务。",
    "gui_worker_failed": "电脑操作后台运行失败；请检查当前结果，再发送新的任务消息。",
    "gui_already_used": "本次电脑操作已经执行，未再次执行；请检查结果后发送新任务。",
    "session_unavailable": "任务会话暂时无法恢复，请检查本地数据目录；已完成的操作不会重复执行。",
    "closed": "任务助手已关闭。",
    "busy": "上一项任务仍在运行或结束中，请稍后发送新任务；本次未启动任务助手。",
    "stop_unconfirmed": "正在等待电脑操作结束，暂时无法确认已停止。请稍后检查任务状态。",
    "checkpoint_unavailable": "任务检查点无法可靠读写，本次任务已停止且未继续派发；聊天记录仍保留，请检查本地数据目录后重试。",
    "context_store_unavailable": "任务资料暂时无法可靠保存，请检查本地数据目录后重试。",
    "provider_auth_failed": "模型服务未接受当前凭据，请检查模型 API 配置。",
    "provider_rate_limited": "模型服务暂时限流，请稍后继续；已完成的操作不会重复执行。",
    "provider_unavailable": "模型服务暂时不可用；已完成的操作已保留。",
    "request_timeout": "本次模型或网络请求未及时返回；已完成的操作已保留。",
    "gui_no_progress": "当前界面没有可确认的新进展，已暂停重复操作，请检查目标或补充指令。",
    "guardian_scope_conflict": "文件授权范围包含应用私有任务记录，本轮未执行；请缩小工作目录或文件授权范围。",
    "guardian_unavailable": "远程任务的安全恢复记录暂不可用，本轮未继续启动。",
    "execution_runtime_invalid": "本机计算工具配置无效，任务未启动；请检查应用私有配置。",
    "execution_runtime_changed": "本机计算工具配置已变化，本轮已停止；已完成的操作不会自动重复。",
}


def _error(code):
    return AgentError(code, _ERROR_MESSAGES.get(code, _ERROR_MESSAGES["backend_failed"]))


def _redact(text, secrets):
    return redact_text(text, secrets, "[已隐藏密钥]")


def _pairs_unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value


def _read_available(pipe):
    """Poll an anonymous pipe without a reader thread, including on Windows."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        available = wintypes.DWORD()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = kernel.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        peek.restype = wintypes.BOOL
        if not peek(msvcrt.get_osfhandle(pipe.fileno()), None, 0, None,
                    ctypes.byref(available), None):
            return b""
        return os.read(pipe.fileno(), min(available.value, 65536)) if available.value else b""
    readable, _, _ = select.select([pipe], [], [], 0)
    return os.read(pipe.fileno(), 65536) if readable else b""


class _ProcessTree:
    """Closing this task's lifetime also closes tool/GUI descendants."""

    def __init__(self, process):
        self.process = process
        self._job = None
        self._confirmed_closed = False
        self._close_lock = threading.Lock()
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   wintypes.LPVOID, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise _error("backend_failed")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(job, int(process._handle))):
            kernel.CloseHandle(job)
            raise _error("backend_failed")
        self._job, self._kernel = job, kernel

    def close(self):
        """Request Stop on this owned tree; this alone is never closure proof."""
        with self._close_lock:
            if self._confirmed_closed: return
            if self._job is not None:
                # Retain the job handle until its accounting proves every
                # member ended; closing it first loses that observation.
                self._kernel.TerminateJobObject(self._job, 1)
            elif sys.platform != "win32" and self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError:
                    pass

    def confirm_closed(self, *, timeout_seconds=2.0):
        """Confirm this exact owned job is empty, then release its handle."""
        with self._close_lock:
            if self._confirmed_closed: return True
            if sys.platform != "win32" or self._job is None:
                # No host PID enumeration/group absence can mint this proof.
                return False
            import ctypes
            from ctypes import wintypes
            class Accounting(ctypes.Structure):
                _fields_ = [(name, ctypes.c_longlong) for name in
                    ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
                _fields_ += [(name, wintypes.DWORD) for name in
                    ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]
            deadline = time.monotonic() + timeout_seconds
            while True:
                accounting = Accounting()
                if not self._kernel.QueryInformationJobObject(self._job, 1, ctypes.byref(accounting),
                                                              ctypes.sizeof(accounting), None):
                    return False
                if accounting.ActiveProcesses == 0:
                    if not self._kernel.CloseHandle(self._job): return False
                    self._job = None
                    self._confirmed_closed = True
                    return True
                if time.monotonic() >= deadline: return False
                time.sleep(min(.02, max(0, deadline - time.monotonic())))


class AgentService:
    """Call execute off the UI thread; construction performs no IO or launch."""

    def __init__(self, *, checkpoint_path=None):
        self._lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._processes = {}
        self._worker_cancel_senders = {}
        self._worker_handles = {}
        self._worker_cleanup_pending = set()
        self._worker_input_writers = {}
        self._worker_lifecycle = WorkerLifecycle()
        self._execution_attempts = ExecutionAttempts()
        self._closed = threading.Event()
        self._session_epoch = 0
        self._session_epochs = {}
        self._checkpoint_status = ""
        self._checkpoint_deletions = []
        self._checkpoint_executor = None
        self._input_lock = threading.Lock()
        self._interaction_broker = InteractionBroker()
        self._active_control = None
        self._file_grants = {}
        private = (Path(checkpoint_path).absolute().parent if checkpoint_path is not None else
                   Path(os.environ.get("JINGBAN_PROFILE_DIR") or
                        Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AURA Desktop Pet"))
        self._guardian_config = private
        self._session_store = DshSessionStore(private / "dsh_sessions")
        self._guardian_launch_epoch = host_secrets.token_hex(32)
        self._unbound_guardian_locations = []
        self._journal = ExecutionJournal(private / "agent_events.sqlite3")
        self._runtime_health = {}
        self._artifact_store = ArtifactStore(private / "agent_artifacts")

    def _guardian_scopes(self, workspace):
        with self._lock:
            return self._guardian_scopes_locked(workspace)

    def _guardian_scopes_locked(self, workspace):
        # Called only with _lock already held. Grant validation and publication
        # share that exact snapshot without recursively acquiring a plain Lock.
        return tuple([str(Path(workspace).resolve()), *[
            grant["path"] for grants in self._file_grants.values() for grant in grants]])

    def _check_guardian_scope(self, workspace, *, extra_grant=None, scope_snapshot=None):
        private_roots = [self._session_store.root, self._artifact_store.root]
        if self._guardian_config is not None:
            private_roots.append((self._guardian_config / "host_job_guardian").resolve())
        values = list(self._guardian_scopes(workspace) if scope_snapshot is None else scope_snapshot)
        if extra_grant is not None:
            values.append(extra_grant["path"])
        try:
            for value in values:
                scope = Path(value).resolve()
                if any(scope == private or scope.is_relative_to(private) or private.is_relative_to(scope)
                       for private in private_roots):
                    raise _error("guardian_scope_conflict")
        except AgentError:
            raise
        except Exception:
            raise _error("guardian_scope_conflict") from None

    def _guardian_storage(self, workspace):
        if sys.platform != "win32" or self._guardian_config is None:
            raise _error("guardian_unavailable")
        from .guardian_worker import PrivateGuardianLocation
        self._check_guardian_scope(workspace)
        # The current settings snapshot and ALL host grants are sampled fresh
        # at each Resume/LF/ACK; RPC/model dictionaries never supply this hook.
        def retain(owner):
            with self._lock: self._unbound_guardian_locations.append(owner)
            return True
        return PrivateGuardianLocation.prepare(self._guardian_config,
            model_scopes=lambda: self._guardian_scopes(workspace), retain_failed_owner=retain)

    def execution_resources_pending(self):
        """Read-only snapshot; unknown closure is busy, not permission to clean."""
        with self._lock:
            if (self._execution_lock.locked()
                    or self._processes or self._worker_handles
                    or self._worker_cleanup_pending or self._worker_input_writers
                    or self._active_control is not None):
                return True
        try:
            pending = self.guardian_resources_pending()
        except Exception:
            return True
        return pending if type(pending) is bool else True

    def guardian_resources_pending(self):
        from .guardian_worker import pending_cleanup_count
        from .guardian_native import has_pending_cleanup
        with self._lock:
            retained = bool(self._unbound_guardian_locations)
        return retained or bool(pending_cleanup_count()) or has_pending_cleanup()

    def _close_recovery_owner(self, owner, location):
        # PrivateRunStore.close clears fields even if audited Native.close
        # reports failure. Snapshot the original owner BEFORE that boundary,
        # then retain it forever on an unknown close; a later no-op is no proof.
        snapshot = (owner, getattr(owner,"directory",None), tuple(getattr(owner,"_pins",())))
        failed = False
        try: owner.close()
        except Exception: failed = True
        if failed or getattr(location._native,"unconfirmed_close",False):
            location._metadata_cleanup_unknown = True
            retained = getattr(location,"_recovery_owners",None)
            if retained is None:
                retained=[]; location._recovery_owners=retained
            retained.append(snapshot)
            with self._lock:
                if not any(item is location for item in self._unbound_guardian_locations):
                    self._unbound_guardian_locations.append(location)
            return False
        return True

    def recover_guardian_jobs(self, queue):
        """Restart stopped-only recovery: read/auth -> SQLite -> optional mark.

        No process lookup, launch, model call, continuation or completion. Only
        schema-v2 runs already bound in the host queue are eligible.
        """
        from .job_queue import WorkerStoppedProof
        from .job_guardian import PrivateRunStore
        with self._lock:
            if self._processes or self._unbound_guardian_locations:
                return False
        if self._guardian_config is None or sys.platform != "win32":
            return False
        location = None
        scanned = False
        try:
            cursor = None
            while True:
                bindings = queue.unclosed_guardian_bindings(after_job_id=cursor)
                if not bindings: break
                if location is None: location = self._guardian_storage(_workspace_root())
                for binding in bindings:
                    cursor = binding.job_id
                    store = None
                    try:
                        store = PrivateRunStore(location._path / binding.run_nonce, location._native)
                        manifest, _, _ = store.identity()
                        if not binding.matches_manifest(manifest): continue
                        evidence = store.read_closure(expected_manifest=manifest)
                        if (evidence is None or evidence.manifest_sha256 != binding.manifest_sha256
                                or evidence.run_nonce != binding.run_nonce): continue
                        def verifier(current):
                            ref = binding.reference
                            if (current.conversation_id, current.generation, current.user_message_id, current.idempotency_key,
                                current.settings_scope, current.origin_peer_id) != (ref.conversation_id, ref.generation,
                                ref.user_message_id, ref.idempotency_key, ref.settings_scope, ref.origin_peer_id):
                                return None
                            return WorkerStoppedProof(current.job_id, ref.conversation_id, ref.generation, ref.user_message_id, True)
                        closed = queue.reconcile_worker_stopped(binding.job_id, guardian_binding=binding, verifier=verifier)
                        if closed.worker_closed_at is not None:
                            # A crash before this optional housekeeping leaves
                            # a reusable proof of the SAME already-closed run.
                            store.consume_closure(expected_manifest=manifest)
                    except Exception:
                        # A missing/ambiguous commit never consumes evidence.
                        # The queue's unresolved global barrier stays in place.
                        continue
                    finally:
                        if store is not None: self._close_recovery_owner(store,location)
                    if getattr(location,"_metadata_cleanup_unknown",False): break
                if location is not None and getattr(location,"_metadata_cleanup_unknown",False): break
                if len(bindings) < 100: break
            scanned = True
        except Exception:
            scanned = False
        finally:
            if location is not None: self._close_recovery_owner(location,location)
        return scanned and not self.guardian_resources_pending()

    def artifacts(self, conversation_id):
        return self._artifact_store.list(conversation_id) if self._artifact_store is not None else []

    def artifact_page(self, conversation_id, reference, *, offset=0, limit=262144):
        if self._artifact_store is None:
            raise ValueError("ARTIFACT_UNAVAILABLE")
        return self._artifact_store.get(conversation_id, reference, offset=offset, limit=limit)

    def artifact_snapshot(self, conversation_id, reference):
        """Host-only desktop export path; never registered as a remote/tool RPC."""
        if self._artifact_store is None:
            raise ValueError("ARTIFACT_UNAVAILABLE")
        return self._artifact_store.verified_snapshot(conversation_id, reference)

    def pending_interaction(self, conversation_id=None):
        return self._interaction_broker.public(conversation_id)

    def _write_control(self, process, frame):
        with self._input_lock:
            process.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
            process.stdin.flush()

    def answer_interaction(self, conversation_id, identity, answer):
        """Called only by the host UI or an authenticated paired peer."""
        with self._lock:
            active = self._active_control
            if active is None or active["conversation_id"] != conversation_id or active["cancel"].is_set():
                return False
            response = self._interaction_broker.answer(conversation_id, identity, answer)
            if response is None or response["task_id"] != active["task_id"]:
                return False
            result = response["answer"]
            grant = result.get("grant")
            if grant is not None:
                try:
                    workspace = active.get("workspace", _workspace_root())
                    self._check_guardian_scope(workspace, extra_grant=grant,
                        scope_snapshot=self._guardian_scopes_locked(workspace))
                except AgentError:
                    result = {"allowed": False, "text": "该范围包含应用私有任务记录，未授予文件访问权限。"}
                    response["answer"] = result
                    grant = None
            if grant is not None:
                collection = self._file_grants.setdefault((conversation_id, active["scope"]), [])
                if grant not in collection:
                    collection.append(grant)
            process = active["process"]
        try:
            self._write_control(process, {"type": "interaction_reply", "id": response["task_id"],
                "interaction_id": identity, "answer": result})
            return True
        except (OSError, ValueError, BrokenPipeError):
            return False

    def steer(self, conversation_id, text):
        if (not isinstance(text, str) or not text.strip() or len(text) > 8000
                or any(ord(ch) < 32 and ch not in "\n\t" for ch in text)):
            return False
        with self._lock:
            active = self._active_control
            if active is None or active["conversation_id"] != conversation_id or active["cancel"].is_set():
                return False
            process, task_id = active["process"], active["task_id"]
            value = _redact(text.strip(), active["secrets"])
        try:
            self._write_control(process, {"type": "steer", "id": task_id, "text": value})
            return True
        except (OSError, ValueError, BrokenPipeError):
            return False

    @property
    def checkpoint_status(self):
        return self._checkpoint_status

    def _flush_checkpoint_deletions(self):
        with self._lock:
            pending = list(self._checkpoint_deletions)
        for future, conversation_id in pending:
            try:
                future.result()
            except Exception:
                try:
                    # Retry erasure only; never replay an interrupted task.
                    with self._execution_lock:
                        self._delete_session(conversation_id)
                except Exception:
                    self._checkpoint_status = _ERROR_MESSAGES["session_unavailable"]
                    raise _error("session_unavailable") from None
        with self._lock:
            self._checkpoint_deletions = [item for item in self._checkpoint_deletions if item not in pending]

    def _delete_session(self, conversation_id):
        # Called while holding _execution_lock. An unconfirmed old process
        # could still recreate files, so erasure must wait for real closure.
        with self._lock:
            if self._processes or self._worker_handles or self._worker_cleanup_pending:
                raise _error("busy")
        self._session_store.clear(conversation_id)
        self._journal.clear(conversation_id)
        self._artifact_store.clear(conversation_id)

    def checkpoint_info(self, conversation_id, settings, api, *, secrets=(), generation=""):
        conversation_uuid(conversation_id)
        return {"backend": "deepseek-harness", "status": "native", "goal_verified": False}

    def clear_session(self, conversation_id=None):
        if conversation_id is not None:
            conversation_uuid(conversation_id)
        with self._lock:
            if conversation_id is None:
                self._session_epoch += 1
                self._session_epochs.clear()
            else:
                self._session_epochs[conversation_id] = self._session_epochs.get(conversation_id, 0) + 1
            active = self._active_control
            if active is not None and (conversation_id is None or active["conversation_id"] == conversation_id):
                active["cancel"].set()
            self._file_grants = {key: value for key, value in self._file_grants.items()
                                 if conversation_id is not None and key[0] != conversation_id}
            if self._checkpoint_executor is None:
                self._checkpoint_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dsh-session-delete")
            def remove():
                # A terminating runner must not recreate the just-deleted home.
                with self._execution_lock:
                    self._delete_session(conversation_id)
            future = self._checkpoint_executor.submit(remove)
            self._checkpoint_deletions.append((future, conversation_id))
            return future

    @staticmethod
    def capabilities(settings, gui_api_key="", *, api=None):
        tools = bool(settings.enabled and settings.tools_enabled)
        computer = bool(settings.enabled and settings.computer_enabled and sys.platform == "win32")
        return [{"name": name, "available": computer if name in COMPUTER_TOOLS else tools,
                 "reason_code": "" if (computer if name in COMPUTER_TOOLS else tools) else "kind_disabled"}
                for name in DSH_TOOL_NAMES if name != ("bash" if sys.platform == "win32" else "pwsh")]

    def readiness(self, settings, key="", context=None):
        try:
            if not isinstance(settings, AgentSettings) or not isinstance(context, dict):
                raise _error("not_configured")
            settings = shared_api_settings(settings, context.get("api"))
            settings.validate()
            if self._closed.is_set():
                raise _error("closed")
            if not settings.enabled:
                raise _error("disabled")
            node = node_executable()
            if node is None or not node.is_file():
                raise _error("gui_runtime_missing")
            if not _WORKER_PATH.is_file():
                raise _error("missing_backend")
            if any(version != _DSH_VERSION for version in dsh_installation().values()):
                raise _error("missing_sdk")
            self._context(context, key, AgentRequest("readiness", "agent"), settings)
            manifest = self.capabilities(settings, key, api=context["api"])
            return {"available": True, "configured": True,
                    "runtime_ready": self._runtime_health.get(str(node)), "reason_code": "", "reason": "",
                    "tools": [item["name"] for item in manifest if item["available"]], "capabilities": manifest}
        except (AgentError, AgentValidationError, OSError) as exc:
            code = exc.code if isinstance(exc, AgentError) else "not_configured"
            return {"available": False, "reason_code": code, "reason": _ERROR_MESSAGES[code],
                    "tools": [], "capabilities": []}

    def cancel_request(self, cancellation_event):
        cancellation_event.set()
        with self._lock:
            handles = [self._worker_handles[pid] for pid, (event, tree) in self._processes.items()
                       if event is cancellation_event and pid in self._worker_handles]
        for handle in handles:
            self._request_worker_stop(handle)

    def _request_worker_stop(self, handle):
        with self._lock:
            control = self._worker_cancel_senders.get(handle)
        if control is None:
            # No complete run command has been committed, or the bounded
            # input writer owns a failed/ambiguous commit. Do not hold the
            # service lock while invoking a native cleanup callback.
            self._worker_lifecycle.request_stop(handle)
            return
        with self._lock:
            if control["requested"] is not None or control["ack"] is not None: return
            control["requested"] = time.monotonic()
            timer = threading.Timer(_GRACEFUL_STOP_SECONDS,
                                    lambda: self._worker_lifecycle.request_stop(handle))
            timer.daemon = True
            control["timer"] = timer
            def send():
                try: self._write_control(handle._process, {"type": "cancel", "id": handle.run_id})
                except Exception: pass
            sender = threading.Thread(target=send, name="dsh-graceful-stop", daemon=True)
            control["sender"] = sender
            timer.start()
            sender.start()

    def _acknowledge_stopped(self, handle, frame):
        with self._lock:
            control = self._worker_cancel_senders.get(handle)
            if control is None or control["ack"] is not None: raise _error("protocol")
        if frame["computer_settled"] and control["guardian_store"] is not None:
            try: control["guardian_store"].mark_external_cleanup_confirmed()
            except Exception: raise _error("stop_unconfirmed") from None
        with self._lock:
            control["ack"] = frame["computer_settled"]
        if frame["computer_settled"]:
            self._worker_lifecycle.confirm_external_cleanup(handle)

    def _graceful_stop(self, handle, buffer):
        """Keep the private channel alive until SDK/native cleanup is proven."""
        with self._lock: control = self._worker_cancel_senders.get(handle)
        if control is None: return
        if control["ack"] is None:
            self._request_worker_stop(handle)
            deadline = control["requested"] + _GRACEFUL_STOP_SECONDS
            while control["ack"] is None and time.monotonic() < deadline:
                try: chunk = _read_available(handle._process.stdout)
                except (OSError, ValueError): break
                buffer += chunk
                if len(buffer) > _MAX_LINE and b"\n" not in buffer: break
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if not raw.strip(): continue
                    try:
                        frame = self._parse_frame(raw, handle.run_id)
                        if frame["type"] == "stopped": self._acknowledge_stopped(handle, frame)
                    except AgentError: continue
                if control["ack"] is not None: break
                if handle._process.poll() is not None and not chunk: break
                time.sleep(.01)
        timer, sender = control["timer"], control["sender"]
        if timer is not None: timer.cancel()
        if sender is not None:
            sender.join(.1)
            if sender.is_alive():
                self._worker_lifecycle.request_stop(handle)
                sender.join(1)

    def worker_closure(self, handle):
        """Host-only opaque proof; no remote/model dictionaries are accepted."""
        return self._worker_lifecycle.receipt(handle)

    def validate_worker_closure(self, handle, receipt):
        return self._worker_lifecycle.validates(handle, receipt)

    def validate_execution_attempt(self, attempt):
        """Host-only invocation identity, never a remote authorization grant."""
        return self._execution_attempts.valid(attempt)

    def execution_no_launch(self, attempt):
        return self._execution_attempts.receipt(attempt)

    def bind_execution_attempt(self, attempt, owner):
        return self._execution_attempts.bind(attempt, owner)

    def validate_execution_no_launch(self, attempt, receipt, *, owner=None):
        return self._execution_attempts.validates(attempt, receipt, owner=owner)

    def _settle_worker(self, handle, *, timeout_seconds=2.0):
        with self._lock:
            self._worker_cleanup_pending.add(handle)
            writer = self._worker_input_writers.get(handle)
            control = self._worker_cancel_senders.get(handle)
        if control is not None and control["sender"] is not None and control["sender"].is_alive():
            self._worker_lifecycle.request_stop(handle)
            control["sender"].join(min(timeout_seconds, 1.0))
            if control["sender"].is_alive(): return None
        if writer is not None:
            try: joined = writer.close(timeout_seconds=min(timeout_seconds,1.0))
            except Exception: joined = False
            if not joined:
                self._worker_lifecycle.request_stop(handle)
                return None
            with self._lock: self._worker_input_writers.pop(handle, None)
        receipt = self._worker_lifecycle.settle(handle, timeout_seconds=timeout_seconds)
        if receipt is not None:
            with self._lock:
                for pid, current in list(self._worker_handles.items()):
                    if current is handle:
                        self._worker_handles.pop(pid, None)
                        self._processes.pop(pid, None)
                self._worker_cleanup_pending.discard(handle)
                self._worker_cancel_senders.pop(handle, None)
            for pipe in (handle._process.stdin, handle._process.stdout):
                if pipe is not None:
                    try: pipe.close()
                    except Exception: pass
        return receipt

    def reconcile_worker_closures(self, *, timeout_seconds=.2):
        """Explicit bounded host retry of cleanup, never Agent task replay."""
        with self._lock:
            for location in tuple(self._unbound_guardian_locations):
                # Lost-wrapper close failures stay sticky. An exact retained
                # directory handle can be retried without reopening a path.
                if location._native.unconfirmed_close or getattr(location,"_metadata_cleanup_unknown",False): continue
                handle = location._directory.directory
                if not handle or location._native.k.CloseHandle(handle):
                    location._directory.directory = 0
                    self._unbound_guardian_locations.remove(location)
            # Never turn a coordinator's cleanup poll into cancellation of a
            # currently running task. Only the execute-finally path places a
            # handle in this recovery set.
            handles = [handle for handle in self._worker_handles.values()
                       if handle in self._worker_cleanup_pending]
        return tuple(receipt for handle in handles
                     if (receipt := self._settle_worker(handle, timeout_seconds=timeout_seconds)) is not None)

    def shutdown(self):
        self._closed.set()
        with self._lock:
            active = [(event, self._worker_handles[pid]) for pid, (event, tree) in self._processes.items()
                      if pid in self._worker_handles]
            self._session_epoch += 1
        for event, handle in active:
            event.set()
            self._request_worker_stop(handle)
        if self._checkpoint_executor is not None:
            self._checkpoint_executor.shutdown(wait=False, cancel_futures=False)

    @staticmethod
    def _context(context, key, request, settings):
        if not isinstance(context, dict):
            raise AgentValidationError("Agent 上下文格式无效。")
        try:
            if context.get("conversation_id") is not None:
                conversation_uuid(context["conversation_id"])
            if context.get("conversation_generation"):
                conversation_uuid(context["conversation_generation"])
            current_image(context.get("current_image_url"))
        except ValueError:
            raise AgentValidationError("Agent 会话或图片无效。") from None
        api = context.get("api")
        if is_dataclass(api):
            api = asdict(api)
        if not isinstance(api, dict):
            raise AgentValidationError("Agent 模型配置格式无效。")
        validate_api(api.get("base_url"), api.get("model"))
        if (not isinstance(key, str) or not key.strip() or len(key) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in key)):
            raise _error("not_configured")
        secrets = (key,)
        profile, effort = api.get("reasoning_profile", "auto"), api.get("reasoning_effort", "default")
        budget, metadata = api.get("reasoning_budget_tokens", 0), api.get("reasoning_metadata")
        try:
            validate_selection(api["base_url"], api["model"], value=effort, profile=profile,
                               budget_tokens=budget, metadata=metadata)
            capability = resolve_reasoning(api["base_url"], api["model"], profile=profile, metadata=metadata)
        except ReasoningError:
            raise AgentValidationError("Agent 思考配置与当前模型不兼容，请检查模型设置。") from None
        config = {"base_url": api["base_url"], "model": api["model"], "api_key": key,
                  "protocol": "openai-completions",
                  "reasoning": {"profile": capability.id, "value": effort, "budget_tokens": budget,
                                "values": list(capability.values), "control": capability.control}}
        messages = context.get("messages", [])
        if not isinstance(messages, list):
            raise AgentValidationError("Agent 参考消息无效。")
        clean = []
        for item in messages:
            if not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant"} or not isinstance(item.get("content"), str):
                raise AgentValidationError("Agent 参考消息必须为文本消息。")
            # The ordinary-chat system prompt contains a JSON reply contract,
            # not the DSH tool protocol. Never import it as harness policy.
            if item["role"] != "system":
                clean.append({"role": item["role"], "content": _redact(item["content"], secrets)})
        safe = {"messages": clean}
        personality = context.get("personality")
        if isinstance(personality, str) and personality.strip():
            safe["personality"] = _redact(personality[:24000], secrets)
        catalog = context.get("image_catalog", [])
        if (not isinstance(catalog, list) or len(catalog) > 64 or any(
                not isinstance(item, dict) or not isinstance(item.get("attachment_id"), str)
                or not re.fullmatch(r"[a-f0-9-]{24,80}", item["attachment_id"])
                or not isinstance(item.get("label"), str) or len(item["label"]) > 240 for item in catalog)):
            raise AgentValidationError("图片参考资料无效。")
        safe["image_catalog"] = [{"attachment_id": item["attachment_id"],
                                  "label": _redact(item["label"], secrets)} for item in catalog]
        image = current_image(context.get("current_image_url"))
        if image:
            safe["current_image_url"] = image
            safe["current_image_id"] = context.get("current_image_id")
        window = context.get("model_context_window")
        if window is not None:
            if type(window) is not int or not 1024 <= window <= 100000000:
                raise AgentValidationError("模型窗口资料无效。")
            config["context_window"] = window
        return config, safe, secrets

    def execute(self, settings, key, request, context, cancellation_event=None, progress_callback=None,
                event_callback=None, interaction_callback=None, image_resolver=None, diagnostic_callback=None,
                worker_lifecycle_callback=None, worker_dispatch_guard=None, execution_attempt_callback=None,
                guardian_launch=None):
        if request is None:
            return None
        identity = context.get("conversation_id") if isinstance(context, dict) else None
        try:
            if identity is not None: conversation_uuid(identity)
        except ValueError:
            identity = None
        attempt = self._execution_attempts.begin(identity)
        acquired, handles = False, []
        def bound(handle):
            handles.append(handle)
            if callable(worker_lifecycle_callback): worker_lifecycle_callback(handle)
        try:
            if callable(execution_attempt_callback): execution_attempt_callback(attempt)
            self._flush_checkpoint_deletions()
            if not self._execution_lock.acquire(blocking=False):
                raise _error("busy")
            acquired = True
            outcome = self._execute_impl(settings, key, request, context, cancellation_event,
                progress_callback, event_callback, interaction_callback, image_resolver,
                diagnostic_callback, bound, worker_dispatch_guard, execution_attempt=attempt,
                guardian_launch=guardian_launch)
            with self._lock:
                unresolved = any(handle in self._worker_cleanup_pending for handle in handles)
            if unresolved:
                outcome = replace(outcome, status="needs_user",
                                  reply=outcome.reply + "\n\n后台工具仍在结束中，请稍后继续。")
            return outcome
        finally:
            if acquired: self._execution_lock.release()
            try: self._execution_attempts.finish(attempt)
            except Exception: pass

    def _execute_impl(self, settings, key, request, context, cancellation_event=None,
                      progress_callback=None, event_callback=None, interaction_callback=None,
                      image_resolver=None, diagnostic_callback=None, worker_lifecycle_callback=None,
                      worker_dispatch_guard=None, *, execution_attempt=None, guardian_launch=None):
        if not isinstance(request, AgentRequest) or not isinstance(settings, AgentSettings):
            raise AgentValidationError("Agent 请求或设置无效。")
        request.validate()
        settings = shared_api_settings(settings, context.get("api") if isinstance(context, dict) else None)
        settings.validate()
        from .guardian_launch import valid as valid_guardian_launch
        if guardian_launch is not None and not valid_guardian_launch(guardian_launch):
            raise _error("guardian_unavailable")
        if self._closed.is_set():
            raise _error("closed")
        with self._lock:
            if self._processes or self._unbound_guardian_locations:
                raise _error("busy")
        if self.guardian_resources_pending():
            raise _error("busy")
        if not settings.enabled:
            raise _error("disabled")
        if ((request.kind == "tools" and not settings.tools_enabled)
                or (request.kind == "computer" and not settings.computer_enabled)
                or not (settings.tools_enabled or settings.computer_enabled)):
            raise _error("kind_disabled")
        event = cancellation_event or threading.Event()
        if event.is_set():
            raise _error("cancelled")
        cid = context.get("conversation_id") if isinstance(context, dict) else None
        cid = cid or str(uuid4())
        with self._lock:
            epoch = (self._session_epoch, self._session_epochs.get(cid, 0))
        def ensure_current():
            with self._lock:
                current = epoch == (self._session_epoch, self._session_epochs.get(cid, 0))
            if not current or event.is_set() or self._closed.is_set():
                raise _error("cancelled")
        config, safe_context, secrets = self._context(context, key, request, settings)
        ensure_current()
        executable = node_executable()
        if executable is None or not executable.is_file():
            raise _error("gui_runtime_missing")
        if not _WORKER_PATH.is_file():
            raise _error("missing_backend")
        if any(version != _DSH_VERSION for version in dsh_installation().values()):
            raise _error("missing_sdk")
        task_id = uuid4().hex
        scope = session_scope(settings, config, generation=context.get("conversation_generation", ""), key=key)
        try:
            storage = self._session_store.prepare(cid, scope)
        except (OSError, ValueError):
            raise _error("session_unavailable") from None
        if guardian_launch is not None:
            ref = guardian_launch.reference
            if ((cid, context.get("conversation_generation")) != (ref.conversation_id, ref.generation)
                    or guardian_launch.validate_attempt(execution_attempt) is not True):
                raise _error("guardian_unavailable")
        options = {"workspace": str(Path(settings.workspace or _workspace_root()).resolve()),
                   "tools_enabled": settings.tools_enabled, "computer_enabled": settings.computer_enabled,
                   "max_steps": settings.max_steps, "timeout_seconds": settings.timeout_seconds}
        self._check_guardian_scope(options["workspace"])
        payload = {"type": "run", "id": task_id, "kind": request.kind,
                   "task": _redact(request.task, secrets), "conversation_id": cid,
                   "config": config, "context": safe_context, "options": options, "storage": storage}
        raw_input = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        environment = worker_environment(storage["home"])
        launch = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                  if sys.platform == "win32" else {"start_new_session": True})
        process = tree = run_handle = result = None
        buffer = b""
        terminal_error = "interrupted"
        deadline = time.monotonic() + settings.timeout_seconds if settings.timeout_seconds else None

        def notify(phase):
            if callable(progress_callback) and phase in PROGRESS_MESSAGES:
                try: progress_callback(phase)
                except Exception: pass

        notify("starting")
        try:
            ensure_current()
            argv = [str(executable), str(_WORKER_PATH)]
            if guardian_launch is None:
                self._execution_attempts.mark_launch_attempted(execution_attempt)
                process = subprocess.Popen(argv, cwd=str(_ROOT), env=environment, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, **launch)
                run_handle = self._worker_lifecycle.register(process, task_id, cid)
                with self._lock:
                    self._processes[process.pid] = (event, None)
                    self._worker_handles[process.pid] = run_handle
                if callable(worker_lifecycle_callback): worker_lifecycle_callback(run_handle)
                tree = _ProcessTree(process)
                self._worker_lifecycle.attach_tree(run_handle, tree)
            else:
                from .guardian_worker import GuardianWorker, owns_location
                def native_bound(current):
                    nonlocal process, tree, run_handle
                    process, tree = current, current.tree
                    run_handle = self._worker_lifecycle.register(current, task_id, cid)
                    self._worker_lifecycle.attach_tree(run_handle, tree)
                    with self._lock:
                        self._processes[current.pid] = (event, tree)
                        self._worker_handles[current.pid] = run_handle
                    if callable(worker_lifecycle_callback): worker_lifecycle_callback(run_handle)
                location = self._guardian_storage(options["workspace"])
                try:
                    process = GuardianWorker.create(argv, cwd=_ROOT, location=location,
                        job_id=guardian_launch.job_id, reference=guardian_launch.reference,
                        installation_id=hashlib.sha256(str(_ROOT.resolve()).encode("utf-8")).hexdigest(),
                        launch_epoch=self._guardian_launch_epoch, environment=environment, owns_location=True,
                        stderr_to_null=True, job_active_limit=None,
                        launch_attempt_callback=lambda: self._execution_attempts.mark_launch_attempted(execution_attempt),
                        lifecycle_callback=native_bound, manifest_commit_callback=guardian_launch.commit_manifest)
                except BaseException:
                    if not owns_location(location) and location._directory.directory:
                        native = location._native
                        if not native.unconfirmed_close and native.k.CloseHandle(location._directory.directory):
                            location._directory.directory = 0
                        else:
                            with self._lock: self._unbound_guardian_locations.append(location)
                    raise
            with self._lock:
                self._processes[process.pid] = (event, tree)
                self._active_control = {"process": process, "task_id": task_id, "conversation_id": cid,
                    "scope": scope, "cancel": event, "workspace": options["workspace"], "secrets": secrets}
            ensure_current()
            if guardian_launch is not None:
                with (worker_dispatch_guard(run_handle) if callable(worker_dispatch_guard) else nullcontext()):
                    ensure_current()
                    self._check_guardian_scope(options["workspace"])
                    process.resume()
            buffer = b""
            startup_deadline = min(deadline or float("inf"), time.monotonic() + 30)
            while b"\n" not in buffer:
                if event.is_set() or self._closed.is_set(): raise _error("cancelled")
                if time.monotonic() >= startup_deadline: raise _error("worker_startup_timeout")
                buffer += _read_available(process.stdout)
                if len(buffer) > _MAX_LINE: raise _error("protocol")
                if process.poll() is not None and b"\n" not in buffer: raise _error("backend_failed")
                if b"\n" not in buffer: event.wait(.01)
            raw, buffer = buffer.split(b"\n", 1)
            ready = self._parse_frame(raw, task_id)
            if (ready.get("type") != "ready" or ready.get("protocol") != 1
                    or ready.get("sdk") != "deepseek-harness"):
                raise _error("protocol")
            writer = StagedRunInput(process.stdin, stop_owned=lambda: self._worker_lifecycle.request_stop(run_handle),
                                    cancellation_event=event)
            if guardian_launch is not None: process.bind_writer(writer)
            with self._lock: self._worker_input_writers[run_handle] = writer
            writer.start()
            writer.stage(raw_input)
            with (worker_dispatch_guard(run_handle) if callable(worker_dispatch_guard) else nullcontext()):
                ensure_current()
                self._check_guardian_scope(options["workspace"])
                if guardian_launch is not None: process.validate_private_scope()
                if options["computer_enabled"]:
                    if guardian_launch is not None: process.store.mark_external_cleanup_required()
                    self._worker_lifecycle.require_external_cleanup(run_handle)
                self._worker_lifecycle.mark_dispatched(run_handle)
                writer.commit()
            if not writer.close(): raise _error("backend_failed")
            with self._lock:
                self._worker_cancel_senders[run_handle] = {"requested": None, "timer": None,
                    "sender": None, "ack": None,
                    "guardian_store": process.store if guardian_launch is not None and options["computer_enabled"] else None}
            while True:
                if event.is_set() or self._closed.is_set(): raise _error("cancelled")
                if deadline is not None and time.monotonic() >= deadline: raise _error("timeout")
                expired = self._interaction_broker.expired()
                if expired is not None:
                    self._write_control(process, {"type": "interaction_reply", "id": task_id,
                        "interaction_id": expired["interaction_id"], "answer": expired["answer"]})
                    if callable(interaction_callback): interaction_callback(None)
                chunk = _read_available(process.stdout)
                buffer += chunk
                if b"\n" not in buffer and len(buffer) > _MAX_LINE: raise _error("protocol")
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if not raw.strip(): continue
                    if len(raw) > _MAX_LINE: raise _error("protocol")
                    frame = self._parse_frame(raw, task_id)
                    kind = frame["type"]
                    if kind == "progress":
                        notify(frame["phase"])
                    elif kind == "activity":
                        value = public_activity(frame)
                        if callable(event_callback):
                            try: event_callback(value)
                            except Exception: pass
                    elif kind == "session":
                        with self._lock:
                            valid_epoch = epoch == (self._session_epoch, self._session_epochs.get(cid, 0))
                        if not valid_epoch: raise _error("cancelled")
                        try: self._session_store.save(cid, scope, frame["dsh_session_id"])
                        except (OSError, ValueError): raise _error("session_unavailable") from None
                    elif kind == "interaction":
                        try: pending = checked_interaction(frame, secrets=secrets)
                        except (ValueError, OSError):
                            self._write_control(process, {"type": "interaction_reply", "id": task_id,
                                "interaction_id": frame["interaction_id"], "answer": {"allowed": False}})
                            continue
                        pending = self._interaction_broker.open(pending, conversation_id=cid, task_id=task_id)
                        if callable(interaction_callback): interaction_callback(pending)
                    elif kind == "resource_request":
                        value = None
                        listed = {item["attachment_id"] for item in safe_context["image_catalog"]}
                        if frame["reference"] in listed and callable(image_resolver) and not event.is_set():
                            try: value = current_image(image_resolver(frame["reference"]))
                            except Exception: pass
                        self._write_control(process, {"type": "resource_reply", "id": task_id,
                            "resource_id": frame["resource_id"], "value": value})
                    elif kind == "diagnostic":
                        if callable(diagnostic_callback):
                            try: diagnostic_callback(frame["data"])
                            except Exception: pass
                    elif kind == "stopped":
                        self._acknowledge_stopped(run_handle, frame)
                    elif kind == "error":
                        code = frame["code"].casefold()
                        code = {"dsh_session_unavailable": "session_unavailable", "dsh_missing": "missing_sdk",
                                "dsh_runtime_missing": "missing_sdk", "dsh_runtime_unsupported": "gui_runtime_missing",
                                "dsh_model_error": "provider_unavailable",
                                "dsh_execution_failed": "agent_failed", "computer_helper_missing": "gui_sdk_unavailable",
                                "dsh_stop_unconfirmed": "stop_unconfirmed",
                                "missing_node": "gui_runtime_missing", "timed_out": "timeout"}.get(code, code)
                        raise _error(code if code in _ERROR_MESSAGES else "backend_failed")
                    elif kind == "final":
                        if result is not None: raise _error("protocol")
                        if event.is_set() or self._closed.is_set(): raise _error("cancelled")
                        with self._lock: stop_control = self._worker_cancel_senders[run_handle]
                        if stop_control["ack"] is not True: raise _error("stop_unconfirmed")
                        status = frame.get("status", "completed")
                        if status in {"cancelled", "timed_out"}:
                            raise _error("cancelled" if status == "cancelled" else "timeout")
                        deliveries, failed = [], False
                        for artifact in frame.get("artifacts", []):
                            try:
                                deliveries.append(self._artifact_store.register_path(cid, artifact["path"],
                                    workspace=options["workspace"], presented=True,
                                    expected_hash=artifact.get("sha256"), expected_size=artifact.get("size_bytes"),
                                    private_roots=(self._session_store.root, self._artifact_store.root,
                                                   self._guardian_config / "host_job_guardian"),
                                    private_profile=self._guardian_config))
                            except (ValueError, OSError): failed = True
                        # DSH owns completion. Do not replace its reply using a
                        # second model judge or legacy finish_task/GUI receipt.
                        reply = _redact(frame["reply"], secrets)
                        if failed:
                            reply += "\n\n部分文件未能保存为下载副本，原文件已保留。"
                            status = "needs_user"
                        result = AgentResult(reply, frame.get("emotion", "neutral"), request.kind,
                                             status=status, artifacts=tuple(deliveries))
                        self._runtime_health[str(executable)] = True
                if result is not None:
                    if result.status == "completed": notify("completed")
                    return result
                if process.poll() is not None and not chunk:
                    raise _error("protocol" if buffer.strip() else "backend_failed")
                event.wait(.02)
        except StagedInputError as exc:
            code = {"WORKER_INPUT_CANCELLED": "cancelled", "WORKER_INPUT_STAGE_TIMEOUT": "worker_input_stage_timeout",
                    "WORKER_INPUT_COMMIT_TIMEOUT": "worker_input_commit_timeout",
                    "WORKER_INPUT_WRITE_FAILED": "worker_input_write_failed"}.get(exc.code, "backend_failed")
            terminal_error = code
            raise _error(code) from None
        except (AgentError, AgentValidationError) as exc:
            terminal_error = exc.code if isinstance(exc, AgentError) else "invalid_request"
            raise
        except Exception:
            terminal_error = "cancelled" if event.is_set() or self._closed.is_set() else "backend_failed"
            raise _error(terminal_error) from None
        finally:
            if run_handle is not None:
                try: self._graceful_stop(run_handle, buffer)
                except Exception: pass  # A broken cleanup channel is never positive evidence.
                self._worker_lifecycle.request_stop(run_handle)
            self._interaction_broker.clear(task_id)
            with self._lock:
                if self._active_control is not None and self._active_control["task_id"] == task_id:
                    self._active_control = None
            if callable(interaction_callback):
                try: interaction_callback(None)
                except Exception: pass
            if process is not None:
                receipt = self._settle_worker(run_handle) if run_handle is not None else None
                if run_handle is None:
                    # Popen succeeded but host registration failed. No task
                    # frame was sent; still close the exact process we own.
                    try:
                        if process.poll() is None: process.kill()
                        process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired): pass
                    for pipe in (process.stdin, process.stdout):
                        if pipe is not None:
                            try: pipe.close()
                            except OSError: pass
                if receipt is None and callable(diagnostic_callback):
                    try: diagnostic_callback({"phase": "runtime", "code": "WORKER_CLEANUP_UNCONFIRMED"})
                    except Exception: pass
                if run_handle is not None and self._worker_lifecycle.external_cleanup_pending(run_handle):
                    raise _error("stop_unconfirmed")

    @staticmethod
    def _parse_frame(raw, task_id):
        try:
            frame = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_unique)
            if not isinstance(frame, dict) or not isinstance(frame.get("type"), str): raise ValueError()
            kind = frame["type"]
            if kind == "ready":
                if frame.get("id") not in (None, task_id): raise ValueError()
                return frame
            if frame.get("id") != task_id: raise ValueError()
            if kind == "progress":
                if frame.get("phase") not in PROGRESS_MESSAGES: raise ValueError()
            elif kind == "activity":
                if (frame.get("kind") not in {"tool", "computer", "progress"}
                        or not isinstance(frame.get("name"), str) or len(frame["name"]) > 160
                        or frame.get("status") not in {"running", "completed", "failed"}
                        or not isinstance(frame.get("event_id"), str)
                        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", frame["event_id"])
                        or "step" in frame and (type(frame["step"]) is not int or frame["step"] < 0)):
                    raise ValueError()
            elif kind == "session":
                if not valid_session_id(frame.get("dsh_session_id")): raise ValueError()
            elif kind == "stopped":
                if type(frame.get("computer_settled")) is not bool: raise ValueError()
            elif kind == "interaction":
                checked_interaction(frame, resolve_resource=False)
            elif kind == "resource_request":
                if (frame.get("resource_kind") != "image"
                        or not isinstance(frame.get("resource_id"), str)
                        or not re.fullmatch(r"resource-[a-f0-9]{32}", frame["resource_id"])
                        or not isinstance(frame.get("reference"), str)
                        or not re.fullmatch(r"[a-f0-9-]{24,80}", frame["reference"])):
                    raise ValueError()
            elif kind == "diagnostic":
                data = frame.get("data")
                if (not isinstance(data, dict) or set(data) - {"phase", "code", "duration_ms", "attempt", "input_tokens", "output_tokens"}
                        or data.get("phase") not in {"model", "tool", "context", "runtime"}
                        or not isinstance(data.get("code", ""), str)
                        or not re.fullmatch(r"[A-Z0-9_]{0,80}", data.get("code", ""))
                        or any(type(value) is not int or value < 0 for key, value in data.items() if key not in {"phase", "code"})):
                    raise ValueError()
            elif kind == "final":
                if (not isinstance(frame.get("reply"), str) or not frame["reply"].strip()
                        or frame.get("emotion", "neutral") not in _EMOTIONS
                        or frame.get("status", "completed") not in {"completed", "needs_user", "cancelled", "timed_out"}):
                    raise ValueError()
                artifacts = frame.get("artifacts", [])
                if (not isinstance(artifacts, list) or len(artifacts) > 64 or any(
                        not isinstance(item, dict) or not isinstance(item.get("path"), str) or len(item["path"]) > 4096
                        or "sha256" in item and (not isinstance(item["sha256"], str)
                                                 or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]))
                        or "size_bytes" in item and (type(item["size_bytes"]) is not int
                                                      or not 0 <= item["size_bytes"] <= 16 * 1024 * 1024)
                        for item in artifacts)):
                    raise ValueError()
            elif kind == "error":
                if not isinstance(frame.get("code"), str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", frame["code"]):
                    raise ValueError()
            else:
                raise ValueError()
            return frame
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise _error("protocol") from None
