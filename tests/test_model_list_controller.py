"""Independent model-list requests with synthetic workers and temporary storage.

No real API, saved application configuration or system credential vault is used.
"""

from pathlib import Path
import tempfile
import threading
import time
import unittest

from PySide6.QtCore import QEvent, QThread, QThreadPool, QTimer
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiError, ChatReply
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.settings_manager import ApiSettings


LIST_KEY = "synthetic-unsaved-model-list-key"


class MemorySettings:
    def __init__(self):
        self.api_reads = self.key_reads = 0
        self.saved = []

    def load_api_settings(self):
        self.api_reads += 1
        return ApiSettings("https://saved.invalid/v1", "synthetic-chat-model")

    def get_api_key(self):
        self.key_reads += 1
        return "synthetic-saved-chat-key"

    def save_api_settings(self, value):
        self.saved.append(("settings", value))

    def save_api_key(self, value):
        self.saved.append(("key", value))


class HeldClient:
    def __init__(self):
        self.started = {}
        self.releases = {}
        self.responses = {}
        self.calls = []
        self.chat_started = threading.Event()
        self.chat_release = threading.Event()
        self.connection_started = threading.Event()
        self.connection_release = threading.Event()
        self.shutdown_calls = 0

    def prepare(self, settings, response, *, released=False):
        self.started[settings.base_url] = threading.Event()
        self.releases[settings.base_url] = threading.Event()
        self.responses[settings.base_url] = response
        if released:
            self.releases[settings.base_url].set()

    def list_models(self, settings, key):
        self.calls.append((settings, key, threading.get_ident()))
        self.started[settings.base_url].set()
        if not self.releases[settings.base_url].wait(timeout=5):
            raise RuntimeError("Synthetic test worker was not released")
        response = self.responses[settings.base_url]
        if isinstance(response, Exception):
            raise response
        return response

    def complete(self, settings, key, messages):
        self.chat_started.set()
        self.chat_release.wait(timeout=5)
        return ChatReply("聊天请求仍正常完成。", "neutral")

    def test_connection(self, settings, key):
        self.connection_started.set()
        self.connection_release.wait(timeout=5)
        return "连接成功：合成连接测试。"

    def release_all(self):
        for release in self.releases.values():
            release.set()
        self.chat_release.set()
        self.connection_release.set()

    def shutdown(self):
        self.shutdown_calls += 1
        self.release_all()


class ModelListControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-model-list-")
        self.directory = Path(self.temporary.name)
        self.settings = MemorySettings()
        self.client = HeldClient()
        self.store = ConversationStore(self.directory / "archive.sqlite3")
        self.chat = ChatController(self.settings, self.client, store=self.store)
        self.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        # Readiness initialization may capture the saved API before discovery.
        # A model-list request must add no further saved-configuration reads.
        self.initial_api_reads = self.settings.api_reads
        self.initial_key_reads = self.settings.key_reads

    def tearDown(self):
        self.client.release_all()
        self.chat.shutdown()
        self.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.store.close()
        self.temporary.cleanup()

    def spin_until(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            QTest.qWait(5)
            time.sleep(0.002)
        self.assertTrue(condition(), "Synthetic Qt worker did not finish")

    def request_settings(self, suffix="models", **kwargs):
        return ApiSettings(base_url=f"https://{suffix}.invalid/v1", **kwargs)

    def chat_state(self):
        return (self.chat.busy, self.chat._request_id, self.chat._request_kind,
                self.chat._active_request_conversation, self.chat.request_status,
                self.chat.connection_status, self.chat.error)

    def test_model_is_optional_request_is_async_and_delivery_uses_gui_thread(self):
        request = self.request_settings()
        self.client.prepare(request, ["model-b", "model-a"])
        before = self.chat_state()
        results, threads, ticks = [], [], []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        start = time.monotonic()
        self.assertTrue(self.chat.fetch_models(request, "  " + LIST_KEY + "  ",
            lambda models, error: (results.append((models, error)), threads.append(threading.get_ident()))))
        self.assertLess(time.monotonic() - start, 0.1)
        self.spin_until(self.client.started[request.base_url].is_set)
        self.spin_until(lambda: len(ticks) >= 3)
        self.assertEqual(results, [])
        self.assertEqual(self.chat_state(), before)
        self.client.releases[request.base_url].set()
        self.spin_until(lambda: bool(results))
        timer.stop()
        self.assertEqual(results, [(["model-b", "model-a"], "")])
        self.assertEqual(threads, [threading.get_ident()])
        self.assertEqual(QThread.currentThread(), self.app.thread())
        self.assertNotEqual(self.client.calls[0][2], threads[0])
        self.assertEqual(self.client.calls[0][:2], (request, LIST_KEY))
        self.assertEqual(self.settings.api_reads, self.initial_api_reads)
        self.assertEqual(self.settings.key_reads, self.initial_key_reads)
        self.assertEqual(self.settings.saved, [])
        self.assertEqual(self.chat.messages, [])
        self.assertEqual(self.store.list_conversations(), [])
        self.assertNotIn(LIST_KEY.encode(), self.store.path.read_bytes())

    def test_two_views_get_their_own_out_of_order_results(self):
        first, second = self.request_settings("first"), self.request_settings("second")
        self.client.prepare(first, ["first-model"])
        self.client.prepare(second, ["second-model"])
        results = []
        self.assertTrue(self.chat.fetch_models(first, LIST_KEY, lambda models, error: results.append(("first", models, error))))
        self.assertTrue(self.chat.fetch_models(second, "synthetic-second-key", lambda models, error: results.append(("second", models, error))))
        self.spin_until(lambda: all(event.is_set() for event in self.client.started.values()))
        self.client.releases[second.base_url].set()
        self.spin_until(lambda: len(results) == 1)
        self.assertEqual(results, [("second", ["second-model"], "")])
        self.client.releases[first.base_url].set()
        self.spin_until(lambda: len(results) == 2)
        self.assertEqual(results[1], ("first", ["first-model"], ""))
        self.assertFalse(self.chat.busy)

    def test_fetch_during_active_chat_does_not_change_request_or_emit_chat_status(self):
        self.assertTrue(self.chat.send("合成聊天继续进行。"))
        self.spin_until(self.client.chat_started.is_set)
        self.spin_until(lambda: "生成回复" in self.chat.request_status)
        before = self.chat_state()
        signals = [QSignalSpy(signal) for signal in (self.chat.busy_changed, self.chat.error_changed,
                   self.chat.connection_status_changed, self.chat.request_status_changed)]
        request = self.request_settings()
        self.client.prepare(request, ["model-a"], released=True)
        results = []
        self.assertTrue(self.chat.fetch_models(request, LIST_KEY, lambda *value: results.append(value)))
        self.spin_until(lambda: bool(results))
        self.assertEqual(self.chat_state(), before)
        self.assertTrue(all(signal.count() == 0 for signal in signals))
        self.client.chat_release.set()
        self.spin_until(lambda: not self.chat.busy)
        self.assertEqual(self.chat.messages[-1]["content"], "聊天请求仍正常完成。")

    def test_fetch_and_connection_test_can_run_independently(self):
        request = self.request_settings()
        self.client.prepare(request, ["model-a"], released=True)
        self.assertTrue(self.chat.test_connection(ApiSettings(model="synthetic-test-model"), "synthetic-connection-key"))
        self.spin_until(self.client.connection_started.is_set)
        before = self.chat_state()
        results = []
        self.assertTrue(self.chat.fetch_models(request, LIST_KEY, lambda *value: results.append(value)))
        self.spin_until(lambda: bool(results))
        self.assertEqual(self.chat_state(), before)
        self.client.connection_release.set()
        self.spin_until(lambda: not self.chat.busy)
        self.assertIn("连接成功", self.chat.connection_status)

    def test_pending_model_request_does_not_prevent_starting_chat(self):
        request = self.request_settings()
        self.client.prepare(request, ["model-a"])
        results = []
        self.assertTrue(self.chat.fetch_models(request, LIST_KEY, lambda *value: results.append(value)))
        self.spin_until(self.client.started[request.base_url].is_set)
        self.assertFalse(self.chat.busy)
        self.assertTrue(self.chat.send("模型列表加载期间也能发消息。"))
        self.spin_until(self.client.chat_started.is_set)
        self.client.releases[request.base_url].set()
        self.spin_until(lambda: bool(results))
        self.assertTrue(self.chat.busy)
        self.client.chat_release.set()
        self.spin_until(lambda: not self.chat.busy)

    def test_invalid_configuration_reports_locally_without_touching_chat_or_keys(self):
        before = self.chat_state()
        for value in (object(), ApiSettings(base_url="invalid"),
                      self.request_settings(timeout_seconds=0), self.request_settings(reasoning_effort="invalid")):
            with self.subTest(configuration=type(value).__name__):
                results = []
                self.assertFalse(self.chat.fetch_models(value, LIST_KEY, lambda *result: results.append(result)))
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0][0], [])
                self.assertIn("配置", results[0][1])
                self.assertNotIn(LIST_KEY, results[0][1])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.chat_state(), before)
        self.assertEqual(self.settings.api_reads, self.initial_api_reads)
        self.assertEqual(self.settings.key_reads, self.initial_key_reads)
        self.assertEqual(self.settings.saved, [])

    def test_missing_or_unsafe_key_and_noncallable_callback_are_rejected(self):
        before = self.chat_state()
        for value in (None, 123, "", "   ", LIST_KEY + "\n", LIST_KEY + "\x7f"):
            with self.subTest(key_type=type(value).__name__):
                results = []
                self.assertFalse(self.chat.fetch_models(self.request_settings(), value, lambda *result: results.append(result)))
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0][0], [])
                self.assertIn("Key", results[0][1])
                self.assertNotIn(LIST_KEY, results[0][1])
        self.assertFalse(self.chat.fetch_models(self.request_settings(), LIST_KEY, None))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.chat_state(), before)

    def test_errors_are_static_and_provider_exception_text_never_leaves_worker(self):
        before = self.chat_state()
        cases = [(ApiError("authentication", "private-provider-body " + LIST_KEY), "认证"),
                 (ApiError("models_unsupported", "private-provider-body " + LIST_KEY), "手动"),
                 (ApiError("timeout", "private-provider-body " + LIST_KEY), "超时"),
                 (ApiError("unknown", "private-provider-body " + LIST_KEY), "失败"),
                 (RuntimeError("private-provider-body " + LIST_KEY), "失败")]
        for index, (error, expected) in enumerate(cases):
            with self.subTest(error_type=type(error).__name__, index=index):
                request = self.request_settings("failure" + str(index))
                self.client.prepare(request, error, released=True)
                results = []
                self.assertTrue(self.chat.fetch_models(request, LIST_KEY, lambda *result: results.append(result)))
                self.spin_until(lambda: bool(results))
                self.assertEqual(results[0][0], [])
                self.assertIn(expected, results[0][1])
                self.assertNotIn("private-provider-body", str(results))
                self.assertNotIn(LIST_KEY, str(results))
        self.assertEqual(self.chat_state(), before)
        self.assertEqual(self.chat.messages, [])
        self.assertEqual(self.settings.saved, [])

    def test_malformed_results_and_echoed_keys_are_not_exposed_as_models(self):
        cases = [({"data": []}, []), (["model-a", 12], []),
                 (["model-" + LIST_KEY], []), (["model-a", LIST_KEY], ["model-a"])]
        for index, (response, expected) in enumerate(cases):
            with self.subTest(index=index):
                request = self.request_settings("response" + str(index))
                self.client.prepare(request, response, released=True)
                results = []
                self.assertTrue(self.chat.fetch_models(request, LIST_KEY, lambda *result: results.append(result)))
                self.spin_until(lambda: bool(results))
                self.assertEqual(results[0][0], expected)
                self.assertEqual(bool(results[0][1]), not bool(expected))
                self.assertNotIn(LIST_KEY, str(results))

    def test_shutdown_cancels_list_work_and_suppresses_all_late_delivery(self):
        request = self.request_settings()
        self.client.prepare(request, ["late-model"])
        results = []
        self.assertTrue(self.chat.fetch_models(request, LIST_KEY, lambda *value: results.append(value)))
        self.spin_until(self.client.started[request.base_url].is_set)
        start = time.monotonic()
        self.chat.shutdown()
        self.assertLess(time.monotonic() - start, 0.1)
        self.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.app.processEvents()
        self.assertEqual(self.client.shutdown_calls, 1)
        self.assertEqual(results, [])
        self.assertFalse(self.chat.fetch_models(request, LIST_KEY, lambda *value: results.append(value)))
        self.assertFalse(self.chat.fetch_models(object(), "", lambda *value: results.append(value)))
        self.assertEqual(results, [])
        self.assertEqual(len(self.client.calls), 1)


if __name__ == "__main__":
    unittest.main()
