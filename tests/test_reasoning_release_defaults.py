"""Fresh highest-effort defaults, explicit choices, and actual local request bytes."""
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiClient
from desktop_pet.reasoning import default_reasoning_selection, reasoning_body
from desktop_pet.settings_manager import ApiSettings
from desktop_pet.ui.reasoning_controls import ReasoningControls
from tests.mock_api import MockApiServer
from tests.test_application import MemoryCredentialBackend, isolated_settings


class ReasoningReleaseDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="reasoning-release-defaults-")
        self.path = Path(self.directory.name) / "settings.ini"
        self.settings = isolated_settings(self.path, MemoryCredentialBackend())
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.hide()
            widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def unset_model(self, base, model):
        raw = QSettings(str(self.path), QSettings.Format.IniFormat)
        raw.setValue("api/base_url", base)
        raw.setValue("api/model", model)
        raw.sync()

    def controls(self):
        widget = ReasoningControls(self.settings, self.settings.load_api_settings())
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        widget.show()
        self.widgets.append(widget)
        return widget

    def test_fresh_known_models_use_their_own_highest_tier_without_writes(self):
        for model, expected in (("deepseek-flash", "max"), ("gpt-5.4", "xhigh"), ("gpt-5", "high")):
            self.unset_model("https://fixture.example/v1", model)
            before = self.path.read_bytes()
            loaded = self.settings.load_api_settings()
            self.assertEqual(loaded.reasoning_effort, expected)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse(self.settings.has_reasoning_preferences(loaded.base_url, model))
            self.assertEqual(self.controls().value_combo.currentData(), expected)

    def test_unsorted_bound_metadata_uses_highest_supported_not_last_or_global_max(self):
        base = "https://fixture.example/v1"
        self.unset_model(base, "private-alias")
        metadata = {"version": 1, "base_url": base, "model": "private-alias",
                    "profile": "openai", "values": ["high", "none", "low"]}
        self.settings.save_reasoning_metadata(metadata, base_url=base, model="private-alias")
        self.assertEqual(self.settings.load_api_settings().reasoning_effort, "high")
        self.assertEqual(self.controls().value_combo.currentData(), "high")
        self.assertEqual(default_reasoning_selection(base, "different-alias", metadata=metadata)["value"], "default")

    def test_unknown_and_unrankable_tokens_do_not_guess_a_protocol_or_tier(self):
        base = "https://fixture.example/v1"
        self.unset_model(base, "private-alias")
        config = self.settings.load_api_settings()
        self.assertEqual(config.reasoning_effort, "default")
        self.assertEqual(reasoning_body(base, config.model, config.reasoning_effort), {})
        metadata = {"version": 1, "base_url": base, "model": "private-alias", "profile": "openai", "values": ["Turbo-Private"]}
        self.assertEqual(default_reasoning_selection(base, "private-alias", metadata=metadata)["value"], "default")

    def test_saved_default_off_and_low_survive_reopen_and_metadata_refresh(self):
        base = "https://fixture.example/v1"
        for value in ("default", "none", "low"):
            self.settings.save_api_settings(ApiSettings(base, "deepseek-flash", 30, value))
            self.assertTrue(self.settings.has_reasoning_preferences(base, "deepseek-flash"))
            self.assertEqual(self.settings.load_api_settings().reasoning_effort, value)
            widget = self.controls()
            widget.set_identity(base, "deepseek-flash", refresh=True)
            self.assertEqual(widget.value_combo.currentData(), value)

    def test_auto_draft_recomputes_after_discovery_but_user_selection_is_kept(self):
        base = "https://fixture.example/v1"
        self.unset_model(base, "deepseek-flash")
        widget = self.controls()
        self.assertEqual(widget.value_combo.currentData(), "max")
        metadata = {"version": 1, "base_url": base, "model": "deepseek-flash", "profile": "deepseek", "values": ["none", "low"]}
        self.settings.save_reasoning_metadata(metadata, base_url=base, model="deepseek-flash")
        widget.set_identity(base, "deepseek-flash", refresh=True)
        self.assertEqual(widget.value_combo.currentData(), "low")
        widget.value_combo.setCurrentIndex(widget.value_combo.findData("none"))
        metadata["values"] = ["none", "low", "max"]
        self.settings.save_reasoning_metadata(metadata, base_url=base, model="deepseek-flash")
        widget.set_identity(base, "deepseek-flash", refresh=True)
        self.assertEqual(widget.value_combo.currentData(), "none")

    def test_new_model_maximum_restores_independent_saved_choice(self):
        base = "https://fixture.example/v1"
        self.settings.save_api_settings(ApiSettings(base, "deepseek-flash", 30, "none"))
        widget = self.controls()
        widget.set_identity(base, "gpt-5.4")
        self.assertEqual(widget.value_combo.currentData(), "xhigh")
        widget.set_identity(base, "deepseek-flash")
        self.assertEqual(widget.value_combo.currentData(), "none")

    def test_toggle_uses_on_while_token_budget_retains_declared_context_aware_default(self):
        self.assertEqual(default_reasoning_selection("https://fixture.example/v1", "alias", "thinking"),
                         {"profile": "thinking", "value": "enabled", "budget_tokens": 0})
        self.assertEqual(default_reasoning_selection("https://fixture.example/v1", "alias", "claude_budget"),
                         {"profile": "claude_budget", "value": "enabled", "budget_tokens": 4096})

    def test_fresh_loaded_default_reaches_actual_connection_request_once(self):
        with MockApiServer() as server:
            self.unset_model(server.base_url, "deepseek-flash")
            server.enqueue("连接成功")
            ApiClient().test_connection(self.settings.load_api_settings(), "SYNTHETIC_LOCAL_ONLY_KEY")
            self.assertEqual(len(server.requests), 1)
            body = server.requests[0]["body"]
            self.assertEqual(body["reasoning_effort"], "max")
            self.assertEqual(body["thinking"], {"type": "enabled"})


if __name__ == "__main__":
    unittest.main()
