"""Real SDK management must never embed shared entities or call an LLM.

All records are synthetic, Qdrant lives in a temporary directory, and both
provider URLs point to the loopback recorder. No model downloads are allowed.
"""

from dataclasses import asdict, replace
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QSettings

from desktop_pet.memory import MemoryService, MemoryUnavailable
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsError, SettingsManager
from tests.test_memory_service import LocalMemoryModels, Vault


@unittest.skipUnless(importlib.util.find_spec("mem0"), "Optional Mem0 SDK is not installed")
class OfflineMemoryManagementTests(unittest.TestCase):
    def make_service(self, root, provider, mode):
        vault = Vault()
        settings = SettingsManager(
            QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat),
            CredentialStore(backend=vault),
        )
        settings.save_api_settings(ApiSettings(provider.base_url, "fixture-chat"))
        settings.save_api_key("fixture-only-chat-key")
        service = MemoryService(
            settings, root / "memory",
            llm_credentials=CredentialStore(account="mem0-llm-key", backend=vault),
            embedding_credentials=CredentialStore(account="mem0-embedding-key", backend=vault),
        )
        self.addCleanup(service.shutdown)
        service.save_settings(replace(
            service.load_settings(), enabled=False, embedding_provider=mode,
            embedding_base_url=provider.base_url, embedding_model="fixture-embedding", embedding_dimensions=8,
            local_embedding_cache_dir=str(root / "not-downloaded"),
        ))
        return service

    def seed_shared_entity(self, service, *, include_foreign=True):
        from qdrant_client import QdrantClient, models
        dimensions = service.load_settings().effective_embedding_dimensions
        own = service.load_settings().user_id
        foreign = str(uuid4())
        first, second, outsider, entity, foreign_entity = [str(uuid4()) for _ in range(5)]
        vector = [0.0] * dimensions
        vector[3] = 1.0
        with closing(QdrantClient(path=str(service.data_dir / "qdrant"))) as client:
            for name in ("aura_memories", "aura_memories_entities"):
                client.create_collection(name, vectors_config=models.VectorParams(size=dimensions, distance=models.Distance.COSINE))
            memories = [
                models.PointStruct(id=first, vector=vector, payload={"data": "测试用户的第一个长期偏好", "user_id": own}),
                models.PointStruct(id=second, vector=vector, payload={"data": "测试用户的另一个长期偏好", "user_id": own}),
            ]
            entities = [
                models.PointStruct(id=entity, vector=vector, payload={
                    "data": "合成共享项目实体", "user_id": own, "linked_memory_ids": [first, second],
                    "entity_type": "project", "untouched": "preserve this metadata",
                }),
            ]
            if include_foreign:
                memories.append(models.PointStruct(id=outsider, vector=vector, payload={"data": "另一个测试用户的偏好", "user_id": foreign}))
                entities.append(models.PointStruct(id=foreign_entity, vector=vector, payload={
                    "data": "另一个用户的合成实体", "user_id": foreign, "linked_memory_ids": [first, outsider],
                }))
            client.upsert("aura_memories", points=memories)
            client.upsert("aura_memories_entities", points=entities)
        from mem0.memory.storage import SQLiteManager
        with closing(SQLiteManager(str(service.data_dir / "mem0_history.sqlite3"))) as history:
            history.add_history(first, None, "当前用户合成事实一", "ADD")
            history.add_history(second, None, "当前用户合成事实二", "ADD")
            history.save_messages([{"role": "user", "content": "当前用户合成候选"}], "user_id=" + own)
            if include_foreign:
                history.add_history(outsider, None, "其它用户合成事实", "ADD")
                history.save_messages([{"role": "user", "content": "其它用户合成候选"}], "agent_id=fixture&user_id=" + foreign)
        return {"first": first, "second": second, "outsider": outsider, "entity": entity,
                "foreign_entity": foreign_entity, "foreign_user": foreign, "vector": vector}

    def test_delete_shared_entities_is_offline_and_preserves_vectors_and_other_users(self):
        from qdrant_client import QdrantClient
        for mode in ("openai", "fastembed"):
            with self.subTest(provider=mode), tempfile.TemporaryDirectory(prefix="aura-offline-delete-") as directory, LocalMemoryModels() as provider:
                service = self.make_service(Path(directory), provider, mode)
                seeded = self.seed_shared_entity(service)
                service.delete(seeded["first"])
                self.assertEqual(provider.requests, [], "Local memory deletion must not send entity text to a provider")
                with closing(QdrantClient(path=str(service.data_dir / "qdrant"))) as client:
                    self.assertEqual(client.retrieve("aura_memories", [seeded["first"]]), [])
                    self.assertTrue(client.retrieve("aura_memories", [seeded["second"]]))
                    self.assertTrue(client.retrieve("aura_memories", [seeded["outsider"]]))
                    entity = client.retrieve("aura_memories_entities", [seeded["entity"]], with_vectors=True)[0]
                    self.assertEqual(entity.payload["linked_memory_ids"], [seeded["second"]])
                    self.assertEqual(entity.payload["untouched"], "preserve this metadata")
                    self.assertEqual(entity.vector, seeded["vector"])
                    outsider = client.retrieve("aura_memories_entities", [seeded["foreign_entity"]])[0]
                    self.assertEqual(outsider.payload["linked_memory_ids"], [seeded["first"], seeded["outsider"]])
                service.delete(seeded["second"])
                with closing(QdrantClient(path=str(service.data_dir / "qdrant"))) as client:
                    self.assertEqual(client.retrieve("aura_memories_entities", [seeded["entity"]]), [])
                    self.assertTrue(client.retrieve("aura_memories", [seeded["outsider"]]))
                self.assertEqual(provider.requests, [])
                self.assertFalse((Path(directory) / "not-downloaded").exists())

    def test_clear_uses_current_user_namespace_and_keeps_other_identity_and_history(self):
        from qdrant_client import QdrantClient
        import sqlite3
        for mode in ("openai", "fastembed"):
            with self.subTest(provider=mode), tempfile.TemporaryDirectory(prefix="aura-offline-clear-") as directory, LocalMemoryModels() as provider:
                service = self.make_service(Path(directory), provider, mode)
                seeded = self.seed_shared_entity(service)
                marker = service.data_dir / "embedding_identity.json"
                identity = json.dumps(service._embedding_identity(service.load_settings()))
                marker.write_text(identity, encoding="utf-8")
                own_rows = service.list_memories()
                self.assertEqual({row["id"] for row in own_rows}, {seeded["first"], seeded["second"]})
                with self.assertRaises(MemoryUnavailable):
                    service.delete(seeded["outsider"])
                result = service.clear()
                self.assertTrue(result["remaining_other_users"])
                self.assertEqual(provider.requests, [])
                self.assertEqual(marker.read_text(encoding="utf-8"), identity)
                with closing(QdrantClient(path=str(service.data_dir / "qdrant"))) as client:
                    self.assertEqual(client.retrieve("aura_memories", [seeded["first"], seeded["second"]]), [])
                    self.assertEqual(client.retrieve("aura_memories_entities", [seeded["entity"]]), [])
                    foreign = client.retrieve("aura_memories", [seeded["outsider"]], with_vectors=True)[0]
                    self.assertEqual(foreign.vector, seeded["vector"])
                    self.assertTrue(client.retrieve("aura_memories_entities", [seeded["foreign_entity"]]))
                with closing(sqlite3.connect(service.data_dir / "mem0_history.sqlite3")) as history:
                    rows = history.execute("SELECT memory_id FROM history").fetchall()
                    self.assertEqual(rows, [(seeded["outsider"],)])
                    scopes = history.execute("SELECT session_scope FROM messages").fetchall()
                    self.assertEqual(scopes, [("agent_id=fixture&user_id=" + seeded["foreign_user"],)])
                with self.assertRaises(SettingsError):
                    service.save_settings(replace(service.load_settings(), embedding_dimensions=16,
                                                  embedding_provider="fastembed" if mode == "openai" else "openai"))

    def test_management_ignores_corrupt_identity_and_uses_existing_collection_dimensions(self):
        from qdrant_client import QdrantClient
        for mode in ("openai", "fastembed"):
            with self.subTest(provider=mode), tempfile.TemporaryDirectory(prefix="aura-corrupt-identity-") as directory, LocalMemoryModels() as provider:
                service = self.make_service(Path(directory), provider, mode)
                seeded = self.seed_shared_entity(service)
                marker = service.data_dir / "embedding_identity.json"
                marker.write_text("{broken embedding marker", encoding="utf-8")
                altered = replace(service.load_settings(), embedding_provider="openai", embedding_dimensions=16)
                service.config_path.write_text(json.dumps(asdict(altered)), encoding="utf-8")
                self.assertEqual(len(service.list_memories()), 2)
                service.delete(seeded["first"])
                result = service.clear()
                self.assertTrue(result["remaining_other_users"])
                self.assertEqual(marker.read_text(encoding="utf-8"), "{broken embedding marker")
                self.assertEqual(provider.requests, [])
                with closing(QdrantClient(path=str(service.data_dir / "qdrant"))) as client:
                    self.assertTrue(client.retrieve("aura_memories", [seeded["outsider"]]))
                    self.assertEqual(client.get_collection("aura_memories").config.params.vectors.size, len(seeded["vector"]))

    def test_single_user_clear_removes_corrupt_identity_and_allows_provider_change(self):
        from qdrant_client import QdrantClient
        import sqlite3
        with tempfile.TemporaryDirectory(prefix="aura-clear-last-user-") as directory, LocalMemoryModels() as provider:
            service = self.make_service(Path(directory), provider, "openai")
            self.seed_shared_entity(service, include_foreign=False)
            marker = service.data_dir / "embedding_identity.json"
            marker.write_text("{damaged", encoding="utf-8")
            result = service.clear()
            self.assertFalse(result["remaining_other_users"])
            self.assertFalse(marker.exists())
            with closing(QdrantClient(path=str(service.data_dir / "qdrant"))) as client:
                self.assertTrue(all(client.count(item.name).count == 0 for item in client.get_collections().collections))
            with closing(sqlite3.connect(service.data_dir / "mem0_history.sqlite3")) as history:
                self.assertEqual(history.execute("SELECT count(*) FROM history").fetchone()[0], 0)
                self.assertEqual(history.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            service.save_settings(replace(service.load_settings(), embedding_provider="fastembed"))
            self.assertEqual(service.load_settings().embedding_provider, "fastembed")
            self.assertEqual(provider.requests, [])

    def test_unknown_identity_with_other_users_becomes_protective_without_guessing_model(self):
        with tempfile.TemporaryDirectory(prefix="aura-clear-unknown-identity-") as directory, LocalMemoryModels() as provider:
            service = self.make_service(Path(directory), provider, "openai")
            self.seed_shared_entity(service)
            result = service.clear()
            self.assertTrue(result["remaining_other_users"])
            marker = json.loads((service.data_dir / "embedding_identity.json").read_text(encoding="utf-8"))
            self.assertEqual(marker, {"unverified_existing_store": True})
            with self.assertRaises(SettingsError):
                service.save_settings(replace(service.load_settings(), embedding_provider="fastembed"))

    def test_clear_keeps_other_user_candidates_even_when_their_vector_namespace_is_empty(self):
        from mem0.memory.storage import SQLiteManager
        import sqlite3
        with tempfile.TemporaryDirectory(prefix="aura-clear-foreign-candidates-") as directory, LocalMemoryModels() as provider:
            service = self.make_service(Path(directory), provider, "openai")
            self.seed_shared_entity(service, include_foreign=False)
            scope = "user_id=" + str(uuid4())
            with closing(SQLiteManager(str(service.data_dir / "mem0_history.sqlite3"))) as history:
                history.save_messages([{"role": "user", "content": "其它测试用户尚未提炼的候选"}], scope)
            result = service.clear()
            self.assertTrue(result["remaining_other_users"])
            with closing(sqlite3.connect(service.data_dir / "mem0_history.sqlite3")) as history:
                self.assertEqual(history.execute("SELECT session_scope FROM messages").fetchall(), [(scope,)])
                self.assertEqual(history.execute("SELECT count(*) FROM history").fetchone()[0], 0)
            self.assertEqual(provider.requests, [])

    def test_clear_preserves_unrelated_collection_and_its_unattributed_history(self):
        from qdrant_client import QdrantClient, models
        from mem0.memory.storage import SQLiteManager
        import sqlite3
        with tempfile.TemporaryDirectory(prefix="aura-clear-unrelated-store-") as directory, LocalMemoryModels() as provider:
            service = self.make_service(Path(directory), provider, "openai")
            self.seed_shared_entity(service, include_foreign=False)
            unknown_id = str(uuid4())
            with closing(QdrantClient(path=str(service.data_dir / "qdrant"))) as client:
                client.create_collection("unrelated_legacy_collection", vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE))
                client.upsert("unrelated_legacy_collection", [models.PointStruct(id=unknown_id, vector=[1.0, 0.0, 0.0, 0.0], payload={"data": "unrelated synthetic fact"})])
            with closing(SQLiteManager(str(service.data_dir / "mem0_history.sqlite3"))) as history:
                history.add_history(unknown_id, None, "无法归属的合成历史", "ADD")
            result = service.clear()
            self.assertTrue(result["remaining_other_users"])
            with closing(QdrantClient(path=str(service.data_dir / "qdrant"))) as client:
                self.assertEqual(client.count("unrelated_legacy_collection").count, 1)
            with closing(sqlite3.connect(service.data_dir / "mem0_history.sqlite3")) as history:
                self.assertEqual(history.execute("SELECT memory_id FROM history").fetchall(), [(unknown_id,)])
            self.assertEqual(provider.requests, [])


