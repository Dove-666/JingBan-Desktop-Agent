"""Real Mem0/Qdrant regressions; only model HTTP is a loopback fixture."""

from contextlib import closing
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from PySide6.QtCore import QSettings

from desktop_pet.conversations.context import ContextAssembler
from desktop_pet.memory import MemoryService, MemoryUnavailable
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
from tests.test_memory_service import LocalMemoryModels, Vault


@unittest.skipUnless(importlib.util.find_spec("mem0"), "Optional Mem0 SDK is not installed")
class MemoryCorrectionAndPagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-correction-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.provider = LocalMemoryModels()
        self.provider.__enter__()
        self.addCleanup(self.provider.__exit__, None, None, None)
        vault = Vault()
        chat = SettingsManager(QSettings(str(self.root / "chat.ini"), QSettings.Format.IniFormat),
                               CredentialStore(backend=vault))
        chat.save_api_settings(ApiSettings(self.provider.base_url, "fixture-chat"))
        chat.save_api_key("fixture-chat-key")
        self.service = MemoryService(chat, self.root / "memory",
            llm_credentials=CredentialStore(account="fixture-llm", backend=vault),
            embedding_credentials=CredentialStore(account="fixture-embedding", backend=vault))
        self.addCleanup(self.service.shutdown)
        self.service.save_settings(replace(self.service.load_settings(), enabled=True, automatic=True,
            embedding_base_url=self.provider.base_url, embedding_model="fixture-embedding", embedding_dimensions=8),
            embedding_key="fixture-embedding-key")
        self.user_id = self.service.load_settings().user_id

    def seed(self, records):
        from qdrant_client import QdrantClient, models
        with closing(QdrantClient(path=str(self.service.data_dir / "qdrant"))) as client:
            client.create_collection("aura_memories", vectors_config=models.VectorParams(size=8, distance=models.Distance.COSINE))
            client.upsert("aura_memories", points=[models.PointStruct(id=identifier, vector=[1.0] + [0.0] * 7,
                payload={"data": text, "user_id": owner, **metadata})
                for identifier, text, owner, metadata in records])

    def test_literal_filter_scans_all_namespaces_and_can_delete_record_after_first_thousand(self):
        ids = [str(uuid4()) for _ in range(1107)]
        foreign = str(uuid4())
        self.seed([(identifier, f"合成事实-{index:04d}", self.user_id,
                    {"agent_id": "agent-a" if index % 2 else "agent-b", "run_id": f"run-{index % 3}"})
                   for index, identifier in enumerate(ids)] + [(foreign, "其他用户私密合成事实", str(uuid4()), {})])
        page = self.service.list_memories_page()
        self.assertEqual((page["total"], page["matched_total"], page["loaded"]), (1107, 1107, 100))
        seen = set()
        for offset in range(0, 1107, 200):
            batch = self.service.list_memories_page(offset=offset, page_size=200)
            seen.update(row["id"] for row in batch["results"])
        self.assertEqual(seen, set(ids))
        beyond = ids[1106]
        exact = self.service.list_memories_page("合成事实-1106")
        self.assertEqual([row["id"] for row in exact["results"]], [beyond])
        self.assertEqual(exact["matched_total"], 1)
        self.assertEqual(len(self.service.list_memories()), 1107)
        self.service.delete(beyond)
        self.assertEqual(self.service.list_memories("合成事实-1106"), [])
        self.assertEqual(self.service.list_memories_page()["total"], 1106)
        with self.assertRaises(MemoryUnavailable):
            self.service.delete(foreign)
        self.assertEqual(self.provider.requests, [])

    def test_manual_correction_uses_sdk_update_retains_history_and_protects_other_users(self):
        identifier, outsider = str(uuid4()), str(uuid4())
        old = "用户长期偏好大字体评论气泡。"
        new = "用户现在偏好小字体评论气泡。"
        self.seed([(identifier, old, self.user_id, {"created_at": "2026-01-01T00:00:00+00:00"}),
                   (outsider, "另一个用户偏好大字体", str(uuid4()), {})])
        with self.assertRaises(MemoryUnavailable):
            self.service.update(outsider, new)
        self.assertEqual(self.provider.requests, [])
        updated = self.service.update(identifier, new, expected_text=old)
        self.assertEqual(updated["id"], identifier)
        self.assertEqual(updated["metadata"]["revision"], 1)
        self.assertEqual(updated["metadata"]["status"], "active")
        self.assertEqual(updated["created_at"], "2026-01-01T00:00:00+00:00")
        with self.assertRaises(MemoryUnavailable):
            self.service.update(identifier, "不应覆盖新事实", expected_text=old)
        self.assertEqual(self.service.list_memories()[0]["memory"], new)
        with closing(sqlite3.connect(self.service.data_dir / "mem0_history.sqlite3")) as history:
            events = history.execute("SELECT old_memory, new_memory, event FROM history WHERE memory_id=?", (identifier,)).fetchall()
        self.assertEqual(events, [(old, new, "UPDATE")])
        self.assertTrue(self.provider.requests)
        self.assertTrue(all(call["path"].endswith("/embeddings") for call in self.provider.requests))
        self.provider.requests.clear()
        audited = self.service.history(identifier)
        self.assertEqual(audited[0]["old_memory"], old)
        self.assertEqual(audited[0]["new_memory"], new)
        self.assertTrue(audited[0]["updated_at"])
        with self.assertRaises(MemoryUnavailable):
            self.service.history(outsider)
        self.assertEqual(self.provider.requests, [], "History must remain local and protect other users")

    def test_explicit_preference_correction_replaces_only_confirmed_id_and_new_context_has_time(self):
        old = self.service.extract([{"role": "user", "content": "记住，我长期偏好大字体评论气泡。"}], conversation_id=str(uuid4()))[0]
        source = "更新我的偏好：以后使用小字体评论气泡，不再使用大字体。"
        new = "用户现在偏好小字体评论气泡。"
        def correction(body):
            content = json.loads(body["messages"][1]["content"])
            target = next(item for item in content["memories"] if item["id"] == old["id"])
            return {"updates": [{"memory_id": target["id"], "old_text": target["memory"], "old_quote": "大字体",
                                 "evidence": source, "text": new, "confirmed": True}]}
        self.provider.correction_response = correction
        changed = self.service.extract([{"role": "user", "content": source}], conversation_id=str(uuid4()), trigger="automatic")
        self.assertEqual(changed[0]["id"], old["id"])
        rows = self.service.list_memories()
        self.assertEqual(len(rows), 1)
        relevant = self.service.search("字体偏好")
        self.assertEqual([row["memory"] for row in relevant], [new])
        context = ContextAssembler().assemble(system_prompt="test", user_text="我的字体偏好", recent_messages=[], memories=relevant)
        encoded = str(context.messages)
        self.assertIn(new, encoded)
        self.assertNotIn(old["memory"], encoded)
        self.assertIn("updated_at", encoded)
        self.assertIn("effective_at", encoded)

    def test_ambiguous_or_forged_model_correction_never_overwrites_stored_fact(self):
        own, foreign = str(uuid4()), str(uuid4())
        old = "用户长期偏好大字体评论气泡。"
        self.seed([(own, old, self.user_id, {}), (foreign, old, str(uuid4()), {})])
        source = "更新我的偏好：以后使用小字体评论气泡，不再使用大字体。"
        for target, quote, evidence in ((foreign, "大字体", source), (own, "大字体", "模型编造了用户改口"),
                                         (own, "用户偏好", source)):
            with self.subTest(target=target, quote=quote):
                self.provider.correction_response = {"updates": [{"memory_id": target, "old_text": old,
                    "old_quote": quote, "evidence": evidence, "text": "被错误改写", "confirmed": True}]}
                with self.assertRaisesRegex(MemoryUnavailable, "无法可靠确认"):
                    self.service.extract([{"role": "user", "content": source}], conversation_id=str(uuid4()))
                self.assertEqual(self.service.list_memories()[0]["memory"], old)
        self.provider.fail = True
        with self.assertRaises(MemoryUnavailable):
            self.service.update(own, "用户偏好小字体评论气泡。", expected_text=old)
        self.provider.fail = False
        self.assertEqual(self.service.list_memories()[0]["memory"], old)

    def test_edit_preserves_expiration_and_explicit_inactive_status(self):
        from desktop_pet.conversations.context import memory_is_active
        expired, inactive = str(uuid4()), str(uuid4())
        self.seed([(expired, "用户偏好大字体气泡。", self.user_id, {"expiration_date": "2000-01-01"}),
                   (inactive, "用户偏好大字体说明。", self.user_id, {"status": "inactive"})])
        for identifier in (expired, inactive):
            updated = self.service.update(identifier, "用户现在偏好小字体。")
            self.assertFalse(memory_is_active(updated))
            if identifier == expired:
                self.assertEqual(updated["expiration_date"], "2000-01-01")
            else:
                self.assertEqual(updated["metadata"]["status"], "inactive")
        self.assertEqual(self.service.search("字体偏好"), [])

    def test_configuration_status_reads_only_local_configuration_and_keys(self):
        status = self.service.configuration_status()
        self.assertTrue(status["chat_configured"])
        self.assertTrue(status["embedding_configured"])
        self.assertTrue(status["extraction_configured"])
        self.assertEqual(self.provider.requests, [])
        self.service.clear_embedding_key()
        self.assertFalse(self.service.configuration_status()["embedding_configured"])
        self.assertEqual(self.provider.requests, [])


if __name__ == "__main__":
    unittest.main()
