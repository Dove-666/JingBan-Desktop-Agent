"""Owned, model-free request interleavings across Stop and Agent timeout."""
from copy import deepcopy
from contextlib import ExitStack
import threading
import unittest
from unittest.mock import patch

from desktop_pet.agents import AgentError
from desktop_pet.api_client import ChatReply
from tests import test_agent_chat_routing as fixture


class HeldChat:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def complete_context(self, settings, key, messages, **_options):
        self.calls.append(deepcopy(messages))
        self.started.set()
        if not self.release.wait(4):
            raise AssertionError("Owned chat release was not supplied")
        return ChatReply("NEW_CHAT_REPLY")

    def cancel_request(self, event):
        event.set()

    def shutdown(self):
        self.release.set()


class AgentResumeFlowReviewTests(unittest.TestCase):
    setUpClass = classmethod(fixture.AgentChatRoutingTests.setUpClass.__func__)
    tearDown = fixture.AgentChatRoutingTests.tearDown
    controller = fixture.AgentChatRoutingTests.controller
    spin = fixture.AgentChatRoutingTests.spin
    finish = fixture.AgentChatRoutingTests.finish

    def setUp(self):
        # Real immutable empty snapshots, without reading machine-local runtime
        # configuration. These tests exercise request interleavings, not tools.
        from desktop_pet.agents import office_runtime, execution_runtime
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(office_runtime, "load_office_runtime", return_value=None))
        stack.enter_context(patch.object(execution_runtime, "load_execution_runtime", return_value=None))
        fixture.AgentChatRoutingTests.setUp(self)

    def test_stopped_agent_late_progress_and_result_cannot_replace_new_chat(self):
        client = HeldChat()
        self.api = client
        self.chat = self.controller(api=client)
        cid = self.chat.current_conversation_id
        self.agent.hold = self.agent.ignore_cancellation = True
        self.assertTrue(self.chat.send("检查自有测试文件", agent_mode=True))
        self.spin(self.agent.started.is_set)
        old_id = self.chat.messages[0]["id"]
        old_progress = self.agent.progress
        self.assertTrue(self.chat.stop_request(cid))
        self.assertTrue(self.chat.send("你好，继续聊一句", agent_mode=False))
        self.spin(client.started.is_set)
        current = self.chat._active_request
        token, phase = self.chat._request_id, self.chat._request_phase
        old_progress("computer")
        self.agent.release.set()
        self.spin(lambda: (cid, old_id) not in self.chat._requests)
        self.assertTrue(self.chat.busy)
        self.assertIs(self.chat._active_request, current)
        self.assertEqual(self.chat._request_id, token)
        self.assertEqual(self.chat._request_phase, phase)
        self.assertEqual(self.chat.error, "")
        self.assertNotIn("任务已完成", str(self.chat.messages))
        client.release.set()
        self.finish()
        rows = self.chat.store.load_messages(cid)
        self.assertEqual([row["content"] for row in rows],
                         ["检查自有测试文件", "你好，继续聊一句", "NEW_CHAT_REPLY"])
        self.assertEqual(rows[0]["metadata"]["request_status"], "cancelled")
        self.assertEqual(rows[1]["metadata"]["request_status"], "complete")
        self.assertFalse(self.chat.error)
        self.assertFalse(self.chat.work_active)

    def test_timeout_callback_cannot_overwrite_synchronously_admitted_next_agent(self):
        cid = self.chat.current_conversation_id
        self.agent.failure = AgentError("timeout", "OWNED_TIMEOUT_CANARY")
        admitted = []

        def start_next(busy):
            if not busy and not admitted:
                admitted.append("starting")
                self.agent.failure = None
                self.agent.hold = True
                admitted.append(self.chat.send("再次检查自有测试文件", agent_mode=True))

        self.chat.busy_changed.connect(start_next)
        self.assertTrue(self.chat.send("检查第一份自有测试文件", agent_mode=True))
        self.spin(lambda: len(self.agent.calls) == 2)
        self.assertEqual(admitted, ["starting", True])
        self.assertTrue(self.chat.busy)
        self.assertTrue(self.chat.can_stop)
        self.assertTrue(self.chat.work_active)
        self.assertEqual(self.chat._active_request["content"], "再次检查自有测试文件")
        self.assertEqual(self.chat.error, "")
        self.assertEqual([row["content"] for row in self.chat.messages],
                         ["检查第一份自有测试文件", "再次检查自有测试文件"])
        self.assertEqual(self.chat.messages[0]["metadata"]["request_status"], "failed")
        self.assertEqual(self.chat.messages[0]["metadata"]["agent_error_code"], "timeout")
        self.assertEqual(self.chat.messages[1]["metadata"]["request_status"], "pending")
        self.agent.release.set()
        self.finish()
        rows = self.chat.store.load_messages(cid)
        self.assertEqual([row["metadata"].get("request_status") for row in rows[:2]],
                         ["failed", "complete"])
        self.assertEqual(rows[-1]["role"], "assistant")
        self.assertEqual(len(self.agent.calls), 2)
        self.assertFalse(self.chat.error)
        self.assertFalse(self.chat.work_active)


if __name__ == "__main__":
    unittest.main()
