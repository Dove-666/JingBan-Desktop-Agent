"""Chat sessions wired to real Mem0 OSS and an on-disk local Qdrant store.

Only model HTTP responses and the OS credential vault are deterministic test
fixtures. The memory service, extractor, persistence, vector search, filtering,
deletion and asynchronous ChatController integration all run their real code.
"""

from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from uuid import uuid4

from PySide6.QtCore import QSettings, QThreadPool, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ChatReply
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.memory import MemoryService
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
from tests.test_memory_service import LocalMemoryModels, Vault


class RecordingContextClient:
    """Inspect the complete model context, including auxiliary system blocks."""

    def __init__(self):
        self.requests = []

    def complete_context(self, settings, key, messages, *, output_tokens):
        self.requests.append(deepcopy(messages))
        return ChatReply("收到，这次回复已经保存。", "happy")


@unittest.skipUnless(importlib.util.find_spec("mem0"), "optional mem0ai SDK not installed")
class Mem0ChatIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.vault = Vault()
        self.settings = SettingsManager(
            QSettings(str(self.path / "settings.ini"), QSettings.Format.IniFormat),
            CredentialStore(backend=self.vault),
        )
        self.controllers = []
        self.services = []
        self.provider = LocalMemoryModels().__enter__()
        self.settings.save_api_settings(ApiSettings(self.provider.base_url, "fixture-chat"))
        self.settings.save_api_key("fixture-chat-key")
        self.memory = self.make_memory()
        self.memory.save_settings(replace(
            self.memory.load_settings(), enabled=True, automatic=True,
            embedding_base_url=self.provider.base_url,
            embedding_model="fixture-embedding", embedding_dimensions=8,
        ), embedding_key="fixture-embedding-key")

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
        for service in self.services:
            service.shutdown()
        self.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.provider.__exit__()
        self.temp.cleanup()

    def make_memory(self):
        result = MemoryService(
            self.settings, self.path / "memory",
            llm_credentials=CredentialStore(account="mem0-llm-key", backend=self.vault),
            embedding_credentials=CredentialStore(account="mem0-embedding-key", backend=self.vault),
        )
        self.services.append(result)
        return result

    def make_chat(self, *, memory=None):
        client = RecordingContextClient()
        chat = ChatController(
            self.settings, client,
            store=ConversationStore(self.path / "conversations.sqlite3"),
            memory_service=memory or self.memory,
        )
        self.controllers.append(chat)
        return chat, client

    def spin_until(self, predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            QTest.qWait(10)
            time.sleep(0.002)
        self.assertTrue(predicate(), "Qt/Mem0 integration did not finish before the deadline")

    def settle(self):
        self.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        # A finished signal can enqueue metadata/extraction work on the GUI
        # thread; consume those callbacks before deciding the system is idle.
        QTest.qWait(25)
        self.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)

    def send(self, chat, text):
        self.assertTrue(chat.send(text), chat.error)
        self.spin_until(lambda: not chat.busy)
        self.settle()
        self.assertEqual("", chat.error)
        self.assertEqual("assistant", chat.messages[-1]["role"])

    def test_real_auto_extraction_restart_cross_session_relevance_and_delete(self):
        chat, client = self.make_chat()
        first_id = chat.current_conversation_id
        self.send(chat, "记住，我长期偏好大字体评论气泡。")
        saved = self.memory.list_memories()
        self.assertEqual(1, len(saved))
        self.assertEqual(first_id, saved[0]["metadata"]["conversation_id"])
        self.assertEqual("explicit_user_fact", saved[0]["metadata"]["source"])
        extraction = [row for row in self.provider.requests if row["path"].endswith("chat/completions")]
        self.assertEqual(1, len(extraction))
        self.assertNotIn("收到，这次回复已经保存", json.dumps(extraction, ensure_ascii=False))

        # Simulate a full application restart: a new memory service reopens
        # the on-disk vectors, and a new ChatController starts a clean session.
        chat.shutdown()
        self.memory.shutdown()
        reopened = self.make_memory()
        fresh, client = self.make_chat(memory=reopened)
        self.assertNotEqual(first_id, fresh.current_conversation_id)
        self.assertEqual([], fresh.messages)
        self.send(fresh, "你还记得我偏好的字体吗？")
        request = client.requests[-1]
        system = "\n".join(row["content"] for row in request if row["role"] == "system")
        self.assertIn("少量相关长期记忆", system)
        self.assertIn("用户长期偏好大字体评论气泡", system)
        self.assertEqual(["你还记得我偏好的字体吗？"],
                         [row["content"] for row in request if row["role"] == "user"])
        self.assertNotIn("记住，我长期偏好", json.dumps(request, ensure_ascii=False))

        fresh.new_conversation()
        self.send(fresh, "这个项目进度怎样？")
        self.assertNotIn("少量相关长期记忆", json.dumps(client.requests[-1], ensure_ascii=False))
        self.assertNotIn("大字体评论气泡", json.dumps(client.requests[-1], ensure_ascii=False))

        reopened.delete(saved[0]["id"])
        fresh.new_conversation()
        self.send(fresh, "你还记得我偏好的字体吗？")
        self.assertEqual([], reopened.search("偏好的字体"))
        self.assertNotIn("少量相关长期记忆", json.dumps(client.requests[-1], ensure_ascii=False))
        # Deleting memory does not remove the separately persisted chat archive.
        self.assertEqual(2, len(fresh.store.load_messages(first_id)))

    def test_screen_comment_never_enters_memory_and_paused_user_fact_is_not_extracted(self):
        chat, client = self.make_chat()
        saved = []
        chat.screen_comment_finished.connect(lambda reference, error: saved.append((reference, error)))
        reference = chat.record_screen_comment(
            chat.current_conversation_id,
            "这个网页上的猫真可爱！", event_id=str(uuid4()),
            app_name="synthetic-browser.exe", context_summary="网页作者表示长期喜欢猫。",
            event_type="interesting", emotion="happy",
        )
        self.assertIsNotNone(reference)
        self.spin_until(lambda: bool(saved))
        self.settle()
        self.assertEqual("", saved[0][1])
        self.assertEqual("proactive_screen", chat.messages[0]["source"])
        self.assertEqual([], self.provider.requests)
        self.assertEqual([], self.memory.list_memories())

        self.memory.save_settings(replace(self.memory.load_settings(), automatic=False))
        self.send(chat, "记住，我长期偏好大字体评论气泡。")
        self.assertTrue(any(row["path"].endswith("/embeddings") for row in self.provider.requests))
        self.assertFalse(any(row["path"].endswith("chat/completions") for row in self.provider.requests))
        self.assertEqual([], self.memory.list_memories())
        # The screen summary remains useful current-session context, without
        # being mistaken for the user's own durable preference.
        request = json.dumps(client.requests[-1], ensure_ascii=False)
        self.assertIn("网页作者表示长期喜欢猫", request)
        self.assertNotIn("少量相关长期记忆", request)

    def test_actual_mem0_provider_failure_keeps_gui_live_and_commits_chat(self):
        self.provider.fail = True
        self.provider.delay = 0.15
        chat, client = self.make_chat()
        ticks = []
        timer = QTimer()
        timer.setInterval(20)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        try:
            start = time.monotonic()
            self.assertTrue(chat.send("这个项目进度怎样？"))
            self.assertLess(time.monotonic() - start, 0.15)
            self.spin_until(lambda: not chat.busy)
            self.settle()
        finally:
            timer.stop()
        self.assertGreaterEqual(len(ticks), 3)
        self.assertEqual("", chat.error)
        self.assertIn("长期记忆检索暂不可用", chat.context_usage)
        self.assertEqual(2, len(chat.store.load_messages(chat.current_conversation_id)))
        self.assertEqual("assistant", chat.messages[-1]["role"])
        self.assertTrue(client.requests)
        self.assertNotIn("少量相关长期记忆", json.dumps(client.requests[-1], ensure_ascii=False))
        self.assertTrue(any(row["path"].endswith("/embeddings") for row in self.provider.requests))


if __name__ == "__main__":
    unittest.main()
