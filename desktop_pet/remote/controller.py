"""Desktop remote service: authenticated RPC and a bounded Qt-thread bridge."""

from collections import OrderedDict, deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
from functools import partial
import base64
import hashlib
import importlib.util
import json
import math
import secrets
import threading
import time
from uuid import UUID, uuid5, NAMESPACE_URL

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from ..conversations import StoreError
from ..chat_images import IMAGE_ID
from ..expression_manager import EXPRESSIONS
from . import PROTOCOL_VERSION
from .attachments import ImageInbox
from .chat_transfer import direct_chat_configuration, normalize_import
from .character_transfer import (character_library_summary, get_character, list_characters,
                                 select_character, validate_character_selection)
from .desktop_capture import DesktopCaptureUnavailable, capture_desktop_image
from .errors import RemoteError
from .settings import PEER_ID, RemotePreferences
from .sync import RemoteSyncStore, SyncError
from ..agents.coordinator import CoordinatorError
from ..image_storage import ImageStorageGate, ImageStorageGateError


CAPABILITIES = ["pet.command", "chat.send", "chat.image", "chat.config", "chat.import", "chat.desktop_context", "agent.send", "agent.queue.v1", "agent.cancel", "agent.answer", "agent.steer", "desktop.snapshot", "sync.snapshot", "sync.pull", "operation.get",
                "image.upload", "image.list", "image.get", "image.delete", "artifact.list", "artifact.get", "context.get",
                "character.list", "character.get", "character.select"]

RETRY_DELAYS_SECONDS = (2, 5, 10, 30, 60)
_TRANSPORT_ERRORS = {
    "dependency_unavailable": "可选通信组件未安装，请在此页安装后重启。",
    "invalid_identity": "远程设备身份无效，请检查系统凭据后重试。",
    "bind_failed": "本机网络监听未能启动，等待网络恢复。",
    "no_usable_address": "尚未检测到可用网络地址，等待网络恢复。",
    "transport_failed": "远程连接已中断，正在自动恢复。",
    "shutdown_pending": "正在等待上一条连接释放资源，随后自动重试。",
}
_RETRYABLE_ERRORS = frozenset(("bind_failed", "no_usable_address", "transport_failed", "shutdown_pending"))
_IMAGE_STORAGE_WRITES = frozenset({"chat.send", "agent.send", "chat.import", "image.upload", "desktop.snapshot", "image.delete"})


