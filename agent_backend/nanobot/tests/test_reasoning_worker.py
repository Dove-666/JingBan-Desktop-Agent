"""Actual worker + pinned SDK reasoning requests, with a loopback-only model."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_worker as fixtures


class ReasoningWorkerTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.WorkerTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.WorkerTests.tearDownClass.__func__)
    setUp = fixtures.WorkerTests.setUp
    tearDown = fixtures.WorkerTests.tearDown
    payload = fixtures.WorkerTests.payload

    def finish_policy(self, profile, value, *, budget=0, legacy="default", metadata=None):
        payload = self.payload()
        selected = {"version": 1, "profile": profile, "value": value, "budget_tokens": budget}
        if metadata is not None:
            selected["metadata"] = metadata
        payload["config"].update(reasoning=selected, reasoning_effort=legacy)
        self.client.send(payload)
        result = self.client.terminal()
        self.assertEqual(result["type"], "final", result)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(set(result["tools_used"]), {"write_file", "read_file"})
        self.assertGreaterEqual(len(fixtures.ModelHandler.calls), 3)
        return fixtures.ModelHandler.calls

    def test_openai_off_reaches_every_actual_streaming_planner_request(self):
        for body in self.finish_policy("openai", "none", legacy="none"):
            self.assertEqual(body["reasoning_effort"], "none")
            self.assertTrue(body["stream"])
            self.assertNotIn("thinking", body)
            self.assertNotIn("reasoning", body)

    def test_native_thinking_mode_is_not_sent_as_an_effort(self):
        for body in self.finish_policy("thinking", "enabled"):
            self.assertEqual(body["thinking"], {"type": "enabled"})
            self.assertNotIn("reasoning_effort", body)

    def test_budget_is_preserved_on_the_tool_loop(self):
        for body in self.finish_policy("qwen_budget", "enabled", budget=8192):
            self.assertEqual(body["enable_thinking"], True)
            self.assertEqual(body["thinking_budget"], 8192)
            self.assertNotIn("reasoning_effort", body)
            self.assertNotIn("extra_body", body)

    def test_deepseek_uses_native_switch_and_omits_sampling_temperature(self):
        replayed_assistants = []
        for body in self.finish_policy("deepseek", "high", legacy="high"):
            self.assertEqual(body["thinking"], {"type": "enabled"})
            self.assertEqual(body["reasoning_effort"], "high")
            self.assertNotIn("temperature", body)
            replayed_assistants.extend(message for message in body["messages"] if message["role"] == "assistant")
        self.assertTrue(replayed_assistants)
        self.assertTrue(all(message.get("reasoning_content") == "" for message in replayed_assistants))

    def test_claude_adaptive_gateway_gets_both_fields_without_temperature(self):
        for body in self.finish_policy("claude_adaptive", "high"):
            self.assertEqual(body["thinking"], {"type": "adaptive"})
            self.assertEqual(body["output_config"], {"effort": "high"})
            self.assertNotIn("reasoning_effort", body)
            self.assertNotIn("temperature", body)

    def test_openrouter_off_is_a_boolean_and_not_an_openai_effort(self):
        for body in self.finish_policy("openrouter", "none"):
            self.assertEqual(body["reasoning"], {"enabled": False})
            self.assertNotIn("reasoning_effort", body)

    def test_custom_value_is_not_replaced_with_a_global_level(self):
        for body in self.finish_policy("custom", "ultra_custom"):
            self.assertEqual(body["reasoning_effort"], "ultra_custom")

    def test_deepseek_default_does_not_invent_an_enabled_mode_or_replay(self):
        for body in self.finish_policy("deepseek", "default"):
            self.assertFalse({"thinking", "reasoning_effort"} & body.keys())
            self.assertTrue(all("reasoning_content" not in message for message in body["messages"]))

    def test_private_sdk_reasoning_is_not_projected_into_worker_frames(self):
        marker = "PRIVATE_REASONING_DO_NOT_SYNC_93b7"
        fixtures.ModelHandler.reasoning_text = marker
        try:
            self.finish_policy("deepseek", "high", legacy="high")
            self.assertNotIn(marker, json.dumps(self.client.all_frames, ensure_ascii=False))
        finally:
            fixtures.ModelHandler.reasoning_text = ""

    def test_host_to_worker_to_actual_http_and_saved_transcript_share_one_policy(self):
        sys.path.insert(0, str(fixtures.REPO))
        from desktop_pet.agents import AgentRequest, AgentService, AgentSettings
        marker = "PRIVATE_REASONING_NOT_IN_SAVED_TRANSCRIPT_947c"
        fixtures.ModelHandler.reasoning_text = marker
        service = AgentService()
        events = []
        try:
            context = {"api": {"base_url": f"http://127.0.0.1:{self.server.server_port}/v1", "model": "test-model",
                "reasoning_effort": "high", "reasoning_profile": "deepseek", "reasoning_budget_tokens": 0,
                "reasoning_metadata": None}, "messages": [], "conversation_id": str(uuid4())}
            with patch("desktop_pet.agents.office_runtime.load_office_runtime", return_value=None), \
                    patch("desktop_pet.agents.execution_runtime.load_execution_runtime", return_value=None):
                result = service.execute(AgentSettings(enabled=True, python_executable=str(fixtures.PYTHON),
                    workspace=str(self.workspace), timeout_seconds=30, max_steps=5), fixtures.KEY,
                    AgentRequest("创建并读取工作目录里的 result.txt"), context,
                    event_callback=lambda *event: events.append(event))
            self.assertEqual(result.status, "completed")
            self.assertEqual((self.workspace / "result.txt").read_text(), "AURA offline test")
            self.assertGreaterEqual(len(fixtures.ModelHandler.calls), 3)
            for body in fixtures.ModelHandler.calls:
                self.assertEqual(body["reasoning_effort"], "high")
                self.assertEqual(body["thinking"], {"type": "enabled"})
                self.assertNotIn("temperature", body)
            self.assertNotIn(marker, json.dumps(service._sessions, ensure_ascii=False))
            self.assertNotIn(marker, json.dumps(events, ensure_ascii=False))
            self.assertNotIn(marker, result.reply)
        finally:
            fixtures.ModelHandler.reasoning_text = ""
            service.shutdown()

    def test_model_default_sends_no_controls_even_with_conflicting_legacy_field(self):
        for body in self.finish_policy("thinking", "default", legacy="high"):
            self.assertFalse({"thinking", "reasoning_effort", "reasoning", "thinking_budget", "enable_thinking"} & body.keys())

    def test_legacy_frame_off_is_not_dropped_by_the_provider_sdk(self):
        payload = self.payload()
        payload["config"]["reasoning_effort"] = "none"
        self.client.send(payload)
        result = self.client.terminal()
        self.assertEqual(result["type"], "final", result)
        self.assertEqual(result["status"], "completed", result)
        self.assertGreaterEqual(len(fixtures.ModelHandler.calls), 3)
        self.assertTrue(all(body.get("reasoning_effort") == "none" for body in fixtures.ModelHandler.calls))

    def test_unknown_nested_keys_fail_before_the_first_model_request(self):
        payload = self.payload()
        payload["config"]["reasoning"] = {"version": 1, "profile": "thinking", "value": "enabled",
            "budget_tokens": 0, "headers": {"Authorization": "must-never-be-used"}}
        self.client.send(payload)
        self.assertEqual(self.client.terminal()["code"], "INVALID_REQUEST")
        self.assertEqual(fixtures.ModelHandler.calls, [])

    def test_new_native_value_cannot_hide_an_invalid_schema_version(self):
        payload = self.payload()
        payload["config"]["reasoning"] = {"version": True, "profile": "thinking", "value": "enabled", "budget_tokens": 0}
        self.client.send(payload)
        self.assertEqual(self.client.terminal()["code"], "INVALID_REQUEST")
        self.assertEqual(fixtures.ModelHandler.calls, [])

    def test_budget_boolean_is_rejected_before_the_first_model_request(self):
        payload = self.payload()
        payload["config"]["reasoning"] = {"version": 1, "profile": "qwen_budget", "value": "enabled", "budget_tokens": True}
        self.client.send(payload)
        self.assertEqual(self.client.terminal()["code"], "INVALID_REQUEST")
        self.assertEqual(fixtures.ModelHandler.calls, [])


if __name__ == "__main__":
    unittest.main()
