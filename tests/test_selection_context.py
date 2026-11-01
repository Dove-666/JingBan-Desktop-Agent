"""Explicit-selection chat uses synthetic text, temporary SQLite and mock APIs."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiError, ChatReply
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ContextAssembler, ContextBudget, ContextBudgetError, ConversationStore
from desktop_pet.conversations.context import estimate_tokens
from desktop_pet.selection.context import MAX_PAGE_CHARACTERS, MAX_SELECTION_CHARACTERS, SelectionContext, SelectionContextError
from desktop_pet.settings_manager import ApiSettings
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until


def source():
    return SelectionContext("SELECTED_FIXTURE 报错需要解释", title="当前测试网页",
                            url="https://example.invalid/current?private=do-not-store#fragment",
                            page_text="PAGE_CONTEXT_FIXTURE 这是当前网页附近的合成正文。",
                            captured_at="2026-10-31T09:00:00Z", app_name="测试浏览器")


def attachments(messages):
    return [item for item in messages if item["content"].startswith("【问问鲸鱼娘")]


class Client:
    def __init__(self, *, fail=False, hold=False):
        self.contexts, self.fail, self.hold = [], fail, hold
        self.entered, self.release = threading.Event(), threading.Event()

    def complete_context(self, settings, key, messages, **kwargs):
        self.contexts.append(deepcopy(messages))
        self.entered.set()
        if self.hold:
            self.release.wait(5)
        if self.fail:
            raise ApiError("network", "合成网络故障")
        return ChatReply("这是合成的解释回答", "thinking")


class SelectionDataTests(unittest.TestCase):
    def test_plain_text_is_bounded_and_private_url_components_are_removed(self):
        context = SelectionContext("x" * (MAX_SELECTION_CHARACTERS + 10),
            page_text="p" * (MAX_PAGE_CHARACTERS + 10),
            url="https://account:private@example.invalid:8443/page?token=private#secret")
        self.assertEqual(len(context.text), MAX_SELECTION_CHARACTERS)
        self.assertEqual(len(context.page_text), MAX_PAGE_CHARACTERS)
        self.assertTrue(context.text_truncated)
        self.assertTrue(context.page_text_truncated)
        self.assertEqual(context.url, "https://example.invalid:8443/page")
        self.assertEqual(SelectionContext.from_payload(context.to_dict()), context)

    def test_payload_validation_and_known_secret_redaction(self):
        for payload in (None, {}, {"text": " "}, {"text": []}, {"text": "x", "page_text": {}},
                        {"text": "x", "captured_at": "2026-10-31"}):
            with self.subTest(payload=payload), self.assertRaises(SelectionContextError):
                SelectionContext.from_payload(payload)
        context = SelectionContext.from_payload({"text": "value " + LOCAL_TEST_KEY,
                                                  "title": LOCAL_TEST_KEY,
                                                  "notes": ["note " + LOCAL_TEST_KEY]}, api_key=LOCAL_TEST_KEY)
        self.assertNotIn(LOCAL_TEST_KEY, json.dumps(context.to_dict()))
        self.assertEqual(SelectionContext("x", url="file:///private/doc.txt").url, "")

    def test_attachment_is_user_reference_and_preserves_current_question(self):
        context = source()
        result = ContextAssembler().assemble(system_prompt="system", user_text="请解释这个错误",
            recent_messages=[], selection_contexts=[{"conversation_id": "one", "context": context.to_dict()}])
        attachment = attachments(result.messages)[0]
        self.assertEqual(attachment["role"], "user")
        self.assertIn("SELECTED_FIXTURE", attachment["content"])
        self.assertIn("PAGE_CONTEXT_FIXTURE", attachment["content"])
        self.assertIn("不可信", attachment["content"])
        self.assertNotIn("private=", attachment["content"])
        self.assertEqual(result.messages[-1]["content"], "请解释这个错误")

    def test_bounded_selection_never_expands_total_context_or_truncates_question(self):
        for total in (512, 1024, 2048, 4096):
            budget = replace(ContextBudget(), total_tokens=total, output_reserve=128, system_tokens=128)
            result = ContextAssembler(budget).assemble(system_prompt="system", user_text="完整当前问题",
                summary="summary" * 2000,
                recent_messages=[{"role": "assistant", "content": "recent" * 1000}],
                selection_contexts=[{"conversation_id": "one", "context": SelectionContext("鲸鱼" * 10000,
                                                          page_text="正文" * 20000).to_dict()}])
            self.assertEqual(result.messages[-1]["content"], "完整当前问题")
            self.assertLessEqual(result.estimated_input_tokens + result.output_reserve, total)
            for attachment in attachments(result.messages):
                self.assertLessEqual(estimate_tokens(attachment["content"]) + 12, budget.selection_tokens)
                payload = json.loads(attachment["content"].split("\n", 2)[2])
                self.assertTrue(payload["selected_text_truncated"])

    def test_selection_cannot_mix_conversations_or_accept_unbound_material(self):
        for selection in ({"context": source().to_dict()},
                          {"conversation_id": "different", "context": source().to_dict()}):
            with self.assertRaises(ContextBudgetError):
                ContextAssembler().assemble(system_prompt="system", user_text="question",
                    recent_messages=[{"conversation_id": "one", "role": "user", "content": "history"}],
                    selection_contexts=[selection])

    def test_required_source_reports_insufficient_budget_instead_of_sending_without_it(self):
        budget = replace(ContextBudget(), total_tokens=512, output_reserve=128, system_tokens=64)
        with self.assertRaisesRegex(ContextBudgetError, "选中资料"):
            ContextAssembler(budget).assemble(system_prompt="system", user_text="question",
                recent_messages=[], selection_contexts=[{"conversation_id": "one", "context": source().to_dict(),
                                                        "required": True}])


class SelectionChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-selection-chat-")
        self.base = Path(self.temp.name)
        self.settings = isolated_settings(self.base / "settings.ini", MemoryCredentialBackend())
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "selection-fixture", 3))
        self.settings.save_api_key(LOCAL_TEST_KEY)
        self.store = ConversationStore(self.base / "archive.sqlite3")
        self.controllers, self.clients = [], []

    def chat(self, client=None, **kwargs):
        client = client or Client()
        self.clients.append(client)
        chat = ChatController(self.settings, client, store=self.store,
                              metadata_client_factory=lambda: None, **kwargs)
        self.controllers.append(chat)
        return chat

    def tearDown(self):
        for client in self.clients:
            client.release.set()
        for chat in self.controllers:
            chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for chat in self.controllers:
            chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def test_selection_is_saved_once_and_followup_uses_same_session_source(self):
        client = Client()
        chat = self.chat(client)
        cid = chat.current_conversation_id
        self.assertTrue(chat.send("解释选中内容", selection_context=source()))
        spin_until(lambda: not chat.busy)
        rows = self.store.load_messages(cid)
        self.assertEqual(rows[0]["metadata"]["selection_context"], source().to_dict())
        self.assertEqual(rows[0]["metadata"]["request_status"], "complete")
        self.assertEqual(len(rows), 2)
        self.assertTrue(chat.send("那应该怎么解决？"))
        spin_until(lambda: not chat.busy)
        self.assertIn("PAGE_CONTEXT_FIXTURE", attachments(client.contexts[-1])[0]["content"])

    def test_failure_restart_retry_retains_original_source_without_duplicate_message(self):
        failed = Client(fail=True)
        chat = self.chat(failed)
        cid = chat.current_conversation_id
        self.assertTrue(chat.send("请解释", selection_context=source().to_dict()))
        spin_until(lambda: not chat.busy)
        original = self.store.load_messages(cid)[0]
        self.assertEqual(original["metadata"]["request_status"], "failed")
        self.assertIn("selection_context", chat.messages[0]["metadata"])
        chat.shutdown()
        client = Client()
        restored = self.chat(client)
        self.assertTrue(restored.open_conversation(cid))
        spin_until(lambda: not restored.history_loading)
        self.assertTrue(restored.retry(cid, original["id"]))
        spin_until(lambda: not restored.busy)
        rows = self.store.load_messages(cid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], original["id"])
        self.assertEqual(rows[0]["metadata"]["selection_context"], source().to_dict())
        self.assertEqual(attachments(client.contexts[0]), attachments(failed.contexts[0]))

    def test_new_conversation_does_not_inherit_selected_page(self):
        client = Client()
        chat = self.chat(client)
        self.assertTrue(chat.send("解释页面", selection_context=source()))
        spin_until(lambda: not chat.busy)
        first_id = chat.current_conversation_id
        chat.new_conversation()
        self.assertNotEqual(chat.current_conversation_id, first_id)
        self.assertTrue(chat.send("一个全新的问题"))
        spin_until(lambda: not chat.busy)
        self.assertEqual(attachments(client.contexts[-1]), [])
        self.assertNotIn("SELECTED_FIXTURE", json.dumps(client.contexts[-1]))

    def test_selected_page_never_enters_automatic_memory_extraction(self):
        class Memory:
            def __init__(self):
                self.extractions = []
            def load_settings(self):
                return SimpleNamespace(enabled=True, automatic=True, relevance_threshold=.6)
            def search(self, query, limit=3):
                return []
            def extract(self, messages, **kwargs):
                self.extractions.extend(messages)
                return []
        memory = Memory()
        chat = self.chat(memory_service=memory)
        self.assertTrue(chat.send("帮我解释页面", selection_context=source()))
        spin_until(lambda: not chat.busy and QThreadPool.globalInstance().activeThreadCount() == 0)
        self.assertEqual(memory.extractions, [])

    def test_stop_preserves_selection_metadata_and_discards_late_reply(self):
        client = Client(hold=True)
        chat = self.chat(client)
        cid = chat.current_conversation_id
        self.assertTrue(chat.send("解释页面", selection_context=source()))
        spin_until(client.entered.is_set)
        self.assertTrue(chat.stop_request())
        client.release.set()
        spin_until(lambda: not chat._requests)
        rows = self.store.load_messages(cid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metadata"]["request_status"], "cancelled")
        self.assertEqual(rows[0]["metadata"]["selection_context"], source().to_dict())

    def test_invalid_source_creates_no_message_or_request(self):
        client = Client()
        chat = self.chat(client)
        self.assertFalse(chat.send("问题", selection_context={"text": []}))
        self.assertEqual(chat.messages, [])
        self.assertEqual(client.contexts, [])


if __name__ == "__main__":
    unittest.main()
