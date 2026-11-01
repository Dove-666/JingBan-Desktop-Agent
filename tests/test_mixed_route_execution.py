"""A wrapped routing reply must reach the existing guarded execution path."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiClient
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.settings_manager import SettingsManager, ApiSettings, CredentialStore
from tests.test_agent_chat_routing import FakeAgent, MemoryVault
from tests.test_remote_controller import spin_until


TASK = "打开任务管理器，看看有哪些正在运行的服务"
PUBLIC_REPLY = "好嘞～我这就去帮你打开任务管理器，看看正在运行的服务。"
WRAPPED_REPLY = PUBLIC_REPLY + "\n\n```json\n" + json.dumps({
    "reply": "好嘞～我这就去帮你打开任务管理器，把正在运行的服务拉出来看看，稍等一下哦。",
    "emotion": "thinking",
    "agent_request": {"kind": "computer", "task": "打开任务管理器，切换到服务选项卡，列出正在运行的服务"},
}, ensure_ascii=False) + "\n```"


class WrappedReplyApi(ApiClient):
    def __init__(self):
        super().__init__()
        self.calls = []

    def request_text(self, settings, key, messages, **kwargs):
        self.calls.append(messages)
        return WRAPPED_REPLY


class MixedRouteExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="aura-wrapped-routing-")
        root = Path(self.folder.name)
        self.settings = SettingsManager(QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat),
            CredentialStore(backend=MemoryVault()))
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "synthetic-router", 5))
        self.settings.save_api_key("synthetic-routing-key")
        self.settings.save_agent_settings(replace(self.settings.load_agent_settings(), enabled=True,
            computer_enabled=True, gui_model="synthetic-no-native-gui"))
        self.settings.save_agent_gui_api_key("synthetic-gui-key")
        self.api = WrappedReplyApi()
        self.agent = FakeAgent()
        self.chat = ChatController(self.settings, self.api, store=ConversationStore(root / "archive.sqlite3"),
            agent_service=self.agent, metadata_client_factory=lambda: None)
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)

    def tearDown(self):
        self.chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.chat.deleteLater()
        QApplication.processEvents()
        self.folder.cleanup()

    def test_exact_prose_and_fenced_protocol_reaches_computer_agent_and_saves_only_final_reply(self):
        self.assertTrue(self.chat.send(TASK, desktop_context=True, desktop_image_id=None,
            desktop_capture_unavailable_reason="own_window"))
        spin_until(lambda: not self.chat.busy)
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(len(self.agent.calls), 1)
        self.assertEqual(self.agent.calls[0]["request"].kind, "computer")
        self.assertEqual(self.agent.calls[0]["request"].task, TASK)
        records = self.chat.store.load_messages(self.chat.current_conversation_id)
        user = next(row for row in records if row["role"] == "user")
        assistant = next(row for row in records if row["role"] == "assistant")
        self.assertTrue(user["metadata"]["agent_execution_started"])
        self.assertEqual(user["metadata"]["request_status"], "complete")
        self.assertEqual(assistant["content"], self.agent.result.reply)
        self.assertNotIn('"agent_request"', assistant["content"])
        self.assertFalse(self.chat.retry(self.chat.current_conversation_id, user["id"]))

    def test_explanation_cannot_execute_the_same_wrapped_protocol(self):
        self.assertTrue(self.chat.send("只解释如何打开任务管理器，不要执行"))
        spin_until(lambda: not self.chat.busy)
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.agent.calls, [])
        user = next(row for row in self.chat.store.load_messages(self.chat.current_conversation_id)
            if row["role"] == "user")
        self.assertFalse(user["metadata"].get("agent_execution_started", False))


if __name__ == "__main__":
    unittest.main()
