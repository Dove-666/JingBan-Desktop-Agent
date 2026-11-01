"""Production chat supplies a generation boundary for durable native replay."""
import unittest

from tests import test_agent_chat_routing as fixtures


class AgentGenerationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.AgentChatRoutingTests.setUpClass.__func__)
    setUp = fixtures.AgentChatRoutingTests.setUp
    tearDown = fixtures.AgentChatRoutingTests.tearDown
    controller = fixtures.AgentChatRoutingTests.controller
    spin = fixtures.AgentChatRoutingTests.spin
    finish = fixtures.AgentChatRoutingTests.finish

    def test_followups_share_generation_but_clear_starts_a_new_generation(self):
        self.assertTrue(self.chat.send("创建本轮测试材料", agent_mode=True))
        self.finish()
        cid = self.chat.current_conversation_id
        first = self.chat.store.load_messages(cid)[0]["id"]
        self.assertEqual(self.agent.calls[-1]["context"]["conversation_generation"], first)
        self.assertTrue(self.chat.send("继续检查刚才的材料", agent_mode=True))
        self.finish()
        self.assertEqual(self.agent.calls[-1]["context"]["conversation_generation"], first)
        self.chat.clear()
        self.finish()
        self.assertEqual(self.chat.store.load_messages(cid), [])
        self.assertTrue(self.chat.send("这次是新的任务", agent_mode=True))
        self.finish()
        current = self.agent.calls[-1]["context"]["conversation_generation"]
        self.assertNotEqual(current, first)
        self.assertEqual(current, self.chat.store.load_messages(cid)[0]["id"])

    def test_different_conversations_do_not_share_generation(self):
        self.assertTrue(self.chat.send("第一段对话", agent_mode=True))
        self.finish()
        first = self.agent.calls[-1]["context"]["conversation_generation"]
        self.chat.new_conversation()
        self.finish()
        self.assertTrue(self.chat.send("第二段对话", agent_mode=True))
        self.finish()
        self.assertNotEqual(first, self.agent.calls[-1]["context"]["conversation_generation"])


if __name__ == "__main__":
    unittest.main()
