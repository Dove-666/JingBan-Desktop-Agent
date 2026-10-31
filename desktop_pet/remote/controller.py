"""Desktop remote service: authenticated RPC and a bounded Qt-thread bridge."""

from collections import OrderedDict, deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from copy import deepcopy
from datetime import datetime, timezone
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
from ..expression_manager import EXPRESSIONS
from . import PROTOCOL_VERSION
from .attachments import ImageInbox
from .errors import RemoteError
from .settings import PEER_ID, RemotePreferences
from .sync import RemoteSyncStore, SyncError


CAPABILITIES = ["pet.command", "chat.send", "sync.snapshot", "sync.pull", "operation.get",
                "image.upload", "image.list", "image.get", "image.delete"]


class RemoteController(QObject):
    status_changed = Signal(dict)
    pairing_changed = Signal(dict)
    devices_changed = Signal(list)
    image_received = Signal(dict)
    _transport_status = Signal(int, dict)
    _ui_request = Signal(object)

    def __init__(self, session, parent=None, *, transport_factory=None, clock=time.time):
        super().__init__(parent)
        self.session = session
        self.preferences = RemotePreferences(session.settings)
        self.sync = RemoteSyncStore(session.chat.store)
        self.images = ImageInbox(self.preferences.directory / "inbox")
        self._clock = clock
        self._transport_factory = transport_factory
        self._transport = None
        self._generation = 0
        self._closed = False
        self._enabled = self.preferences.enabled
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
        self._pair_timer = QTimer(self)
        self._pair_timer.setInterval(1000)
        self._pair_timer.timeout.connect(self._expire_pairing)
        if self._enabled:
            QTimer.singleShot(0, self.retry)

    def _dependency_available(self):
        return self._transport_factory is not None or importlib.util.find_spec("iroh") is not None

    @property
    def enabled(self):
        return self._enabled

    @property
    def status(self):
        return deepcopy(self._status)

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

    def set_enabled(self, enabled):
        if type(enabled) is not bool or self._closed:
            return False
        try:
            self.preferences.set_enabled(enabled)
        except RemoteError as exc:
            if not enabled:
                self._enabled = False
                self._stop_transport()
            self._set_status("error", exc.message)
            return False
        self._enabled = enabled
        if enabled:
            self.retry()
        else:
            self._stop_transport()
            self._set_status("disabled", endpoint_id="", ticket="")
        return True

    def retry(self):
        if self._closed or not self._enabled:
            return
        self._stop_transport()
        if not self._dependency_available():
            self._set_status("error", "可选通信组件未安装，请在此页安装后重启。")
            return
        self._set_status("starting", endpoint_id="", ticket="")
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
            self._set_status("error", exc.message)
        except Exception:
            self._set_status("error", "无法启动远程连接，请检查通信组件及网络后重试。")

    @Slot(int, dict)
    def _accept_status(self, generation, status):
        if self._closed or not self._enabled or generation != self._generation:
            return
        state = status.get("state")
        if state == "listening":
            self._set_status("listening", endpoint_id=str(status.get("endpoint_id", "")),
                ticket=str(status.get("ticket", "")), connection_type=status.get("connection_type", "unknown"))
        elif state in ("error", "stopped"):
            self.cancel_pairing()
            self._set_status("error", "远程连接已断开，请检查网络后重试。", endpoint_id="", ticket="")
        elif state == "starting":
            self._set_status("starting")

    def _cancel_ui_requests(self):
        with self._futures_lock:
            for future in self._futures:
                future.cancel()
            self._futures.clear()

    def _stop_transport(self):
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
        if method == "status":
            return self._call_ui(lambda: {"protocol_version": PROTOCOL_VERSION,
                "state": self._status["state"], "chat_busy": self.session.chat.busy,
                "current_conversation_id": self.session.chat.current_conversation_id,
                "capabilities": CAPABILITIES}, peer_id, generation=generation)
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
        if method not in ("pet.command", "chat.send", "image.upload", "image.delete"):
            raise RemoteError("unknown_method", "不支持此远程操作。")
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
            return {"duplicate": True, **reservation}
        try:
            if method == "pet.command":
                result = self._call_ui(lambda: self._pet_command(params), peer_id, generation=generation)
            elif method == "chat.send":
                result = self._call_ui(lambda: self._send_chat(peer_id, request_id, params), peer_id,
                                       generation=generation)
            elif method == "image.upload":
                self._require_authorized(peer_id, generation)
                result = self.images.upload(params)
                self.image_received.emit(result)
            else:
                result = self.images.delete(params["id"])
            self.sync.complete_operation(peer_id, request_id, {"ok": True, "result": result})
            return result
        except Exception:
            self.sync.complete_operation(peer_id, request_id, {"ok": False, "error": {
                "code": "operation_failed", "message": "该操作未完成，请确认电脑状态后使用新的请求标识。"}})
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

    def _send_chat(self, peer_id, request_id, params):
        text = params.get("text")
        conversation_id = params.get("conversation_id", self.session.chat.current_conversation_id)
        if (not isinstance(text, str) or not text.strip() or len(text) > 8000
                or not isinstance(conversation_id, str) or str(UUID(conversation_id)) != conversation_id):
            raise RemoteError("invalid_request", "聊天文字或会话标识无效。")
        message_id = str(uuid5(NAMESPACE_URL, "aura-remote:" + peer_id + ":" + request_id))
        if not self.session.chat.send(text, message_id=message_id, conversation_id=conversation_id):
            raise RemoteError("chat_unavailable", "聊天暂不可用，请检查电脑端聊天配置、忙碌状态或会话状态。")
        return {"accepted": True, "conversation_id": conversation_id, "message_id": message_id,
                "reply_delivery": "sync.pull"}

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._enabled = False
        self._stop_transport()
        self._set_status("disabled", endpoint_id="", ticket="")
