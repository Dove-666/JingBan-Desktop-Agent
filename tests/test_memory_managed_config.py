"""Managed memory setup, using temporary stores and controlled workers only."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from PySide6.QtCore import QEvent, QSettings, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.memory import MemoryService, MemoryUnavailable
from desktop_pet.memory_controller import LazyMemoryService, MemoryController
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
from tests.test_application import spin_until
from tests.test_memory_controller import FakeMemoryService
from tests.test_memory_service import Vault


class ManagedMemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-managed-memory-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = Vault()
        self.chat = SettingsManager(QSettings(str(self.root / "chat.ini"), QSettings.Format.IniFormat),
                                    CredentialStore(backend=self.vault))
        self.chat.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "fixture-chat", 3))
        self.chat.save_api_key("fixture-only-chat-key")
        self.service = MemoryService(self.chat, self.root / "memory",
            llm_credentials=CredentialStore(account="managed-llm", backend=self.vault),
            embedding_credentials=CredentialStore(account="managed-embedding", backend=self.vault))
        self.addCleanup(self.service.shutdown)
        # Exercise migration of legacy, unconfigured defaults as well as new
        # managed stores. Production-created settings now default to local.
        self.service.save_settings(replace(self.service.load_settings(), embedding_provider="openai"))
        self.service.prepare_local_model = Mock(return_value={"ready": True})

    def test_fresh_store_is_local_without_enabling_or_starting_preparation(self):
        fresh = MemoryService(self.chat, self.root / "fresh-memory",
            llm_credentials=CredentialStore(account="managed-llm", backend=self.vault),
            embedding_credentials=CredentialStore(account="managed-embedding", backend=self.vault))
        self.addCleanup(fresh.shutdown)
        self.assertEqual(fresh.load_settings().embedding_provider, "fastembed")
        self.assertFalse(fresh.load_settings().enabled or fresh.load_settings().automatic)

    def test_real_empty_legacy_management_collection_can_use_local_defaults(self):
        self.assertEqual(self.service.list_memories_page()["total"], 0)
        self.assertTrue((self.service.data_dir / "qdrant").is_dir())
        self.service.set_automatic_enabled(True)
        self.assertEqual(self.service.load_settings().embedding_provider, "fastembed")
        self.assertTrue(self.service.load_settings().automatic)

    def test_real_populated_foreign_collection_without_identity_is_preserved(self):
        from qdrant_client import QdrantClient, models
        store = self.service.data_dir / "qdrant"
        local = QdrantClient(path=str(store))
        try:
            local.create_collection("foreign-data", vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
            local.upsert("foreign-data", points=[models.PointStruct(id=1, vector=[1., 0., 0.], payload={"fact": "keep"})])
        finally:
            local.close()
        with self.assertRaises(MemoryUnavailable):
            self.service.set_automatic_enabled(True)
        self.assertEqual(self.service.load_settings().embedding_provider, "openai")
        local = QdrantClient(path=str(store))
        try:
            self.assertEqual(local.count("foreign-data").count, 1)
        finally:
            local.close()

    def test_explicit_enable_prepares_local_default_then_persists_without_copying_keys(self):
        before = self.service.load_settings()
        result = self.service.set_automatic_enabled(True)
        after = self.service.load_settings()
        self.assertEqual(after.user_id, before.user_id)
        self.assertTrue(after.enabled and after.automatic and after.use_chat_model)
        self.assertEqual(after.embedding_provider, "fastembed")
        self.assertEqual(after.local_embedding_model, "BAAI/bge-small-zh-v1.5")
        self.assertEqual(result, {"prepared": True, "automatic": True})
        self.service.prepare_local_model.assert_called_once_with()
        self.assertNotIn("fixture-only-chat-key", self.service.config_path.read_text(encoding="utf-8"))
        # Only the controller may reopen the gate for a still-current request.
        self.assertTrue(self.service.automatic_paused)

    def test_download_failure_does_not_enable_collection_and_can_be_retried(self):
        self.service.prepare_local_model.side_effect = MemoryUnavailable("模型暂时无法下载。")
        with self.assertRaises(MemoryUnavailable):
            self.service.set_automatic_enabled(True)
        failed = self.service.load_settings()
        self.assertFalse(failed.enabled or failed.automatic)
        self.assertEqual(failed.embedding_provider, "fastembed")
        self.service.prepare_local_model.side_effect = None
        self.service.set_automatic_enabled(True)
        self.assertTrue(self.service.load_settings().automatic)

    def test_disable_preserves_retrieval_vector_identity_and_every_technical_option(self):
        original = replace(self.service.load_settings(), enabled=True, automatic=True,
                           embedding_base_url="http://127.0.0.1:9/v1", embedding_model="custom-remote",
                           embedding_dimensions=8, timeout_seconds=47, max_results=3)
        self.service.save_settings(original, embedding_key="fixture-only-embedding-key")
        identity = self.service.data_dir / "embedding_identity.json"
        identity.write_text(json.dumps(self.service._embedding_identity(original)), encoding="utf-8")
        identity_before = identity.read_bytes()
        self.service.set_automatic_enabled(False)
        self.assertEqual(self.service.load_settings(), replace(original, automatic=False))
        self.assertEqual(identity.read_bytes(), identity_before)
        self.service.prepare_local_model.assert_not_called()

    def test_existing_remote_model_and_credential_are_retained(self):
        original = replace(self.service.load_settings(), embedding_base_url="http://127.0.0.1:9/v1",
                           embedding_model="existing-remote", embedding_dimensions=8)
        self.service.save_settings(original, embedding_key="fixture-only-embedding-key")
        result = self.service.set_automatic_enabled(True)
        self.assertEqual(self.service.load_settings(), replace(original, enabled=True, automatic=True))
        self.assertFalse(result["prepared"])
        self.service.prepare_local_model.assert_not_called()

    def test_bound_default_remote_store_is_not_switched_even_without_credentials(self):
        original = self.service.load_settings()
        identity = self.service.data_dir / "embedding_identity.json"
        identity.write_text(json.dumps(self.service._embedding_identity(original)), encoding="utf-8")
        with self.assertRaises(MemoryUnavailable):
            self.service.set_automatic_enabled(True)
        self.assertEqual(self.service.load_settings(), original)
        self.service.prepare_local_model.assert_not_called()

    def test_non_vector_files_in_empty_legacy_store_are_not_deleted(self):
        store = self.service.data_dir / "qdrant"
        store.mkdir()
        sentinel = store / "old-data.bin"
        sentinel.write_bytes(b"existing-owned-vector-data")
        self.service.set_automatic_enabled(True)
        self.assertEqual(self.service.load_settings().embedding_provider, "fastembed")
        self.assertEqual(sentinel.read_bytes(), b"existing-owned-vector-data")

    def test_cancelled_preparation_cannot_commit_automatic_on(self):
        cancelled = threading.Event()
        self.service.prepare_local_model.side_effect = lambda: cancelled.set()
        with self.assertRaises(MemoryUnavailable):
            self.service.set_automatic_enabled(True, cancelled=cancelled)
        self.assertFalse(self.service.load_settings().automatic)
        self.assertTrue(self.service.automatic_paused)

    def test_independent_extraction_model_and_custom_local_model_are_preserved(self):
        original = replace(self.service.load_settings(), use_chat_model=False,
            llm_base_url="http://127.0.0.1:8/v1", llm_model="old-extractor", embedding_provider="fastembed",
            local_embedding_model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        self.service.save_settings(original, llm_key="fixture-only-extractor-key")
        self.service.set_automatic_enabled(True)
        self.assertEqual(self.service.load_settings(), replace(original, enabled=True, automatic=True))

    def test_missing_chat_configuration_does_not_enable_or_download(self):
        self.chat.clear_api_key()
        with self.assertRaisesRegex(MemoryUnavailable, "模型与 API"):
            self.service.set_automatic_enabled(True)
        self.assertFalse(self.service.load_settings().automatic)
        self.service.prepare_local_model.assert_not_called()

    def test_previously_accepted_disable_may_finish_after_service_shutdown(self):
        self.service.save_settings(replace(self.service.load_settings(), enabled=True, automatic=True))
        self.service.shutdown()
        self.service.set_automatic_enabled(False)
        self.assertTrue(self.service.load_settings().enabled)
        self.assertFalse(self.service.load_settings().automatic)

    def test_lazy_accepted_disable_can_finish_if_shutdown_precedes_worker_lookup(self):
        self.service.save_settings(replace(self.service.load_settings(), enabled=True, automatic=True))
        constructed = []
        lazy = LazyMemoryService(lambda: (constructed.append(True), self.service)[1])
        lazy.set_automatic_paused(True)
        lazy.shutdown()
        lazy.set_automatic_enabled(False)
        self.assertEqual(constructed, [True])
        self.assertFalse(self.service.load_settings().automatic)
        self.assertTrue(self.service.automatic_paused)


class ManagedFakeMemoryService(FakeMemoryService):
    def __init__(self):
        super().__init__()
        self.automatic_paused = False
        self.prepare_entered = threading.Event()
        self.prepare_release = threading.Event()
        self.prepare_release.set()
        self.fail_enable = False
        self.fail_disable = False

    def set_automatic_paused(self, paused):
        self.automatic_paused = paused

    def set_automatic_enabled(self, enabled, *, cancelled=None):
        self._call("set_automatic_enabled", enabled)
        if enabled:
            self.prepare_entered.set()
            if not self.prepare_release.wait(4):
                raise AssertionError("Preparation fixture was not released")
            if cancelled is not None and cancelled.is_set():
                raise MemoryUnavailable("已取消开启自动记忆。")
            if self.fail_enable:
                raise MemoryUnavailable("模型暂时无法下载，请重试。")
        elif self.fail_disable:
            raise MemoryUnavailable("无法写入记忆设置。")
        self.settings = replace(self.settings, automatic=enabled,
                                enabled=True if enabled else self.settings.enabled)
        return {"prepared": enabled, "automatic": enabled}

    def shutdown(self):
        self.prepare_release.set()
        super().shutdown()


class ManagedMemoryControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.service = ManagedFakeMemoryService()
        self.controller = MemoryController(self.service)
        spin_until(lambda: not self.controller.busy)

    def tearDown(self):
        self.service.prepare_release.set()
        self.service.release.set()
        self.controller.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.controller.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_reading_settings_never_starts_setup_and_enable_runs_in_worker(self):
        self.assertFalse(self.service.prepare_entered.is_set())
        self.assertEqual(self.controller.setup_state, "idle")
        self.assertTrue(self.controller.set_automatic_enabled(True))
        spin_until(lambda: not self.controller.busy)
        self.assertTrue(self.controller.automatic_enabled)
        self.assertEqual(self.controller.setup_state, "ready")
        self.assertTrue(all(thread != threading.get_ident() for thread in self.service.threads))

    def test_first_enable_failure_restores_saved_switch_and_explicit_retry_succeeds(self):
        self.service.fail_enable = True
        self.assertTrue(self.controller.set_automatic_enabled(True))
        spin_until(lambda: not self.controller.busy)
        self.assertFalse(self.controller.automatic_enabled)
        self.assertFalse(self.controller.settings.automatic)
        self.assertEqual(self.controller.setup_state, "failed")
        self.service.fail_enable = False
        self.assertTrue(self.controller.prepare_automatic_memory())
        spin_until(lambda: not self.controller.busy)
        self.assertTrue(self.controller.automatic_enabled)

    def test_setup_failure_remains_actionable_after_an_unrelated_list_refresh(self):
        self.service.fail_enable = True
        self.controller.set_automatic_enabled(True)
        spin_until(lambda: not self.controller.busy)
        error = self.controller.setup_error
        self.assertTrue(error)
        self.controller.refresh()
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.setup_error, error)
        self.assertEqual(self.controller.error, error)
        self.assertEqual(self.controller.setup_state, "failed")

    def test_chat_failure_is_visible_but_empty_candidates_are_not_errors(self):
        self.controller.settings = replace(self.controller.settings, enabled=True, automatic=True)
        self.controller.handle_chat_memory_status("没有需要保存的候选记忆。")
        self.assertEqual(self.controller.setup_state, "idle")
        self.controller.handle_chat_memory_status("长期记忆提炼未完成；聊天消息已经保存在本机。 模型尚未准备")
        self.assertEqual(self.controller.setup_state, "failed")
        self.assertTrue(self.controller.setup_error)
        self.assertNotIn("模型尚未准备", self.controller.error)

    def test_repair_when_automatic_is_off_keeps_collection_off_and_existing_retrieval_on(self):
        self.service.settings = replace(self.service.settings, enabled=True, automatic=False, embedding_provider="fastembed")
        self.controller.settings = self.service.settings
        self.controller._automatic_requested = False
        self.service.prepare_local_model = Mock(return_value={"ready": True})
        self.controller.handle_chat_memory_status("记忆检索失败，普通聊天仍可使用。")
        self.assertEqual(self.controller.setup_state, "failed")
        self.assertTrue(self.controller.prepare_automatic_memory())
        spin_until(lambda: not self.controller.busy)
        self.service.prepare_local_model.assert_called_once_with()
        self.assertEqual(self.controller.setup_state, "ready")
        self.assertTrue(self.service.settings.enabled)
        self.assertFalse(self.service.settings.automatic)
        self.assertFalse(self.controller.automatic_enabled)
        self.assertFalse(self.controller.error)
        self.assertFalse(any(call[0] == "set_automatic_enabled" for call in self.service.calls))

    def test_disable_while_preparing_wins_over_late_setup_and_is_persisted(self):
        self.service.prepare_release.clear()
        self.assertTrue(self.controller.set_automatic_enabled(True))
        spin_until(self.service.prepare_entered.is_set)
        self.assertTrue(self.controller.set_automatic_enabled(False))
        self.assertTrue(self.service.automatic_paused)
        self.service.prepare_release.set()
        spin_until(lambda: not self.controller.busy)
        self.assertFalse(self.service.settings.automatic)
        self.assertFalse(self.controller.automatic_enabled)
        self.assertEqual(self.controller.setup_state, "idle")
        self.assertFalse(self.controller.prepare_automatic_memory())

    def test_disable_while_listing_closes_gate_immediately_then_saves(self):
        self.service.settings = replace(self.service.settings, enabled=True, automatic=True)
        self.controller.settings = self.service.settings
        self.service.block = True
        self.service.release.clear()
        self.assertTrue(self.controller.refresh())
        spin_until(self.service.entered.is_set)
        self.assertTrue(self.controller.set_automatic_enabled(False))
        self.assertTrue(self.service.automatic_paused)
        self.assertFalse(self.controller.automatic_enabled)
        self.service.release.set()
        spin_until(lambda: not self.controller.busy)
        self.assertTrue(self.service.settings.enabled)
        self.assertFalse(self.service.settings.automatic)

    def test_failed_disable_keeps_runtime_stopped_and_explains_restart_risk(self):
        self.service.settings = replace(self.service.settings, enabled=True, automatic=True)
        self.controller.settings = self.service.settings
        self.service.fail_disable = True
        self.assertTrue(self.controller.set_automatic_enabled(False))
        spin_until(lambda: not self.controller.busy)
        self.assertTrue(self.controller.settings.automatic)
        self.assertFalse(self.controller.automatic_enabled)
        self.assertTrue(self.service.automatic_paused)
        self.assertIn("重启后可能恢复", self.controller.error)

    def test_accepted_disable_is_saved_after_shutdown_without_a_gui_completion_callback(self):
        self.service.settings = replace(self.service.settings, enabled=True, automatic=True)
        self.controller.settings = self.service.settings
        self.service.block = True
        self.service.release.clear()
        write_started, allow_write = threading.Event(), threading.Event()
        original = self.service.set_automatic_enabled
        def delayed_write(enabled, **kwargs):
            if not enabled:
                write_started.set()
                if not allow_write.wait(3):
                    raise AssertionError("The accepted disk write was not released")
                self.assertTrue(self.service.closed)
            return original(enabled, **kwargs)
        self.service.set_automatic_enabled = delayed_write
        self.assertTrue(self.controller.refresh())
        spin_until(self.service.entered.is_set)
        self.assertTrue(self.controller.set_automatic_enabled(False))
        spin_until(write_started.is_set)
        self.controller.shutdown()
        allow_write.set()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.assertFalse(self.service.settings.automatic)
        self.assertTrue(self.service.settings.enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
