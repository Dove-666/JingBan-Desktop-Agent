"""Bounded, on-demand nanobot subprocess host, independent of Qt and SDKs."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .contracts import AgentError, AgentRequest, AgentResult, AgentValidationError
from .settings import AGENT_WORKSPACE_ROOT, DEFAULT_PYTHON, AgentSettings, validate_api
from .activity import TOOL_LABELS, ACTION_LABELS, public_activity
from .checkpoints import CheckpointError, CheckpointStore, scope_fingerprint
from agent_backend.nanobot.session_history import (
    bounded_history, close_interrupted_tools, conversation_uuid, current_image, text_record,
)


_ROOT = Path(__file__).resolve().parents[2]
_WORKER_PATH = _ROOT / "agent_backend" / "nanobot" / "worker.py"
_MAX_LINE = 128_000
_EMOTIONS = {"neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talking"}
_GUI_TOOLS = frozenset({"computer_use", "desktop_act", "open_application", "verify_desktop_goal"})
_VERIFICATION_FIELDS = frozenset({"status", "method", "reason", "checked_at"})
_GUI_UNCONFIRMED_REPLY = (
    "操作已经结束，但尚未确认目标确实达成。请检查当前画面；"
    "本次未标记为已完成，也未自动重复点击或提交。"
)
PROGRESS_MESSAGES = {
    "starting": "正在启动任务助手…", "planning": "正在规划任务…",
    "tool": "正在使用任务工具…", "computer": "正在操作电脑…",
    "finishing": "正在整理任务结果…", "completed": "任务已完成。",
}
_ERROR_MESSAGES = {
    "vision_rejected": "图文请求未被模型接口接受，请确认模型支持图片并检查接口配置。",
    "disabled": "任务助手尚未启用，请在设置中启用后再试。",
    "kind_disabled": "这类任务尚未启用，请先检查任务助手设置。",
    "not_configured": "任务助手尚未配置完成，请检查独立 Python 环境和模型设置。",
    "missing_backend": "未找到任务助手后端，请先完成后端安装。",
    "missing_sdk": "任务助手依赖尚未安装，请先完成独立后端环境安装。",
    "sdk_unlimited_unsupported": "当前任务 SDK 与不限步数模式不兼容，任务未执行；请检查本地后端版本。",
    "gui_not_configured": "电脑操作模型尚未配置，请填写 GUI 模型地址、名称和密钥。",
    "cancelled": "任务已停止；已经完成的操作会保留，未自动重试。",
    "timeout": "任务超时并已停止；已经完成的操作会保留，未自动重试。",
    "protocol": "任务助手返回格式无效，已停止且未自动重试。",
    "backend_failed": "任务助手未能完成这次操作；请检查当前结果，再发送新的任务消息。",
    "agent_failed": "任务规划模型请求失败；请检查模型服务是否可用，再发送新的任务消息。",
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
    "gui_model_failed": "电脑操作模型请求失败；请检查视觉模型服务、密钥和动作格式是否可用。",
    "gui_context_length_exceeded": "电脑操作上下文已达到模型服务的实际容量；请新建任务或选择容量更大的模型。",
    "model_context_exceeded": "任务上下文已达到模型服务的实际容量；请新建对话或选择容量更大的模型。",
    "gui_action_failed": "鼠标或键盘操作执行失败，任务已停止；请检查当前画面。",
    "gui_unsupported_action": "模型返回了尚不支持的电脑动作，任务已停止；请检查当前画面。",
    "gui_sdk_failed": "电脑操作组件未能完成任务；请检查画面，再发送新的任务消息。",
    "gui_invalid_request": "电脑操作参数无效；请检查任务助手中的模型类型和动作格式。",
    "gui_invalid_provider": "电脑操作的动作格式不受支持；请检查任务助手设置。",
    "gui_busy": "上一项电脑操作尚未结束；请稍后发送新任务。",
    "gui_worker_failed": "电脑操作后台运行失败；请检查当前结果，再发送新的任务消息。",
    "gui_already_used": "本次电脑操作已经执行，未再次执行；请检查结果后发送新任务。",
    "closed": "任务助手已关闭。",
    "busy": "上一项任务仍在运行或结束中，请稍后发送新任务；本次未启动任务助手。",
    "checkpoint_unavailable": "任务检查点无法可靠读写，本次任务已停止且未继续派发；聊天记录仍保留，请检查本地数据目录后重试。",
}


def _error(code):
    return AgentError(code, _ERROR_MESSAGES.get(code, _ERROR_MESSAGES["backend_failed"]))


def _redact(text, secrets):
    for secret in sorted({value for value in secrets if isinstance(value, str) and value},
                         key=len, reverse=True):
        text = text.replace(secret, "[已隐藏密钥]")
    return text


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
        with self._close_lock:
            if self._job is not None:
                self._kernel.CloseHandle(self._job)
                self._job = None
            elif sys.platform != "win32":
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError:
                    pass


class AgentService:
    """Call execute from a Qt worker. Construction performs no I/O or work."""

    def __init__(self, *, checkpoint_path=None):
        self._lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._processes = {}
        self._closed = threading.Event()
        # Native messages come only from our worker, never phone/chat imports.
        # Production can opt into a separate bounded text checkpoint store;
        # construction does not create files, threads or SQLite connections.
        self._sessions = {}
        self._session_scopes = {}
        self._session_epoch = 0
        self._checkpoint_epoch = 0
        self._checkpoint_store = CheckpointStore(checkpoint_path) if checkpoint_path is not None else None
        self._checkpoint_status = ""
        self._checkpoint_deletions = []
        self._checkpoint_executor = None

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
            data = self._checkpoint_store.load(conversation_id,
                scope_fingerprint(settings, api, secrets=secrets, generation=generation), secrets=secrets)
        except (AgentError, CheckpointError, ValueError, TypeError):
            self._checkpoint_warning()
            return {"status": "unavailable", "goal_verified": False}
        if data is None:
            return None
        return {key: value for key, value in data.items() if key != "history"}

    def clear_session(self, conversation_id=None):
        """Invalidate replay when the host deletes or clears a conversation."""
        if conversation_id is not None:
            conversation_uuid(conversation_id)
        with self._lock:
            self._session_epoch += 1
            self._checkpoint_epoch += 1
            if conversation_id is None:
                self._sessions.clear()
                self._session_scopes.clear()
            else:
                self._sessions.pop(conversation_id, None)
                self._session_scopes.pop(conversation_id, None)
            store = self._checkpoint_store
            if store is not None and not self._closed.is_set():
                if self._checkpoint_executor is None:
                    self._checkpoint_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aura-checkpoint-delete")

                def remove():
                    try:
                        store.clear(conversation_id)
                    except Exception:
                        self._checkpoint_warning()
                        raise

                future = self._checkpoint_executor.submit(remove)
                self._checkpoint_deletions.append((future, conversation_id))
                return future

    @staticmethod
    def capabilities(settings, gui_api_key=""):
        """Describe configured permissions; never enable a flag or capture pixels."""
        tools = bool(settings.enabled and settings.tools_enabled)
        computer = bool(settings.enabled and settings.computer_enabled)
        gui_configured = False
        try:
            validate_api(settings.gui_base_url, settings.gui_model)
            gui_configured = isinstance(gui_api_key, str) and bool(gui_api_key.strip())
        except AgentValidationError:
            pass
        manifest = []
        for name in ("read_file", "write_file", "search_files", "web_fetch", "system_info"):
            manifest.append({"name": name, "available": tools,
                             "reason_code": "" if tools else "kind_disabled"})
        for name in ("calculate", "analyze_csv", "verify_artifact"):
            manifest.append({"name": name, "available": tools,
                             "reason_code": "" if tools else "kind_disabled"})
        for name in ("update_plan", "finish_task"):
            available = tools or computer
            manifest.append({"name": name, "available": available,
                             "reason_code": "" if available else "kind_disabled"})
        for name in ("inspect_system", "inspect_disk_usage", "inspect_directory"):
            available = tools and sys.platform == "win32"
            manifest.append({"name": name, "available": available,
                             "reason_code": "" if available else "kind_disabled" if not tools else "gui_unsupported_platform"})
        discovery = (tools or computer) and sys.platform == "win32"
        manifest.append({"name": "discover_applications", "available": discovery,
                         "reason_code": "" if discovery else "kind_disabled" if not (tools or computer) else "gui_unsupported_platform"})
        manifest.append({"name": "open_application", "available": computer and sys.platform == "win32",
                         "reason_code": "" if computer and sys.platform == "win32" else "kind_disabled" if not computer else "gui_unsupported_platform"})
        manifest.append({"name": "computer_use", "available": computer and gui_configured,
                         "reason_code": "" if computer and gui_configured else "kind_disabled" if not computer else "gui_not_configured"})
        for name in ("desktop_observe", "desktop_act", "desktop_verify_state", "verify_desktop_goal"):
            available = computer and gui_configured and sys.platform == "win32"
            reason = ("" if available else "kind_disabled" if not computer else
                      "gui_unsupported_platform" if sys.platform != "win32" else "gui_not_configured")
            manifest.append({"name": name, "available": available, "reason_code": reason})
        return manifest

    def readiness(self, settings, key="", context=None):
        """Safe status for desktop/phone clients, with the actual enabled tools."""
        try:
            if not isinstance(settings, AgentSettings):
                raise _error("not_configured")
            settings.validate()
            if self._closed.is_set():
                raise _error("closed")
            if not settings.enabled:
                raise _error("disabled")
            if not Path(settings.python_executable or DEFAULT_PYTHON).is_file():
                raise _error("not_configured")
            if not _WORKER_PATH.is_file():
                raise _error("missing_backend")
            if not isinstance(context, dict):
                raise _error("not_configured")
            self._context(context, key, AgentRequest("readiness", "agent"), settings)
            manifest = self.capabilities(settings, context.get("gui_api_key", ""))
            return {"available": True, "reason_code": "", "reason": "",
                    "tools": [item["name"] for item in manifest if item["available"]],
                    "capabilities": manifest}
        except (AgentError, AgentValidationError) as exc:
            code = exc.code if isinstance(exc, AgentError) else "not_configured"
            return {"available": False, "reason_code": code, "reason": _ERROR_MESSAGES[code],
                    "tools": [], "capabilities": []}

    def cancel_request(self, cancellation_event):
        cancellation_event.set()
        with self._lock:
            trees = [tree for event, tree in self._processes.values()
                     if event is cancellation_event]
        for tree in trees:
            tree.close()

    def shutdown(self):
        self._closed.set()
        with self._lock:
            active = list(self._processes.values())
            self._sessions.clear()
            self._session_scopes.clear()
            self._session_epoch += 1
        for event, tree in active:
            event.set()
            tree.close()
        if self._checkpoint_executor is not None:
            # Accepted erasures finish without blocking the GUI shutdown path.
            self._checkpoint_executor.shutdown(wait=False, cancel_futures=False)

    @staticmethod
    def _context(context, key, request, settings):
        if not isinstance(context, dict) or set(context) - {"api", "messages", "gui_api_key", "conversation_id", "conversation_generation", "current_image_url"}:
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
                or set(api) - {"base_url", "model", "timeout_seconds", "reasoning_effort"}):
            raise AgentValidationError("Agent 模型配置格式无效。")
        validate_api(api.get("base_url"), api.get("model"))
        effort = api.get("reasoning_effort", "default")
        if effort not in ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
            raise AgentValidationError("Agent 思考强度无效。")
        gui_key = context.get("gui_api_key", "")
        if (not isinstance(key, str) or not key.strip() or len(key) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in key)):
            raise _error("not_configured")
        if (not isinstance(gui_key, str) or len(gui_key) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in gui_key)):
            raise _error("gui_not_configured")
        if request.kind == "computer":
            validate_api(settings.gui_base_url, settings.gui_model)
            if not gui_key.strip():
                raise _error("gui_not_configured")
        messages = context.get("messages", [])
        if not isinstance(messages, list):
            raise AgentValidationError("Agent 参考上下文过大或格式无效。")
        safe = []
        secrets = (key, gui_key)
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or message["role"] not in ("system", "user", "assistant")
                    or not isinstance(message["content"], str)):
                raise AgentValidationError("Agent 参考消息必须为文本消息。")
            safe.append({"role": message["role"], "content": _redact(message["content"], secrets)})
        text = json.dumps(safe, ensure_ascii=False, separators=(",", ":")) if safe else ""
        config = {"base_url": api["base_url"], "model": api["model"], "api_key": key,
                  "reasoning_effort": effort}
        gui_available = any(item["name"] == "computer_use" and item["available"]
                            for item in AgentService.capabilities(settings, gui_key))
        if request.kind == "computer" or request.kind == "agent" and gui_available:
            config["gui"] = {"base_url": settings.gui_base_url, "model": settings.gui_model,
                             "provider": settings.gui_provider, "api_key": gui_key,
                             "coordinate_profile": settings.gui_coordinate_profile}
        return config, text, secrets

    def execute(self, settings, key, request, context, cancellation_event=None, progress_callback=None, event_callback=None):
        """Allow one task, including its child-process cleanup, at a time."""
        if request is None:
            return None
        if not self._execution_lock.acquire(blocking=False):
            raise _error("busy")
        try:
            return self._execute_impl(settings, key, request, context,
                                      cancellation_event, progress_callback, event_callback)
        finally:
            self._execution_lock.release()

    def _execute_impl(self, settings, key, request, context, cancellation_event=None, progress_callback=None, event_callback=None):
        """Run one explicitly routed task, once. Null requests never spawn."""
        if request is None:
            return None
        if not isinstance(request, AgentRequest):
            raise AgentValidationError("Agent 请求类型无效。")
        request.validate()
        if not isinstance(settings, AgentSettings):
            raise AgentValidationError("Agent 设置类型无效。")
        settings.validate()
        if self._closed.is_set():
            raise _error("closed")
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
        options["workspace"] = str(Path(settings.workspace or AGENT_WORKSPACE_ROOT).resolve())
        options["tools_enabled"] = settings.tools_enabled
        options["computer_enabled"] = settings.computer_enabled
        session_id = context.get("conversation_id") if request.kind == "agent" else None
        try:
            if session_id is not None:
                conversation_uuid(session_id)
            image_url = current_image(context.get("current_image_url"))
        except ValueError:
            raise AgentValidationError("Agent conversation or current image is invalid.") from None
        checkpoint_store = self._checkpoint_store if session_id else None
        options["checkpoint_required"] = checkpoint_store is not None
        scope = scope_fingerprint(settings, config, secrets=secrets,
                                  generation=context.get("conversation_generation", "")) if session_id else None
        with self._lock:
            session_epoch = self._session_epoch
            checkpoint_epoch = self._checkpoint_epoch
            if session_id and self._session_scopes.get(session_id) not in (None, scope):
                self._sessions.pop(session_id, None)
            previous_history = list(self._sessions.get(session_id, [])) if session_id else []
        if checkpoint_store is not None:
            self._flush_checkpoint_deletions()
            try:
                saved = checkpoint_store.load(session_id, scope, secrets=secrets)
                if saved is not None:
                    previous_history = saved["history"]
                    with self._lock:
                        self._checkpoint_status = (
                            "模型、账户或权限已变更，旧工具记录未恢复；本次按当前请求重新开始。"
                            if not saved["scope_matches"] else
                            "已恢复中断任务记录；需要先核对实际结果，再按本次请求继续。"
                            if saved["recovered_running"] else "已恢复当前会话的任务检查点。"
                        )
                checkpoint_store.begin(session_id, scope, task_id,
                    previous_history + [text_record({"role": "user", "content": request.task}, secrets)], secrets=secrets)
                if saved is None:
                    with self._lock:
                        self._checkpoint_status = ""
            except (CheckpointError, ValueError, TypeError):
                self._checkpoint_warning()
                raise _error("checkpoint_unavailable") from None
        payload = {"type": "run", "id": task_id, "task": _redact(request.task, secrets),
                   "kind": request.kind, "context": context_text, "config": config, "options": options}
        if session_id:
            payload.update(conversation_id=session_id, session_history=previous_history)
        if image_url:
            payload["current_image_url"] = image_url
        raw_input = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        environment = {name: value for name, value in os.environ.items()
                       if not re.search(r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|OPENAI|ANTHROPIC|NANOBOT|PYTHONPATH)",
                                        name, re.IGNORECASE)}
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                            "OPENAI_LOG": "error", "NO_COLOR": "1"})
        launch = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                  if sys.platform == "win32" else {"start_new_session": True})
        process, tree = None, None
        transcript = list(previous_history)
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
            process = subprocess.Popen([str(executable), "-I", "-u", str(_WORKER_PATH)],
                cwd=str(_ROOT), env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, **launch)
            tree = _ProcessTree(process)
            with self._lock:
                self._processes[process.pid] = (event, tree)
            if event.is_set() or self._closed.is_set():
                raise _error("cancelled")
            process.stdin.write(raw_input)
            process.stdin.flush()
            buffer = b""
            while True:
                if event.is_set() or self._closed.is_set():
                    raise _error("cancelled")
                if deadline is not None and time.monotonic() >= deadline:
                    raise _error("timeout")
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
                    elif frame_type == "activity":
                        if (frame.get("kind") == "computer"
                                or frame.get("kind") == "tool" and frame.get("name") in _GUI_TOOLS):
                            gui_used = True
                        if callable(event_callback):
                            try:
                                event_callback(public_activity(frame))
                            except Exception:
                                pass
                    elif frame_type == "session":
                        if not session_id:
                            raise _error("protocol")
                        try:
                            message = text_record(frame["message"], secrets)
                            transcript.append(message)
                            if any(call.get("function", {}).get("name") in _GUI_TOOLS
                                   for call in message.get("tool_calls", [])):
                                gui_used = True
                        except (ValueError, TypeError):
                            raise _error("protocol") from None
                        received_records += 1
                        with self._lock:
                            if session_epoch == self._session_epoch:
                                self._sessions[session_id] = bounded_history(transcript)
                                self._session_scopes[session_id] = scope
                                while len(self._sessions) > 64:
                                    removed = next(iter(self._sessions))
                                    self._sessions.pop(removed)
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
                                                  barrier=barrier, secrets=secrets)
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
                        process.stdin.write((json.dumps({"type": "checkpoint_ack", "id": task_id,
                            "barrier_id": frame["barrier_id"]}) + "\n").encode("utf-8"))
                        process.stdin.flush()
                    elif frame_type == "error":
                        code = frame["code"].casefold()
                        code = {"timed_out": "timeout", "missing_node": "missing_sdk",
                                "missing_dependencies": "missing_sdk"}.get(code, code)
                        raise _error(code if code in _ERROR_MESSAGES else "backend_failed")
                    elif frame_type == "final":
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
                        result = AgentResult(_redact(reply, secrets), emotion, request.kind, status=status)
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
                        self._session_scopes[session_id] = scope
                        while len(self._sessions) > 64:
                            removed = next(iter(self._sessions))
                            self._sessions.pop(removed)
                            self._session_scopes.pop(removed, None)
                if checkpoint_store is not None:
                    with self._lock:
                        valid_epoch = checkpoint_epoch == self._checkpoint_epoch
                    if valid_epoch:
                        try:
                            checkpoint_store.save(session_id, scope, task_id, transcript,
                                status=result.status if result is not None else
                                    terminal_error if terminal_error in {"cancelled", "timeout"} else "interrupted",
                                goal_verified=result is not None and goal_verified, secrets=secrets)
                        except (CheckpointError, ValueError, TypeError):
                            self._checkpoint_warning()
            if process is not None:
                with self._lock:
                    self._processes.pop(process.pid, None)
                if tree is not None:
                    tree.close()
                elif process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()

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
