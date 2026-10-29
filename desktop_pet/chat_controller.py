"""In-memory chat sessions and Qt workers, independent of all windows."""

import threading

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from .api_client import ApiClient, ApiError, ChatReply, EMOTIONS, MAX_REPLY_CHARACTERS, redact_secret
from .settings_manager import ApiSettings, CredentialsError, SettingsError, SettingsManager


MAX_INPUT_CHARACTERS = 8_000
MAX_VISIBLE_MESSAGES = 50
MAX_CONTEXT_TURNS = 12
MAX_CONTEXT_CHARACTERS = 48_000


class _WorkerSignals(QObject):
    finished = Signal(int, str, object, str)


class _RequestWorker(QRunnable):
    def __init__(self, request_id: int, kind: str, client: ApiClient,
                 settings: ApiSettings, key: str, messages: list[dict]):
        super().__init__()
        self.signals = _WorkerSignals()
        self._request_id = request_id
        self._kind = kind
        self._client = client
        self._settings = settings
        self._key = key
        self._messages = messages
        self.cancelled = threading.Event()

    @Slot()
    def run(self) -> None:
        result = None
        error = ""
        try:
            if self.cancelled.is_set():
                return
            if self._kind == "test":
                result = self._client.test_connection(self._settings, self._key)
                result = redact_secret(str(result), self._key)
            else:
                reply = self._client.complete(self._settings, self._key, self._messages)
                if not isinstance(reply, ChatReply) or not isinstance(reply.reply, str) or not reply.reply.strip():
                    raise ApiError("invalid_response", "服务商没有返回可显示的文本回复。")
                result = ChatReply(
                    redact_secret(reply.reply, self._key)[:MAX_REPLY_CHARACTERS],
                    reply.emotion if reply.emotion in EMOTIONS else "neutral",
                )
        except (ApiError, SettingsError, CredentialsError) as exc:
            error = redact_secret(str(exc), self._key)
        except Exception:
            # Never forward a raw traceback/provider exception to the interface.
            error = "请求失败，请检查 API 配置和网络后重试。"
        finally:
            self._key = ""
            self._messages = []
        if not self.cancelled.is_set():
            self.signals.finished.emit(self._request_id, self._kind, result, error)


