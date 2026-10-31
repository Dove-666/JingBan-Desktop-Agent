"""Settings round trips and security boundaries without real credentials."""

import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QSettings

from desktop_pet.settings_manager import (
    ApiSettings, CredentialsError, CredentialStore, SettingsError, SettingsManager,
)


class FakeVault:
    def __init__(self):
        self.values = {}
        self.fail = False
        self.ignore_writes = False
        self.ignore_deletes = False
        self.secret_for_exception = "SYNTHETIC-SECRET-NEVER-PRINT"

    def _check(self):
        if self.fail:
            raise RuntimeError(self.secret_for_exception)

    def get_password(self, service, account):
        self._check()
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        self._check()
        if not self.ignore_writes:
            self.values[(service, account)] = password

    def delete_password(self, service, account):
        self._check()
        if not self.ignore_deletes:
            self.values.pop((service, account), None)


class ApiSettingsTests(unittest.TestCase):
    def test_custom_base_and_empty_unsaved_model(self):
        config = ApiSettings("http://localhost:8888/custom/v1", "", 1)
        config.validate(require_model=False)
        with self.assertRaisesRegex(SettingsError, "Model Name"):
            config.validate()
        ApiSettings("https://provider.example/v1/", "model-A", 120).validate()

    def test_rejects_sensitive_or_malformed_url_without_echoing_input(self):
        urls = (
            "https://user:SYNTHETIC-SECRET@example.test/v1",
            "https://example.test/v1?key=SYNTHETIC-SECRET",
            "https://example.test/v1#SYNTHETIC-SECRET",
            "https://example.test/\nSYNTHETIC-SECRET",
            "https://example.test/\tSYNTHETIC-SECRET",
            "https://example.test/ SYNTHETIC-SECRET",
            "file:///SYNTHETIC-SECRET",
            "https://example.test:99999/v1",
            "https:///v1", "relative/path", "https://[broken/v1", "",
        )
        for value in urls:
            with self.subTest(url_case=urls.index(value)):
                with self.assertRaises(SettingsError) as caught:
                    ApiSettings(value, "model").validate()
                self.assertNotIn("SYNTHETIC-SECRET", str(caught.exception))

    def test_timeout_and_model_validation(self):
        for value in (0, 121, float("nan"), float("inf"), True, "30"):
            with self.subTest(value=value):
                with self.assertRaises(SettingsError):
                    ApiSettings(model="model", timeout_seconds=value).validate()
        for model in (None, "", "  ", "model\n"):
            with self.subTest(model=model):
                with self.assertRaises(SettingsError):
                    ApiSettings(model=model).validate()


class SettingsManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-settings-test-")
        self.path = Path(self.directory.name) / "preferences.ini"
        self.vault = FakeVault()
        self.store = CredentialStore(backend=self.vault)
        self.manager = self.make_manager()

    def tearDown(self):
        self.directory.cleanup()

    def make_manager(self):
        return SettingsManager(QSettings(str(self.path), QSettings.Format.IniFormat), self.store)

    def test_preferences_and_scale_survive_new_manager(self):
        expected = ApiSettings("https://example.test/custom/v1", "test-model", 75)
        self.manager.save_api_settings(expected)
        self.manager.save_scale(175)
        restarted = self.make_manager()
        self.assertEqual(restarted.load_api_settings(), expected)
        self.assertEqual(restarted.load_scale(), 175)
        self.assertEqual(Path(restarted.config_path), self.path)

    def test_reading_font_persists_independently_of_pet_scale_and_rejects_invalid_values(self):
        self.assertEqual(self.manager.load_reading_font(), 14)
        self.manager.save_scale(50)
        self.manager.save_reading_font(18)
        restarted = self.make_manager()
        self.assertEqual(restarted.load_scale(), 50)
        self.assertEqual(restarted.load_reading_font(), 18)
        for value in (True, 6, 23, 14.5, "18"):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                restarted.save_reading_font(value)
        raw = QSettings(str(self.path), QSettings.Format.IniFormat)
        raw.setValue("pet/reading_font_points", 6)
        raw.sync()
        self.assertEqual(self.make_manager().load_reading_font(), 14)

    def test_chat_reading_size_roundtrip_and_invalid_config_keeps_other_preferences(self):
        self.assertEqual(self.manager.load_chat_font_size(), 16)
        self.manager.save_scale(75)
        self.manager.save_reading_font(18)
        self.manager.save_chat_font_size(20)
        self.assertEqual(self.make_manager().load_chat_font_size(), 20)
        for value in (True, False, 11, 23, 16.5, "18"):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                self.manager.save_chat_font_size(value)
        raw = QSettings(str(self.path), QSettings.Format.IniFormat)
        for value in (True, 11, "damaged", 16.5):
            raw.setValue("chat/reading_font_size", value)
            raw.sync()
            self.assertEqual(self.make_manager().load_chat_font_size(), 16)
        self.assertEqual(self.make_manager().load_scale(), 75)
        self.assertEqual(self.make_manager().load_reading_font(), 18)

    def test_defaults_and_corrupted_preferences_are_safe(self):
        self.assertEqual(self.manager.load_api_settings(), ApiSettings())
        self.assertEqual(self.manager.load_scale(), 100)
        settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        for key, value in (("api/base_url", "not-a-url"), ("api/timeout_seconds", "bad")):
            settings.setValue(key, value)
        settings.setValue("pet/scale_percent", "broken")
        settings.sync()
        self.assertEqual(self.make_manager().load_api_settings(), ApiSettings())
        self.assertEqual(self.make_manager().load_scale(), 100)

    def test_invalid_scale_never_saved(self):
        for scale in (49, 201, True, 100.5, "100"):
            with self.subTest(scale=scale):
                with self.assertRaises(SettingsError):
                    self.manager.save_scale(scale)
        self.assertEqual(self.manager.load_scale(), 100)

    def test_invalid_api_url_never_saved(self):
        with self.assertRaises(SettingsError):
            self.manager.save_api_settings(ApiSettings("https://example.test/?key=synthetic-secret"))
        self.assertFalse(self.path.exists())

    def test_known_key_in_ordinary_fields_is_never_persisted(self):
        secret = "SYNTHETIC-CREDENTIAL-PASTED-IN-WRONG-FIELD"
        self.manager.save_api_key(secret)
        for settings in (
            ApiSettings("https://example.test/v1/" + secret, "model"),
            ApiSettings("https://example.test/v1", "model-" + secret),
        ):
            with self.subTest(field="base_url" if secret in settings.base_url else "model"):
                with self.assertRaises(SettingsError) as caught:
                    self.manager.save_api_settings(settings)
                self.assertNotIn(secret, str(caught.exception))
                self.assertFalse(self.path.exists())

    def test_new_key_in_ordinary_fields_is_rejected_before_saving_config(self):
        secret = "SYNTHETIC-NEW-CREDENTIAL"
        with self.assertRaises(SettingsError):
            self.manager.save_api_settings(ApiSettings(model=secret), pending_api_key=secret)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.manager.get_api_key(), "")

    def test_key_cannot_be_saved_if_already_present_in_ordinary_config(self):
        secret = "SYNTHETIC-NEW-CREDENTIAL"
        self.manager.save_api_settings(ApiSettings(model=secret))
        with self.assertRaises(CredentialsError) as caught:
            self.manager.save_api_key(secret)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.manager.get_api_key(), "")

    def test_key_round_trip_delete_and_no_plaintext_secret(self):
        secret = "SYNTHETIC-KEY-ONLY-FOR-TESTS"
        self.manager.save_api_settings(ApiSettings(model="test-model"))
        self.manager.save_api_key(secret)
        self.manager.save_scale(125)
        restarted = self.make_manager()
        self.assertTrue(restarted.credentials_available)
        self.assertEqual(restarted.get_api_key(), secret)
        self.assertNotIn(secret, self.path.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", self.path.read_text(encoding="utf-8"))
        restarted.clear_api_key()
        self.assertEqual(restarted.get_api_key(), "")
        self.assertNotIn((self.store.service, self.store.account), self.vault.values)
        restarted.clear_api_key()  # Already absent is a successful clear.

    def test_credential_failures_do_not_leak_backend_exception(self):
        self.vault.fail = True
        for operation in (
            self.manager.get_api_key,
            lambda: self.manager.save_api_key("SYNTHETIC-KEY"),
            self.manager.clear_api_key,
        ):
            with self.subTest(operation=operation):
                try:
                    operation()
                except CredentialsError as error:
                    rendered = "".join(traceback.format_exception(error))
                    self.assertNotIn(self.vault.secret_for_exception, rendered)
                    self.assertNotIn(self.vault.secret_for_exception, self.manager.credential_status)
                else:
                    self.fail("CredentialsError was not raised")

    def test_silent_backend_write_and_delete_failures_are_reported(self):
        self.vault.ignore_writes = True
        with self.assertRaises(CredentialsError):
            self.manager.save_api_key("synthetic")
        self.vault.ignore_writes = False
        self.manager.save_api_key("synthetic")
        self.vault.ignore_deletes = True
        with self.assertRaisesRegex(CredentialsError, "删除"):
            self.manager.clear_api_key()
        self.assertEqual(self.manager.get_api_key(), "synthetic")

    def test_unavailable_vault_does_not_break_preferences(self):
        with patch("desktop_pet.settings_manager.sys.platform", "linux"):
            unavailable = CredentialStore()
        manager = SettingsManager(QSettings(str(self.path), QSettings.Format.IniFormat), unavailable)
        self.assertFalse(manager.credentials_available)
        self.assertIn("未使用明文回退", manager.credential_status)
        manager.save_api_settings(ApiSettings(model="still-usable"))
        manager.save_scale(150)
        self.assertEqual(manager.load_scale(), 150)
        for operation in (manager.get_api_key, lambda: manager.save_api_key("synthetic"), manager.clear_api_key):
            with self.assertRaises(CredentialsError):
                operation()

    def test_perception_preferences_survive_reload_without_runtime_data(self):
        from dataclasses import replace
        from desktop_pet.perception.settings import PerceptionSettings

        preferences = replace(PerceptionSettings(), enabled=True, allow_remote=True,
                              observation_interval_seconds=15, privacy_keywords=("private", "密码"))
        self.manager.save_perception_settings(preferences)
        reloaded = SettingsManager(QSettings(str(self.path), QSettings.Format.IniFormat),
                                   self.manager._credentials)
        self.assertEqual(reloaded.load_perception_settings(), preferences)
        self.assertNotIn("screenshot", self.path.read_text(encoding="utf-8"))

    def test_malformed_consent_cannot_enable_remote_capture(self):
        self.manager._settings.setValue("perception/enabled", "invalid-but-truthy")
        self.manager._settings.setValue("perception/allow_remote", True)
        loaded = self.manager.load_perception_settings()
        self.assertFalse(loaded.enabled)
        self.assertFalse(loaded.allow_remote)


if __name__ == "__main__":
    unittest.main()
