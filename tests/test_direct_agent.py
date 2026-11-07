"""Explicit desktop Agent admission never requires a routing model or pixels."""
from dataclasses import replace
import json
import unittest
from uuid import uuid4
from unittest.mock import patch

from desktop_pet.agents import AgentError
from desktop_pet.remote.sync import RemoteSyncStore, sanitize_message
from tests import test_agent_chat_routing as routing_fixtures
from tests import test_agent_shutdown_completion as completion_fixtures
from tests import test_remote_desktop_assist as remote_fixtures


class DirectAgentTests(unittest.TestCase):
    setUpClass = classmethod(routing_fixtures.AgentChatRoutingTests.setUpClass.__func__)
    setUp = routing_fixtures.AgentChatRoutingTests.setUp
    tearDown = routing_fixtures.AgentChatRoutingTests.tearDown
    controller = routing_fixtures.AgentChatRoutingTests.controller
    spin = routing_fixtures.AgentChatRoutingTests.spin
    finish = routing_fixtures.AgentChatRoutingTests.finish

    def test_neutral_followup_and_question_use_native_agent_without_chat_classifier(self):
        cid = self.chat.current_conversation_id
        for text in ("看看当前系统情况", "继续", "?"):
            self.assertTrue(self.chat.send(text, agent_mode=True))
            self.finish()
        self.assertEqual(len(self.agent.calls), 3)
        self.assertEqual(self.api.calls, [])
        self.assertEqual([c["request"].task for c in self.agent.calls], ["看看当前系统情况", "继续", "?"])
        self.assertEqual({c["request"].kind for c in self.agent.calls}, {"agent"})
        self.assertEqual({c["context"]["conversation_id"] for c in self.agent.calls}, {cid})
        self.assertIn("任务已完成", str(self.agent.calls[-1]["context"]["messages"]))
        for message in self.chat.store.load_messages(cid)[::2]:
            self.assertEqual(message["metadata"]["execution_mode"], "agent")
            self.assertTrue(message["metadata"]["agent_execution_started"])
            delegation = next(event for event in message["metadata"]["execution"]["events"]
                              if event["id"] == "summary:delegation")
            self.assertEqual(delegation["label"], "任务分派说明（直接请求）")
            self.assertEqual(delegation["detail"], "直接进入电脑 Agent 工具流程。")
            self.assertEqual((delegation["kind"], delegation["status"]), ("summary", "completed"))

    def test_persistent_desktop_choice_does_not_change_other_conversations(self):
        cid = self.chat.current_conversation_id
        self.assertTrue(self.chat.set_agent_mode(True))
        self.assertTrue(self.chat.agent_mode)
        self.chat.new_conversation()
        self.assertFalse(self.chat.agent_mode)
        self.assertTrue(self.settings.load_conversation_agent_mode(cid))

    def test_direct_agent_failure_has_safe_sync_diagnosis_and_no_retry_replay(self):
        self.agent.failure = AgentError("gui_action_failed", "safe fixture failure")
        self.assertTrue(self.chat.send("继续", agent_mode=True))
        self.finish()
        user = self.chat.store.load_messages(self.chat.current_conversation_id)[0]
        public = sanitize_message(user)
        self.assertEqual(public["execution_mode"], "agent")
        self.assertEqual(public["agent_error_code"], "gui_action_failed")
        self.assertIn("鼠标", public["agent_error"])
        self.assertFalse(self.chat.retry())
        self.assertEqual(len(self.agent.calls), 1)

    def test_private_metadata_and_malformed_activity_never_sync(self):
        cid = self.chat.current_conversation_id
        self.chat.store.append_message(cid, "user", "fixture", "user", metadata={
            "request_status": "pending", "execution_mode": "agent", "private_key": "PRIVATE_VALUE",
            "execution": {"events": ["malformed", {"label": "PRIVATE_VALUE"}, {"label": "正在操作电脑…"}]}})
        page = RemoteSyncStore(self.chat.store).snapshot()
        encoded = json.dumps(page, ensure_ascii=False)
        self.assertNotIn("PRIVATE_VALUE", encoded)
        self.assertIn("正在操作电脑…", encoded)


