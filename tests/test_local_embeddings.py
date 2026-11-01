"""Local-mode contracts, safe configuration and offline SDK management.

No public model download is allowed here. Real ONNX inference has a separate
explicit smoke command in tests.local_embedding_smoke.
"""

from dataclasses import asdict, replace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings

from desktop_pet.memory import MemoryService, MemoryUnavailable
from desktop_pet.memory.settings import LOCAL_EMBEDDING_MODELS
from desktop_pet.settings_manager import CredentialStore, CredentialsError, SettingsError, SettingsManager
from tests.test_memory_service import SimulatedMemoryHelper, Vault


class LocalEmbeddingSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-local-embedding-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = Vault()
        self.settings = SettingsManager(QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat),
                                        CredentialStore(backend=self.vault))
        self.embedding_credentials = CredentialStore(account="mem0-embedding-key", backend=self.vault)
        self.service = MemoryService(self.settings, self.root / "memory",
                                     llm_credentials=CredentialStore(account="mem0-llm-key", backend=self.vault),
                                     embedding_credentials=self.embedding_credentials)
        self.addCleanup(self.service.shutdown)

    def local_settings(self, **values):
        return replace(self.service.load_settings(), embedding_provider="fastembed",
                       local_embedding_cache_dir=str(self.root / "models"), **values)

    def test_legacy_settings_and_remote_identity_reopen_without_rewriting(self):
        original = self.service.load_settings()
        raw = asdict(original)
        for name in ("embedding_provider", "local_embedding_model", "local_embedding_cache_dir", "local_embedding_threads"):
            raw.pop(name)
        self.service.config_path.write_text(json.dumps(raw), encoding="utf-8")
        restored = self.service.load_settings()
        self.assertEqual(restored.embedding_provider, "openai")
        self.assertEqual(restored.effective_embedding_dimensions, 1536)
        self.assertEqual(self.service._embedding_identity(restored), {
            "base_url": original.embedding_base_url, "model": original.embedding_model, "dimensions": 1536,
        })
        self.assertNotIn("embedding_provider", json.loads(self.service.config_path.read_text(encoding="utf-8")))

    def test_local_dimensions_are_catalog_values_and_do_not_change_remote_preferences(self):
        for model, size in LOCAL_EMBEDDING_MODELS.items():
            with self.subTest(model=model):
                settings = self.local_settings(local_embedding_model=model)
                settings.validate()
                self.assertEqual(settings.effective_embedding_dimensions, size)
                self.assertEqual(settings.embedding_dimensions, 1536)
                self.assertEqual(replace(settings, embedding_provider="openai").effective_embedding_dimensions, 1536)

    def test_rejects_unknown_provider_model_threads_and_relative_cache(self):
        for field, value in (("embedding_provider", "arbitrary"), ("local_embedding_model", "untrusted/model-id"),
                             ("local_embedding_threads", True), ("local_embedding_threads", 0),
                             ("local_embedding_threads", 9), ("local_embedding_cache_dir", "relative/models"),
                             ("local_embedding_cache_dir", "bad\x00path")):
            with self.subTest(field=field, value=value), self.assertRaises(SettingsError):
                replace(self.local_settings(), **{field: value}).validate()

    def test_local_configuration_and_status_do_not_access_embedding_credentials(self):
        settings = self.local_settings(enabled=True)
        self.service.save_settings(settings)
        with patch.object(self.embedding_credentials, "get_api_key", side_effect=CredentialsError("unavailable")) as getter:
            config, secrets = self.service._configuration(settings, needs_llm=False, needs_embedding=True)
            status = self.service.credential_status()
        getter.assert_not_called()
        self.assertIn("无需 API Key", status)
        self.assertEqual(config["embedder"]["provider"], "fastembed")
        self.assertEqual(config["embedder"]["config"]["model_kwargs"]["threads"], 2)
        self.assertNotIn("api_key", config["embedder"]["config"])
        self.assertEqual(config["vector_store"]["config"]["embedding_model_dims"], 512)
        self.assertEqual(secrets, ())

    def test_local_management_does_not_require_model_or_embedding_credentials(self):
        settings = self.local_settings(enabled=False)
        self.service.save_settings(settings)
        config, _ = self.service._configuration(settings, needs_llm=False, needs_embedding=False)
        self.assertEqual(config["embedder"]["provider"], "openai")
        self.assertEqual(config["vector_store"]["config"]["embedding_model_dims"], 512)
        if importlib.util.find_spec("mem0") is not None:
            self.assertEqual(self.service.list_memories(), [])
        self.assertFalse((self.root / "models").exists())

    def test_switching_vector_provider_requires_clear_but_cache_and_threads_do_not(self):
        original = self.service.load_settings()
        path = self.service.data_dir / "embedding_identity.json"
        path.write_text(json.dumps(self.service._embedding_identity(original)), encoding="utf-8")
        with self.assertRaises(SettingsError):
            self.service.save_settings(self.local_settings())
        path.write_text(json.dumps(self.service._embedding_identity(self.local_settings())), encoding="utf-8")
        self.service.save_settings(self.local_settings(local_embedding_threads=1))
        self.assertEqual(self.service.load_settings().local_embedding_threads, 1)
        with self.assertRaises(SettingsError):
            self.service.save_settings(self.local_settings(local_embedding_model="BAAI/bge-small-en-v1.5"))

    def test_explicit_download_is_independent_of_enabled_switch_and_never_receives_keys(self):
        self.service.save_settings(self.local_settings(enabled=False))
        self.settings.save_api_key("fixture-only-chat-secret")
        helper = SimulatedMemoryHelper()
        original_start = __import__("subprocess").Popen
        def launch(command, *args, **kwargs):
            if isinstance(command, list) and "desktop_pet.memory.worker" in command:
                return helper
            return original_start(command, *args, **kwargs)
        with patch("desktop_pet.memory.service.subprocess.Popen", side_effect=launch):
            self.service.prepare_local_model()
        self.assertLessEqual(helper.timeout, 120)
        self.assertGreater(helper.timeout, 110)
        self.assertEqual(helper.payload["operation"], "prepare_model")
        self.assertEqual(set(helper.payload["config"]), {"embedder"})
        self.assertNotIn("fixture-only-chat-secret", json.dumps(helper.payload))
        self.assertNotIn("qdrant", json.dumps(helper.payload["config"]))
        self.assertFalse(self.service.load_settings().enabled)

    def test_download_rejects_remote_mode_before_requiring_remote_key(self):
        with self.assertRaisesRegex(MemoryUnavailable, "选择并保存本地"):
            self.service.prepare_local_model()

    def test_search_helper_enforces_offline_environment_without_embedding_credentials(self):
        self.service.save_settings(self.local_settings(enabled=True))
        helper = SimulatedMemoryHelper()
        launched = {}
        original_start = __import__("subprocess").Popen
        def launch(command, *args, **kwargs):
            if isinstance(command, list) and "desktop_pet.memory.worker" in command:
                launched.update(kwargs)
                return helper
            return original_start(command, *args, **kwargs)
        with patch("desktop_pet.memory.service.subprocess.Popen", side_effect=launch):
            self.assertEqual(self.service.search("桌宠字体偏好"), [])
        self.assertEqual(launched["env"]["HF_HUB_OFFLINE"], "1")
        self.assertEqual(launched["env"]["HF_HUB_DISABLE_IMPLICIT_TOKEN"], "1")
        self.assertNotIn("OPENAI_API_KEY", launched["env"])
        self.assertNotIn("HF_TOKEN", launched["env"])
        self.assertEqual(helper.payload["config"]["embedder"]["provider"], "fastembed")
        self.assertNotIn("api_key", helper.payload["config"]["embedder"]["config"])

    def test_missing_local_cache_gives_safe_error_without_implicit_download(self):
        self.service.save_settings(self.local_settings(enabled=True, timeout_seconds=10))
        with self.assertRaises(MemoryUnavailable) as caught:
            self.service.search("桌宠字体偏好")
        if importlib.util.find_spec("fastembed") is None:
            self.assertIn("requirements-memory-local", str(caught.exception))
        else:
            self.assertIn("模型尚未准备好", str(caught.exception))
        self.assertFalse(any((self.root / "models").rglob("*.onnx")))


