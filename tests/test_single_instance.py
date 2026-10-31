"""Actual temporary Qt locks and local IPC; no production settings or vault.

The secondary processes only activate the isolated owner's command signal.
The real main-entrypoint case explicitly forbids creating a second controller.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.app import main
from desktop_pet.single_instance import SingleInstance, SingleInstanceError, instance_identity
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until


class SingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-single-instance-")
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "settings.ini"
        self.instances = []
        self.sockets = []
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        for socket in self.sockets:
            socket.abort()
            socket.deleteLater()
        for instance in self.instances:
            instance.release()
            instance.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def make_instance(self, path=None):
        instance = SingleInstance(path or self.path)
        self.instances.append(instance)
        return instance

    def connect(self, instance):
        socket = QLocalSocket()
        self.sockets.append(socket)
        socket.connectToServer(instance.server_name)
        spin_until(lambda: socket.state() == QLocalSocket.LocalSocketState.ConnectedState)
        return socket

    def child(self, code, *arguments):
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen")
        for name in tuple(environment):
            if name.startswith("OPENAI_") or name in ("MEM0_API_KEY", "MEM0_DIR"):
                environment.pop(name, None)
        child = subprocess.Popen(
            [sys.executable, "-c", code, *map(str, arguments)],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        self.children.append(child)
        return child

    def test_canonical_path_identity_is_stable_and_configurations_are_isolated(self):
        path, name = instance_identity(self.path)
        self.assertEqual((path, name), instance_identity(self.directory / "nested" / ".." / "settings.ini"))
        self.assertEqual((path, name), instance_identity(self.path.absolute()))
        self.assertNotEqual(name, instance_identity(self.directory / "other.ini")[1])
        self.assertEqual(path.parent, self.directory.resolve())
        self.assertNotIn(str(self.directory), name)
        if os.name == "nt":
            self.assertEqual((path, name)[1], instance_identity(str(self.path).upper())[1])

    def test_owner_uses_current_user_permissions_and_lifetime_lock(self):
        owner = self.make_instance()
        self.assertTrue(owner.start())
        self.assertTrue(owner.is_primary)
        self.assertEqual(owner._lock.staleLockTime(), 0)
        self.assertEqual(owner._server.socketOptions(), QLocalServer.SocketOption.UserAccessOption)
        other_configuration = self.make_instance(self.directory / "other.ini")
        self.assertTrue(other_configuration.start())
        owner.release()
        owner.release()
        replacement = self.make_instance()
        self.assertTrue(replacement.start())

    def test_live_owner_without_ipc_fails_closed_and_never_deletes_lock(self):
        lock_path, _ = instance_identity(self.path)
        lock = QLockFile(str(lock_path))
        lock.setStaleLockTime(0)
        self.assertTrue(lock.tryLock(0))
        try:
            original = lock_path.read_bytes()
            secondary = self.make_instance()
            with self.assertRaisesRegex(SingleInstanceError, "不会启动第二个桌宠"):
                secondary.start(timeout_ms=100)
            secondary.release()
            self.assertTrue(lock.isLocked())
            self.assertEqual(lock_path.read_bytes(), original)
            third = QLockFile(str(lock_path))
            third.setStaleLockTime(0)
            self.assertFalse(third.tryLock(0))
        finally:
            lock.unlock()

    def test_two_real_processes_forward_chat_and_debug_with_ack(self):
        owner = self.make_instance()
        owner.start()
        spy = QSignalSpy(owner.activation_requested)
        code = (
            "import sys; from PySide6.QtCore import QCoreApplication; "
            "from desktop_pet.single_instance import SingleInstance; "
            "app=QCoreApplication([]); guard=SingleInstance(sys.argv[1]); "
            "result=guard.start(sys.argv[2]); print('PRIMARY' if result else 'ACTIVATED'); guard.release()"
        )
        for index, command in enumerate(("chat", "debug"), start=1):
            child = self.child(code, self.path, command)
            spin_until(lambda: child.poll() is not None)
            output, errors = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, errors)
            self.assertEqual(output.strip(), "ACTIVATED")
            self.assertEqual(spy.count(), index)
            self.assertEqual(spy.at(index - 1), [command])
            self.assertTrue(owner.is_primary)

    def test_duplicate_real_entrypoint_does_not_construct_second_pet_or_session(self):
        owner = self.make_instance()
        owner.start()
        spy = QSignalSpy(owner.activation_requested)
        code = """
import sys
from pathlib import Path
from unittest.mock import patch
from tests.test_application import isolated_settings, MemoryCredentialBackend
from desktop_pet.app import main
settings = isolated_settings(Path(sys.argv[1]), MemoryCredentialBackend())
with patch('desktop_pet.app.ApplicationController', side_effect=AssertionError('second controller created')):
    result = main(['--debug-panel'], settings=settings)
