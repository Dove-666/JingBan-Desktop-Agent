"""Persisted screen-comment -> linked bubble -> follow-up Qt/HTTP flow.

Foreground metadata and images are synthetic. Network calls go only to the
loopback model fixture, with temporary SQLite and in-memory credentials.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.conversations import ConversationStore, StoreError
from desktop_pet.memory.settings import MemorySettings
from desktop_pet.perception.settings import PerceptionSettings
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_perception_integration import AdjustableClock, SyntheticScene, event_json
from tests.test_assistant_ui import eval_js, wait_js, set_dom_text, click_dom


class DisabledTestMemory:
    """Keep this screen-integration suite away from the OS credential vault."""
    def __init__(self):
        self.extract_calls = []

    def load_settings(self):
        return MemorySettings()

    def credential_status(self):
        return "仅使用测试内存凭据，长期记忆关闭。"

    def search(self, *args, **kwargs):
        return []

    def extract(self, *args, **kwargs):
        self.extract_calls.append((args, kwargs))
        return []

    def shutdown(self):
        pass


class PersistentScreenFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-persistent-screen-")
        self.path = Path(self.directory.name)
        self.settings = isolated_settings(self.path / "settings.ini", MemoryCredentialBackend())
        self.test_memory = DisabledTestMemory()
        self.memory_patch = patch("desktop_pet.application_controller.MemoryService", return_value=self.test_memory)
        self.memory_patch.start()
        self.sessions = []
        self.session = self.make_session()
        self.scene = SyntheticScene()
        self.clock = AdjustableClock()
        self.session.perception.observer = self.scene.observer()
        self.session.perception._clock = self.clock
        self.session.show()
        self.app.processEvents()

    def make_session(self):
        session = ApplicationController(PetConfig(), settings=self.settings)
        session.pet_controller.asset_player.directory = self.path / "empty-animations"
        self.sessions.append(session)
        return session

    def tearDown(self):
        for session in self.sessions:
            session.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for session in self.sessions:
            for window in (session.pet, session.debug_panel, session.chat_window):
                if window is not None:
                    window.deleteLater()
            session.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.memory_patch.stop()
        self.directory.cleanup()

    def configure(self, server):
        self.settings.save_api_settings(ApiSettings(server.base_url, "test-model", 5))
        self.settings.save_api_key(LOCAL_TEST_KEY)
        self.assertTrue(self.session.perception.update_settings(
            PerceptionSettings(enabled=True, allow_remote=True)))
        self.session.perception._timer.stop()

    def create_comment(self, server, **kwargs):
        server.enqueue(event_json(**kwargs))
        self.assertTrue(self.session.perception.poll())
        spin_until(lambda: not self.session.perception.busy)
        spin_until(lambda: bool(self.session.chat.messages))
        return self.session.chat.messages[-1]

    def test_proactive_comment_is_atomic_assistant_event_without_open_chat(self):
        with MockApiServer() as server:
            self.configure(server)
            self.assertIsNone(self.session.chat_window)
            conversation_id = self.session.chat.current_conversation_id
            message = self.create_comment(server)
            self.assertEqual(message["role"], "assistant")
            self.assertEqual(message["source"], "proactive_screen")
            self.assertEqual(message["conversation_id"], conversation_id)
            event = message["screen_event"]
            self.assertEqual(message["screen_event_id"], event["id"])
            self.assertEqual(event["conversation_id"], conversation_id)
            self.assertEqual(event["message_id"], message["id"])
            self.assertEqual(event["context_summary"], "合成画面出现新图表")
            self.assertEqual(event["event_type"], "interesting")
            self.assertEqual(event["app_name"], "synthetic-editor.exe")
            self.assertTrue(event["observed_at"])
            self.assertIsNone(self.session.chat_window)
            self.assertTrue(self.session.pet.bubble.isVisible())
            persisted = ConversationStore(self.session.chat.store.path).load_messages(conversation_id)
            self.assertEqual(persisted[0]["id"], message["id"])
            self.assertEqual(persisted[0]["content"], "这个合成图表很有趣！")
            self.assertNotIn("image", persisted[0]["metadata"])
            self.assertNotIn("image", event["metadata"])
            self.assertEqual(self.session.chat.store.get_conversation(conversation_id)["message_count"], 1)
            self.assertEqual(self.test_memory.extract_calls, [])

    def test_bubble_qt_click_opens_exact_conversation_and_followup_has_event_context(self):
        with MockApiServer() as server:
            self.configure(server)
            message = self.create_comment(server)
            conversation_id = message["conversation_id"]
            self.session.chat.new_conversation()
            focused = QSignalSpy(self.session.chat.message_focus_requested)
            bubble = self.session.pet.bubble
            self.assertFalse(bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
            self.assertFalse(bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput)
            QTest.mouseClick(bubble, Qt.MouseButton.LeftButton, pos=bubble.rect().center())
            spin_until(lambda: self.session.chat_window is not None and not self.session.chat.history_loading)
            self.assertTrue(self.session.chat_window.isVisible())
            self.assertEqual(self.session.chat.current_conversation_id, conversation_id)
            self.assertEqual(focused.count(), 1)
            self.assertEqual(focused.at(0)[0], message["id"])
            panel = self.session.chat_window.chat_panel
            spin_until(lambda: panel.web_view is not None and panel._frontend_ready)
            wait_js(panel, "document.body.innerText.includes('来自屏幕感知')")
            wait_js(panel, "Boolean(document.querySelector('.event-details'))")
            self.assertIn("合成画面出现新图表", eval_js(panel, "document.querySelector('.event-details').textContent"))
            server.enqueue('{"reply":"我们继续讨论刚才的合成图表。","emotion":"thinking"}')
            set_dom_text(panel, "textarea", "你刚才说的那个画面有什么值得注意？")
            click_dom(panel, "[data-testid=send-button]")
            self.assertTrue(self.session.chat.busy)
            spin_until(lambda: not self.session.chat.busy)
            request = server.requests[-1]["body"]
            self.assertIn("合成画面出现新图表", json.dumps(request, ensure_ascii=False))
            self.assertIn(message["screen_event_id"], json.dumps(request, ensure_ascii=False))
            self.assertEqual(len(self.session.chat.store.load_messages(conversation_id)), 3)
            self.session.chat_window.close()
            self.session.open_chat_window()
            self.assertEqual(self.session.chat.current_conversation_id, conversation_id)
            wait_js(panel, "document.body.innerText.includes('我们继续讨论')")

    def test_new_session_never_copies_old_events_and_restart_restores_only_on_request(self):
        with MockApiServer() as server:
            self.configure(server)
            message = self.create_comment(server)
            original_id, event_id = message["conversation_id"], message["screen_event_id"]
            new_id = self.session.chat.new_conversation()
            server.enqueue('{"reply":"这里是独立的新会话。","emotion":"neutral"}')
            self.assertTrue(self.session.chat.send("新的话题"))
            spin_until(lambda: not self.session.chat.busy)
            request = json.dumps(server.requests[-1]["body"], ensure_ascii=False)
            self.assertNotIn("合成画面出现新图表", request)
            self.assertNotIn(event_id, request)
            self.assertEqual(self.session.chat.store.recent_screen_events(new_id), [])
            self.session.shutdown()
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            restarted = self.make_session()
            restarted.perception._timer.stop()
            # Do not show this restart instance: its observer is never polled.
            self.assertNotIn(restarted.chat.current_conversation_id, (original_id, new_id))
            self.assertEqual(restarted.chat.messages, [])
            self.assertTrue(restarted.chat.open_conversation(original_id))
            spin_until(lambda: not restarted.chat.history_loading)
            self.assertEqual(restarted.chat.messages[0]["screen_event_id"], event_id)
            self.assertEqual(restarted.chat.messages[0]["source"], "proactive_screen")
            self.assertEqual(len(restarted.chat.messages), 1)

    def test_delayed_screen_analysis_keeps_explicit_original_conversation(self):
        with MockApiServer() as server:
            self.configure(server)
            original_id = self.session.chat.current_conversation_id
            server.enqueue(event_json(), delay=0.25)
            self.assertTrue(self.session.perception.poll())
            spin_until(server.received.is_set)
            replacement = self.session.chat.new_conversation()
            spin_until(lambda: not self.session.perception.busy)
            saved = self.session.chat.store.load_messages(original_id)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["source"], "proactive_screen")
            self.assertEqual(self.session.chat.current_conversation_id, replacement)
            self.assertEqual(self.session.chat.messages, [])
            self.assertEqual(self.session.chat.store.load_messages(replacement), [])
            bubble = self.session.pet.bubble
            QTest.mouseClick(bubble, Qt.MouseButton.LeftButton, pos=bubble.rect().center())
            spin_until(lambda: not self.session.chat.history_loading)
            self.assertEqual(self.session.chat.current_conversation_id, original_id)

    def test_quiet_observation_is_not_chat_message_or_long_term_memory(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(event_json(kind="none", reply="", importance=0.0))
            self.session.perception.poll()
            spin_until(lambda: not self.session.perception.busy)
            self.assertEqual(self.session.chat.messages, [])
            self.assertEqual(self.session.chat.store.list_conversations(), [])
            self.assertEqual(self.session.perception.history[-1]["动作"], "quiet")
            self.assertFalse(self.session.pet.bubble.isVisible())
            self.assertEqual(self.test_memory.extract_calls, [])

    def test_deleting_session_cancels_pending_analysis_without_resurrection(self):
        with MockApiServer() as server:
            self.configure(server)
            cid = self.session.chat.current_conversation_id
            self.session.chat.store.append_message(cid, "user", "已有会话", "user")
            server.enqueue(event_json(), delay=0.3)
            self.assertTrue(self.session.perception.poll())
            spin_until(server.received.is_set)
            self.assertTrue(self.session.chat.delete_conversation(cid))
            self.assertFalse(self.session.perception.busy)
            self.assertIsNone(self.session.chat.record_screen_comment(cid, "迟到评论",
                event_id="late-deleted", app_name="synthetic", context_summary="旧画面", event_type="interesting"))
            spin_until(lambda: self.session.chat.current_conversation_id != cid)
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            self.assertIsNone(self.session.chat.store.get_conversation(cid))
            self.assertEqual(self.session.chat.store.load_messages(cid), [])
            self.assertFalse(self.session.pet.bubble.isVisible())
            self.assertNotEqual(self.session.chat.current_conversation_id, cid)

    def test_clearing_session_cancels_old_analysis_but_allows_new_chat(self):
        with MockApiServer() as server:
            self.configure(server)
            cid = self.session.chat.current_conversation_id
            self.session.chat.store.append_message(cid, "user", "清空前的内容", "user")
            server.enqueue(event_json(), delay=0.3)
            self.assertTrue(self.session.perception.poll())
            spin_until(server.received.is_set)
            self.session.chat.clear()
            self.assertFalse(self.session.perception.busy)
            spin_until(lambda: not self.session.chat.history_loading
                       and QThreadPool.globalInstance().activeThreadCount() == 0)
            self.assertEqual(self.session.chat.store.load_messages(cid), [])
            self.assertEqual(self.session.chat.current_conversation_id, cid)
            self.assertFalse(self.session.pet.bubble.isVisible())
            server.enqueue('{"reply":"这是清空后的新聊天","emotion":"neutral"}')
            self.assertTrue(self.session.chat.send("从新内容开始"))
            spin_until(lambda: not self.session.chat.busy)
            self.assertEqual(len(self.session.chat.store.load_messages(cid)), 2)
            self.assertNotIn("清空前", json.dumps(server.requests[-1]["body"], ensure_ascii=False))

    def test_failed_comment_transaction_does_not_show_unarchived_bubble(self):
        with MockApiServer() as server:
            self.configure(server)
            def reject(*args, **kwargs):
                raise StoreError("测试存储不可用。")
            with patch.object(self.session.chat.store, "append_screen_comment", side_effect=reject):
                server.enqueue(event_json())
                self.assertTrue(self.session.perception.poll())
                spin_until(lambda: not self.session.perception.busy)
            self.assertFalse(self.session.pet.bubble.isVisible())
            self.assertEqual(self.session.chat.messages, [])
            self.assertEqual(self.session.chat.store.list_conversations(), [])
            self.assertIn("屏幕评论未保存", self.session.perception.status)


if __name__ == "__main__":
    unittest.main()
