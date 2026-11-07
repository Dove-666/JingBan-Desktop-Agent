"""Real Qt save/clear buttons invalidate queue policy synchronously.

Reuse owned INI/SQLite, memory-only credential, artwork, startup and camera
fixtures. No production keys, registry, model requests or system mouse input.
"""
import hashlib
import threading
from unittest.mock import patch
import unittest

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop_pet.settings_manager import CredentialsError, SettingsError
from desktop_pet.agents.coordinator import settings_identity
from tests import test_settings_navigation as navigation_fixture
from tests.test_startup_settings_ui import FakeStartupManager


class AgentQueueSettingsRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.startup = FakeStartupManager()
        with patch("desktop_pet.application_controller.StartupManager", return_value=self.startup):
            navigation_fixture.EmbeddedSettingsNavigationTests.setUp(self)
        self.session.startup_manager = self.startup
        self.session.open_settings("api")
        self.page = self.session.chat_window.settings_page
        self.ui_thread = threading.get_ident()

    tearDown = navigation_fixture.EmbeddedSettingsNavigationTests.tearDown

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def key_digest(self):
        try: return self.digest(self.settings.get_api_key())
        except CredentialsError: return "unavailable"

    def observer(self, events, *, real=None):
        def refreshed():
            event = {"kind": "refresh", "thread": threading.get_ident(), "qt_thread": QThread.currentThread(),
                     "api": self.settings.load_api_settings(), "key_digest": self.key_digest()}
            if real is not None: event["result"] = real()
            events.append(event)
            return event.get("result")
        return refreshed

    @staticmethod
    def click(button, events):
        # _save_api/_clear_key were connected first when the actual QWidget
        # was built. This later direct signal observer runs after that slot
        # returns, proving refresh did not wait for a queued Qt callback.
        after = lambda: events.append({"kind": "button_slot_returned"})
        button.clicked.connect(after)
        try: button.click()
        finally: button.clicked.disconnect(after)

    def assert_synchronous(self, events):
        self.assertEqual(["refresh", "button_slot_returned"], [event["kind"] for event in events])
        self.assertEqual(self.ui_thread, events[0]["thread"])
        self.assertEqual(self.app.thread(), events[0]["qt_thread"])

    def fill(self, *, model="synthetic-replacement-model", key="SYNTHETIC_QUEUE_REPLACEMENT_KEY"):
        self.page.base_url_input.setText("https://queue-refresh.invalid/custom/v1")
        self.page.model_input.setText(model)
        self.page.key_input.setText(key)

    def scope_digest(self):
        policy = getattr(self.session.chat, "_agent_queue_policy", None)
        if policy is None: return None
        return settings_identity(policy, peer="a" * 64, authorization="b" * 64,
            generation="00000000-0000-0000-0000-000000000001")

    def assert_shared_policy(self, *, key=None):
        policy = self.session.chat._agent_queue_policy
        self.assertIsNotNone(policy)
        api = self.settings.load_api_settings()
        selected_key = self.settings.get_api_key() if key is None else key
        self.assertEqual(policy["api"], api)
        self.assertEqual(policy["key"], selected_key)
        self.assertEqual(policy["gui_key"], selected_key)
        self.assertEqual(policy["agent"].gui_base_url, api.base_url)
        self.assertEqual(policy["agent"].gui_model, api.model)
        self.assertEqual((policy["agent"].gui_provider, policy["agent"].gui_coordinate_profile),
                         ("ui-tars-1.5", "pixel"))

    def test_route_and_key_are_committed_before_same_thread_refresh_and_save_button_return(self):
        events = []; self.fill()
        real = getattr(self.session.chat, "refresh_agent_queue_policy", None)
        with patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events, real=real), create=True):
            self.click(self.page.save_api_button, events)
        self.assert_synchronous(events)
        self.assertEqual("https://queue-refresh.invalid/custom/v1", events[0]["api"].base_url)
        self.assertEqual("synthetic-replacement-model", events[0]["api"].model)
        self.assertEqual(self.digest("SYNTHETIC_QUEUE_REPLACEMENT_KEY"), events[0]["key_digest"])
        self.assertEqual("", self.page.key_input.text())
        self.assertFalse(self.page.show_key_button.isChecked())
        self.assertNotIn("SYNTHETIC_QUEUE_REPLACEMENT_KEY", (self.base / "settings.ini").read_text(encoding="utf-8"))
        self.assert_shared_policy(key="SYNTHETIC_QUEUE_REPLACEMENT_KEY")

    def test_blank_key_preserves_saved_key_while_changed_route_still_refreshes(self):
        before = self.key_digest(); events = []
        self.fill(key="")
        with patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events), create=True):
            self.click(self.page.save_api_button, events)
        self.assert_synchronous(events)
        self.assertEqual(before, events[0]["key_digest"])
        self.assertEqual("synthetic-replacement-model", events[0]["api"].model)

    def test_ordinary_save_failure_before_mutation_never_publishes_new_policy_or_key(self):
        before, key_before = self.settings.load_api_settings(), self.key_digest()
        real = self.session.chat.refresh_agent_queue_policy
        real(); scope_before = self.scope_digest()
        self.assertIsNotNone(scope_before)
        self.fill(); events = []
        with patch.object(self.settings, "save_api_settings", side_effect=SettingsError("Synthetic write rejected")), \
             patch.object(self.settings, "save_api_key") as key_save, \
             patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events, real=real)):
            self.click(self.page.save_api_button, events)
        key_save.assert_not_called()
        self.assert_synchronous(events)
        self.assertEqual(before, self.settings.load_api_settings())
        self.assertEqual(key_before, self.key_digest())
        self.assertEqual(scope_before, self.scope_digest())
        self.assertIn("Synthetic write rejected", self.page.api_error_label.text())

    def test_partial_qsettings_sync_failure_refreshes_actual_changed_route_before_return(self):
        real = self.session.chat.refresh_agent_queue_policy
        real(); scope_before, key_before = self.scope_digest(), self.key_digest()
        self.assertIsNotNone(scope_before)
        self.fill(); events = []
        with patch.object(self.settings, "_sync", side_effect=SettingsError("Synthetic partial sync failure")), \
             patch.object(self.settings, "save_api_key") as key_save, \
             patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events, real=real)):
            self.click(self.page.save_api_button, events)
        self.assert_synchronous(events)
        key_save.assert_not_called()
        self.assertEqual("synthetic-replacement-model", events[0]["api"].model)
        self.assertEqual("https://queue-refresh.invalid/custom/v1", events[0]["api"].base_url)
        self.assertEqual(key_before, events[0]["key_digest"])
        self.assertNotEqual(scope_before, self.scope_digest())
        self.assertIn("Synthetic partial sync failure", self.page.api_error_label.text())

    def test_unavailable_credential_read_during_refresh_revokes_old_policy_without_throwing(self):
        real = self.session.chat.refresh_agent_queue_policy
        real(); self.assertIsNotNone(self.scope_digest())
        self.fill(key=""); events = []
        with patch.object(self.settings, "get_api_key", side_effect=CredentialsError("Synthetic vault unavailable")), \
             patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events, real=real)):
            self.click(self.page.save_api_button, events)
        self.assert_synchronous(events)
        self.assertEqual("unavailable", events[0]["key_digest"])
        self.assertIs(False, events[0]["result"])
        self.assertIsNone(self.scope_digest())

    def test_key_save_failure_after_ordinary_commit_refreshes_even_when_vault_effect_is_uncertain(self):
        original = self.settings.save_api_key
        for actually_saved in (False, True):
            with self.subTest(actually_saved=actually_saved):
                before = self.key_digest(); events = []
                new_key = "SYNTHETIC_UNCERTAIN_QUEUE_KEY_" + str(actually_saved)
                self.fill(model="synthetic-key-failure-" + str(actually_saved), key=new_key)
                def uncertain(key):
                    if actually_saved: original(key)
                    raise CredentialsError("Synthetic credential confirmation failed")
                with patch.object(self.settings, "save_api_key", side_effect=uncertain), \
                     patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events), create=True):
                    self.click(self.page.save_api_button, events)
                self.assert_synchronous(events)
                self.assertEqual("synthetic-key-failure-" + str(actually_saved), events[0]["api"].model)
                self.assertEqual(self.digest(new_key) if actually_saved else before, events[0]["key_digest"])
                self.assertIn("普通配置已保存，密钥未保存", self.page.api_error_label.text())

    def test_confirmed_clear_refreshes_after_deletion_and_before_button_return(self):
        events = []; self.page.key_input.setText("SYNTHETIC_DRAFT_TO_CLEAR")
        real = getattr(self.session.chat, "refresh_agent_queue_policy", None)
        if real is not None:
            real()
            self.assert_shared_policy()
        with patch("desktop_pet.ui.settings_controls.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
             patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events, real=real), create=True):
            self.click(self.page.clear_key_button, events)
        self.assert_synchronous(events)
        self.assertEqual(self.digest(""), events[0]["key_digest"])
        self.assertEqual("", self.page.key_input.text())
        if real is not None:
            self.assertIs(False, events[0]["result"])
            self.assertIsNone(self.session.chat._agent_queue_policy)

    def test_failed_clear_still_refreshes_whether_or_not_credential_was_already_deleted(self):
        original_clear = self.settings.clear_api_key
        for actually_deleted in (False, True):
            with self.subTest(actually_deleted=actually_deleted):
                self.settings.save_api_key("SYNTHETIC_CLEAR_FAILURE_SAVED_KEY")
                before = self.key_digest(); events = []
                def uncertain():
                    if actually_deleted: original_clear()
                    raise CredentialsError("Synthetic deletion confirmation failed")
                with patch("desktop_pet.ui.settings_controls.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                     patch.object(self.settings, "clear_api_key", side_effect=uncertain), \
                     patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events), create=True):
                    self.click(self.page.clear_key_button, events)
                self.assert_synchronous(events)
                self.assertEqual(self.digest("") if actually_deleted else before, events[0]["key_digest"])
                self.assertIn("无法确认 API Key 已删除", self.page.connection_label.text())

    def test_cancel_clear_keeps_credential_and_does_not_invalidate_or_publish(self):
        before = self.key_digest(); events = []
        with patch("desktop_pet.ui.settings_controls.QMessageBox.question", return_value=QMessageBox.StandardButton.No), \
             patch.object(self.session.chat, "refresh_agent_queue_policy", create=True) as refreshed:
            self.click(self.page.clear_key_button, events)
        refreshed.assert_not_called()
        self.assertEqual(before, self.key_digest())
        self.assertEqual(["button_slot_returned"], [event["kind"] for event in events])

    def test_legacy_controller_without_new_hook_keeps_existing_save_and_clear_buttons_working(self):
        original = self.page.chat_controller
        class LegacyController:
            def __getattr__(self, name):
                if name == "refresh_agent_queue_policy": raise AttributeError(name)
                return getattr(original, name)
        self.fill(); events = []
        with patch.object(self.page, "chat_controller", LegacyController()):
            self.click(self.page.save_api_button, events)
            self.assertEqual("synthetic-replacement-model", self.settings.load_api_settings().model)
            with patch("desktop_pet.ui.settings_controls.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                self.click(self.page.clear_key_button, events)
        self.assertEqual(self.digest(""), self.key_digest())
        self.assertEqual(["button_slot_returned"] * 2, [event["kind"] for event in events])

    def agent_panel(self):
        self.session.open_settings("agent")
        return self.session.chat_window.settings_page._pages["agent"]

    def test_agent_public_settings_written_then_exception_still_refresh_actual_policy_synchronously(self):
        panel = self.agent_panel()
        real = self.session.chat.refresh_agent_queue_policy
        real(); before = self.scope_digest(); events = []
        panel.tools_checkbox.setChecked(not self.settings.load_agent_settings().tools_enabled)
        expected = panel.tools_checkbox.isChecked()
        original = self.settings.save_agent_settings
        def uncertain(value, **kwargs):
            original(value, **kwargs)
            raise OSError("Synthetic Agent sync confirmation failed")
        with patch.object(self.settings, "save_agent_settings", side_effect=uncertain), \
             patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events, real=real)):
            self.click(panel.save_button, events)
        self.assert_synchronous(events)
        self.assertEqual(expected, self.settings.load_agent_settings().tools_enabled)
        self.assertNotEqual(before, self.scope_digest())
        self.assertTrue(panel.error_label.text())
        self.assertNotIn("Synthetic Agent", panel.error_label.text())

    def test_global_key_written_then_exception_refreshes_both_model_credentials_synchronously(self):
        real = self.session.chat.refresh_agent_queue_policy
        real(); before = self.scope_digest(); events = []
        new_key = "SYNTHETIC_GLOBAL_WRITE_THEN_ERROR"
        self.fill(model="synthetic-shared-write-failure", key=new_key)
        original = self.settings.save_api_key
        def uncertain(value):
            original(value)
            raise CredentialsError("Synthetic shared key confirmation failed")
        with patch.object(self.settings, "save_api_key", side_effect=uncertain), \
             patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events, real=real)):
            self.click(self.page.save_api_button, events)
        self.assert_synchronous(events)
        self.assertEqual(self.digest(new_key), self.key_digest())
        self.assert_shared_policy(key=new_key)
        self.assertNotEqual(before, self.scope_digest())
        self.assertNotIn(new_key, self.page.api_error_label.text())

    def test_legacy_gui_configuration_and_key_changes_cannot_change_effective_queue_policy(self):
        real = self.session.chat.refresh_agent_queue_policy
        real(); before = self.scope_digest()
        self.assert_shared_policy()
        for name, value in (("gui_base_url", "https://obsolete-gui.invalid/other/v1"),
                            ("gui_model", "obsolete-separate-model"),
                            ("gui_provider", "ui-tars-1.0"),
                            ("gui_coordinate_profile", "native")):
            self.settings._settings.setValue("agents/" + name, value)
        self.settings.save_agent_gui_api_key("SYNTHETIC_UNUSED_LEGACY_GUI_KEY")
        self.session.chat.refresh_agent_settings()
        self.assert_shared_policy()
        self.assertEqual(before, self.scope_digest())
        self.settings.clear_agent_gui_api_key()
        self.session.chat.refresh_agent_settings()
        self.assert_shared_policy()
        self.assertEqual(before, self.scope_digest())

    def test_unreadable_agent_preferences_cannot_leave_old_enabled_policy_after_notify(self):
        panel = self.agent_panel()
        real = self.session.chat.refresh_agent_queue_policy
        real(); before = self.scope_digest(); events = []
        panel.tools_checkbox.setChecked(not panel.tools_checkbox.isChecked())
        with patch.object(self.settings, "load_agent_settings", side_effect=OSError("Synthetic Agent preferences unavailable")), \
             patch.object(self.settings, "save_agent_settings", side_effect=OSError("Synthetic Agent save unavailable")), \
             patch.object(self.session.chat, "refresh_agent_queue_policy", side_effect=self.observer(events, real=real)):
            self.click(panel.save_button, events)
        self.assert_synchronous(events)
        self.assertFalse(self.session.chat.agent_settings.enabled)
        policy = self.session.chat._agent_queue_policy
        self.assertTrue(policy is None or policy["agent"].enabled is False)
        self.assertNotEqual(before, self.scope_digest())


if __name__ == "__main__":
    unittest.main()