class RemoteController(QObject):
    status_changed = Signal(dict)
    pairing_changed = Signal(dict)
    devices_changed = Signal(list)
    image_received = Signal(dict)
    _transport_status = Signal(int, dict)
    _ui_request = Signal(object)
    _chat_imported = Signal(str)

    def __init__(self, session, parent=None, *, transport_factory=None, clock=time.time):
        super().__init__(parent)
        self.session = session
        self.preferences = RemotePreferences(session.settings)
        self.sync = RemoteSyncStore(session.chat.store)
        self.images = ImageInbox(self.preferences.directory / "inbox")
        self.session.chat.set_image_inbox(self.images)
        self._clock = clock
        self._transport_factory = transport_factory
        self._transport = None
        self._generation = 0
        self._closed = False
        self._enabled = self.preferences.enabled
        self._retry_attempt = 0
        self._retry_generation = None
        self._pairing = {}
        self._rate_lock = threading.Lock()
        self._rates = OrderedDict()
        self._futures_lock = threading.Lock()
        self._futures = set()
        self._status = {"state": "disabled", "endpoint_id": "", "ticket": "", "message": "",
                        "dependency_available": self._dependency_available()}
        if self.preferences.load_error:
            self._status.update(state="error", message="远程连接设置损坏，已停止授权；请重新开启并配对设备。")
        self._transport_status.connect(self._accept_status, Qt.ConnectionType.QueuedConnection)
        self._ui_request.connect(self._run_ui_request, Qt.ConnectionType.QueuedConnection)
        self._chat_imported.connect(self._refresh_imported_chat, Qt.ConnectionType.QueuedConnection)
        self._pair_timer = QTimer(self)
        self._pair_timer.setInterval(1000)
        self._pair_timer.timeout.connect(self._expire_pairing)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_automatically)
        binder=getattr(self.session.chat,"bind_remote_agent_authority",None)
        if callable(binder): binder(self._agent_authorization)
        if self._enabled:
            # An owned timer can be cancelled if the user disables the service
            # before startup runs, unlike an anonymous singleShot callback.
            self._status["state"] = "starting"
            self._retry_generation = self._generation
            self._retry_timer.start(0)

    def _dependency_available(self):
        return self._transport_factory is not None or importlib.util.find_spec("iroh") is not None

    @property
    def enabled(self):
        return self._enabled

    @property
    def status(self):
        value = deepcopy(self._status)
        if value.get("auto_retry") and self._retry_timer.isActive():
            value["retry_in_seconds"] = max(0, math.ceil(self._retry_timer.remainingTime() / 1000))
        return value

    @property
    def pairing(self):
        return deepcopy(self._pairing)

    @property
    def devices(self):
        return self.preferences.devices

    def _set_status(self, state, message="", **values):
        self._status = {**self._status, **values, "state": state, "message": message,
                        "dependency_available": self._dependency_available()}
        self.status_changed.emit(self.status)

    def _agent_guard(self):
        getter=getattr(self.session.chat,"agent_authority_guard",None)
        return getter() if callable(getter) else nullcontext()

    def _agent_authorization(self,peer,*,create=False):
        if self._closed or not self._enabled or not self.preferences.enabled: return None
        return self.preferences.agent_authorization(peer,create=create)

    def _agent_authority_changed(self):
        method=getattr(self.session.chat,"remote_agent_authority_changed",None)
        if callable(method): method()

    def set_enabled(self, enabled):
        with self._agent_guard():
            result=self._set_enabled(enabled)
            self._agent_authority_changed()
            return result

    def _set_enabled(self, enabled):
        if type(enabled) is not bool or self._closed:
            return False
        try:
            self.preferences.set_enabled(enabled)
        except RemoteError as exc:
            if not enabled:
                self._enabled = False
                self._stop_transport()
            self._set_status("error", exc.message, error_code=exc.code,
                             auto_retry=False if not enabled else self._status.get("auto_retry", False),
                             retry_in_seconds=None if not enabled else self._status.get("retry_in_seconds"))
            return False
        self._enabled = enabled
        if enabled:
            self.retry()
        else:
            self._stop_transport()
            self._retry_attempt = 0
            self._set_status("disabled", endpoint_id="", ticket="", error_code="", auto_retry=False,
                             retry_in_seconds=None, local_available=False, relay_available=False)
        return True

    def retry(self):
        """Manual retry is immediate; it also replaces a pending startup retry."""
        if self._closed or not self._enabled:
            return
        self._cancel_retry()
        if self._status["state"] == "starting" and self._transport is not None:
            return
        self._retry_attempt = 0
        self._start_connection()

    def _cancel_retry(self):
        self._retry_timer.stop()
        self._retry_generation = None

    def _retry_automatically(self):
        generation, self._retry_generation = self._retry_generation, None
        if self._closed or not self._enabled or generation != self._generation:
            return
        if self._status["state"] == "starting" and self._transport is not None:
            return
        self._start_connection()

    def _connection_error(self, code):
        """Only stable categories enter the UI, never transport exceptions."""
        code = code if isinstance(code, str) and code in _TRANSPORT_ERRORS else "transport_failed"
        self._stop_transport()
        delay = None
        if code in _RETRYABLE_ERRORS and self._enabled and not self._closed:
            delay = RETRY_DELAYS_SECONDS[min(self._retry_attempt, len(RETRY_DELAYS_SECONDS) - 1)]
            self._retry_attempt += 1
            self._retry_generation = self._generation
            self._retry_timer.start(max(1, round(delay * 1000)))
        self._set_status("error", _TRANSPORT_ERRORS[code], endpoint_id="", ticket="", error_code=code,
                         auto_retry=delay is not None,
                         retry_in_seconds=math.ceil(delay) if delay is not None else None,
                         local_available=False, relay_available=False)

    def _start_connection(self):
        if self._closed or not self._enabled:
            return
        self._stop_transport()
        if not self._dependency_available():
            self._connection_error("dependency_unavailable")
            return
        self._set_status("starting", endpoint_id="", ticket="", error_code="", auto_retry=False,
                         retry_in_seconds=None, local_available=False, relay_available=False)
        try:
            secret = self.preferences.identity_secret()
            factory = self._transport_factory
            if factory is None:
                from .transport import IrohTransport
                factory = IrohTransport
            generation = self._generation
            extra = {"is_authorized": self.preferences.authorized} if self._transport_factory is None else {}
            self._transport = factory(secret_hex=secret,
                on_request=lambda peer, request: self.handle_request(peer, request, generation=generation),
                on_status=lambda status: self._transport_status.emit(generation, status), **extra)
            self._transport.start()
        except RemoteError as exc:
            self._stop_transport()
            self._set_status("error", exc.message, error_code=exc.code, auto_retry=False,
                             retry_in_seconds=None, endpoint_id="", ticket="")
        except Exception:
            self._connection_error("transport_failed")

    @Slot(int, dict)
    def _accept_status(self, generation, status):
        if self._closed or not self._enabled or generation != self._generation:
            return
        state = status.get("state")
        if state == "listening":
            self._cancel_retry()
            self._retry_attempt = 0
            self._set_status("listening", endpoint_id=str(status.get("endpoint_id", "")),
                ticket=str(status.get("ticket", "")), connection_type=status.get("connection_type", "unknown"),
                local_available=status.get("local_available") is True,
                relay_available=status.get("relay_available") is True,
                error_code="", auto_retry=False, retry_in_seconds=None)
        elif state in ("error", "stopped"):
            self._connection_error(status.get("error_code", "transport_failed"))
        elif state == "starting":
            self._set_status("starting")

    def _cancel_ui_requests(self):
        with self._futures_lock:
            for future in self._futures:
                future.cancel()
            self._futures.clear()

    def _stop_transport(self):
        self._cancel_retry()
        self._generation += 1
        self.cancel_pairing()
        self._cancel_ui_requests()
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.stop()

    def begin_pairing(self):
        if self._closed or not self._enabled or self._status["state"] != "listening":
            return {}
        self.cancel_pairing()
        self._pairing = {"code": secrets.token_urlsafe(18), "expires_at": self._clock() + 300,
                         "ticket": self._status["ticket"]}
        data = {"version": PROTOCOL_VERSION, "endpoint_id": self._status["endpoint_id"], **self._pairing}
        encoded = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
        self._pairing["uri"] = "aura://pair/" + encoded
        self._pair_timer.start()
        self.pairing_changed.emit(self.pairing)
        return self.pairing

    def cancel_pairing(self):
        self._pair_timer.stop()
        if self._pairing:
            self._pairing = {}
            self.pairing_changed.emit({})

    def _expire_pairing(self):
        if self._pairing and self._clock() >= self._pairing["expires_at"]:
            self.cancel_pairing()

    def revoke_device(self, peer_id):
        with self._agent_guard():
            result=self._revoke_device(peer_id)
            self._agent_authority_changed()
            return result

    def _revoke_device(self, peer_id):
        try:
            self.preferences.revoke(peer_id)
            self.devices_changed.emit(self.devices)
            return True
        except RemoteError as exc:
            self._set_status(self._status["state"], exc.message)
            return False

    def _pair_device(self, peer_id, params):
        if self.preferences.authorized(peer_id):
            return {"paired": True, "device_id": peer_id, "protocol_version": PROTOCOL_VERSION}
        code = params.get("code")
        name = params.get("name", "手机")
        if (not isinstance(code, str) or len(code) > 128 or not self._pairing
                or self._clock() >= self._pairing["expires_at"]
                or not secrets.compare_digest(code.encode("utf-8"), self._pairing["code"].encode("utf-8"))):
            self._expire_pairing()
            raise RemoteError("pairing_failed", "配对信息无效或已过期，请在电脑端重新生成。")
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
                or any(ord(c) < 32 for c in name)):
            raise RemoteError("invalid_request", "设备名称无效。")
        self.preferences.add_device(peer_id, name.strip(), datetime.now(timezone.utc).isoformat())
        self.cancel_pairing()
        self.devices_changed.emit(self.devices)
        return {"paired": True, "device_id": peer_id, "protocol_version": PROTOCOL_VERSION}

    def _check_rate(self, peer_id, method):
        now = time.monotonic()
        key = (peer_id, "image" if method == "image.upload" else "requests")
        with self._rate_lock:
            events = self._rates.pop(key, deque())
            while events and events[0] <= now - 60:
                events.popleft()
            if len(events) >= (8 if method == "image.upload" else 120):
                self._rates[key] = events
                raise RemoteError("rate_limited", "请求过于频繁，请稍后重试。")
            events.append(now)
            self._rates[key] = events
            while len(self._rates) > 256:
                self._rates.popitem(last=False)

    def _require_authorized(self, peer_id, generation=None):
        if self._closed or not self._enabled or generation is not None and generation != self._generation:
            raise RemoteError("disabled", "远程连接已关闭。")
        if not self.preferences.authorized(peer_id):
            raise RemoteError("unauthorized", "设备尚未配对或授权已撤销。")

    def _call_ui(self, operation, peer_id=None, *, generation=None):
        generation = self._generation if generation is None else generation
        if QThread.currentThread() == self.thread():
            if peer_id is not None:
                self._require_authorized(peer_id, generation)
            return operation()
        future = Future()
        with self._futures_lock:
            self._futures.add(future)
        try:
            self._ui_request.emit((future, generation, peer_id, operation))
            return future.result(timeout=5)
        except FutureTimeout:
            future.cancel()
            raise RemoteError("busy", "电脑暂时忙碌，请稍后重试。") from None
        finally:
            with self._futures_lock:
                self._futures.discard(future)

    @Slot(object)
    def _run_ui_request(self, item):
        future, generation, peer_id, operation = item
        if future.cancelled():
            return
        if self._closed or not self._enabled or generation != self._generation:
            future.cancel()
            return
        if not future.set_running_or_notify_cancel():
            return
        try:
            if peer_id is not None:
                self._require_authorized(peer_id, generation)
            future.set_result(operation())
        except Exception as exc:
            future.set_exception(exc)

    def handle_request(self, peer_id, request, *, generation=None):
        request_id = request.get("id") if isinstance(request, dict) else None
        generation = self._generation if generation is None else generation
        try:
            if (not isinstance(peer_id, str) or not PEER_ID.fullmatch(peer_id)
                    or not isinstance(request, dict) or not isinstance(request_id, str)
                    or str(UUID(request_id)) != request_id
                    or not isinstance(request.get("method"), str)
                    or not isinstance(request.get("params", {}), dict)):
                raise RemoteError("invalid_request", "请求格式无效。")
            if self._closed or not self._enabled or generation != self._generation:
                raise RemoteError("disabled", "远程连接已关闭。")
            method, params = request["method"], request.get("params", {})
            self._check_rate(peer_id, method)
            if method == "pair":
                result = self._call_ui(lambda: self._pair_device(peer_id, params), generation=generation)
            else:
                self._require_authorized(peer_id, generation)
                result = self._dispatch(peer_id, request_id, method, params, generation)
                self._require_authorized(peer_id, generation)
            return {"id": request_id, "ok": True, "result": result}
        except RemoteError as exc:
            return {"id": request_id, "ok": False, "error": {"code": exc.code, "message": exc.message}}
        except SyncError as exc:
            return {"id": request_id, "ok": False, "error": {
                "code": getattr(exc, "code", "sync_error"), "message": str(exc)}}
        except StoreError:
            return {"id": request_id, "ok": False, "error": {
                "code": "storage_error", "message": "无法访问电脑端聊天记录，请检查本地存储后重试。"}}
        except (ValueError, TypeError, KeyError):
            return {"id": request_id, "ok": False, "error": {
                "code": "invalid_request", "message": "请求参数无效。"}}
        except Exception:
            # Neither provider exceptions nor filesystem/vault paths reach peers.
            return {"id": request_id, "ok": False, "error": {
                "code": "operation_failed", "message": "操作未完成，请检查电脑端状态后重试。"}}

    def _dispatch(self, peer_id, request_id, method, params, generation):
        if method not in _IMAGE_STORAGE_WRITES:
            return self._dispatch_operation(peer_id, request_id, method, params, generation)
        self._require_image_storage_host_open()
        gate = getattr(self.session.chat, "image_storage_gate", None)
        mutated = getattr(self.session.chat, "mark_image_storage_mutated", None)
        if not isinstance(gate, ImageStorageGate) or not callable(mutated):
            raise RemoteError("busy", "图片存储状态暂不可用，请稍后重试。")
        owner = gate.create_owner()
        token = None
        try:
            token = gate.maintenance(owner) if method == "image.delete" else gate.activity(owner)
            self._notify_image_storage_changed()
            ui_call = self._call_ui if method == "image.delete" else partial(self._call_image_storage_ui, gate, owner)
            return gate.run(token, lambda: self._dispatch_operation(
                peer_id, request_id, method, params, generation, ui_call=ui_call))
        except ImageStorageGateError:
            raise RemoteError("busy", "图片存储正在维护或使用中，请稍后重试。") from None
        finally:
            if token is not None:
                gate.release(token)
            gate.close_owner(owner)
            self._notify_image_storage_changed()

    def _require_image_storage_host_open(self):
        if getattr(self.session.chat, "_closed", True) is not False:
            raise RemoteError("disabled", "电脑端聊天已关闭，请重新启动后再试。")

    def _notify_image_storage_changed(self):
        notify = getattr(self.session.chat, "notify_image_storage_changed", None)
        if callable(notify):
            try:
                notify()
            except Exception:
                pass  # Display delivery cannot retire work or replace its result.

    def _call_image_storage_ui(self, gate, owner, operation, peer_id=None, *, generation=None):
        inner = gate.activity(owner)

        def admitted_operation():
            self._require_image_storage_host_open()
            return operation()

        def run_ui():
            try:
                return gate.run(inner, admitted_operation)
            finally:
                self._notify_image_storage_changed()

        try:
            return self._call_ui(run_ui, peer_id, generation=generation)
        finally:
            # A timed-out Future can already be running on Qt. Only its real
            # run_ui finally may remove that activity; release just discards it.
            gate.release(inner)
            self._notify_image_storage_changed()

    def _check_image_storage_delete(self):
        self._require_image_storage_host_open()
        chat = self.session.chat
        service_query = getattr(getattr(chat, "_agent_service", None), "execution_resources_pending", None)
        queue = getattr(getattr(chat, "_agent_queue", None), "queue", None)
        queue_query = getattr(queue, "has_image_maintenance_blocker", None)
        deleted = getattr(chat, "mark_image_storage_deleted", None)
        mutated = getattr(chat, "mark_image_storage_mutated", None)
        try:
            clear = (callable(service_query) and callable(queue_query) and callable(deleted) and callable(mutated)
                     and service_query() is False and queue_query() is False)
        except Exception:
            clear = False
        if not clear:
            raise RemoteError("busy", "电脑任务仍在运行或退出状态尚未确认，请稍后管理图片。")

    def _dispatch_operation(self, peer_id, request_id, method, params, generation, *, ui_call=None):
        ui_call = self._call_ui if ui_call is None else ui_call
        if method in _IMAGE_STORAGE_WRITES:
            self._require_image_storage_host_open()
            self._require_authorized(peer_id, generation)
        if method == "image.delete":
            self._check_image_storage_delete()
        if method == "context.get":
            if set(params) - {"conversation_id", "generation", "query"}:
                raise RemoteError("invalid_request", "上下文同步参数无效。")
            cid = params.get("conversation_id")
            if not isinstance(cid, str) or str(UUID(cid)) != cid:
                raise RemoteError("invalid_request", "上下文同步需要有效的会话标识。")
            expected = params.get("generation")
            if expected is not None and (not isinstance(expected, str) or str(UUID(expected)) != expected):
                raise RemoteError("invalid_request", "会话版本无效。")
            query = params.get("query", "")
            if not isinstance(query, str) or len(query) > 4000:
                raise RemoteError("invalid_request", "上下文查询过长。")
            self._require_authorized(peer_id, generation)
            policy = ui_call(self.session.chat.context_export_policy, peer_id, generation=generation)
            result = self.session.chat.context_envelope_for(cid, generation=expected, query=query, policy=policy)
            self._require_authorized(peer_id, generation)
            return result
        if method in {"artifact.list", "artifact.get"}:
            cid = params.get("conversation_id")
            try:
                if not isinstance(cid, str) or str(UUID(cid)) != cid:
                    raise ValueError
            except ValueError:
                raise RemoteError("invalid_request", "交付文件需要有效的会话标识。") from None
            self._require_authorized(peer_id, generation)
            if method == "artifact.list":
                if set(params) != {"conversation_id"}:
                    raise RemoteError("invalid_request", "交付文件列表只接受会话标识。")
                result = {"artifacts": self.session.chat.agent_artifacts_for(cid)}
            else:
                if set(params) - {"conversation_id", "reference", "offset", "limit"} or "reference" not in params:
                    raise RemoteError("invalid_request", "读取交付文件只接受当前会话、文件标识和分块位置。")
                try:
                    result = self.session.chat.agent_artifact_page(cid, params["reference"],
                        offset=params.get("offset", 0), limit=params.get("limit", 262144))
                except (ValueError, OSError):
                    raise RemoteError("artifact_unavailable", "交付文件已变化、不可用或不属于此会话。") from None
            self._require_authorized(peer_id, generation)
            return result
        if method == "status":
            return ui_call(lambda: {"protocol_version": PROTOCOL_VERSION,
                "agent_trace_version": 1,
                "state": self._status["state"], "chat_busy": self.session.chat.busy,
                "current_conversation_id": self.session.chat.current_conversation_id,
                "capabilities": CAPABILITIES, "desktop_context": self._desktop_readiness(),
                **self._agent_status()}, peer_id, generation=generation)
        if method == "chat.config":
            if params:
                raise RemoteError("invalid_request", "配置同步不接受附加参数。")
            # Never reserve/log this response: it deliberately contains the
            # credential, only within an authenticated encrypted connection.
            def configuration():
                result = direct_chat_configuration(self.session.settings)
                try:
                    result["character_library"] = character_library_summary(self.session.settings)
                except RemoteError:
                    pass  # Keep the prior valid phone role when the shelf is unreadable.
                return result
            return ui_call(configuration, peer_id, generation=generation)
        if method == "character.list":
            return ui_call(lambda: list_characters(self.session.settings, params), peer_id,
                           generation=generation)
        if method == "character.get":
            return ui_call(lambda: get_character(self.session.settings, params), peer_id,
                           generation=generation)
        if method == "chat.import":
            normalized = normalize_import(params)
            ui_call(lambda: self._check_chat_import(normalized["conversation_id"]), peer_id,
                          generation=generation)
            image_id = normalized["messages"][0].get("image_id")
            if image_id is not None:
                self.images.metadata(image_id)
            self._require_authorized(peer_id, generation)
            self._require_image_storage_host_open()
            with self._agent_guard():
                self._require_authorized(peer_id, generation)
                self._require_image_storage_host_open()
                result = self.sync.import_exchange(peer_id, request_id, normalized)
            self._chat_imported.emit(normalized["conversation_id"])
            return result
        if method == "sync.snapshot":
            return self.sync.snapshot(limit=params.get("limit", 100), cursor=params.get("cursor", ""))
        if method == "sync.pull":
            return self.sync.pull(since=params.get("since", 0), limit=params.get("limit", 100))
        if method == "operation.get":
            operation_id = params.get("request_id")
            if not isinstance(operation_id, str) or str(UUID(operation_id)) != operation_id:
                raise RemoteError("invalid_request", "操作标识无效。")
            return self.sync.get_operation(peer_id, operation_id) or {"state": "not_found"}
        if method == "image.list":
            return self.images.list(params.get("offset", 0), params.get("limit", 50))
        if method in ("image.get", "image.delete"):
            image_id = params.get("id")
            if not isinstance(image_id, str) or not PEER_ID.fullmatch(image_id):
                raise RemoteError("invalid_request", "图片标识无效。")
            if method == "image.get":
                return self.images.get(image_id)
        if method not in ("pet.command", "chat.send", "agent.send", "agent.cancel", "agent.answer", "agent.steer", "image.upload", "image.delete", "desktop.snapshot", "character.select"):
            raise RemoteError("unknown_method", "不支持此远程操作。")
        if method == "character.select":
            # Only validated IDs/hashes, never a card body or prompt, enter the
            # durable operation log. Read responses stay ephemeral above.
            params = validate_character_selection(params)
        expires_at = None
        if method == "pet.command":
            expires_at = params.get("expires_at")
            if (type(expires_at) not in (int, float) or not math.isfinite(expires_at)
                    or not self._clock() < expires_at <= self._clock() + 60):
                raise RemoteError("expired", "桌宠指令已过期，或未提供 60 秒内的有效期。")
        # Durable request IDs prevent replay after lost replies and reconnects.
        operation_params = params
        if method == "image.upload":
            encoded = params.get("data_base64")
            if not isinstance(encoded, str):
                raise RemoteError("invalid_image", "图片编码无效。")
            operation_params = {key: params[key] for key in ("filename", "mime_type", "sha256") if key in params}
            operation_params["encoded_sha256"] = hashlib.sha256(encoded.encode("ascii")).hexdigest()
            operation_params["encoded_length"] = len(encoded)
        reservation = self.sync.reserve_operation(peer_id, request_id,
            {"method": method, "params": operation_params}, expires_at=expires_at, ephemeral=method == "pet.command")
        if not reservation["accepted"]:
            if method=="agent.send":
                recover=getattr(self.session.chat,"recover_remote_agent_ack",None)
                if callable(recover):
                    ack=ui_call(lambda:recover(peer_id,request_id),peer_id,generation=generation)
                    if ack is not None: return {"duplicate":True,**ack}
            return {"duplicate": True, **reservation}
        try:
            if method == "pet.command":
                result = ui_call(lambda: self._pet_command(params), peer_id, generation=generation)
            elif method == "character.select":
                result = ui_call(lambda: select_character(self.session.settings, self.session.chat, params),
                                 peer_id, generation=generation)
            elif method == "agent.cancel":
                result = ui_call(lambda: self._cancel_agent(params), peer_id, generation=generation)
            elif method == "agent.answer":
                result = ui_call(lambda: self._answer_agent(params), peer_id, generation=generation)
            elif method == "agent.steer":
                result = ui_call(lambda: self._steer_agent(params), peer_id, generation=generation)
            elif method == "agent.send":
                if not {"conversation_id", "text"} <= set(params) or set(params) - {"conversation_id", "text", "image_id"}:
                    raise RemoteError("invalid_request", "电脑 Agent 请求只接受会话、文字和本次图片。")
                if "image_id" in params:
                    image_id = params["image_id"]
                    if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
                        raise RemoteError("invalid_image", "图片标识无效，请先上传图片再发送。")
                    self.images.metadata(image_id)
                result = ui_call(lambda: self._send_agent(peer_id, request_id, params), peer_id,
                                       generation=generation)
            elif method == "chat.send":
                if any(name in params for name in ("desktop_image_id", "desktop_capture_unavailable_reason")):
                    raise RemoteError("invalid_request", "电脑画面状态由电脑端确认，不能由远程请求指定。")
                if "desktop_context" in params and type(params["desktop_context"]) is not bool:
                    raise RemoteError("invalid_request", "电脑协助选项必须为布尔值。")
                if params.get("desktop_context") and "image_id" in params:
                    raise RemoteError("invalid_request", "电脑协助与图片附件不能同时发送。")
                desktop_image_id = None
                desktop_capture_unavailable_reason = None
                if params.get("desktop_context"):
                    ui_call(lambda: self._validate_chat_params(params), peer_id, generation=generation)
                    try:
                        desktop_image_id = self._capture_desktop(peer_id, generation, ui_call=ui_call)["id"]
                    except DesktopCaptureUnavailable as exc:
                        if exc.blocked_code != "own_window":
                            raise
                        # AURA intentionally never photographs its own chat or
                        # settings. An explicit task can still reach the normal
                        # current-user routing gate, without invented pixels.
                        ui_call(self._desktop_chat_fallback_permission, peer_id, generation=generation)
                        desktop_capture_unavailable_reason = "own_window"
                if "image_id" in params:
                    image_id = params["image_id"]
                    if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
                        raise RemoteError("invalid_image", "图片标识无效，请先上传图片再发送。")
                    self.images.metadata(image_id)
                result = ui_call(lambda: self._send_chat(peer_id, request_id, params, desktop_image_id,
                    desktop_capture_unavailable_reason), peer_id,
                                       generation=generation)
            elif method == "desktop.snapshot":
                if params:
                    raise RemoteError("invalid_request", "获取电脑画面不接受附加参数。")
                result = self._capture_desktop(peer_id, generation, ui_call=ui_call)
            elif method == "image.upload":
                self._require_authorized(peer_id, generation)
                self._require_image_storage_host_open()
                result = self.images.upload(params)
                self.session.chat.mark_image_storage_mutated(result["id"])
                self.image_received.emit(result)
            else:
                self._require_authorized(peer_id, generation)
                self._check_image_storage_delete()
                failed = True
                try:
                    result = self.images.delete(params["id"])
                    failed = False
                finally:
                    try:
                        self.session.chat.mark_image_storage_deleted(params["id"])
                    except BaseException:
                        if not failed:
                            raise
            self.sync.complete_operation(peer_id, request_id, {"ok": True, "result": result})
            return result
        except Exception as exc:
            if method=="agent.send":
                recover=getattr(self.session.chat,"recover_remote_agent_ack",None)
                if callable(recover):
                    ack=ui_call(lambda:recover(peer_id,request_id),peer_id,generation=generation)
                    if ack is not None:
                        # The exact queue enrollment is durable even if the
                        # separate operation-result write was interrupted.
                        return ack
            error = ({"code": exc.code, "message": exc.message} if isinstance(exc, RemoteError) else {
                "code": "operation_failed", "message": "该操作未完成，请确认电脑状态后使用新的请求标识。"})
            self.sync.complete_operation(peer_id, request_id, {"ok": False, "error": error})
            raise

    def _pet_command(self, params):
        # This method executes only on the application thread, after rechecking authorization.
        if self._clock() >= params["expires_at"]:
            raise RemoteError("expired", "桌宠指令已过期。")
        action = params.get("action")
        controller = self.session.pet_controller
        if action == "expression":
            value = params.get("value")
            if not isinstance(value, str) or value not in EXPRESSIONS:
                raise RemoteError("invalid_request", "表情名称无效。")
            controller.set_expression(value, feedback=False)
        elif action == "scale":
            value = params.get("value")
            if type(value) is not int or not 50 <= value <= 200:
                raise RemoteError("invalid_request", "大小必须为 50～200 的整数百分比。")
            controller.set_scale(value)
        elif action == "animation":
            value = params.get("value")
            if not isinstance(value, str) or value not in {item[0] for item in controller.animation_catalog()}:
                raise RemoteError("invalid_request", "动作名称无效。")
            if not controller.play_asset_animation(value):
                raise RemoteError("busy", "当前暂时无法播放此动作。")
        elif action == "visibility":
            value = params.get("value")
            if type(value) is not bool or not value and not self.session.tray.available:
                raise RemoteError("invalid_request", "当前无法执行此显示操作。")
            self.session.pet.setVisible(value)
            self.session.tray.refresh()
        elif action == "say":
            value = params.get("value")
            if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                raise RemoteError("invalid_request", "气泡文字须为 1～1000 字。")
            controller.respond(value.strip())
        else:
            raise RemoteError("invalid_request", "不支持此桌宠指令。")
        return {"applied": True, "action": action}

    def _check_chat_import(self, conversation_id):
        state = self.session.chat.external_import_state(conversation_id)
        if state == "conversation_deleted":
            raise RemoteError(state, "会话或消息正在删除，不能通过回写恢复。")
        if state != "ready":
            raise RemoteError("busy", "电脑正在保存此会话，请稍后回写。")

    @Slot(str)
    def _refresh_imported_chat(self, conversation_id):
        if not self._closed:
            self.session.chat.refresh_external_import(conversation_id)

    def _desktop_capture_permission(self):
        perception = getattr(self.session.chat, "perception_controller", None)
        if perception is None:
            perception = getattr(self.session, "perception", None)
        settings = getattr(perception, "settings", None)
        if (perception is None or getattr(perception, "source_kind", "screen") != "screen"
                or getattr(perception, "_closed", False) or getattr(perception, "paused", True)
                or getattr(perception, "_permission_save_failed", False)
                or not getattr(settings, "enabled", False) or not getattr(settings, "allow_remote", False)):
            raise RemoteError("desktop_permission_denied", "电脑端尚未允许屏幕读取或已暂停；请在电脑设置 → 屏幕感知中确认授权，不会自动开启。")
        return perception.observer, settings

    def _desktop_readiness(self):
        try:
            self._desktop_capture_permission()
        except RemoteError as exc:
            return {"available": False, "reason_code": exc.code, "reason": exc.message}
        return {"available": True, "reason_code": "", "reason": ""}

    def _agent_status(self):
        readiness = self.session.chat.agent_readiness()
        cid = self.session.chat.agent_running_conversation_id
        pending = False
        query = getattr(getattr(self.session.chat, "_agent_service", None), "execution_resources_pending", None)
        if callable(query):
            try:
                value = query()
                pending = value if type(value) is bool else True
            except Exception:
                pending = True
        return {"agent_available": readiness["available"],
                "agent_unavailable_reason": readiness["reason"],
                "agent_capabilities": readiness.get("tools", []),
                "agent_busy": cid is not None or pending, "agent_conversation_id": cid,
                "agent_interaction": (self.session.chat.agent_interaction_for(cid)
                    if cid is not None and callable(getattr(self.session.chat, "agent_interaction_for", None)) else None)}

    def _answer_agent(self, params):
        if (set(params) != {"conversation_id", "interaction_id", "answer"}
                or not isinstance(params["conversation_id"], str)
                or not isinstance(params["interaction_id"], str) or not isinstance(params["answer"], dict)):
            raise RemoteError("invalid_request", "答复需要当前会话、问题标识和明确选择。")
        try:
            if str(UUID(params["conversation_id"])) != params["conversation_id"]:
                raise ValueError
        except ValueError:
            raise RemoteError("invalid_request", "会话标识无效。") from None
        accepted = self.session.chat.answer_agent_interaction(params["interaction_id"], params["answer"],
                                                             conversation_id=params["conversation_id"])
        return {"accepted": bool(accepted)}

    def _steer_agent(self, params):
        if (set(params) != {"conversation_id", "text"} or not isinstance(params["conversation_id"], str)
                or not isinstance(params["text"], str)):
            raise RemoteError("invalid_request", "补充指令需要当前会话和文字。")
        try:
            if str(UUID(params["conversation_id"])) != params["conversation_id"]:
                raise ValueError
        except ValueError:
            raise RemoteError("invalid_request", "会话标识无效。") from None
        return {"accepted": bool(self.session.chat.steer_agent(params["text"], conversation_id=params["conversation_id"]))}

    def _send_agent(self, peer_id, request_id, params):
        text, conversation_id = self._validate_chat_params(params,allow_busy=True)
        readiness = self.session.chat.agent_readiness()
        if not readiness["available"]:
            raise RemoteError("agent_unavailable", readiness["reason"])
        admission=getattr(self.session.chat,"admit_remote_agent",None)
        if callable(admission):
            try: return admission(peer_id,request_id,conversation_id,text,image_id=params.get("image_id"))
            except CoordinatorError as exc:
                raise RemoteError("agent_queue_unavailable","任务暂未加入队列，请保持原请求编号查询状态；本次不会自动重复执行。") from None
        message_id = str(uuid5(NAMESPACE_URL, "aura-remote:" + peer_id + ":" + request_id))
        options = {"agent_mode": True}
        if "image_id" in params:
            options["image_id"] = params["image_id"]
        if not self.session.chat.send(text, message_id=message_id, conversation_id=conversation_id, **options):
            raise RemoteError("chat_unavailable", "电脑 Agent 暂不可用，请检查电脑配置和任务状态。")
        return {"accepted": True, "conversation_id": conversation_id, "message_id": message_id,
                "execution_mode": "agent", "reply_delivery": "sync.pull"}

    def _cancel_agent(self, params):
        cid = params.get("conversation_id")
        if set(params) != {"conversation_id"} or not isinstance(cid, str) or str(UUID(cid)) != cid:
            raise RemoteError("invalid_request", "停止任务需要有效的会话标识。")
        cancel=getattr(self.session.chat,"cancel_queued_agent",None)
        if callable(cancel):
            result=cancel(cid)
            if isinstance(result,dict) and result.get("cancelled") is True:
                return {"conversation_id":cid,**result}
            if result is True: return {"cancelled":True,"conversation_id":cid}
        if self.session.chat.agent_running_conversation_id != cid:
            return {"cancelled": False, "conversation_id": cid}
        return {"cancelled": self.session.chat.stop_request(cid), "conversation_id": cid}

    def _desktop_chat_fallback_permission(self):
        observer, settings = self._desktop_capture_permission()
        current_code = getattr(observer, "current_blocked_code", None)
        if not callable(current_code) or current_code(settings) != "own_window":
            raise DesktopCaptureUnavailable()

    def _capture_desktop(self, peer_id, generation, *, ui_call=None):
        self._require_image_storage_host_open()
        ui_call = self._call_ui if ui_call is None else ui_call
        observer, settings = ui_call(self._desktop_capture_permission, peer_id, generation=generation)
        payload = capture_desktop_image(observer, settings)
        # Recheck revocation and consent after capture, before retaining pixels
        # or exposing them to a phone/model. Never change perception settings.
        current_observer, current_settings = ui_call(self._desktop_capture_permission, peer_id,
                                                          generation=generation)
        if current_observer is not observer or current_settings != settings:
            raise RemoteError("desktop_permission_denied", "电脑屏幕授权已变化，画面已丢弃，请重新获取。")
        self._require_authorized(peer_id, generation)
        self._require_image_storage_host_open()
        result = self.images.upload(payload)
        self.session.chat.mark_image_storage_mutated(result["id"])
        return result

    def _validate_chat_params(self, params, *, allow_busy=False):
        text = params.get("text")
        conversation_id = params.get("conversation_id", self.session.chat.current_conversation_id)
        if (not isinstance(text, str) or not text.strip() or len(text) > 8000
                or not isinstance(conversation_id, str) or str(UUID(conversation_id)) != conversation_id):
            raise RemoteError("invalid_request", "聊天文字或会话标识无效。")
        if self.session.chat.busy and not allow_busy:
            raise RemoteError("chat_unavailable", "电脑正在处理另一条请求，请稍后再试。")
        return text, conversation_id

    def _send_chat(self, peer_id, request_id, params, desktop_image_id=None,
                   desktop_capture_unavailable_reason=None):
        text, conversation_id = self._validate_chat_params(params)
        message_id = str(uuid5(NAMESPACE_URL, "aura-remote:" + peer_id + ":" + request_id))
        options = {"image_id": params["image_id"]} if "image_id" in params else {}
        if params.get("desktop_context"):
            options.update(desktop_context=True, desktop_image_id=desktop_image_id)
            if desktop_capture_unavailable_reason == "own_window":
                # Recheck consent and the safe failure reason at UI admission,
                # after capture and before any model/Agent request is accepted.
                self._desktop_chat_fallback_permission()
                options["desktop_capture_unavailable_reason"] = "own_window"
        if not self.session.chat.send(text, message_id=message_id, conversation_id=conversation_id, **options):
            raise RemoteError("chat_unavailable", "聊天暂不可用，请检查电脑端聊天配置、忙碌状态或会话状态。")
        return {"accepted": True, "conversation_id": conversation_id, "message_id": message_id,
                "reply_delivery": "sync.pull"}

    def shutdown(self):
        with self._agent_guard(): self._shutdown()

    def _shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._enabled = False
        self._agent_authority_changed()
        self._stop_transport()
        self._set_status("disabled", endpoint_id="", ticket="", error_code="", auto_retry=False,
                         retry_in_seconds=None, local_available=False, relay_available=False)
