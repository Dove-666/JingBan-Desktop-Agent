"""Persistent, isolated chat sessions with asynchronous storage and requests."""

from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import base64
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import threading
import weakref
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, Qt, Signal, Slot, QTimer, QThread

from .api_client import ApiClient, ApiError, ChatReply, EMOTIONS, MAX_REPLY_CHARACTERS, SYSTEM_PROMPT, redact_secret
from .async_tasks import TaskRunner
from .conversation_metadata import context_summary, update_session_metadata
from .conversations import ConversationStore, ContextAssembler, ContextBudget, ContextBudgetError, StoreError
from .conversations.export import export_conversation_file
from .conversations.chronology import timestamp_key
from .settings_manager import ApiSettings, CredentialsError, SettingsError, SettingsManager
from .character_cards import CharacterCard, CharacterCardError
from .character_prompts import (
    DIRECT_AGENT_PROMPT, character_system_prompt, direct_agent_system_prompt,
    validate_character_prompt, harness_personality,
)
from .selection.context import SelectionContext, SelectionContextError
from .chat_images import IMAGE_ERRORS, IMAGE_ID, references_previous_image, compares_previous_image
from .agents import AgentError, AgentRequest, AgentService, AgentSettings, AgentValidationError
from .agents.settings import shared_api_settings
from .agents.routing import agent_system_prompt, routing_allowed
from .agents.service import _ERROR_MESSAGES as AGENT_ERRORS
from .agents.artifacts import checked_artifact_metadata, MAX_ARTIFACT_BYTES
from .agents.job_queue import JobQueue
from .agents.coordinator import HostAgentCoordinator, CoordinatorError
from .execution_trace import ExecutionTrace, ExecutionViewUpdate, merge_execution, text_messages
from .draft_owner import LocalDraftOwner
from .image_storage import ImageStorageGate, ImageStorageGateError
from desktop_pet.redaction import redact_text


def _image_import_failure(exception):
    """Classify only trusted inbox/storage failures; never forward their text."""
    from .remote.errors import RemoteError
    if isinstance(exception, RemoteError) and exception.code == "storage_full":
        return ApiError("image_storage_full", "图片存储空间已满（64 MiB），本次图片未添加，文字已保留。")
    if (isinstance(exception, (OSError, sqlite3.Error))
            or isinstance(exception, RemoteError) and exception.code == "storage_error"):
        return ApiError("image_storage_error", "无法读取或保存图片，请检查本地可用空间和读写权限后重试；文字已保留。")
    return ApiError("image_unavailable", "无法读取图片；请选择有效的 PNG、JPEG 或 WebP 图片，最多 4 MiB、1600 万像素。")

MAX_INPUT_CHARACTERS = 8_000
# Kept as import-compatible constants. The archive is complete; request context
# is bounded by ContextBudget rather than the number of displayed messages.
MAX_VISIBLE_MESSAGES = 50
MAX_CONTEXT_TURNS = 12
MAX_CONTEXT_CHARACTERS = 48_000
_CURRENT_CHARACTER = object()


def _agent_uuid(value):
    try: return type(value) is str and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, TypeError, AttributeError): return False


REQUEST_PHASES = {
    "saving_message": (0, "正在保存消息…"),
    "retrieving_memory": (1, "正在检索相关记忆…"),
    "generating_reply": (2, "正在生成回复…"),
    "agent_starting": (2, "正在启动任务助手…"),
    "agent_planning": (2, "正在规划任务…"),
    "agent_tool": (2, "正在使用任务工具…"),
    "agent_computer": (2, "正在操作电脑…"),
    "agent_finishing": (2, "正在整理任务结果…"),
    "agent_verifying": (2, "正在核验任务结果…"),
    "agent_completed": (2, "正在整理任务结果…"),
    "saving_reply": (3, "正在保存回复…"),
}
RECOVERABLE_STATUSES = {"failed", "cancelled", "interrupted"}
AGENT_REPLAY_STATUS = "该任务已启动；请检查已完成的操作，再发新的任务消息。"
REPLY_COMMIT_UNCONFIRMED = "回复保存结果暂时无法确认，请重新读取此会话。"
DESKTOP_CAPTURE_UNAVAILABLE_NOTICES = {
    "own_window": ("\n\n[电脑协助状态：本轮未提供截图，前台是鲸伴自身窗口。"
                   "不能声称看到了当前桌面或软件图标。此状态不构成操作授权；"
                   "仅依据用户本次明确指令使用已启用的任务助手，并通过工具重新验证画面。]")
}
MODEL_LIST_ERRORS = {
    "not_configured": "尚未配置 API Key，请先填写密钥。",
    "authentication": "API Key 无效或认证失败，请检查密钥。",
    "permission": "认证或访问权限不足，请检查 API Key 权限。",
    "models_unsupported": "当前服务商不支持获取模型列表，请手动填写模型名称。",
    "empty_models": "服务商未返回可用模型，请手动填写模型名称。",
    "models_incomplete": "服务商的模型列表未能完整获取，请手动填写模型名称。",
    "invalid_response": "服务商返回的模型列表格式不兼容，请手动填写模型名称。",
    "network": "网络连接失败，请检查 Base URL、代理、证书和网络连接。",
    "timeout": "获取模型列表超时，请检查网络或稍后重试。",
    "rate_limit": "请求受到限流或额度不足，请稍后重试。",
    "cancelled": "获取模型列表已取消。",
    "provider": "服务商返回错误，请检查接口设置或稍后重试。",
}
MODEL_LIST_FAILURE = "获取模型列表失败，请检查 API 设置和网络后重试。"


def _collect_redaction_keys(settings_manager, key=""):
    keys = [key] if key else []
    getter = getattr(settings_manager, "get_known_api_keys", None)
    if callable(getter):
        keys.extend(getter())
    return tuple(keys)


