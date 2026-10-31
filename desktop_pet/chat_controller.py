"""Persistent, isolated chat sessions with asynchronous storage and requests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import weakref
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .api_client import ApiClient, ApiError, ChatReply, EMOTIONS, MAX_REPLY_CHARACTERS, SYSTEM_PROMPT, redact_secret
from .async_tasks import TaskRunner
from .conversation_metadata import context_summary, update_session_metadata
from .conversations import ConversationStore, ContextAssembler, ContextBudget, ContextBudgetError, StoreError
from .conversations.export import export_conversation_file
from .conversations.chronology import timestamp_key
from .settings_manager import ApiSettings, CredentialsError, SettingsError, SettingsManager
from .selection.context import SelectionContext, SelectionContextError


MAX_INPUT_CHARACTERS = 8_000
# Kept as import-compatible constants. The archive is complete; request context
# is bounded by ContextBudget rather than the number of displayed messages.
MAX_VISIBLE_MESSAGES = 50
MAX_CONTEXT_TURNS = 12
MAX_CONTEXT_CHARACTERS = 48_000
REQUEST_PHASES = {
    "saving_message": (0, "正在保存消息…"),
    "retrieving_memory": (1, "正在检索相关记忆…"),
    "generating_reply": (2, "正在生成回复…"),
    "saving_reply": (3, "正在保存回复…"),
}
RECOVERABLE_STATUSES = {"failed", "cancelled", "interrupted"}


class ChatController(QObject):
    messages_changed = Signal()
    busy_changed = Signal(bool)
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
    _request_phase_changed = Signal(int, str, str)

    def __init__(self, settings_manager: SettingsManager, api_client: ApiClient | None = None,
                 parent: QObject | None = None, *, store: ConversationStore | None = None,
                 memory_service=None, context_budget: ContextBudget | None = None,
                 metadata_client_factory=None):
        super().__init__(parent)
        self._settings_manager = settings_manager
        self._client = api_client if api_client is not None else ApiClient()
        self._memory = memory_service
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
        self._metadata_active = set()
        self._metadata_clients = set()
        self._metadata_factory = metadata_client_factory
        if self._metadata_factory is None and isinstance(self._client, ApiClient):
            self._metadata_factory = ApiClient
        self._request_phase_changed.connect(self._accept_request_phase, Qt.ConnectionType.QueuedConnection)
        self.conversation_changed.connect(self._refresh_request_status)
        self._refresh_conversations()

    @property
    def messages(self):
        return deepcopy(self._messages)

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
        request = self._requests.get((conversation_id, message_id))
        if request is not None:
            phase = request.get("phase", "")
            label = "上次记忆检索" if phase == "retrieving_memory" else "上次请求"
            return {"available": False, "status": label + "仍在结束；结束后可重试，也可继续发送新消息。"}
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
        self._refresh_request_status()

    def _publish_request_phase(self, token, conversation_id, phase):
        try:
            self._request_phase_changed.emit(token, conversation_id, phase)
        except RuntimeError:
            pass  # Qt may already be gone while an accepted disk write finishes.

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
            self._set_error(redact_secret(str(exc), key or ""), "settings")
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
            if (item.get("metadata", {}).get("request_status") == "pending"
                    and previous.get("metadata", {}).get("request_status") in {"complete", *RECOVERABLE_STATUSES}
                    and (conversation_id, item["id"]) not in self._requests):
                item["metadata"]["request_status"] = previous["metadata"]["request_status"]
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
        self.messages_changed.emit()

    def _recover_messages(self, conversation_id, messages):
        for item in messages:
            if item.get("role") != "user":
                continue
            metadata = item.setdefault("metadata", {})
            status = metadata.get("request_status")
            request = self._requests.get((conversation_id, item["id"]))
            if request is not None:
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
        self.messages_changed.emit()
        return self._current_id

    def open_conversation(self, conversation_id, message_id=None, *, focus_message_id=None):
        if self._closed or not isinstance(conversation_id, str) or not conversation_id:
            return False
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
        self.messages_changed.emit()
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
                self.messages_changed.emit()
                return
            self._install_snapshot(conversation_id, *result)
            if message_id:
                self.message_focus_requested.emit(message_id)
        self._runner.submit(load, finished)
        return True

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
        if self._closed:
            return False
        if (any(key[0] == conversation_id for key in self._requests)
                or conversation_id == self._active_request_conversation or self._pending_screen_count.get(conversation_id)
                or self._pending_cancel_count.get(conversation_id)):
            self._set_error("请等待此会话的消息保存和请求结束后再删除。")
            return False
        if conversation_id in self._invalidating_conversations:
            return False
        self._invalidating_conversations.add(conversation_id)
        self._deleted_conversations.add(conversation_id)
        self.conversation_invalidated.emit(conversation_id)
        def finished(result, error):
            self._invalidating_conversations.discard(conversation_id)
            if error:
                self._deleted_conversations.discard(conversation_id)
                self._set_error(error)
                return
            self._retry_messages.pop(conversation_id, None)
            self._recoverable_messages = {key: value for key, value in self._recoverable_messages.items()
                                          if key[0] != conversation_id}
            self._request_owners = {key: value for key, value in self._request_owners.items() if key[0] != conversation_id}
            self._message_locks = {key: value for key, value in self._message_locks.items() if key[0] != conversation_id}
            if self._current_id == conversation_id:
                self.new_conversation()
            self._refresh_conversations()
        self._runner.submit(lambda: self.store.delete_conversation(conversation_id), finished)
        return True

    def clear(self):
        if self._closed:
            return
        conversation_id = self._current_id
        if (self._busy or any(key[0] == conversation_id for key in self._requests)
                or self._pending_screen_count.get(conversation_id) or self._pending_cancel_count.get(conversation_id)):
            self._set_error("请等待当前请求和消息保存结束，再清空聊天记录。")
            return
        if conversation_id in self._invalidating_conversations:
            return
        self._invalidating_conversations.add(conversation_id)
        self.conversation_invalidated.emit(conversation_id)
        if not self._history_loading:
            self._history_restore_view = self._visible_view()
        self._load_id += 1
        token = self._load_id
        self._set_history_loading(True)
        # Clear is optimistic; a disk failure restores the previous visible data.
        previous = self._messages
        self._messages = []
        self.messages_changed.emit()
        def clear():
            self.store.clear_messages(conversation_id)
            return self.store.get_conversation(conversation_id)
        def finished(result, error):
            self._invalidating_conversations.discard(conversation_id)
            if self._history_restore_view and self._history_restore_view[0] == conversation_id:
                # If a different history load fails after this clear succeeds,
                # restoring A must show the cleared view rather than old text.
                if not error:
                    view = self._history_restore_view
                    self._history_restore_view = (conversation_id, result["title"] if result else "新对话",
                                                  "", [], view[4])
            if self._current_id == conversation_id and token == self._load_id:
                self._set_history_loading(False)
                self._history_restore_view = None
            if error:
                if self._current_id == conversation_id:
                    self._messages = previous
                    self.messages_changed.emit()
                self._set_error(error)
                return
            self._retry_messages.pop(conversation_id, None)
            self._recoverable_messages = {key: value for key, value in self._recoverable_messages.items()
                                          if key[0] != conversation_id}
            self._request_owners = {key: value for key, value in self._request_owners.items() if key[0] != conversation_id}
            self._message_locks = {key: value for key, value in self._message_locks.items() if key[0] != conversation_id}
            if self._current_id == conversation_id:
                self._current_title = result["title"] if result else "新对话"
                self._current_summary = ""
                self.conversation_changed.emit()
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

    def send(self, text, *, message_id=None, conversation_id=None, selection_context=None):
        if self._closed or self._busy or self._history_loading:
            return False
        target_id = self._current_id if conversation_id is None else conversation_id
        if not isinstance(target_id, str) or not target_id.strip() or len(target_id) > 200:
            return False
        if message_id is not None and (not isinstance(message_id, str) or not message_id.strip()
                                       or len(message_id) > 200):
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
        configuration = self._configuration()
        if configuration is None:
            return False
        settings, key = configuration
        metadata = {"request_status": "pending"}
        if selection_context is not None:
            try:
                metadata["selection_context"] = SelectionContext.from_payload(
                    selection_context, api_key=key).to_dict()
            except (SelectionContextError, TypeError):
                self._set_error("未能获取有效的选中资料，请重新选择文字后再试。")
                return False
        try:
            # Refuse an impossible question before displaying or persisting a
            # failed request; the current question is never silently shortened.
            ContextAssembler(self._budget).assemble(system_prompt=SYSTEM_PROMPT,
                user_text=redact_secret(text, key), recent_messages=[],
                selection_contexts=([{"conversation_id": target_id,
                    "context": metadata["selection_context"], "required": True}]
                    if "selection_context" in metadata else []))
        except ContextBudgetError as exc:
            self._set_error(str(exc))
            return False
        message = {"id": message_id or str(uuid4()), "conversation_id": target_id, "role": "user",
                   "content": redact_secret(text, key), "source": "user", "metadata": metadata,
                   "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
        if target_id == self._current_id:
            self._messages.append(deepcopy(message))
            self.messages_changed.emit()
        self._begin_chat(message, settings, key)
        return True

    def retry(self, conversation_id=None, message_id=None):
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
        finally:
            request["commit_lock"].release()
        cancel = getattr(self._client, "cancel_request", None)
        if callable(cancel):
            cancel(request["cancel"])
        def persist_cancelled():
            # Do not wait for an adapter's network response to preserve Stop.
            # Serialize with the accepted initial user write, including a stop
            # pressed before that worker was scheduled by the thread pool.
            with request["write_lock"]:
                target = (request["conversation_id"], request["message_id"])
                if self._request_owners.get(target) is not request:
                    return
                messages = self.store.load_messages(request["conversation_id"])
                if any(item.get("role") == "assistant" and item.get("source") == "assistant"
                       and item.get("metadata", {}).get("reply_to") == request["message_id"] for item in messages):
                    self.store.reconcile_request(request["conversation_id"], request["message_id"])
                    return
                if not any(item["id"] == request["message_id"] for item in messages):
                    self.store.append_message(request["conversation_id"], "user", request["content"], "user",
                        message_id=request["message_id"], metadata=dict(request["metadata"], request_status="cancelled"),
                        created_at=request["created_at"])
                else:
                    self.store.set_message_status(request["conversation_id"], request["message_id"], "cancelled")
        cid = request["conversation_id"]
        self._pending_cancel_count[cid] = self._pending_cancel_count.get(cid, 0) + 1
        def persisted(_result, error):
            self._pending_cancel_count[cid] -= 1
            if (error and not self._closed and self._request_id == stopped_token and self._current_id == cid
                    and self._request_owners.get((cid, request["message_id"])) is request):
                self._set_error("停止状态未能保存：" + error, "history", cid)
        self._runner.submit(persist_cancelled, persisted)
        self._request_id += 1
        stopped_token = self._request_id
        self._active_request = None
        self._active_request_conversation = None
        self._set_busy(False)
        self._mark_message_status(request["conversation_id"], request["message_id"], "cancelled")
        def accepts_feedback():
            return (not self._closed and self._request_id == stopped_token
                    and request["conversation_id"] == self._current_id)
        if accepts_feedback():
            self._set_error("已停止等待回复；服务端可能已收到请求。" +
                            self.message_retry_state(request["conversation_id"], request["message_id"])["status"],
                            "retry", request["conversation_id"], request["message_id"])
        if accepts_feedback():
            self.emotion_requested.emit("neutral")
        self.retry_availability_changed.emit()
        return True

    def _mark_message_status(self, conversation_id, message_id, status):
        if conversation_id == self._current_id:
            for item in self._messages:
                if item.get("id") == message_id:
                    item.setdefault("metadata", {})["request_status"] = status
                    if status in RECOVERABLE_STATUSES:
                        self._recoverable_messages[(conversation_id, message_id)] = deepcopy(item)
                        self._retry_messages[conversation_id] = deepcopy(item)
                    else:
                        self._recoverable_messages.pop((conversation_id, message_id), None)
                        if self._retry_messages.get(conversation_id, {}).get("id") == message_id:
                            self._retry_messages.pop(conversation_id, None)
            self.messages_changed.emit()

    def _begin_chat(self, user_message, settings, key):
        self._request_id += 1
        token = self._request_id
        conversation_id = user_message["conversation_id"]
        request = {"conversation_id": conversation_id, "message_id": user_message["id"],
                   "content": user_message["content"], "cancel": threading.Event(),
                   "metadata": deepcopy(user_message.get("metadata", {})),
                   "created_at": user_message.get("created_at"),
                   "phase": "saving_message",
                   "commit_lock": threading.Lock(), "committed": False}
        target = (conversation_id, user_message["id"])
        request["write_lock"] = self._message_locks.setdefault(target, threading.Lock())
        self._request_owners[target] = request
        self._requests[target] = request
        self._active_request = request
        self._active_request_conversation = conversation_id
        self._request_kind = "chat"
        self._request_phase, self._request_phase_rank = "saving_message", 0
        self._set_error("")
        self._mark_message_status(conversation_id, user_message["id"], "pending")
        self._set_busy(True)
        self.emotion_requested.emit("thinking")
        budget = self._budget
        def run():
            # The accepted user write always completes, even during shutdown.
            try:
                with request["write_lock"]:
                    all_messages = self.store.load_messages(conversation_id)
                    initial_status = "cancelled" if self._stop.is_set() or request["cancel"].is_set() else "pending"
                    if not any(item["id"] == user_message["id"] for item in all_messages):
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
                        self.store.set_message_status(conversation_id, user_message["id"], initial_status)
                if self._stop.is_set() or request["cancel"].is_set():
                    self.store.set_message_status(conversation_id, user_message["id"], "cancelled")
                    return {"cancelled": True}
                conversation = self.store.get_conversation(conversation_id)
                all_messages = self.store.load_messages(conversation_id)
                recent = [item for item in all_messages if item["id"] != user_message["id"]
                          and item.get("metadata", {}).get("request_status") not in
                              {"pending", *RECOVERABLE_STATUSES}]
                cursor = conversation.get("summary_through_id")
                if cursor:
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
                    for item in reversed(recent[-budget.max_recent_messages:]):
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
                        memories = self._memory.search(user_message["content"], limit=3)
                    except Exception:
                        memory_warning = "长期记忆检索暂不可用，本轮使用会话和屏幕情境继续。"
                context = ContextAssembler(budget, memory_threshold=memory_threshold).assemble(system_prompt=SYSTEM_PROMPT,
                    user_text=user_message["content"], recent_messages=recent,
                    summary=context_summary(conversation, [item for item in all_messages
                                             if item["id"] != user_message["id"]]),
                    screen_events=events, memories=memories, selection_contexts=selections)
                if self._stop.is_set() or request["cancel"].is_set():
                    raise ApiError("cancelled", "请求已取消。")
                self._publish_request_phase(token, conversation_id, "generating_reply")
                complete = getattr(self._client, "complete_context", None)
                if callable(complete):
                    options = {"output_tokens": context.output_reserve}
                    if isinstance(self._client, ApiClient):
                        options["cancellation_event"] = request["cancel"]
                    reply = complete(settings, key, context.messages, **options)
                else:
                    # Compatibility for existing lightweight test/client adapters.
                    reply = self._client.complete(settings, key,
                        [item for item in context.messages if item["role"] in {"user", "assistant"}])
                if not isinstance(reply, ChatReply) or not isinstance(reply.reply, str) or not reply.reply.strip():
                    raise ApiError("invalid_response", "服务商没有返回可显示的文本回复。")
                reply = ChatReply(redact_secret(reply.reply, key)[:MAX_REPLY_CHARACTERS],
                                  reply.emotion if reply.emotion in EMOTIONS else "neutral")
                with request["commit_lock"]:
                    if self._stop.is_set() or request["cancel"].is_set():
                        raise ApiError("cancelled", "请求已取消。")
                    self._publish_request_phase(token, conversation_id, "saving_reply")
                    saved_reply = self.store.complete_request(conversation_id, user_message["id"], reply.reply,
                                                              emotion=reply.emotion)
                    request["committed"] = True
                return {"reply": reply, "saved_reply": saved_reply,
                    "conversation": self.store.get_conversation(conversation_id),
                    "messages": self.store.load_messages(conversation_id), "context": context,
                    "memory_warning": memory_warning}
            except Exception as exc:
                # Do not let an API failure erase an already committed user message.
                if request["committed"]:
                    return {"history_failure": "回复已保存，但重新读取记录失败。请重新读取此会话。"}
                cancelled = self._stop.is_set() or request["cancel"].is_set()
                self.store.set_message_status(conversation_id, user_message["id"],
                                              "cancelled" if cancelled else "failed")
                if cancelled:
                    return {"cancelled": True}
                return {"failure": redact_secret(str(exc), key) if isinstance(exc, (ApiError, StoreError))
                        else "操作失败，请检查本地存储、配置或网络后重试。",
                        "configuration_error": isinstance(exc, ApiError) and exc.code in
                        {"authentication", "permission", "model_not_found", "not_configured", "invalid_messages"}}
        def accepts_feedback():
            return (not self._closed and token == self._request_id
                    and conversation_id == self._current_id
                    and conversation_id not in self._invalidating_conversations
                    and conversation_id not in self._deleted_conversations)

        def finished(result, error):
            self._requests.pop((conversation_id, user_message["id"]), None)
            self.retry_availability_changed.emit()
            if token != self._request_id:
                if result and result.get("already_complete"):
                    self._mark_message_status(conversation_id, user_message["id"], "complete")
                if result and result.get("cancelled"):
                    self._mark_message_status(conversation_id, user_message["id"], "cancelled")
                    if (not self._closed and self._error_action == "retry"
                            and self._error_conversation_id == conversation_id
                            and self._error_message_id == user_message["id"] and not self._busy):
                        self._set_error("已停止等待回复；现在可以重试这条消息，也可继续聊天。",
                                        "retry", conversation_id, user_message["id"])
                self._refresh_conversations()
                return
            self._active_request = None
            self._active_request_conversation = None
            self._set_busy(False)
            if result and result.get("already_complete"):
                self._mark_message_status(conversation_id, user_message["id"], "complete")
                self._install_snapshot(conversation_id, result["conversation"], result["messages"])
                if accepts_feedback():
                    self.emotion_requested.emit("neutral")
                self._refresh_conversations()
                return
            error = error or (result or {}).get("failure", "")
            if result and result.get("history_failure"):
                self._mark_message_status(conversation_id, user_message["id"], "complete")
                if accepts_feedback():
                    self._set_error(result["history_failure"], "history", conversation_id)
                    self.emotion_requested.emit("neutral")
                self._refresh_conversations()
                return
            if error:
                failed = deepcopy(user_message)
                failed["metadata"] = dict(failed.get("metadata", {}), request_status="failed")
                self._retry_messages[conversation_id] = failed
                self._recoverable_messages[(conversation_id, user_message["id"])] = failed
                if conversation_id == self._current_id:
                    for item in self._messages:
                        if item.get("id") == user_message["id"]:
                            item["metadata"] = dict(item.get("metadata", {}), request_status="failed")
                    self.messages_changed.emit()
                if accepts_feedback():
                    self._set_error(redact_secret(error, key),
                                    "settings" if (result or {}).get("configuration_error") else "retry",
                                    conversation_id, user_message["id"])
                if accepts_feedback():
                    self.emotion_requested.emit("neutral")
            elif result and not result.get("cancelled"):
                self._recoverable_messages.pop((conversation_id, user_message["id"]), None)
                if self._retry_messages.get(conversation_id, {}).get("id") == user_message["id"]:
                    self._retry_messages.pop(conversation_id, None)
                self._install_snapshot(conversation_id, result["conversation"], result["messages"])
                context = result["context"]
                usage = (f"本次输入估算 {context.estimated_input_tokens} / "
                         f"{budget.total_tokens - budget.output_reserve} Token，输出预留 {context.output_reserve}；"
                         f"未携带 {context.dropped_messages} 条较早消息。")
                if result["memory_warning"]:
                    usage += " " + result["memory_warning"]
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
                self._mark_message_status(conversation_id, user_message["id"], "cancelled")
            self._refresh_conversations()
        self._runner.submit(run, finished)

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
                error = redact_secret(error, key)
                self._set_error(error)
                self._set_status("连接失败：" + error)
            else:
                self._set_status(redact_secret(str(result), key))
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
        content = redact_secret(content, key)
        context_summary = redact_secret(context_summary, key)
        app_name = redact_secret(app_name, key)
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
        self._stop.set()
        self._request_id += 1
        cancel = getattr(self._client, "shutdown", None)
        if callable(cancel):
            cancel()
        for client in list(self._metadata_clients):
            cancel = getattr(client, "shutdown", None)
            if callable(cancel):
                cancel()
        self._runner.shutdown()
        self._set_busy(False)