class OfflineProviderBoundaryTests(unittest.TestCase):
    def test_offline_transport_blocks_real_openai_embedding_and_chat_requests(self):
        from openai import APIConnectionError, OpenAI
        from desktop_pet.memory.worker import _offline_http_client
        with LocalMemoryModels() as provider, OpenAI(api_key="fixture-only-no-network", base_url=provider.base_url,
                                                   max_retries=0, http_client=_offline_http_client()) as client:
            with self.assertRaises(APIConnectionError):
                client.embeddings.create(model="fixture", input="must remain inside this test")
            with self.assertRaises(APIConnectionError):
                client.chat.completions.create(model="fixture", messages=[{"role": "user", "content": "not sent"}])
            self.assertEqual(provider.requests, [])

    @unittest.skipUnless(importlib.util.find_spec("mem0"), "Optional Mem0 SDK is not installed")
    def test_optional_spacy_without_model_uses_sdk_fallback_without_download(self):
        from desktop_pet.memory.worker import _disable_implicit_nlp_downloads
        from mem0.utils import spacy_models
        fake_spacy = ModuleType("spacy")
        fake_spacy.util = SimpleNamespace(is_package=lambda name: False)
        fake_spacy.load = Mock(side_effect=AssertionError("No installed model should be loaded"))
        fake_cli = ModuleType("spacy.cli")
        fake_cli.download = Mock(side_effect=AssertionError("No implicit model downloads"))
        original = spacy_models._ensure_model_available
        with patch.dict("sys.modules", {"spacy": fake_spacy, "spacy.cli": fake_cli}), patch.multiple(
            spacy_models, _nlp_full=None, _nlp_lemma=None, _load_failed_full=False, _load_failed_lemma=False,
        ):
            try:
                _disable_implicit_nlp_downloads()
                self.assertIsNone(spacy_models.get_nlp_full())
                self.assertIsNone(spacy_models.get_nlp_lemma())
                fake_cli.download.assert_not_called()
                fake_spacy.load.assert_not_called()
            finally:
                spacy_models._ensure_model_available = original


if __name__ == "__main__":
    unittest.main()
