"""Owned, provider-free memory readiness reads and legacy facade contracts."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QSettings, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.memory.service import MemoryService, MemoryUnavailable
from desktop_pet.memory_controller import LazyMemoryService, MemoryController
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
from tests.test_application import spin_until


SECRET = "OWNED_READINESS_SECRET_NEVER_SERIALIZE"


class CountingVault:
    def __init__(self):
        self.values = {}
        self.reads = Counter()
        self.unreadable = set()

    def get_password(self, service, account):
        self.reads[account] += 1
        if account in self.unreadable:
            raise RuntimeError(SECRET)
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class MemoryReadinessSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-readiness-owned-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = CountingVault()
        self.chat = SettingsManager(
            QSettings(str(self.root / "chat.ini"), QSettings.Format.IniFormat),
            CredentialStore(backend=self.vault),
        )
        self.chat.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "owned-model"))
        self.chat.save_api_key(SECRET)
        self.service = MemoryService(
            self.chat, self.root / "memory",
            llm_credentials=CredentialStore(account="owned-llm", backend=self.vault),
            embedding_credentials=CredentialStore(account="owned-embedding", backend=self.vault),
        )
        self.addCleanup(self.service.shutdown)
        self.controllers = []
        self.addCleanup(self.close_controllers)
        self.vault.reads.clear()

    def close_controllers(self):
        for controller in self.controllers:
            controller.shutdown()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(3000))
        for controller in self.controllers:
            controller.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def controller(self, service=None):
        with patch.object(MemoryController, "_operate", return_value=True):
            controller = MemoryController(service or self.service)
        self.controllers.append(controller)
        return controller

    def read_on_worker(self, function):
        with ThreadPoolExecutor(max_workers=1) as worker:
            return worker.submit(function).result(timeout=3)

    def test_controller_state_reads_memory_json_and_each_selected_key_once(self):
        settings = replace(self.service.load_settings(), embedding_provider="openai")
        self.service.save_settings(settings, embedding_key=SECRET)
        self.vault.reads.clear()
        controller = self.controller()
        original = self.service.load_settings
        with patch.object(self.service, "load_settings", wraps=original) as reads:
            result = self.read_on_worker(controller._load_state)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(self.vault.reads, {"chat-api-key": 1, "owned-embedding": 1})
        self.assertEqual(result[0], settings)
        self.assertTrue(result[2]["chat_configured"] and result[2]["embedding_configured"])
        self.assertNotIn(SECRET, repr(result))

    def test_readiness_does_not_construct_an_unused_credential_or_settings_store(self):
        controller = self.controller()
        with patch.object(CredentialStore, "__init__", side_effect=AssertionError("UNUSED_VAULT")), \
                patch.object(SettingsManager, "__init__", side_effect=AssertionError("UNUSED_SETTINGS")):
            result = self.read_on_worker(controller._load_state)
        self.assertTrue(result[2]["chat_configured"])

    def test_missing_unreadable_and_restored_keys_refresh_without_a_long_lived_cache(self):
        settings = replace(self.service.load_settings(), embedding_provider="openai")
        self.service.save_settings(settings)
        controller = self.controller()
        ready = self.read_on_worker(controller._load_state)
        self.assertIn("Embedding 密钥：未保存", ready[1])
        self.assertFalse(ready[2]["embedding_configured"])
        self.vault.unreadable.update(("chat-api-key", "owned-embedding"))
        unavailable = self.read_on_worker(controller._load_state)
        self.assertIn("提炼模型复用的聊天密钥：无法读取", unavailable[1])
        self.assertIn("Embedding 密钥：无法读取", unavailable[1])
        self.assertIsNone(unavailable[2]["chat_configured"])
        self.assertIsNone(unavailable[2]["embedding_configured"])
        self.assertNotIn(SECRET, repr(unavailable))
        self.vault.unreadable.clear()
        self.service.set_embedding_key(SECRET)
        restored = self.read_on_worker(controller._load_state)
        self.assertTrue(restored[2]["embedding_configured"])
        self.assertIn("Embedding 密钥：已保存", restored[1])

    def test_invalid_model_precedes_unreadable_presence_for_configuration_flags(self):
        self.chat.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", ""))
        settings = replace(self.service.load_settings(), use_chat_model=False, llm_model="", embedding_model="", embedding_provider="openai")
        self.service.save_settings(settings)
        self.vault.unreadable.update(("chat-api-key", "owned-llm", "owned-embedding"))
        self.vault.reads.clear()
        result = self.read_on_worker(self.controller()._load_state)
        self.assertIn("独立记忆提炼密钥：无法读取", result[1])
        self.assertIn("Embedding 密钥：无法读取", result[1])
        self.assertEqual(result[2], {"chat_configured": False, "extraction_configured": False,
            "embedding_configured": False, "embedding_provider": "openai", "uses_chat_model": False,
            "vector_space_bound": False})
        self.assertNotIn("chat-api-key", self.vault.reads)

    def test_independent_model_samples_three_distinct_credentials_once(self):
        settings = replace(self.service.load_settings(), use_chat_model=False, llm_model="owned-extractor", embedding_provider="openai")
        self.service.save_settings(settings, llm_key=SECRET, embedding_key=SECRET)
        self.vault.reads.clear()
        result = self.read_on_worker(self.controller()._load_state)
        self.assertEqual(self.vault.reads, {"chat-api-key": 1, "owned-llm": 1, "owned-embedding": 1})
        self.assertFalse(result[0].enabled)
        self.assertTrue(result[2]["chat_configured"] and result[2]["extraction_configured"] and result[2]["embedding_configured"])

    def test_snapshot_derives_all_views_from_one_memory_configuration(self):
        original = self.service.load_settings()
        changed = replace(original, use_chat_model=False, llm_model="owned-extractor", embedding_provider="openai")
        controller = self.controller()
        with patch.object(self.service, "load_settings", side_effect=[original, changed, changed]) as reads:
            result = self.read_on_worker(controller._load_state)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(result[0], original)
        self.assertTrue(result[2]["uses_chat_model"])
        self.assertEqual(result[2]["embedding_provider"], "fastembed")
        self.assertIn("提炼模型复用的聊天密钥", result[1])

    def test_fresh_worker_qsettings_observes_another_saved_configuration(self):
        controller = self.controller()
        before = self.read_on_worker(controller._load_state)
        writer = SettingsManager(QSettings(self.chat.config_path, QSettings.Format.IniFormat),
            CredentialStore(backend=self.vault))
        writer.save_api_settings(ApiSettings("http://127.0.0.1:1/other", ""))
        self.assertFalse(self.read_on_worker(controller._load_state)[2]["chat_configured"])
        writer.save_api_settings(ApiSettings("http://127.0.0.1:1/other", "new-owned-model"))
        self.assertTrue(before[2]["chat_configured"])
        self.assertTrue(self.read_on_worker(controller._load_state)[2]["chat_configured"])

    def test_readonly_status_does_not_write_scan_vectors_or_start_work(self):
        controller = self.controller()
        with patch.object(Path, "write_text", side_effect=AssertionError("WRITE")), \
                patch.object(Path, "iterdir", side_effect=AssertionError("SCAN")), \
                patch.object(self.service, "_read_identity", side_effect=AssertionError("IDENTITY")), \
                patch.object(self.service, "_configuration", side_effect=AssertionError("EXECUTION")):
            result = self.read_on_worker(controller._load_state)
        self.assertFalse(result[0].enabled or result[0].automatic)

    def test_corrupt_memory_configuration_fails_before_any_credential_read(self):
        self.service.config_path.write_text("[]", encoding="utf-8")
        with self.assertRaises(MemoryUnavailable):
            self.read_on_worker(self.controller()._load_state)
        self.assertFalse(self.vault.reads)

    def test_public_status_calls_remain_fresh_and_compatible(self):
        self.assertIn("提炼模型复用的聊天密钥：已保存", self.read_on_worker(self.service.credential_status))
        self.vault.unreadable.add("chat-api-key")
        self.assertIsNone(self.read_on_worker(self.service.configuration_status)["chat_configured"])
        self.vault.unreadable.clear()
        self.vault.values.clear()
        self.assertIn("提炼模型复用的聊天密钥：未保存", self.read_on_worker(self.service.credential_status))
        self.assertFalse(self.read_on_worker(self.service.configuration_status)["chat_configured"])

    def test_lazy_legacy_service_and_direct_legacy_service_keep_the_old_fallback(self):
        from tests.test_memory_controller import FakeMemoryService
        for lazy in (False, True):
            with self.subTest(lazy=lazy):
                legacy = FakeMemoryService()
                wrapped = LazyMemoryService(lambda: legacy) if lazy else legacy
                result = self.read_on_worker(self.controller(wrapped)._load_state)
                self.assertEqual(result, (legacy.settings, legacy.credential_status(), {}))
                self.assertEqual([item[0] for item in legacy.calls[:2]], ["load_settings", "credential_status"])

    def test_actual_lazy_startup_uses_worker_snapshot_and_no_extra_store(self):
        main_thread = threading.get_ident()
        threads = []
        load = self.service.load_settings
        def observed():
            threads.append(threading.get_ident())
            return load()
        lazy = LazyMemoryService(lambda: self.service)
        with patch.object(self.service, "load_settings", side_effect=observed), \
                patch.object(CredentialStore, "__init__", side_effect=AssertionError("EXTRA_VAULT")):
            controller = MemoryController(lazy)
            self.controllers.append(controller)
            spin_until(lambda: not controller.busy and not controller._runner._tasks)
        self.assertEqual(controller.error, "")
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], main_thread)
        self.assertFalse(controller.settings.enabled)

    def test_snapshot_failure_is_not_hidden_by_legacy_fallback(self):
        for lazy in (False, True):
            with self.subTest(lazy=lazy):
                service = LazyMemoryService(lambda: self.service) if lazy else self.service
                controller = self.controller(service)
                with patch.object(self.service, "load_state_snapshot", side_effect=AttributeError("OWNED_FAILURE")), \
                        patch.object(self.service, "credential_status", side_effect=AssertionError("LEGACY_FALLBACK")):
                    with self.assertRaisesRegex(AttributeError, "OWNED_FAILURE"):
                        self.read_on_worker(controller._load_state)

    def test_api_reader_and_instance_parser_share_defaults_and_validated_fields(self):
        from desktop_pet.settings_manager import read_api_settings
        values = (("api/model", ""), ("api/model", "valid-model"),
                  ("api/timeout_seconds", "invalid"), ("api/timeout_seconds", "50"),
                  ("api/reasoning_effort", "invalid"), ("api/base_url", "file:///bad"))
        for key, value in values:
            with self.subTest(key=key, value=value):
                self.chat._settings.setValue(key, value)
                self.assertEqual(read_api_settings(self.chat._settings), self.chat.load_api_settings())


if __name__ == "__main__":
    unittest.main()
