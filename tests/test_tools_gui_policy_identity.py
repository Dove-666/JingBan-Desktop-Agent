"""Actual host policy/Controller with synthetic in-memory model adapters only."""
from contextlib import contextmanager
import json
import threading
import unittest
from unittest.mock import patch

from tests import test_agent_chat_routing as fixture


class ToolsGuiPolicyIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AgentChatRoutingTests.setUpClass()

    def setUp(self):
        self.owned = fixture.AgentChatRoutingTests()
        self.owned.setUp()
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        self.owned.tearDown()

    def test_tools_without_gui_credential_execute_with_empty_gui_context(self):
        owned = self.owned
        self.assertTrue(owned.chat.send("帮我创建一个测试文件"))
        owned.finish()
        self.assertEqual(len(owned.agent.calls), 1)
        self.assertEqual(owned.agent.calls[0]["context"]["gui_api_key"], "")
        self.assertEqual(owned.agent.calls[0]["request"].kind, "tools")
        self.assertEqual(owned.chat.image_storage_gate.snapshot()["activity_count"], 0)

    def test_saved_gui_credential_is_host_only_and_redacted_from_tool_inputs_and_reply(self):
        owned = self.owned
        owned.settings.save_agent_gui_api_key(fixture.GUI_KEY)
        self.assertTrue(owned.chat.refresh_agent_queue_policy())
        owned.agent.result = fixture.AgentResult("工具结果回显 " + fixture.GUI_KEY, "neutral")
        prepared_flags = []
        actual = owned.chat._agent_queue.reserve_guard
        @contextmanager
        def inspect(request, context=None, *, prepared=None):
            prepared_flags.append(bool(prepared and prepared["gui_key"] == fixture.GUI_KEY))
            with actual(request, context, prepared=prepared):
                yield
        with patch.object(owned.chat._agent_queue, "reserve_guard", inspect):
            self.assertTrue(owned.chat.send("帮我创建一个测试文件，误粘贴资料 " + fixture.GUI_KEY))
            owned.finish()
        self.assertEqual(len(owned.agent.calls), 1)
        self.assertEqual(prepared_flags, [True])
        self.assertEqual(owned.agent.calls[0]["context"]["gui_api_key"], "")
        self.assertEqual(owned.agent.calls[0]["request"].kind, "tools")
        serialized = json.dumps({
            "agent_calls": owned.agent.calls,
            "ordinary_calls": owned.api.calls,
            "archive": owned.chat.store.load_messages(owned.chat.current_conversation_id),
            "visible_messages": owned.chat.messages,
        }, default=str, ensure_ascii=False)
        self.assertNotIn(fixture.GUI_KEY, serialized)
        self.assertIn("已隐藏密钥", serialized)

    def test_published_gui_credential_change_after_preparation_cancels_before_reservation(self):
        owned = self.owned
        owned.settings.save_agent_gui_api_key(fixture.GUI_KEY)
        self.assertTrue(owned.chat.refresh_agent_queue_policy())
        entered = threading.Event()
        prepared_flags = []
        actual = owned.chat._agent_queue.reserve_guard
        @contextmanager
        def held(request, context=None, *, prepared=None):
            prepared_flags.append(bool(prepared and prepared["gui_key"] == fixture.GUI_KEY))
            entered.set()
            if not self.release.wait(4):
                raise AssertionError("OWNED_POLICY_BOUNDARY_NOT_RELEASED")
            with actual(request, context, prepared=prepared):
                yield
        with patch.object(owned.chat._agent_queue, "reserve_guard", held):
            self.assertTrue(owned.chat.send("帮我创建一个测试文件"))
            owned.spin(entered.is_set)
            owned.settings.save_agent_gui_api_key("changed-synthetic-gui-credential")
            self.assertTrue(owned.chat.refresh_agent_queue_policy())
            self.release.set()
            owned.spin(lambda: not owned.chat._requests and not owned.chat._pending_cancel_count.get(owned.chat.current_conversation_id))
        self.assertEqual(prepared_flags, [True])
        self.assertEqual(owned.agent.calls, [])
        self.assertEqual(len(owned.api.calls), 1)
        archived = owned.chat.store.load_messages(owned.chat.current_conversation_id)
        self.assertEqual(len(archived), 1)
        self.assertFalse(archived[0]["metadata"].get("agent_execution_started", False))
        self.assertEqual(archived[0]["metadata"]["request_status"], "cancelled")
        self.assertEqual(owned.chat.image_storage_gate.snapshot()["activity_count"], 0)


if __name__ == "__main__":
    unittest.main()
