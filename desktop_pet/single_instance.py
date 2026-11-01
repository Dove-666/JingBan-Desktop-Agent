"""One desktop-pet process per canonical settings file, with bounded local IPC.

The lock protects the entire application lifetime.  Qt checks dead processes;
we never delete a lock based on its age or forcibly remove a live owner's lock.
Only fixed activation commands cross the current-user local socket.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time

from PySide6.QtCore import QLockFile, QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstanceError(RuntimeError):
    """A safe, static startup error; never expose paths or backend messages."""


def instance_identity(config_path: str | Path) -> tuple[Path, str]:
    """Equivalent absolute settings paths share a lock and IPC endpoint."""
    try:
        canonical = Path(config_path).expanduser().resolve()
        if not str(config_path).strip() or canonical.is_dir():
            raise ValueError
        normalized = os.path.normcase(str(canonical))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
        return canonical.parent / f".aura-{digest}.lock", f"aura-desktop-{digest}"
    except (OSError, ValueError, RuntimeError, TypeError):
        raise SingleInstanceError("无法访问桌宠配置目录，请检查当前用户的目录权限。") from None


class SingleInstance(QObject):
    """``start`` returns True for the owner, False after acknowledged activation.

    A failed activation raises rather than allowing a second pet or Mem0 owner.
    ``start`` must precede construction of ApplicationController.  Every socket
    accepts one short ASCII frame, has a deadline, and cannot run arbitrary code.
    """

    activation_requested = Signal(str)
    COMMANDS = {"chat": b"AURA/1 chat\n", "debug": b"AURA/1 debug\n",
                "silent": b"AURA/1 silent\n"}
    ACK = b"AURA/1 OK\n"
    MAX_FRAME_BYTES = 32
    MAX_CLIENTS = 8
    CLIENT_TIMEOUT_MS = 1000

    def __init__(self, config_path: str | Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.lock_path, self.server_name = instance_identity(config_path)
        self._lock = QLockFile(str(self.lock_path))
        self._lock.setStaleLockTime(0)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.setMaxPendingConnections(self.MAX_CLIENTS)
        self._server.newConnection.connect(self._accept_connections)
        self._clients: dict[QLocalSocket, tuple[bytearray, QTimer]] = {}

    @property
    def is_primary(self) -> bool:
        return self._lock.isLocked()

    def start(self, command: str = "chat", *, timeout_ms: int = 2500) -> bool:
        if command not in self.COMMANDS:
            raise SingleInstanceError("桌宠启动命令无效。")
        if self.is_primary:
            raise SingleInstanceError("桌宠单实例保护已经启动。")
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise SingleInstanceError("无法创建桌宠运行锁，请检查配置目录权限。") from None
        if self._lock.tryLock(0):
            # Only the proven lock owner may remove an abandoned IPC endpoint.
            # QLockFile itself safely reclaims a lock left by a dead process.
            QLocalServer.removeServer(self.server_name)
            if not self._server.listen(self.server_name):
                self.release()
                raise SingleInstanceError("桌宠本地通信服务无法启动，请检查系统权限。")
            return True
        if self._lock.error() != QLockFile.LockError.LockFailedError:
            raise SingleInstanceError("无法创建桌宠运行锁，请检查配置目录权限。")
        self._activate_existing(command, timeout_ms)
        return False

    def _activate_existing(self, command: str, timeout_ms: int) -> None:
        deadline = time.monotonic() + max(100, min(int(timeout_ms), 5000)) / 1000
        socket = QLocalSocket()
        socket.setReadBufferSize(self.MAX_FRAME_BYTES + 1)
        try:
            # The first process may own its lock while its listener is starting.
            while time.monotonic() < deadline:
                socket.abort()
                socket.connectToServer(self.server_name)
                remaining = max(1, round((deadline - time.monotonic()) * 1000))
                if socket.waitForConnected(min(150, remaining)):
                    break
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            else:
                raise SingleInstanceError("桌宠已在运行，但暂时无法唤醒窗口。请稍后重试，不会启动第二个桌宠。")
            socket.write(self.COMMANDS[command])
            socket.flush()
            received = bytearray()
            while time.monotonic() < deadline:
                if socket.bytesAvailable():
                    received.extend(bytes(socket.read(self.MAX_FRAME_BYTES + 1)))
                    if len(received) > self.MAX_FRAME_BYTES:
                        break
                    if b"\n" in received:
                        if bytes(received) == self.ACK:
                            return
                        break
                remaining = max(1, round((deadline - time.monotonic()) * 1000))
                if not socket.waitForReadyRead(remaining) and not socket.bytesAvailable():
                    break
            raise SingleInstanceError("桌宠已在运行，但窗口唤醒未得到确认。请稍后重试，不会启动第二个桌宠。")
        finally:
            socket.abort()

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            if len(self._clients) >= self.MAX_CLIENTS:
                socket.abort()
                socket.deleteLater()
                continue
            socket.setReadBufferSize(self.MAX_FRAME_BYTES + 1)
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda socket=socket: self._drop_client(socket))
            self._clients[socket] = (bytearray(), timer)
            socket.readyRead.connect(lambda socket=socket: self._read_command(socket))
            socket.disconnected.connect(lambda socket=socket: self._drop_client(socket))
            timer.start(self.CLIENT_TIMEOUT_MS)
            if socket.bytesAvailable():
                self._read_command(socket)

    def _read_command(self, socket: QLocalSocket) -> None:
        client = self._clients.get(socket)
        if client is None or socket.property("aura_command_handled"):
            return
        buffer, timer = client
        buffer.extend(bytes(socket.read(self.MAX_FRAME_BYTES + 1)))
        if len(buffer) > self.MAX_FRAME_BYTES:
            self._drop_client(socket)
            return
        if b"\n" not in buffer:
            return
        commands = {frame: command for command, frame in self.COMMANDS.items()}
        command = commands.get(bytes(buffer))
        if command is None:
            self._drop_client(socket)
            return
        # Mark it before dispatch: one activation per client.  Keep the socket
        # until disconnect so pending ACK bytes cannot be destroyed too early.
        socket.setProperty("aura_command_handled", True)
        if command != "silent":
            self.activation_requested.emit(command)
        socket.write(self.ACK)
        socket.flush()
        socket.disconnectFromServer()

    def _drop_client(self, socket: QLocalSocket) -> None:
        client = self._clients.pop(socket, None)
        if client is not None:
            client[1].stop()
            socket.abort()
            socket.deleteLater()

    def release(self) -> None:
        """Idempotent; a secondary instance can never release the owner's lock."""
        for socket in list(self._clients):
            self._drop_client(socket)
        self._server.close()
        self._lock.unlock()
