"""Real SDK/loopback requests and isolated Qt settings for model reasoning effort."""

from dataclasses import replace
from pathlib import Path
import tempfile
import traceback
import unittest

from PySide6.QtCore import QEvent, QSettings, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiClient, ApiError
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.settings_manager import ApiSettings, REASONING_EFFORTS, SettingsError
from desktop_pet.ui.settings_page import SettingsPage
from tests.mock_api import MockApiServer
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until


KEY = "synthetic-reasoning-local-only-token"


class ReasoningEffortSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-reasoning-settings-")
        self.path = Path(self.directory.name) / "settings.ini"
        self.vault = MemoryCredentialBackend()
        self.settings = isolated_settings(self.path, self.vault)

    def tearDown(self):
        self.directory.cleanup()

    def test_legacy_positional_settings_default_to_provider_behavior(self):
        config = ApiSettings("https://example.invalid/v1", "existing-model", 17)
        self.assertEqual(config.reasoning_effort, "default")
        config.validate()
        self.assertEqual(replace(config, model="changed-model").reasoning_effort, "default")

    def test_every_supported_setting_round_trips_without_saving_credentials_in_ini(self):
        self.settings.save_api_key(KEY)
        for effort in REASONING_EFFORTS:
            with self.subTest(effort=effort):
                config = ApiSettings("https://example.invalid/custom/v1", "reasoning-model", 23, effort, "legacy")
                self.settings.save_api_settings(config)
                reopened = isolated_settings(self.path, self.vault)
                self.assertEqual(reopened.load_api_settings(), config)
                self.assertEqual(reopened.get_api_key(), KEY)
        self.assertNotIn(KEY, self.path.read_text(encoding="utf-8"))

    def test_invalid_effort_is_rejected_before_writes_without_echoing_the_value(self):
        original = ApiSettings("https://example.invalid/v1", "kept-model", 21, "low", "legacy")
        self.settings.save_api_settings(original)
        for invalid in (None, True, 1, [], "HIGH", "auto", " high ", KEY):
            with self.subTest(case=type(invalid).__name__):
                with self.assertRaises(SettingsError) as caught:
                    self.settings.save_api_settings(replace(original, reasoning_effort=invalid))
                self.assertNotIn(KEY, str(caught.exception))
                self.assertEqual(self.settings.load_api_settings(), original)

    def test_missing_or_corrupt_new_field_preserves_existing_api_configuration(self):
        original = ApiSettings("https://example.invalid/old/v1", "existing-model", 44)
        self.settings.save_api_settings(original)
        raw = QSettings(str(self.path), QSettings.Format.IniFormat)
        raw.remove("api/reasoning_effort")
        raw.sync()
        self.assertEqual(isolated_settings(self.path, self.vault).load_api_settings(), original)
        for invalid in ("corrupt-value", True, 42, KEY):
            with self.subTest(case=type(invalid).__name__):
                raw.setValue("api/reasoning_effort", invalid)
                raw.sync()
                self.assertEqual(isolated_settings(self.path, self.vault).load_api_settings(), original)


