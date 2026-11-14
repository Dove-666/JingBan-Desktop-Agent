"""Bounded, on-demand nanobot subprocess host, independent of Qt and SDKs."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import secrets as host_secrets
import threading
import time
from uuid import UUID, uuid4

from .contracts import AgentError, AgentRequest, AgentResult, AgentValidationError
from .settings import AGENT_WORKSPACE_ROOT, DEFAULT_PYTHON, AgentSettings, shared_api_settings, validate_api
from ..bundle_runtime import frozen_workspace_root
from ..reasoning import ReasoningError, reasoning_body, validate_selection
from .activity import TOOL_LABELS, ACTION_LABELS, public_activity
from .checkpoints import CheckpointError, CheckpointStore, scope_fingerprint, legacy_scope_fingerprint
from .interactions import InteractionBroker, checked_interaction
from .journal import ExecutionJournal
from .artifacts import ArtifactStore
from .process_lifecycle import WorkerLifecycle
from .execution_attempt import ExecutionAttempts
from .staged_input import StagedRunInput, StagedInputError
from agent_backend.nanobot.tool_catalog import CATALOG
from agent_backend.nanobot.redaction import redact_text
from agent_backend.nanobot.acceptance_report import checked_assessment
from agent_backend.nanobot.partial_delivery import FAILURE_DIAGNOSTIC_CODES
from agent_backend.nanobot.goal_judge import JUDGE_DIAGNOSTIC_CODES
from agent_backend.nanobot.goal_history import GoalHistoryError, checked_goal_history, legacy_goal_history
from agent_backend.nanobot.session_history import (
    bounded_history, close_interrupted_tools, conversation_uuid, current_image, text_record,
)


_ROOT = Path(__file__).resolve().parents[2]
_WORKER_PATH = _ROOT / "agent_backend" / "nanobot" / "worker.py"


def _workspace_root():
    return frozen_workspace_root() or AGENT_WORKSPACE_ROOT
_MAX_LINE = 128_000
_EMOTIONS = {"neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talking"}
_GUI_TOOLS = frozenset(name for name, spec in CATALOG.items()
                       if spec.effect == "desktop" or name == "verify_desktop_goal")
_VERIFICATION_FIELDS = frozenset({"status", "method", "reason", "checked_at"})
_GUI_UNCONFIRMED_REPLY = (
    "未完成：电脑操作的结果检查未通过。"
)
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
    "not_configured": "任务助手尚未配置完成，请检查独立 Python 环境和模型设置。",
    "missing_backend": "未找到任务助手后端，请先完成后端安装。",
    "missing_sdk": "任务助手依赖尚未安装，请先完成独立后端环境安装。",
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
    "gui_runtime_missing": "电脑操作需要 Node.js，当前任务环境找不到它；安装或修复后请重启桌宠。",
    "gui_start_failed": "电脑操作后台启动失败；请检查 Node.js 和本地 UI-TARS 安装。",
    "gui_exited": "电脑操作后台意外退出；请检查当前结果，再发送新的任务消息。",
    "gui_protocol": "电脑操作后台返回格式无效，任务已停止；请检查当前结果。",
    "gui_failed": "电脑操作未能完成；请检查当前画面，再发送更明确的任务消息。",
    "gui_sdk_unavailable": "电脑操作依赖尚未就绪；请修复本地 UI-TARS 和 Windows 操作组件。",
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
    "closed": "任务助手已关闭。",
    "busy": "上一项任务仍在运行或结束中，请稍后发送新任务；本次未启动任务助手。",
    "checkpoint_unavailable": "任务检查点无法可靠读写，本次任务已停止且未继续派发；聊天记录仍保留，请检查本地数据目录后重试。",
    "goal_history_unavailable": "这段任务的完整要求无法继续恢复，请新建对话并重新说明当前目标；已完成的操作不会自动重放。",
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


def _verification_timestamp(report):
    """Validate the worker's evidence envelope, never parse model prose as proof.

    The worker binds this report to the original task. A terminal report belongs
    to this process request, not a tool argument or a restored chat message.
    Native verifiers and independent visual checks use the same small envelope.
    """
    if (not isinstance(report, dict) or set(report) != _VERIFICATION_FIELDS
            or not isinstance(report.get("status"), str)
            or report["status"] not in {"verified", "failed", "unverified"}
            or not isinstance(report.get("method"), str)
            or report["method"] not in {"visual", "uia", "native"}
            or not isinstance(report.get("reason"), str)
            or not 1 <= len(report["reason"].strip()) <= 2000
            or any(ord(char) < 32 and char not in "\n\t" for char in report["reason"])
            or not isinstance(report.get("checked_at"), str)
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", report["checked_at"])):
        raise ValueError("invalid GUI verification envelope")
    value = datetime.fromisoformat(report["checked_at"].replace("Z", "+00:00"))
    if value.tzinfo is None or abs(value.utcoffset().total_seconds()) > 14 * 3600:
        raise ValueError("invalid GUI verification timestamp")
    return value.timestamp()


def _verified_gui_result(frame, *, started_at, now):
    report = frame.get("verification")
    if frame.get("gui_goal_verified") is not True or not isinstance(report, dict):
        return False
    checked_at = _verification_timestamp(report)
    # All processes run on the same host clock. Two seconds accommodate ISO
    # precision; neither a prior task's evidence nor a future report is proof.
    # Action-generation freshness belongs to the execution verifier. Pipe
    # receipt times cannot establish causal order: several old events and a
    # newer check can already be buffered when the host next reads stdout.
    return report["status"] == "verified" and started_at - 2 <= checked_at <= now + 2


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
    """Call execute from a Qt worker. Construction performs no I/O or work."""

    def __init__(self, *, checkpoint_path=None):
        self._lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._processes = {}
        self._worker_handles = {}
        self._worker_cleanup_pending = set()
        self._worker_input_writers = {}
        self._worker_lifecycle = WorkerLifecycle()
        self._execution_attempts = ExecutionAttempts()
        self._closed = threading.Event()
        # Native messages come only from our worker, never phone/chat imports.
        # Production can opt into a separate bounded text checkpoint store;
        # construction does not create files, threads or SQLite connections.
        self._sessions = {}
        self._session_goals = {}
        self._session_scopes = {}
        self._session_epoch = 0
        self._checkpoint_epoch = 0
        self._checkpoint_store = CheckpointStore(checkpoint_path) if checkpoint_path is not None else None
        self._checkpoint_status = ""
        self._checkpoint_deletions = []
        self._checkpoint_executor = None
        self._input_lock = threading.Lock()
        self._interaction_broker = InteractionBroker()
        self._active_control = None
        self._file_grants = {}
        self._guardian_config = (Path(checkpoint_path).absolute().parent
                                 if checkpoint_path is not None and Path(checkpoint_path).is_absolute() else None)
        self._guardian_launch_epoch = host_secrets.token_hex(32)
        self._unbound_guardian_locations = []
        self._journal = (ExecutionJournal(Path(checkpoint_path).with_name("agent_events.sqlite3"))
                         if checkpoint_path is not None else None)
        self._runtime_health = {}
        self._artifact_store = (ArtifactStore(Path(checkpoint_path).with_name("agent_artifacts"))
                                if checkpoint_path is not None else None)

    def _guardian_scopes(self, workspace):
        with self._lock:
            return self._guardian_scopes_locked(workspace)

    def _guardian_scopes_locked(self, workspace):
        # Called only with _lock already held. Grant validation and publication
        # share that exact snapshot without recursively acquiring a plain Lock.
        return tuple([str(Path(workspace).resolve()), *[
            grant["path"] for grants in self._file_grants.values() for grant in grants]])

    def _check_guardian_scope(self, workspace, *, extra_grant=None, scope_snapshot=None):
        from .execution_runtime import _ROOT as execution_private_root
        private_roots = [execution_private_root.resolve()]
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

    @staticmethod
    def _check_execution_policy(expected_identity):
        from .execution_runtime import capture_execution_runtime, ExecutionRuntimeConfigurationError
        try: current = capture_execution_runtime().identity
        except ExecutionRuntimeConfigurationError:
            raise _error("execution_runtime_invalid") from None
        if current != expected_identity:
            raise _error("execution_runtime_changed")

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
        """Queue a real user correction for the next model step, never tool arguments."""
        if (not isinstance(text, str) or not text.strip() or len(text) > 8000
                or any(ord(ch) < 32 and ch not in "\n\t" for ch in text)):
            return False
        with self._lock:
            active = self._active_control
            if active is None or active["conversation_id"] != conversation_id or active["cancel"].is_set():
                return False
            process, task_id = active["process"], active["task_id"]
            goal_input = None
            goal_inputs = active.get("goal_inputs")
            if isinstance(goal_inputs, list):
                projected = _redact(text.strip(), active.get("goal_secrets", ()))
                try:
                    checked_goal_history([*active["goal_history"],
                                          *(item["text"] for item in goal_inputs), projected])
                except GoalHistoryError:
                    return False
                # Record provenance before dispatch; a worker user-role frame
                # must match this host input before it can amend the goal.
                goal_input = {"text": projected,
                              "replay": text_record({"role": "user", "content": projected},
                                                    active.get("goal_secrets", ()))["content"]}
                goal_inputs.append(goal_input)
        try:
            self._write_control(process, {"type": "steer", "id": task_id,
                                          "text": goal_input["text"] if goal_input is not None else text.strip()})
            pending = self._interaction_broker.public(conversation_id)
            if pending is not None:
                # Corrections may supersede a question, but never approve it.
                reply = self._interaction_broker.answer(conversation_id, pending["id"],
                    {"allowed": False} if pending["kind"] == "file_access" else {"text": ""})
                if reply is not None:
                    self._write_control(process, {"type": "interaction_reply", "id": task_id,
                        "interaction_id": pending["id"], "answer": reply["answer"]})
            return True
        except (OSError, ValueError, BrokenPipeError):
            if goal_input is not None:
                with self._lock:
                    goal_inputs[:] = [item for item in goal_inputs if item is not goal_input]
            return False

    @property
    def checkpoint_status(self):
        """Static user-facing state only; never a database path or exception."""
        with self._lock:
            return self._checkpoint_status

    def _checkpoint_warning(self):
        with self._lock:
            self._checkpoint_status = _ERROR_MESSAGES["checkpoint_unavailable"]

    def _flush_checkpoint_deletions(self):
        # Called from execute's existing worker, never a Qt view. It orders a
        # subsequent task after already accepted conversation deletions.
        with self._lock:
            pending = list(self._checkpoint_deletions)
        for future, conversation_id in pending:
            try:
                future.result()
            except Exception:
                try:
                    # A later explicit user request may retry an idempotent
                    # erasure after a transient lock, never an Agent action.
                    self._checkpoint_store.clear(conversation_id)
                except Exception:
                    self._checkpoint_warning()
                    raise _error("checkpoint_unavailable") from None
        with self._lock:
            self._checkpoint_deletions = [item for item in self._checkpoint_deletions if not item[0].done()]

    def checkpoint_info(self, conversation_id, settings, api, *, secrets=(), generation=""):
        """Read from a worker; the result contains no replay messages."""
        conversation_uuid(conversation_id)
        if self._checkpoint_store is None:
            return None
        try:
            self._flush_checkpoint_deletions()
            settings = shared_api_settings(settings, api)
            data = self._checkpoint_store.load(conversation_id,
                scope_fingerprint(settings, api, secrets=tuple(secrets[:1]), generation=generation), secrets=secrets)
        except (AgentError, CheckpointError, ValueError, TypeError):
            self._checkpoint_warning()
            return {"status": "unavailable", "goal_verified": False}
        if data is None:
            return None
        return {key: value for key, value in data.items() if key not in {"history", "goal_history"}}

    def clear_session(self, conversation_id=None):
        """Invalidate replay when the host deletes or clears a conversation."""
        if conversation_id is not None:
            conversation_uuid(conversation_id)
        with self._lock:
            self._session_epoch += 1
            self._checkpoint_epoch += 1
            if conversation_id is None:
                self._sessions.clear()
                self._session_goals.clear()
                self._session_scopes.clear()
                self._file_grants.clear()
            else:
                self._sessions.pop(conversation_id, None)
                self._session_goals.pop(conversation_id, None)
                self._session_scopes.pop(conversation_id, None)
                self._file_grants = {key: grants for key, grants in self._file_grants.items()
                                     if key[0] != conversation_id}
            store = self._checkpoint_store
            if store is not None and not self._closed.is_set():
                if self._checkpoint_executor is None:
                    self._checkpoint_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aura-checkpoint-delete")

                def remove():
                    try:
                        store.clear(conversation_id)
                        if self._journal is not None:
                            self._journal.clear(conversation_id)
                        if self._artifact_store is not None:
                            self._artifact_store.clear(conversation_id)
                    except Exception:
                        self._checkpoint_warning()
                        raise

                future = self._checkpoint_executor.submit(remove)
                self._checkpoint_deletions.append((future, conversation_id))
                return future

    @staticmethod
    def capabilities(settings, gui_api_key="", *, api=None):
        """Describe configured permissions; never enable a flag or capture pixels."""
        tools = bool(settings.enabled and settings.tools_enabled)
        computer = bool(settings.enabled and settings.computer_enabled)
        gui_configured = False
        try:
            if api is not None:
                settings = shared_api_settings(settings, api)
            validate_api(settings.gui_base_url, settings.gui_model)
            gui_configured = isinstance(gui_api_key, str) and bool(gui_api_key.strip())
        except AgentValidationError:
            pass
        manifest = []
        from agent_backend.nanobot.browser_session import find_browser
        browser_available = find_browser() is not None if tools else False
        for name, spec in CATALOG.items():
            available = spec.available(tools_enabled=tools, computer_enabled=computer,
                                       gui_configured=gui_configured)
            permission = tools if spec.permission == "tools" else computer if spec.permission == "computer" else tools or computer
            reason = ("" if available else "kind_disabled" if not permission else
                      "gui_unsupported_platform" if spec.windows_only and sys.platform != "win32" else
                      "gui_not_configured" if spec.requires_gui and not gui_configured else "not_configured")
            if available and name.startswith("browser_") and not browser_available:
                available, reason = False, "browser_not_installed"
            if available and name == "compute_project_artifact":
                # Permission alone does not register the host-bound tool.
                # Static callers have no captured policy/CID/grant authority.
                available, reason = False, "needs_host_scope"
            manifest.append({"name": name, "available": available, "reason_code": reason})
        return manifest

    def _project_readiness_reason(self, settings, context, config, secrets):
        """Metadata only; never construct a runtime owner or launch a probe."""
        if not context.get("conversation_id"):
            return "conversation_required"
        execution = config.get("project_execution")
        if not isinstance(execution, dict) or execution.get("enabled") is not True:
            return "execution_runtime_disabled"
        from agent_backend.nanobot.project_runtime import (
            checked_configuration, project_scope_conflicts, ProjectRuntimeError)
        from .execution_runtime import _ROOT as private_root, _CONFIG as private_configuration
        try:
            checked_configuration(execution, config.get("project_execution_identity"))
        except ProjectRuntimeError:
            return "execution_runtime_invalid"
        try:
            scope = scope_fingerprint(settings, config, secrets=(config["api_key"],),
                generation=context.get("conversation_generation", ""))
            with self._lock:
                grants = tuple(self._file_grants.get((context["conversation_id"], scope), ()))
            paths = [Path(settings.workspace or _workspace_root()).resolve(),
                *[grant["path"] for grant in grants]]
            if project_scope_conflicts(paths, host_private_root=private_root,
                    host_configuration=private_configuration):
                return "execution_scope_conflict"
        except (KeyError, TypeError, ValueError, OSError):
            return "execution_scope_conflict"
        return ""

    def readiness(self, settings, key="", context=None):
        """Safe status for desktop/phone clients, with the actual enabled tools."""
        try:
            if not isinstance(settings, AgentSettings):
                raise _error("not_configured")
            if not isinstance(context, dict):
                raise _error("not_configured")
            settings = shared_api_settings(settings, context.get("api"))
            settings.validate()
            if self._closed.is_set():
                raise _error("closed")
            if not settings.enabled:
                raise _error("disabled")
            interpreter = Path(settings.python_executable or DEFAULT_PYTHON)
            if not interpreter.is_file():
                raise _error("not_configured")
            if sys.platform == "win32" and interpreter.suffix.casefold() == ".exe":
                with interpreter.open("rb") as stream:
                    if stream.read(2) != b"MZ":
                        raise _error("not_configured")
            if not _WORKER_PATH.is_file():
                raise _error("missing_backend")
            if not isinstance(context, dict):
                raise _error("not_configured")
            config, _, secrets = self._context(context, key, AgentRequest("readiness", "agent"), settings)
            # A malformed or changed captured policy also blocks real dispatch;
            # it cannot be downgraded to an optional-tool-only metadata warning.
            self._check_execution_policy(config.get("project_execution_identity", ""))
            manifest = self.capabilities(settings, key, api=context["api"])
            if settings.tools_enabled and sys.platform == "win32":
                reason = self._project_readiness_reason(settings, context, config, secrets)
                for item in manifest:
                    if item["name"] == "compute_project_artifact":
                        item.update(available=not reason, reason_code=reason)
                        break
            return {"available": True, "configured": True, "runtime_ready": self._runtime_health.get(str(interpreter)),
                    "reason_code": "", "reason": "",
                    "tools": [item["name"] for item in manifest if item["available"]],
                    "capabilities": manifest}
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
            self._worker_lifecycle.request_stop(handle)

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
            self._sessions.clear()
            self._session_goals.clear()
            self._session_scopes.clear()
            self._session_epoch += 1
        for event, handle in active:
            event.set()
            self._worker_lifecycle.request_stop(handle)
        if self._checkpoint_executor is not None:
            # Accepted erasures finish without blocking the GUI shutdown path.
            self._checkpoint_executor.shutdown(wait=False, cancel_futures=False)

    @staticmethod
    def _context(context, key, request, settings):
        if not isinstance(context, dict) or set(context) - {"api", "messages", "gui_api_key", "conversation_id", "conversation_generation", "current_image_url", "current_image_id", "model_context_window", "image_catalog", "office_runtime_snapshot", "execution_runtime_snapshot"}:
            raise AgentValidationError("Agent 上下文格式无效。")
        try:
            if context.get("conversation_id") is not None:
                conversation_uuid(context["conversation_id"])
            generation = context.get("conversation_generation", "")
            if generation is not None and not isinstance(generation, str):
                raise ValueError("invalid conversation generation")
            if generation:
                conversation_uuid(generation)
            current_image(context.get("current_image_url"))
        except ValueError:
            raise AgentValidationError("Agent conversation or current image is invalid.") from None
        api = context.get("api")
        if is_dataclass(api):
            api = asdict(api)
        if (not isinstance(api, dict)
                or set(api) - {"base_url", "model", "timeout_seconds", "reasoning_effort",
                               "reasoning_profile", "reasoning_budget_tokens", "reasoning_metadata"}):
            raise AgentValidationError("Agent 模型配置格式无效。")
        validate_api(api.get("base_url"), api.get("model"))
        settings = shared_api_settings(settings, api)
        effort = api.get("reasoning_effort", "default")
        reasoning = None
        if any(name in api for name in ("reasoning_profile", "reasoning_budget_tokens", "reasoning_metadata")):
            profile = api.get("reasoning_profile", "auto")
            budget = api.get("reasoning_budget_tokens", 0)
            metadata = api.get("reasoning_metadata")
            try:
                validate_selection(api["base_url"], api["model"], value=effort, profile=profile,
                                   budget_tokens=budget, metadata=metadata)
                wire = reasoning_body(api["base_url"], api["model"], value=effort, profile=profile,
                                      budget_tokens=budget, metadata=metadata)
            except ReasoningError:
                raise AgentValidationError("Agent 思考配置与当前模型或接口不兼容，请检查模型设置。") from None
            reasoning = {"version": 1, "profile": profile, "value": effort, "budget_tokens": budget}
            if metadata is not None:
                reasoning["metadata"] = metadata
            # Detach the immutable request snapshot from a mutable UI/metadata
            # dictionary. The shared validator admits bounded JSON only.
            try:
                encoded_reasoning = json.dumps(reasoning, allow_nan=False, separators=(",", ":"))
                if len(encoded_reasoning.encode("utf-8")) > 16_384 or metadata is not None and type(metadata) is not dict:
                    raise ValueError()
                reasoning = json.loads(encoded_reasoning)
            except (TypeError, ValueError):
                raise AgentValidationError("Agent 思考配置格式无效，请检查模型设置。") from None
            # Old readers only understand the legacy OpenAI field. They must
            # not treat provider-native modes or token budgets as an effort.
            effort = wire.get("reasoning_effort", "default")
            if effort not in ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
                effort = "default"
        elif effort not in ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
            raise AgentValidationError("Agent 思考强度无效。")
        gui_key = context.get("gui_api_key", "")
        if (not isinstance(key, str) or not key.strip() or len(key) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in key)):
            raise _error("not_configured")
        if (not isinstance(gui_key, str) or len(gui_key) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in gui_key)):
            raise _error("gui_not_configured")
        messages = context.get("messages", [])
        if not isinstance(messages, list):
            raise AgentValidationError("Agent 参考上下文过大或格式无效。")
        safe = []
        # A retired GUI credential may still occur in an old caller's frame.
        # Retain it for redaction only; it never selects an endpoint or account.
        secrets = tuple(dict.fromkeys((key, gui_key)))
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or message["role"] not in ("system", "user", "assistant")
                    or not isinstance(message["content"], str)):
                raise AgentValidationError("Agent 参考消息必须为文本消息。")
            safe.append({"role": message["role"], "content": _redact(message["content"], secrets)})
        text = json.dumps(safe, ensure_ascii=False, separators=(",", ":")) if safe else ""
        config = {"base_url": api["base_url"], "model": api["model"], "api_key": key,
                  "reasoning_effort": effort}
        if reasoning is not None:
            config["reasoning"] = reasoning
        from .office_runtime import (capture_office_runtime, valid_office_runtime_snapshot,
                                     OfficeRuntimeConfigurationError)
        try:
            office_snapshot = (context["office_runtime_snapshot"] if "office_runtime_snapshot" in context
                               else capture_office_runtime())
            if not valid_office_runtime_snapshot(office_snapshot):
                raise OfficeRuntimeConfigurationError()
            office = office_snapshot.configuration
            if office is not None: config["office_renderer"] = office
        except OfficeRuntimeConfigurationError:
            raise _error("office_runtime_invalid") from None
        from .execution_runtime import (capture_execution_runtime, valid_execution_runtime_snapshot,
                                        ExecutionRuntimeConfigurationError)
        try:
            execution_snapshot = (context["execution_runtime_snapshot"] if "execution_runtime_snapshot" in context
                                  else capture_execution_runtime())
            if not valid_execution_runtime_snapshot(execution_snapshot):
                raise ExecutionRuntimeConfigurationError()
            execution = execution_snapshot.configuration
            if execution is not None:
                config["project_execution_identity"] = execution_snapshot.identity
                # A legacy call without a CID cannot acquire a project owner.
                # Its ordinary tools remain usable with the scoped policy ID.
                if context.get("conversation_id"):
                    config["project_execution"] = execution
        except ExecutionRuntimeConfigurationError:
            raise _error("execution_runtime_invalid") from None
        window = context.get("model_context_window")
        if window is not None and (type(window) is not int or not 1024 <= window <= 100_000_000):
            raise AgentValidationError("模型窗口资料无效。")
        if window is not None:
            config["context_window_tokens"] = window
        catalog = context.get("image_catalog", [])
        if (not isinstance(catalog, list) or len(catalog) > 64 or any(
                not isinstance(item, dict) or set(item) != {"attachment_id", "label"}
                or not isinstance(item["attachment_id"], str) or not re.fullmatch(r"[a-f0-9-]{24,80}", item["attachment_id"])
                or not isinstance(item["label"], str) or len(item["label"]) > 240 for item in catalog)):
            raise AgentValidationError("图片参考资料无效。")
        image_id = context.get("current_image_id")
        if image_id is not None and (not isinstance(image_id, str)
                or not re.fullmatch(r"[a-f0-9]{64}", image_id)
                or not context.get("current_image_url")
                or image_id not in {item["attachment_id"] for item in catalog}):
            raise AgentValidationError("当前图片参考资料无效。")
        gui_available = any(item["name"] == "computer_use" and item["available"]
                            for item in AgentService.capabilities(settings, key, api=api))
        if request.kind == "computer" or request.kind == "agent" and gui_available:
            config["gui"] = {"base_url": settings.gui_base_url, "model": settings.gui_model,
                             "provider": settings.gui_provider, "api_key": key,
                             "coordinate_profile": settings.gui_coordinate_profile}
        return config, text, secrets

    def execute(self, settings, key, request, context, cancellation_event=None, progress_callback=None, event_callback=None,
                interaction_callback=None, image_resolver=None, diagnostic_callback=None, worker_lifecycle_callback=None,
                worker_dispatch_guard=None, execution_attempt_callback=None, guardian_launch=None):
        """Allow one task, including its child-process cleanup, at a time."""
        if request is None:
            return None
        identity = context.get("conversation_id") if isinstance(context, dict) else None
        # Invalid public context still receives the original validation error
        # from _execute_impl; it cannot bind a canonical queued conversation.
        try:
            if identity is not None: conversation_uuid(identity)
        except ValueError: identity = None
        attempt = self._execution_attempts.begin(identity)
        acquired = False
        handles = []
        def bound(handle):
            handles.append(handle)
            if callable(worker_lifecycle_callback): worker_lifecycle_callback(handle)
        try:
            if callable(execution_attempt_callback): execution_attempt_callback(attempt)
            if not self._execution_lock.acquire(blocking=False):
                raise _error("busy")
            acquired = True
            outcome = self._execute_impl(settings, key, request, context,
                                      cancellation_event, progress_callback, event_callback, interaction_callback, image_resolver,
                                      diagnostic_callback, bound, worker_dispatch_guard, execution_attempt=attempt,
                                      guardian_launch=guardian_launch)
            with self._lock:
                unresolved = any(handle in self._worker_cleanup_pending for handle in handles)
            if isinstance(outcome, AgentResult) and unresolved:
                # _execute_impl's finally ran before this point. Updating its
                # local result inside finally would not alter a prior return.
                acceptance = dict(outcome.acceptance) if outcome.acceptance is not None else {
                    "version": 1, "judge_status": "not_run", "requirements_checked": 0,
                    "requirements_passed": 0, "artifacts_checked": 0, "artifacts_passed": 0}
                acceptance["passed"] = False
                acceptance["issue_codes"] = sorted(set(acceptance.get("issue_codes", [])) | {"WORKER_CLEANUP_UNCONFIRMED"})
                outcome = replace(outcome, status="needs_user", acceptance=acceptance,
                    reply=outcome.reply + "\n\n未完成：后台工具未能正常退出。")
            return outcome
        finally:
            try:
                if acquired: self._execution_lock.release()
            finally:
                # Last operation on every invocation exit. No callback or
                # launch-capable code follows this seal. A seal failure leaves
                # proof unavailable rather than replacing an original error.
                try: self._execution_attempts.finish(attempt)
                except Exception: pass

    def _execute_impl(self, settings, key, request, context, cancellation_event=None, progress_callback=None, event_callback=None,
                      interaction_callback=None, image_resolver=None, diagnostic_callback=None, worker_lifecycle_callback=None,
                      worker_dispatch_guard=None, *, execution_attempt=None, guardian_launch=None):
        """Run one explicitly routed task, once. Null requests never spawn."""
        if request is None:
            return None
        if not isinstance(request, AgentRequest):
            raise AgentValidationError("Agent 请求类型无效。")
        request.validate()
        if not isinstance(settings, AgentSettings):
            raise AgentValidationError("Agent 设置类型无效。")
        if isinstance(context, dict):
            settings = shared_api_settings(settings, context.get("api"))
        settings.validate()
        from .guardian_launch import valid as valid_guardian_launch
        if guardian_launch is not None and not valid_guardian_launch(guardian_launch):
            raise _error("guardian_unavailable")
        if self._closed.is_set():
            raise _error("closed")
        with self._lock:
            if self._processes or self._unbound_guardian_locations:
                # A prior exact worker/tree is still unconfirmed. Its empty UI
                # or an expired queue lease must not admit overlapping effects.
                raise _error("busy")
        from .guardian_worker import pending_cleanup_count
        from .guardian_native import has_pending_cleanup
        if pending_cleanup_count() or has_pending_cleanup():
            raise _error("busy")
        if not settings.enabled:
            raise _error("disabled")
        if (request.kind == "tools" and not settings.tools_enabled
                or request.kind == "computer" and not settings.computer_enabled):
            raise _error("kind_disabled")
        event = cancellation_event if cancellation_event is not None else threading.Event()
        if event.is_set():
            raise _error("cancelled")
        config, context_text, secrets = self._context(context, key, request, settings)
        executable = Path(settings.python_executable or DEFAULT_PYTHON)
        if not executable.is_file():
            raise _error("not_configured")
        if not _WORKER_PATH.is_file():
            raise _error("missing_backend")
        task_id = uuid4().hex
        options = {"timeout_seconds": settings.timeout_seconds, "max_steps": settings.max_steps}
        options["include_activity"] = callable(event_callback)
        options["workspace"] = str(Path(settings.workspace or _workspace_root()).resolve())
        self._check_guardian_scope(options["workspace"])
        options["tools_enabled"] = settings.tools_enabled
        options["computer_enabled"] = settings.computer_enabled
        session_id = context.get("conversation_id")
        try:
            if session_id is not None:
                conversation_uuid(session_id)
            image_url = current_image(context.get("current_image_url"))
        except ValueError:
            raise AgentValidationError("Agent conversation or current image is invalid.") from None
        checkpoint_store = self._checkpoint_store if session_id else None
        options["checkpoint_required"] = checkpoint_store is not None
        scope = scope_fingerprint(settings, config, secrets=(key,),
                                  generation=context.get("conversation_generation", "")) if session_id else None
        if guardian_launch is not None:
            ref = guardian_launch.reference
            if (session_id, context.get("conversation_generation")) != (ref.conversation_id, ref.generation):
                raise _error("guardian_unavailable")
            if guardian_launch.validate_attempt(execution_attempt) is not True:
                raise _error("guardian_unavailable")
        options["file_grants"] = list(self._file_grants.get((session_id, scope), []))
        if checkpoint_store is not None:
            options["context_store_root"] = str(checkpoint_store.context_root)
            options["context_store_namespace"] = f"{session_id}:{scope}"
        with self._lock:
            session_epoch = self._session_epoch
            checkpoint_epoch = self._checkpoint_epoch
            if session_id and self._session_scopes.get(session_id) not in (None, scope):
                self._sessions.pop(session_id, None)
                self._session_goals.pop(session_id, None)
            previous_history = list(self._sessions.get(session_id, [])) if session_id else []
            previous_goals = list(self._session_goals.get(session_id, [])) if session_id else []
        if checkpoint_store is not None:
            self._flush_checkpoint_deletions()
            try:
                saved = checkpoint_store.load(session_id, scope, secrets=secrets,
                    legacy_scope_hash=legacy_scope_fingerprint(settings, config, secrets=(key,),
                        generation=context.get("conversation_generation", "")))
                if saved is not None:
                    previous_history = saved["history"]
                    previous_goals = saved["goal_history"]
                    with self._lock:
                        self._checkpoint_status = (
                            "模型、账户或权限已变更，旧工具记录未恢复；本次按当前请求重新开始。"
                            if not saved["scope_matches"] else
                            "已恢复中断任务记录；需要先核对实际结果，再按本次请求继续。"
                            if saved["recovered_running"] else "已恢复当前会话的任务检查点。"
                        )
                if not previous_goals:
                    previous_goals = legacy_goal_history(previous_history)
                checkpoint_store.begin(session_id, scope, task_id,
                    previous_history + [text_record({"role": "user", "content": request.task}, secrets)], secrets=secrets,
                    goal_history=checked_goal_history([*previous_goals, _redact(request.task, secrets)]))
                if saved is None:
                    with self._lock:
                        self._checkpoint_status = ""
            except GoalHistoryError:
                raise _error("goal_history_unavailable") from None
            except (CheckpointError, ValueError, TypeError):
                self._checkpoint_warning()
                raise _error("checkpoint_unavailable") from None
        payload = {"type": "run", "id": task_id, "task": _redact(request.task, secrets),
                   "kind": request.kind, "context": context_text, "config": config, "options": options}
        if session_id:
            try:
                if not previous_goals:
                    previous_goals = legacy_goal_history(previous_history)
                checked_goal_history([*previous_goals, _redact(request.task, secrets)])
            except GoalHistoryError:
                raise _error("goal_history_unavailable") from None
            payload.update(conversation_id=session_id, session_history=previous_history,
                           goal_history=checked_goal_history(previous_goals))
            if config.get("project_execution", {}).get("enabled") is True:
                payload["execution_authority"] = {"version": 1, "conversation_id": session_id,
                    "generation": context.get("conversation_generation") or str(uuid4()),
                    "request_id": str(UUID(task_id)), "settings_scope": scope}
        if image_url:
            payload["current_image_url"] = image_url
            if context.get("current_image_id"):
                payload["current_image_id"] = context["current_image_id"]
        if context.get("image_catalog"):
            payload["image_catalog"] = context["image_catalog"]
        raw_input = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        environment = {name: value for name, value in os.environ.items()
                       if not re.search(r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|OPENAI|ANTHROPIC|NANOBOT|PYTHONPATH)",
                                        name, re.IGNORECASE)}
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                            "OPENAI_LOG": "error", "NO_COLOR": "1"})
        launch = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                  if sys.platform == "win32" else {"start_new_session": True})
        process, tree, run_handle = None, None, None
        transcript = list(previous_history)
        current_goals = checked_goal_history([*previous_goals, _redact(request.task, secrets)])
        authorized_goal_inputs = []
        received_user_task = False
        received_records = 0
        result = None
        gui_used = request.kind == "computer"
        goal_verified = False
        started_at = time.time()
        terminal_error = "interrupted"
        last_barrier = 0
        deadline = time.monotonic() + settings.timeout_seconds if settings.timeout_seconds else None
        def notify(phase):
            if callable(progress_callback) and phase in PROGRESS_MESSAGES:
                try:
                    progress_callback(phase)
                except Exception:
                    pass
        notify("starting")
        try:
            if event.is_set() or self._closed.is_set():
                raise _error("cancelled")
            if guardian_launch is None:
                self._execution_attempts.mark_launch_attempted(execution_attempt)
                process = subprocess.Popen([str(executable), "-I", "-u", str(_WORKER_PATH)],
                    cwd=str(_ROOT), env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0, **launch)
                run_handle = self._worker_lifecycle.register(process, task_id, session_id)
                with self._lock:
                    self._processes[process.pid] = (event, None)
                    self._worker_handles[process.pid] = run_handle
                if callable(worker_lifecycle_callback):
                    worker_lifecycle_callback(run_handle)
                tree = _ProcessTree(process)
                self._worker_lifecycle.attach_tree(run_handle, tree)
            else:
                from .guardian_worker import GuardianWorker, owns_location
                def native_bound(current):
                    nonlocal process, tree, run_handle
                    process, tree = current, current.tree
                    run_handle = self._worker_lifecycle.register(current, task_id, session_id)
                    self._worker_lifecycle.attach_tree(run_handle, tree)
                    with self._lock:
                        self._processes[current.pid] = (event, tree)
                        self._worker_handles[current.pid] = run_handle
                    if callable(worker_lifecycle_callback): worker_lifecycle_callback(run_handle)
                location = self._guardian_storage(options["workspace"])
                try:
                    process = GuardianWorker.create([str(executable.resolve()), "-I", "-u", str(_WORKER_PATH)],
                        cwd=_ROOT, location=location, job_id=guardian_launch.job_id, reference=guardian_launch.reference,
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
                self._active_control = {"process": process, "task_id": task_id,
                    "goal_inputs": authorized_goal_inputs, "goal_history": current_goals, "goal_secrets": secrets,
                                        "conversation_id": session_id, "scope": scope, "cancel": event,
                                        "workspace": options["workspace"]}
            if self._journal is not None and session_id:
                self._journal.append(session_id, task_id, "task_started", {"kind": request.kind}, secrets=secrets)
            if event.is_set() or self._closed.is_set():
                raise _error("cancelled")
            if guardian_launch is not None:
                with (worker_dispatch_guard(run_handle) if callable(worker_dispatch_guard) else nullcontext()):
                    if event.is_set() or self._closed.is_set(): raise _error("cancelled")
                    self._check_guardian_scope(options["workspace"])
                    self._check_execution_policy(config.get("project_execution_identity", ""))
                    process.resume()
            buffer = b""
            startup_deadline = min(deadline or float("inf"), time.monotonic() + 15)
            while b"\n" not in buffer:
                if event.is_set() or self._closed.is_set(): raise _error("cancelled")
                if time.monotonic() >= startup_deadline:
                    raise _error("timeout" if deadline is not None and time.monotonic() >= deadline else "worker_startup_timeout")
                buffer += _read_available(process.stdout)
                if len(buffer) > _MAX_LINE: raise _error("protocol")
                if process.poll() is not None and b"\n" not in buffer: raise _error("backend_failed")
                if b"\n" not in buffer: event.wait(.01)
            ready, buffer = buffer.split(b"\n",1)
            ready = self._parse_frame(ready,task_id)
            if (set(ready) != {"type","protocol","sdk","sdk_version"} or ready.get("type") != "ready"
                    or type(ready.get("protocol")) is not int or ready["protocol"] != 1
                    or ready.get("sdk") != "nanobot-ai" or ready.get("sdk_version") != "0.4.0"):
                raise _error("protocol")
            writer = StagedRunInput(process.stdin, stop_owned=lambda: self._worker_lifecycle.request_stop(run_handle),
                                    cancellation_event=event)
            if guardian_launch is not None: process.bind_writer(writer)
            with self._lock: self._worker_input_writers[run_handle] = writer
            writer.start()
            # JSON serialization escaped all embedded CR/LF. The trusted reader
            # cannot parse/dispatch a command until the final one-byte newline.
            writer.stage(raw_input[:-1])
            with (worker_dispatch_guard(run_handle) if callable(worker_dispatch_guard) else nullcontext()):
                if event.is_set() or self._closed.is_set(): raise _error("cancelled")
                self._check_guardian_scope(options["workspace"])
                self._check_execution_policy(config.get("project_execution_identity", ""))
                if guardian_launch is not None: process.validate_private_scope()
                self._worker_lifecycle.mark_dispatched(run_handle)
                writer.commit()
            if not writer.close(): raise _error("backend_failed")
            while True:
                if event.is_set() or self._closed.is_set():
                    raise _error("cancelled")
                if deadline is not None and time.monotonic() >= deadline:
                    raise _error("timeout")
                expired = self._interaction_broker.expired()
                if expired is not None:
                    self._write_control(process, {"type": "interaction_reply", "id": task_id,
                        "interaction_id": expired["interaction_id"], "answer": expired["answer"]})
                    if callable(interaction_callback):
                        interaction_callback(None)
                chunk = _read_available(process.stdout)
                buffer += chunk
                if b"\n" not in buffer and len(buffer) > _MAX_LINE:
                    raise _error("protocol")
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if not raw.strip():
                        continue
                    if len(raw) > _MAX_LINE:
                        raise _error("protocol")
                    frame = self._parse_frame(raw, task_id)
                    frame_type = frame["type"]
                    if frame_type == "progress":
                        notify(frame["phase"])
                    elif frame_type == "interaction":
                        try:
                            pending = checked_interaction(frame, secrets=secrets)
                        except (ValueError, OSError):
                            self._write_control(process, {"type": "interaction_reply", "id": task_id,
                                "interaction_id": frame["interaction_id"],
                                "answer": {"allowed": False, "code": "FILE_GRANT_UNAVAILABLE"}})
                            continue
                        pending = self._interaction_broker.open(pending, conversation_id=session_id, task_id=task_id)
                        if self._journal is not None and session_id:
                            self._journal.append(session_id, task_id, "interaction", pending, secrets=secrets)
                        if callable(interaction_callback):
                            interaction_callback(pending)
                    elif frame_type == "resource_request":
                        value = None
                        listed = {item["attachment_id"] for item in context.get("image_catalog", [])}
                        if (frame["resource_kind"] == "image" and frame["reference"] in listed
                                and callable(image_resolver) and not event.is_set()):
                            try:
                                value = current_image(image_resolver(frame["reference"]))
                            except Exception:
                                value = None
                        self._write_control(process, {"type": "resource_reply", "id": task_id,
                            "resource_id": frame["resource_id"], "value": value})
                    elif frame_type == "diagnostic":
                        if (callable(diagnostic_callback) and frame["data"].get("code") in FAILURE_DIAGNOSTIC_CODES | JUDGE_DIAGNOSTIC_CODES
                                and frame["data"].get("phase") in {"model", "runtime"}):
                            try:
                                diagnostic_callback({"phase": frame["data"]["phase"], "code": frame["data"]["code"]})
                            except Exception:
                                pass
                        if self._journal is not None and session_id:
                            self._journal.append(session_id, task_id, "diagnostic", frame["data"], secrets=secrets)
                    elif frame_type == "activity":
                        if (frame.get("kind") == "computer"
                                or frame.get("kind") == "tool" and frame.get("name") in _GUI_TOOLS):
                            gui_used = True
                        if callable(event_callback):
                            try:
                                from agent_backend.nanobot.activity_subject import subject_detail
                                value = public_activity(frame)
                                detail = subject_detail(frame.get("name"), frame.get("subject"))
                                if frame.get("kind") == "tool" and detail:
                                    value["subject"] = dict(frame["subject"])
                                    if "name" in value["subject"]:
                                        value["subject"]["name"] = _redact(value["subject"]["name"], secrets)
                                event_callback(value)
                            except Exception:
                                pass
                        if self._journal is not None and session_id:
                            self._journal.append(session_id, task_id, "tool_activity", public_activity(frame), secrets=secrets)
                    elif frame_type == "session":
                        if not session_id:
                            raise _error("protocol")
                        try:
                            message = text_record(frame["message"], secrets)
                            if message["role"] == "user":
                                if not received_user_task:
                                    # The original exact request was already
                                    # persisted before launch. Its replay can
                                    # be UTF-8 clipped or carry image markers.
                                    received_user_task = True
                                else:
                                    with self._lock:
                                        if (authorized_goal_inputs and frame["message"].get("content")
                                                == authorized_goal_inputs[0]["replay"]):
                                            # Transcript previews may be clipped; durable goals
                                            # retain the exact host input, never its replay copy.
                                            current_goals[:] = checked_goal_history([
                                                *current_goals, authorized_goal_inputs.pop(0)["text"]])
                            transcript.append(message)
                            if self._journal is not None:
                                self._journal.append(session_id, task_id, "message", message, secrets=secrets)
                            if any(call.get("function", {}).get("name") in _GUI_TOOLS
                                   for call in message.get("tool_calls", [])):
                                gui_used = True
                        except (ValueError, TypeError):
                            raise _error("protocol") from None
                        received_records += 1
                        with self._lock:
                            if session_epoch == self._session_epoch:
                                self._sessions[session_id] = bounded_history(transcript)
                                self._session_goals[session_id] = list(current_goals)
                                self._session_scopes[session_id] = scope
                                while len(self._sessions) > 64:
                                    removed = next(iter(self._sessions))
                                    self._sessions.pop(removed)
                                    self._session_goals.pop(removed, None)
                                    self._session_scopes.pop(removed, None)
                    elif frame_type == "checkpoint":
                        if checkpoint_store is None or not session_id:
                            raise _error("protocol")
                        barrier = int(frame["barrier_id"].split("-", 1)[1])
                        if barrier != last_barrier + 1:
                            raise _error("protocol")
                        with self._lock:
                            valid_epoch = checkpoint_epoch == self._checkpoint_epoch
                        if not valid_epoch:
                            raise _error("cancelled")
                        try:
                            checkpoint_store.save(session_id, scope, task_id, transcript,
                                                  barrier=barrier, secrets=secrets, goal_history=current_goals)
                        except (CheckpointError, ValueError, TypeError):
                            self._checkpoint_warning()
                            result = None
                            raise _error("checkpoint_unavailable") from None
                        with self._lock:
                            valid_epoch = checkpoint_epoch == self._checkpoint_epoch
                        if not valid_epoch or event.is_set() or self._closed.is_set():
                            raise _error("cancelled")
                        last_barrier = barrier
                        # This ACK is emitted only after SQLite COMMIT. Without
                        # it the worker must not invoke the next tool body.
                        with (worker_dispatch_guard(run_handle) if callable(worker_dispatch_guard) else nullcontext()):
                            if event.is_set() or self._closed.is_set(): raise _error("cancelled")
                            self._check_guardian_scope(options["workspace"])
                            self._check_execution_policy(config.get("project_execution_identity", ""))
                            if guardian_launch is not None: process.validate_private_scope()
                            self._write_control(process, {"type": "checkpoint_ack", "id": task_id,
                                "barrier_id": frame["barrier_id"]})
                    elif frame_type == "error":
                        code = frame["code"].casefold()
                        code = {"timed_out": "timeout", "missing_node": "missing_sdk",
                                "missing_dependencies": "missing_sdk"}.get(code, code)
                        raise _error(code if code in _ERROR_MESSAGES else "backend_failed")
                    elif frame_type == "final":
                        self._check_execution_policy(config.get("project_execution_identity", ""))
                        if result is not None:
                            raise _error("protocol")
                        if event.is_set() or self._closed.is_set():
                            raise _error("cancelled")
                        status = frame.get("status", "completed")
                        if status in ("cancelled", "timed_out"):
                            raise _error("cancelled" if status == "cancelled" else "timeout")
                        gui_used = gui_used or frame.get("gui_used", False)
                        goal_verified = status == "completed" and gui_used and _verified_gui_result(
                            frame, started_at=started_at, now=time.time())
                        reply, emotion = frame["reply"], frame.get("emotion", "neutral")
                        if gui_used and status == "completed" and not goal_verified:
                            status, reply, emotion = "needs_user", _GUI_UNCONFIRMED_REPLY, "thinking"
                        acceptance = checked_assessment(frame.get("acceptance"))
                        if acceptance is not None and status == "completed" and not acceptance["passed"]:
                            status = "needs_user"
                            reply = "未完成：结果检查未通过。"
                        result = AgentResult(_redact(reply, secrets), emotion, request.kind, status=status,
                                             acceptance=acceptance)
                        self._runtime_health[str(executable)] = True
                        if self._artifact_store is not None and session_id:
                            from agent_backend.nanobot.safe_files import WorkspaceFiles
                            authorized_files = WorkspaceFiles(Path(options["workspace"]),
                                grants=self._file_grants.get((session_id, scope), []))
                            deliveries = []
                            delivery_failed = False
                            for report in frame.get("artifacts", []):
                                try:
                                    deliveries.append(self._artifact_store.register(session_id, report, authorized_files))
                                except (OSError, ValueError):
                                    delivery_failed = True
                            result = AgentResult(result.reply, result.emotion, result.kind,
                                                 status=result.status, artifacts=tuple(deliveries), acceptance=result.acceptance)
                            if delivery_failed:
                                result = AgentResult(result.reply + "\n\n文件交付失败：无法保存下载副本，原文件已保留。",
                                    result.emotion, result.kind, status="needs_user", artifacts=result.artifacts,
                                    acceptance=result.acceptance)
                # A result is sufficient; close any remaining GUI/tool children.
                if result is not None:
                    if checkpoint_store is not None:
                        with self._lock:
                            valid_epoch = checkpoint_epoch == self._checkpoint_epoch
                        if not valid_epoch:
                            raise _error("cancelled")
                        try:
                            final_transcript = list(transcript)
                            if (not final_transcript or final_transcript[-1].get("role") != "assistant"
                                    or final_transcript[-1].get("content") != result.reply):
                                final_transcript.append({"role": "assistant", "content": result.reply})
                            checkpoint_store.save(session_id, scope, task_id,
                                final_transcript,
                                status=result.status, goal_verified=goal_verified, secrets=secrets)
                        except (CheckpointError, ValueError, TypeError):
                            self._checkpoint_warning()
                            result = None
                            raise _error("checkpoint_unavailable") from None
                    if event.is_set() or self._closed.is_set():
                        raise _error("cancelled")
                    if result.status == "completed":
                        notify("completed")
                    return result
                if process.poll() is not None and not chunk:
                    if buffer.strip():
                        raise _error("protocol")
                    raise _error("backend_failed")
                time.sleep(0.02)
        except StagedInputError as exc:
            terminal_error = {"WORKER_INPUT_CANCELLED":"cancelled", "WORKER_INPUT_STAGE_TIMEOUT":"worker_input_stage_timeout",
                "WORKER_INPUT_COMMIT_TIMEOUT":"worker_input_commit_timeout", "WORKER_INPUT_WRITE_FAILED":"worker_input_write_failed"}.get(
                    exc.code,"backend_failed")
            result, goal_verified = None,False
            raise _error(terminal_error) from None
        except (AgentError, AgentValidationError) as exc:
            terminal_error = exc.code if isinstance(exc, AgentError) else "invalid_request"
            result, goal_verified = None, False
            raise
        except Exception:
            result, goal_verified = None, False
            if event.is_set() or self._closed.is_set():
                raise _error("cancelled") from None
            raise _error("backend_failed") from None
        finally:
            # Terminal storage must not delay Stop. Closure proof remains with
            # the existing tail settle after persistence and callbacks finish.
            if run_handle is not None:
                self._worker_lifecycle.request_stop(run_handle)
            self._interaction_broker.clear(task_id)
            with self._lock:
                if self._active_control is not None and self._active_control["task_id"] == task_id:
                    self._active_control = None
            if callable(interaction_callback):
                try:
                    interaction_callback(None)
                except Exception:
                    pass
            if self._journal is not None and session_id:
                try:
                    self._journal.append(session_id, task_id, "task_finished",
                        {"status": result.status if result is not None else terminal_error,
                         "goal_verified": bool(result is not None and goal_verified)}, secrets=secrets)
                except Exception:
                    self._checkpoint_warning()
            if session_id and process is not None:
                if not received_records:
                    transcript.append(text_record({"role": "user", "content": request.task}, secrets))
                transcript = close_interrupted_tools(transcript)
                if result is not None:
                    if not transcript or transcript[-1].get("role") != "assistant" or transcript[-1].get("content") != result.reply:
                        transcript.append({"role": "assistant", "content": result.reply})
                else:
                    transcript.append({"role": "assistant", "content":
                        f"[Host outcome: {terminal_error}. The turn ended; completed side effects remain. Inspect actual state before acting. Only the next user turn can authorize continuation.]"})
                with self._lock:
                    if session_epoch == self._session_epoch:
                        self._sessions[session_id] = bounded_history(transcript)
                        self._session_goals[session_id] = list(current_goals)
                        self._session_scopes[session_id] = scope
                        while len(self._sessions) > 64:
                            removed = next(iter(self._sessions))
                            self._sessions.pop(removed)
                            self._session_goals.pop(removed, None)
                            self._session_scopes.pop(removed, None)
                if checkpoint_store is not None:
                    with self._lock:
                        valid_epoch = checkpoint_epoch == self._checkpoint_epoch
                    if valid_epoch:
                        try:
                            checkpoint_store.save(session_id, scope, task_id, transcript,
                                status=result.status if result is not None else
                                    terminal_error if terminal_error in {"cancelled", "timeout"} else "interrupted",
                                goal_verified=result is not None and goal_verified, secrets=secrets,
                                goal_history=current_goals)
                        except (CheckpointError, ValueError, TypeError):
                            self._checkpoint_warning()
            if process is not None:
                receipt = self._settle_worker(run_handle) if run_handle is not None else None
                if receipt is None and callable(diagnostic_callback):
                    try:
                        diagnostic_callback({"phase": "runtime", "code": "WORKER_CLEANUP_UNCONFIRMED"})
                    except Exception:
                        pass
                # Keep exact Popen/tree/pipe ownership for explicit later
                # reconciliation if closure was not proved.

    @staticmethod
    def _parse_frame(raw, task_id):
        try:
            frame = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_unique)
            if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                raise ValueError
            kind = frame["type"]
            if kind == "ready":
                if frame.get("id") not in (None, task_id):
                    raise ValueError
            elif frame.get("id") != task_id:
                raise ValueError
            if kind == "progress":
                if frame.get("phase") not in PROGRESS_MESSAGES:
                    raise ValueError
            elif kind == "interaction":
                checked_interaction(frame, resolve_resource=False)
            elif kind == "resource_request":
                if (frame.get("resource_kind") != "image"
                        or not isinstance(frame.get("resource_id"), str)
                        or not re.fullmatch(r"resource-[a-f0-9]{32}", frame["resource_id"])
                        or not isinstance(frame.get("reference"), str)
                        or not re.fullmatch(r"[a-f0-9-]{24,80}", frame["reference"])):
                    raise ValueError
            elif kind == "diagnostic":
                data = frame.get("data")
                if (not isinstance(data, dict) or set(data) - {"phase", "code", "duration_ms", "attempt", "input_tokens", "output_tokens"}
                        or not isinstance(data.get("phase"), str) or data["phase"] not in {"model", "tool", "context", "runtime"}
                        or not isinstance(data.get("code", ""), str)
                        or not re.fullmatch(r"[A-Z0-9_]{0,80}", data.get("code", ""))
                        or any(type(value) is not int or value < 0 for key, value in data.items()
                               if key not in {"phase", "code"})):
                    raise ValueError
            elif kind == "activity":
                event_kind = frame.get("kind")
                labels = TOOL_LABELS if event_kind == "tool" else ACTION_LABELS if event_kind in ("computer", "progress") else {}
                if (not isinstance(frame.get("name"), str) or frame["name"] not in labels
                        or frame.get("status") not in ("running", "completed", "failed")
                        or not isinstance(frame.get("event_id"), str)
                        or not re.fullmatch(r"[a-z0-9_-]{1,80}", frame["event_id"])
                        or "step" in frame and (type(frame["step"]) is not int or frame["step"] < 0)):
                    raise ValueError
            elif kind == "final":
                checked_assessment(frame.get("acceptance"))
                if (not isinstance(frame.get("reply"), str) or not frame["reply"].strip()
                        or frame.get("emotion", "neutral") not in _EMOTIONS
                        or frame.get("status", "completed") not in
                           ("completed", "cancelled", "timed_out", "needs_user")):
                    raise ValueError
                for field in ("gui_used", "gui_goal_verified"):
                    if field in frame and type(frame[field]) is not bool:
                        raise ValueError
                report = frame.get("verification")
                if report is not None:
                    _verification_timestamp(report)
                if frame.get("gui_goal_verified") is True and (
                        frame.get("gui_used") is not True or report is None
                        or report["status"] != "verified"):
                    raise ValueError
                artifacts = frame.get("artifacts", [])
                if (not isinstance(artifacts, list) or len(artifacts) > 64 or any(
                        not isinstance(item, dict) or not isinstance(item.get("path"), str)
                        or len(item["path"]) > 4096 or item.get("passed") is not True
                        or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                        for item in artifacts)):
                    raise ValueError
            elif kind == "session":
                if set(frame) != {"type", "id", "message"} or not isinstance(frame["message"], dict):
                    raise ValueError
                text_record(frame["message"])
            elif kind == "checkpoint":
                if (set(frame) != {"type", "id", "barrier_id"}
                        or not isinstance(frame.get("barrier_id"), str)
                        or not re.fullmatch(r"checkpoint-[1-9][0-9]{0,8}", frame["barrier_id"])):
                    raise ValueError
            elif kind == "error":
                if not isinstance(frame.get("code"), str) or len(frame["code"]) > 64:
                    raise ValueError
            elif kind != "ready":
                raise ValueError
            return frame
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise _error("protocol") from None
