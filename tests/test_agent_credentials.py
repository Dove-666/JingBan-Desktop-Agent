"""Real temporary QSettings and memory-only credentials for Agent preferences."""

from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings

from desktop_pet.agents import AgentSettings
from desktop_pet.agents.settings import AGENT_WORKSPACE_ROOT, GUI_CREDENTIAL_ACCOUNT
from desktop_pet.settings_manager import (
    ApiSettings, CredentialStore, CredentialsError, SettingsError, SettingsManager,
)
from tests.test_agent_chat_routing import MemoryVault


CHAT_KEY = "local-credential-chat-secret"
GUI_KEY = "local-credential-gui-secret"
PENDING_KEY = "local-credential-pending-secret"


class FailingVault(MemoryVault):
    fail_writes = False

    def set_password(self, service, account, value):
        if self.fail_writes:
            raise OSError("private-vault-detail " + value)
        super().set_password(service, account, value)


class AgentCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-agent-credentials-")
        self.path = Path(self.directory.name) / "settings.ini"
        self.vault = FailingVault()
        self.manager = self.new_manager()
        self.manager.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "fixture-chat-model", 5))
        self.manager.save_api_key(CHAT_KEY)
        self.manager.save_agent_gui_api_key(GUI_KEY)

    def new_manager(self):
        return SettingsManager(settings=QSettings(str(self.path), QSettings.Format.IniFormat),
                               credential_store=CredentialStore(backend=self.vault))

    def tearDown(self):
        self.manager._settings.sync()
        self.directory.cleanup()

    def assert_no_ini_secrets(self):
        self.manager._settings.sync()
        text = self.path.read_text(encoding="utf-8-sig")
        for key in (CHAT_KEY, GUI_KEY, PENDING_KEY):
            self.assertNotIn(key, text)

    def test_gui_credential_uses_independent_slot_and_survives_manager_reopen(self):
        self.assertEqual(self.manager.get_api_key(), CHAT_KEY)
        self.assertEqual(self.manager.get_agent_gui_api_key(), GUI_KEY)
        accounts = {account for _service, account in self.vault.values}
        self.assertIn("chat-api-key", accounts)
        self.assertIn(GUI_CREDENTIAL_ACCOUNT, accounts)
        reopened = self.new_manager()
        self.assertEqual(reopened.get_api_key(), CHAT_KEY)
        self.assertEqual(reopened.get_agent_gui_api_key(), GUI_KEY)
        self.assert_no_ini_secrets()

    def test_gui_key_and_chat_key_are_never_written_to_ini(self):
        settings = replace(self.manager.load_agent_settings(), gui_model="ui-tars-fixture", max_steps=18)
        self.manager.save_agent_settings(settings)
        self.assertEqual(self.new_manager().load_agent_settings(), settings)
        self.assertFalse(any("api_key" in name for name in self.manager._settings.allKeys()))
        self.assertNotIn(GUI_KEY, repr(asdict(settings)))
        self.assert_no_ini_secrets()

    def test_agent_public_fields_reject_pending_chat_and_gui_keys_before_writing(self):
        original = self.manager.load_agent_settings()
        for secret in (PENDING_KEY, CHAT_KEY, GUI_KEY):
            candidates = [
                replace(original, gui_model="model/" + secret),
                replace(original, gui_base_url="https://example.test/v1/" + secret),
                replace(original, workspace=str(AGENT_WORKSPACE_ROOT / secret)),
                replace(original, python_executable=str(Path(original.python_executable).with_name("python-" + secret + ".exe"))),
            ]
            for candidate in candidates:
                with self.subTest(secret=secret, candidate=candidate):
                    with self.assertRaises(SettingsError) as caught:
                        self.manager.save_agent_settings(candidate,
                            pending_api_key=PENDING_KEY if secret == PENDING_KEY else "")
                    for key in (CHAT_KEY, GUI_KEY, PENDING_KEY):
                        self.assertNotIn(key, str(caught.exception))
                    self.assertEqual(self.manager.load_agent_settings(), original)
        self.assert_no_ini_secrets()

    def test_clearing_gui_key_preserves_chat_key_and_public_preferences(self):
        original = self.manager.load_agent_settings()
        self.manager.clear_agent_gui_api_key()
        self.assertEqual(self.manager.get_agent_gui_api_key(), "")
        self.assertEqual(self.manager.get_api_key(), CHAT_KEY)
        self.assertEqual(self.manager.load_agent_settings(), original)
        self.assertNotIn(GUI_CREDENTIAL_ACCOUNT, {account for _service, account in self.vault.values})
        self.assert_no_ini_secrets()

    def test_corrupted_gui_preferences_disable_agent_without_changing_credentials(self):
        for key, value in (("gui_provider", "unrecognized"), ("gui_base_url", "https://user:password@example.test"),
                           ("gui_model", "model\nwith-control"), ("enabled", "invalid-bool"),
                           ("timeout_seconds", "NaN"), ("workspace", "relative-directory")):
            with self.subTest(key=key):
                self.manager._settings.remove("agents")
                self.manager._settings.setValue("agents/" + key, value)
                self.manager._settings.sync()
                self.assertFalse(self.manager.load_agent_settings().enabled)
                self.assertEqual(self.manager.get_api_key(), CHAT_KEY)
                self.assertEqual(self.manager.get_agent_gui_api_key(), GUI_KEY)
        self.assert_no_ini_secrets()

    def test_default_agent_is_enabled_without_requiring_a_gui_model_or_key(self):
        self.manager.clear_agent_gui_api_key()
        settings = self.manager.load_agent_settings()
        self.assertEqual(settings, AgentSettings())
        self.assertTrue(settings.enabled)
        self.assertFalse(settings.computer_enabled)
        self.assertEqual(settings.gui_model, "")
        self.manager.save_agent_settings(settings)
        self.assertTrue(self.manager.load_agent_settings().tools_enabled)
        self.assert_no_ini_secrets()

    def test_vault_failure_is_sanitized_and_does_not_replace_existing_credentials(self):
        self.vault.fail_writes = True
        with self.assertRaises(CredentialsError) as caught:
            self.manager.save_agent_gui_api_key(PENDING_KEY)
        self.assertNotIn(PENDING_KEY, str(caught.exception))
        self.assertNotIn("private-vault-detail", str(caught.exception))
        self.assertEqual(self.manager.get_agent_gui_api_key(), GUI_KEY)
        self.assertEqual(self.manager.get_api_key(), CHAT_KEY)
        self.assert_no_ini_secrets()

    def test_invalid_key_drafts_are_rejected_without_changing_saved_key(self):
        for key in ("", " ", "not-a-valid\nkey", "key\x00value"):
            with self.subTest(key=repr(key)):
                with self.assertRaises(CredentialsError):
                    self.manager.save_agent_gui_api_key(key)
                self.assertEqual(self.manager.get_agent_gui_api_key(), GUI_KEY)
        self.assert_no_ini_secrets()

    def test_new_gui_key_cannot_legitimize_a_key_already_pasted_in_public_agent_fields(self):
        unsafe = replace(self.manager.load_agent_settings(), gui_model=PENDING_KEY)
        self.manager.save_agent_settings(unsafe)
        with self.assertRaises(CredentialsError) as caught:
            self.manager.save_agent_gui_api_key(PENDING_KEY)
        self.assertNotIn(PENDING_KEY, str(caught.exception))
        self.assertEqual(self.manager.get_agent_gui_api_key(), GUI_KEY)
        self.assertEqual(self.manager.get_api_key(), CHAT_KEY)

    def test_chat_public_api_settings_reject_saved_gui_key_without_plaintext_write(self):
        original = self.manager.load_api_settings()
        for candidate in (replace(original, model="model-" + GUI_KEY),
                          replace(original, base_url="https://example.test/v1/" + GUI_KEY)):
            with self.subTest(candidate=candidate):
                with self.assertRaises(SettingsError) as caught:
                    self.manager.save_api_settings(candidate)
                self.assertNotIn(GUI_KEY, str(caught.exception))
                self.assertEqual(self.manager.load_api_settings(), original)
        self.assert_no_ini_secrets()

    def test_perception_and_camera_preferences_reject_saved_gui_key(self):
        for loader, saver in ((self.manager.load_perception_settings, self.manager.save_perception_settings),
                              (self.manager.load_camera_settings, self.manager.save_camera_settings)):
            original = loader()
            unsafe = replace(original, privacy_keywords=(*original.privacy_keywords, GUI_KEY))
            with self.assertRaises(SettingsError) as caught:
                saver(unsafe)
            self.assertNotIn(GUI_KEY, str(caught.exception))
            self.assertEqual(loader(), original)
        self.assert_no_ini_secrets()

    def test_known_key_getter_is_read_only_deduplicated_and_sanitizes_vault_failures(self):
        with patch.object(self.vault, "set_password", wraps=self.vault.set_password) as save:
            keys = self.manager.get_known_api_keys()
            self.assertEqual(set(keys), {CHAT_KEY, GUI_KEY})
            self.assertEqual(len(keys), 2)
            save.assert_not_called()
        original = self.vault.get_password
        def broken_gui_read(service, account):
            if account == GUI_CREDENTIAL_ACCOUNT:
                raise OSError("private-backend-detail " + GUI_KEY)
            return original(service, account)
        with patch.object(self.vault, "get_password", side_effect=broken_gui_read):
            self.assertEqual(self.manager.get_known_api_keys(), (CHAT_KEY,))
        self.manager.save_agent_gui_api_key(CHAT_KEY)
        self.assertEqual(self.manager.get_known_api_keys(), (CHAT_KEY,))
        self.manager.clear_api_key()
        self.manager.clear_agent_gui_api_key()
        self.assertEqual(self.manager.get_known_api_keys(), ())

    def test_new_key_save_rejects_secret_already_pasted_in_other_public_preferences(self):
        for save_key in (self.manager.save_api_key, self.manager.save_agent_gui_api_key):
            original = self.manager.load_api_settings()
            self.manager.save_api_settings(replace(original, model="model-" + PENDING_KEY))
            with self.assertRaises(CredentialsError) as caught:
                save_key(PENDING_KEY)
            self.assertNotIn(PENDING_KEY, str(caught.exception))
            self.assertEqual(self.manager.get_api_key(), CHAT_KEY)
            self.assertEqual(self.manager.get_agent_gui_api_key(), GUI_KEY)
            self.manager.save_api_settings(original)
        self.assert_no_ini_secrets()

    def test_memory_public_configuration_and_candidates_include_gui_key_in_known_secrets(self):
        from desktop_pet.memory.service import MemoryService
        service = MemoryService(self.manager, Path(self.directory.name) / "memory",
            llm_credentials=CredentialStore(account="mem0-llm-key", backend=self.vault),
            embedding_credentials=CredentialStore(account="mem0-embedding-key", backend=self.vault))
        try:
            self.assertIn(GUI_KEY, service._known_keys())
            original = service.load_settings()
            with self.assertRaises(SettingsError):
                service.save_settings(replace(original, llm_model="model-" + GUI_KEY))
            self.assertEqual(service.load_settings(), original)
            self.assertNotIn(GUI_KEY, service.config_path.read_text(encoding="utf-8"))
        finally:
            service.shutdown()


if __name__ == "__main__":
    unittest.main()