class ReasoningEffortTransportTests(unittest.TestCase):
    def test_default_omits_reasoning_parameter_in_actual_sdk_http_payload(self):
        with MockApiServer() as server:
            server.enqueue("默认模型行为")
            reply = ApiClient().complete_context(ApiSettings(server.base_url, "gpt-5.4"), KEY,
                [{"role": "user", "content": "测试默认思考强度"}], output_tokens=128)
            self.assertEqual(reply.reply, "默认模型行为")
            self.assertEqual(len(server.requests), 1)
            self.assertNotIn("reasoning_effort", server.requests[0]["body"])
            self.assertEqual(server.requests[0]["body"]["max_completion_tokens"], 128)

    def test_each_explicit_level_reaches_the_sdk_without_changing_output_budget(self):
        with MockApiServer() as server:
            for index, effort in enumerate(REASONING_EFFORTS[1:], start=1):
                with self.subTest(effort=effort):
                    server.enqueue("已收到思考强度")
                    config = ApiSettings(server.base_url, "gpt-5.4", 5, effort, "legacy")
                    ApiClient().complete_context(config, KEY, [{"role": "user", "content": "测试参数"}],
                                                 output_tokens=192)
                    self.assertEqual(len(server.requests), index)
                    body = server.requests[-1]["body"]
                    self.assertEqual(body["reasoning_effort"], effort)
                    self.assertEqual(body["max_completion_tokens"], 192)
                    self.assertNotIn("max_tokens", body)
                    self.assertNotIn("extra_body", body)

    def test_connection_test_uses_the_selected_effort_in_a_real_request(self):
        with MockApiServer() as server:
            server.enqueue("连接成功")
            status = ApiClient().test_connection(ApiSettings(server.base_url, "compatible-model", 5, "medium", "legacy"), KEY)
            self.assertIn("连接成功", status)
            self.assertEqual(server.requests[0]["body"]["reasoning_effort"], "medium")
            self.assertEqual(len(server.requests), 1)

    def test_rejected_effort_gives_static_default_guidance_without_retry_or_body_leak(self):
        with MockApiServer() as server:
            for index, status in enumerate((400, 422, 501), start=1):
                with self.subTest(status=status):
                    server.enqueue("SENSITIVE-PROVIDER-BODY " + KEY +
                                   " Unsupported parameter: reasoning_effort", status=status)
                    with self.assertRaises(ApiError) as caught:
                        ApiClient().complete(ApiSettings(server.base_url, "compatible-model", 5, "xhigh", "legacy"), KEY,
                                             [{"role": "user", "content": "一次请求"}])
                    self.assertEqual(caught.exception.code, "reasoning_unsupported")
                    self.assertIn("默认", str(caught.exception))
                    formatted = "".join(traceback.format_exception(type(caught.exception), caught.exception,
                                                                   caught.exception.__traceback__))
                    self.assertNotIn(KEY, formatted)
                    self.assertNotIn("SENSITIVE-PROVIDER-BODY", formatted)
                    self.assertEqual(len(server.requests), index)
                    self.assertEqual(server.requests[-1]["body"]["reasoning_effort"], "xhigh")

    def test_other_provider_errors_are_not_misreported_as_reasoning_errors(self):
        with MockApiServer() as server:
            for index, (status, message, expected) in enumerate((
                    (400, "Unsupported parameter: temperature", "provider"),
                    (401, "reasoning_effort " + KEY, "authentication"),
                    (404, "reasoning_effort model was not found", "model_not_found")), start=1):
                with self.subTest(status=status):
                    server.enqueue(message, status=status)
                    with self.assertRaises(ApiError) as caught:
                        ApiClient().complete(ApiSettings(server.base_url, "compatible-model", 5, "high", "legacy"), KEY,
                                             [{"role": "user", "content": "测试错误分类"}])
                    self.assertEqual(caught.exception.code, expected)
                    self.assertNotIn(KEY, str(caught.exception))
                    self.assertEqual(len(server.requests), index)

    def test_image_request_reports_rejected_effort_before_generic_vision_failure(self):
        with MockApiServer() as server:
            server.enqueue("Unsupported value for reasoning_effort " + KEY, status=400)
            messages = [{"role": "user", "content": [
                {"type": "text", "text": "合成图片问题"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ]}]
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(ApiSettings(server.base_url, "vision-model", 5, "max", "legacy"), KEY, messages)
            self.assertEqual(caught.exception.code, "reasoning_unsupported")
            self.assertEqual(len(server.requests), 1)


class ReasoningEffortUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-reasoning-ui-")
        self.path = Path(self.directory.name)
        self.vault = MemoryCredentialBackend()
        self.settings = isolated_settings(self.path / "settings.ini", self.vault)
        self.server = MockApiServer()
        self.server.__enter__()
        self.settings.save_api_settings(ApiSettings(self.server.base_url, "gpt-5.4", 5))
        self.settings.save_api_key(KEY)
        self.chat = ChatController(self.settings, store=ConversationStore(self.path / "history.sqlite3"))
        self.panels = []
        self.page = self.make_page()
        spin_until(lambda: not self.chat._runner._tasks)

    def make_page(self):
        panel = SettingsPage(None, self.settings, self.chat)
        panel.show_section("api")
        panel.resize(900, 700)
        panel.show()
        self.panels.append(panel)
        self.app.processEvents()
        return panel

    def tearDown(self):
        self.chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.server.__exit__(None, None, None)
        self.directory.cleanup()

    def select_effort(self, effort):
        self.page.reasoning_effort_combo.setCurrentIndex(self.page.reasoning_effort_combo.findData(effort))

    def test_dropdown_exposes_localized_options_and_save_restores_selected_setting(self):
        combo = self.page.reasoning_effort_combo
        self.assertEqual([combo.itemData(index) for index in range(combo.count())], ["default", "none", "low", "medium", "high", "xhigh"])
        self.assertEqual([combo.itemText(index) for index in range(combo.count())],
                         ["默认（由模型决定）", "关闭思考", "低", "中", "高", "很高"])
        self.assertEqual(combo.accessibleName(), "模型思考强度")
        self.select_effort("high")
        self.page.save_api_button.click()
        self.assertTrue(self.page.api_error_label.isHidden())
        self.assertEqual(isolated_settings(self.path / "settings.ini", self.vault).load_api_settings().reasoning_effort, "high")
        reopened = self.make_page()
        self.assertEqual(reopened.reasoning_effort_combo.currentData(), "high")
        self.assertNotIn(KEY, (self.path / "settings.ini").read_text(encoding="utf-8"))
        self.server.enqueue("使用已保存强度进行聊天")
        self.assertTrue(self.chat.send("验证保存后的参数"))
        spin_until(lambda: not self.chat.busy)
        self.assertEqual(self.server.requests[0]["body"]["reasoning_effort"], "high")

    def test_unsaved_dropdown_is_used_for_connection_test_without_changing_saved_config(self):
        self.select_effort("medium")
        self.server.enqueue("连接成功")
        self.page.test_connection_button.click()
        spin_until(lambda: not self.chat.busy)
        self.assertEqual(self.server.requests[0]["body"]["reasoning_effort"], "medium")
        self.assertEqual(self.settings.load_api_settings().reasoning_effort, "default")
        self.assertIn("连接成功", self.page.connection_label.text())

    def test_changing_effort_while_test_runs_marks_its_late_result_as_old_configuration(self):
        self.select_effort("low")
        self.server.enqueue("连接成功", delay=.15)
        self.page.test_connection_button.click()
        spin_until(self.server.received.is_set)
        self.select_effort("high")
        spin_until(lambda: not self.chat.busy)
        self.assertEqual(self.server.requests[0]["body"]["reasoning_effort"], "low")
        self.assertIn("此结果对应测试发起时", self.page.connection_label.text())

    def test_unsupported_effort_is_visible_and_returning_to_default_makes_one_new_request(self):
        self.select_effort("xhigh")
        self.server.enqueue("Unsupported reasoning_effort " + KEY, status=400)
        self.page.test_connection_button.click()
        spin_until(lambda: not self.chat.busy)
        self.assertFalse(self.page.api_error_label.isHidden())
        self.assertIn("默认", self.page.api_error_label.text())
        self.assertNotIn(KEY, self.page.api_error_label.text() + self.page.connection_label.text())
        self.assertEqual(len(self.server.requests), 1)
        self.select_effort("default")
        self.server.enqueue("连接成功")
        self.page.test_connection_button.click()
        spin_until(lambda: not self.chat.busy)
        self.assertEqual(len(self.server.requests), 2)
        self.assertNotIn("reasoning_effort", self.server.requests[-1]["body"])
        self.assertIn("连接成功", self.page.connection_label.text())

    def test_chat_rejected_effort_offers_settings_and_retains_the_user_message(self):
        self.select_effort("high")
        self.page.save_api_button.click()
        self.server.enqueue("Unsupported reasoning_effort " + KEY, status=400)
        self.assertTrue(self.chat.send("这条问题失败后也要保留"))
        spin_until(lambda: not self.chat.busy)
        self.assertEqual(self.chat.error_action, "settings")
        self.assertIn("默认", self.chat.error)
        self.assertNotIn(KEY, self.chat.error)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.chat.messages[0]["content"], "这条问题失败后也要保留")
        self.assertEqual(self.chat.messages[0]["metadata"]["request_status"], "failed")


if __name__ == "__main__":
    unittest.main()
