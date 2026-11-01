"""Real Qt, temporary SQLite and loopback SDK tests for isolated sessions.

No test uses the user's credential backend, production settings or desktop.
"""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.api_client import ApiError
from desktop_pet.conversations import ContextBudget, ConversationStore, estimate_tokens
from desktop_pet.memory.service import MemoryUnavailable
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until


class UnavailableMemory:
    def search(self, query, limit=3):
        raise MemoryUnavailable("测试中的记忆服务暂不可用。")

    def extract(self, messages, **kwargs):
        raise MemoryUnavailable("测试中的记忆服务暂不可用。")


class SessionControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-persistent-session-")
        self.path = Path(self.directory.name)
        self.settings = isolated_settings(self.path / "settings.ini", MemoryCredentialBackend())
        self.store = ConversationStore(self.path / "archive.sqlite3")
        self.controllers = []
        self.chat = self.make_chat()

    def make_chat(self, **kwargs):
        controller = ChatController(self.settings, store=self.store, **kwargs)
        self.controllers.append(controller)
        return controller

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for controller in self.controllers:
            controller.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def configure(self, server):
        self.settings.save_api_settings(ApiSettings(server.base_url, "test-model", 5))
        self.settings.save_api_key(LOCAL_TEST_KEY)

    def send(self, server, text, reply="已收到本轮消息。", **response):
        server.enqueue(json.dumps({"reply": reply, "emotion": "happy"}, ensure_ascii=False), **response)
        self.assertTrue(self.chat.send(text))
        spin_until(lambda: not self.chat.busy)
        self.assertFalse(self.chat.error, self.chat.error)

    def metadata_done(self):
        spin_until(lambda: not self.chat._metadata_active)

    def test_distinct_uuid_drafts_leave_no_empty_history(self):
        first = self.chat.current_conversation_id
        self.assertEqual(str(UUID(first)), first)
        second = self.chat.new_conversation()
        self.assertNotEqual(first, second)
        self.assertEqual(str(UUID(second)), second)
        spin_until(lambda: not self.chat._runner._tasks)
        self.assertEqual(self.store.list_conversations(), [])
        self.assertIsNone(self.store.get_conversation(first))
        self.assertIsNone(self.store.get_conversation(second))

    def test_restart_creates_new_draft_and_explicit_old_resume_keeps_history(self):
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "本次在讨论独立会话", "旧会话的第一条回复。")
            old_id = self.chat.current_conversation_id
            self.metadata_done()
            self.chat.shutdown()
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            self.chat = self.make_chat()
            self.assertNotEqual(self.chat.current_conversation_id, old_id)
            self.assertEqual(self.chat.messages, [])
            spin_until(lambda: len(self.chat.conversations) == 1)
            self.assertTrue(self.chat.open_conversation(old_id))
            spin_until(lambda: not self.chat.history_loading)
            self.assertEqual([m["content"] for m in self.chat.messages],
                             ["本次在讨论独立会话", "旧会话的第一条回复。"])
            self.send(server, "继续这个旧话题", "在原会话继续回答。")
            self.assertEqual(len(self.store.load_messages(old_id)), 4)
            body = server.requests[-1]["body"]
            self.assertIn("旧会话的第一条回复。", json.dumps(body, ensure_ascii=False))
            new_id = self.chat.new_conversation()
            self.send(server, "完全独立的新问题", "这是另一个会话。")
            request = json.dumps(server.requests[-1]["body"], ensure_ascii=False)
            self.assertNotIn("旧会话的第一条回复", request)
            self.assertNotIn("继续这个旧话题", request)
            self.assertEqual(len(self.store.load_messages(new_id)), 2)
            self.assertEqual(len(self.store.load_messages(old_id)), 4)

    def test_late_reply_persists_original_conversation_without_overwriting_new_view(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue('{"reply":"迟到但属于原会话","emotion":"happy"}', delay=0.25)
            original = self.chat.current_conversation_id
            self.assertTrue(self.chat.send("请求发出后切换"))
            spin_until(server.received.is_set)
            replacement = self.chat.new_conversation()
            emotions = QSignalSpy(self.chat.emotion_requested)
            spin_until(lambda: not self.chat.busy)
            self.assertNotEqual(original, replacement)
            self.assertEqual(self.chat.messages, [])
            self.assertEqual(self.chat.current_conversation_id, replacement)
            self.assertEqual(emotions.count(), 0)
            self.assertEqual(self.store.load_messages(original)[-1]["content"], "迟到但属于原会话")
            self.assertEqual(self.store.load_messages(replacement), [])

    def test_failed_request_retry_reuses_user_message_and_recovers_status(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue("test authentication failure", status=401)
            self.assertTrue(self.chat.send("失败后重试这一条"))
            spin_until(lambda: not self.chat.busy)
            self.assertTrue(self.chat.error)
            conversation_id = self.chat.current_conversation_id
            failed = self.store.load_messages(conversation_id)
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["metadata"]["request_status"], "failed")
            server.enqueue('{"reply":"重试成功","emotion":"neutral"}')
            self.assertTrue(self.chat.retry())
            spin_until(lambda: not self.chat.busy)
            messages = self.store.load_messages(conversation_id)
            self.assertFalse(self.chat.error)
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["id"], failed[0]["id"])
            self.assertEqual(messages[0]["metadata"]["request_status"], "complete")
            self.assertEqual(sum(m["role"] == "user" for m in messages), 1)

    def test_rename_search_and_delete_cascade_leave_other_session_intact(self):
        conversation_id = self.chat.current_conversation_id
        event_id = str(uuid4())
        reference = self.chat.record_screen_comment(conversation_id, "SQLite 图表评论", event_id=event_id,
            app_name="synthetic-editor.exe", context_summary="SQLite 测试图表", event_type="interesting")
        spin_until(lambda: bool(self.chat.messages))
        self.metadata_done()
        other = str(uuid4())
        self.store.append_message(other, "user", "另一个保留会话", "user")
        self.assertTrue(self.chat.rename_conversation(conversation_id, "手工标题：图表测试"))
        spin_until(lambda: self.chat.current_title == "手工标题：图表测试")
        self.chat.search_conversations("SQLite")
        spin_until(lambda: len(self.chat.conversations) == 1)
        self.assertEqual(self.chat.conversations[0]["id"], conversation_id)
        self.chat.search_conversations("手工标题")
        spin_until(lambda: len(self.chat.conversations) == 1)
        self.assertEqual(self.chat.conversations[0]["id"], conversation_id)
        self.assertIsNotNone(self.store.get_screen_event(event_id, conversation_id))
        self.assertEqual(self.store.load_messages(conversation_id)[0]["id"], reference["message_id"])
        self.assertTrue(self.chat.delete_conversation(conversation_id))
        spin_until(lambda: self.chat.current_conversation_id != conversation_id)
        self.assertIsNone(self.store.get_conversation(conversation_id))
        self.assertEqual(self.store.load_messages(conversation_id), [])
        self.assertIsNone(self.store.get_screen_event(event_id, conversation_id))
        self.assertEqual(len(self.store.load_messages(other)), 1)

    def test_title_generated_once_and_incremental_summary_cursor_is_persistent(self):
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "开始研究长期记忆系统")
            self.metadata_done()
            conversation_id = self.chat.current_conversation_id
            self.assertEqual(len(server.metadata_requests), 1)
            self.assertTrue(self.store.get_conversation(conversation_id)["title_generated"])
            initial = self.store.get_conversation(conversation_id)
            self.assertTrue(initial["summary"])
            self.assertIsNone(initial["summary_through_id"])
            for index in range(22):
                role = "user" if index % 2 == 0 else "assistant"
                self.store.append_message(conversation_id, role, f"较早内容 {index}", role)
            self.send(server, "现在总结前面的项目进度")
            self.metadata_done()
            summarized = self.store.get_conversation(conversation_id)
            self.assertTrue(summarized["summary"])
            self.assertIsNotNone(summarized["summary_through_id"])
            self.assertEqual(len(server.metadata_requests), 2)
            second_payload = json.loads(server.metadata_requests[-1]["body"]["messages"][-1]["content"])
            self.assertFalse(second_payload["generate_title"])
            self.assertTrue(second_payload["update_summary"])
            self.assertEqual(len(second_payload["messages"]), 8)
            self.send(server, "当前任务继续")
            self.metadata_done()
            self.assertEqual(len(server.metadata_requests), 2)
            reopened = ConversationStore(self.store.path).get_conversation(conversation_id)
            self.assertEqual(reopened["summary_through_id"], summarized["summary_through_id"])
            self.assertEqual(reopened["summary"], summarized["summary"])
            self.assertEqual(reopened["title"], summarized["title"])

    def test_real_http_context_is_bounded_with_summary_and_output_reserve(self):
        self.chat.shutdown()
        budget = ContextBudget(total_tokens=4096, output_reserve=512, recent_tokens=1000,
                               max_recent_messages=16, summary_tokens=600)
        self.chat = self.make_chat(context_budget=budget)
        conversation_id = self.chat.current_conversation_id
        for index in range(80):
            role = "user" if index % 2 == 0 else "assistant"
            self.store.append_message(conversation_id, role, f"archived message {index} " + "x" * 80, role)
        self.store.update_metadata(conversation_id, title="预算测试", summary="实际讨论的是消息上下文预算。")
        with MockApiServer() as server:
            self.configure(server)
            question = "完整保留当前问题，不得截断。"
            self.send(server, question)
            body = server.requests[-1]["body"]
            messages = body["messages"]
            self.assertEqual(body["max_tokens"], 512)
            self.assertEqual(messages[-1]["content"], question)
            self.assertLessEqual(len([m for m in messages[:-1] if m["role"] in {"user", "assistant"}]), 16)
            self.assertLessEqual(sum(estimate_tokens(m["content"]) + 12 for m in messages), 4096 - 512)
            self.assertIn("实际讨论的是消息上下文预算", json.dumps(messages, ensure_ascii=False))
            self.assertNotIn("archived message 0 ", json.dumps(messages))
            self.assertEqual(len(self.store.load_messages(conversation_id)), 82)
            self.assertIn("未携带", self.chat.context_usage)

    def test_unavailable_memory_does_not_lose_or_block_chat(self):
        self.chat.shutdown()
        self.chat = self.make_chat(memory_service=UnavailableMemory())
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "这个问题不依赖长期记忆", "依然能够正常聊天。")
            self.assertEqual(len(self.chat.messages), 2)
            self.assertIn("长期记忆检索暂不可用", self.chat.context_usage)
            self.assertEqual(self.store.load_messages(self.chat.current_conversation_id)[-1]["content"],
                             "依然能够正常聊天。")

    def test_missing_history_load_reports_error_and_restores_current_view(self):
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "有效会话")
            original_id, messages = self.chat.current_conversation_id, self.chat.messages
            self.assertTrue(self.chat.open_conversation(str(uuid4())))
            spin_until(lambda: not self.chat.history_loading)
            self.assertIn("历史会话加载失败", self.chat.error)
            self.assertEqual(self.chat.current_conversation_id, original_id)
            self.assertEqual(self.chat.messages, messages)

    def test_screen_save_older_snapshot_keeps_pending_user_visible_after_api_failure(self):
        with MockApiServer() as server:
            self.configure(server)
            held_callbacks = []
            original_submit = self.chat._runner.submit

            def hold_result(function, callback):
                return original_submit(function, lambda result, error: held_callbacks.append((callback, result, error)))

            cid = self.chat.current_conversation_id
            with patch.object(self.chat._runner, "submit", side_effect=hold_result):
                self.chat.record_screen_comment(cid, "先保存的屏幕评论", event_id=str(uuid4()),
                    app_name="synthetic-editor.exe", context_summary="测试合成图表", event_type="interesting")
            spin_until(lambda: bool(held_callbacks))
            # The real worker committed its event and read messages, but the
            # GUI callback is delayed until after a new user turn is visible.
            callback, result, error = held_callbacks.pop()
            self.assertEqual(len(result[2]), 1)
            server.enqueue("test authentication failure", status=401, delay=0.2)
            self.assertTrue(self.chat.send("这条失败消息必须继续显示"))
            spin_until(server.received.is_set)
            pending_id = self.chat.messages[-1]["id"]
            callback(result, error)
            self.assertIn(pending_id, [m["id"] for m in self.chat.messages])
            spin_until(lambda: not self.chat.busy)
            self.assertTrue(self.chat.error)
            visible = next(m for m in self.chat.messages if m["id"] == pending_id)
            self.assertEqual(visible["metadata"]["request_status"], "failed")
            self.assertEqual(len(self.chat.messages), 2)
            self.assertEqual({m["id"] for m in self.chat.messages},
                             {m["id"] for m in self.store.load_messages(cid)})

    def test_second_history_load_failure_restores_last_successful_view_not_loading_placeholder(self):
        original_id = self.chat.current_conversation_id
        self.store.append_message(original_id, "user", "最后成功显示的完整会话", "user")
        other_id = str(uuid4())
        self.store.append_message(other_id, "user", "慢速加载的另一个会话", "user")
        self.assertTrue(self.chat.open_conversation(original_id))
        spin_until(lambda: not self.chat.history_loading)
        original_title, original_messages = self.chat.current_title, self.chat.messages
        entered, release = threading.Event(), threading.Event()
        get_conversation = self.store.get_conversation

        def delayed_read(conversation_id):
            if conversation_id == other_id:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Test did not release the blocked history worker")
            return get_conversation(conversation_id)

        with patch.object(self.store, "get_conversation", side_effect=delayed_read):
            try:
                self.assertTrue(self.chat.open_conversation(other_id))
                spin_until(entered.is_set)
                self.assertTrue(self.chat.history_loading)
                self.assertTrue(self.chat.open_conversation(str(uuid4())))
                spin_until(lambda: not self.chat.history_loading)
                self.assertIn("历史会话加载失败", self.chat.error)
                self.assertEqual(self.chat.current_conversation_id, original_id)
                self.assertEqual(self.chat.current_title, original_title)
                self.assertEqual(self.chat.messages, original_messages)
            finally:
                release.set()
            spin_until(lambda: not self.chat._runner._tasks)
        self.assertEqual(self.chat.current_conversation_id, original_id)
        self.assertEqual(self.chat.messages, original_messages)

    def test_failed_auxiliary_title_request_uses_real_message_fallback(self):
        class FailedMetadataClient:
            def request_text(self, *args, **kwargs):
                raise ApiError("network", "辅助请求测试故障。")

            def shutdown(self):
                pass

        self.chat.shutdown()
        self.chat = self.make_chat(metadata_client_factory=FailedMetadataClient)
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "调试鲸鱼娘的历史会话", "辅助请求失败也正常回复。")
            self.metadata_done()
            conversation = self.store.get_conversation(self.chat.current_conversation_id)
            self.assertEqual(conversation["title"], "调试鲸鱼娘的历史会话")
            self.assertTrue(conversation["title_generated"])
            self.assertFalse(self.chat.error)
            self.assertEqual(len(self.chat.messages), 2)

    def test_metadata_response_after_clear_cannot_restore_old_overview(self):
        from desktop_pet.conversation_metadata import update_session_metadata

        cid = self.chat.current_conversation_id
        self.store.append_message(cid, "user", "需要清空的旧内容", "user")
        store = self.store

        class ClearDuringMetadata:
            def request_text(self, *args, **kwargs):
                store.clear_messages(cid)
                store.append_message(cid, "user", "完全不同的新内容", "user")
                return '{"title":"旧标题","summary":"旧内容的概况"}'

        update_session_metadata(self.store, cid, ClearDuringMetadata(), ApiSettings(), "")
        conversation = self.store.get_conversation(cid)
        self.assertEqual(conversation["title"], "新对话")
        self.assertEqual(conversation["summary"], "")

    def test_irrelevant_memory_is_omitted_and_retrieval_stays_small(self):
        class SelectiveMemory:
            def __init__(self):
                self.calls = []

            def search(self, query, limit=3):
                self.calls.append((query, limit))
                return ([{"memory": "用户正在开发 AURA。", "score": 0.95}]
                        + [{"memory": "无关内容不得注入。", "score": 0.01}] * 30)

            def extract(self, *args, **kwargs):
                return []

        memory = SelectiveMemory()
        self.chat.shutdown()
        self.chat = self.make_chat(memory_service=memory)
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "我现在开发的项目是什么？")
            request = json.dumps(server.requests[-1]["body"]["messages"], ensure_ascii=False)
            self.assertIn("用户正在开发 AURA", request)
            self.assertNotIn("无关内容不得注入", request)
            self.assertEqual(memory.calls, [("我现在开发的项目是什么？", 3)])

    def test_impossible_current_question_is_not_truncated_or_persisted(self):
        self.chat.shutdown()
        self.chat = self.make_chat(context_budget=ContextBudget(total_tokens=1600, output_reserve=512))
        with MockApiServer() as server:
            self.configure(server)
            current_id = self.chat.current_conversation_id
            self.assertFalse(self.chat.send("当前问题必须完整。" * 200))
            self.assertIn("当前问题超过上下文预算", self.chat.error)
            self.assertEqual(self.chat.messages, [])
            self.assertEqual(self.store.load_messages(current_id), [])
            self.assertEqual(server.requests, [])

    def test_shutdown_keeps_accepted_user_write_and_drops_cancelled_reply(self):
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue('{"reply":"退出后不再写回复","emotion":"neutral"}', delay=0.35)
            conversation_id = self.chat.current_conversation_id
            self.assertTrue(self.chat.send("退出也要保存这条用户消息"))
            spin_until(server.received.is_set)
            self.chat.shutdown()
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            messages = self.store.load_messages(conversation_id)
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["metadata"]["request_status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
