"""Asynchronous facade; Qt views do not know Mem0's SDK or vector store."""
import threading
from concurrent.futures import Future
from datetime import datetime
from PySide6.QtCore import QObject, Signal

from .async_tasks import TaskRunner
from .memory.settings import MemorySettings


class LazyMemoryService:
    """Construct even the memory configuration store on a worker thread."""
    def __init__(self, factory):
        self._factory = factory
        self._instance = None
        self._lock = threading.Lock()
        self._closed = False
        self._automatic_pause = threading.Event()

    def _get(self):
        with self._lock:
            if self._closed:
                from .memory.service import MemoryUnavailable
                raise MemoryUnavailable("长期记忆已停止。")
            if self._instance is None:
                instance = self._factory()
                binder = getattr(instance, "bind_automatic_pause", None)
                if callable(binder):
                    binder(self._automatic_pause)
                # Publish only after binding the shared Event. A GUI pause
                # during factory creation is visible before any operation.
                self._instance = instance
                if self._closed:
                    instance.shutdown()
                    from .memory.service import MemoryUnavailable
                    raise MemoryUnavailable("长期记忆已停止。")
            return self._instance

    @property
    def automatic_paused(self):
        return self._automatic_pause.is_set()

    def set_automatic_paused(self, paused):
        # Never acquire the factory lock or instantiate the service on the GUI
        # thread. Both facades observe this same Event, avoiding copied-state
        # races while construction is still in progress.
        if paused:
            self._automatic_pause.set()
        else:
            self._automatic_pause.clear()
        instance = self._instance
        if instance is not None:
            notifier = getattr(instance, "automatic_pause_updated", None)
            if callable(notifier):
                notifier()  # Read the shared Event, never replay a stale bool.

    def __getattr__(self, name):
        return lambda *args, **kwargs: getattr(self._get(), name)(*args, **kwargs)

    def load_state_snapshot(self):
        instance = self._get()
        snapshot = getattr(instance, "load_state_snapshot", None)
        if callable(snapshot):
            return snapshot()
        settings, credentials = instance.load_settings(), instance.credential_status()
        reader = getattr(instance, "configuration_status", None)
        try:
            overview = reader() if callable(reader) else {}
        except AttributeError:
            overview = {}
        return settings, credentials, overview

    def set_automatic_enabled(self, enabled, *, cancelled=None):
        if enabled:
            return self._get().set_automatic_enabled(True, cancelled=cancelled)
        # A previously accepted privacy-enhancing disk write must finish even
        # when exit closes the lazy facade before its queued worker starts.
        # No other operation may construct/use a service after shutdown.
        with self._lock:
            if self._instance is None:
                instance = self._factory()
                binder = getattr(instance, "bind_automatic_pause", None)
                if callable(binder):
                    binder(self._automatic_pause)
                self._instance = instance
                if self._closed:
                    instance.shutdown()
            instance = self._instance
        return instance.set_automatic_enabled(False, cancelled=cancelled)

    def shutdown(self):
        # A factory may be waiting on disk in a worker. Publishing then
        # rechecking _closed in _get handles a late instance without making
        # the GUI wait for that factory's lock.
        self._closed = True
        instance = self._instance
        if instance is not None:
            instance.shutdown()