print('EXIT', result)
"""
        child = self.child(code, self.path)
        spin_until(lambda: child.poll() is not None)
        output, errors = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, errors)
        self.assertIn("EXIT 0", output)
        self.assertEqual(spy.count(), 1)
        self.assertEqual(spy.at(0), ["debug"])
        self.assertFalse((self.directory / "conversations.sqlite3").exists())
        self.assertFalse((self.directory / "memory").exists())

    def test_fragmented_frame_is_dispatched_once_and_acknowledged(self):
        owner = self.make_instance()
        owner.start()
        spy = QSignalSpy(owner.activation_requested)
        socket = self.connect(owner)
        received = bytearray()
        socket.readyRead.connect(lambda: received.extend(bytes(socket.readAll())))
        socket.write(b"AURA/1 de")
        socket.flush()
        self.app.processEvents()
        self.assertEqual(spy.count(), 0)
        socket.write(b"bug\n")
        socket.flush()
        spin_until(lambda: spy.count() == 1)
        spin_until(lambda: bool(received))
        self.assertEqual(spy.at(0), ["debug"])
        self.assertEqual(received, SingleInstance.ACK)

    def test_invalid_oversized_and_multiple_commands_are_rejected(self):
        owner = self.make_instance()
        owner.start()
        spy = QSignalSpy(owner.activation_requested)
        for frame in (b"exec calc.exe\n", b"AURA/1 open C:/secret\n", b"AURA/1 chat\nAURA/1 debug\n", b"x" * 100):
            with self.subTest(frame=frame[:25]):
                socket = self.connect(owner)
                socket.write(frame)
                socket.flush()
                spin_until(lambda: socket.state() == QLocalSocket.LocalSocketState.UnconnectedState)
                self.assertEqual(bytes(socket.readAll()), b"")
                self.assertEqual(spy.count(), 0)
        self.assertTrue(owner.is_primary)

    def test_idle_client_is_disconnected_by_deadline(self):
        owner = self.make_instance()
        owner.CLIENT_TIMEOUT_MS = 40
        owner.start()
        socket = self.connect(owner)
        socket.write(b"AURA/")
        socket.flush()
        spin_until(lambda: socket.state() == QLocalSocket.LocalSocketState.UnconnectedState)
        self.assertEqual(owner._clients, {})

    def test_server_start_failure_releases_lock_and_does_not_create_controller(self):
        owner = self.make_instance()
        with patch.object(owner._server, "listen", return_value=False):
            with self.assertRaisesRegex(SingleInstanceError, "本地通信服务无法启动"):
                owner.start()
        self.assertFalse(owner.is_primary)
        replacement = self.make_instance()
        self.assertTrue(replacement.start())

    def test_configuration_directory_error_is_static_and_safe(self):
        blocker = self.directory / "not-a-directory"
        blocker.write_text("ordinary file", encoding="utf-8")
        instance = self.make_instance(blocker / "settings.ini")
        with self.assertRaises(SingleInstanceError) as caught:
            instance.start()
        self.assertIn("配置目录权限", str(caught.exception))
        self.assertNotIn(str(blocker), str(caught.exception))
        self.assertFalse(instance.is_primary)

    def test_dead_process_lock_is_recovered_without_force_removal(self):
        lock_path, _ = instance_identity(self.path)
        ready_path = self.directory / "owner-ready"
        code = """
import sys, time
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QLockFile
app = QCoreApplication([])
lock = QLockFile(sys.argv[1]); lock.setStaleLockTime(0)
if not lock.tryLock(0): raise SystemExit(4)
Path(sys.argv[2]).write_text('locked', encoding='utf-8')
time.sleep(30)
"""
        child = self.child(code, lock_path, ready_path)
        spin_until(ready_path.exists)
        child.kill()
        child.communicate(timeout=5)
        # QLockFile, rather than our code, checks the dead owner and reclaims it.
        self.assertTrue(lock_path.exists())
        owner = self.make_instance()
        self.assertTrue(owner.start())

    def test_missing_image_entrypoint_releases_lock_before_retry(self):
        settings = isolated_settings(self.path, MemoryCredentialBackend())
        with patch("desktop_pet.app.QMessageBox.critical") as error:
            result = main(["--image", str(self.directory / "missing.png")], settings=settings)
        self.assertEqual(result, 2)
        error.assert_called_once()
        replacement = self.make_instance()
        self.assertTrue(replacement.start())

    def test_invalid_activation_command_is_never_sent(self):
        owner = self.make_instance()
        with self.assertRaisesRegex(SingleInstanceError, "命令无效"):
            owner.start("run-python")
        self.assertFalse(owner.is_primary)


if __name__ == "__main__":
    unittest.main()
