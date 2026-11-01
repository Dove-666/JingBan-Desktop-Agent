"""Ordinary chat -> persisted assistant message -> linked pet speech.

Use real Qt controllers, SQLite, SDK and loopback HTTP. Preferences/credentials
are temporary, screen observations synthetic, and no system input is sent.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool, Qt, QTextBoundaryFinder
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.perception.settings import PerceptionSettings
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_perception_integration import AdjustableClock, SyntheticScene, event_json
from tests.test_persistent_screen_flow import DisabledTestMemory


class ChatSpeechDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-chat-speech-")
        self.path = Path(self.directory.name)
        self.settings = isolated_settings(self.path / "settings.ini", MemoryCredentialBackend())
        self.memory_patch = patch("desktop_pet.application_controller.MemoryService", return_value=DisabledTestMemory())
        self.memory_patch.start()
        self.session = ApplicationController(PetConfig(), settings=self.settings)
        self.session.pet_controller.asset_player.directory = self.path / "no-clips"
        self.session.pet.refresh_timer.stop()
        self.session.show()
        self.app.processEvents()
        self.chat = self.session.chat
        self.bubble = self.session.pet.bubble

    def tearDown(self):
        self.session.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for window in (self.session.pet, self.session.debug_panel, self.session.chat_window):
            if window is not None:
                window.deleteLater()
        self.session.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.memory_patch.stop()
        self.directory.cleanup()

    def configure(self, server):
        self.settings.save_api_settings(ApiSettings(server.base_url, "test-model", 5))
        self.settings.save_api_key(LOCAL_TEST_KEY)

    def send_reply(self, server, text, emotion="happy", **plan):
        server.enqueue(json.dumps({"reply": text, "emotion": emotion}, ensure_ascii=False), **plan)
        self.assertTrue(self.chat.send("请回答这个本地集成测试问题。"))
        spin_until(lambda: not self.chat.busy)
        self.assertFalse(self.chat.error, self.chat.error)
        return self.chat.messages[-1]

    def settle(self):
        spin_until(lambda: not self.chat._runner._tasks)

    def test_completed_reply_displays_once_with_its_emotion_and_real_message_link(self):
        reply = "这个回答来自实际 API 文本。\n\n第二段保留  两个空格与换行。🐳"
        with MockApiServer() as server:
            self.configure(server)
            received = QSignalSpy(self.chat.reply_received)
            with patch.object(self.bubble, "say", wraps=self.bubble.say) as say:
                message = self.send_reply(server, reply, "surprised")
                self.settle()
                self.assertEqual(say.call_count, 1, "Thinking/final emotion must not overwrite the reply with fixed feedback")
            self.assertEqual(received.count(), 1)
            self.assertEqual(self.session.pet_controller.expression, "surprised")
            self.assertTrue(self.bubble.isVisible())
            self.assertEqual(self.bubble._text, reply)
            self.assertEqual(message["role"], "assistant")
            self.assertEqual(message["content"], reply)
            self.assertEqual(message["metadata"]["emotion"], "surprised")
            reference = self.bubble._conversation_link
            self.assertEqual(reference["conversation_id"], message["conversation_id"])
            self.assertEqual(reference["message_id"], message["id"])
            self.assertEqual(self.chat.store.load_messages(message["conversation_id"])[-1]["id"], message["id"])
            self.assertEqual(received.at(0)[0:2], [reply, "surprised"])
            self.assertEqual(received.at(0)[2]["message_id"], message["id"])

    def test_linked_reply_click_focuses_exact_message_and_reading_history_does_not_replay(self):
        with MockApiServer() as server:
            self.configure(server)
            message = self.send_reply(server, "可以从这条气泡回到真正的回复。", "thinking")
            self.settle()
            received = QSignalSpy(self.chat.reply_received)
            focused = QSignalSpy(self.chat.message_focus_requested)
            sequence = self.bubble._speech_sequence
            self.assertFalse(self.bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
            self.assertFalse(self.bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput)
            QTest.mouseClick(self.bubble, Qt.MouseButton.LeftButton, pos=self.bubble.rect().center())
            spin_until(lambda: self.session.chat_window is not None and not self.chat.history_loading)
            self.assertTrue(self.session.chat_window.isVisible())
            self.assertEqual(self.chat.current_conversation_id, message["conversation_id"])
            self.assertEqual(focused.count(), 1)
            self.assertEqual(focused.at(0)[0], message["id"])
            self.session.chat_window.chat_panel._activate_native()
            self.assertIn(message["content"], self.session.chat_window.chat_panel.transcript.toPlainText())
            self.bubble.dismiss()
            self.chat.new_conversation()
            self.assertTrue(self.chat.open_conversation(message["conversation_id"]))
            spin_until(lambda: not self.chat.history_loading)
            self.assertEqual(received.count(), 0)
            self.assertEqual(self.bubble._speech_sequence, sequence)
            self.assertFalse(self.bubble.isVisible())

    def test_long_multiline_reply_preserves_all_content_and_graphemes_across_pages(self):
        reply = ("第一段：中文与鲸鱼🐳，家人👨‍👩‍👧‍👦一起看海。\n"
                 "第二段保留换行和组合字 e\u0301。\n\n") * 20
        with MockApiServer() as server:
            self.configure(server)
            self.session.pet_controller.set_scale(50)
            message = self.send_reply(server, reply, "neutral")
            self.assertEqual(self.bubble._text, reply.strip())
            self.assertEqual(message["content"], reply.strip())
            self.assertEqual("".join(self.bubble._pages), reply.strip())
            self.assertGreater(len(self.bubble._pages), 1)
            finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, reply.strip())
            offset = 0
            for index, page in enumerate(self.bubble._pages):
                self.assertEqual(self.bubble._page_text, page)
                self.assertEqual(self.bubble._layout.text(), page.replace("\n", "\u2028"))
                self.assertTrue(self.bubble._body_rect.contains(self.bubble._text_rect))
                self.assertLessEqual(self.bubble._layout.boundingRect().height(), self.bubble._text_rect.height() + 1)
                offset += len(page.encode("utf-16-le")) // 2
                finder.setPosition(offset)
                self.assertTrue(finder.isAtBoundary(), "Pagination split a family emoji or combining mark")
                if index + 1 < len(self.bubble._pages):
                    self.bubble._next_page()

    def test_late_reply_for_other_conversation_is_saved_without_speech(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue('{"reply":"属于原会话的迟到回复。","emotion":"happy"}', delay=.3)
            original = self.chat.current_conversation_id
            received = QSignalSpy(self.chat.reply_received)
            with patch.object(self.bubble, "say", wraps=self.bubble.say) as say:
                self.assertTrue(self.chat.send("请求发出后切换。"))
                spin_until(server.received.is_set)
                replacement = self.chat.new_conversation()
                spin_until(lambda: not self.chat.busy)
                self.settle()
                self.assertEqual(say.call_count, 0)
            self.assertNotEqual(original, replacement)
            self.assertEqual(self.chat.messages, [])
            self.assertEqual(received.count(), 0)
            self.assertFalse(self.bubble.isVisible())
            self.assertEqual(self.chat.store.load_messages(original)[-1]["content"], "属于原会话的迟到回复。")

    def test_failed_api_reply_never_shows_emotion_feedback_as_fake_response(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue("local auth failure", status=401)
            received = QSignalSpy(self.chat.reply_received)
            with patch.object(self.bubble, "say", wraps=self.bubble.say) as say:
                self.assertTrue(self.chat.send("这次会失败。"))
                spin_until(lambda: not self.chat.busy)
                self.assertEqual(say.call_count, 0)
            self.assertTrue(self.chat.error)
            self.assertEqual(received.count(), 0)
            self.assertFalse(self.bubble.isVisible())
            self.assertEqual(self.session.pet_controller.expression, "neutral")
            self.assertFalse(any(message["role"] == "assistant" for message in self.chat.messages))

    def assert_immediate_resend_keeps_new_thinking(self, *, first_fails):
        """A real busy signal starts round two inside round one's callback."""
        with MockApiServer() as server:
            self.configure(server)
            if first_fails:
                server.enqueue("old request auth failure", status=401)
            else:
                server.enqueue('{"reply":"第一轮旧回复不该弹出。","emotion":"happy"}')
            newest = "第二轮的新回复，只有这一条应该说出来。\n仍保留换行。"
            server.enqueue(json.dumps({"reply": newest, "emotion": "sleepy"}, ensure_ascii=False), delay=.55)
            started = []

            def resend_when_idle(busy):
                if not busy and not started:
                    # Mark before send: send emits busy_changed(True) reentrantly.
                    started.append(None)
                    started[0] = self.chat.send("立即发送的第二条问题。")

            self.chat.busy_changed.connect(resend_when_idle)
            received = QSignalSpy(self.chat.reply_received)
            emotions = QSignalSpy(self.chat.emotion_requested)
            try:
                with patch.object(self.bubble, "say", wraps=self.bubble.say) as say:
                    self.assertTrue(self.chat.send("第一条问题。"))
                    spin_until(lambda: len(server.requests) == 2)
                    self.assertEqual(started, [True])
                    self.assertTrue(self.chat.busy, "The delayed second reply must still be pending")
                    self.assertEqual(self.session.pet_controller.expression, "thinking")
                    self.assertEqual([emotions.at(i)[0] for i in range(emotions.count())], ["thinking", "thinking"])
                    self.assertEqual(received.count(), 0)
                    self.assertEqual(say.call_count, 0)
                    self.assertFalse(self.bubble.isVisible())
                    self.assertFalse(self.chat.error, "Round one's error must not contaminate the new request")
                    spin_until(lambda: not self.chat.busy)
                    self.settle()
                    self.assertEqual(say.call_count, 1)
                self.assertEqual(self.session.pet_controller.expression, "sleepy")
                self.assertFalse(self.chat.error)
                self.assertEqual(self.bubble._text, newest)
                self.assertEqual(received.count(), 1)
                self.assertEqual(received.at(0)[0], newest)
                message = self.chat.messages[-1]
                self.assertEqual(message["content"], newest)
                self.assertEqual(self.bubble._conversation_link["message_id"], message["id"])
            finally:
                self.chat.busy_changed.disconnect(resend_when_idle)

    def test_success_callback_does_not_override_immediately_resent_thinking_or_reply(self):
        self.assert_immediate_resend_keeps_new_thinking(first_fails=False)

    def test_error_callback_does_not_override_immediately_resent_thinking_or_reply(self):
        self.assert_immediate_resend_keeps_new_thinking(first_fails=True)

    def test_clear_dismisses_existing_reply_and_reopening_does_not_replay_it(self):
        with MockApiServer() as server:
            self.configure(server)
            message = self.send_reply(server, "即将清空的回复。")
            self.settle()
            sequence = self.bubble._speech_sequence
            received = QSignalSpy(self.chat.reply_received)
            self.chat.clear()
            spin_until(lambda: not self.chat.history_loading)
            self.settle()
            self.assertEqual(self.chat.messages, [])
            self.assertFalse(self.bubble.isVisible())
            self.assertTrue(self.chat.open_conversation(message["conversation_id"]))
            spin_until(lambda: not self.chat.history_loading)
            self.assertEqual(received.count(), 0)
            self.assertEqual(self.bubble._speech_sequence, sequence)
            self.assertFalse(self.bubble.isVisible())

    def test_delete_dismisses_existing_reply_and_leaves_no_dead_message_link(self):
        with MockApiServer() as server:
            self.configure(server)
            message = self.send_reply(server, "即将删除的会话回复。")
            self.settle()
            received = QSignalSpy(self.chat.reply_received)
            self.assertTrue(self.chat.delete_conversation(message["conversation_id"]))
            self.settle()
            self.assertIsNone(self.chat.store.get_conversation(message["conversation_id"]))
            self.assertNotEqual(self.chat.current_conversation_id, message["conversation_id"])
            self.assertFalse(self.bubble.isVisible())
            self.assertEqual(received.count(), 0)

    def test_screen_comment_uses_existing_delivery_once_without_ordinary_reply_signal(self):
        with MockApiServer() as server:
            self.configure(server)
            perception = self.session.perception
            perception.observer = SyntheticScene().observer()
            perception._clock = AdjustableClock()
            self.assertTrue(perception.update_settings(PerceptionSettings(enabled=True, allow_remote=True)))
            perception._timer.stop()
            received = QSignalSpy(self.chat.reply_received)
            server.enqueue(event_json())
            with patch.object(self.bubble, "say", wraps=self.bubble.say) as say:
                self.assertTrue(perception.poll())
                spin_until(lambda: not perception.busy)
                spin_until(lambda: bool(self.chat.messages))
                self.settle()
                self.assertEqual(say.call_count, 1)
            message = self.chat.messages[-1]
            self.assertEqual(message["source"], "proactive_screen")
            self.assertEqual(self.bubble._text, message["content"])
            self.assertEqual(self.bubble._conversation_link["message_id"], message["id"])
            self.assertEqual(received.count(), 0)


if __name__ == "__main__":
    unittest.main()