class ChatController(QObject):
    messages_changed = Signal()
    messages_view_changed = Signal(object)
    busy_changed = Signal(bool)
    work_active_changed = Signal(bool)
    error_changed = Signal(str)
    emotion_requested = Signal(str)
    reply_received = Signal(str, str, object)
    connection_status_changed = Signal(str)
    conversations_changed = Signal()
    conversation_changed = Signal()
    history_busy_changed = Signal(bool)
    message_focus_requested = Signal(str)
    context_usage_changed = Signal(str)
    screen_comment_finished = Signal(object, str)
    conversation_invalidated = Signal(str)
    memory_status_changed = Signal(str)
    memory_saved = Signal()
    export_busy_changed = Signal(bool)
    export_finished = Signal(str, str, str)
    request_status_changed = Signal(str)
    retry_availability_changed = Signal()
    reading_font_changed = Signal(int)
    character_changed = Signal()
    agent_settings_changed = Signal()
    agent_interaction_changed = Signal(object)
    _request_phase_changed = Signal(int, str, str)
    _execution_changed = Signal(str, str, object)
    streaming_reply_changed = Signal()
    _stream_delta = Signal(int, str, str)
    _agent_queue_wake = Signal()
    image_storage_changed = Signal()
    image_storage_content_changed = Signal(str)
    _image_storage_completion = Signal(object)

    def __init__(self, settings_manager: SettingsManager, api_client: ApiClient | None = None,
                 parent: QObject | None = None, *, store: ConversationStore | None = None,
                 memory_service=None, context_budget: ContextBudget | None = None,
                 metadata_client_factory=None, agent_service=None):
        super().__init__(parent)
        self._image_storage_gate = ImageStorageGate()
        self._image_storage_owner = self._image_storage_gate.create_owner()
        self._image_storage_epoch_lock = threading.Lock()
        self._image_storage_epoch = 0
        self._image_storage_manager = None
        self._image_storage_completion.connect(self._finish_image_storage_task, Qt.ConnectionType.QueuedConnection)
        self._settings_manager = settings_manager
        character_loader = getattr(settings_manager, "load_character_card", None)
        saved_character = character_loader() if callable(character_loader) else None
        self._character_card = saved_character if isinstance(saved_character, CharacterCard) else None
        self._client = api_client if api_client is not None else ApiClient()
        # SQLite is opened lazily by execute on its worker thread. Keep test
        # doubles/relative paths memory-only and production state beside the
        # existing per-user conversation database, outside the checkout.
        configuration_path = getattr(settings_manager, "config_path", "")
        checkpoint_path = (Path(configuration_path).parent / "agent_checkpoints.sqlite3"
                           if configuration_path and Path(configuration_path).is_absolute() else None)
        self._agent_service = (agent_service if agent_service is not None
                               else AgentService(checkpoint_path=checkpoint_path))
        self._agent_settings = replace(AgentSettings(), enabled=False)
        self.refresh_agent_settings()
        self._memory = memory_service
        self._image_inbox = None
        self._temporary = None
        if store is None:
            config_path = getattr(settings_manager, "config_path", "")
            if config_path:
                path = Path(config_path).parent / "conversations.sqlite3"
            else:
                # In-memory Settings test doubles must not touch real user data.
                self._temporary = tempfile.TemporaryDirectory(prefix="aura-chat-")
                self._temporary_cleanup = weakref.finalize(self, self._temporary.cleanup)
                path = Path(self._temporary.name) / "conversations.sqlite3"
            store = ConversationStore(path)
        self.store = store
        budget_loader = getattr(settings_manager, "load_context_budget", None)
        self._budget = context_budget or (budget_loader() if callable(budget_loader) else ContextBudget())
        self._budget.validate()
        font_loader = getattr(settings_manager, "load_chat_font_size", None)
        self._reading_font_size = font_loader() if callable(font_loader) else 16
        self._runner = TaskRunner(self)
        self._stop = threading.Event()
        self._closed = False
        self._busy = False
        self._work_active = False
        self._export_busy = False
        self._history_loading = False
        self._error = ""
        self._error_action = ""
        self._error_conversation_id = ""
        self._error_message_id = ""
        self._connection_status = "尚未测试连接。"
        self._messages = []
        self._conversations = []
        self._current_id = str(uuid4())
        self._current_title = "新对话"
        self._current_summary = ""
        self._context_usage = "尚未发送请求；完整历史与有限模型上下文分别管理。"
        self._history_restore_view = None
        self._request_id = 0
        self._streaming_reply = None
        self._active_request_conversation = None
        self._request_kind = ""
        self._request_phase = ""
        self._request_phase_rank = -1
        self._request_status = ""
        self._load_id = 0
        self._list_id = 0
        self._query = ""
        self._listed_query = ""
        self._retry_messages = {}
        self._recoverable_messages = {}
        self._requests = {}
        self._request_owners = {}
        self._message_locks = {}
        self._active_request = None
        self._pending_screen_count = {}
        self._pending_cancel_count = {}
        self._invalidating_conversations = set()
        self._deleted_conversations = set()
        self._unconfirmed_delete_reads = {}
        self._unconfirmed_reply_commits = {}
        self._historical_reply_candidates = {}
        self._metadata_active = set()
        self._metadata_clients = set()
        self._metadata_factory = metadata_client_factory
        if self._metadata_factory is None and isinstance(self._client, ApiClient):
            self._metadata_factory = ApiClient
        self._request_phase_changed.connect(self._accept_request_phase, Qt.ConnectionType.QueuedConnection)
        self._execution_changed.connect(self._accept_execution, Qt.ConnectionType.QueuedConnection)
        self._stream_delta.connect(self._accept_stream_delta, Qt.ConnectionType.QueuedConnection)
        self.conversation_changed.connect(self._clear_stream_preview)
        self.conversation_changed.connect(self._refresh_request_status)
        self.conversation_invalidated.connect(self._clear_agent_session)
        self._agent_queue = None
        self._agent_queue_policy = None
        self._agent_queue_timer = QTimer(self)
        self._agent_queue_timer.setInterval(5000)
        self._agent_queue_timer.timeout.connect(self._pump_agent_queue)
        self._agent_queue_wake.connect(self._pump_agent_queue, Qt.ConnectionType.QueuedConnection)
        if isinstance(self.store, ConversationStore):
            queue = JobQueue(self.store.path.with_name("agent_jobs.sqlite3"))
            self._agent_queue = HostAgentCoordinator(queue,self.store,self._agent_service,
                configuration=self._current_agent_queue_policy,
                invalidated=lambda cid:self._closed or cid in self._invalidating_conversations or cid in self._deleted_conversations,
                completion_invalidated=lambda cid:cid in self._invalidating_conversations or cid in self._deleted_conversations,
                start=lambda job,message,policy:self._begin_chat(message,policy["api"],policy["key"],job_context=job),
                idle=lambda:not self._busy and not self._history_loading and not self._requests,
                wake=self._agent_queue_wake.emit,
                allow_new=lambda cid:cid not in self._deleted_conversations and cid not in self._invalidating_conversations)
            self._agent_queue.initialize()
            self._capture_agent_queue_policy()
            if self._agent_queue.recovery_pending: self._agent_queue_timer.start()
        self.busy_changed.connect(lambda busy:self._agent_queue_wake.emit() if not busy else None)
        self.history_busy_changed.connect(lambda busy:self._agent_queue_wake.emit() if not busy else None)
        self.conversation_invalidated.connect(self._invalidate_queued_agents)
        self._agent_queue_wake.emit()
        self._refresh_conversations()

    def agent_authority_guard(self):
        queue=getattr(self,"_agent_queue",None)
        return queue.lock if queue is not None else nullcontext()

    @property
    def image_storage_gate(self):
        return self._image_storage_gate

    @property
    def image_storage_manager(self):
        """Lazy native-only manager; constructing it performs no storage IO."""
        if self._image_storage_manager is None:
            from .image_storage_manager import ImageStorageManager
            self._image_storage_manager = ImageStorageManager(self)
        return self._image_storage_manager

    def notify_image_storage_changed(self):
        self.image_storage_changed.emit()

    @property
    def image_storage_epoch(self):
        """Host-only content generation, readable from image workers and views."""
        with self._image_storage_epoch_lock:
            return self._image_storage_epoch

    def mark_image_storage_mutated(self, image_id):
        """Invalidate before releasing a physical image mutation's gate token.

        Signals may reach Qt later; readers compare the generation directly so
        an older queued preview cannot restore bytes from before the mutation.
        This changes no archive metadata or phone synchronization protocol.
        """
        if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
            raise ValueError("IMAGE_STORAGE_INVALID_IMAGE")
        with self._image_storage_epoch_lock:
            self._image_storage_epoch += 1
        self.image_storage_content_changed.emit(image_id)
        self.notify_image_storage_changed()

    def mark_image_storage_deleted(self, image_id):
        self.mark_image_storage_mutated(image_id)

    @Slot(object)
    def _finish_image_storage_task(self, completion):
        completion()

    def _image_storage_admission(self, *, feedback=True):
        try:
            return self._image_storage_gate.activity(self._image_storage_owner)
        except ImageStorageGateError:
            if feedback and not self._closed:
                self._set_error("图片存储正在维护，请稍后重试。")
            return None

    def _submit_image_storage_task(self, function, callback, *, on_submit_failed=None):
        """Keep physical work and queued GUI completion as separate lifetimes.

        Releasing a running physical token only requests discard, so a start
        exception cannot pretend that an accepted runnable never entered. The
        GUI token bridges even an exceptional function return to its callback.
        Shutdown retires pending GUI tokens only after real runner drain.
        """
        gate = self._image_storage_gate
        physical = gate.activity(self._image_storage_owner)
        ui_completion = gate.activity(self._image_storage_owner)
        state_lock = threading.Lock()
        state = {"done": False, "submit_failed": False, "delivered": False,
                 "result": None, "error": ""}

        def notify():
            try: self.notify_image_storage_changed()
            except RuntimeError: pass

        def complete(result, error):
            with state_lock:
                if state["delivered"]: return
                state["delivered"] = True
            try:
                if not self._closed:
                    callback(result, error)
            finally:
                gate.release(ui_completion)
                notify()

        def queue_completion():
            with state_lock:
                result, error = state["result"], state["error"]
            try:
                self._image_storage_completion.emit(lambda: complete(result, error))
            except RuntimeError:
                gate.release(ui_completion)

        def run():
            try:
                result = gate.run(physical, function)
                with state_lock: state["result"] = result
                return result
            except BaseException as exc:
                with state_lock:
                    state["error"] = (str(exc) if type(exc).__module__.startswith("desktop_pet.")
                                      else "操作失败，请检查本地存储、配置或网络后重试。")
                raise
            finally:
                with state_lock:
                    state["done"] = True
                    fallback = state["submit_failed"]
                if fallback:
                    queue_completion()
                elif self._closed:
                    gate.release(ui_completion)
                notify()

        def submission_failed():
            if callable(on_submit_failed): on_submit_failed()
            with state_lock:
                state["submit_failed"] = True
                if state["result"] is None and not state["error"]:
                    state["error"] = "操作未能启动，请检查本地资源后重试。"
                done = state["done"]
            cancelled_before_entry = gate.release(physical)
            if done or cancelled_before_entry:
                queue_completion()
            notify()

        notify()
        try:
            accepted = self._runner.submit(run, complete)
        except BaseException as exc:
            submission_failed()
            if not isinstance(exc, Exception): raise
            return False
        if accepted is None:
            submission_failed()
            return False
        return True

    def _capture_agent_queue_policy(self):
        # QSettings and credential APIs belong exclusively to the Qt thread.
        # Native worker guards read only this immutable captured snapshot.
        if QThread.currentThread()!=self.thread(): return False
        try:
            if getattr(self,"_agent_settings_read_failed",False): raise ValueError("QUEUE_CONFIGURATION_UNAVAILABLE")
            api=self._settings_manager.load_api_settings()
            key=self._settings_manager.get_api_key()
            api.validate()
            agent_settings = shared_api_settings(self._agent_settings, api)
            agent_settings.validate()
            if not isinstance(key,str) or not key: raise ValueError("QUEUE_CONFIGURATION_UNAVAILABLE")
            policy={"api":api,"key":key,"agent":agent_settings,"gui_key":key}
        except Exception:
            policy=None
        with self.agent_authority_guard(): self._agent_queue_policy=policy
        return policy is not None

    def _current_agent_queue_policy(self):
        # DSH owns its runtime configuration. Queue authority still uses the
        # GUI-thread API/permission snapshot and never reads the vault here.
        policy = self._agent_queue_policy
        if policy is None: return None
        return dict(policy)

    def refresh_agent_queue_policy(self):
        """Qt settings publication; failed reads revoke instead of preserving stale authority."""
        try:
            previous=getattr(self,"_agent_queue_policy",None)
            self._capture_agent_queue_policy()
            active=getattr(self,"_active_request",None)
            if previous!=self._agent_queue_policy and active is not None and active.get("agent_kind"):
                self.stop_request(active["conversation_id"])
            if self._agent_queue is not None:
                self._agent_queue.authority_changed(self.stop_request)
        except Exception:
            with self.agent_authority_guard(): self._agent_queue_policy=None
        return self._agent_queue_policy is not None

    def bind_remote_agent_authority(self, provider):
        if self._agent_queue is not None: self._agent_queue.bind_authority(provider)

    def remote_agent_authority_changed(self):
        if self._agent_queue is not None: self._agent_queue.authority_changed(self.stop_request)

    def admit_remote_agent(self,peer,request_id,cid,text,*,image_id=None):
        if self._closed: raise CoordinatorError("QUEUE_UNAVAILABLE")
        admission = self._image_storage_admission(feedback=False)
        if admission is None: raise CoordinatorError("QUEUE_BUSY")
        try:
            return self._image_storage_gate.run(admission,
                lambda: self._admit_remote_agent(peer,request_id,cid,text,image_id=image_id))
        finally:
            self.notify_image_storage_changed()

    def _admit_remote_agent(self,peer,request_id,cid,text,*,image_id=None):
        if self._agent_queue is None: raise CoordinatorError("QUEUE_UNAVAILABLE")
        self._capture_agent_queue_policy()
        result=self._agent_queue.admit(peer,request_id,cid,text,image_id=image_id)
        if cid==self._current_id and not any(item["id"]==result["message_id"] for item in self._messages):
            message=self.store.read_message(cid,result["message_id"])
            if message is not None:
                self._messages.append(message)
                self._notify_messages_changed()
        self._refresh_conversations()
        return result

    def recover_remote_agent_ack(self,peer,request_id):
        return self._agent_queue.recover_ack(peer,request_id) if self._agent_queue is not None else None

    def cancel_queued_agent(self,cid):
        return self._agent_queue.cancel(cid,self.stop_request) if self._agent_queue is not None else False

    @Slot()
    def _pump_agent_queue(self):
        if self._closed or self._agent_queue is None: return
        admission = self._image_storage_admission(feedback=False)
        if admission is None: return
        # Enclose the whole claim/configuration/start chain; an idle check alone
        # would leave a maintenance race before the durable claim transaction.
        self._image_storage_gate.run(admission, self._pump_admitted_agent_queue)

    def _pump_admitted_agent_queue(self):
        self._capture_agent_queue_policy()
        try: self._agent_queue.pump()
        except Exception:
            self._agent_queue._failed=True
        if self._agent_queue._active is not None or self._agent_queue.recovery_pending:
            if not self._agent_queue_timer.isActive(): self._agent_queue_timer.start()
        else:
            self._agent_queue_timer.stop()

    def _invalidate_queued_agents(self,cid):
        if not _agent_uuid(cid): return
        if self._agent_queue is not None: self._agent_queue.invalidate(cid)

    @property
    def messages(self):
        return deepcopy(self._messages)

    @property
    def streaming_reply(self):
        value = self._streaming_reply
        return deepcopy(value) if value and value["conversationId"] == self._current_id else None

    def _clear_stream_preview(self):
        if self._streaming_reply is not None:
            self._streaming_reply = None
            self.streaming_reply_changed.emit()

    @Slot(int, str, str)
    def _accept_stream_delta(self, token, conversation_id, text):
        request = self._active_request
        if (self._closed or not self._busy or request is None or request.get("committed")
                or request["cancel"].is_set() or token != self._request_id
                or conversation_id != self._active_request_conversation
                or conversation_id != self._current_id or conversation_id in self._invalidating_conversations
                or conversation_id in self._deleted_conversations or not isinstance(text, str) or not text):
            return
        self._streaming_reply = {"conversationId": conversation_id, "requestId": str(token),
                                 "text": text, "state": "streaming"}
        self.streaming_reply_changed.emit()

    def _interrupt_stream_preview(self, token, conversation_id):
        value = self._streaming_reply
        if value and value["requestId"] == str(token) and value["conversationId"] == conversation_id:
            value["state"] = "interrupted"
            self.streaming_reply_changed.emit()

    @property
    def agent_settings(self):
        return self._agent_settings

    def refresh_agent_settings(self):
        with self.agent_authority_guard(): self._refresh_agent_settings()

    def _refresh_agent_settings(self):
        previous = self._agent_settings
        loader = getattr(self._settings_manager, "load_agent_settings", None)
        self._agent_settings_read_failed=False
        try:
            value = loader() if callable(loader) else replace(AgentSettings(), enabled=False)
            value.validate()
        except Exception:
            self._agent_settings_read_failed=True
            value = replace(AgentSettings(), enabled=False)
        self._agent_settings = value
        if getattr(self,"_agent_queue",None) is not None: self.refresh_agent_queue_policy()
        active = getattr(self, "_active_request", None)
        if active is not None and active.get("agent_kind") and (
                not value.enabled or active["agent_kind"] == "computer" and not value.computer_enabled
                or active["agent_kind"] == "tools" and not value.tools_enabled
                or active["agent_kind"] == "agent" and previous != value):
            self.stop_request()
        self.agent_settings_changed.emit()

    @property
    def agent_mode(self):
        loader = getattr(self._settings_manager, "load_conversation_agent_mode", None)
        return bool(loader(self._current_id)) if callable(loader) else getattr(self, "_agent_modes", {}).get(self._current_id, False)

    def set_agent_mode(self, enabled):
        if type(enabled) is not bool or self._busy or self._history_loading:
            return False
        saver = getattr(self._settings_manager, "save_conversation_agent_mode", None)
        try:
            if callable(saver):
                saver(self._current_id, enabled)
            else:
                if not hasattr(self, "_agent_modes"):
                    self._agent_modes = {}
                self._agent_modes[self._current_id] = enabled
        except (SettingsError, ValueError):
            self._set_error("Agent 模式未能保存，请检查本地配置。")
            return False
        self.conversation_changed.emit()
        return True

    def agent_readiness(self):
        try:
            api = self._settings_manager.load_api_settings()
            key = self._settings_manager.get_api_key()
            service = self._agent_service if isinstance(self._agent_service, AgentService) else AgentService()
            return service.readiness(shared_api_settings(self._agent_settings, api), key,
                {"api": api, "messages": [], "gui_api_key": key})
        except Exception:
            return {"available": False, "reason_code": "not_configured", "reason": "电脑 Agent 配置尚未就绪。", "tools": []}

    @property
    def agent_running_conversation_id(self):
        request = self._active_request
        return request["conversation_id"] if request is not None and request.get("agent_kind") else None

    @property
    def pending_agent_interaction(self):
        pending = getattr(self._agent_service, "pending_interaction", None)
        return pending(self._current_id) if callable(pending) else None

    def agent_interaction_for(self, conversation_id):
        pending = getattr(self._agent_service, "pending_interaction", None)
        return pending(conversation_id) if callable(pending) else None

    @property
    def agent_artifacts(self):
        return self.agent_artifacts_for(self._current_id)

    def agent_artifacts_for(self, conversation_id):
        artifacts = getattr(self._agent_service, "artifacts", None)
        if not callable(artifacts):
            return []
        try:
            return artifacts(conversation_id)
        except (ValueError, OSError):
            return []

    def agent_artifact_page(self, conversation_id, reference, *, offset=0, limit=262144):
        getter = getattr(self._agent_service, "artifact_page", None)
        if not callable(getter):
            raise ValueError("ARTIFACT_UNAVAILABLE")
        return getter(conversation_id, reference, offset=offset, limit=limit)

    def export_agent_artifact(self, reference, path, *, conversation_id=None):
        """The native Save dialog supplies path; web/model callers never do."""
        cid = conversation_id or self._current_id
        if cid != self._current_id or getattr(self, "_closed", False):
            return False
        target = Path(path).expanduser().resolve()
        if not target.parent.is_dir() or target.is_dir():
            return False
        descriptor, temporary = tempfile.mkstemp(prefix=".aura-export-", dir=target.parent)
        try:
            digest = hashlib.sha256()
            offset, metadata = 0, None
            with os.fdopen(descriptor, "wb") as stream:
                snapshot = getattr(getattr(self, "_agent_service", None), "artifact_snapshot", None)
                if callable(snapshot):
                    value = snapshot(cid, reference)
                    if not isinstance(value, tuple) or len(value) != 2:
                        raise ValueError("ARTIFACT_CHANGED")
                    metadata, raw = value
                    metadata = checked_artifact_metadata(metadata, reference)
                    if (not isinstance(raw, bytes) or len(raw) > MAX_ARTIFACT_BYTES
                            or len(raw) != metadata["bytes"] or hashlib.sha256(raw).hexdigest() != metadata["sha256"]):
                        raise ValueError("ARTIFACT_CHANGED")
                    if cid != self._current_id or getattr(self, "_closed", False):
                        raise ValueError("ARTIFACT_UNAVAILABLE")
                    stream.write(raw)
                    digest.update(raw)
                    offset = len(raw)
                else:
                    # Older host/custom services retain the existing paged
                    # protocol. A failed snapshot never falls back/retries.
                    while True:
                        page = self.agent_artifact_page(cid, reference, offset=offset)
                        if metadata is None:
                            metadata = page
                        elif any(page[key] != metadata[key] for key in ("reference", "name", "mime", "bytes", "sha256", "verification_scope")):
                            raise ValueError("ARTIFACT_CHANGED")
                        raw = base64.b64decode(page["data_base64"], validate=True)
                        stream.write(raw)
                        digest.update(raw)
                        offset += len(raw)
                        if page["next_offset"] is None:
                            break
                        if page["next_offset"] != offset:
                            raise ValueError("INVALID_ARTIFACT_PAGE")
                stream.flush()
                os.fsync(stream.fileno())
            if (offset != metadata["bytes"] or digest.hexdigest() != metadata["sha256"]
                    or Path(temporary).stat().st_size != offset):
                raise ValueError("ARTIFACT_CHANGED")
            if cid != self._current_id or getattr(self, "_closed", False):
                raise ValueError("ARTIFACT_UNAVAILABLE")
            os.replace(temporary, target)
            return True
        except (ValueError, OSError):
            return False
        finally:
            Path(temporary).unlink(missing_ok=True)

    def answer_agent_interaction(self, identity, answer, conversation_id=None):
        cid = conversation_id or self._current_id
        respond = getattr(self._agent_service, "answer_interaction", None)
        if not callable(respond) or cid != self.agent_running_conversation_id:
            return False
        try:
            accepted = bool(respond(cid, identity, answer))
        except (ValueError, OSError):
            return False
        if accepted:
            self.agent_interaction_changed.emit(None)
        return accepted

    def steer_agent(self, text, conversation_id=None):
        cid = conversation_id or self._current_id
        steer = getattr(self._agent_service, "steer", None)
        if (not callable(steer) or cid != self.agent_running_conversation_id
                or not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_CHARACTERS):
            return False
        text = self._redact_known_secrets(text.strip())
        if not steer(cid, text):
            return False
        message = {"id": str(uuid4()), "conversation_id": cid, "role": "user", "content": text,
                   "source": "user", "metadata": {"execution_mode": "agent", "agent_steering": True,
                                                      "request_status": "complete"},
                   "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
        if cid == self._current_id:
            self._messages.append(deepcopy(message))
            self._notify_messages_changed()
        def persist():
            return self.store.append_message(cid, "user", text, "user", message_id=message["id"],
                                             metadata=message["metadata"], created_at=message["created_at"])
        def finished(_, error):
            if error:
                self._set_error("补充指令已发送，但聊天记录保存未完成。")
            self.conversations_changed.emit()
        self._runner.submit(persist, finished)
        return True

    def _clear_agent_session(self, conversation_id):
        # Legacy archive IDs remain valid chat IDs, but cannot mint canonical
        # native execution/queue authority or a checkpoint cleanup request.
        if not _agent_uuid(conversation_id): return
        clear = getattr(self._agent_service, "clear_session", None)
        if callable(clear):
            future = clear(conversation_id)
            if future is not None and callable(getattr(future, "result", None)):
                def finished(_, error):
                    if error:
                        self._set_error("对话已更新，但 Agent 恢复记录清理未完成。再次执行前将重新检查，旧操作不会自动重放。")
                self._runner.submit(lambda: future.result(timeout=10), finished)

    def _routing_for_turn(self, text, metadata):
        return metadata.get("execution_mode") != "agent" and self._agent_settings.enabled and routing_allowed(
            text, selection_context=metadata.get("selection_context") is not None)

    def _redact_known_secrets(self, text, key=""):
        """Keep either configured API credential out of chat data and output."""
        return redact_text(text, _collect_redaction_keys(self._settings_manager, key), "[已隐藏密钥]")

    def _view_redactor(self):
        """Sample once inside a single view operation, only if it has a trace."""
        pending = object()
        keys = pending

        def redact(text):
            nonlocal keys
            if keys is pending:
                # Reentry or a failed sample must refuse this operation's
                # trace instead of rereading or projecting it unredacted.
                keys = None
                keys = _collect_redaction_keys(self._settings_manager)
            if keys is None:
                raise ValueError("VIEW_REDACTION_UNAVAILABLE")
            return redact_text(text, keys, "[已隐藏密钥]")

        return redact

    def _redact_reference(self, value, key=""):
        if isinstance(value, str):
            return self._redact_known_secrets(value, key)
        if isinstance(value, list):
            return [self._redact_reference(item, key) for item in value]
        if isinstance(value, dict):
            # Resolved image bytes are data, not text; substitution corrupts them.
            if value.get("type") == "image_url" and isinstance(value.get("image_url"), dict):
                return deepcopy(value)
            return {name: self._redact_reference(item, key) for name, item in value.items()}
        return value

    @property
    def conversations(self):
        return deepcopy(self._conversations)

    @property
    def current_conversation_id(self):
        return self._current_id

    @property
    def current_title(self):
        return self._current_title

    @property
    def current_summary(self):
        return self._current_summary

    @property
    def history_loading(self):
        return self._history_loading

    @property
    def busy(self):
        return self._busy

    @property
    def work_active(self):
        """An Agent task is running, independently of the visible chat."""
        return self._work_active

    @property
    def request_status(self):
        return self._request_status

    @property
    def error(self):
        return self._error

    @property
    def error_action(self):
        return self._error_action

    @property
    def error_conversation_id(self):
        return self._error_conversation_id

    @property
    def error_message_id(self):
        return self._error_message_id

    @property
    def request_conversation_id(self):
        return self._active_request_conversation or ""

    @property
    def can_stop(self):
        return bool(self._busy and self._active_request and self._request_kind == "chat")

    @property
    def reading_font_size(self):
        return self._reading_font_size

    @property
    def character_card(self):
        return self._character_card

    def set_character_card(self, card):
        if self._closed:
            return False
        try:
            if card is not None:
                if not isinstance(card, CharacterCard):
                    raise CharacterCardError("角色卡格式无效，请重新导入。")
                card = CharacterCard.from_dict(card.to_dict())
                prompts = (character_system_prompt(card), direct_agent_system_prompt(card),
                           agent_system_prompt(character_system_prompt(card)))
                for prompt in prompts:
                    validate_character_prompt(prompt)
                active_prompts = prompts if self._agent_settings.enabled else prompts[:1]
                for prompt in active_prompts:
                    ContextAssembler(self._budget).assemble(system_prompt=prompt,
                        user_text="你好", recent_messages=[])
            self._settings_manager.save_character_card(card)
        except (CharacterCardError, SettingsError, ContextBudgetError) as exc:
            self._set_error(str(exc))
            return False
        self._character_card = card
        self.character_changed.emit()
        return True

    def set_reading_font_size(self, size):
        if not isinstance(size, int) or isinstance(size, bool) or not 12 <= size <= 22:
            return False
        saver = getattr(self._settings_manager, "save_chat_font_size", None)
        try:
            if callable(saver):
                saver(size)
        except SettingsError as exc:
            self._set_error(str(exc))
            return False
        if size != self._reading_font_size:
            self._reading_font_size = size
            self.reading_font_changed.emit(size)
        return True

    def message_retry_state(self, conversation_id, message_id):
        if (conversation_id, message_id) in self._unconfirmed_reply_commits:
            return {"available": False, "status": REPLY_COMMIT_UNCONFIRMED}
        request = self._requests.get((conversation_id, message_id))
        message = self._recoverable_messages.get((conversation_id, message_id), {})
        if (request or message).get("metadata", {}).get("agent_execution_started"):
            return {"available": False, "status": AGENT_REPLAY_STATUS}
        if request is not None:
            phase = request.get("phase", "")
            label = "上次记忆检索" if phase == "retrieving_memory" else "上次请求"
            return {"available": False, "status": label + "仍在结束；结束后可重试，也可继续发送新消息。"}
        if message.get("metadata", {}).get("agent_queue_origin") is not None:
            return {"available": False, "status": "请查看原排队任务的状态，或发送新的任务消息。"}
        available = ((conversation_id, message_id) in self._recoverable_messages
                     and not self._closed and not self._busy and not self._history_loading)
        return {"available": bool(available), "status": ""}

    @property
    def export_busy(self):
        return self._export_busy

    def export_conversation(self, conversation_id, destination, format="markdown"):
        """Export the explicit archived session without touching chat/API state."""
        if (self._closed or self._busy or self._history_loading or self._export_busy
                or conversation_id in self._invalidating_conversations
                or conversation_id in self._deleted_conversations
                or not isinstance(conversation_id, str) or not conversation_id.strip()):
            return False
        self._export_busy = True
        self.export_busy_changed.emit(True)
        # Capture both values now: switching conversations while this worker is
        # reading or writing must not redirect the export to another session.
        captured_id, captured_destination = conversation_id, str(destination)
        def finished(result, error):
            self._export_busy = False
            self.export_busy_changed.emit(False)
            self.export_finished.emit(captured_id, captured_destination, error)
        self._runner.submit(
            lambda: export_conversation_file(self.store.path, captured_id, captured_destination, format), finished)
        return True

    @property
    def connection_status(self):
        return self._connection_status

    @property
    def context_budget(self):
        return self._budget

    @property
    def context_usage(self):
        return self._context_usage

    def _set_error(self, message, action="", conversation_id="", message_id=""):
        self._error = message
        self._error_action = action if message else ""
        self._error_conversation_id = conversation_id if message else ""
        self._error_message_id = message_id if message else ""
        self.error_changed.emit(message)

    def _set_status(self, message):
        self._connection_status = message
        self.connection_status_changed.emit(message)

    def _set_busy(self, value):
        if self._busy != value:
            self._busy = value
            if not value:
                self._request_kind = ""
                self._request_phase = ""
                self._request_phase_rank = -1
            self._refresh_request_status()
            self.busy_changed.emit(value)
            self.retry_availability_changed.emit()

    def _refresh_work_active(self):
        # Direct Agent turns are tagged on admission. Routed turns acquire
        # their tag only after the model has actually selected an Agent.
        active = not self._closed and any(
            request.get("agent_kind") and not request["cancel"].is_set()
            for request in self._requests.values()
        )
        if active != self._work_active:
            self._work_active = active
            self.work_active_changed.emit(active)

    def _refresh_request_status(self):
        if self._closed or not self._busy:
            status = ""
        elif self._request_kind == "connection":
            status = "正在测试聊天服务连接…"
        elif self._active_request_conversation != self._current_id:
            status = "另一会话的请求正在处理中；当前会话尚未发送，请等待完成。"
        else:
            status = REQUEST_PHASES.get(self._request_phase, (-1, "正在处理请求…"))[1]
        if status != self._request_status:
            self._request_status = status
            self.request_status_changed.emit(status)

    @Slot(int, str, str)
    def _accept_request_phase(self, token, conversation_id, phase):
        # Only this GUI-thread receiver can publish progress. No provider text,
        # title, path, key or message content is accepted as a progress label.
        if (self._closed or not self._busy or self._request_kind != "chat"
                or token != self._request_id or conversation_id != self._active_request_conversation
                or phase not in REQUEST_PHASES):
            return
        rank, _ = REQUEST_PHASES[phase]
        if rank < self._request_phase_rank:
            return
        self._request_phase, self._request_phase_rank = phase, rank
        if self._active_request is not None:
            self._active_request["phase"] = phase
        self._refresh_work_active()
        self._refresh_request_status()

    def _publish_request_phase(self, token, conversation_id, phase):
        if phase in REQUEST_PHASES:
            request = next((item for item in tuple(self._requests.values())
                            if item.get("token") == token and item["conversation_id"] == conversation_id), None)
            if request is not None and request["trace"].event("phase:" + phase, "progress", REQUEST_PHASES[phase][1]):
                self._save_execution(request)
        try:
            self._request_phase_changed.emit(token, conversation_id, phase)
        except RuntimeError:
            pass  # Qt may already be gone while an accepted disk write finishes.

    def _save_execution(self, request, *, persist=True):
        refresh = request.get("refresh_trace_keys")
        if callable(refresh):
            refresh()
        snapshot = request["trace"].snapshot()
        snapshot = merge_execution(request["metadata"].get("execution"), snapshot)
        request["metadata"]["execution"] = snapshot
        if persist and request.get("persisted"):
            with request["write_lock"]:
                target = (request["conversation_id"], request["message_id"])
                if self._request_owners.get(target) is request:
                    self.store.update_execution(*target, snapshot)
        try:
            self._execution_changed.emit(request["conversation_id"], request["message_id"], snapshot)
        except RuntimeError:
            pass
        return snapshot

    def _retire_inactive_request_owners(self, conversation_id):
        """Release private requests after their main and cancellation writes end."""
        if self._pending_cancel_count.get(conversation_id, 0):
            return
        # Main requests remain registered until their real completion callback.
        for target, owner in tuple(self._request_owners.items()):
            if (target[0] == conversation_id and target not in self._requests
                    and target not in self._unconfirmed_reply_commits
                    and target not in self._historical_reply_candidates
                    and self._request_owners.get(target) is owner):
                self._request_owners.pop(target, None)

    def _notify_messages_changed(self, update=None):
        # An explicit view event survives reentrant legacy listeners. Every
        # other message mutation publishes its own complete-state barrier.
        self.messages_view_changed.emit(update)
        self.messages_changed.emit()

    @Slot(str, str, object)
    def _accept_execution(self, conversation_id, message_id, execution):
        if self._closed or conversation_id != self._current_id:
            return
        for message in self._messages:
            if message.get("id") == message_id:
                metadata = message.setdefault("metadata", {})
                unconfirmed = self._unconfirmed_reply_commits.get((conversation_id, message_id))
                metadata["execution"] = (deepcopy(unconfirmed) if unconfirmed is not None else
                    merge_execution(metadata.get("execution"), execution))
                cached = self._recoverable_messages.get((conversation_id, message_id))
                if cached is not None:
                    cached.setdefault("metadata", {})["execution"] = deepcopy(metadata["execution"])
                if self._retry_messages.get(conversation_id, {}).get("id") == message_id:
                    self._retry_messages[conversation_id].setdefault("metadata", {})["execution"] = deepcopy(metadata["execution"])
                self._notify_messages_changed(ExecutionViewUpdate(conversation_id, message_id,
                    json.dumps(metadata["execution"], ensure_ascii=False)))
                return

    def _set_history_loading(self, value):
        if self._history_loading != value:
            self._history_loading = value
            self.history_busy_changed.emit(value)
            self.retry_availability_changed.emit()

    def _set_usage(self, value):
        self._context_usage = value
        self.context_usage_changed.emit(value)

    def _configuration(self):
        try:
            settings = self._settings_manager.load_api_settings()
            key = self._settings_manager.get_api_key()
            if self._validate_configuration(settings, key):
                if getattr(self,"_agent_queue",None) is not None: self.refresh_agent_queue_policy()
                return settings, key
        except (SettingsError, CredentialsError) as exc:
            self._set_error(str(exc), "settings")
        return None

    def _validate_configuration(self, settings, key):
        try:
            settings.validate(require_model=True)
            if not isinstance(key, str) or not key.strip():
                self._set_error("尚未配置 API Key，请在 API 设置中保存密钥。", "settings")
                return False
        except (SettingsError, CredentialsError) as exc:
            self._set_error(self._redact_known_secrets(str(exc), key or ""), "settings")
            return False
        return True

    def _refresh_conversations(self):
        if self._closed:
            return
        self._list_id += 1
        token, query = self._list_id, self._query
        def finished(result, error):
            if token != self._list_id:
                return
            if error:
                self._set_error(error, "history")
                return
            self._conversations = result
            self._listed_query = query
            self.conversations_changed.emit()
        self._runner.submit(lambda: self.store.list_conversations(query), finished)

    def _install_snapshot(self, conversation_id, conversation, messages):
        if conversation_id != self._current_id:
            return
        installed = deepcopy(messages)
        visible = {item.get("id"): item for item in self._messages}
        for item in installed:
            previous = visible.get(item.get("id"), {})
            previous_metadata = previous.get("metadata", {})
            if isinstance(previous_metadata.get("execution"), dict):
                incoming = item.get("metadata", {}).get("execution")
                item.setdefault("metadata", {})["execution"] = (merge_execution(previous_metadata["execution"], incoming)
                    if isinstance(incoming, dict) else deepcopy(previous_metadata["execution"]))
            if previous_metadata.get("agent_execution_started"):
                item.setdefault("metadata", {})["agent_execution_started"] = True
            if (item.get("metadata", {}).get("request_status") == "pending"
                    and previous_metadata.get("request_status") in {"complete", *RECOVERABLE_STATUSES}
                    and (conversation_id, item["id"]) not in self._requests):
                item["metadata"]["request_status"] = previous_metadata["request_status"]
                if previous_metadata.get("agent_error_code") in AGENT_ERRORS:
                    item["metadata"]["agent_error_code"] = previous_metadata["agent_error_code"]
        known_ids = {item.get("id") for item in installed}
        # A screen-save worker can have read its snapshot just before send()
        # displays a new user message. Keep that optimistic turn while its
        # request is pending or failed; a subsequent complete snapshot replaces
        # it by ID. Otherwise a late screen callback could erase the visible
        # failed message even though it has already been committed to SQLite.
        for item in self._messages:
            if (item.get("conversation_id") == conversation_id and item.get("id") not in known_ids):
                installed.append(deepcopy(item))
                known_ids.add(item.get("id"))
        # A snapshot read before a newly committed turn must not reorder it
        # after a newer optimistic question when their callbacks race.
        installed.sort(key=lambda item: timestamp_key(item.get("created_at")))
        self._recover_messages(conversation_id, installed)
        self._messages = installed
        if conversation:
            self._current_title = conversation["title"]
            self._current_summary = conversation["summary"]
        if not self._history_loading:
            self._history_restore_view = None
        self.conversation_changed.emit()
        self._notify_messages_changed()

    def _recover_messages(self, conversation_id, messages):
        for item in messages:
            if item.get("role") != "user":
                continue
            metadata = item.setdefault("metadata", {})
            status = metadata.get("request_status")
            unconfirmed = self._unconfirmed_reply_commits.get((conversation_id, item["id"]))
            if unconfirmed is not None:
                metadata["execution"] = deepcopy(unconfirmed)
                metadata["request_status"] = status = "interrupted"
            request = self._requests.get((conversation_id, item["id"]))
            if request is not None and unconfirmed is None:
                if request["metadata"].get("agent_execution_started"):
                    metadata["agent_execution_started"] = True
                metadata["request_status"] = status = "cancelled" if request["cancel"].is_set() else "pending"
            if status == "pending" and (conversation_id, item["id"]) not in self._requests:
                metadata["request_status"] = status = "interrupted"
            if status in RECOVERABLE_STATUSES:
                self._recoverable_messages[(conversation_id, item["id"])] = deepcopy(item)
                self._retry_messages[conversation_id] = deepcopy(item)
            else:
                self._recoverable_messages.pop((conversation_id, item["id"]), None)
                if self._retry_messages.get(conversation_id, {}).get("id") == item["id"]:
                    self._retry_messages.pop(conversation_id, None)

    def reload_history(self, conversation_id=""):
        if self._closed or self._history_loading:
            return False
        if conversation_id:
            return self.open_conversation(conversation_id)
        self._refresh_conversations()
        return True

    def external_import_state(self, conversation_id):
        """Qt-thread admission only; SQLite tombstones remain authoritative."""
        if conversation_id in self._deleted_conversations or conversation_id in self._invalidating_conversations:
            return "conversation_deleted"
        if (self._closed or any(key[0] == conversation_id for key in self._requests)
                or self._pending_screen_count.get(conversation_id) or self._pending_cancel_count.get(conversation_id)):
            return "busy"
        return "ready"

    def context_export_policy(self):
        """Capture credential-derived redaction inputs on the Qt thread."""
        getter = getattr(self._settings_manager, "get_known_api_keys", None)
        return {"secrets": tuple(getter()) if callable(getter) else (),
                "model_context_tokens": self._budget.model_context_tokens}

    def context_envelope_for(self, conversation_id, *, generation=None, query="", policy=None):
        """Export references to one archive for an authenticated paired client.

        No API credentials, screen pixels, tool roles or execution grants are
        part of this envelope. The final archive read fences clear/delete races.
        """
        from .conversations.context_protocol import build_context_envelope
        policy = policy if policy is not None else self.context_export_policy()
        if (self._closed or conversation_id in self._deleted_conversations
                or conversation_id in self._invalidating_conversations):
            raise StoreError("会话已清空或删除。")
        conversation = self.store.get_conversation(conversation_id)
        if conversation is None:
            raise StoreError("会话不存在。")
        messages = self.store.load_messages(conversation_id)
        actual_generation = messages[0]["id"] if messages else None
        if generation is not None and generation != actual_generation:
            raise StoreError("会话版本已变化，请重新同步。")
        memories, memory_status = [], "disabled"
        memory = self._memory
        if memory is not None:
            try:
                memory_settings = memory.load_settings()
                enabled = getattr(memory_settings, "enabled", False)
                if enabled:
                    lookup = query or next((row["content"] for row in reversed(messages)
                                           if row["role"] == "user" and row.get("metadata",{}).get("request_status")
                                           in {None,"complete"}), "")
                    memories = memory.search(lookup[:4000], limit=6) if lookup else []
                    threshold = getattr(memory_settings, "relevance_threshold", 0.6)
                    memories = [row for row in memories if isinstance(row, dict)
                                and (row.get("score") is None or type(row.get("score")) in {float, int}
                                     and math.isfinite(row["score"]) and row["score"] >= threshold)]
                    memory_status = "available"
            except Exception:
                memory_status = "unavailable"
        envelope = build_context_envelope(conversation_id, messages,
            summary=context_summary(conversation, messages), memories=memories,
            model_context_tokens=policy["model_context_tokens"],
            redact=lambda text: redact_text(text, policy["secrets"], "[已隐藏密钥]"))
        if (self.store.read_conversation_generation(conversation_id) != actual_generation
                or self.store.get_conversation(conversation_id) is None
                or conversation_id in self._deleted_conversations
                or conversation_id in self._invalidating_conversations):
            raise StoreError("会话版本已变化，请重新同步。")
        envelope["memory_status"] = memory_status
        return envelope

    def refresh_external_import(self, conversation_id):
        """Refresh archive views after phone import, without selecting a chat or AI."""
        if self._closed or conversation_id in self._deleted_conversations:
            return
        self._refresh_conversations()
        if conversation_id != self._current_id:
            return
        self._load_id += 1
        token = self._load_id
        def finished(result, error):
            if self._closed or token != self._load_id or conversation_id != self._current_id:
                return
            if error or conversation_id in self._deleted_conversations:
                return
            self._set_history_loading(False)
            self._install_snapshot(conversation_id, *result)
        self._runner.submit(lambda: (self.store.get_conversation(conversation_id),
                                    self.store.load_messages(conversation_id)), finished)

    def _visible_view(self):
        return (self._current_id, self._current_title, self._current_summary,
                deepcopy(self._messages), self._context_usage)

    def new_conversation(self):
        if self._closed:
            return None
        self._load_id += 1
        self._set_history_loading(False)
        self._current_id = str(uuid4())
        self._current_title = "新对话"
        self._current_summary = ""
        self._messages = []
        self._history_restore_view = None
        self._set_error("")
        self._set_usage("新会话独立开始；没有载入其他会话的原始消息或概况。")
        self.conversation_changed.emit()
        self._notify_messages_changed()
        return self._current_id

    def restore_local_draft_conversation(self, candidate):
        """Adopt one host-validated unsent draft CID into a blank local view.

        No archive read, history load or message/network write occurs on this
        GUI-thread path. The owner checked archive identity in its I/O worker;
        its opaque candidate must still activate, and new input wins.
        """
        owner = getattr(self, "_local_draft_owner", None)
        cid = getattr(candidate, "conversation_id", None)
        canonical = _agent_uuid(cid)
        current = self._current_id
        if (type(owner) is not LocalDraftOwner or not canonical or self._closed or self._busy
                or self._history_loading or self._messages or self._active_request_conversation
                or getattr(candidate, "blank_conversation_id", None) != current
                or self._requests or current in self._invalidating_conversations
                or cid in self._invalidating_conversations or cid in self._deleted_conversations
                or any(row.get("id") in {current, cid} for row in self._conversations)):
            return False
        draft = owner.snapshot(current)
        if draft is None or draft.text or not owner.activate_candidate(candidate):
            return False
        self._load_id += 1
        self._current_id, self._current_title, self._current_summary = cid, "新对话", ""
        self._history_restore_view = None
        self.conversation_changed.emit()
        self._notify_messages_changed()
        return True

    def _begin_local_draft_invalidation(self, cid, kind):
        owner = getattr(self, "_local_draft_owner", None)
        if type(owner) is not LocalDraftOwner: return None, None
        return owner, owner.begin_invalidation(cid, kind=kind)

    @staticmethod
    def _wait_local_draft_invalidation(owner, ticket):
        if owner is not None and not owner.wait_invalidation(ticket, timeout=1):
            raise StoreError("本地草稿暂时无法可靠保存，聊天记录未改动，请稍后重试。")

    def open_conversation(self, conversation_id, message_id=None, *, focus_message_id=None):
        if self._closed or not isinstance(conversation_id, str) or not conversation_id:
            return False
        # Only a new explicit history read after an unconfirmed delete may
        # release that delete's conservative admission guard. Old snapshots,
        # background refreshes and ordinary _install_snapshot calls cannot.
        delete_read = self._unconfirmed_delete_reads.get(conversation_id)
        reply_reads = {target: marker for target, marker in self._unconfirmed_reply_commits.items()
                       if target[0] == conversation_id}
        completion_reads = {target: candidate for target, candidate in self._historical_reply_candidates.items()
                            if target[0] == conversation_id}
        message_id = focus_message_id or message_id
        if not message_id and self._query and self._query == self._listed_query:
            # A body search should land on its actual hit, including one far
            # above the latest messages. Explicit linked-message navigation
            # always takes precedence over the current history search.
            message_id = next((item.get("matched_message_id") for item in self._conversations
                               if item["id"] == conversation_id), None)
        # Rapidly selecting B then C must retain the last real view A as the
        # fallback. The temporary B loading placeholder is not a usable view.
        if not self._history_loading or self._history_restore_view is None:
            self._history_restore_view = self._visible_view()
        self._load_id += 1
        token = self._load_id
        self._current_id = conversation_id
        self._current_title = "正在加载会话…"
        self._current_summary = ""
        self._messages = []
        self._set_history_loading(True)
        self._set_error("")
        self.conversation_changed.emit()
        self._notify_messages_changed()
        def load():
            conversation = self.store.get_conversation(conversation_id)
            if conversation is None:
                raise StoreError("指定历史会话不存在，可能已被删除。")
            messages = self.store.load_messages(conversation_id)
            for item in messages:
                if (item.get("role") == "user" and item.get("metadata", {}).get("request_status") in
                        {"pending", *RECOVERABLE_STATUSES}
                        and (conversation_id, item["id"]) not in self._requests):
                    recovered = self.store.recover_pending_message(conversation_id, item["id"])
                    item["metadata"] = recovered["metadata"]
            return conversation, messages
        def finished(result, error):
            if token != self._load_id or self._current_id != conversation_id:
                return
            self._set_history_loading(False)
            if error:
                (self._current_id, self._current_title, self._current_summary,
                 self._messages, usage) = self._history_restore_view
                self._history_restore_view = None
                self._set_usage(usage)
                self._set_error("历史会话加载失败：" + error, "history", conversation_id)
                self.conversation_changed.emit()
                self._notify_messages_changed()
                return
            if (delete_read is not None
                    and self._unconfirmed_delete_reads.get(conversation_id) is delete_read
                    and conversation_id not in self._invalidating_conversations):
                self._unconfirmed_delete_reads.pop(conversation_id, None)
                self._deleted_conversations.discard(conversation_id)
            if (conversation_id not in self._invalidating_conversations
                    and conversation_id not in self._deleted_conversations):
                for target, marker in reply_reads.items():
                    if self._unconfirmed_reply_commits.get(target) is marker:
                        self._unconfirmed_reply_commits.pop(target, None)
            self._retire_inactive_request_owners(conversation_id)
            self._install_snapshot(conversation_id, *result)
            for target, candidate in completion_reads.items():
                self._reconcile_historical_reply(target, candidate, result[1], token)
            if message_id:
                self.message_focus_requested.emit(message_id)
        self._runner.submit(load, finished)
        return True

    def _reconcile_historical_reply(self, target, candidate, messages, load_id):
        def accepted_read():
            return (not self._closed and self._load_id == load_id and self._current_id == target[0]
                    and self._historical_reply_candidates.get(target) is candidate
                    and self._request_owners.get(target) is candidate.request
                    and target[0] not in self._invalidating_conversations
                    and target[0] not in self._deleted_conversations)
        def reconcile():
            return self._agent_queue.reconcile_unknown_completion(candidate, messages, accepted_read=accepted_read)
        def finished(reply_id, error):
            if error or not reply_id or not accepted_read():
                return
            candidate.request["committed"] = True
            candidate.request["saved_reply_id"] = reply_id
            self._historical_reply_candidates.pop(target, None)
            self._retire_inactive_request_owners(target[0])
        self._runner.submit(reconcile, finished)

    def search_conversations(self, query=""):
        if self._closed:
            return
        self._query = str(query).strip()[:1000]
        self._refresh_conversations()

    def rename_conversation(self, conversation_id, title):
        if self._closed:
            return False
        def rename():
            self.store.rename_conversation(conversation_id, title)
            return self.store.get_conversation(conversation_id)
        def finished(result, error):
            if error:
                self._set_error(error)
            else:
                if conversation_id == self._current_id:
                    self._current_title = result["title"]
                    self.conversation_changed.emit()
                self._refresh_conversations()
        self._runner.submit(rename, finished)
        return True

    def delete_conversation(self, conversation_id):
        with self.agent_authority_guard(): return self._delete_conversation(conversation_id)

    def _readback_unconfirmed_archive_mutation(self, conversation_id, error):
        """Worker-only readback: a failed ACK does not prove SQLite rollback."""
        public_error = (str(error) if type(error).__module__.startswith("desktop_pet.") else
                        "聊天记录操作结果暂时无法确认；未自动重试，请刷新历史查看实际记录。")
        result = {"mutation_unconfirmed": True, "readback_known": False,
                  "conversation": None, "messages": [], "error": public_error or "聊天记录操作结果暂时无法确认。"}
        try:
            conversation = self.store.get_conversation(conversation_id)
            messages = self.store.load_messages(conversation_id)
        except BaseException:
            # Never substitute a saved GUI view for an unreadable archive.
            return result
        result.update(readback_known=True, conversation=conversation,
                      messages=messages if conversation is not None else [])
        return result

    def _install_unconfirmed_archive_readback(self, conversation_id, result, error="", *, deleting=False):
        """GUI-only installation of actual worker readback, never old rows."""
        known = bool(result and result.get("readback_known"))
        conversation = result.get("conversation") if known else None
        messages = deepcopy(result.get("messages", [])) if known else []
        title = (conversation["title"] if conversation is not None else
                 "会话已删除" if known and deleting else "会话记录暂不可用")
        summary = conversation.get("summary", "") if conversation is not None else ""
        # These cached rows cannot authorize a retry after an uncertain delete.
        self._retry_messages.pop(conversation_id, None)
        self._recoverable_messages = {key: value for key, value in self._recoverable_messages.items()
                                      if key[0] != conversation_id}
        if deleting:
            if not known:
                self._unconfirmed_delete_reads[conversation_id] = object()
            else:
                self._unconfirmed_delete_reads.pop(conversation_id, None)
                if conversation is not None:
                    self._deleted_conversations.discard(conversation_id)
        if self._history_restore_view and self._history_restore_view[0] == conversation_id:
            view = self._history_restore_view
            self._history_restore_view = (conversation_id, title, summary, deepcopy(messages), view[4])
        if self._current_id == conversation_id:
            # A same-CID history read could have begun before the real commit.
            # This newer readback supersedes that pending snapshot; a load for
            # another CID is left untouched and keeps its reconciled fallback.
            self._load_id += 1
            self._history_restore_view = None
            self._current_title, self._current_summary = title, summary
            self._messages = messages  # _install_snapshot deliberately merges old rows: unsuitable here.
            self._recover_messages(conversation_id, self._messages)
            self._set_history_loading(False)
            self.conversation_changed.emit()
            self._notify_messages_changed()
        message = error or (result or {}).get("error") or "聊天记录操作结果暂时无法确认，请刷新历史查看。"
        self._set_error(message, "history", conversation_id)
        self._refresh_conversations()

    def _delete_conversation(self, conversation_id):
        if self._closed:
            return False
        if (any(key[0] == conversation_id for key in self._requests)
                or conversation_id == self._active_request_conversation or self._pending_screen_count.get(conversation_id)
                or self._pending_cancel_count.get(conversation_id)):
            self._set_error("请等待此会话的消息保存和请求结束后再删除。")
            return False
        if conversation_id in self._invalidating_conversations:
            return False
        try: draft_owner, draft_ticket = self._begin_local_draft_invalidation(conversation_id, "delete")
        except Exception:
            self._set_error("本地草稿暂时无法可靠保存，聊天记录未改动，请稍后重试。")
            return False
        self._invalidating_conversations.add(conversation_id)
        self._deleted_conversations.add(conversation_id)
        self._unconfirmed_delete_reads.pop(conversation_id, None)
        self.conversation_invalidated.emit(conversation_id)
        def finished(result, error):
            self._invalidating_conversations.discard(conversation_id)
            if error or result and result.get("mutation_unconfirmed"):
                self._install_unconfirmed_archive_readback(conversation_id, result, error, deleting=True)
                return
            self._retry_messages.pop(conversation_id, None)
            self._unconfirmed_reply_commits = {target: marker
                for target, marker in self._unconfirmed_reply_commits.items() if target[0] != conversation_id}
            self._historical_reply_candidates = {target: candidate
                for target, candidate in self._historical_reply_candidates.items() if target[0] != conversation_id}
            self._recoverable_messages = {key: value for key, value in self._recoverable_messages.items()
                                          if key[0] != conversation_id}
            self._request_owners = {key: value for key, value in self._request_owners.items() if key[0] != conversation_id}
            self._message_locks = {key: value for key, value in self._message_locks.items() if key[0] != conversation_id}
            if self._history_restore_view and self._history_restore_view[0] == conversation_id:
                view = self._history_restore_view
                self._history_restore_view = (conversation_id, "会话已删除", "", [], view[4])
            if self._current_id == conversation_id:
                self.new_conversation()
            self._refresh_conversations()
        def remove():
            try:
                self._wait_local_draft_invalidation(draft_owner, draft_ticket)
                self.store.delete_conversation(conversation_id)
            except BaseException as exc:
                if draft_owner is not None: draft_owner.finish_invalidation(draft_ticket, success=False)
                return self._readback_unconfirmed_archive_mutation(conversation_id, exc)
            else:
                if draft_owner is not None: draft_owner.finish_invalidation(draft_ticket, success=True)
        self._runner.submit(remove, finished)
        return True

    def clear(self):
        with self.agent_authority_guard(): return self._clear_conversation()

    def _clear_conversation(self):
        if self._closed:
            return
        conversation_id = self._current_id
        if (self._busy or any(key[0] == conversation_id for key in self._requests)
                or self._pending_screen_count.get(conversation_id) or self._pending_cancel_count.get(conversation_id)):
            self._set_error("请等待当前请求和消息保存结束，再清空聊天记录。")
            return
        if conversation_id in self._invalidating_conversations:
            return
        try: draft_owner, draft_ticket = self._begin_local_draft_invalidation(conversation_id, "clear")
        except Exception:
            self._set_error("本地草稿暂时无法可靠保存，聊天记录未改动，请稍后重试。")
            return
        self._invalidating_conversations.add(conversation_id)
        self.conversation_invalidated.emit(conversation_id)
        if not self._history_loading:
            self._history_restore_view = self._visible_view()
        self._load_id += 1
        self._set_history_loading(True)
        # Clear is optimistic; failed ACKs need a real archive readback, since
        # SQLite may already have committed the deletion.
        self._messages = []
        self._notify_messages_changed()
        def clear():
            try:
                self._wait_local_draft_invalidation(draft_owner, draft_ticket)
                self.store.clear_messages(conversation_id)
            except BaseException as exc:
                if draft_owner is not None: draft_owner.finish_invalidation(draft_ticket, success=False)
                return self._readback_unconfirmed_archive_mutation(conversation_id, exc)
            else:
                if draft_owner is not None: draft_owner.finish_invalidation(draft_ticket, success=True)
            # The mutation is already committed. A later read-only title
            # failure cannot restore messages that were actually deleted.
            try:
                conversation = self.store.get_conversation(conversation_id)
            except Exception:
                return {"conversation": None, "metadata_unavailable": True}
            return {"conversation": conversation, "metadata_unavailable": False}
        def finished(result, error):
            self._invalidating_conversations.discard(conversation_id)
            if error or result and result.get("mutation_unconfirmed"):
                self._install_unconfirmed_archive_readback(conversation_id, result, error)
                return
            metadata_unavailable = bool(not error and result and result.get("metadata_unavailable"))
            conversation = result.get("conversation") if not error and result else None
            if self._history_restore_view and self._history_restore_view[0] == conversation_id:
                # If a different history load fails after this clear succeeds,
                # restoring A must show the cleared view rather than old text.
                if not error:
                    view = self._history_restore_view
                    self._history_restore_view = (conversation_id, conversation["title"] if conversation else "新对话",
                                                  "", [], view[4])
            self._retry_messages.pop(conversation_id, None)
            self._unconfirmed_reply_commits = {target: marker
                for target, marker in self._unconfirmed_reply_commits.items() if target[0] != conversation_id}
            self._historical_reply_candidates = {target: candidate
                for target, candidate in self._historical_reply_candidates.items() if target[0] != conversation_id}
            self._recoverable_messages = {key: value for key, value in self._recoverable_messages.items()
                                          if key[0] != conversation_id}
            self._request_owners = {key: value for key, value in self._request_owners.items() if key[0] != conversation_id}
            self._message_locks = {key: value for key, value in self._message_locks.items() if key[0] != conversation_id}
            if self._current_id == conversation_id:
                # A successful clear also supersedes any same-CID history
                # snapshot read before its commit. Other CIDs retain their
                # own load token and the empty fallback installed above.
                self._load_id += 1
                self._current_title = conversation["title"] if conversation else "新对话"
                self._current_summary = ""
                self._messages = []
                self._history_restore_view = None
                self._set_history_loading(False)
                self.conversation_changed.emit()
                self._notify_messages_changed()
                if metadata_unavailable:
                    self._set_error("聊天记录已清空，但会话信息暂时无法读取，请刷新历史后重试。", "history", conversation_id)
                else:
                    self._set_error("")
            self._refresh_conversations()
        self._runner.submit(clear, finished)

    def update_context_budget(self, total_tokens, output_reserve):
        try:
            budget = replace(self._budget, total_tokens=total_tokens, output_reserve=output_reserve)
            budget.validate()
            saver = getattr(self._settings_manager, "save_context_budget", None)
            if callable(saver):
                saver(budget)
            self._budget = budget
            self._set_usage(f"预算已设为 {total_tokens}，输出预留 {output_reserve}；下次请求生效。")
            return True
        except (ContextBudgetError, SettingsError) as exc:
            self._set_error(str(exc))
            return False

    def set_image_inbox(self, inbox):
        """Inject the host's bounded inbox; image I/O stays in chat workers."""
        self._image_inbox = inbox

    def import_local_image(self, path):
        """Worker-only import from the native file picker, into the shared inbox."""
        from pathlib import Path
        import base64
        from .chat_images import MAX_CHAT_IMAGE_BYTES
        if self._closed or self._image_inbox is None:
            raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
        try:
            selected = Path(path)
            with selected.open("rb") as handle:
                data = handle.read(MAX_CHAT_IMAGE_BYTES + 1)
            if not data or len(data) > MAX_CHAT_IMAGE_BYTES:
                raise ApiError("image_unavailable", "图片不能超过 4 MiB，请选择较小的 PNG、JPEG 或 WebP 图片。")
            # Local directory names never enter the archive or WebChannel.
            name = selected.name[:160]
            if not name or any(ord(character) < 32 for character in name):
                name = "image"
            metadata = self._image_inbox.upload({"filename": name,
                "data_base64": base64.b64encode(data).decode("ascii")})
            self.mark_image_storage_mutated(metadata["id"])
            return metadata, self._image_inbox.get(metadata["id"])
        except ApiError:
            raise
        except Exception as exc:
            raise _image_import_failure(exc) from None

    def import_image_data(self, filename, mime_type, data_base64):
        """Worker-only import of supplied bytes; never resolve a web path or URL."""
        from .chat_images import MAX_CHAT_IMAGE_BYTES
        if self._closed or self._image_inbox is None:
            raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
        try:
            if (not isinstance(filename, str) or not 1 <= len(filename) <= 160
                    or any(ord(character) < 32 or ord(character) == 127 for character in filename)
                    or mime_type not in ("image/png", "image/jpeg", "image/webp")
                    or not isinstance(data_base64, str) or not data_base64
                    or len(data_base64) > ((MAX_CHAT_IMAGE_BYTES + 2) // 3) * 4):
                raise ValueError("INVALID_IMAGE_DATA")
            metadata = self._image_inbox.upload({"filename": filename, "mime_type": mime_type,
                                                  "data_base64": data_base64})
            self.mark_image_storage_mutated(metadata["id"])
            return metadata, self._image_inbox.get(metadata["id"])
        except Exception as exc:
            raise _image_import_failure(exc) from None

    def get_message_image(self, conversation_id, message_id, image_id):
        """Worker-only read for a current message's internally generated action."""
        if (self._closed or conversation_id != self._current_id
                or not isinstance(message_id, str) or not message_id or len(message_id) > 200
                or not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id)
                or self._image_inbox is None):
            raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
        try:
            with self.store._connection() as connection:
                row = connection.execute("""SELECT role,
                    CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.image_id') END AS image_id
                    FROM messages WHERE id = ? AND conversation_id = ?""",
                    (message_id, conversation_id)).fetchone()
            if (row is None or row["role"] != "user" or row["image_id"] != image_id
                    or conversation_id != self._current_id or self._closed):
                raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
            return self._image_inbox.get(image_id)
        except ApiError:
            raise
        except Exception as exc:
            code = "image_missing" if getattr(exc, "code", None) in {"not_found", "image_missing"} else "image_unavailable"
            raise ApiError(code, IMAGE_ERRORS[code]) from None

    @staticmethod
    def _pin_image_reference(metadata, previous_messages, created_at, text=""):
        pinned = deepcopy(metadata)
        pinned.pop("image_error_code", None)
        history_pair = (not pinned.get("image_id") and compares_previous_image(text)
                        and pinned.get("execution_mode") != "agent" and not pinned.get("desktop_context"))
        if history_pair and "context_image_pair" not in pinned and "context_image_id" not in pinned:
            pair, characters = [], 0
            for previous in reversed(previous_messages[-20:]):
                characters += len(str(previous.get("content", "")))
                if characters > 48_000:
                    break
                meta = previous.get("metadata", {})
                if (previous.get("role") == "user" and meta.get("image_id")
                        and meta.get("request_status") not in {"queued", "pending", *RECOVERABLE_STATUSES}
                        and timestamp_key(previous.get("created_at")) <= timestamp_key(created_at)):
                    pair.append({"image_id": meta["image_id"], "message_id": previous["id"]})
                    if len(pair) == 2:
                        break
            # Persist absence and the exact source message IDs as well. A retry
            # must not select a later imported/backdated photograph.
            pinned["context_image_pair"] = list(reversed(pair)) if len(pair) == 2 else []
            pinned["context_image_id"] = None
        if (pinned.get("execution_mode") != "agent" and ("image_id" not in pinned or compares_previous_image(text))
                and "context_image_id" not in pinned and not pinned.get("desktop_context")):
            # Use the complete same-session archive, even if its text has been
            # summarized. A retry must not inherit a later, unrelated image.
            recent, characters = [], 0
            if (compares_previous_image(text) if "image_id" in pinned else references_previous_image(text)):
                for previous in reversed(previous_messages[-20:]):
                    characters += len(str(previous.get("content", "")))
                    if characters > 48_000:
                        break
                    if previous.get("metadata", {}).get("request_status") not in {"queued", "pending", *RECOVERABLE_STATUSES}:
                        recent.append(previous)
            for previous in recent:
                if (previous.get("role") == "user"
                        and timestamp_key(previous.get("created_at")) <= timestamp_key(created_at)
                        and "image_id" in previous.get("metadata", {})):
                    pinned["context_image_id"] = previous["metadata"]["image_id"]
                    pinned["context_image_message_id"] = previous["id"]
                    break
            # Pin the absence too: retrying an older text turn must not select
            # a later imported attachment with a backdated timestamp.
            pinned.setdefault("context_image_id", None)
        return pinned

    def _load_chat_image(self, image_id):
        if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id) or self._image_inbox is None:
            raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
        try:
            image = self._image_inbox.get(image_id)
            return "data:" + image["mime_type"] + ";base64," + image["data_base64"]
        except Exception as exc:
            code = "image_missing" if getattr(exc, "code", None) in {"not_found", "image_missing"} else "image_unavailable"
            raise ApiError(code, IMAGE_ERRORS[code]) from None

    @staticmethod
    def _desktop_prompt_text(text, metadata):
        reason = metadata.get("desktop_capture_unavailable_reason")
        if (metadata.get("desktop_context") is True and metadata.get("desktop_image_id") is None
                and isinstance(reason, str) and reason in DESKTOP_CAPTURE_UNAVAILABLE_NOTICES):
            return text + DESKTOP_CAPTURE_UNAVAILABLE_NOTICES[reason]
        return text

    def send(self, text, *, message_id=None, conversation_id=None, selection_context=None, image_id=None,
             desktop_context=False, desktop_image_id=None, desktop_capture_unavailable_reason=None, agent_mode=False):
        if self._closed or self._busy or self._history_loading:
            return False
        admission = self._image_storage_admission()
        if admission is None: return False
        try:
            return self._image_storage_gate.run(admission, lambda: self._send_admitted(text,
                message_id=message_id, conversation_id=conversation_id, selection_context=selection_context,
                image_id=image_id, desktop_context=desktop_context, desktop_image_id=desktop_image_id,
                desktop_capture_unavailable_reason=desktop_capture_unavailable_reason, agent_mode=agent_mode))
        finally:
            self.notify_image_storage_changed()

    def _send_admitted(self, text, *, message_id=None, conversation_id=None, selection_context=None, image_id=None,
                       desktop_context=False, desktop_image_id=None, desktop_capture_unavailable_reason=None, agent_mode=False):
        if self._closed or self._busy or self._history_loading:
            return False
        target_id = self._current_id if conversation_id is None else conversation_id
        if not isinstance(target_id, str) or not target_id.strip() or len(target_id) > 200:
            return False
        if message_id is not None and (not isinstance(message_id, str) or not message_id.strip()
                                       or len(message_id) > 200):
            return False
        if (target_id, message_id) in self._unconfirmed_reply_commits:
            self._set_error(REPLY_COMMIT_UNCONFIRMED, "history", target_id, message_id)
            return False
        if (target_id in self._invalidating_conversations
                or target_id in self._deleted_conversations):
            self._set_error("当前会话正在清空或删除，请等待操作完成或新建对话。")
            return False
        if not isinstance(text, str) or not text.strip():
            return False
        text = text.strip()
        if len(text) > MAX_INPUT_CHARACTERS:
            self._set_error(f"单条消息最多 {MAX_INPUT_CHARACTERS} 个字符，请缩短后再发送。")
            return False
        if agent_mode is True:
            try:
                query = getattr(self._agent_service, "execution_resources_pending", None)
                pending = callable(query) and query() is not False
            except Exception:
                pending = True
            if pending:
                self._set_error(AGENT_ERRORS["busy"])
                return False
        configuration = self._configuration()
        if configuration is None:
            return False
        settings, key = configuration
        metadata = {"request_status": "pending"}
        if type(agent_mode) is not bool:
            self._set_error("Agent 模式参数无效。")
            return False
        if agent_mode:
            readiness = self.agent_readiness()
            if not readiness["available"]:
                self._set_error(readiness["reason"])
                return False
            metadata["execution_mode"] = "agent"
        if type(desktop_context) is not bool or desktop_context and image_id is not None:
            self._set_error("电脑画面与图片附件不能同时发送。")
            return False
        if desktop_capture_unavailable_reason is not None and (
                desktop_context is not True or desktop_image_id is not None
                or not isinstance(desktop_capture_unavailable_reason, str)
                or desktop_capture_unavailable_reason not in DESKTOP_CAPTURE_UNAVAILABLE_NOTICES):
            self._set_error("电脑画面状态无效，请重新获取后再试。")
            return False
        if desktop_context:
            if (desktop_capture_unavailable_reason is None
                    and (not isinstance(desktop_image_id, str) or not IMAGE_ID.fullmatch(desktop_image_id))):
                self._set_error("电脑画面不可用，请重新获取后再试。")
                return False
            metadata.update(desktop_context=True, desktop_image_id=desktop_image_id)
            if desktop_capture_unavailable_reason is not None:
                metadata["desktop_capture_unavailable_reason"] = desktop_capture_unavailable_reason
        if image_id is not None:
            if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
                self._set_error("图片标识无效，请重新发送图片后重试。")
                return False
            metadata["image_id"] = image_id
        if selection_context is not None:
            try:
                metadata["selection_context"] = SelectionContext.from_payload(
                    selection_context, api_key=key).to_dict()
                metadata["selection_context"] = self._redact_reference(metadata["selection_context"], key)
            except (SelectionContextError, TypeError):
                self._set_error("未能获取有效的选中资料，请重新选择文字后再试。")
                return False
        selected_character = self._character_card
        try:
            # Refuse an impossible question before displaying or persisting a
            # failed request; the current question is never silently shortened.
            base_prompt = character_system_prompt(selected_character)
            prompt = (direct_agent_system_prompt(selected_character) if agent_mode else agent_system_prompt(base_prompt)
                      if self._routing_for_turn(text, metadata) else base_prompt)
            ContextAssembler(self._budget).assemble(system_prompt=prompt,
                user_text=self._desktop_prompt_text(self._redact_known_secrets(text, key), metadata), recent_messages=[],
                selection_contexts=([{"conversation_id": target_id,
                    "context": metadata["selection_context"], "required": True}]
                    if "selection_context" in metadata else []))
        except (ContextBudgetError, CharacterCardError) as exc:
            self._set_error(str(exc))
            return False
        message = {"id": message_id or str(uuid4()), "conversation_id": target_id, "role": "user",
                   "content": self._redact_known_secrets(text, key), "source": "user", "metadata": metadata,
                   "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
        if target_id == self._current_id:
            self._messages.append(deepcopy(message))
            self._notify_messages_changed()
        self._begin_chat(message, settings, key, character_card=selected_character)
        return True

    def retry(self, conversation_id=None, message_id=None):
        if self._closed or self._busy or self._history_loading:
            return False
        admission = self._image_storage_admission()
        if admission is None: return False
        try:
            return self._image_storage_gate.run(admission, lambda: self._retry_admitted(conversation_id, message_id))
        finally:
            self.notify_image_storage_changed()

    def _retry_admitted(self, conversation_id=None, message_id=None):
        if self._closed or self._busy or self._history_loading:
            return False
        if (self._current_id in self._invalidating_conversations
                or self._current_id in self._deleted_conversations):
            return False
        conversation_id = conversation_id or self._current_id
        # Stale UI commands must never retry another session's latest turn.
        if conversation_id != self._current_id:
            return False
        message = (self._recoverable_messages.get((conversation_id, message_id)) if message_id
                   else self._retry_messages.get(conversation_id))
        if message is None:
            return False
        if (conversation_id, message["id"]) in self._unconfirmed_reply_commits:
            self._set_error(REPLY_COMMIT_UNCONFIRMED, "history", conversation_id, message["id"])
            return False
        if message.get("metadata",{}).get("agent_queue_origin") is not None:
            self._set_error("请查看排队任务的状态，或发送新的任务消息。", "", conversation_id,message["id"])
            return False
        if message.get("metadata", {}).get("agent_execution_started"):
            self._set_error(AGENT_REPLAY_STATUS, "", conversation_id, message["id"])
            return False
        if (conversation_id, message["id"]) in self._requests:
            self._set_error(self.message_retry_state(conversation_id, message["id"])["status"],
                            "retry", conversation_id, message["id"])
            return False  # The cancelled worker is still finalizing its write.
        configuration = self._configuration()
        if configuration is None:
            return False
        self._begin_chat(deepcopy(message), *configuration)
        return True

    def stop_request(self, conversation_id=None):
        """Stop one request. A provider may already have received its input.

        Cancellation wins until the final reply transaction starts. Reject a
        too-late stop while committing instead of falsely claiming it stopped.
        Adapters without cancel_request still discard their eventual response.
        """
        request = self._active_request
        if (not self.can_stop or request is None
                or conversation_id is not None and conversation_id != request["conversation_id"]):
            return False
        if not request["commit_lock"].acquire(blocking=False):
            return False
        try:
            if request["committed"]:
                return False
            request["cancel"].set()
            agent_stopping = bool(request.get("agent_kind") or request["metadata"].get("agent_execution_started"))
            stop_status = "interrupted" if agent_stopping else "cancelled"
            if agent_stopping: request["metadata"]["agent_error_code"] = "stop_unconfirmed"
            request["trace"].finish(stop_status)
            self._save_execution(request, persist=False)
        finally:
            request["commit_lock"].release()
        cancel = getattr(self._client, "cancel_request", None)
        if callable(cancel):
            cancel(request["cancel"])
        cancel_memory = getattr(self._memory, "cancel_request", None)
        if callable(cancel_memory):
            cancel_memory(request["cancel"])
        self._agent_service.cancel_request(request["cancel"])
        def persist_cancelled():
            # Do not wait for an adapter's network response to preserve Stop.
            # Serialize with the accepted initial user write, including a stop
            # pressed before that worker was scheduled by the thread pool.
            with request["write_lock"]:
                target = (request["conversation_id"], request["message_id"])
                if self._request_owners.get(target) is not request:
                    return
                saved_stop_status = "cancelled" if request.get("stop_confirmed") else stop_status
                messages = self.store.load_messages(request["conversation_id"])
                if any(item.get("role") == "assistant" and item.get("source") == "assistant"
                       and item.get("metadata", {}).get("reply_to") == request["message_id"] for item in messages):
                    self.store.reconcile_request(request["conversation_id"], request["message_id"])
                    return
                if not any(item["id"] == request["message_id"] for item in messages):
                    request["metadata"] = self._pin_image_reference(request["metadata"], messages, request["created_at"], request["content"])
                    self.store.append_message(request["conversation_id"], "user", request["content"], "user",
                        message_id=request["message_id"], metadata=dict(request["metadata"], request_status=saved_stop_status),
                        created_at=request["created_at"])
                else:
                    self.store.set_message_status(request["conversation_id"], request["message_id"], saved_stop_status,
                        execution=request["trace"].snapshot(),
                        **({"agent_error_code": request["metadata"].get("agent_error_code", "stop_unconfirmed")}
                           if agent_stopping else {}))
        cid = request["conversation_id"]
        self._pending_cancel_count[cid] = self._pending_cancel_count.get(cid, 0) + 1
        def persisted(_result, error):
            self._pending_cancel_count[cid] -= 1
            if (error and not self._closed and self._request_id == stopped_token and self._current_id == cid
                    and self._request_owners.get((cid, request["message_id"])) is request):
                self._set_error("停止状态未能保存：" + error, "history", cid)
            self._retire_inactive_request_owners(cid)
        self._submit_image_storage_task(persist_cancelled, persisted)
        self._interrupt_stream_preview(self._request_id, request["conversation_id"])
        self._request_id += 1
        stopped_token = self._request_id
        self._active_request = None
        self._active_request_conversation = None
        self._set_busy(False)
        self._refresh_work_active()
        self._mark_message_status(request["conversation_id"], request["message_id"], stop_status,
                                  metadata=request["metadata"])
        def accepts_feedback():
            return (not self._closed and self._request_id == stopped_token
                    and request["conversation_id"] == self._current_id)
        if accepts_feedback():
            if agent_stopping:
                self._set_error("正在等待任务结束…",
                                "", request["conversation_id"], request["message_id"])
            else:
                self._set_error("已停止等待回复；服务端可能已收到请求。" +
                                self.message_retry_state(request["conversation_id"], request["message_id"])["status"],
                                "retry", request["conversation_id"], request["message_id"])
        if accepts_feedback():
            self.emotion_requested.emit("neutral")
        self.retry_availability_changed.emit()
        return True

    def _mark_message_status(self, conversation_id, message_id, status, *, metadata=None):
        unconfirmed = self._unconfirmed_reply_commits.get((conversation_id, message_id))
        if unconfirmed is not None:
            status = "interrupted"
        if conversation_id == self._current_id:
            for item in self._messages:
                if item.get("id") == message_id:
                    if metadata is not None:
                        previous_execution = item.get("metadata", {}).get("execution")
                        item.setdefault("metadata", {}).update(deepcopy(metadata))
                        if isinstance(metadata.get("execution"), dict):
                            item["metadata"]["execution"] = merge_execution(previous_execution, metadata["execution"])
                    if unconfirmed is not None:
                        item.setdefault("metadata", {})["execution"] = deepcopy(unconfirmed)
                    item.setdefault("metadata", {})["request_status"] = status
                    if status in RECOVERABLE_STATUSES:
                        self._recoverable_messages[(conversation_id, message_id)] = deepcopy(item)
                        self._retry_messages[conversation_id] = deepcopy(item)
                    else:
                        self._recoverable_messages.pop((conversation_id, message_id), None)
                        if self._retry_messages.get(conversation_id, {}).get("id") == message_id:
                            self._retry_messages.pop(conversation_id, None)
            self._notify_messages_changed()

    def _read_committed_reply(self, request, reply, execution, generation):
        """Read one archive snapshot after an uncertain save acknowledgement."""
        cid, user_id = request["conversation_id"], request["message_id"]
        messages = self.store.load_messages(cid)
        if (self._request_owners.get((cid, user_id)) is not request
                or cid in self._invalidating_conversations or cid in self._deleted_conversations
                or not messages or messages[0]["id"] != generation):
            raise StoreError(REPLY_COMMIT_UNCONFIRMED)
        user = next((item for item in messages if item["id"] == user_id), None)
        candidates = [item for item in messages if item.get("metadata", {}).get("reply_to") == user_id]
        if (user is not None and not candidates
                and user.get("metadata", {}).get("request_status") != "complete"):
            return None
        if user is None or len(candidates) != 1:
            raise StoreError(REPLY_COMMIT_UNCONFIRMED)
        saved = candidates[0]
        user_metadata, saved_metadata = user.get("metadata", {}), saved.get("metadata", {})
        if (user.get("conversation_id") != cid or user.get("role") != "user" or user.get("source") != "user"
                or user.get("content") != request["content"] or user_metadata.get("request_status") != "complete"
                or user_metadata.get("agent_execution_started") != request["metadata"].get("agent_execution_started")
                or user_metadata.get("execution") != execution
                or saved.get("conversation_id") != cid or saved.get("role") != "assistant"
                or saved.get("source") != "assistant" or not isinstance(saved.get("id"), str)
                or not saved["id"] or saved.get("content") != reply.reply
                or saved_metadata.get("emotion") != reply.emotion or saved_metadata.get("execution") != execution):
            raise StoreError(REPLY_COMMIT_UNCONFIRMED)
        return saved

    def _begin_chat(self, user_message, settings, key, *, job_context=None, character_card=_CURRENT_CHARACTER):
        agent_settings = self._agent_settings
        direct_agent = user_message.get("metadata", {}).get("execution_mode") == "agent"
        allow_agent = self._routing_for_turn(user_message["content"], user_message.get("metadata", {}))
        self._request_id += 1
        token = self._request_id
        conversation_id = user_message["conversation_id"]
        request = {"conversation_id": conversation_id, "message_id": user_message["id"],
                   "content": user_message["content"], "cancel": threading.Event(),
                   "metadata": deepcopy(user_message.get("metadata", {})),
                   "created_at": user_message.get("created_at"),
                   "phase": "saving_message",
                   "commit_lock": threading.Lock(), "committed": False, "token": token,
                   "character_card": self._character_card if character_card is _CURRENT_CHARACTER else character_card}
        if direct_agent:
            request["agent_kind"] = "agent"
        if job_context is not None:
            self._agent_queue.attach_request(job_context,request)
        known_keys = [key]
        getter = getattr(self._settings_manager, "get_known_api_keys", None)
        if callable(getter):
            known_keys.extend(getter())
        known_keys = tuple(sorted({secret for secret in known_keys if isinstance(secret, str) and secret}, key=len, reverse=True))
        def refresh_trace_keys():
            nonlocal known_keys
            if callable(getter):
                current = getter()
                known_keys = tuple(sorted(set(known_keys).union(
                    secret for secret in current if isinstance(secret, str) and secret), key=len, reverse=True))
        def trace_redact(text):
            for secret in known_keys:
                text = redact_secret(text, secret)
            return text
        request["refresh_trace_keys"] = refresh_trace_keys
        request["trace"] = ExecutionTrace(settings.model, trace_redact)
        request["metadata"]["execution"] = request["trace"].snapshot()
        target = (conversation_id, user_message["id"])
        request["write_lock"] = self._message_locks.setdefault(target, threading.Lock())
        self._request_owners[target] = request
        self._requests[target] = request
        self._active_request = request
        self._active_request_conversation = conversation_id
        self._clear_stream_preview()
        self._request_kind = "chat"
        self._request_phase, self._request_phase_rank = "saving_message", 0
        self._set_error("")
        self._mark_message_status(conversation_id, user_message["id"], "pending", metadata=request["metadata"])
        self._set_busy(True)
        self._refresh_work_active()
        self.emotion_requested.emit("thinking")
        budget = self._budget
        def run_turn():
            # The accepted user write always completes, even during shutdown.
            try:
                with request["write_lock"]:
                    all_messages = self.store.load_messages(conversation_id)
                    initial_status = "cancelled" if self._stop.is_set() or request["cancel"].is_set() else "pending"
                    if not any(item["id"] == user_message["id"] for item in all_messages):
                        request["metadata"] = self._pin_image_reference(
                            request["metadata"], all_messages, user_message.get("created_at"), user_message["content"])
                        user_message["metadata"] = deepcopy(request["metadata"])
                        self.store.append_message(conversation_id, "user", user_message["content"], "user",
                            message_id=user_message["id"], metadata=dict(request["metadata"], request_status=initial_status),
                            created_at=user_message.get("created_at"))
                    else:
                        recovered, existing_reply = self.store.reconcile_request(conversation_id, user_message["id"])
                        if existing_reply is not None:
                            with request["commit_lock"]:
                                request["committed"] = True
                            return {"already_complete": True,
                                    "conversation": self.store.get_conversation(conversation_id),
                                    "messages": self.store.load_messages(conversation_id)}
                        # The archived reference owns retries, including after
                        # restart or when newer images have since been sent.
                        user_message.update(recovered)
                        user_message["metadata"].pop("image_error_code", None)
                        user_message["metadata"].pop("agent_error_code", None)
                        request["metadata"] = deepcopy(user_message["metadata"])
                        request["metadata"]["execution"] = request["trace"].snapshot()
                        self.store.set_message_status(conversation_id, user_message["id"], initial_status)
                    request["persisted"] = True
                    self.store.update_execution(conversation_id, user_message["id"], request["trace"].snapshot())
                if self._stop.is_set() or request["cancel"].is_set():
                    request["trace"].finish("cancelled")
                    self._save_execution(request, persist=False)
                    self.store.set_message_status(conversation_id, user_message["id"], "cancelled",
                                                  execution=request["metadata"]["execution"])
                    return {"cancelled": True}
                if user_message.get("metadata", {}).get("agent_execution_started"):
                    raise ApiError("agent_already_attempted", "这条消息已经启动过任务助手；请先检查结果，再发送新的任务消息，避免重复操作。")
                conversation = self.store.get_conversation(conversation_id)
                all_messages = self.store.load_messages(conversation_id)
                # Future queued user tasks are not authorization or context for
                # this turn. Keep replies already committed for earlier users,
                # even if a queued user's timestamp predates that earlier reply.
                current_index=next(index for index,item in enumerate(all_messages) if item["id"]==user_message["id"])
                prior_users={item["id"] for item in all_messages[:current_index] if item["role"]=="user"}
                all_messages=[item for index,item in enumerate(all_messages) if index<=current_index
                    or (item["role"]=="assistant" and item.get("source")=="assistant"
                        and item.get("metadata",{}).get("reply_to") in prior_users)]
                if job_context is not None:
                    conversation=dict(conversation,summary="",summary_through_id=None)
                image_metadata = user_message.get("metadata", {})
                pair_references = []
                if (not image_metadata.get("image_id") and compares_previous_image(user_message["content"])
                        and image_metadata.get("execution_mode") != "agent" and not image_metadata.get("desktop_context")):
                    pair = image_metadata.get("context_image_pair")
                    if not isinstance(pair, list) or len(pair) != 2:
                        raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
                    source_ids = []
                    for reference in pair:
                        if not isinstance(reference, dict) or set(reference) != {"image_id", "message_id"}:
                            raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
                        original = next((item for item in all_messages if item["id"] == reference["message_id"]
                            and item["role"] == "user" and item.get("metadata", {}).get("image_id") == reference["image_id"]
                            and item.get("metadata", {}).get("request_status") not in {"queued", "pending", *RECOVERABLE_STATUSES}), None)
                        if original is None or original["id"] in source_ids:
                            raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
                        source_ids.append(original["id"])
                        pair_references.append((original["id"], self._load_chat_image(reference["image_id"])))
                if (image_metadata.get("image_id") and image_metadata.get("execution_mode") != "agent"
                        and compares_previous_image(user_message["content"]) and not image_metadata.get("context_image_id")):
                    raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
                image_id = (image_metadata.get("desktop_image_id") if image_metadata.get("desktop_context") else
                            image_metadata.get("image_id", image_metadata.get("context_image_id")))
                image_url = self._load_chat_image(image_id) if image_id is not None else None
                comparison_source = image_metadata.get("context_image_message_id") if image_metadata.get("image_id") else None
                comparison_url = None
                if image_metadata.get("image_id") and image_metadata.get("context_image_id"):
                    original = next((item for item in all_messages if item["id"] == comparison_source
                        and item["role"] == "user" and item.get("metadata", {}).get("image_id") == image_metadata["context_image_id"]), None)
                    if original is None:
                        raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
                    comparison_url = self._load_chat_image(image_metadata["context_image_id"])
                recent = [item for item in all_messages if item["id"] != user_message["id"]
                          and item.get("metadata", {}).get("request_status") not in
                              {"queued","pending", *RECOVERABLE_STATUSES}]
                cursor = conversation.get("summary_through_id")
                if cursor and getattr(budget, "mode", "manual") == "manual":
                    for index, item in enumerate(recent):
                        if item["id"] == cursor:
                            recent = recent[index + 1:]
                            break
                events = self.store.recent_screen_events(conversation_id, query=user_message["content"], limit=3)
                # Retrying a turn reuses its original snapshot. A short follow-up
                # may refer to the latest selection in this same session only.
                selection = user_message.get("metadata", {}).get("selection_context")
                selection_required = selection is not None
                if selection is None:
                    for item in reversed(recent):
                        selection = item.get("metadata", {}).get("selection_context")
                        if selection is not None:
                            break
                selections = ([{"conversation_id": conversation_id, "context": selection,
                                "required": selection_required}]
                              if selection is not None else [])
                memories, memory_warning, memory_threshold = [], "", 0.6
                if self._memory is not None:
                    try:
                        loader = getattr(self._memory, "load_settings", None)
                        memory_enabled = True
                        if callable(loader):
                            memory_settings = loader()
                            memory_threshold = memory_settings.relevance_threshold
                            memory_enabled = getattr(memory_settings, "enabled", True)
                        if memory_enabled:
                            request["phase"] = "retrieving_memory"
                            self._publish_request_phase(token, conversation_id, "retrieving_memory")
                        search = getattr(self._memory, "search_for_request", None)
                        if callable(search):
                            memories = search(user_message["content"], limit=3, cancelled=request["cancel"])
                        else:
                            memories = self._memory.search(user_message["content"], limit=3)
                    except Exception:
                        memory_warning = "长期记忆检索暂不可用，本轮使用会话和屏幕情境继续。"
                card = request["character_card"]
                base_prompt = character_system_prompt(card)
                prompt = direct_agent_system_prompt(card) if direct_agent else agent_system_prompt(base_prompt) if allow_agent else base_prompt
                context = ContextAssembler(budget, memory_threshold=memory_threshold).assemble(system_prompt=prompt,
                    user_text=self._desktop_prompt_text(user_message["content"], image_metadata), recent_messages=recent,
                    summary=context_summary(conversation, [item for item in all_messages
                                             if item["id"] != user_message["id"]]),
                    screen_events=events, memories=memories, selection_contexts=selections)
                if self._stop.is_set() or request["cancel"].is_set():
                    raise ApiError("cancelled", "请求已取消。")
                self._publish_request_phase(token, conversation_id, "generating_reply")
                request_messages = context.messages
                if image_url is not None or pair_references:
                    request_messages = deepcopy(context.messages)
                    request_message_ids = list(context.message_ids)
                    source_id = image_metadata.get("context_image_message_id") if "image_id" not in image_metadata else None
                    references = list(pair_references)
                    if comparison_url is not None:
                        references.append((comparison_source, comparison_url))
                    if image_url is not None:
                        references.append((source_id, image_url))
                    for reference_index, (source_id, reference_url) in enumerate(references):
                        target_index = -1
                        if source_id:
                            try:
                                target_index = request_message_ids.index(source_id)
                            except ValueError:
                                original = next((item for item in all_messages if item["id"] == source_id
                                                 and item["role"] == "user"), None)
                                if original is None:
                                    raise ApiError("image_unavailable", IMAGE_ERRORS["image_unavailable"])
                                later = [request_message_ids.index(next_source) for next_source, _ in references[reference_index + 1:]
                                         if next_source and next_source in request_message_ids]
                                target_index = min(later) if later else len(request_messages) - 1
                                request_messages.insert(target_index,
                                    {"role": "user", "content": "本会话先前上传的图片（仅作历史参考）：" + original["content"]})
                                request_message_ids.insert(target_index, source_id)
                        request_messages[target_index]["content"] = [
                            {"type": "text", "text": request_messages[target_index]["content"]},
                            {"type": "image_url", "image_url": {"url": reference_url}},
                        ]
                request_messages = self._redact_reference(request_messages, key)
                request["trace"].context(request_messages, context, settings.model)
                self._save_execution(request)
                complete = getattr(self._client, "complete_context", None)
                if direct_agent:
                    # Native tool loop: no preceding chat/classification call.
                    reply = None
                elif callable(complete):
                    options = {"output_tokens": context.output_reserve}
                    if isinstance(self._client, ApiClient):
                        options["cancellation_event"] = request["cancel"]
                        options["on_delta"] = lambda text: self._stream_delta.emit(token, conversation_id, text)
                        if getattr(context, "policy", "manual") == "model":
                            def recover_capacity(rejected_messages):
                                nonlocal context
                                from .conversations.context_protocol import compact_provider_messages
                                from .conversations.context import estimate_tokens
                                before = len(json.dumps(rejected_messages, ensure_ascii=False).encode("utf-8"))
                                compacted = compact_provider_messages(rejected_messages,
                                    target_bytes=max(1024, min(4194304, before // 2)))
                                if compacted == rejected_messages:
                                    return rejected_messages
                                omitted = max(0, len(rejected_messages) - len(compacted) + 1)
                                context = replace(context, messages=compacted,
                                    estimated_input_tokens=sum(estimate_tokens(json.dumps(row, ensure_ascii=False))
                                                               for row in compacted),
                                    dropped_messages=omitted,
                                    history_messages=max(0, context.history_messages - omitted))
                                request["context_compacted"] = True
                                request["trace"].context(compacted, context, settings.model)
                                self._save_execution(request)
                                return compacted
                            options["on_context_exceeded"] = recover_capacity
                        if allow_agent:
                            options["allow_agent_routing"] = True
                    reply = complete(settings, key, request_messages, **options)
                else:
                    # Compatibility for existing lightweight test/client adapters.
                    reply = self._client.complete(settings, key,
                        [item for item in request_messages if item["role"] in {"user", "assistant"}])
                if direct_agent or allow_agent and isinstance(reply, ChatReply) and reply.agent_request is not None:
                    try:
                        route = AgentRequest(user_message["content"], "agent") if direct_agent else AgentRequest.from_value(reply.agent_request)
                    except AgentValidationError:
                        raise ApiError("invalid_agent_route", "任务分派格式无效，本轮未启动任务助手。") from None
                    request["agent_kind"] = route.kind
                    request["trace"].event("summary:delegation", "summary",
                                           "任务分派说明（直接请求）" if direct_agent else "任务分派说明（模型公开回复）",
                                           "completed", detail="直接进入电脑 Agent 工具流程。" if direct_agent else reply.reply)
                    self._save_execution(request)
                    # Settings may change while the ordinary model is replying.
                    # Claim the request before checking the latest immutable
                    # snapshot so disabling it also cancels a dispatch in flight.
                    agent_settings = shared_api_settings(self._agent_settings, settings)
                    if not agent_settings.enabled:
                        raise ApiError("agent_disabled", "任务助手已关闭，本轮未执行工具或电脑操作。")
                    if route.kind == "tools" and not agent_settings.tools_enabled:
                        raise ApiError("agent_disabled", "工具任务尚未启用，请在设置 → 任务助手中开启。")
                    configured_gui_key = key
                    # The shared API is the only request destination. Keep the
                    # captured queue policy and actual dispatch in agreement.
                    gui_key = configured_gui_key if route.kind in {"agent", "computer"} else ""
                    if route.kind == "computer":
                        if not agent_settings.computer_enabled:
                            raise ApiError("agent_disabled", "电脑操作尚未启用，请在设置 → 电脑 Agent 中开启。")
                    if request["cancel"].is_set() or self._stop.is_set():
                        raise ApiError("cancelled", "请求已取消。")
                    # Anchor execution to the user's actual request, not a
                    # rewritten task or instructions copied from references.
                    route = AgentRequest(user_message["content"], route.kind)
                    try:
                        prepared={"api":settings,"key":key,"agent":agent_settings,"gui_key":configured_gui_key}
                        with (self._agent_queue.reserve_guard(request,job_context,prepared=prepared) if self._agent_queue is not None else nullcontext()):
                            with request["write_lock"]:
                                self.store.reserve_agent_execution(conversation_id, user_message["id"])
                                request["metadata"]["agent_execution_started"] = True
                                user_message.setdefault("metadata", {})["agent_execution_started"] = True
                    except AgentError as exc:
                        raise ApiError("agent_"+exc.code,exc.message) from None
                    request["trace"].models(settings.model,
                        agent_settings.gui_model if route.kind in {"computer", "agent"} and gui_key else "")
                    self._publish_request_phase(token, conversation_id, "agent_starting")
                    agent_messages = [item for item in text_messages(request_messages, trace_redact)
                                      if item["role"] in {"system", "user", "assistant"}]
                    def agent_progress(phase):
                        self._publish_request_phase(token, conversation_id, "agent_" + phase)
                    def agent_event(event):
                        if not isinstance(event, dict):
                            return
                        from desktop_pet.agents.activity_subject import subject_detail_for_label
                        detail = (subject_detail_for_label(event.get("label"), event.get("subject"))
                                  if event.get("kind") == "tool" else None)
                        if request["trace"].event(event.get("id"), event.get("kind"), event.get("label"),
                                                  event.get("status", "running"), step=event.get("step"),
                                                  detail=detail):
                            self._save_execution(request)
                    def agent_interaction(pending):
                        if self._agent_queue is not None:
                            self._agent_queue.interaction(job_context,request,pending)
                        self.agent_interaction_changed.emit(pending)
                    try:
                        agent_options = {"cancellation_event": request["cancel"], "progress_callback": agent_progress}
                        if isinstance(self._agent_service, AgentService):
                            agent_options["event_callback"] = agent_event
                            agent_options["interaction_callback"] = agent_interaction
                            if self._agent_queue is not None:
                                agent_options["execution_attempt_callback"] = lambda attempt:self._agent_queue.execution_attempt_bound(request,job_context,attempt)
                                agent_options["worker_lifecycle_callback"] = lambda handle:self._agent_queue.worker_bound(request,job_context,handle)
                                agent_options["worker_dispatch_guard"] = lambda handle:self._agent_queue.dispatch_guard(request,job_context,handle)
                                if job_context is not None:
                                    agent_options["guardian_launch"] = self._agent_queue.guardian_launch(request,job_context)
                        agent_context = {"api": settings, "messages": agent_messages, "gui_api_key": gui_key}
                        agent_context["personality"] = harness_personality(request["character_card"])
                        agent_context["conversation_id"] = conversation_id
                        agent_context["conversation_generation"] = all_messages[0]["id"] if all_messages else user_message["id"]
                        window = getattr(self._budget, "model_context_tokens", None)
                        if window is not None:
                            agent_context["model_context_window"] = window
                        image_catalog = []
                        for item in all_messages:
                            attachment = item.get("metadata", {}).get("image_id")
                            if isinstance(attachment, str) and IMAGE_ID.fullmatch(attachment):
                                image_catalog.append({"attachment_id": attachment, "label": item.get("content", "图片")[:240] or "图片"})
                        image_catalog = image_catalog[-64:]
                        if image_catalog and isinstance(self._agent_service, AgentService):
                            agent_context["image_catalog"] = image_catalog
                            allowed_images = {item["attachment_id"] for item in image_catalog}
                            def resolve_image(reference):
                                if reference not in allowed_images:
                                    raise ValueError("IMAGE_NOT_IN_CONVERSATION")
                                return self._load_chat_image(reference)
                            agent_options["image_resolver"] = resolve_image
                        if direct_agent:
                            agent_context["conversation_id"] = conversation_id
                            # Clearing a conversation changes its first stored
                            # message, so even a failed checkpoint deletion
                            # cannot resurrect the previous native tool state.
                            agent_context["conversation_generation"] = (
                                all_messages[0]["id"] if all_messages else user_message["id"])
                            if image_url is not None and image_metadata.get("image_id"):
                                agent_context["current_image_url"] = image_url
                                agent_context["current_image_id"] = image_metadata["image_id"]
                        if self._agent_queue is not None: self._agent_queue.service_entered(job_context)
                        outcome = self._agent_service.execute(agent_settings, key, route,
                            agent_context, **agent_options)
                    except AgentError as exc:
                        raise ApiError("agent_" + exc.code, exc.message) from None
                    except AgentValidationError as exc:
                        raise ApiError("agent_configuration", str(exc)) from None
                    text = outcome.reply
                    request["result_state"] = "needs_user" if outcome.status == "needs_user" else "completed"
                    request["agent_artifact_refs"]=tuple(item["reference"] for item in outcome.artifacts[:16]
                        if isinstance(item,dict) and isinstance(item.get("reference"),str))
                    reply = ChatReply(text, outcome.emotion)
                if not isinstance(reply, ChatReply) or not isinstance(reply.reply, str) or not reply.reply.strip():
                    raise ApiError("invalid_response", "服务商没有返回可显示的文本回复。")
                public_reply = self._redact_known_secrets(reply.reply, key)
                if not request.get("agent_kind") and getattr(context, "policy", "manual") != "model":
                    public_reply = public_reply[:MAX_REPLY_CHARACTERS]
                reply = ChatReply(public_reply,
                                  reply.emotion if reply.emotion in EMOTIONS else "neutral")
                generation = all_messages[0]["id"] if all_messages else user_message["id"]
                with request["commit_lock"]:
                    if self._stop.is_set() or request["cancel"].is_set():
                        raise ApiError("cancelled", "请求已取消。")
                    self._publish_request_phase(token, conversation_id, "saving_reply")
                    request["trace"].finish(request.get("result_state", "completed"))
                    execution = self._save_execution(request, persist=False)
                    try:
                        saved_reply = self.store.complete_request(conversation_id, user_message["id"], reply.reply,
                                                                 emotion=reply.emotion, execution=execution)
                    except Exception:
                        try:
                            saved_reply = self._read_committed_reply(request, reply, execution, generation)
                        except Exception:
                            candidate = (self._agent_queue.unknown_completion_candidate(
                                job_context, request, reply, execution, generation,
                                completed=outcome.status == "completed") if job_context is not None else None)
                            return {"save_unconfirmed": True, "completion_candidate": candidate}
                        if saved_reply is None:
                            raise
                    request["committed"] = True
                    request["saved_reply_id"] = saved_reply["id"]
                return {"reply": reply, "saved_reply": saved_reply,
                    "conversation": self.store.get_conversation(conversation_id),
                    "messages": self.store.load_messages(conversation_id), "context": context,
                    "memory_warning": memory_warning, "has_image": image_url is not None,
                    "context_compacted": bool(request.get("context_compacted"))}
            except Exception as exc:
                # Do not let an API failure erase an already committed user message.
                if request["committed"]:
                    return {"history_failure": "回复已保存，但重新读取记录失败。请重新读取此会话。"}
                stop_unconfirmed = isinstance(exc, ApiError) and exc.code == "agent_stop_unconfirmed"
                cancelled = (self._stop.is_set() or request["cancel"].is_set()) and not stop_unconfirmed
                if cancelled: request["stop_confirmed"] = True
                if isinstance(exc, ApiError) and exc.partial_reply and not cancelled:
                    try:
                        self._stream_delta.emit(token, conversation_id,
                            self._redact_known_secrets(exc.partial_reply, key))
                    except RuntimeError:
                        pass
                request["trace"].finish("cancelled" if cancelled else "failed", replace_completed=True)
                self._save_execution(request, persist=False)
                image_error = exc.code if isinstance(exc, ApiError) and exc.code in IMAGE_ERRORS and not cancelled else None
                status_options = {"image_error_code": image_error} if image_error else {}
                status_options["execution"] = request["metadata"]["execution"]
                if isinstance(exc, ApiError) and request.get("agent_kind"):
                    code = exc.code[6:] if exc.code.startswith("agent_") else exc.code
                    if code in AGENT_ERRORS:
                        request["metadata"]["agent_error_code"] = code
                        status_options["agent_error_code"] = code
                self.store.set_message_status(conversation_id, user_message["id"],
                                              "cancelled" if cancelled else "failed", **status_options)
                if cancelled:
                    return {"cancelled": True}
                return {"failure": self._redact_known_secrets(str(exc), key) if isinstance(exc, (ApiError, StoreError))
                        else "操作失败，请检查本地存储、配置或网络后重试。",
                        "configuration_error": isinstance(exc, ApiError) and exc.code in
                        {"authentication", "permission", "model_not_found", "not_configured", "invalid_messages", "vision_rejected", "reasoning_unsupported", "gui_not_configured", "agent_configuration", "agent_disabled",
                         "agent_kind_disabled", "agent_not_configured", "agent_missing_backend", "agent_missing_sdk",
                         "agent_gui_not_configured", "agent_gui_runtime_missing", "agent_gui_sdk_unavailable"},
                        "image_error": image_error}
        def run():
            request["worker_entered"] = True
            result=None
            try:
                result=run_turn()
                return result
            finally:
                # Run even when UI callbacks are discarded on Stop/shutdown.
                if job_context is not None:
                    try: self._agent_queue.finished(job_context,request,result)
                    except Exception:
                        # A ledger outage cannot erase an already committed
                        # chat reply. It does block all later Agent effects.
                        with self._agent_queue.lock: self._agent_queue._failed=True

        def accepts_feedback():
            return (not self._closed and token == self._request_id
                    and conversation_id == self._current_id
                    and conversation_id not in self._invalidating_conversations
                    and conversation_id not in self._deleted_conversations)

        def finished(result, error):
            if job_context is not None and not request.get("worker_entered"):
                # A cancelled-before-entry runnable never entered Service.
                # Preserve the coordinator's own closure rules for every run
                # that did enter, rather than treating thread drain as proof.
                try: self._agent_queue.finished(job_context, request, result)
                except Exception:
                    with self._agent_queue.lock: self._agent_queue._failed = True
            if (result and result.get("save_unconfirmed") and not self._closed
                    and self._request_owners.get((conversation_id, user_message["id"])) is request):
                candidate = result.get("completion_candidate")
                if candidate is not None:
                    self._historical_reply_candidates[(conversation_id, user_message["id"])] = candidate
                execution = deepcopy(request["metadata"]["execution"])
                execution["state"] = "interrupted"
                execution["events"] = [event for event in execution["events"] if event["kind"] != "summary"]
                execution["events"].append({"id": "summary:commit_unconfirmed", "kind": "summary",
                    "elapsed_ms": execution["duration_ms"], "label": "回复保存结果待确认", "status": "failed"})
                self._unconfirmed_reply_commits[(conversation_id, user_message["id"])] = execution
                self._mark_message_status(conversation_id, user_message["id"], "interrupted", metadata=request["metadata"])
            target = (conversation_id, user_message["id"])
            self._requests.pop(target, None)
            self._retire_inactive_request_owners(conversation_id)
            self._refresh_work_active()
            # The worker may have closed and emitted its wake before this UI
            # callback removes the last request. Wake again for every exit,
            # including a stale token after Stop; an idle queue has no poller.
            self._agent_queue_wake.emit()
            self.retry_availability_changed.emit()
            if token != self._request_id:
                if result and result.get("already_complete"):
                    self._mark_message_status(conversation_id, user_message["id"], "complete")
                if result and result.get("cancelled"):
                    self._mark_message_status(conversation_id, user_message["id"], "cancelled",
                                              metadata=request["metadata"])
                    if (not self._closed and self._error_action == "retry"
                            and self._error_conversation_id == conversation_id
                            and self._error_message_id == user_message["id"] and not self._busy):
                        self._set_error("已停止等待回复；现在可以重试这条消息，也可继续聊天。",
                                        "retry", conversation_id, user_message["id"])
                    elif (not self._closed and request.get("agent_kind") and not self._busy
                            and self._error_conversation_id == conversation_id
                            and self._error_message_id == user_message["id"]):
                        self._set_error(AGENT_ERRORS["cancelled"], "", conversation_id, user_message["id"])
                elif result and result.get("failure"):
                    self._mark_message_status(conversation_id, user_message["id"], "failed", metadata=request["metadata"])
                    if (not self._closed and not self._busy and self._error_conversation_id == conversation_id
                            and self._error_message_id == user_message["id"]):
                        self._set_error(result["failure"], "", conversation_id, user_message["id"])
                self._refresh_conversations()
                return
            self._active_request = None
            self._active_request_conversation = None
            if result and (result.get("saved_reply") or result.get("already_complete")):
                self._clear_stream_preview()
            else:
                self._interrupt_stream_preview(token, conversation_id)
            self._set_busy(False)
            if result and result.get("already_complete"):
                self._mark_message_status(conversation_id, user_message["id"], "complete")
                self._install_snapshot(conversation_id, result["conversation"], result["messages"])
                if accepts_feedback():
                    self.emotion_requested.emit("neutral")
                self._refresh_conversations()
                return
            error = error or (result or {}).get("failure", "")
            if result and result.get("save_unconfirmed"):
                if accepts_feedback():
                    self._set_error(REPLY_COMMIT_UNCONFIRMED, "history", conversation_id, user_message["id"])
                    self.emotion_requested.emit("neutral")
                self._refresh_conversations()
                return
            if result and result.get("history_failure"):
                self._mark_message_status(conversation_id, user_message["id"], "complete")
                if accepts_feedback():
                    self._set_error(result["history_failure"], "history", conversation_id)
                    self.emotion_requested.emit("neutral")
                self._refresh_conversations()
                return
            if error:
                failed = deepcopy(user_message)
                failed["metadata"] = dict(request["metadata"], request_status="failed")
                if (result or {}).get("image_error"):
                    failed["metadata"]["image_error_code"] = result["image_error"]
                self._retry_messages[conversation_id] = failed
                self._recoverable_messages[(conversation_id, user_message["id"])] = failed
                if conversation_id == self._current_id:
                    for item in self._messages:
                        if item.get("id") == user_message["id"]:
                            item["metadata"] = deepcopy(failed["metadata"])
                    self._notify_messages_changed()
                if accepts_feedback():
                    self._set_error(self._redact_known_secrets(error, key),
                                    "settings" if (result or {}).get("configuration_error") else
                                    "" if failed["metadata"].get("agent_execution_started") else "retry",
                                    conversation_id, user_message["id"])
                if accepts_feedback():
                    self.emotion_requested.emit("neutral")
            elif result and not result.get("cancelled"):
                self._recoverable_messages.pop((conversation_id, user_message["id"]), None)
                if self._retry_messages.get(conversation_id, {}).get("id") == user_message["id"]:
                    self._retry_messages.pop(conversation_id, None)
                self._install_snapshot(conversation_id, result["conversation"], result["messages"])
                context = result["context"]
                if getattr(context, "policy", "manual") == "model":
                    usage = (f"上下文跟随模型；本轮文本输入估算 {context.estimated_input_tokens} Token，"
                             f"携带 {getattr(context, 'history_messages', 0)} 条历史消息。")
                    if result.get("context_compacted"):
                        usage += f" 模型窗口拒绝后压缩了 {context.dropped_messages} 条旧记录，原始记录仍在本地。"
                else:
                    usage = (f"本次输入估算 {context.estimated_input_tokens} / "
                             f"{budget.total_tokens - budget.output_reserve} Token，输出预留 {context.output_reserve}；"
                             f"未携带 {context.dropped_messages} 条较早消息。")
                if result["memory_warning"]:
                    usage += " " + result["memory_warning"]
                    self.memory_status_changed.emit(result["memory_warning"])
                if result.get("has_image"):
                    usage += " 图片已随本轮问题发送；图像 Token 由模型计算，不包含在上述文本估算中。"
                if accepts_feedback():
                    self._set_usage(usage)
                if accepts_feedback():
                    self.emotion_requested.emit(result["reply"].emotion)
                # Deliver only this newly persisted ordinary reply. Loading a
                # history snapshot (or saving a screen comment) is not speech.
                # Earlier GUI signals may switch/clear the view or start a new
                # request, so validate again at the point of delivery.
                if accepts_feedback():
                    saved_reply = result["saved_reply"]
                    self.reply_received.emit(saved_reply["content"], result["reply"].emotion,
                        {"conversation_id": conversation_id, "message_id": saved_reply["id"]})
                self._schedule_metadata(conversation_id, settings, key)
                self._extract_memory(user_message)
            elif result and result.get("cancelled"):
                self._mark_message_status(conversation_id, user_message["id"], "cancelled",
                                          metadata=request["metadata"])
            self._refresh_conversations()
        self._submit_image_storage_task(run, finished,
            on_submit_failed=lambda: request["cancel"].set())

    def fetch_models(self, settings, key, callback) -> bool:
        """Fetch unsaved settings independently of the active chat request.

        Each caller owns its callback; no shared list, request token or chat
        status is changed. TaskRunner delivers accepted work on the Qt thread.
        Invalid input is reported immediately without consulting saved keys.
        """
        if self._closed or not callable(callback):
            return False
        try:
            if not isinstance(settings, ApiSettings):
                raise SettingsError("invalid settings")
            settings.validate(require_model=False)
        except (SettingsError, TypeError, AttributeError):
            callback([], "API 配置无效，请检查 Base URL、超时和思考强度。")
            return False
        if not isinstance(key, str) or not key.strip():
            callback([], MODEL_LIST_ERRORS["not_configured"])
            return False
        if any(ord(character) < 32 or ord(character) == 127 for character in key):
            callback([], "API Key 不能包含换行或控制字符。")
            return False
        request_key = key.strip()

        def request():
            if self._stop.is_set():
                return [], MODEL_LIST_ERRORS["cancelled"]
            try:
                result = self._client.list_models(settings, request_key)
            except ApiError as exc:
                # Never expose provider exception bodies, even from adapters.
                return [], MODEL_LIST_ERRORS.get(exc.code, MODEL_LIST_FAILURE)
            except Exception:
                return [], MODEL_LIST_FAILURE
            if not isinstance(result, list) or any(not isinstance(model, str) for model in result):
                return [], MODEL_LIST_ERRORS["invalid_response"]
            from .model_reasoning_metadata import ModelChoices
            admitted = [model for model in result if request_key not in model]
            models = ModelChoices(admitted, reasoning_metadata={model: metadata for model, metadata in
                getattr(result, "reasoning_metadata", {}).items() if model in admitted})
            return models, "" if models else MODEL_LIST_ERRORS["empty_models"]

        def finished(result, error):
            if self._closed:
                return
            if error or not isinstance(result, tuple) or len(result) != 2:
                callback([], MODEL_LIST_FAILURE)
            else:
                callback(*result)

        return self._runner.submit(request, finished) is not None

    def test_connection(self, settings, key):
        if self._closed or self._busy:
            return False
        if not self._validate_configuration(settings, key):
            self._set_status("连接未测试：请先检查配置。")
            return False
        self._request_id += 1
        token = self._request_id
        self._request_kind = "connection"
        self._request_phase, self._request_phase_rank = "", -1
        self._active_request_conversation = None
        self._set_error("")
        self._set_status("正在发起聊天请求以测试连接……")
        self._set_busy(True)
        def finished(result, error):
            if token != self._request_id:
                return
            self._set_busy(False)
            if error:
                error = self._redact_known_secrets(error, key)
                self._set_error(error)
                self._set_status("连接失败：" + error)
            else:
                self._set_status(self._redact_known_secrets(str(result), key))
        self._runner.submit(lambda: self._client.test_connection(settings, key), finished)
        return True

    def record_screen_comment(self, conversation_id, content, *, event_id, app_name,
                              context_summary, event_type, emotion="neutral", observed_at=None,
                              source_kind="screen"):
        if source_kind not in ("screen", "camera"):
            return None
        if (self._closed or conversation_id in self._invalidating_conversations
                or conversation_id in self._deleted_conversations):
            return None
        try:
            key = self._settings_manager.get_api_key()
        except CredentialsError:
            key = ""
        content = self._redact_known_secrets(content, key)
        context_summary = self._redact_known_secrets(context_summary, key)
        app_name = self._redact_known_secrets(app_name, key)
        reference = {"conversation_id": conversation_id, "message_id": str(uuid4()),
                     "event_id": event_id, "content": content,
                      "emotion": emotion if emotion in EMOTIONS else "neutral", "source_kind": source_kind}
        self._pending_screen_count[conversation_id] = self._pending_screen_count.get(conversation_id, 0) + 1
        def save():
            message = self.store.append_screen_comment(conversation_id, content, event_id=event_id,
                app_name=app_name, context_summary=context_summary, event_type=event_type,
                observed_at=observed_at, message_id=reference["message_id"], metadata={"emotion": reference["emotion"]},
                source_kind=source_kind, created_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
            return message, self.store.get_conversation(conversation_id), self.store.load_messages(conversation_id)
        def finished(result, error):
            self._pending_screen_count[conversation_id] -= 1
            if error:
                self._set_error(("摄像头" if source_kind == "camera" else "屏幕") + "评论未能保存：" + error)
                self.screen_comment_finished.emit(reference, error)
                return
            self._install_snapshot(conversation_id, result[1], result[2])
            self._refresh_conversations()
            self.screen_comment_finished.emit(reference, "")
            configuration = self._configuration_for_metadata()
            self._schedule_metadata(conversation_id, *(configuration or (None, "")))
        self._runner.submit(save, finished)
        return reference

    def _configuration_for_metadata(self):
        try:
            settings = self._settings_manager.load_api_settings()
            key = self._settings_manager.get_api_key()
            settings.validate()
            return (settings, key) if key else None
        except (SettingsError, CredentialsError):
            return None

    def _schedule_metadata(self, conversation_id, settings, key):
        if self._closed or conversation_id in self._metadata_active:
            return
        self._metadata_active.add(conversation_id)
        client = self._metadata_factory() if self._metadata_factory and settings is not None else None
        if client is not None:
            self._metadata_clients.add(client)
        def update():
            try:
                if self._stop.is_set():
                    return None
                return update_session_metadata(self.store, conversation_id, client, settings, key)
            finally:
                if client is not None:
                    cancel = getattr(client, "shutdown", None)
                    if callable(cancel):
                        cancel()
        def finished(result, error):
            self._metadata_active.discard(conversation_id)
            self._metadata_clients.discard(client)
            if result and conversation_id == self._current_id:
                self._current_title = result["title"]
                self._current_summary = result["summary"]
                self.conversation_changed.emit()
            if not error:
                self._refresh_conversations()
        self._runner.submit(update, finished)

    def _extract_memory(self, user_message):
        if self._memory is None or self._closed:
            return
        if user_message.get("metadata", {}).get("selection_context"):
            # Webpage quotations must not become inferred personal preferences,
            # even if the selected text itself contains an explicit assertion.
            return
        # The service itself checks enabled/automatic and admits only explicit
        # durable user assertions. Screen observations and assistant text never
        # enter this extraction path.
        message = deepcopy(user_message)
        def extract():
            loader = getattr(self._memory, "load_settings", None)
            if callable(loader):
                settings = loader()
                if not settings.enabled or not settings.automatic:
                    return []
            return self._memory.extract([message], conversation_id=message["conversation_id"], trigger="automatic")
        def finished(result, error):
            if error:
                self.memory_status_changed.emit("长期记忆提炼未完成；聊天消息已经保存在本机。 " + error)
            elif result:
                self.memory_status_changed.emit(f"已通过 Mem0 更新 {len(result)} 条长期记忆。")
                self.memory_saved.emit()
        self._runner.submit(extract, finished)

    def shutdown(self):
        """Cancel network work immediately; accepted database writes finish."""
        if self._closed:
            return
        self._closed = True
        self.notify_image_storage_changed()
        self._refresh_work_active()
        self._agent_queue_timer.stop()
        if self._agent_queue is not None: self._agent_queue.shutdown()
        self._stop.set()
        cancel_memory = getattr(self._memory, "cancel_request", None)
        if callable(cancel_memory):
            for request in list(self._requests.values()):
                cancel_memory(request["cancel"])
        self._agent_service.shutdown()
        self._request_id += 1
        cancel = getattr(self._client, "shutdown", None)
        if callable(cancel):
            cancel()
        for client in list(self._metadata_clients):
            cancel = getattr(client, "shutdown", None)
            if callable(cancel):
                cancel()
        draft_owner = getattr(self, "_local_draft_owner", None)
        finalize_drafts = None
        if type(draft_owner) is LocalDraftOwner:
            def finalize_drafts():
                # This callback runs only after real accepted worker functions
                # finish, independently of queued GUI result delivery.
                changed = getattr(draft_owner, "archive_changed", None)
                if callable(changed): changed()
                draft_owner.shutdown(timeout=1)
        def drained():
            try:
                if finalize_drafts is not None: finalize_drafts()
            finally:
                self._image_storage_gate.close_owner(self._image_storage_owner)
        self._runner.shutdown(on_drained=drained)
        self._set_busy(False)
