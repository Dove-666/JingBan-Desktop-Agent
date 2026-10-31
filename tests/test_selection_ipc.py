"""Real current-user local IPC with temporary identities and Qt-safe clients."""

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from desktop_pet.selection.context import SelectionContextError
from desktop_pet.selection.ipc import MAX_MESSAGE_BYTES, MAX_RESPONSE_BYTES, SelectionInbox, deliver_selection, encode_frame, inbox_name
from tests.test_application import spin_until


def request():
    return {"version": 1, "action": "ask_selection", "request_id": str(uuid4()), "context": {
        "text": "SELECTED_IPC_FIXTURE 内容", "page_text": "CURRENT_PAGE_FIXTURE 正文",
        "title": "测试当前页面", "url": "https://account:private@example.invalid/page?token=private#fragment",
        "captured_at": "2026-10-31T09:00:00Z", "app_name": "Microsoft Edge"}}


class SelectionIpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-selection-inbox-")
        self.config = Path(self.temp.name) / "fixture.ini"
        self.inboxes, self.threads, self.servers, self.sockets = [], [], [], []

    def tearDown(self):
        for inbox in self.inboxes:
            inbox.shutdown()
            inbox.deleteLater()
        for socket in self.sockets:
            socket.abort()
            socket.deleteLater()
        for server in self.servers:
            server.close()
            server.deleteLater()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "temporary IPC worker did not finish")
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def inbox(self, receiver=lambda context, request_id: True):
        inbox = SelectionInbox(self.config, receiver)
        self.inboxes.append(inbox)
        self.assertTrue(inbox.start())
        return inbox

    def worker(self, callback, *, timeout=4):
        result = {}
        done = threading.Event()
        def run():
            try:
                result["value"] = callback()
            except BaseException as error:
                result["error"] = error
            finally:
                done.set()
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        spin_until(done.is_set, timeout=timeout)
        thread.join(1)
        if "error" in result:
            raise result["error"]
        return result["value"]

    def exchange(self, chunks, *, timeout_ms=1000):
        """The blocking local socket always lives in this helper's worker."""
        def client():
            socket = QLocalSocket()
            socket.connectToServer(inbox_name(self.config))
            if not socket.waitForConnected(timeout_ms):
                return {"connected": False, "data": b"", "disconnected": True}
            received = bytearray()
            try:
                for chunk in chunks:
                    socket.write(chunk)
                    socket.flush()
                    time.sleep(.015)
                deadline = time.monotonic() + timeout_ms / 1000
                while time.monotonic() < deadline:
                    if socket.bytesAvailable():
                        received.extend(bytes(socket.readAll()))
                        if len(received) >= 4:
                            length = struct.unpack("<I", received[:4])[0]
                            if len(received) >= 4 + length:
                                break
                    if socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                        break
                    socket.waitForReadyRead(max(1, min(15, round((deadline - time.monotonic()) * 1000))))
                    # Windows PySide blocking socket waits can retain the GIL.
                    # Let the GUI's real QLocalServer process readiness/timers.
                    time.sleep(.003)
                return {"connected": True, "data": bytes(received),
                        "disconnected": socket.state() == QLocalSocket.LocalSocketState.UnconnectedState}
            finally:
                socket.abort()
        return self.worker(client)

    def deliver(self, value, *, timeout_ms=800):
        """Exercise the actual blocking host helper in an independent process.

        It must not share the GUI's Python GIL, just like the real native host.
        Popen.communicate itself runs in a worker while the main Qt loop spins.
        """
        code = ("import json,sys; from PySide6.QtCore import QCoreApplication; "
                "from desktop_pet.selection.ipc import deliver_selection; "
                "app=QCoreApplication([]); "
                "result=deliver_selection(sys.argv[1],json.load(sys.stdin),timeout_ms=int(sys.argv[2])); "
                "print(json.dumps(result,ensure_ascii=True))")
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen")
        for name in tuple(environment):
            if name.startswith("OPENAI_") or name in {"MEM0_API_KEY", "MEM0_DIR"}:
                environment.pop(name, None)
        def child():
            return subprocess.run([sys.executable, "-c", code, str(self.config), str(timeout_ms)],
                input=json.dumps(value), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", cwd=Path(__file__).resolve().parents[1],
                env=environment, timeout=5)
        result = self.worker(child, timeout=7)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def decode(self, data):
        self.assertGreaterEqual(len(data), 4)
        length = struct.unpack("<I", data[:4])[0]
        self.assertEqual(len(data), 4 + length)
        return json.loads(data[4:].decode("utf-8"))

    def raw_server(self, response):
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.servers.append(server)
        def accept():
            while server.hasPendingConnections():
                socket = server.nextPendingConnection()
                self.sockets.append(socket)
                def read(socket=socket):
                    socket.readAll()
                    if response is not None and not socket.property("responded"):
                        socket.setProperty("responded", True)
                        socket.write(response)
                        socket.flush()
                        socket.disconnectFromServer()
                socket.readyRead.connect(read)
                if socket.bytesAvailable():
                    read()
        server.newConnection.connect(accept)
        self.assertTrue(server.listen(inbox_name(self.config)))
        return server

    def test_fragmented_frame_acknowledges_exact_request_once_and_strips_private_url(self):
        calls = []
        inbox = self.inbox(lambda context, request_id: calls.append((context, request_id)) or True)
        self.assertEqual(inbox._server.socketOptions(), QLocalServer.SocketOption.UserAccessOption)
        value = request()
        frame = encode_frame(value)
        result = self.exchange([frame[:2], frame[2:7], frame[7:41], frame[41:]])
        ack = self.decode(result["data"])
        self.assertEqual(ack, {"ok": True, "request_id": value["request_id"]})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], value["request_id"])
        self.assertEqual(calls[0][0].url, "https://example.invalid/page")
        self.assertNotIn("SELECTED_IPC_FIXTURE", result["data"].decode("utf-8"))

    def test_same_request_id_is_acknowledged_without_opening_second_composer(self):
        calls = []
        self.inbox(lambda context, request_id: calls.append(request_id) or True)
        value = request()
        first = self.deliver(value)
        second = self.deliver(value)
        self.assertTrue(first["ok"])
        self.assertEqual(second, {"ok": True, "request_id": value["request_id"], "duplicate": True})
        self.assertEqual(calls, [value["request_id"]])

    def test_oversize_zero_trailing_and_invalid_json_frames_are_rejected_without_dispatch(self):
        calls = []
        self.inbox(lambda *args: calls.append(args) or True)
        good = encode_frame(request())
        frames = [struct.pack("<I", MAX_MESSAGE_BYTES + 1), struct.pack("<I", 0), good + b"x",
                  struct.pack("<I", 2) + b"{x", struct.pack("<I", 1) + b"\xff"]
        for frame in frames:
            with self.subTest(frame=frame[:8]):
                result = self.decode(self.exchange([frame])["data"])
                self.assertFalse(result["ok"])
                self.assertNotIn("context", result)
        self.assertEqual(calls, [])

    def test_wrong_action_invalid_context_and_noncanonical_id_are_rejected(self):
        calls = []
        self.inbox(lambda *args: calls.append(args) or True)
        for mutate in (lambda value: value.update(action="run_command"),
                       lambda value: value.update(version=True),
                       lambda value: value.update(request_id="not-a-uuid"),
                       lambda value: value.update(request_id=value["request_id"].upper()),
                       lambda value: value["context"].update(text=[]),
                       lambda value: value.update(extra="SELECTED_SECRET")):
            value = request()
            mutate(value)
            with self.subTest(value=value):
                result = self.decode(self.exchange([encode_frame(value)])["data"])
                self.assertFalse(result["ok"])
                self.assertNotIn("SELECTED_SECRET", json.dumps(result))
        self.assertEqual(calls, [])

    def test_receiver_false_and_exception_are_reported_and_not_deduplicated_as_success(self):
        for exception in (False, True):
            calls = []
            def receiver(context, request_id):
                calls.append(request_id)
                if exception:
                    raise RuntimeError("PRIVATE_PAGE_ERROR")
                return False
            inbox = self.inbox(receiver)
            value = request()
            for _ in range(2):
                result = self.deliver(value)
                self.assertFalse(result["ok"])
                self.assertNotIn("PRIVATE_PAGE_ERROR", json.dumps(result))
            self.assertEqual(len(calls), 2)
            inbox.shutdown()

    def test_partial_idle_client_is_disconnected_by_deadline_and_shutdown_closes_inbox(self):
        calls = []
        inbox = self.inbox(lambda *args: calls.append(args) or True)
        inbox.TIMEOUT_MS = 40
        result = self.exchange([b"\x10\x00"], timeout_ms=500)
        self.assertTrue(result["disconnected"])
        self.assertEqual(result["data"], b"")
        spin_until(lambda: not inbox._clients)
        self.assertEqual(calls, [])
        inbox.shutdown()
        inbox.shutdown()
        self.assertFalse(inbox.start())
        self.assertFalse(inbox._server.isListening())
        result = self.deliver(request(), timeout_ms=100)
        self.assertFalse(result["ok"])

    def test_deliver_validates_before_connecting_and_unstarted_inbox_fails_safely(self):
        value = request()
        value["action"] = "execute"
        with self.assertRaises(SelectionContextError):
            self.worker(lambda: deliver_selection(self.config, value, timeout_ms=100))
        result = self.deliver(request(), timeout_ms=100)
        self.assertFalse(result["ok"])
        self.assertNotIn(str(self.config), result["error"])

    def test_real_host_rejects_wrong_ack_identity_or_oversized_response(self):
        for response in (encode_frame({"ok": True, "request_id": str(uuid4())}),
                         struct.pack("<I", MAX_RESPONSE_BYTES + 1)):
            server = self.raw_server(response)
            result = self.deliver(request(), timeout_ms=200)
            self.assertFalse(result["ok"])
            server.close()

    def test_real_host_deadline_leaves_gui_responsive_when_listener_never_acknowledges(self):
        self.raw_server(None)
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        try:
            started = time.monotonic()
            result = self.deliver(request(), timeout_ms=120)
            self.assertFalse(result["ok"])
            self.assertGreaterEqual(len(ticks), 3)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            timer.stop()

    def test_shutdown_disconnects_an_active_partial_client_without_receiver_call(self):
        calls = []
        inbox = self.inbox(lambda *args: calls.append(args) or True)
        QTimer.singleShot(50, inbox.shutdown)
        result = self.exchange([b"\x10\x00"], timeout_ms=500)
        self.assertTrue(result["disconnected"])
        self.assertEqual(result["data"], b"")
        self.assertFalse(inbox._server.isListening())
        self.assertFalse(inbox._clients)
        self.assertEqual(calls, [])

    def test_real_native_host_waits_for_slow_first_composer_ack_without_false_failure_or_spawn(self):
        """A first native QDialog/font load can exceed the old 300ms deadline."""
        from tools.browser_ask_host import ALLOWED_ORIGIN
        calls = []
        def slow_composer(context, request_id):
            calls.append((context, request_id))
            time.sleep(.6)
            return True
        self.inbox(slow_composer)
        value = request()
        value.pop("request_id")
        code = """
import sys
from unittest.mock import patch
from tools import browser_ask_host as host
with patch.object(host, 'start_pet', side_effect=AssertionError('MUST_NOT_SPAWN')) as launch:
    status = host.main(['--config-path', sys.argv[1], sys.argv[2]],
                       input_stream=sys.stdin.buffer, output_stream=sys.stdout.buffer)
    if launch.called:
        raise SystemExit(92)
raise SystemExit(status)
"""
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen")
        for name in tuple(environment):
            if name.startswith("OPENAI_") or name in {"MEM0_API_KEY", "MEM0_DIR"}:
                environment.pop(name, None)
        def child():
            return subprocess.run([sys.executable, "-c", code, str(self.config), ALLOWED_ORIGIN],
                input=encode_frame(value), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=Path(__file__).resolve().parents[1], env=environment, timeout=6)
        started = time.monotonic()
        result = self.worker(child, timeout=8)
        self.assertGreaterEqual(time.monotonic() - started, .55)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(self.decode(result.stdout), {"ok": True})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
