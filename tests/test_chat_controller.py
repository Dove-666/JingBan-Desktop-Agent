import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiError, ChatReply
from desktop_pet.chat_controller import ChatController
from desktop_pet.settings_manager import ApiSettings, CredentialsError
from tests.mock_api import MockApiServer


KEY = "local-only-controller-token"


class MemorySettings:
    def __init__(self, settings=None, key=KEY):
        self.settings = settings or ApiSettings(model="test-model")
        self.key = key
        self.credential_error = False

    def load_api_settings(self):
        return self.settings

    def get_api_key(self):
        if self.credential_error:
            raise CredentialsError("系统凭据存储不可用。")
        return self.key


class RecordingClient:
    def __init__(self):
        self.requests = []
        self.fail_next = False
        self.reply = ChatReply("鲸鱼娘的回答", "happy")

    def complete(self, settings, key, messages):
        self.requests.append([dict(message) for message in messages])
        if self.fail_next:
            self.fail_next = False
            raise ApiError("network", "网络连接失败。")
        return self.reply

    def test_connection(self, settings, key):
        return "连接成功：真实测试替身。"


class ChatControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
        self.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)

    def make_controller(self, settings=None, client=None):
        controller = ChatController(settings or MemorySettings(), client)
        self.controllers.append(controller)
        return controller

    def spin_until(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            QTest.qWait(10)
            time.sleep(0.002)  # Let Python disk workers run on Windows as Qt pumps events.
        self.assertTrue(condition(), "Qt worker did not finish within the test deadline")

    def test_no_configuration_empty_and_oversized_input_are_rejected(self):
        controller = self.make_controller(MemorySettings(key=""), RecordingClient())
        self.assertFalse(controller.send(""))
        self.assertFalse(controller.send("  "))
        self.assertFalse(controller.send("hello"))
        self.assertIn("API Key", controller.error)
        self.assertFalse(controller.busy)
        self.assertEqual(controller.messages, [])
        self.assertFalse(controller.send("x" * 8001))
        self.assertIn("8000", controller.error)

    def test_credential_unavailable_is_reported_without_crash(self):
        settings = MemorySettings()
        settings.credential_error = True
        controller = self.make_controller(settings, RecordingClient())
        self.assertFalse(controller.send("你好"))
        self.assertIn("凭据", controller.error)

    def test_send_emotions_and_duplicate_request_guard(self):
        controller = self.make_controller(client=RecordingClient())
        emotions = QSignalSpy(controller.emotion_requested)
        self.assertTrue(controller.send("你好"))
        self.assertTrue(controller.busy)
        self.assertFalse(controller.send("重复"))
        self.assertFalse(controller.test_connection(ApiSettings(model="test-model"), KEY))
        self.spin_until(lambda: not controller.busy)
        self.assertEqual([emotions.at(i)[0] for i in range(emotions.count())], ["thinking", "happy"])
        self.assertEqual([message["role"] for message in controller.messages], ["user", "assistant"])

    def test_failed_turn_is_not_included_in_next_request_context(self):
        client = RecordingClient()
        client.fail_next = True
        controller = self.make_controller(client=client)
        controller.send("失败的消息")
        self.spin_until(lambda: not controller.busy)
        self.assertIn("网络", controller.error)
        self.assertTrue(controller.send("新的消息"))
        self.spin_until(lambda: not controller.busy)
        self.assertEqual(client.requests[-1], [{"role": "user", "content": "新的消息"}])
        self.assertEqual(controller.error, "")

    def test_complete_archive_is_separate_from_bounded_request_context(self):
        client = RecordingClient()
        controller = self.make_controller(client=client)
        for index in range(30):
            self.assertTrue(controller.send(f"消息{index}"))
            self.spin_until(lambda: not controller.busy)
        self.assertEqual(len(controller.messages), 60)
        self.assertLessEqual(len(client.requests[-1]), 17)  # 16 recent + complete current question.
        self.assertNotEqual(client.requests[-1][0]["content"], "消息0")
        self.assertEqual(client.requests[-1][-1]["content"], "消息29")
        copy = controller.messages
        copy[0]["content"] = "outside mutation"
        self.assertNotEqual(copy[0]["content"], controller.messages[0]["content"])

    def test_token_budget_preserves_question_and_refuses_impossible_input(self):
        client = RecordingClient()
        client.reply = ChatReply("r" * 24000)
        controller = self.make_controller(client=client)
        for index in range(3):
            self.assertTrue(controller.send("u" * 1000))
            self.spin_until(lambda: not controller.busy)
        self.assertEqual(client.requests[-1][-1]["content"], "u" * 1000)
        self.assertNotIn("r" * 24000, [item["content"] for item in client.requests[-1]])
        previous_messages = len(controller.messages)
        self.assertFalse(controller.send("u" * 8000))
        self.assertIn("预算", controller.error)
        self.assertEqual(len(controller.messages), previous_messages)

    def test_keys_never_enter_displayed_or_sent_conversation(self):
        client = RecordingClient()
        client.reply = ChatReply("密钥回显 " + KEY, "surprised")
        controller = self.make_controller(client=client)
        controller.send("我误粘贴了 " + KEY)
        self.spin_until(lambda: not controller.busy)
        self.assertNotIn(KEY, json.dumps(controller.messages))
        self.assertNotIn(KEY, json.dumps(client.requests))
        self.assertIn("已隐藏密钥", controller.messages[-1]["content"])

    def test_clear_removes_session_context(self):
        client = RecordingClient()
        controller = self.make_controller(client=client)
        controller.send("上一轮")
        self.spin_until(lambda: not controller.busy)
        controller.clear()
        self.assertEqual(controller.messages, [])
        self.spin_until(lambda: not controller.history_loading)
        controller.send("新的一轮")
        self.spin_until(lambda: not controller.busy)
        self.assertEqual(client.requests[-1], [{"role": "user", "content": "新的一轮"}])

    def test_real_delayed_http_request_does_not_block_qt_timer(self):
        with MockApiServer() as server:
            server.enqueue('{"reply":"成功","emotion":"happy"}', delay=0.4)
            settings = MemorySettings(ApiSettings(server.base_url, "test-model", 3))
            controller = self.make_controller(settings)
            ticks = []
            timer = QTimer()
            timer.setInterval(20)
            timer.timeout.connect(lambda: ticks.append(time.monotonic()))
            timer.start()
            start = time.monotonic()
            self.assertTrue(controller.send("你好"))
            self.assertLess(time.monotonic() - start, 0.1)
            controller.clear()  # Clearing during a request is explicitly guarded.
            self.assertIn("等待", controller.error)
            self.spin_until(lambda: not controller.busy)
            timer.stop()
            self.assertGreaterEqual(len(ticks), 10)
            self.assertEqual(controller.messages[-1]["content"], "成功")

    def test_connection_test_success_and_failure_follow_actual_http(self):
        with MockApiServer() as server:
            controller = self.make_controller()
            settings = ApiSettings(server.base_url, "test-model")
            server.enqueue("测试成功")
            self.assertTrue(controller.test_connection(settings, KEY))
            self.spin_until(lambda: not controller.busy)
            self.assertIn("连接成功", controller.connection_status)
            server.enqueue("unauthorized " + KEY, status=401)
            self.assertTrue(controller.test_connection(settings, KEY))
            self.spin_until(lambda: not controller.busy)
            self.assertIn("401", controller.connection_status)
            self.assertNotIn(KEY, controller.error)
            self.assertEqual(controller.messages, [])

    def test_shutdown_cancels_slow_http_request_without_wait_or_late_updates(self):
        with MockApiServer() as server:
            server.enqueue("这条回复不应显示", delay=3)
            controller = self.make_controller(MemorySettings(ApiSettings(server.base_url, "test-model", 30)))
            controller.send("将被取消的消息")
            self.spin_until(server.received.is_set)
            changed = QSignalSpy(controller.messages_changed)
            start = time.monotonic()
            controller.shutdown()
            self.assertLess(time.monotonic() - start, 0.1)
            self.assertFalse(controller.busy)
            self.assertFalse(controller.send("退出后不能发送"))
            self.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0, timeout=1.5)
            self.assertEqual(changed.count(), 0)
            self.assertEqual(len(controller.messages), 1)

    def test_application_exit_during_slow_request_has_no_running_thread_crash(self):
        # Run a separate Qt application so global thread-pool destruction and
        # interpreter shutdown are exercised, rather than just closing a widget.
        code = """
import sys
import time
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from desktop_pet.chat_controller import ChatController
from desktop_pet.settings_manager import ApiSettings
class Settings:
    def load_api_settings(self):
        return ApiSettings(sys.argv[1], 'test-model', 30)
    def get_api_key(self):
        return 'local-only-exit-test-token'
app = QApplication([])
controller = ChatController(Settings())
app.aboutToQuit.connect(controller.shutdown)
controller.send('exit test')
deadline = time.monotonic() + 5
timer = QTimer()
def maybe_quit():
    if Path(sys.argv[2]).exists():
        app.quit()
    elif time.monotonic() >= deadline:
        app.exit(9)
timer.timeout.connect(maybe_quit)
timer.start(10)
exit_code = app.exec()
print('clean exit')
raise SystemExit(exit_code)
"""
        with MockApiServer() as server, tempfile.TemporaryDirectory(prefix="aura-inflight-exit-") as directory:
            server.enqueue("这条回复来不及返回", delay=8)
            marker = Path(directory) / "quit-requested"
            process = subprocess.Popen([sys.executable, "-c", code, server.base_url, str(marker)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                # The HTTP server, rather than an assumed startup delay,
                # proves a request is in flight before asking Qt to exit.
                request_started = server.received.wait(timeout=4)
                start = time.monotonic()
                marker.write_text("quit", encoding="ascii")
                stdout, stderr = process.communicate(timeout=3)
                self.assertTrue(request_started, "No HTTP request began before the explicit startup deadline")
                self.assertEqual(process.returncode, 0, stderr)
                self.assertIn("clean exit", stdout)
                self.assertNotIn("QThread", stderr)
                self.assertLess(time.monotonic() - start, 2.5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=2)


if __name__ == "__main__":
    unittest.main()
