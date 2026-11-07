"""Current-user local inbox for explicit, bounded browser selection requests."""
from __future__ import annotations

from collections import OrderedDict
import json
import struct
import time
from uuid import UUID

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ..single_instance import instance_identity
from .context import SelectionContext, SelectionContextError


MAX_MESSAGE_BYTES = 96 * 1024
MAX_RESPONSE_BYTES = 4096


def inbox_name(config_path):
    return instance_identity(config_path)[1] + "-selection"


def validate_request(value):
    if (not isinstance(value, dict) or set(value) != {"version", "action", "request_id", "context"}
            or type(value["version"]) is not int or value["version"] != 1
            or value["action"] != "ask_selection"):
        raise SelectionContextError("选区请求格式无效，请重新选择文字后再试。")
    request_id = value["request_id"]
    try:
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SelectionContextError("选区请求标识无效，请重新选择文字后再试。") from None
    context = SelectionContext.from_dict(value["context"])
    return request_id, context


def encode_frame(value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not payload or len(payload) > MAX_MESSAGE_BYTES:
        raise SelectionContextError("选区内容太长，请缩小选中文字范围后再试。")
    return struct.pack("<I", len(payload)) + payload


class SelectionInbox(QObject):
    """No API requests, clipboard reads or commands are accepted by this inbox.

    The receiver returns True only after it has accepted a question composer.
    Source data is never included in the response or in logs.
    """
    MAX_CLIENTS = 4
    TIMEOUT_MS = 2500

    def __init__(self, config_path, receiver, parent=None):
        super().__init__(parent)
        self.name = inbox_name(config_path)
        self.receiver = receiver
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.setMaxPendingConnections(self.MAX_CLIENTS)
        self._server.newConnection.connect(self._accept)
        self._clients = {}
        self._accepted = OrderedDict()
        self._closed = False

    def start(self):
        return not self._closed and (self._server.isListening() or self._server.listen(self.name))

    def _accept(self):
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            if self._closed or len(self._clients) >= self.MAX_CLIENTS:
                socket.abort()
                socket.deleteLater()
                continue
            socket.setReadBufferSize(MAX_MESSAGE_BYTES + 5)
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda socket=socket: self._drop(socket))
            self._clients[socket] = (bytearray(), timer)
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(lambda socket=socket: self._drop(socket))
            timer.start(self.TIMEOUT_MS)
            if socket.bytesAvailable():
                self._read(socket)

    def _reply(self, socket, result):
        socket.setProperty("selection_handled", True)
        socket.write(encode_frame(result))
        socket.flush()
        socket.disconnectFromServer()

    def _read(self, socket):
        entry = self._clients.get(socket)
        if entry is None or socket.property("selection_handled"):
            return
        buffer, _timer = entry
        buffer.extend(bytes(socket.read(MAX_MESSAGE_BYTES + 5)))
        if len(buffer) < 4:
            return
        length = struct.unpack("<I", buffer[:4])[0]
        if not 0 < length <= MAX_MESSAGE_BYTES or len(buffer) > 4 + length:
            self._reply(socket, {"ok": False, "error": "选区请求过长或格式无效。"})
            return
        if len(buffer) < 4 + length:
            return
        try:
            request_id, context = validate_request(json.loads(bytes(buffer[4:]).decode("utf-8")))
        except (ValueError, UnicodeError, TypeError, RecursionError):
            self._reply(socket, {"ok": False, "error": "无法读取选区内容，请重新选择文字后再试。"})
            return
        now = time.monotonic()
        while self._accepted and (len(self._accepted) >= 64 or now - next(iter(self._accepted.values())) > 180):
            self._accepted.popitem(last=False)
        if request_id in self._accepted:
            self._reply(socket, {"ok": True, "request_id": request_id, "duplicate": True})
            return
        try:
            accepted = self.receiver(context, request_id) is True
        except Exception:
            accepted = False
        if not accepted:
            self._reply(socket, {"ok": False, "error": "提问窗口暂时无法打开，请关闭多余的提问框后重试。"})
            return
        self._accepted[request_id] = now
        self._reply(socket, {"ok": True, "request_id": request_id})

    def _drop(self, socket):
        entry = self._clients.pop(socket, None)
        if entry:
            entry[1].stop()
            socket.abort()
            socket.deleteLater()

    def shutdown(self):
        self._closed = True
        for socket in list(self._clients):
            self._drop(socket)
        self._server.close()
        self._accepted.clear()
        self.receiver = None


def deliver_selection(config_path, request, *, timeout_ms=2500):
    """Blocking helper for the short-lived native host, never the GUI thread."""
    validate_request(request)
    outgoing = encode_frame(request)
    socket = QLocalSocket()
    socket.setReadBufferSize(MAX_RESPONSE_BYTES + 5)
    deadline = time.monotonic() + max(0.1, min(timeout_ms / 1000, 10))
    try:
        socket.connectToServer(inbox_name(config_path))
        if not socket.waitForConnected(max(1, round((deadline - time.monotonic()) * 1000))):
            return {"ok": False, "error": "鲸伴尚未就绪，请启动鲸伴后再试。"}
        socket.write(outgoing)
        socket.flush()
        received = bytearray()
        while time.monotonic() < deadline:
            if socket.bytesAvailable():
                received.extend(bytes(socket.read(MAX_RESPONSE_BYTES + 5)))
                if len(received) >= 4:
                    length = struct.unpack("<I", received[:4])[0]
                    if not 0 < length <= MAX_RESPONSE_BYTES or len(received) > 4 + length:
                        break
                    if len(received) == 4 + length:
                        result = json.loads(bytes(received[4:]).decode("utf-8"))
                        if (isinstance(result, dict) and isinstance(result.get("ok"), bool)
                                and (result["ok"] is False or result.get("request_id") == request["request_id"])):
                            return result
                        break
            if not socket.waitForReadyRead(max(1, round((deadline - time.monotonic()) * 1000))) and not socket.bytesAvailable():
                break
    except (ValueError, UnicodeError, TypeError, RecursionError):
        pass
    finally:
        socket.abort()
    return {"ok": False, "error": "桌宠没有确认打开提问框，请稍后重试。"}
