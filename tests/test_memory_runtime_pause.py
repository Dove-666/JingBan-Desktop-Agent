"""Runtime-only automatic extraction privacy gate; no production settings.

Controlled helper processes expose the launch/registration/queue races. Real
chat still uses its ordinary SDK against a localhost-only model fixture.
"""

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QEvent, QSettings, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.memory import MemoryService, MemoryUnavailable
from desktop_pet.memory_controller import LazyMemoryService, MemoryController
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
from desktop_pet.ui.memory_panel import MemoryPanel
from tests.mock_api import MockApiServer
from tests.test_application import spin_until
from tests.test_memory_service import Vault
from tests.test_session_ui import MemoryControllerFake


class ControlledHelper:
    def __init__(self, *, blocked=False, result=None):
        self.args = ["controlled-memory-helper"]
        self.returncode = None
        self.payload = None
        self.terminated = False
        self.killed = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.result = [] if result is None else result
        if not blocked:
            self.release.set()

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self.payload = json.loads(input.decode("utf-8"))
            self.started.set()
            if not self.release.wait(timeout or 3):
                raise subprocess.TimeoutExpired(self.args, timeout)
            if self.returncode is None:
                self.returncode = 0
        return json.dumps({"ok": True, "result": self.result}).encode("utf-8"), b""

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.release.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.release.set()


class RuntimeMemoryPauseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-runtime-memory-pause-")
        self.root = Path(self.temporary.name)
        self.vault = Vault()
        self.settings = SettingsManager(QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat),
                                        CredentialStore(backend=self.vault))
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "fixture-chat", 3))
        self.settings.save_api_key("fixture-only-chat-key")
        self.service = MemoryService(
            self.settings, self.root / "memory",
            llm_credentials=CredentialStore(account="mem0-llm-key", backend=self.vault),
            embedding_credentials=CredentialStore(account="mem0-embedding-key", backend=self.vault),
        )
        self.service.save_settings(replace(self.service.load_settings(), enabled=True, automatic=True,
                                          embedding_base_url="http://127.0.0.1:1/v1", embedding_model="fixture-embedding",
                                          embedding_dimensions=8), embedding_key="fixture-only-embedding-key")
        self.helpers = []
        self.threads = []
        self.qt_objects = []

    def tearDown(self):
        for helper in self.helpers:
            helper.release.set()
        for obj in self.qt_objects:
            if hasattr(obj, "shutdown"):
                obj.shutdown()
            if hasattr(obj, "hide"):
                obj.hide()
        self.service.shutdown()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "A controlled background operation did not stop")
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for obj in self.qt_objects:
            obj.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temporary.cleanup()

    @contextmanager
    def helpers_from(self, factory):
        original = subprocess.Popen
        def launch(command, *args, **kwargs):
            if isinstance(command, list) and "desktop_pet.memory.worker" in command:
                helper = factory()
                self.helpers.append(helper)
                return helper
            return original(command, *args, **kwargs)
        with patch("desktop_pet.memory.service.subprocess.Popen", side_effect=launch):
            yield

    def background(self, function):
        outcome = {}
        def run():
            try:
                outcome["result"] = function()
            except Exception as error:
                outcome["error"] = error
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread, outcome

    def extract(self, *, service=None, trigger="automatic"):
        return (service or self.service).extract(
            [{"role": "user", "content": "请记住，我长期喜欢简洁的桌宠回复。"}],
            conversation_id=str(uuid4()), trigger=trigger,
        )

    def test_lazy_pause_before_use_does_not_construct_service_or_read_configuration(self):
        constructed = []
        lazy = LazyMemoryService(lambda: (constructed.append(True), self.service)[1])
        lazy.set_automatic_paused(True)
        self.assertEqual(constructed, [])
        self.assertTrue(lazy.automatic_paused)
        with self.helpers_from(lambda: self.fail("A paused automatic extraction started a helper")):
            self.assertEqual(self.extract(service=lazy), [])
        self.assertEqual(constructed, [True])
        self.assertIs(lazy._automatic_pause, self.service._automatic_pause)

    def test_pause_during_lazy_factory_does_not_wait_for_factory_lock(self):
        entered, release = threading.Event(), threading.Event()
        def factory():
            entered.set()
            self.assertTrue(release.wait(3))
            return self.service
        lazy = LazyMemoryService(factory)
        with self.helpers_from(lambda: self.fail("A creation-racing pause admitted a helper")):
            thread, outcome = self.background(lambda: self.extract(service=lazy))
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            lazy.set_automatic_paused(True)
            self.assertLess(time.monotonic() - before, 0.1)
            release.set()
            thread.join(2)
        self.assertEqual(outcome, {"result": []})
        self.assertTrue(self.service.automatic_paused)

    def test_lazy_shutdown_is_immediate_and_cleans_a_late_factory_instance(self):
        entered, release = threading.Event(), threading.Event()
        def factory():
            entered.set()
            self.assertTrue(release.wait(3))
            return self.service
        lazy = LazyMemoryService(factory)
        thread, outcome = self.background(lazy.load_settings)
        self.assertTrue(entered.wait(1))
        before = time.monotonic()
        lazy.shutdown()
        self.assertLess(time.monotonic() - before, 0.1)
        release.set()
        thread.join(2)
        self.assertIsInstance(outcome.get("error"), MemoryUnavailable)
        self.assertTrue(self.service._closed.is_set())

    def test_paused_acceptance_skips_even_settings_io(self):
        self.service.set_automatic_paused(True)
        with patch.object(self.service, "load_settings", side_effect=AssertionError("No disk read should be needed")):
            self.assertEqual(self.extract(), [])

    def test_queued_automatic_call_stops_while_storage_lock_is_still_held(self):
        read = threading.Event()
        original = self.service.load_settings
        def observed():
            result = original()
            if threading.current_thread() is not threading.main_thread():
                read.set()
            return result
        with patch.object(self.service, "load_settings", side_effect=observed), self.helpers_from(
            lambda: self.fail("A queued paused operation started a helper")
        ):
            with self.service._lock:
                thread, outcome = self.background(self.extract)
                self.assertTrue(read.wait(1))
                self.service.set_automatic_paused(True)
                thread.join(0.5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, {"result": []})

    def test_pause_racing_popen_registration_never_sends_payload(self):
        launching, release = threading.Event(), threading.Event()
        helper = ControlledHelper()
        def factory():
            launching.set()
            self.assertTrue(release.wait(3))
            return helper
        with self.helpers_from(factory):
            thread, outcome = self.background(self.extract)
            self.assertTrue(launching.wait(1))
            self.service.set_automatic_paused(True)
            release.set()
            thread.join(2)
        self.assertEqual(outcome, {"result": []})
        self.assertIsNone(helper.payload)
        self.assertTrue(helper.terminated)
        self.assertFalse(self.service._processes)
        self.assertFalse(self.service._process_operations)
        self.assertFalse((self.service.data_dir / "embedding_identity.json").exists())

    def test_active_automatic_helper_is_cancelled_without_changing_saved_automatic(self):
        helper = ControlledHelper(blocked=True)
        before = self.service.config_path.read_bytes()
        with self.helpers_from(lambda: helper):
            thread, outcome = self.background(self.extract)
            self.assertTrue(helper.started.wait(1))
            self.service.set_automatic_paused(True)
            self.service.set_automatic_paused(False)  # Resuming cannot revive a cancelled helper.
            thread.join(2)
        self.assertTrue(helper.terminated)
        self.assertEqual(outcome, {"result": []})
        self.assertEqual(self.service.config_path.read_bytes(), before)
        self.assertTrue(self.service.load_settings().automatic)
        self.assertFalse(self.service._process_operations)
        self.assertFalse(self.service._cancelled_automatic)
        self.assertTrue((self.service.data_dir / "embedding_identity.json").exists(), "A possibly committed cancellation must retain vector-space protection")

    def test_runtime_pause_does_not_cancel_search_explicit_extraction_or_model_preparation(self):
        cases = (
            ("search", lambda: self.service.search("解释偏好")),
            ("extract", lambda: self.extract(trigger="explicit")),
            ("prepare_model", self.service.prepare_local_model),
        )
        for operation, action in cases:
            with self.subTest(operation=operation):
                self.service.set_automatic_paused(False)
                if operation == "prepare_model":
                    self.service.save_settings(replace(self.service.load_settings(), embedding_provider="fastembed"))
                helper = ControlledHelper(blocked=True)
                with self.helpers_from(lambda: helper):
                    thread, outcome = self.background(action)
                    self.assertTrue(helper.started.wait(1))
                    self.service.set_automatic_paused(True)
                    self.assertFalse(helper.terminated)
                    self.assertNotIn("result", outcome)
                    helper.release.set()
                    thread.join(2)
                self.assertNotIn("error", outcome)
                self.assertFalse(helper.terminated)
                self.assertEqual(helper.payload["operation"], operation)

    def test_busy_management_ui_can_pause_immediately_without_disk_write_or_cancelling_management(self):
        controller = MemoryController(self.service)
        self.qt_objects.append(controller)
        spin_until(lambda: not controller.busy)
        panel = MemoryPanel(controller)
        self.qt_objects.append(panel)
        helper = ControlledHelper(blocked=True)
        before = self.service.config_path.read_bytes()
        with self.helpers_from(lambda: helper):
            controller.refresh()
            spin_until(helper.started.is_set)
            self.assertTrue(controller.busy)
            self.assertTrue(panel.pause_checkbox.isEnabled())
            start = time.monotonic()
            panel.pause_checkbox.click()
            self.assertLess(time.monotonic() - start, 0.1)
            self.assertTrue(controller.automatic_paused)
            self.assertTrue(self.service.automatic_paused)
            self.assertTrue(controller.settings.automatic)
            self.assertEqual(self.service.config_path.read_bytes(), before)
            self.assertFalse(helper.terminated)
            controller.settings_changed.emit(controller.settings)  # An older saved snapshot cannot unpause.
            self.assertTrue(panel.pause_checkbox.isChecked())
            self.assertIn("本次运行", panel.pause_checkbox.text())
            helper.release.set()
            spin_until(lambda: not controller.busy)
        self.assertTrue(panel.pause_checkbox.isChecked())

    def test_paused_automatic_memory_keeps_ordinary_chat_and_retrieval_working(self):
        self.service.set_automatic_paused(True)
        with MockApiServer() as server, self.helpers_from(ControlledHelper):
            self.settings.save_api_settings(ApiSettings(server.base_url, "fixture-chat", 5))
            chat = ChatController(self.settings, memory_service=self.service)
            self.qt_objects.append(chat)
            server.enqueue("自动提炼暂停后，我们仍然可以继续聊天。", delay=0.1)
            ticks = []
            timer = QTimer()
            timer.setInterval(5)
            timer.timeout.connect(lambda: ticks.append(True))
            timer.start()
            try:
                self.assertTrue(chat.send("请记住，我长期喜欢简洁回复。"))
                spin_until(lambda: not chat.busy)
                spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
                self.assertFalse(chat.error, chat.error)
                self.assertIn("仍然可以继续聊天", chat.messages[-1]["content"])
                self.assertGreater(len(ticks), 3)
                self.assertTrue(self.helpers)
                self.assertTrue(all(helper.payload["operation"] == "search" for helper in self.helpers))
            finally:
                timer.stop()

    def test_corrupt_identity_allows_disable_preferences_but_still_blocks_provider_changes(self):
        marker = self.service.data_dir / "embedding_identity.json"
        marker.write_text("{damaged", encoding="utf-8")
        self.service.save_settings(replace(self.service.load_settings(), enabled=False, automatic=False, timeout_seconds=4))
        self.assertFalse(self.service.load_settings().enabled)
        self.assertFalse(self.service.load_settings().automatic)
        with self.assertRaises(MemoryUnavailable):
            self.service.save_settings(replace(self.service.load_settings(), embedding_provider="fastembed"))
        self.assertEqual(marker.read_text(encoding="utf-8"), "{damaged")
        with self.helpers_from(lambda: ControlledHelper(result={"remaining_other_users": False})):
            self.service.clear()
        self.assertFalse(marker.exists())
        self.service.save_settings(replace(self.service.load_settings(), embedding_provider="fastembed"))

    def test_legacy_ui_controller_without_runtime_api_reports_error_without_saving(self):
        controller = MemoryControllerFake()
        panel = MemoryPanel(controller)
        self.qt_objects.append(panel)
        with patch.object(controller, "set_automatic_paused", None):
            panel.pause_checkbox.click()
        self.assertFalse(panel.pause_checkbox.isChecked())
        self.assertIn("重启", panel.error_label.text())
        self.assertFalse(controller.calls)


if __name__ == "__main__":
    unittest.main()
