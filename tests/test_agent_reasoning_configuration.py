"""Reasoning controls reach the task frame without importing a model SDK or GUI."""
from contextlib import ExitStack
import unittest
from unittest.mock import patch

from desktop_pet.agents import AgentRequest, AgentService, AgentSettings, AgentValidationError
from desktop_pet.settings_manager import ApiSettings


class AgentReasoningConfigurationTests(unittest.TestCase):
    def context(self, api, *, computer=False):
        # Optional runtimes and capabilities are irrelevant to this pure frame
        # projection; no host configuration, device or process is inspected.
        with ExitStack() as stack:
            stack.enter_context(patch("desktop_pet.agents.office_runtime.load_office_runtime", return_value=None))
            stack.enter_context(patch("desktop_pet.agents.execution_runtime.load_execution_runtime", return_value=None))
            stack.enter_context(patch.object(AgentService, "capabilities", return_value=[]))
            return AgentService._context({"api": api, "messages": [], "gui_api_key": "owned-gui-key"},
                "owned-main-key", AgentRequest("合成测试", kind="computer" if computer else "tools"),
                AgentSettings(gui_base_url="https://gui.invalid/v1", gui_model="independent-gui-model"))[0]

    def api(self, **changes):
        return {"base_url": "https://synthetic.invalid/v1", "model": "test-model", "reasoning_effort": "default",
                "reasoning_profile": "openai", "reasoning_budget_tokens": 0, "reasoning_metadata": None, **changes}

    def test_openai_none_is_explicit_in_the_new_versioned_policy(self):
        config = self.context(self.api(reasoning_effort="none"))
        self.assertEqual(config["reasoning"], {"version": 1, "profile": "openai", "value": "none", "budget_tokens": 0})
        self.assertEqual(config["reasoning_effort"], "none")

    def test_native_provider_mode_does_not_become_a_legacy_effort(self):
        config = self.context(self.api(reasoning_profile="thinking", reasoning_effort="enabled"))
        self.assertEqual(config["reasoning"]["value"], "enabled")
        self.assertEqual(config["reasoning_effort"], "default")

    def test_budget_and_custom_effort_are_preserved_without_coercion(self):
        config = self.context(self.api(reasoning_profile="qwen_budget", reasoning_effort="enabled", reasoning_budget_tokens=8192))
        self.assertEqual(config["reasoning"]["budget_tokens"], 8192)
        custom = self.context(self.api(reasoning_profile="custom", reasoning_effort="ultra_custom"))
        self.assertEqual(custom["reasoning"]["value"], "ultra_custom")
        self.assertEqual(custom["reasoning_effort"], "default")

    def test_declared_metadata_is_copied_and_remains_bound_to_the_api(self):
        metadata = {"version": 1, "base_url": "https://synthetic.invalid/v1", "model": "test-model",
                    "profile": "openai", "values": ["medium", "ultra_custom"]}
        config = self.context(self.api(reasoning_profile="auto", reasoning_effort="ultra_custom", reasoning_metadata=metadata))
        metadata["values"].append("changed-after-admission")
        self.assertEqual(config["reasoning"]["metadata"]["values"], ["medium", "ultra_custom"])
        with self.assertRaises(AgentValidationError):
            self.context(self.api(reasoning_profile="auto", reasoning_effort="ultra_custom",
                reasoning_metadata={**metadata, "model": "other-model"}))

    def test_new_dataclass_fields_are_admitted_and_gui_reuses_global_route(self):
        api = ApiSettings("https://synthetic.invalid/v1", "test-model", 30, "high",
                          reasoning_profile="openai")
        config = self.context(api, computer=True)
        self.assertEqual(config["reasoning"]["value"], "high")
        self.assertEqual(config["gui"]["base_url"], api.base_url)
        self.assertEqual(config["gui"]["model"], api.model)
        self.assertEqual(config["gui"]["coordinate_profile"], "pixel")
        self.assertNotIn("reasoning", config["gui"])
        self.assertNotIn("reasoning_effort", config["gui"])
        self.assertEqual(config["gui"]["api_key"], "owned-main-key")

    def test_legacy_dict_keeps_its_previous_schema(self):
        api = {"base_url": "https://synthetic.invalid/v1", "model": "test-model", "reasoning_effort": "high"}
        config = self.context(api)
        self.assertEqual(config["reasoning_effort"], "high")
        self.assertNotIn("reasoning", config)
        with self.assertRaises(AgentValidationError):
            self.context({**api, "reasoning_effort": "ultra_custom"})

    def test_invalid_or_oversize_settings_are_rejected_before_a_worker_can_start(self):
        for changes in ({"reasoning_budget_tokens": True}, {"reasoning_profile": "unknown"},
                        {"reasoning_effort": "high\ninjected"}, {"reasoning_budget_tokens": -1},
                        {"reasoning_metadata": {"unused": "x" * 17000}},
                        {"reasoning_metadata": {"unused": float("nan")}},
                        {"reasoning_metadata": []}, {"unexpected": {"headers": {}}}):
            with self.subTest(changes=changes), self.assertRaises(AgentValidationError):
                self.context(self.api(**changes))


if __name__ == "__main__":
    unittest.main()