class LocalEmbeddingProviderTests(unittest.TestCase):
    def test_model_catalog_matches_installed_fastembed_without_loading_weights(self):
        if importlib.util.find_spec("fastembed") is None:
            # The optional package is not necessary to validate the catalog's
            # own contract; actual provider smoke requires explicit install.
            self.assertEqual(LOCAL_EMBEDDING_MODELS["BAAI/bge-small-zh-v1.5"], 512)
            return
        from fastembed import TextEmbedding
        actual = {row["model"]: row["dim"] for row in TextEmbedding.list_supported_models()}
        for model, size in LOCAL_EMBEDDING_MODELS.items():
            self.assertEqual(actual[model], size)

    def test_adapter_forces_cpu_offline_and_passes_cache_and_thread_count(self):
        if importlib.util.find_spec("fastembed") is None:
            self.assertFalse(importlib.util.find_spec("fastembed"))
            return
        from mem0.configs.embeddings.base import BaseEmbedderConfig
        from desktop_pet.memory import local_embedder
        with patch.object(local_embedder, "TextEmbedding") as embedding:
            embedding.get_embedding_size.return_value = 512
            config = BaseEmbedderConfig(model="BAAI/bge-small-zh-v1.5", embedding_dims=512,
                                        model_kwargs={"cache_dir": "test-cache", "threads": 2})
            local_embedder.LocalFastEmbedEmbedding(config)
        embedding.assert_called_once_with(model_name="BAAI/bge-small-zh-v1.5", cache_dir="test-cache", threads=2,
                                          providers=["CPUExecutionProvider"], local_files_only=True)


if __name__ == "__main__":
    unittest.main()
