"""Ordered GUI-thread progress with explicit request/session ownership."""

from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThread, QThreadPool, QTimer
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiError, ChatReply
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.chat_panel import ChatPanel
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until


class HeldClient:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.worker_thread = None
        self.error = False

    def complete_context(self, settings, key, messages, *, output_tokens):
        self.worker_thread = threading.get_ident()
        self.entered.set()
        self.release.wait(timeout=5)
        if self.error:
            raise ApiError("network", "合成连接失败 " + key)
        return ChatReply("这是合成回复。", "neutral")

    def test_connection(self, settings, key):
        self.worker_thread = threading.get_ident()
        self.entered.set()
        self.release.wait(timeout=5)
        return "连接成功：合成测试。"

    def shutdown(self):
        self.release.set()


class HeldMemory:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.entered = threading.Event()
        self.release = threading.Event()

    def load_settings(self):
        return SimpleNamespace(enabled=self.enabled, automatic=False, relevance_threshold=0.6)

    def search(self, query, limit=3):
        if self.enabled:
            self.entered.set()
            self.release.wait(timeout=5)
        return []


class ChatProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-chat-progress-")
        self.directory = Path(self.temporary.name)
        self.settings = isolated_settings(self.directory / "settings.ini", MemoryCredentialBackend())
        self.settings.save_api_settings(ApiSettings("https://example.invalid/v1", "synthetic-progress-model", 5))
        self.settings.save_api_key(LOCAL_TEST_KEY)
        self.client = HeldClient()
        self.memory = HeldMemory()
        self.store = ConversationStore(self.directory / "archive.sqlite3")
        self.chat = ChatController(self.settings, self.client, store=self.store, memory_service=self.memory)
        self.panel = ChatPanel(self.chat)
        self.panel.resize(850, 650)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.memory.release.set()
        self.client.release.set()
        self.chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.panel.close()
        self.panel.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def proceed_to_generation(self):
        self.assertTrue(self.chat.send("请解释当前项目的接口。"))
        spin_until(self.memory.entered.is_set)
        spin_until(lambda: "检索相关记忆" in self.chat.request_status)
        self.memory.release.set()
        spin_until(self.client.entered.is_set)
        spin_until(lambda: "生成回复" in self.chat.request_status)

    def test_save_memory_generate_save_order_and_public_updates_use_gui_thread(self):
        states, receiver_threads = [], []
        main_thread = threading.get_ident()
        self.chat.request_status_changed.connect(lambda text: (states.append(text), receiver_threads.append(threading.get_ident())))
        assistant_write, release_write = threading.Event(), threading.Event()
        original_complete = self.store.complete_request
        def complete(*args, **kwargs):
            assistant_write.set()
            release_write.wait(timeout=5)
            return original_complete(*args, **kwargs)
        try:
            with patch.object(self.store, "complete_request", side_effect=complete):
                self.proceed_to_generation()
                self.client.release.set()
                spin_until(assistant_write.is_set)
                spin_until(lambda: "保存回复" in self.chat.request_status)
                release_write.set()
                spin_until(lambda: not self.chat.busy)
            self.assertEqual(states, ["正在保存消息…", "正在检索相关记忆…", "正在生成回复…", "正在保存回复…", ""])
            self.assertTrue(all(value == main_thread for value in receiver_threads))
            self.assertEqual(QThread.currentThread(), self.app.thread())
            self.assertNotEqual(self.client.worker_thread, main_thread)
            self.assertEqual(self.chat.request_status, "")
            self.assertEqual(self.panel.status_label.text(), "准备就绪")
        finally:
            release_write.set()

    def test_disabled_memory_skips_retrieval_label_without_additional_requests(self):
        self.memory.enabled = False
        states = QSignalSpy(self.chat.request_status_changed)
        self.assertTrue(self.chat.send("记忆未启用也能聊天。"))
        spin_until(self.client.entered.is_set)
        spin_until(lambda: "生成回复" in self.chat.request_status)
        self.client.release.set()
        spin_until(lambda: not self.chat.busy)
        labels = [states.at(index)[0] for index in range(states.count())]
        self.assertFalse(any("检索相关记忆" in value for value in labels))
        self.assertEqual(len(self.store.load_messages(self.chat.current_conversation_id)), 2)

    def test_switching_session_shows_other_request_and_ignores_old_view_phase(self):
        self.proceed_to_generation()
        original = self.chat.current_conversation_id
        token = self.chat._request_id
        new_id = self.chat.new_conversation()
        self.assertIn("另一会话", self.chat.request_status)
        self.assertIn("当前会话尚未发送", self.panel.status_label.text())
        before = self.chat.request_status
        self.chat._request_phase_changed.emit(token, original, "saving_reply")
        self.app.processEvents()
        self.assertEqual(self.chat.request_status, before)
        self.assertEqual(self.chat.current_conversation_id, new_id)
        self.assertEqual(self.chat.messages, [])
        self.client.release.set()
        spin_until(lambda: not self.chat.busy)
        self.assertEqual(self.chat.request_status, "")
        self.assertEqual(self.chat.messages, [])
        self.assertEqual(self.panel.status_label.text(), "准备就绪")
        self.assertEqual(len(self.store.load_messages(original)), 2)

    def test_reopening_request_owner_restores_actual_phase(self):
        self.proceed_to_generation()
        original = self.chat.current_conversation_id
        self.chat.new_conversation()
        self.assertIn("另一会话", self.chat.request_status)
        self.assertTrue(self.chat.open_conversation(original))
        spin_until(lambda: not self.chat.history_loading)
        self.assertEqual(self.chat.request_status, "正在生成回复…")
        self.assertEqual(self.panel.status_label.text(), "正在生成回复…")

    def test_stale_token_wrong_owner_unknown_text_and_regressing_phase_are_rejected(self):
        self.proceed_to_generation()
        before = self.chat.request_status
        token, owner = self.chat._request_id, self.chat.current_conversation_id
        states = QSignalSpy(self.chat.request_status_changed)
        for old_token, cid, phase in ((token - 1, owner, "saving_reply"),
                                      (token, "other-session", "saving_reply"),
                                      (token, owner, LOCAL_TEST_KEY),
                                      (token, owner, "saving_message")):
            self.chat._request_phase_changed.emit(old_token, cid, phase)
        self.app.processEvents()
        self.assertEqual(self.chat.request_status, before)
        self.assertEqual(states.count(), 0)
        self.assertNotIn(LOCAL_TEST_KEY, self.chat.request_status)

    def test_finished_request_phase_cannot_overwrite_next_request(self):
        self.proceed_to_generation()
        old_token = self.chat._request_id
        owner = self.chat.current_conversation_id
        self.client.release.set()
        spin_until(lambda: not self.chat.busy)
        self.client.entered.clear()
        self.client.release.clear()
        self.assertTrue(self.chat.send("第二次请求。"))
        spin_until(self.client.entered.is_set)
        spin_until(lambda: "生成回复" in self.chat.request_status)
        self.chat._request_phase_changed.emit(old_token, owner, "saving_reply")
        self.app.processEvents()
        self.assertEqual(self.chat.request_status, "正在生成回复…")

    def test_connection_test_has_independent_label_and_returns_to_ready(self):
        states = QSignalSpy(self.chat.request_status_changed)
        self.assertTrue(self.chat.test_connection(self.settings.load_api_settings(), LOCAL_TEST_KEY))
        spin_until(self.client.entered.is_set)
        self.assertEqual(self.chat.request_status, "正在测试聊天服务连接…")
        self.chat.new_conversation()
        self.assertEqual(self.chat.request_status, "正在测试聊天服务连接…")
        self.assertNotIn("另一会话", self.panel.status_label.text())
        self.client.release.set()
        spin_until(lambda: not self.chat.busy)
        self.assertEqual([states.at(index)[0] for index in range(states.count())], ["正在测试聊天服务连接…", ""])
        self.assertEqual(self.chat.messages, [])

    def test_failure_resets_phase_and_never_includes_key_or_user_text(self):
        self.client.error = True
        states = QSignalSpy(self.chat.request_status_changed)
        self.proceed_to_generation()
        self.client.release.set()
        spin_until(lambda: not self.chat.busy)
        self.assertEqual(self.chat.request_status, "")
        self.assertTrue(self.chat.error)
        self.assertNotIn(LOCAL_TEST_KEY, self.chat.error)
        for index in range(states.count()):
            self.assertNotIn(LOCAL_TEST_KEY, states.at(index)[0])
            self.assertNotIn("请解释当前项目", states.at(index)[0])

    def test_shutdown_resets_phase_and_discards_late_worker_updates(self):
        self.proceed_to_generation()
        token, owner = self.chat._request_id, self.chat.current_conversation_id
        self.chat.shutdown()
        self.assertEqual(self.chat.request_status, "")
        self.chat._request_phase_changed.emit(token, owner, "saving_reply")
        self.app.processEvents()
        self.assertEqual(self.chat.request_status, "")
        self.assertFalse(self.chat.busy)


if __name__ == "__main__":
    unittest.main()
