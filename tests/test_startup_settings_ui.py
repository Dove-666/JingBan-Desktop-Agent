"""Startup settings use real Qt/local INI with an injected memory-only service.

These tests never read or write the user's HKCU Run key or credential vault.
The existing isolated application fixture supplies synthetic artwork/history.
"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from desktop_pet.settings_manager import SettingsError
from tests.test_application import MemoryCredentialBackend, isolated_settings
from tests import test_settings_navigation as navigation_fixture


class FakeStartupManager:
    """Small injectable service with no OS registry or process operations."""

    def __init__(self, *, enabled=False, available=True):
        self.enabled = enabled
        self.available = available
        self.reads = 0
        self.changes = []
        self.read_error = False
        self.write_error = False

    def is_enabled(self):
        self.reads += 1
        if self.read_error:
            raise OSError("synthetic-private-registry-error")
        return self.enabled

    def set_enabled(self, enabled):
        self.changes.append(enabled)
        if self.write_error:
            raise OSError("synthetic-private-registry-error")
        self.enabled = enabled


class StartupSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.startup = FakeStartupManager()
        # Patch before constructing ApplicationController so even registry reads
        # are isolated. The fixture's other services already use test doubles.
        with patch("desktop_pet.application_controller.StartupManager", return_value=self.startup, create=True):
            navigation_fixture.EmbeddedSettingsNavigationTests.setUp(self)
        self.session.startup_manager = self.startup

    tearDown = navigation_fixture.EmbeddedSettingsNavigationTests.tearDown

    def open_general(self):
        self.session.open_settings("general")
        return self.session.chat_window.settings_page

    def test_opening_settings_reads_defaults_without_enabling_autostart(self):
        self.assertFalse(self.settings.load_silent_start())
        page = self.open_general()
        self.assertIs(page.startup_manager, self.startup)
        self.assertFalse(page.autostart_checkbox.isChecked())
        self.assertFalse(page.silent_start_checkbox.isChecked())
        self.assertTrue(page.autostart_checkbox.isEnabled())
        self.assertEqual(self.startup.changes, [])
        self.assertFalse(self.settings._settings.contains("startup/silent"))
        self.assertFalse(self.session.chat.busy)

    def test_existing_preferences_are_displayed_without_rewriting_them(self):
        self.startup.enabled = True
        self.settings.save_silent_start(True)
        page = self.open_general()
        self.assertTrue(page.autostart_checkbox.isChecked())
        self.assertTrue(page.silent_start_checkbox.isChecked())
        self.assertEqual(self.startup.changes, [])
        self.session.open_settings("api")
        self.session.open_settings("general")
        self.assertIs(self.session.chat_window.settings_page, page)
        self.assertEqual(self.startup.changes, [])

    def test_user_can_toggle_autostart_without_changing_silent_or_api_settings(self):
        api_before = self.settings.load_api_settings()
        page = self.open_general()
        page.autostart_checkbox.click()
        self.assertTrue(self.startup.enabled)
        self.assertTrue(page.autostart_checkbox.isChecked())
        self.assertIn("已开启", page.startup_status_label.text())
        page.autostart_checkbox.click()
        self.assertFalse(self.startup.enabled)
        self.assertEqual(self.startup.changes, [True, False])
        self.assertFalse(self.settings.load_silent_start())
        self.assertEqual(self.settings.load_api_settings(), api_before)

    def test_registry_write_failure_rolls_back_checkbox_without_exposing_details(self):
        self.startup.enabled = True
        self.startup.write_error = True
        page = self.open_general()
        page.autostart_checkbox.click()
        self.assertTrue(page.autostart_checkbox.isChecked())
        self.assertTrue(self.startup.enabled)
        self.assertEqual(self.startup.changes, [False])
        self.assertFalse(page.startup_status_label.isHidden())
        self.assertIn("未能保存", page.startup_status_label.text())
        self.assertNotIn("synthetic-private", page.startup_status_label.text())

    def test_registry_read_failure_disables_only_autostart_and_does_not_write(self):
        self.startup.read_error = True
        page = self.open_general()
        self.assertFalse(page.autostart_checkbox.isEnabled())
        self.assertTrue(page.silent_start_checkbox.isEnabled())
        self.assertEqual(self.startup.changes, [])
        self.assertIn("无法读取", page.startup_status_label.text())
        self.assertNotIn("synthetic-private", page.startup_status_label.text())

    def test_unavailable_autostart_does_not_disable_silent_preference(self):
        self.startup.available = False
        page = self.open_general()
        self.assertFalse(page.autostart_checkbox.isEnabled())
        self.assertTrue(page.silent_start_checkbox.isEnabled())
        self.assertEqual(self.startup.reads, 0)
        self.assertEqual(self.startup.changes, [])

    def test_silent_toggle_persists_for_a_new_settings_manager_without_hiding_live_pet(self):
        self.session.show()
        page = self.open_general()
        page.silent_start_checkbox.click()
        self.assertTrue(self.settings.load_silent_start())
        reopened = isolated_settings(Path(self.settings.config_path), self.backend)
        self.assertTrue(reopened.load_silent_start())
        self.assertEqual(self.startup.changes, [])
        self.assertTrue(self.session.pet.isVisible(), "Changing next-start preference must not hide the current pet")
        self.assertIn("下次开机自动启动", page.startup_status_label.text())

    def test_sync_failure_rolls_back_preference_memory_and_checkbox(self):
        for initial in (False, True):
            with self.subTest(initial=initial):
                self.settings.save_silent_start(initial)
                page = self.open_general()
                # Refresh the same existing form through its real saved state
                # when the test changes the preference between subtests.
                page._silent_start_enabled = initial
                page._restore_checked(page.silent_start_checkbox, initial)
                with patch.object(self.settings, "_sync", side_effect=SettingsError("synthetic-storage-error")):
                    page.silent_start_checkbox.click()
                self.assertEqual(self.settings.load_silent_start(), initial)
                self.assertEqual(page.silent_start_checkbox.isChecked(), initial)
                self.assertIn("未能保存", page.startup_status_label.text())
                self.assertNotIn("synthetic-storage-error", page.startup_status_label.text())
        self.assertEqual(self.startup.changes, [])


class SilentStartupPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-silent-preference-")
        self.path = Path(self.directory.name) / "settings.ini"
        self.backend = MemoryCredentialBackend()
        self.settings = isolated_settings(self.path, self.backend)

    def tearDown(self):
        self.settings._settings.sync()
        self.directory.cleanup()

    def test_default_and_restart_recovery(self):
        self.assertFalse(self.settings.load_silent_start())
        self.settings.save_silent_start(True)
        self.assertTrue(isolated_settings(self.path, self.backend).load_silent_start())
        self.settings.save_silent_start(False)
        self.assertFalse(isolated_settings(self.path, self.backend).load_silent_start())

    def test_rejects_non_boolean_values_without_overwriting_saved_preference(self):
        self.settings.save_silent_start(True)
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(SettingsError):
                    self.settings.save_silent_start(value)
                self.assertTrue(self.settings.load_silent_start())

    def test_damaged_preference_safely_defaults_to_visible_startup(self):
        for value in ("invalid", "yes", "1", 1, 0, None, ["true"]):
            with self.subTest(value=value):
                self.settings._settings.setValue("startup/silent", value)
                self.assertFalse(self.settings.load_silent_start())

    def test_ini_boolean_strings_restore(self):
        for value, expected in (("true", True), ("TRUE", True), ("false", False)):
            with self.subTest(value=value):
                self.settings._settings.setValue("startup/silent", value)
                self.assertEqual(self.settings.load_silent_start(), expected)

    def test_failed_sync_restores_missing_and_existing_in_memory_settings(self):
        self.assertFalse(self.settings._settings.contains("startup/silent"))
        with patch.object(self.settings, "_sync", side_effect=SettingsError("synthetic-write-failure")):
            with self.assertRaises(SettingsError):
                self.settings.save_silent_start(True)
        self.assertFalse(self.settings._settings.contains("startup/silent"))
        self.assertFalse(self.settings.load_silent_start())
        self.settings.save_silent_start(True)
        with patch.object(self.settings, "_sync", side_effect=SettingsError("synthetic-write-failure")):
            with self.assertRaises(SettingsError):
                self.settings.save_silent_start(False)
        self.assertTrue(self.settings.load_silent_start())


if __name__ == "__main__":
    unittest.main()
