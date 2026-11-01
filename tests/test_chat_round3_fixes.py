"""User recovery regressions with disposable archives and synthetic services."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QObject, QThreadPool, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ChatReply
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_assistant_ui import wait_js, wait_until


class Client:
    def __init__(self):
        self.contexts = []
    def complete_context(self, settings, key, messages, **kwargs):
        self.contexts.append(deepcopy(messages))
        return ChatReply("这是一条新的合成回答", "happy")


class HeldMemory:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
    def load_settings(self):
        return SimpleNamespace(enabled=True, automatic=False, relevance_threshold=.6)
    def search(self, query, limit=3):
        self.entered.set()
        self.release.wait(5)
        return []


class Visual(QObject):
    runtime_changed = Signal(object)
    settings_requested = Signal()
    def __init__(self, paused=False):
        super().__init__()
        self.paused, self.calls = paused, []
    def runtime_snapshot(self):
        return {"paused": self.paused, "enabled": True, "allowRemote": False, "cameraActive": not self.paused,
                "analysisError": "合成识别错误", "permissionSaveFailed": True,
                "persistenceWarning": "合成保存警告"}
    def set_paused(self, paused):
        self.paused = paused
        self.calls.append(paused)
        self.runtime_changed.emit(self.runtime_snapshot())


class ChatRound3Fixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-chat-round3-fix-")
        self.base = Path(self.temp.name)
        self.settings = isolated_settings(self.base / "fixture.ini", MemoryCredentialBackend())
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "synthetic-model", 3))
        self.settings.save_api_key(LOCAL_TEST_KEY)
        self.controllers, self.panels, self.held = [], [], []
    def chat(self, path=None, client=None, **kwargs):
        chat = ChatController(self.settings, client or Client(),
                              store=ConversationStore(path or self.base / "archive.sqlite3"),
                              metadata_client_factory=lambda: None, **kwargs)
        self.controllers.append(chat)
        return chat
    def open(self, chat, cid):
        self.assertTrue(chat.open_conversation(cid))
        spin_until(lambda: not chat.history_loading)
    def panel(self, chat):
        panel = AssistantChatPanel(chat)
        self.panels.append(panel)
        return panel
    def tearDown(self):
        for memory in self.held:
            memory.release.set()
        for chat in self.controllers:
            chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        for chat in self.controllers:
            chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def test_legacy_v2_pending_saved_reply_recovers_complete_without_request_or_speech(self):
        path = self.base / "legacy.sqlite3"
        with sqlite3.connect(path) as connection:
            ConversationStore._migrate_v1(connection)
            ConversationStore._migrate_v2(connection)
            connection.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES ('legacy','旧记录','2026','2026')")
            connection.execute("""INSERT INTO messages(id,conversation_id,role,content,created_at,source,metadata)
                VALUES ('old-user','legacy','user','旧问题','2026','user','{"request_status":"pending"}')""")
            connection.execute("""INSERT INTO messages(id,conversation_id,role,content,created_at,source,metadata)
                VALUES ('old-reply','legacy','assistant','已经保存的回答','2026','assistant',?)""",
                (json.dumps({"reply_to": "old-user", "emotion": "sleepy"}),))
            connection.execute("PRAGMA user_version=2")
        client = Client()
        chat = self.chat(path, client)
        replies = QSignalSpy(chat.reply_received)
        self.open(chat, "legacy")
        self.assertEqual(chat.messages[0]["metadata"]["request_status"], "complete")
        self.assertEqual(chat.store.load_messages("legacy")[0]["metadata"]["request_status"], "complete")
        self.assertFalse(chat.retry("legacy", "old-user"))
        self.assertEqual(client.contexts, [])
        self.assertEqual(replies.count(), 0)
        self.assertEqual(len(chat.messages), 2)
        self.assertEqual(chat.messages[1]["metadata"]["emotion"], "sleepy")

    def test_stale_retry_reconciles_reply_saved_after_history_load_without_calling_model(self):
        client = Client()
        chat = self.chat(client=client)
        chat.store.append_message("one", "user", "旧失败问题", "user", message_id="u",
                                  metadata={"request_status": "failed"})
        self.open(chat, "one")
        chat.store.append_message("one", "assistant", "历史载入后实际已保存的回复", "assistant",
                                  metadata={"reply_to": "u", "emotion": "sleepy"})
        replies = QSignalSpy(chat.reply_received)
        emotions = QSignalSpy(chat.emotion_requested)
        self.assertTrue(chat.retry("one", "u"))
        spin_until(lambda: not chat.busy)
        self.assertEqual(client.contexts, [])
        self.assertEqual(replies.count(), 0)
        self.assertEqual(emotions.at(emotions.count() - 1)[0], "neutral")
        self.assertEqual(chat.messages[0]["metadata"]["request_status"], "complete")
        self.assertEqual(len(chat.messages), 2)

    def test_all_unfinished_statuses_are_excluded_from_titles_summaries_and_later_requests(self):
        for status in ("pending", "failed", "cancelled", "interrupted"):
            with self.subTest(status=status):
                client = Client()
                chat = self.chat(self.base / (status + ".sqlite3"), client)
                marker = "UNFINISHED_FIXTURE_" + status
                chat.store.append_message(status, "user", marker, "user", metadata={"request_status": status})
                self.open(chat, status)
                self.assertTrue(chat.send("这是已完成的新话题"))
                spin_until(lambda: not chat.busy and not chat._metadata_active)
                conversation = chat.store.get_conversation(status)
                self.assertNotIn(marker, conversation["title"])
                self.assertNotIn(marker, conversation["summary"])
                self.assertTrue(chat.send("继续这个已完成的话题"))
                spin_until(lambda: not chat.busy)
                self.assertNotIn(marker, json.dumps(client.contexts, ensure_ascii=False))
                self.assertEqual(chat.store.load_messages(status)[0]["content"], marker)

    def test_old_contaminated_summary_does_not_bypass_unfinished_filter(self):
        client = Client()
        chat = self.chat(client=client)
        marker = "OLD_CANCELLED_SUMMARY_FIXTURE"
        chat.store.append_message("one", "user", marker, "user", metadata={"request_status": "cancelled"})
        chat.store.append_message("one", "user", "保留的已完成问题", "user", metadata={"request_status": "complete"})
        chat.store.append_message("one", "assistant", "保留的已完成回答", "assistant")
        chat.store.update_metadata("one", title="用户现有标题", summary="旧概况包含" + marker)
        self.open(chat, "one")
        self.assertTrue(chat.send("新一轮的问题"))
        spin_until(lambda: not chat.busy and not chat._metadata_active)
        self.assertNotIn(marker, json.dumps(client.contexts, ensure_ascii=False))
        self.assertNotIn(marker, chat.store.get_conversation("one")["summary"])
        self.assertEqual(chat.store.get_conversation("one")["title"], "用户现有标题")

    def test_stopping_memory_search_explains_retry_wait_then_automatically_enables_it(self):
        memory, client = HeldMemory(), Client()
        self.held.append(memory)
        chat = self.chat(client=client, memory_service=memory)
        panel = self.panel(chat)
        panel._activate_native()
        self.assertTrue(chat.send("先检索再停止"))
        spin_until(memory.entered.is_set)
        self.assertTrue(chat.stop_request(chat.current_conversation_id))
        state = panel.web_state()
        self.assertFalse(state["busy"])
        self.assertFalse(state["messages"][0]["retryAvailable"])
        self.assertIn("结束后", state["messages"][0]["retryStatus"])
        self.assertFalse(state["errorRetryAvailable"])
        self.assertFalse(panel.retry_button.isEnabled())
        self.assertNotIn("aura-retry:", panel.transcript.toHtml())
        changed = QSignalSpy(chat.retry_availability_changed)
        memory.release.set()
        spin_until(lambda: not chat._requests)
        self.assertGreater(changed.count(), 0)
        self.assertTrue(panel.web_state()["messages"][0]["retryAvailable"])
        self.assertTrue(panel.retry_button.isEnabled())
        self.assertIn("aura-retry:", panel.transcript.toHtml())
        self.assertTrue(chat.retry(chat.current_conversation_id, chat.messages[0]["id"]))
        spin_until(lambda: not chat.busy)
        self.assertEqual(len(client.contexts), 1)

    def test_partial_webengine_failure_rebuilds_full_channel_and_retains_native_draft(self):
        chat = self.chat()
        panel = self.panel(chat)
        cid = chat.current_conversation_id
        panel.dispatch_web_action("draft", {"conversationId": cid, "text": "失败前的网页草稿"})
        with patch("PySide6.QtWebEngineCore.QWebEngineProfile", side_effect=RuntimeError("fixture profile failure")):
            panel._ensure_web()
        self.assertIsNone(panel.web_view)
        self.assertFalse(panel.compatibility_button.isHidden())
        panel._activate_native()
        self.assertEqual(panel.input.toPlainText(), "失败前的网页草稿")
        panel.input.setPlainText("兼容模式继续编辑的草稿")
        panel.resize(900, 700)
        panel.show()
        panel._reload_web()
        wait_until(lambda: panel._frontend_ready)
        self.assertTrue(panel._web_initialized)
        self.assertIsNotNone(panel.web_view.page().webChannel())
        wait_js(panel, "document.querySelector('#message-input')?.value === '兼容模式继续编辑的草稿'")

    def test_camera_runtime_pause_and_settings_are_independent_of_screen_pause(self):
        chat = self.chat()
        screen, camera = Visual(paused=True), Visual()
        chat.perception_controller, chat.camera_controller = screen, camera
        panel = self.panel(chat)
        states = QSignalSpy(panel.bridge.stateChanged)
        settings = QSignalSpy(camera.settings_requested)
        self.assertEqual(panel.web_state()["camera"]["analysisError"], "合成识别错误")
        self.assertTrue(panel.dispatch_web_action("setCameraPaused", {"paused": True}))
        spin_until(lambda: states.count() > 0)
        self.assertTrue(panel.web_state()["camera"]["paused"])
        self.assertTrue(screen.paused)
        self.assertTrue(panel.dispatch_web_action("setCameraPaused", {"paused": False}))
        self.assertTrue(screen.paused)
        self.assertFalse(camera.paused)
        self.assertFalse(panel.dispatch_web_action("setCameraPaused", {"paused": "false"}))
        self.assertTrue(panel.dispatch_web_action("pauseAllVisual", {}))
        self.assertTrue(screen.paused and camera.paused)
        self.assertTrue(panel.dispatch_web_action("openCameraSettings", {}))
        self.assertTrue(panel.dispatch_web_action("cameraSettings", {}))
        self.assertEqual(settings.count(), 2)

    def test_chat_font_persists_and_updates_both_native_views_without_touching_drafts(self):
        chat = self.chat()
        first, second = self.panel(chat), self.panel(chat)
        first.input.setPlainText("原生未发送草稿")
        self.assertEqual(first.web_state()["chatFontSize"], 16)
        self.assertTrue(first.dispatch_web_action("setChatFontSize", {"size": 20}))
        self.assertEqual(self.settings.load_chat_font_size(), 20)
        for panel in (first, second):
            self.assertEqual(panel.transcript.font().pixelSize(), 20)
            self.assertEqual(panel.input.font().pixelSize(), 20)
            self.assertEqual(panel.web_state()["chatFontSize"], 20)
        self.assertEqual(first.input.toPlainText(), "原生未发送草稿")
        for invalid in (True, 11, 23, "18", None):
            self.assertFalse(first.dispatch_web_action("setChatFontSize", {"size": invalid}))
        self.assertEqual(self.chat(self.base / "other.sqlite3").reading_font_size, 20)


if __name__ == "__main__":
    unittest.main()
