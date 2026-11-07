"""Public trace lifecycle with temporary SQLite, Qt and local-only doubles."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QSettings, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.agents import AgentError, AgentResult, AgentService
from desktop_pet.api_client import MAX_REPLY_CHARACTERS
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore, ContextBudget, StoreError
from desktop_pet.execution_trace import ExecutionTrace, merge_execution, normalize_execution
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
from tests.test_agent_chat_routing import FakeAgent, FakeApi, MemoryVault, KEY, GUI_KEY


class ExecutionTraceDataTests(unittest.TestCase):
    def test_only_public_text_context_survives_with_image_placeholders_and_redacted_keys(self):
        trace = ExecutionTrace("model-" + KEY, lambda text: text.replace(KEY, "[redacted]"))
        result = SimpleNamespace(policy="model", history_messages=2, estimated_input_tokens=123,
                                 model_context_tokens=None, output_reserve=None,
                                 source_counts={"system": 1, "history": 2, "current": 1})
        messages = [{"role": "system", "content": "public policy " + KEY},
                    {"role": "user", "content": [{"type": "text", "text": "question " + KEY},
                                                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET_IMAGE"}},
                                                   {"type": "reasoning", "text": "HIDDEN_MODEL_THOUGHT"}],
                     "reasoning_content": "HIDDEN_MODEL_THOUGHT"},
                    {"role": "assistant", "content": "public answer", "analysis": "HIDDEN_MODEL_THOUGHT"},
                    {"role": "reasoning", "content": "HIDDEN_MODEL_THOUGHT"}]
        trace.context(messages, result, "model-" + KEY)
        snapshot = trace.finish("completed")
        encoded = json.dumps(snapshot)
        self.assertNotIn(KEY, encoded)
        self.assertNotIn("SECRET_IMAGE", encoded)
        self.assertNotIn("data:image", encoded)
        self.assertNotIn("HIDDEN_MODEL_THOUGHT", encoded)
        self.assertIn("[图片内容已省略]", snapshot["context"]["messages"][1]["content"])
        self.assertEqual(snapshot["context"]["history_messages"], 2)
        self.assertIsNone(snapshot["context"]["model_context_tokens"])
        self.assertIsNone(snapshot["context"]["output_tokens"])

    def test_trace_preserves_all_actual_events_and_all_context_messages(self):
        trace = ExecutionTrace("fixture")
        for index in range(1500):
            trace.event(f"tool:{index}", "tool", "读取文件", "completed", step=index + 1)
        messages = [{"role": "user", "content": "完整上下文 " + str(index)} for index in range(120)]
        trace.context(messages, SimpleNamespace(), "fixture")
        snapshot = trace.finish("completed")
        self.assertEqual(len(snapshot["events"]), 1502)
        self.assertEqual(len(snapshot["context"]["messages"]), 120)
        self.assertEqual(snapshot["events"][-2]["detail"], "第 1500 步")

    def test_terminal_state_rejects_late_progress_even_with_larger_elapsed_time(self):
        trace = ExecutionTrace("fixture")
        running = trace.snapshot()
        stopped = trace.finish("cancelled")
        self.assertFalse(trace.event("late", "computer", "晚到操作"))
        trace.finish("completed")
        self.assertEqual(trace.snapshot()["state"], "cancelled")
        running["duration_ms"] = stopped["duration_ms"] + 10000
        self.assertEqual(merge_execution(stopped, running)["state"], "cancelled")

    def test_store_copies_completed_trace_atomically_and_recovers_interrupted_trace(self):
        with tempfile.TemporaryDirectory(prefix="aura-trace-store-") as directory:
            path = Path(directory) / "archive.sqlite3"
            store = ConversationStore(path)
            trace = ExecutionTrace("fixture")
            user = store.append_message("session", "user", "question", "user",
                                        metadata={"request_status": "pending", "execution": trace.snapshot()})
            execution = trace.finish("needs_user")
            assistant = store.complete_request("session", user["id"], "请在窗口内完成登录", execution=execution)
            self.assertEqual(assistant["metadata"]["execution"], execution)
            self.assertEqual(store.load_messages("session")[0]["metadata"]["execution"], execution)
            pending = ExecutionTrace("fixture")
            orphan = store.append_message("orphan", "user", "interrupted", "user",
                                          metadata={"request_status": "pending", "execution": pending.snapshot()})
            recovered, _ = store.reconcile_request("orphan", orphan["id"])
            self.assertEqual(recovered["metadata"]["execution"]["state"], "interrupted")
            resumed = ConversationStore(path)
            self.assertEqual(resumed.load_messages("orphan")[0]["metadata"]["execution"]["state"], "interrupted")
            late = pending.snapshot()
            late["duration_ms"] += 10000
            store.update_execution("orphan", orphan["id"], late)
            self.assertEqual(store.load_messages("orphan")[0]["metadata"]["execution"]["state"], "interrupted")


class PublicEventAgent(AgentService):
    def __init__(self):
        super().__init__()
        self.calls = []

    def execute(self, settings, key, request, context, cancellation_event=None,
                progress_callback=None, event_callback=None, interaction_callback=None,
                image_resolver=None, diagnostic_callback=None, worker_lifecycle_callback=None,
                worker_dispatch_guard=None, execution_attempt_callback=None, guardian_launch=None):
        self.calls.append(context)
        progress_callback("planning")
        event_callback({"id": "tool:read_file:1", "kind": "tool", "label": "读取文件", "status": "running",
                        "step": 1, "reasoning": "HIDDEN_THOUGHT", "arguments": {"key": KEY}})
        event_callback({"id": "tool:read_file:1", "kind": "tool", "label": "读取文件", "status": "completed",
                        "step": 1, "detail": "HIDDEN_THOUGHT", "arguments": {"key": KEY}})
        return AgentResult("已确认测试工具结果。")


class ExecutionTraceControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-trace-controller-")
        self.path = Path(self.directory.name)
        self.settings = SettingsManager(settings=QSettings(str(self.path / "settings.ini"), QSettings.Format.IniFormat),
                                        credential_store=CredentialStore(backend=MemoryVault()))
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "fixture-model", 5))
        self.settings.save_api_key(KEY)
        self.settings.save_agent_gui_api_key(GUI_KEY)
        self.api, self.agent = FakeApi(), FakeAgent()
        self.api.route = None
        self.controllers = []
        self.chat = self.controller(self.agent)

    def controller(self, agent, path=None):
        chat = ChatController(self.settings, self.api,
                              store=ConversationStore(path or self.path / f"archive-{len(self.controllers)}.sqlite3"),
                              agent_service=agent, metadata_client_factory=lambda: None)
        self.controllers.append(chat)
        return chat

    def spin(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.005)
        self.assertTrue(condition(), "local trace worker did not settle")

    def finish(self, chat=None):
        chat = chat or self.chat
        self.spin(lambda: not chat.busy and not chat._runner._tasks)
        self.app.processEvents()

    def tearDown(self):
        self.api.release.set()
        self.agent.release.set()
        for chat in self.controllers:
            chat.shutdown()
        self.spin(lambda: not QThreadPool.globalInstance().activeThreadCount())
        for chat in self.controllers:
            chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def test_normal_chat_live_phases_completed_context_and_restart(self):
        self.api.hold = True
        self.api.reply = "普通回复 " + GUI_KEY
        self.assertTrue(self.chat.send("你好，测试上下文 " + KEY))
        self.spin(self.api.started.is_set)
        self.spin(lambda: any(event["id"] == "phase:generating_reply"
                             for event in self.chat.messages[0]["metadata"]["execution"]["events"]))
        running = self.chat.messages[0]["metadata"]["execution"]
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["context"]["policy"], "model")
        self.assertEqual(running["context"]["messages"], self.api.calls[0]["messages"])
        self.api.release.set()
        self.finish()
        archived = self.chat.store.load_messages(self.chat.current_conversation_id)
        execution = archived[-1]["metadata"]["execution"]
        self.assertEqual(execution["state"], "completed")
        self.assertEqual(archived[0]["metadata"]["execution"], execution)
        self.assertGreaterEqual(execution["duration_ms"], running["duration_ms"])
        self.assertEqual(execution["events"][-1]["kind"], "summary")
        self.assertIsNone(execution["context"]["output_tokens"])
        self.assertNotIn(KEY, json.dumps(archived))
        self.assertNotIn(GUI_KEY, json.dumps(archived))
        conversation_id = self.chat.current_conversation_id
        self.chat.shutdown()
        resumed = self.controller(FakeAgent(), self.chat.store.path)
        self.assertTrue(resumed.open_conversation(conversation_id))
        self.spin(lambda: not resumed.history_loading)
        self.assertEqual(resumed.messages[-1]["metadata"]["execution"], execution)

    def test_agent_actual_public_events_are_recorded_without_tool_arguments_or_reasoning(self):
        agent = PublicEventAgent()
        chat = self.controller(agent)
        self.api.route = {"kind": "tools", "task": "read fixture"}
        self.api.reply = "我会读取测试文件并核对结果。" + KEY + GUI_KEY
        self.assertTrue(chat.send("帮我读取测试文件"))
        self.finish(chat)
        execution = chat.messages[-1]["metadata"]["execution"]
        tools = [event for event in execution["events"] if event["kind"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["label"], "读取文件")
        self.assertEqual(tools[0]["status"], "completed")
        self.assertEqual(tools[0]["detail"], "第 1 步")
        delegation = next(event for event in execution["events"] if event["id"] == "summary:delegation")
        self.assertIn("模型公开回复", delegation["label"])
        self.assertIn("我会读取测试文件", delegation["detail"])
        self.assertNotIn("HIDDEN_THOUGHT", json.dumps(execution))
        self.assertNotIn(KEY, json.dumps(execution))
        self.assertNotIn(GUI_KEY, json.dumps(execution))
        self.assertEqual(agent.calls[0]["messages"], execution["context"]["messages"])
        self.assertEqual(execution["context"]["agent_model"], "fixture-model")

    def test_cancelled_agent_trace_survives_late_progress_reply_and_sqlite_snapshot(self):
        self.api.route = {"kind": "tools", "task": "fixture"}
        self.agent.hold = self.agent.ignore_cancellation = True
        self.assertTrue(self.chat.send("帮我创建一个测试文件"))
        self.spin(self.agent.started.is_set)
        conversation_id = self.chat.current_conversation_id
        user_id = self.chat.messages[0]["id"]
        late_running = deepcopy(self.chat.messages[0]["metadata"]["execution"])
        self.assertTrue(self.chat.stop_request())
        self.agent.progress("computer")
        self.agent.release.set()
        self.finish()
        late_running["duration_ms"] += 10000
        self.chat.store.update_execution(conversation_id, user_id, late_running)
        self.chat._execution_changed.emit(conversation_id, user_id, late_running)
        self.app.processEvents()
        archived = self.chat.store.load_messages(conversation_id)
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["metadata"]["execution"]["state"], "cancelled")
        self.assertEqual(self.chat.messages[0]["metadata"]["execution"]["state"], "cancelled")

    def test_needs_user_and_failed_trace_states_are_persisted(self):
        self.api.route = {"kind": "tools", "task": "fixture"}
        self.agent.result = AgentResult("请完成当前窗口。", status="needs_user")
        self.assertTrue(self.chat.send("帮我创建一个测试文件"))
        self.finish()
        self.assertEqual(self.chat.messages[-1]["metadata"]["execution"]["state"], "needs_user")
        self.agent.failure = AgentError("backend_failed", "固定测试错误")
        self.assertTrue(self.chat.send("帮我创建另一个测试文件"))
        self.finish()
        failed = self.chat.messages[-1]
        self.assertEqual(failed["role"], "user")
        self.assertEqual(failed["metadata"]["execution"]["state"], "failed")
        self.assertEqual(self.chat.store.load_messages(self.chat.current_conversation_id)[-1]["metadata"]["execution"]["state"], "failed")

    def test_model_context_keeps_full_reply_beyond_legacy_output_character_limit(self):
        self.api.reply = "完整正文。" * (MAX_REPLY_CHARACTERS // 5 + 100)
        self.assertGreater(len(self.api.reply), MAX_REPLY_CHARACTERS)
        self.assertTrue(self.chat.send("你好，回复正文长度测试"))
        self.finish()
        self.assertEqual(self.chat.messages[-1]["content"], self.api.reply)

    def test_model_context_and_agent_receive_all_fifty_history_messages(self):
        agent = PublicEventAgent()
        chat = self.controller(agent)
        conversation_id = chat.current_conversation_id
        for index in range(25):
            chat.store.append_message(conversation_id, "user", f"history user {index}", "user")
            chat.store.append_message(conversation_id, "assistant", f"history assistant {index}", "assistant")
        self.api.route = {"kind": "tools", "task": "fixture"}
        self.assertTrue(chat.send("帮我读取测试文件"))
        self.finish(chat)
        execution = chat.messages[-1]["metadata"]["execution"]
        self.assertEqual(execution["context"]["history_messages"], 50)
        self.assertEqual(agent.calls[0]["messages"], execution["context"]["messages"])
        self.assertEqual(sum(message["role"] in {"user", "assistant"}
                             for message in agent.calls[0]["messages"]), 51)

    def test_model_context_keeps_already_summarized_history_in_request_trace_and_agent(self):
        agent = PublicEventAgent()
        chat = self.controller(agent)
        conversation_id = chat.current_conversation_id
        history = [chat.store.append_message(conversation_id, role, f"original history {index}", role)
                   for index, role in enumerate(("user", "assistant") * 3)]
        summary = "existing summary reference"
        chat.store.update_metadata(conversation_id, summary=summary, summary_through_id=history[3]["id"])
        self.api.route = {"kind": "tools", "task": "fixture"}
        user_text = "帮我读取测试文件"
        self.assertTrue(chat.send(user_text))
        self.finish(chat)
        execution = chat.messages[-1]["metadata"]["execution"]
        sent = self.api.calls[-1]["messages"]
        self.assertEqual([message["content"] for message in sent if message["role"] in {"user", "assistant"}],
                         [message["content"] for message in history] + [user_text])
        self.assertTrue(any(summary in message["content"] for message in sent if message["role"] == "system"))
        self.assertEqual(execution["context"]["history_messages"], len(history))
        self.assertEqual(execution["context"]["messages"], sent)
        self.assertEqual(agent.calls[0]["messages"], sent)

    def test_explicit_manual_context_still_uses_existing_summary_cursor(self):
        self.chat._budget = ContextBudget(mode="manual")
        conversation_id = self.chat.current_conversation_id
        history = [self.chat.store.append_message(conversation_id, role, f"manual history {index}", role)
                   for index, role in enumerate(("user", "assistant") * 3)]
        self.chat.store.update_metadata(conversation_id, summary="manual summary",
                                        summary_through_id=history[3]["id"])
        user_text = "你好，检查手动上下文兼容"
        self.assertTrue(self.chat.send(user_text))
        self.finish()
        self.assertEqual([message["content"] for message in self.api.calls[-1]["messages"]
                          if message["role"] in {"user", "assistant"}],
                         [message["content"] for message in history[4:]] + [user_text])

    def test_reply_commit_failure_does_not_leave_a_completed_execution(self):
        with patch.object(self.chat.store, "complete_request", side_effect=StoreError("固定归档错误")):
            self.assertTrue(self.chat.send("你好，回复提交故障测试"))
            self.finish()
        archived = self.chat.store.load_messages(self.chat.current_conversation_id)
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["metadata"]["execution"]["state"], "failed")
        self.assertEqual(self.chat.messages[0]["metadata"]["execution"]["state"], "failed")

    def test_new_gui_credential_saved_while_router_waits_is_also_redacted_from_public_plan(self):
        self.api.route = {"kind": "tools", "task": "fixture"}
        self.api.hold = True
        new_key = "new-during-request-secret"
        self.assertTrue(self.chat.send("帮我读取测试文件"))
        self.spin(self.api.started.is_set)
        self.settings.save_agent_gui_api_key(new_key)
        self.api.reply = "公开分派说明 " + new_key + GUI_KEY
        self.api.release.set()
        self.finish()
        archived = self.chat.store.load_messages(self.chat.current_conversation_id)
        self.assertNotIn(new_key, json.dumps(archived))
        self.assertNotIn(GUI_KEY, json.dumps(archived))


if __name__ == "__main__":
    unittest.main(verbosity=2)
