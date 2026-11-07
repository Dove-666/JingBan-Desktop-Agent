"""Real local SDK requests, model metadata, scoped INI and native controls."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiClient, ApiError
from desktop_pet.settings_manager import ApiSettings, SettingsError
from desktop_pet.ui.reasoning_controls import ReasoningControls
from tests.mock_api import MockApiServer
from tests.test_application import MemoryCredentialBackend, isolated_settings
from tests.test_model_list_api import ModelApiServer

KEY = "only-local-adaptive-reasoning-credential"


class AdaptiveReasoningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reasoning-profile-")
        self.path = Path(self.temp.name) / "settings.ini"
        self.vault = MemoryCredentialBackend()
        self.settings = isolated_settings(self.path, self.vault)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.hide()
            widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def controls(self, config):
        widget = ReasoningControls(self.settings, config)
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        widget.show()
        self.widgets.append(widget)
        return widget

    def test_known_models_get_their_own_options_and_switch_resets_invalid_value(self):
        widget = self.controls(ApiSettings(model="gpt-5.4"))
        values = lambda: [widget.value_combo.itemData(i) for i in range(widget.value_combo.count())]
        self.assertEqual(values(), ["default", "none", "low", "medium", "high", "xhigh"])
        widget.value_combo.setCurrentIndex(widget.value_combo.findData("xhigh"))
        widget.set_identity("https://api.deepseek.com", "deepseek-flash")
        self.assertEqual(values(), ["default", "none", "low", "high", "max"])
        self.assertEqual(widget.value_combo.currentData(), "max")
        widget.set_identity("https://api.openai.com/v1", "gpt-5.4")
        self.assertEqual(widget.value_combo.currentData(), "xhigh")

    def test_unknown_alias_only_default_until_explicit_protocol_and_custom_token(self):
        widget = self.controls(ApiSettings("https://gateway.example/v1", "my-private-alias"))
        self.assertEqual(widget.value_combo.count(), 1)
        widget.profile_combo.setCurrentIndex(widget.profile_combo.findData("custom"))
        widget.value_combo.setEditText("Ultra-Deep_2")
        config = widget.api_settings("https://gateway.example/v1", "my-private-alias", 30)
        config.validate()
        self.assertEqual(config.reasoning_effort, "Ultra-Deep_2")
        self.assertEqual(config.reasoning_profile, "custom")

    def test_provider_and_model_choices_persist_independently(self):
        one = ApiSettings("https://gateway.example/v1", "alias-one", 30, "ultra", "custom")
        two = ApiSettings("https://gateway.example/v1", "alias-two", 30, "enabled", "thinking")
        self.settings.save_api_settings(one)
        self.settings.save_api_settings(two)
        reopened = isolated_settings(self.path, self.vault)
        self.assertEqual(reopened.load_reasoning_preferences(one.base_url, one.model),
                         {"profile": "custom", "value": "ultra", "budget_tokens": 0})
        self.assertEqual(reopened.load_reasoning_preferences(two.base_url, two.model)["value"], "enabled")
        self.assertIsNone(reopened.load_reasoning_preferences("https://other.example/v1", one.model))

    def test_old_saved_effort_migrates_without_changing_its_wire_semantics(self):
        raw = QSettings(str(self.path), QSettings.Format.IniFormat)
        raw.setValue("api/base_url", "https://legacy.example/v1")
        raw.setValue("api/model", "old-model")
        raw.setValue("api/reasoning_effort", "max")
        raw.sync()
        config = self.settings.load_api_settings()
        self.assertEqual((config.reasoning_profile, config.reasoning_effort), ("legacy", "max"))
        raw.setValue("api/reasoning_profile", "damaged-profile")
        raw.sync()
        config = self.settings.load_api_settings()
        self.assertEqual((config.base_url, config.model), ("https://legacy.example/v1", "old-model"))
        self.assertEqual((config.reasoning_profile, config.reasoning_effort), ("auto", "default"))

    def test_budget_survives_collapsed_controls_and_settings_reopen(self):
        widget = self.controls(ApiSettings("https://gateway.example/v1", "qwen-alias", 30,
                                          "enabled", "qwen_budget", 8192))
        widget.profile_combo.setCurrentIndex(widget.profile_combo.findData("custom"))
        widget.profile_combo.setCurrentIndex(widget.profile_combo.findData("qwen_budget"))
        self.assertEqual(widget.budget_spin.value(), 8192)
        widget.hide()
        config = widget.api_settings("https://gateway.example/v1", "qwen-alias", 30)
        self.assertEqual(config.reasoning_budget_tokens, 8192)
        self.settings.save_api_settings(config)
        self.assertEqual(isolated_settings(self.path, self.vault).load_api_settings(), config)

    def test_model_discovery_retains_only_explicit_bounded_reasoning_metadata(self):
        with ModelApiServer() as server:
            server.enqueue({"data": [{"id": "alias", "reasoning": {"profile": "openai", "values": ["low", "Ultra"]},
                                      "instructions": "PRIVATE-UNTRUSTED-INSTRUCTIONS"}]})
            models = ApiClient().list_models(ApiSettings(server.base_url, ""), KEY)
            self.assertEqual(models, ["alias"])
            metadata = models.reasoning_metadata["alias"]
            self.assertEqual(set(metadata), {"version", "base_url", "model", "profile", "values"})
            self.settings.save_reasoning_metadata(metadata, base_url=server.base_url, model="alias")
            config = ApiSettings(server.base_url, "alias", 30, "Ultra", "auto", 0, metadata)
            self.settings.save_api_settings(config)
            self.assertEqual(isolated_settings(self.path, self.vault).load_api_settings(), config)
            text = self.path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE-UNTRUSTED-INSTRUCTIONS", text)
            self.assertNotIn(KEY, text)
            self.assertIsNone(self.settings.load_reasoning_metadata(server.base_url, "other-alias"))

    def test_streaming_and_buffered_sdk_send_actual_profile_fields_once(self):
        cases = [
            ("deepseek", "none", 0, {"thinking": {"type": "disabled"}}),
            ("deepseek", "max", 0, {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}),
            ("thinking", "enabled", 0, {"thinking": {"type": "enabled"}}),
            ("qwen_budget", "enabled", 8192, {"enable_thinking": True, "thinking_budget": 8192}),
            ("openrouter", "high", 0, {"reasoning": {"effort": "high"}}),
            ("custom", "Ultra-Deep_2", 0, {"reasoning_effort": "Ultra-Deep_2"}),
        ]
        with MockApiServer() as server:
            for stream in (False, True):
                for profile, value, budget, expected in cases:
                    with self.subTest(profile=profile, stream=stream):
                        before = len(server.requests)
                        server.enqueue("适配后的回复")
                        config = ApiSettings(server.base_url, "alias", 5, value, profile, budget)
                        deltas = []
                        ApiClient().complete_context(config, KEY, [{"role": "user", "content": "本地验证"}],
                            **({"on_delta": deltas.append} if stream else {}))
                        self.assertEqual(len(server.requests), before + 1)
                        body = server.requests[-1]["body"]
                        for key, setting in expected.items():
                            self.assertEqual(body[key], setting)
                        self.assertNotIn("extra_body", body)
                        if "reasoning_effort" not in expected:
                            self.assertNotIn("reasoning_effort", body)

    def test_provider_reasoning_error_is_static_and_never_retries(self):
        with MockApiServer() as server:
            server.enqueue("Unsupported thinking_budget PRIVATE " + KEY, status=400)
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete(ApiSettings(server.base_url, "alias", 5, "enabled", "qwen_budget", 8192),
                                     KEY, [{"role": "user", "content": "本地错误"}])
            self.assertEqual(caught.exception.code, "reasoning_unsupported")
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(len(server.requests), 1)

    def test_unsupported_known_level_rejected_before_any_request(self):
        with self.assertRaises(SettingsError):
            ApiSettings(model="gpt-5.4", reasoning_effort="max").validate()

    def test_legacy_editor_shows_model_options_without_rewriting_original_choice(self):
        raw = QSettings(str(self.path), QSettings.Format.IniFormat)
        raw.setValue("api/base_url", "https://api.deepseek.com/v1")
        raw.setValue("api/model", "deepseek-flash")
        raw.setValue("api/reasoning_effort", "max")
        raw.sync()
        config = self.settings.load_api_settings()
        widget = self.controls(config)
        self.assertEqual(widget.profile_combo.currentData(), "auto")
        self.assertEqual([widget.value_combo.itemData(i) for i in range(widget.value_combo.count())],
                         ["default", "none", "low", "high", "max"])
        self.assertEqual(self.settings.load_api_settings().reasoning_profile, "legacy")
        self.assertEqual(widget.api_settings(config.base_url, config.model, 30).reasoning_effort, "max")

    def test_budget_output_conflict_does_not_send_a_billable_request(self):
        with MockApiServer() as server:
            config = ApiSettings(server.base_url, "alias", 5, "enabled", "claude_budget", 2048)
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(config, KEY, [{"role": "user", "content": "本地预算校验"}], output_tokens=128)
            self.assertEqual(caught.exception.code, "reasoning_unsupported")
            self.assertEqual(server.requests, [])

    def test_unsaved_custom_reasoning_cannot_echo_a_credential_into_ui_or_request(self):
        with MockApiServer() as server:
            config = ApiSettings(server.base_url, "alias", 5, KEY, "custom")
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete(config, KEY, [{"role": "user", "content": "本地密钥边界"}])
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(server.requests, [])


if __name__ == "__main__":
    unittest.main()
