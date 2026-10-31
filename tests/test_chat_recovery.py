"""Recovery/stop tests use temporary SQLite, memory credentials and loopback only."""

import json
from pathlib import Path
import tempfile
import threading
import time
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiClient, ApiError, ChatReply
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore, StoreError
from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from desktop_pet.ui.chat_panel import ChatPanel, validated_external_url
from tests.mock_api import MockApiServer
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until


class HeldFirstReply:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.contexts = []

    def complete_context(self, settings, key, messages, **kwargs):
        self.contexts.append(messages)
        if len(self.contexts) == 1:
            self.entered.set()
            self.release.wait(5)
            return ChatReply("这条迟到回复应被丢弃")
        return ChatReply("继续聊天的有效回复")


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-chat-recovery-")
        self.path = Path(self.temp.name)
        self.settings = isolated_settings(self.path / "settings.ini", MemoryCredentialBackend())
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "local-fixture", 4))
        self.settings.save_api_key(LOCAL_TEST_KEY)
        self.store = ConversationStore(self.path / "archive.sqlite3")
        self.controllers, self.panels, self.held = [], [], []

    def make_chat(self, client=None):
        chat = ChatController(self.settings, client, store=self.store, metadata_client_factory=lambda: None)
        self.controllers.append(chat)
        return chat

    def open(self, chat, cid):
        self.assertTrue(chat.open_conversation(cid))
        spin_until(lambda: not chat.history_loading)

    def tearDown(self):
        for client in self.held:
            client.release.set()
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

    def test_failed_background_turn_reopens_and_retries_after_controller_restart(self):
        class FailedClient:
            def complete_context(self, *args, **kwargs):
                raise ApiError("network", "合成网络失败")
        chat = self.make_chat(FailedClient())
        original = chat.current_conversation_id
        self.assertTrue(chat.send("原会话的问题"))
        chat.new_conversation()
        spin_until(lambda: not chat.busy)
        self.open(chat, original)
        failed_id = chat.messages[0]["id"]
        self.assertEqual(chat.messages[0]["metadata"]["request_status"], "failed")
        chat.shutdown()
        client = HeldFirstReply()
        client.release.set()
        resumed = self.make_chat(client)
        self.open(resumed, original)
        self.assertFalse(resumed.retry("stale-session", failed_id))
        self.assertFalse(resumed.retry(original, "unknown-message"))
        self.assertTrue(resumed.retry(original, failed_id))
        spin_until(lambda: not resumed.busy)
        rows = self.store.load_messages(original)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], failed_id)
        self.assertEqual(rows[0]["metadata"]["request_status"], "complete")

    def test_legacy_pending_becomes_persisted_interrupted_and_retries_same_id(self):
        cid = str(uuid4())
        original = self.store.append_message(cid, "user", "上次退出时未完成的问题", "user",
                                             metadata={"request_status": "pending"})
        client = HeldFirstReply()
        client.release.set()
        chat = self.make_chat(client)
        self.open(chat, cid)
        self.assertEqual(chat.messages[0]["metadata"]["request_status"], "interrupted")
        self.assertEqual(self.store.load_messages(cid)[0]["metadata"]["request_status"], "interrupted")
        self.assertTrue(chat.retry(cid, original["id"]))
        spin_until(lambda: not chat.busy)
        self.assertEqual(sum(row["role"] == "user" for row in self.store.load_messages(cid)), 1)

    def test_stale_pending_history_snapshot_cannot_overwrite_completed_request(self):
        client = HeldFirstReply()
        self.held.append(client)
        chat = self.make_chat(client)
        captured, release_snapshot = threading.Event(), threading.Event()
        capture_once = threading.Event()
        capture_once.set()
        self.assertTrue(chat.send("正常完成不能因历史快照交错变成中断"))
        spin_until(client.entered.is_set)
        cid = chat.current_conversation_id
        original_load = self.store.load_messages
        def load(target):
            rows = original_load(target)
            if target == cid and capture_once.is_set():
                capture_once.clear()
                self.assertEqual(rows[0]["metadata"]["request_status"], "pending")
                captured.set()
                release_snapshot.wait(5)
            return rows
        try:
            with patch.object(self.store, "load_messages", side_effect=load):
                self.assertTrue(chat.open_conversation(cid))
                spin_until(captured.is_set)
                client.release.set()
                spin_until(lambda: not chat.busy)
                self.assertEqual(original_load(cid)[0]["metadata"]["request_status"], "complete")
                release_snapshot.set()
                spin_until(lambda: not chat.history_loading)
            self.assertEqual(original_load(cid)[0]["metadata"]["request_status"], "complete")
            self.assertEqual(chat.messages[0]["metadata"]["request_status"], "complete")
            self.assertEqual(sum(row["role"] == "assistant" for row in chat.messages), 1)
            self.assertFalse(chat.retry(cid, chat.messages[0]["id"]))
        finally:
            release_snapshot.set()

    def test_pending_recovery_compare_and_swap_keeps_newer_terminal_states(self):
        cid = str(uuid4())
        for status in ["complete", "failed", "cancelled", "interrupted"]:
            with self.subTest(status=status):
                row = self.store.append_message(cid, "user", f"状态已经是{status}", "user",
                                               metadata={"request_status": status})
                recovered = self.store.recover_pending_message(cid, row["id"])
                self.assertEqual(recovered["metadata"]["request_status"], status)
                actual = next(item for item in self.store.load_messages(cid) if item["id"] == row["id"])
                self.assertEqual(actual["metadata"]["request_status"], status)

    def test_multiple_recoverable_turns_use_explicit_message_id(self):
        cid = str(uuid4())
        first = self.store.append_message(cid, "user", "先失败的问题", "user", metadata={"request_status": "failed"})
        second = self.store.append_message(cid, "user", "后来停止的问题", "user", metadata={"request_status": "cancelled"})
        client = HeldFirstReply()
        client.release.set()
        chat = self.make_chat(client)
        self.open(chat, cid)
        self.assertTrue(chat.retry(cid, first["id"]))
        spin_until(lambda: not chat.busy)
        rows = self.store.load_messages(cid)
        self.assertEqual(rows[-1]["metadata"]["reply_to"], first["id"])
        self.assertEqual(rows[1]["id"], second["id"])
        self.assertEqual(rows[1]["metadata"]["request_status"], "cancelled")
        self.assertNotIn("后来停止的问题", json.dumps(client.contexts[-1], ensure_ascii=False))

    def test_stop_discards_late_response_and_new_request_can_finish_first(self):
        client = HeldFirstReply()
        self.held.append(client)
        chat = self.make_chat(client)
        cid = chat.current_conversation_id
        replies = QSignalSpy(chat.reply_received)
        self.assertTrue(chat.send("要取消的问题"))
        spin_until(client.entered.is_set)
        cancelled_id = chat.messages[0]["id"]
        self.assertFalse(chat.stop_request("stale-session"))
        self.assertTrue(chat.stop_request(cid))
        self.assertFalse(chat.busy)
        spin_until(lambda: self.store.load_messages(cid)[0]["metadata"]["request_status"] == "cancelled")
        self.assertFalse(client.release.is_set(), "Stop must persist before the held provider returns")
        self.assertTrue(chat.send("新的独立问题"))
        spin_until(lambda: not chat.busy)
        client.release.set()
        spin_until(lambda: not chat._requests)
        rows = self.store.load_messages(cid)
        self.assertEqual([row["content"] for row in rows],
                         ["要取消的问题", "新的独立问题", "继续聊天的有效回复"])
        self.assertEqual(rows[0]["metadata"]["request_status"], "cancelled")
        self.assertEqual(chat.messages[0]["id"], cancelled_id)
        self.assertEqual(replies.count(), 1)
        self.assertNotIn("要取消的问题", json.dumps(client.contexts[-1], ensure_ascii=False))
        self.assertFalse(chat.error)

    def test_stop_background_request_preserves_other_session_and_restart_status(self):
        client = HeldFirstReply()
        self.held.append(client)
        chat = self.make_chat(client)
        cid = chat.current_conversation_id
        self.assertTrue(chat.send("停止前切换会话"))
        spin_until(client.entered.is_set)
        new_id = chat.new_conversation()
        self.assertTrue(chat.stop_request(cid))
        client.release.set()
        spin_until(lambda: not chat._requests)
        self.assertEqual(chat.current_conversation_id, new_id)
        self.assertEqual(chat.messages, [])
        self.assertFalse(chat.error)
        chat.shutdown()
        restarted = self.make_chat(HeldFirstReply())
        self.open(restarted, cid)
        self.assertEqual(restarted.messages[0]["metadata"]["request_status"], "cancelled")

    def test_stop_feedback_cannot_override_request_started_inside_busy_signal(self):
        client = HeldFirstReply()
        self.held.append(client)
        chat = self.make_chat(client)
        emotions, resent = [], []
        chat.emotion_requested.connect(emotions.append)
        self.assertTrue(chat.send("停止这一轮"))
        spin_until(client.entered.is_set)
        def resend(busy):
            if not busy and not resent:
                resent.append(True)
                resent.append(chat.send("在busy信号中立即发起第二轮"))
        chat.busy_changed.connect(resend)
        self.assertTrue(chat.stop_request(chat.current_conversation_id))
        self.assertTrue(resent[-1])
        self.assertTrue(chat.busy)
        self.assertEqual(emotions, ["thinking", "thinking"])
        self.assertEqual(chat.error, "")
        spin_until(lambda: not chat.busy)
        client.release.set()
        spin_until(lambda: not chat._requests)
        self.assertEqual(chat.messages[-1]["content"], "继续聊天的有效回复")
        self.assertEqual(chat.error, "")

    def test_stop_cancels_real_loopback_http_and_reuses_transport(self):
        with MockApiServer() as server:
            self.settings.save_api_settings(ApiSettings(server.base_url, "fixture", 4))
            server.enqueue('{"reply":"迟到网络回复"}', delay=0.65)
            chat = self.make_chat()
            cid = chat.current_conversation_id
            self.assertTrue(chat.send("等待真实HTTP"))
            spin_until(server.received.is_set)
            started = time.monotonic()
            self.assertTrue(chat.stop_request(cid))
            spin_until(lambda: not chat._requests, timeout=1.0)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(chat._client._cancelled.is_set())
            self.assertEqual(len(self.store.load_messages(cid)), 1)
            server.enqueue('{"reply":"取消后正常回复"}')
            self.assertTrue(chat.send("继续真实HTTP"))
            spin_until(lambda: not chat.busy)
            self.assertEqual(chat.messages[-1]["content"], "取消后正常回复")

    def test_stop_during_commit_returns_false_without_claiming_cancellation(self):
        client = HeldFirstReply()
        client.release.set()
        chat = self.make_chat(client)
        writing, release_write = threading.Event(), threading.Event()
        original = self.store.complete_request
        def hold(*args, **kwargs):
            writing.set()
            release_write.wait(5)
            return original(*args, **kwargs)
        try:
            with patch.object(self.store, "complete_request", side_effect=hold):
                self.assertTrue(chat.send("正在提交的回复"))
                spin_until(writing.is_set)
                self.assertFalse(chat.stop_request(chat.current_conversation_id))
                self.assertTrue(chat.busy)
                release_write.set()
                spin_until(lambda: not chat.busy)
            self.assertEqual(len(chat.messages), 2)
            self.assertFalse(chat.error)
        finally:
            release_write.set()

    def test_missing_key_opens_settings_and_does_not_overwrite_guidance(self):
        self.settings.clear_api_key()
        chat = self.make_chat()
        opened = []
        panel = ChatPanel(chat, settings_opener=lambda: opened.append("settings"))
        self.panels.append(panel)
        panel.input.setPlainText("配置好后继续发送的草稿")
        panel._send()
        guidance = chat.error
        self.assertEqual(chat.error_action, "settings")
        self.assertEqual(panel.retry_button.text(), "打开设置")
        panel.retry_button.click()
        self.assertEqual(opened, ["settings"])
        self.assertEqual(chat.error, guidance)
        self.assertEqual(panel.input.toPlainText(), "配置好后继续发送的草稿")
        self.assertEqual(chat.messages, [])

    def test_history_recovery_reloads_failed_target_and_preserves_previous_view(self):
        cid = str(uuid4())
        self.store.append_message(cid, "user", "恢复的历史记录", "user")
        chat = self.make_chat()
        previous = chat.current_conversation_id
        with patch.object(self.store, "get_conversation", side_effect=StoreError("合成读取故障")):
            self.open(chat, cid)
        self.assertEqual(chat.current_conversation_id, previous)
        self.assertEqual(chat.error_action, "history")
        self.assertEqual(chat.error_conversation_id, cid)
        self.assertTrue(chat.reload_history(chat.error_conversation_id))
        spin_until(lambda: not chat.history_loading)
        self.assertEqual(chat.current_conversation_id, cid)
        self.assertEqual(chat.messages[0]["content"], "恢复的历史记录")

    def test_native_fallback_shares_sessions_drafts_and_can_return_to_web(self):
        chat = self.make_chat()
        panel = AssistantChatPanel(chat)
        self.panels.append(panel)
        original = chat.current_conversation_id
        panel.dispatch_web_action("draft", {"conversationId": original, "text": "网页中的原会话草稿"})
        panel._web_failed()
        self.assertFalse(panel.compatibility_button.isHidden())
        panel.compatibility_button.click()
        self.assertFalse(panel.splitter.isHidden())
        self.assertEqual(panel.input.toPlainText(), "网页中的原会话草稿")
        panel.input.setPlainText("兼容聊天中继续编辑")
        chat.new_conversation()
        self.assertEqual(panel.input.toPlainText(), "")
        self.assertEqual(panel.web_state()["drafts"][original], "兼容聊天中继续编辑")
        panel.input.setPlainText("新会话的独立草稿")
        with patch.object(panel, "_ensure_web") as ensure:
            panel._reload_web()
            ensure.assert_called_once()
        self.assertEqual(panel.web_state()["drafts"][chat.current_conversation_id], "新会话的独立草稿")
        self.assertTrue(panel.splitter.isHidden())

    def test_bridge_checks_external_links_and_explicit_retry_targets(self):
        chat = self.make_chat()
        panel = AssistantChatPanel(chat)
        self.panels.append(panel)
        for url in ["file:///C:/secret", "javascript:alert(1)", "data:text/html,test", "https://",
                    "https://user:pass@example.test/", "https://example.test/\n", "https://example.test/%0a",
                    "https://example.test\\@evil.test", "https://example.test:99999/"]:
            with self.subTest(url=url), patch("desktop_pet.ui.assistant_panel.QDesktopServices.openUrl") as opened:
                self.assertFalse(panel.dispatch_web_action("openExternal", {"url": url}))
                self.assertFalse(panel.dispatch_web_action("copyLink", {"url": url}))
                opened.assert_not_called()
        with patch("desktop_pet.ui.assistant_panel.QDesktopServices.openUrl", return_value=True) as opened:
            self.assertTrue(panel.dispatch_web_action("openExternal", {"url": "https://example.test/path?q=1#part"}))
            self.assertEqual(opened.call_args.args[0].scheme(), "https")
        self.assertTrue(panel.dispatch_web_action("copyLink", {"url": "https://example.test/path?q=1#part"}))
        self.assertEqual(QApplication.clipboard().text(), "https://example.test/path?q=1#part")
        self.assertFalse(panel.dispatch_web_action("retry", {}))
        self.assertFalse(panel.dispatch_web_action("retry", {"conversationId": "stale", "messageId": "unknown"}))

    def test_complete_request_is_atomic_and_idempotent(self):
        cid = str(uuid4())
        user = self.store.append_message(cid, "user", "只保存一次的问题", "user", metadata={"request_status": "pending"})
        first = self.store.complete_request(cid, user["id"], "已提交的回复")
        second = self.store.complete_request(cid, user["id"], "重复回调应复用原回复")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.load_messages(cid)), 2)
        self.assertEqual(self.store.load_messages(cid)[0]["metadata"]["request_status"], "complete")

    def test_failed_user_status_commit_rolls_back_assistant_in_same_transaction(self):
        cid = str(uuid4())
        self.store.append_message(cid, "user", "提交失败要能安全重试", "user", message_id="rollback-user",
                                  metadata={"request_status": "pending"})
        with sqlite3.connect(str(self.store.path)) as connection:
            connection.execute("""CREATE TRIGGER fixture_disk_failure BEFORE UPDATE ON messages
                WHEN NEW.id = 'rollback-user' AND json_extract(NEW.metadata, '$.request_status') = 'complete'
                BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END""")
        with self.assertRaises(StoreError):
            self.store.complete_request(cid, "rollback-user", "不该部分保存的回复")
        rows = self.store.load_messages(cid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metadata"]["request_status"], "pending")

    def test_stale_screen_snapshot_does_not_erase_saved_reply_or_revert_status(self):
        client = HeldFirstReply()
        client.release.set()
        chat = self.make_chat(client)
        self.assertTrue(chat.send("先完成正常聊天"))
        spin_until(lambda: not chat.busy)
        cid = chat.current_conversation_id
        stale = chat.messages[0]
        stale["metadata"]["request_status"] = "pending"
        chat._install_snapshot(cid, self.store.get_conversation(cid), [stale])
        self.assertEqual(len(chat.messages), 2)
        self.assertEqual(chat.messages[0]["metadata"]["request_status"], "complete")
        self.assertEqual(chat.messages[-1]["role"], "assistant")


if __name__ == "__main__":
    unittest.main()