class ChatController(QObject):
    messages_changed = Signal()
    busy_changed = Signal(bool)
    error_changed = Signal(str)
    emotion_requested = Signal(str)
    connection_status_changed = Signal(str)

    def __init__(self, settings_manager: SettingsManager, api_client: ApiClient | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._settings_manager = settings_manager
        self._client = api_client if api_client is not None else ApiClient()
        self._messages: list[dict[str, str]] = []
        self._completed_turns: list[tuple[dict[str, str], dict[str, str]]] = []
        self._pending_user: dict[str, str] | None = None
        self._busy = False
        self._closed = False
        self._error = ""
        self._connection_status = "尚未测试连接。"
        self._request_id = 0
        self._worker: _RequestWorker | None = None

    @property
    def messages(self) -> list[dict[str, str]]:
        return [dict(message) for message in self._messages]

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def error(self) -> str:
        return self._error

    @property
    def connection_status(self) -> str:
        return self._connection_status

    def _set_error(self, message: str) -> None:
        self._error = message
        self.error_changed.emit(message)

    def _set_status(self, message: str) -> None:
        self._connection_status = message
        self.connection_status_changed.emit(message)

    def _set_busy(self, busy: bool) -> None:
        if self._busy != busy:
            self._busy = busy
            self.busy_changed.emit(busy)

    def _validate_configuration(self, settings: ApiSettings, key: str | None) -> bool:
        try:
            settings.validate(require_model=True)
            if not isinstance(key, str) or not key.strip():
                self._set_error("尚未配置 API Key，请在 API 设置中保存密钥。")
                return False
        except (SettingsError, CredentialsError) as exc:
            self._set_error(redact_secret(str(exc), key or ""))
            return False
        return True

    def send(self, text: str) -> bool:
        if self._closed or self._busy:
            return False
        text = text.strip()
        if not text:
            return False
        if len(text) > MAX_INPUT_CHARACTERS:
            self._set_error(f"单条消息最多 {MAX_INPUT_CHARACTERS} 个字符，请缩短后再发送。")
            return False
        try:
            settings = self._settings_manager.load_api_settings()
            key = self._settings_manager.get_api_key()
        except (SettingsError, CredentialsError) as exc:
            self._set_error(str(exc))
            return False
        if not self._validate_configuration(settings, key):
            return False
        # Keys accidentally pasted by the user must not enter displayed history.
        user_message = {"role": "user", "content": redact_secret(text, key)}
        context = [dict(message) for turn in self._completed_turns for message in turn]
        context.append(dict(user_message))
        self._pending_user = user_message
        self._messages.append(dict(user_message))
        self._messages = self._messages[-MAX_VISIBLE_MESSAGES:]
        self.messages_changed.emit()
        self.emotion_requested.emit("thinking")
        self._start("chat", settings, key, context)
        return True

    def clear(self) -> None:
        # The UI owns the confirmation. Prevent a late completion from restoring
        # an apparently cleared conversation or silently retaining its context.
        if self._busy:
            self._set_error("请等待当前请求结束，再清空聊天记录。")
            return
        self._messages.clear()
        self._completed_turns.clear()
        self._pending_user = None
        self._set_error("")
        self.messages_changed.emit()

    def test_connection(self, settings: ApiSettings, key: str) -> bool:
        if self._closed or self._busy:
            return False
        if not self._validate_configuration(settings, key):
            self._set_status("连接未测试：请先检查配置。")
            return False
        self._set_status("正在发起聊天请求以测试连接……")
        self._start("test", settings, key, [])
        return True

    def _start(self, kind: str, settings: ApiSettings, key: str, messages: list[dict]) -> None:
        self._request_id += 1
        self._set_error("")
        self._set_busy(True)
        worker = _RequestWorker(self._request_id, kind, self._client, settings, key, messages)
        worker.signals.finished.connect(self._finished)
        self._worker = worker
        # Use the global pool so destroying a closed panel/controller never
        # destroys a running QThread or blocks waiting for its private pool.
        QThreadPool.globalInstance().start(worker)

    @Slot(int, str, object, str)
    def _finished(self, request_id: int, kind: str, result: object, error: str) -> None:
        if self._closed or request_id != self._request_id:
            return
        self._worker = None
        self._set_busy(False)
        if error:
            self._set_error(error)
            if kind == "test":
                self._set_status("连接失败：" + error)
            else:
                self._pending_user = None
                self.emotion_requested.emit("neutral")
            return
        if kind == "test":
            self._set_status(str(result))
            return
        if isinstance(result, ChatReply) and self._pending_user is not None:
            assistant = {"role": "assistant", "content": result.reply}
            self._messages.append(assistant)
            self._messages = self._messages[-MAX_VISIBLE_MESSAGES:]
            self._completed_turns.append((self._pending_user, assistant))
            self._completed_turns = self._completed_turns[-MAX_CONTEXT_TURNS:]
            while len(self._completed_turns) > 1 and sum(
                len(message["content"]) for turn in self._completed_turns for message in turn
            ) > MAX_CONTEXT_CHARACTERS:
                self._completed_turns.pop(0)
            self._pending_user = None
            self.messages_changed.emit()
            self.emotion_requested.emit(result.emotion)

    def shutdown(self) -> None:
        """Stop accepting requests and cancel HTTP without blocking the GUI."""
        if self._closed:
            return
        self._closed = True
        self._request_id += 1
        if self._worker is not None:
            self._worker.cancelled.set()
        cancel = getattr(self._client, "shutdown", None)
        if callable(cancel):
            cancel()
        self._pending_user = None
        self._set_busy(False)
