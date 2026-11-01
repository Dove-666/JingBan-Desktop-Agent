"""Model policy uses full admitted context; all fixtures and HTTP are local."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings

from desktop_pet.api_client import ApiClient, ApiError
from desktop_pet.conversations.context import ContextAssembler, ContextBudget, ContextBudgetError
from desktop_pet.model_context import context_window_rejected, model_context_tokens, sanitized_model_context_metadata
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsError, SettingsManager
from tests.mock_api import MockApiServer
from tests.test_settings import FakeVault


class ModelContextTests(unittest.TestCase):
    def test_model_policy_keeps_all_history_system_and_current_without_application_partitions(self):
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"MESSAGE-{i}:" + "原始历史" * 1000,
                    "conversation_id": "same"} for i in range(120)]
        system = "完整角色设定" * 1000
        current = "完整当前问题" * 2000
        result = ContextAssembler(ContextBudget(mode="model")).assemble(system_prompt=system, user_text=current,
                                                                        recent_messages=history)
        self.assertEqual(result.messages[0], {"role": "system", "content": system})
        self.assertEqual(result.messages[1:-1], [{"role": m["role"], "content": m["content"]} for m in history])
        self.assertEqual(result.messages[-1], {"role": "user", "content": current})
        self.assertEqual(result.policy, "model")
        self.assertIsNone(result.output_reserve)
        self.assertIsNone(result.model_context_tokens)
        self.assertEqual(result.dropped_messages, 0)
        self.assertEqual(result.history_messages, 120)
        self.assertEqual(result.source_counts["history"], 120)
        self.assertGreater(result.estimated_input_tokens, 8192)

    def test_model_policy_preserves_all_admitted_reference_data_but_keeps_trust_filters(self):
        events = [{"id": f"event-{i}", "conversation_id": "same", "context_summary": f"EVENT-{i}:" + "画面" * 500}
                  for i in range(9)]
        memories = [{"memory": f"MEMORY-{i}:" + "限定条件" * 500, "score": 0.9} for i in range(8)]
        memories += [{"memory": "INACTIVE-MUST-NOT-SEND", "score": 1, "metadata": {"status": "superseded"}},
                     {"memory": "UNRELATED-MUST-NOT-SEND", "score": 0.1}]
        selection = {"conversation_id": "same", "required": True,
                     "context": {"text": "选中文字" * 1000, "page_text": "提供的页面正文" * 1000,
                                 "title": "完整标题", "text_truncated": False, "page_text_truncated": False}}
        summary = "完整会话概况" * 1000
        result = ContextAssembler(ContextBudget(mode="model")).assemble(system_prompt="system", user_text="current",
            recent_messages=[], summary=summary, screen_events=events, memories=memories, selection_contexts=[selection])
        serialized = json.dumps(result.messages, ensure_ascii=False)
        self.assertIn(summary, serialized)
        for event in events:
            self.assertIn(event["context_summary"], serialized)
        for memory in memories[:8]:
            self.assertIn(memory["memory"], serialized)
        self.assertNotIn("MUST-NOT-SEND", serialized)
        self.assertIn(selection["context"]["text"], serialized)
        self.assertIn(selection["context"]["page_text"], serialized)
        self.assertEqual(result.source_counts, {"system": 1, "history": 0, "summary": 1, "events": 9,
                                               "memories": 8, "selection": 1, "current": 1})
        self.assertIsNone(result.output_reserve)

    def test_known_metadata_window_is_reported_without_using_byte_estimate_as_fake_tokenizer(self):
        result = ContextAssembler(ContextBudget(mode="model", model_context_tokens=4096)).assemble(
            system_prompt="角色" * 1000, user_text="当前" * 1000,
            recent_messages=[{"role": "assistant", "content": "中文" * 1000}])
        self.assertEqual(result.model_context_tokens, 4096)
        self.assertEqual(result.dropped_messages, 0)
        self.assertGreater(result.estimated_input_tokens, 4096)

    def test_model_policy_keeps_conversation_isolation_and_role_validation(self):
        with self.assertRaisesRegex(ContextBudgetError, "不同会话"):
            ContextAssembler(ContextBudget(mode="model")).assemble(system_prompt="role", user_text="question",
                recent_messages=[{"role": "user", "content": "one", "conversation_id": "one"},
                                 {"role": "assistant", "content": "two", "conversation_id": "two"}])
        with self.assertRaises(ContextBudgetError):
            ContextAssembler(ContextBudget(mode="model")).assemble(system_prompt="role", user_text="question",
                recent_messages=[{"role": "system", "content": "untrusted"}])

    def test_manual_budget_api_remains_explicit_and_bounded(self):
        budget = replace(ContextBudget(), total_tokens=512, output_reserve=128, system_tokens=128)
        result = ContextAssembler(budget).assemble(system_prompt="role", user_text="current",
            recent_messages=[{"role": "assistant", "content": "old" * 1000}])
        self.assertEqual(result.policy, "manual")
        self.assertEqual(result.output_reserve, 128)
        self.assertEqual(result.dropped_messages, 1)
        self.assertLessEqual(result.estimated_input_tokens + result.output_reserve, 512)


class ModelMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "settings.ini"
        self.settings = SettingsManager(QSettings(str(self.path), QSettings.Format.IniFormat),
                                        CredentialStore(backend=FakeVault()))
    def tearDown(self):
        self.settings._settings.sync()
        self.directory.cleanup()

    def test_missing_or_legacy_mode_migrates_to_model_without_fetching_or_writing(self):
        self.settings._settings.setValue("context/total_tokens", 8192)
        self.settings._settings.setValue("context/max_recent_messages", 16)
        self.settings._settings.sync()
        before = self.path.read_bytes()
        with patch("desktop_pet.api_client.AsyncOpenAI") as sdk:
            result = self.settings.load_context_budget()
            sdk.assert_not_called()
        self.assertEqual(result.mode, "model")
        self.assertIsNone(result.model_context_tokens)
        self.assertEqual(before, self.path.read_bytes())

    def test_explicit_manual_budget_roundtrip_remains_compatible(self):
        original = replace(ContextBudget(), total_tokens=16384)
        self.settings.save_context_budget(original)
        self.assertEqual(self.settings.load_context_budget(), original)

    def test_exact_provider_model_metadata_is_cached_per_endpoint_and_model_without_extra_fields(self):
        api = ApiSettings("https://provider.example/v1", "custom-alias")
        self.settings.save_api_settings(api)
        self.settings.save_model_context_metadata({"id": "custom-alias", "context_length": 131072,
                                                   "secret": "DO-NOT-PERSIST", "reasoning": "DO-NOT-PERSIST"}, api)
        self.assertEqual(self.settings.load_context_budget().model_context_tokens, 131072)
        self.assertNotIn("DO-NOT-PERSIST", self.path.read_text(encoding="utf-8"))
        self.assertIsNone(self.settings.load_model_context_metadata(replace(api, model="another-alias")))
        self.assertIsNone(self.settings.load_model_context_metadata(replace(api, base_url="https://other.example/v1")))

    def test_model_name_is_not_used_to_guess_a_window_and_conflicting_metadata_is_unknown(self):
        for metadata in ({"id": "alias"}, {"id": "different", "context_length": 131072},
                         {"id": "alias", "context_length": True}, {"id": "alias", "context_length": "131072"},
                         {"id": "alias", "context_length": 8192, "limits": {"context_window_tokens": 16384}}):
            self.assertIsNone(model_context_tokens(metadata, "alias"))
            self.assertIsNone(sanitized_model_context_metadata(metadata, "alias"))
        self.assertIsNone(model_context_tokens({"id": "gpt-5"}, "gpt-5"))
        self.assertEqual(model_context_tokens({"id": "alias", "limits": {"max_context_window_tokens": 100000}}, "alias"), 100000)
        with self.assertRaises(SettingsError):
            self.settings.save_model_context_metadata({"id": "alias"}, ApiSettings(model="alias"))

    def test_context_window_machine_code_is_detected_without_echoing_provider_body(self):
        self.assertTrue(context_window_rejected(SimpleNamespace(body={"error": {
            "code": "context_length_exceeded", "message": "PRIVATE"}})))
        self.assertFalse(context_window_rejected(SimpleNamespace(body={"error": {
            "code": "authentication_error", "message": "PRIVATE"}})))


class ModelTransportTests(unittest.TestCase):
    def test_automatic_context_omits_output_cap_and_preserves_long_reply_in_single_request(self):
        with MockApiServer() as server:
            reply = "完整模型回答" * 5000
            server.enqueue(json.dumps({"reply": reply, "emotion": "neutral"}, ensure_ascii=False))
            result = ApiClient().complete_context(ApiSettings(server.base_url, "unknown-alias"), "fake-key",
                [{"role": "system", "content": "role"}, {"role": "user", "content": "question"}])
            self.assertEqual(result.reply, reply)
            self.assertEqual(len(server.requests), 1)
            body = server.requests[0]["body"]
            self.assertNotIn("max_tokens", body)
            self.assertNotIn("max_completion_tokens", body)

    def test_context_window_failure_is_explicit_static_and_never_retried_with_smaller_context(self):
        with MockApiServer() as server:
            server.enqueue("Maximum context length exceeded fake-key PRIVATE-REMOTE-BODY", status=400)
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(ApiSettings(server.base_url, "alias"), "fake-key",
                    [{"role": "user", "content": "complete question"}])
            self.assertEqual(caught.exception.code, "context_length_exceeded")
            self.assertIn("未被自动截断", str(caught.exception))
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertNotIn("fake-key", str(caught.exception))
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(server.requests[0]["body"]["messages"][-1]["content"], "complete question")


if __name__ == "__main__":
    unittest.main()
