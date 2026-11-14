"""Public configuration, character and question boundaries of the DSH migration."""
import unittest
from uuid import uuid4

from desktop_pet.agents.settings import AgentSettings, load_agent_settings
from desktop_pet.agents.interactions import checked_interaction
from desktop_pet.character_cards import CharacterCard
from desktop_pet.character_prompts import harness_personality
from desktop_pet.ui.agent_interaction import public_interaction


class Store:
    def __init__(self, values):
        self.values = values

    def value(self, key, default=None):
        return self.values.get(key, default)


class DshCompatibilityTests(unittest.TestCase):
    def test_old_backend_migrates_without_enabling_disabled_permissions(self):
        value = load_agent_settings(Store({"agents/backend": "nanobot",
            "agents/computer_enabled": False, "agents/tools_enabled": True}))
        self.assertEqual(value.backend, "deepseek-harness")
        self.assertTrue(value.enabled)
        self.assertTrue(value.tools_enabled)
        self.assertFalse(value.computer_enabled)
        self.assertNotIn("nanobot", value.python_executable)

    def test_default_has_no_artificial_step_or_total_time_limit(self):
        value = AgentSettings()
        self.assertEqual((value.max_steps, value.timeout_seconds), (0, 0))
        value.validate()

    def test_character_original_macro_does_not_import_chat_json_reply_protocol(self):
        value = harness_personality(CharacterCard(name="小鲸", personality="活泼", system_prompt="{{original}} {{char}}"))
        self.assertIn("小鲸", value)
        self.assertIn("活泼", value)
        self.assertNotIn("agent_request", value)
        self.assertNotIn('"reply"', value)
        self.assertNotIn("{{original}}", value)

    def test_long_plan_is_preserved_at_host_and_public_ui_boundary(self):
        question = "需要确认的计划\n" + "先读取目录，再输出报告。" * 500
        checked = checked_interaction({"interaction_id": "interaction-" + uuid4().hex,
            "kind": "question", "question": question, "options": ["批准", "拒绝"]})
        cid = str(uuid4())
        checked["conversation_id"] = cid
        self.assertEqual(public_interaction(checked, cid)["question"], question)
        with self.assertRaises(ValueError):
            checked_interaction({"interaction_id": "interaction-" + uuid4().hex,
                "kind": "question", "question": "字" * 16001})


if __name__ == "__main__":
    unittest.main()