class MemoryController(QObject):
    settings_changed = Signal(object)
    busy_changed = Signal(bool)
    error_changed = Signal(str)
    status_changed = Signal(str)
    credentials_status_changed = Signal(str)
    memories_changed = Signal()
    automatic_pause_changed = Signal(bool)
    history_loaded = Signal(object)
    memory_update_finished = Signal(str, bool, str)
    settings_saved = Signal()
    readiness_changed = Signal(str)
    setup_state_changed = Signal(str)
    automatic_setup_finished = Signal(bool, str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.settings = MemorySettings()
        self.busy = False
        self.error = ""
        self.status = "正在读取记忆设置；未配置时普通聊天仍可使用。"
        self.credentials_status = "正在读取记忆凭据状态；尚未验证连接。"
        self.memories = []
        self.memory_query = ""
        self.memory_total = 0
        self.memory_matched_total = 0
        self.memory_offset = 0
        self.memory_page_size = 100
        self.memory_has_more = False
        self.memory_preferred_id = None
        self.memory_list_stale = False
        self._pending_refresh = False
        self._configuration_status = {}
        self._embedding_tested_at = ""
        self._extraction_tested_at = ""
        self._local_model_verified = False
        self._embedding_last_failed = False
        self.setup_state = "idle"
        self.setup_error = ""
        self._automatic_setup_cancel = threading.Event()
        self._automatic_requested = None
        self._pending_automatic_disable = False
        self._accepted_disable_write = None
        self.readiness = "正在读取本地配置；尚未验证任何模型连接。"
        paused = getattr(service, "automatic_paused", False)
        self.automatic_paused = paused if type(paused) is bool else False
        self._closed = False
        self._runner = TaskRunner(self)
        self._operate(self._load_state, self._loaded)

    def _operate(self, function, success, failure=None):
        if self._closed or self.busy:
            return False
        self.busy = True
        self.busy_changed.emit(True)
        self.error = self.setup_error
        self.error_changed.emit(self.error)
        def done(result, error):
            self.busy = False
            self.busy_changed.emit(False)
            if error:
                self.error = self.setup_error or error
                self.error_changed.emit(self.error)
                self._status("记忆操作未完成，聊天与会话记录仍可使用。")
                if failure is not None:
                    failure(error)
            else:
                success(result)
            if self._pending_automatic_disable and not self._closed:
                self._pending_automatic_disable = False
                self._change_automatic_enabled(False)
            elif self._pending_refresh and not self._closed:
                self._pending_refresh = False
                self.refresh(self.memory_query, offset=self.memory_offset)
        self._runner.submit(function, done)
        return True

    def _status(self, text):
        self.status = text
        self.status_changed.emit(text)

    def _credentials_status(self, text):
        self.credentials_status = text
        self.credentials_status_changed.emit(text)

    def _loaded(self, result):
        self.settings, credential_status, self._configuration_status = result
        self.settings_changed.emit(self.settings)
        self._credentials_status(credential_status)
        self._status("长期记忆开关已开启；连接尚未验证。" if self.settings.enabled else "长期记忆已关闭。")
        self._update_readiness()

    def _load_state(self):
        snapshot = getattr(self.service, "load_state_snapshot", None)
        if callable(snapshot):
            return snapshot()
        settings, credentials = self.service.load_settings(), self.service.credential_status()
        return settings, credentials, self._read_configuration_overview()

    def _read_configuration_overview(self):
        reader = getattr(self.service, "configuration_status", None)
        # LazyMemoryService defers attribute lookup until this worker runs.
        try:
            overview = reader() if callable(reader) else {}
        except AttributeError:
            overview = {}
        return overview

    def _update_readiness(self):
        overview = self._configuration_status
        def state(key):
            value = overview.get(key)
            return "配置完整" if value is True else "待配置（地址、模型或密钥）" if value is False else "配置未确认"
        chat = "聊天：" + state("chat_configured") + "；本面板未测试聊天连接。"
        if self.settings.embedding_provider == "fastembed":
            embedding = "Embedding：本地模型" + ("文件已验证" if self._local_model_verified else "文件尚未验证，请保存选择后下载 / 检查")
        else:
            embedding = "Embedding：" + state("embedding_configured")
        embedding += "；最近检索失败，请检查错误后重试。" if self._embedding_last_failed else f"；检索已验证 {self._embedding_tested_at}。" if self._embedding_tested_at else "；检索尚未测试。"
        extraction = "提炼：" + ("复用聊天，" if self.settings.use_chat_model else "独立模型，") + state("extraction_configured")
        extraction += f"；最近成功提炼 {self._extraction_tested_at}。" if self._extraction_tested_at else "；本次面板尚无成功提炼记录。"
        self.readiness = "\n".join((chat, embedding, extraction))
        self.readiness_changed.emit(self.readiness)

    def set_automatic_paused(self, paused):
        if self._closed:
            return False
        try:
            self.service.set_automatic_paused(bool(paused))
        except Exception:
            self._status("未能修改本次运行的自动提炼暂停状态；未修改已保存的记忆配置。")
            return False
        self.automatic_paused = bool(paused)
        self.automatic_pause_changed.emit(self.automatic_paused)
        self._status(
            "本次运行已暂停自动提炼，正在进行的自动任务已请求取消；已有记忆仍可检索，已保存的配置未改变。"
            if paused else "本次运行的自动提炼暂停已解除；是否自动提炼仍由已保存的启用与自动提炼设置决定。"
        )
        return True

    @property
    def automatic_enabled(self):
        return bool(self.settings.enabled and self.settings.automatic and not self.automatic_paused)

    @property
    def automatic_requested(self):
        return self._automatic_requested

    def _setup_state(self, state):
        self.setup_state = state
        self.setup_state_changed.emit(state)

    def handle_chat_memory_status(self, text):
        """Keep actionable automatic-memory failures visible outside logs."""
        self._status(str(text))
        if self._closed or not self.settings.enabled:
            return
        failed = ("提炼未完成" in text and self.settings.automatic and self._automatic_requested is not False) or (
            "检索" in text and any(word in text for word in ("失败", "未完成", "不可用"))
        )
        if failed:
            if "重启后可能恢复" not in self.setup_error:
                self.setup_error = "记忆暂时不可用，请重新准备后再试；聊天记录已保留。"
            self.error = self.setup_error
            self.error_changed.emit(self.error)
            self._setup_state("failed")

    def set_automatic_enabled(self, enabled):
        """A persistent switch; an off action always closes the gate at once."""
        if self._closed or type(enabled) is not bool or (
            enabled and (self.busy or self._pending_automatic_disable)
        ):
            return False
        self._automatic_requested = enabled
        if not enabled:
            self._automatic_setup_cancel.set()
        else:
            self._automatic_setup_cancel = threading.Event()
        if not self.set_automatic_paused(True):
            return False
        if self.busy:
            self._pending_automatic_disable = True
            if self._accepted_disable_write is None:
                # Submit the disk write now, rather than waiting for a GUI
                # callback that is deliberately discarded during shutdown.
                accepted = Future()
                self._accepted_disable_write = accepted
                def persist_disabled():
                    try:
                        result = self.service.set_automatic_enabled(False, cancelled=cancelled)
                        accepted.set_result((result, ""))
                    except Exception as exception:
                        error = (str(exception) if type(exception).__module__.startswith("desktop_pet.")
                                 else "关闭自动记忆设置未能保存。")
                        accepted.set_result((None, error))
                cancelled = self._automatic_setup_cancel
                self._runner.submit(persist_disabled, lambda *_args: None)
            self._status("正在关闭自动记忆；已有记忆仍可使用。")
            return True
        return self._change_automatic_enabled(enabled)

    def prepare_automatic_memory(self):
        """Explicit retry; opening a panel never downloads a model."""
        if self._closed or self.busy:
            return False
        if self.settings.enabled and not self.settings.automatic:
            return self._repair_retrieval()
        if self._automatic_requested is False:
            return False
        if not (self.settings.enabled and self.settings.automatic) and not (
            self.setup_state == "failed" and self._automatic_requested is True
        ):
            return False
        return self.set_automatic_enabled(True)

    def _repair_retrieval(self):
        """Repair existing memory without enabling automatic collection."""
        self.setup_error = ""
        self._setup_state("repairing")
        self._status("正在准备已有记忆…")
        def perform():
            settings = self.service.load_settings()
            if settings.embedding_provider == "fastembed":
                self.service.prepare_local_model()
            self.service.search("记忆功能检查", limit=1)
            return self._load_state()
        def done(state):
            self._loaded(state)
            self.setup_error = self.error = ""
            self.error_changed.emit("")
            self._local_model_verified = self.settings.embedding_provider == "fastembed"
            self._setup_state("ready")
            self._status("已有记忆可以继续使用；自动记忆保持关闭。")
            self.automatic_setup_finished.emit(True, "")
        def failed(_error):
            self.setup_error = self.error = "记忆暂时不可用，请稍后重新准备；聊天仍可正常使用。"
            self.error_changed.emit(self.error)
            self._setup_state("failed")
            self.automatic_setup_finished.emit(False, self.error)
        return self._operate(perform, done, failed)

    def _change_automatic_enabled(self, enabled):
        cancelled = self._automatic_setup_cancel
        accepted = self._accepted_disable_write if not enabled else None
        if accepted is not None:
            self._accepted_disable_write = None
        if enabled:
            self.setup_error = ""
            self._setup_state("preparing")
            self._status("正在准备记忆功能，首次使用可能需要下载文件…")
        def perform():
            result, error = None, ""
            try:
                if accepted is not None:
                    result, error = accepted.result()
                else:
                    result = self.service.set_automatic_enabled(enabled, cancelled=cancelled)
            except Exception as exception:
                error = (str(exception) if type(exception).__module__.startswith("desktop_pet.")
                         else "记忆暂时无法准备，请稍后重试；聊天仍可正常使用。")
            try:
                state = self._load_state()
            except Exception:
                state = None
                error = error or "无法读取记忆状态，请稍后重试；聊天仍可正常使用。"
            return state, result, error
        def done(payload):
            state, result, error = payload
            if state is not None:
                self._loaded(state)
            # A later off request owns the gate, even when an earlier enable
            # finished committing just before cancellation reached its worker.
            superseded = enabled and (cancelled.is_set() or self._automatic_requested is False)
            if superseded:
                self._setup_state("idle")
                return
            if error:
                self._setup_state("failed")
                if not enabled:
                    error = "本次运行已停止自动记忆，但关闭设置未能保存；重启后可能恢复，请重试。"
                self.setup_error = error
                self.error = error
                self.error_changed.emit(error)
                self._status("记忆设置未完成，原有记忆和聊天记录已保留。")
                self.automatic_setup_finished.emit(False, error)
                return
            if enabled:
                if not self.set_automatic_paused(False):
                    self.setup_error = "自动记忆未能开启，请重试。"
                    self.error = self.setup_error
                    self.error_changed.emit(self.error)
                    self._setup_state("failed")
                    self.automatic_setup_finished.emit(False, "自动记忆未能开启，请重试。")
                    return
                self._local_model_verified = bool(isinstance(result, dict) and result.get("prepared"))
                self._setup_state("ready")
                self._status("自动记忆已开启。")
            else:
                self._setup_state("idle")
                self._status("自动记忆已关闭；已有记忆仍可使用。")
            self.setup_error = self.error = ""
            self.error_changed.emit("")
            self.settings_saved.emit()
            self._update_readiness()
            self.automatic_setup_finished.emit(True, "")
        return self._operate(perform, done)

    def save_settings(self, settings, llm_key="", embedding_key=""):
        def perform():
            self.service.save_settings(settings, llm_key=llm_key, embedding_key=embedding_key)
            return self._load_state()
        def done(result):
            self._embedding_tested_at = self._extraction_tested_at = ""
            self._local_model_verified = False
            self._embedding_last_failed = False
            self._loaded(result)
            self.settings_saved.emit()
        return self._operate(perform, done)

    def _read_page(self, query, offset=0):
        reader = getattr(self.service, "list_memories_page", None)
        if callable(reader):
            result = reader(query, offset=offset, page_size=self.memory_page_size)
            if isinstance(result, list):
                # Older host/helper implementations may still return their
                # complete list. Normalize at this facade boundary.
                result = {"results": result, "total": len(result), "matched_total": len(result),
                          "offset": 0, "has_more": False}
            elif offset and result["matched_total"] <= offset:
                offset = max(0, ((result["matched_total"] - 1) // self.memory_page_size) * self.memory_page_size)
                result = reader(query, offset=offset, page_size=self.memory_page_size)
        else:
            # Compatibility for lightweight service implementations used by hosts.
            rows = self.service.list_memories(query)
            result = {"results": rows, "total": len(rows), "matched_total": len(rows), "offset": 0, "has_more": False}
        result["configuration_status"] = self._read_configuration_overview()
        return result

    def _show_page(self, result, query, *, preferred_id=None):
        self.memories = result["results"]
        self.memory_query = query
        self.memory_total = result["total"]
        self.memory_matched_total = result["matched_total"]
        self.memory_offset = result["offset"]
        self.memory_has_more = result["has_more"]
        self.memory_preferred_id = preferred_id
        self.memory_list_stale = bool(result.get("stale", False))
        self._configuration_status = result.get("configuration_status", self._configuration_status)
        self._update_readiness()
        self.memories_changed.emit()

    def refresh(self, query="", *, offset=0):
        captured_query = str(query).strip()[:4000]
        def done(result):
            self._show_page(result, captured_query)
            self._status(f"已从本地读取 {len(self.memories)} 条长期记忆；当前用户共 {self.memory_total} 条，匹配 {self.memory_matched_total} 条。此次操作未验证 Embedding 或提炼连接。")
        return self._operate(lambda: self._read_page(captured_query, max(0, int(offset))), done)

    def notify_memories_saved(self):
        """Connect a successful chat extraction signal here on the GUI thread."""
        if self._closed:
            return False
        self._extraction_tested_at = datetime.now().strftime("%H:%M:%S")
        self._update_readiness()
        if self.busy:
            self._pending_refresh = True
            return True
        return self.refresh(self.memory_query, offset=self.memory_offset)

    def next_page(self):
        if not self.memory_has_more:
            return False
        return self.refresh(self.memory_query, offset=self.memory_offset + self.memory_page_size)

    def previous_page(self):
        if self.memory_offset <= 0:
            return False
        return self.refresh(self.memory_query, offset=max(0, self.memory_offset - self.memory_page_size))

    def update_memory(self, memory_id, text, expected_text=None):
        captured_query, offset = self.memory_query, self.memory_offset
        def perform():
            self.service.update(memory_id, text, expected_text=expected_text)
            try:
                return self._read_page(captured_query, offset), ""
            except Exception as error:
                message = str(error) if type(error).__module__.startswith("desktop_pet.") else "列表暂时无法刷新，请手动刷新重试。"
                # The SDK has already verified its write. A separate listing
                # failure must not be reported as an unsaved correction.
                return None, message
        def done(result):
            page, refresh_error = result
            if page is not None:
                self._show_page(page, captured_query)
                self._status("记忆已通过 Mem0 更新；旧值和更正时间保存在 Mem0 历史中，聊天仅使用当前有效版本。")
            else:
                self.error = refresh_error
                self.error_changed.emit(refresh_error)
                self._status("记忆纠正已经保存；列表刷新未完成，请手动刷新重试。")
            self.memory_update_finished.emit(str(memory_id), True, "")
        return self._operate(perform, done, lambda error: self.memory_update_finished.emit(str(memory_id), False, error))

    def load_history(self, memory_id):
        return self._operate(lambda: self.service.history(memory_id), self.history_loaded.emit)

    def delete_memory(self, memory_id):
        captured_query, offset = self.memory_query, self.memory_offset
        previous_rows = [dict(row) for row in self.memories]
        deleted_index = next((index for index, row in enumerate(previous_rows) if row.get("id") == memory_id), None)
        remaining_rows = [row for row in previous_rows if row.get("id") != memory_id]
        cached_total = max(0, self.memory_total - 1)
        cached_matched = max(0, self.memory_matched_total - (deleted_index is not None))
        fallback = {"results": remaining_rows, "total": cached_total, "matched_total": cached_matched,
                    "offset": offset, "has_more": offset + len(remaining_rows) < cached_matched, "stale": True}
        def done(result):
            page, refresh_error = result
            preferred_id = None
            if deleted_index is not None and page["results"]:
                # Keep the closest surviving global position, including when
                # deleting the last page's sole row moves us back one page.
                position = offset + deleted_index - page["offset"]
                position = max(0, min(position, len(page["results"]) - 1))
                preferred_id = page["results"][position]["id"]
            self._show_page(page, captured_query, preferred_id=preferred_id)
            if refresh_error:
                self.error = refresh_error
                self.error_changed.emit(refresh_error)
                self._status("记忆已经删除；列表刷新未完成。已隐藏删除项，当前显示缓存，请刷新重试。")
            else:
                self._status("记忆已从 Mem0 实际删除。")
        def perform():
            self.service.delete(memory_id)
            try:
                return self._read_page(captured_query, offset), ""
            except Exception as error:
                message = str(error) if type(error).__module__.startswith("desktop_pet.") else "列表暂时无法刷新，请手动刷新重试。"
                # Only a successful SDK delete reaches this path. Keep that
                # confirmed result separate from a later local-read failure.
                return fallback, message
        return self._operate(perform, done)

    def clear_memories(self):
        def done(result):
            self.memories = []
            self.memory_total = self.memory_matched_total = self.memory_offset = 0
            self.memory_has_more = False
            self.memory_preferred_id = None
            self.memory_list_stale = False
            self.memories_changed.emit()
            status = "当前用户的长期记忆已实际清空；聊天历史保留。"
            if isinstance(result, dict) and result.get("remaining_other_users"):
                status += "本机向量库仍有其他用户数据，原 Embedding 身份保护继续生效。"
            self._status(status)
        return self._operate(self.service.clear, done)

    def clear_llm_key(self):
        def perform():
            self.service.clear_llm_key()
            return self.service.credential_status()
        def done(result):
            self._credentials_status(result)
            if not self.settings.use_chat_model:
                self._configuration_status["extraction_configured"] = False
                self._extraction_tested_at = ""
            self._update_readiness()
            self._status("独立记忆模型密钥已从系统凭据删除。")
        return self._operate(perform, done)

    def clear_embedding_key(self):
        def perform():
            self.service.clear_embedding_key()
            return self.service.credential_status()
        def done(result):
            self._credentials_status(result)
            self._configuration_status["embedding_configured"] = False
            self._embedding_tested_at = ""
            self._update_readiness()
            self._status("Embedding 密钥已从系统凭据删除。")
        return self._operate(perform, done)

    def test_connection(self):
        def done(result):
            self._embedding_last_failed = False
            self._embedding_tested_at = datetime.now().strftime("%H:%M:%S")
            self._update_readiness()
            self._status("记忆检索请求成功，Embedding 与本地向量存储可用；模型提炼需实际提炼验证。")
        def perform():
            if not self.service.load_settings().enabled:
                from .memory.service import MemoryUnavailable
                raise MemoryUnavailable("请先启用并保存记忆设置，再测试连接。")
            return self.service.search("记忆连接测试", limit=1)
        def failed(_error):
            self._embedding_last_failed = True
            self._update_readiness()
        return self._operate(perform, done, failed)

    def prepare_local_model(self):
        def done(result):
            self._local_model_verified = True
            self._update_readiness()
            self._status(
                f"本地模型已就绪：{result['model']}（{result['dimensions']} 维）。"
                "仅准备了模型文件，记忆开关和自动提炼设置保持原样。"
            )
        return self._operate(self.service.prepare_local_model, done)

    def shutdown(self):
        self._closed = True
        self._automatic_setup_cancel.set()
        self._runner.shutdown()
        self.service.shutdown()