class DirectAgentRpcTests(unittest.TestCase):
    setUpClass = classmethod(remote_fixtures.RemoteDesktopAssistTests.setUpClass.__func__)
    setUp = remote_fixtures.RemoteDesktopAssistTests.setUp
    tearDown = remote_fixtures.RemoteDesktopAssistTests.tearDown
    rpc = remote_fixtures.RemoteDesktopAssistTests.rpc
    configure = remote_fixtures.RemoteDesktopAssistTests.configure
    configure_agent = remote_fixtures.RemoteDesktopAssistTests.configure_agent

    def test_rpc_admits_agent_without_screen_permission_and_deduplicates(self):
        from types import SimpleNamespace
        self.configure(SimpleNamespace(base_url="http://127.0.0.1:1/v1"))
        self.chat._metadata_factory = lambda: None
        agent = self.configure_agent()
        self.assertFalse(self.rpc("status")["result"]["desktop_context"]["available"])
        self.assertTrue(self.rpc("status")["result"]["agent_available"])
        self.assertEqual(self.rpc("status")["result"]["agent_trace_version"], 1)
        request = str(uuid4())
        with patch.object(self.chat, "_client", completion_fixtures.NoProvider()), \
                patch.object(self.remote, "_capture_desktop", side_effect=AssertionError("must not capture")):
            first = self.rpc("agent.send", {"conversation_id": self.cid, "text": "继续"}, request_id=request)
            self.assertTrue(first["ok"], first)
            second = self.rpc("agent.send", {"conversation_id": self.cid, "text": "继续"}, request_id=request)
            self.assertTrue(second["result"]["duplicate"])
            from PySide6.QtCore import QThreadPool
            from PySide6.QtTest import QTest
            for _ in range(600):
                if agent.calls and not self.chat.busy and not QThreadPool.globalInstance().activeThreadCount():
                    break
                QTest.qWait(10)
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(agent.calls[0]["request"].kind, "agent")
        self.assertEqual(agent.calls[0]["context"]["conversation_id"], self.cid)
        self.assertEqual(self.rpc("agent.cancel", {"conversation_id": self.cid})["result"]["cancelled"], False)
        user = next(message for message in self.chat.store.load_messages(self.cid)
                    if message["id"] == first["result"]["message_id"])
        delegation = next(event for event in user["metadata"]["execution"]["events"]
                          if event["id"] == "summary:delegation")
        self.assertEqual(delegation["label"], "任务分派说明（直接请求）")
        self.assertEqual(delegation["detail"], "直接进入电脑 Agent 工具流程。")

    def test_rpc_private_overrides_cannot_enter_agent(self):
        from types import SimpleNamespace
        self.configure(SimpleNamespace(base_url="http://127.0.0.1:1/v1"))
        self.chat._metadata_factory = lambda: None
        agent = self.configure_agent()
        result = self.rpc("agent.send", {"conversation_id": self.cid, "text": "?", "tools_enabled": True})
        self.assertFalse(result["ok"])
        self.assertFalse(self.rpc("agent.send", {"text": "continue"})["ok"])
        self.assertFalse(self.rpc("agent.send", {"conversation_id": self.cid})["ok"])
        self.assertEqual(agent.calls, [])


class RegisteredDirectAgentTraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        completion_fixtures.AgentShutdownCompletionTests.setUpClass()

    def setUp(self):
        self.case = completion_fixtures.AgentShutdownCompletionTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_registered_phone_queue_preserves_direct_request_source(self):
        with patch.object(self.case.chat._client, "complete_context",
                          wraps=self.case.chat._client.complete_context) as classifier:
            ack = self.case.send()
            self.case.wait_gui()
            classifier.assert_not_called()
        self.assertIsNotNone(self.case.chat._agent_queue)
        self.assertEqual(self.case.calls, 1)
        self.assertEqual(self.case.job().state, "completed")
        self.assertIsNotNone(self.case.job().worker_closed_at)
        messages = self.case.store.load_messages(self.case.cid)
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["id"], ack["message_id"])
        execution = messages[0]["metadata"]["execution"]
        self.assertEqual(messages[1]["metadata"]["execution"], execution)
        delegation = next(event for event in execution["events"] if event["id"] == "summary:delegation")
        self.assertEqual(delegation["label"], "任务分派说明（直接请求）")
        self.assertEqual(delegation["detail"], "直接进入电脑 Agent 工具流程。")
        self.assertEqual((delegation["kind"], delegation["status"]), ("summary", "completed"))
        public = next(item["data"] for item in self.case.remote.sync.snapshot()["items"]
                      if item["entity"] == "message" and item["id"] == ack["message_id"])
        public_event = next(event for event in public["agent_trace"]["events"]
                            if event["label"] == delegation["label"])
        index = execution["events"].index(delegation)
        self.assertEqual(public_event, {"id": f"step-{index}", "kind": "summary",
            "label": delegation["label"], "status": "completed", "elapsed_ms": delegation["elapsed_ms"]})


if __name__ == "__main__":
    unittest.main()
