"""Real SQLite/Qt RPC tests for private configuration and AI-free imports."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiClient, SYSTEM_PROMPT
from desktop_pet.remote.sync import RemoteSyncStore
from desktop_pet.settings_manager import ApiSettings
from tests import test_remote_controller as support
from tests.mock_api import MockApiServer


def exchange(cid=None, *, image_id=None):
    timestamp = datetime.now(timezone.utc)
    user = {"id": str(uuid4()), "role": "user", "content": "phone direct question",
            "created_at": timestamp.isoformat()}
    if image_id is not None:
        user["image_id"] = image_id
    return {"conversation_id": cid or str(uuid4()), "title": "Phone direct conversation",
            "messages": [user, {"id": str(uuid4()), "role": "assistant", "content": "phone direct answer",
                "created_at": (timestamp + timedelta(seconds=1)).isoformat(), "reply_to": user["id"]}]}


class DirectChatRpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    setUp = support.RemoteControllerTests.setUp
    tearDown = support.RemoteControllerTests.tearDown
    start = support.RemoteControllerTests.start
    pair = support.RemoteControllerTests.pair
    request = support.RemoteControllerTests.request

    def enable(self):
        self.start()
        self.pair()

    def configure(self, base="https://api.example.test/v1", key="PRIVATE_DIRECT_CONFIG_KEY"):
        self.settings.save_api_settings(ApiSettings(base, "configured-model", 41, "high"))
        self.settings.save_api_key(key)
        return key

    def test_config_is_authorized_ephemeral_effective_and_revision_tracks_changes(self):
        key = self.configure()
        self.start()
        self.assertEqual(self.request("chat.config")["error"]["code"], "unauthorized")
        self.pair()
        capabilities = self.request("status")["result"]["capabilities"]
        self.assertIn("chat.config", capabilities)
        self.assertIn("chat.import", capabilities)
        request_id = str(uuid4())
        config = self.request("chat.config", request_id=request_id)["result"]
        self.assertTrue(config["available"])
        self.assertEqual(config["system_prompt"], SYSTEM_PROMPT)
        self.assertEqual(config["api_key"], key)
        self.assertEqual(config["model"], "configured-model")
        self.assertEqual(config["timeout_seconds"], 41)
        self.assertEqual(config["reasoning_effort"], "high")
        self.assertRegex(config["revision"], r"^[0-9a-f]{64}$")
        self.assertEqual(config, self.request("chat.config")["result"])
        self.assertIsNone(self.remote.sync.get_operation(support.PEER, request_id))
        self.assertNotIn(key, json.dumps(self.request("sync.snapshot")["result"]))
        self.assertNotIn(key, json.dumps(self.request("sync.pull")["result"]))
        self.assertNotIn(key, (self.path / "settings.ini").read_text(encoding="utf-8"))
        self.settings.save_api_key("DIFFERENT_DIRECT_CONFIG_KEY")
        self.assertNotEqual(config["revision"], self.request("chat.config")["result"]["revision"])
        self.settings.clear_api_key()
        unavailable = self.request("chat.config")["result"]
        self.assertEqual(set(unavailable), {"version", "available", "revision"})
        self.assertFalse(unavailable["available"])
        self.assertNotEqual(config["revision"], unavailable["revision"])

    def test_config_vault_read_runs_on_qt_thread_and_error_text_does_not_escape(self):
        self.configure()
        self.enable()
        threads = []
        getter = self.settings.get_api_key
        def read():
            threads.append(threading.get_ident())
            return getter()
        with patch.object(self.settings, "get_api_key", side_effect=read), ThreadPoolExecutor(1) as executor:
            future = executor.submit(self.request, "chat.config")
            support.spin_until(future.done)
            self.assertTrue(future.result()["result"]["available"])
        self.assertEqual(threads, [threading.get_ident()])
        with patch.object(self.vault, "get_password", side_effect=RuntimeError("PRIVATE_BACKEND_SECRET")):
            response = self.request("chat.config")
        self.assertTrue(response["ok"])
        self.assertFalse(response["result"]["available"])
        self.assertNotIn("PRIVATE_BACKEND_SECRET", json.dumps(response))

    def test_import_is_atomic_durable_and_deduplicates_same_ids_without_ai(self):
        self.enable()
        params, request_id = exchange(), str(uuid4())
        with patch.object(self.chat, "send", side_effect=AssertionError("Must not call AI")):
            first = self.request("chat.import", params, request_id=request_id)
            self.assertTrue(first["ok"], first)
            self.assertFalse(first["result"]["duplicate"])
            duplicate = self.request("chat.import", params, request_id=request_id)
            second_id = self.request("chat.import", params)
        self.assertTrue(duplicate["result"]["duplicate"])
        self.assertTrue(second_id["result"]["duplicate"])
        messages = self.chat.store.load_messages(params["conversation_id"])
        self.assertEqual(len(messages), 2)
        self.assertEqual([m["id"] for m in messages], [m["id"] for m in params["messages"]])
        self.assertEqual(messages[0]["metadata"]["request_status"], "complete")
        self.assertEqual(messages[1]["metadata"]["reply_to"], messages[0]["id"])
        operation = RemoteSyncStore(self.chat.store).get_operation(support.PEER, request_id)
        self.assertEqual(operation["state"], "complete")
        self.assertTrue(operation["result"]["ok"])
        self.assertNotIn(params["messages"][0]["content"], json.dumps(operation))
        changes = self.request("sync.pull")["result"]["changes"]
        public = [row["data"] for row in changes if row["entity"] == "message"]
        self.assertEqual(len(public), 2)
        self.assertEqual(public[1]["reply_to"], messages[0]["id"])
        self.assertNotIn("remote_import", json.dumps(public))

    def test_import_collision_or_cross_conversation_is_rejected_without_mutating_history(self):
        self.enable()
        params, request_id = exchange(), str(uuid4())
        self.assertTrue(self.request("chat.import", params, request_id=request_id)["ok"])
        before = self.chat.store.load_messages(params["conversation_id"])
        for same_request in (True, False):
            changed = deepcopy(params)
            changed["messages"][0]["content"] = "conflicting replacement"
            result = self.request("chat.import", changed, request_id=request_id if same_request else None)
            self.assertEqual(result["error"]["code"], "import_conflict")
        other = deepcopy(params)
        other["conversation_id"] = str(uuid4())
        self.assertEqual(self.request("chat.import", other)["error"]["code"], "import_conflict")
        self.assertEqual(self.chat.store.load_messages(params["conversation_id"]), before)
        self.assertIsNone(self.chat.store.get_conversation(other["conversation_id"]))

    def test_import_validates_roles_fields_uuids_reply_ownership_and_timestamps_before_any_write(self):
        self.enable()
        original = exchange()
        cases = []
        for field, value in (("id", "not-a-uuid"), ("created_at", "2026-10-31T13:00:00"),
                             ("role", "system"), ("content", ""), ("image_id", "../private")):
            params = deepcopy(original)
            params["messages"][0][field] = value
            cases.append(params)
        params = deepcopy(original)
        params["messages"][1]["reply_to"] = str(uuid4())
        cases.append(params)
        params = deepcopy(original)
        params["messages"][1]["image_id"] = "a" * 64
        cases.append(params)
        params = deepcopy(original)
        params["messages"][0]["content"] = "\ud800"
        cases.append(params)
        for params in cases:
            self.assertFalse(self.request("chat.import", params)["ok"])
        self.assertIsNone(self.chat.store.get_conversation(original["conversation_id"]))
        self.assertEqual(self.remote.sync.pull()["changes"], [])

    def test_import_does_not_overwrite_existing_title_and_accepts_equivalent_utc_times(self):
        self.enable()
        params = exchange()
        params["title"] = "A phone title longer than the thirty two character fallback"
        self.assertTrue(self.request("chat.import", params)["ok"])
        self.assertEqual(self.chat.store.get_conversation(params["conversation_id"])["title"], params["title"])
        self.chat.store.rename_conversation(params["conversation_id"], "Desktop manual title")
        followup = exchange(params["conversation_id"])
        self.assertTrue(self.request("chat.import", followup)["ok"])
        self.assertEqual(self.chat.store.get_conversation(params["conversation_id"])["title"], "Desktop manual title")
        for message in params["messages"]:
            message["created_at"] = datetime.fromisoformat(message["created_at"]).astimezone(
                timezone(timedelta(hours=8))).isoformat()
        self.assertTrue(self.request("chat.import", params)["result"]["duplicate"])

    def test_concurrent_imports_share_one_committed_exchange_and_two_receipts(self):
        self.enable()
        params = exchange()
        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(self.remote.sync.import_exchange, support.PEER, str(uuid4()), params)
                       for _ in range(2)]
            results = [future.result(timeout=8) for future in futures]
        self.assertEqual(sorted(result["duplicate"] for result in results), [False, True])
        self.assertEqual(len(self.chat.store.load_messages(params["conversation_id"])), 2)

    def test_deleted_conversation_and_cleared_messages_cannot_be_resurrected(self):
        self.enable()
        for delete_conversation in (True, False):
            params, request_id = exchange(), str(uuid4())
            self.assertTrue(self.request("chat.import", params, request_id=request_id)["ok"])
            if delete_conversation:
                self.chat.store.delete_conversation(params["conversation_id"])
            else:
                self.chat.store.clear_messages(params["conversation_id"])
            for retry_id in (request_id, str(uuid4())):
                result = self.request("chat.import", params, request_id=retry_id)
                self.assertEqual(result["error"]["code"], "conversation_deleted")
            self.assertEqual(self.chat.store.load_messages(params["conversation_id"]), [])

    def test_imported_image_reference_is_public_but_missing_images_and_injected_metadata_fail(self):
        self.enable()
        missing = exchange(image_id="e" * 64)
        self.assertEqual(self.request("chat.import", missing)["error"]["code"], "image_missing")
        self.assertIsNone(self.chat.store.get_conversation(missing["conversation_id"]))
        uploaded = self.request("image.upload", support.image_params())["result"]
        params = exchange(image_id=uploaded["id"])
        self.assertTrue(self.request("chat.import", params)["ok"])
        public = [row["data"] for row in self.request("sync.pull")["result"]["changes"]
                  if row["entity"] == "message" and row["conversation_id"] == params["conversation_id"]]
        self.assertEqual(public[0]["image_id"], uploaded["id"])
        self.assertNotIn("data_base64", json.dumps(public))
        injected = exchange()
        injected["messages"][0]["metadata"] = {"api_key": "PRIVATE_INJECTION"}
        self.assertEqual(self.request("chat.import", injected)["error"]["code"], "invalid_request")
        self.request("image.delete", {"id": uploaded["id"]})
        self.assertEqual(self.request("chat.import", params)["error"]["code"], "image_missing")

    def test_import_rolls_back_conversation_messages_journal_and_receipt_on_disk_failure(self):
        self.enable()
        params, request_id = exchange(), str(uuid4())
        before = self.remote.sync.pull()["next_since"]
        with self.chat.store._connection() as connection:
            connection.execute("""CREATE TRIGGER qa_fail_import BEFORE INSERT ON messages
                WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'qa'); END""")
        failed = self.request("chat.import", params, request_id=request_id)
        self.assertEqual(failed["error"]["code"], "storage_error")
        self.assertIsNone(self.chat.store.get_conversation(params["conversation_id"]))
        self.assertEqual(self.chat.store.load_messages(params["conversation_id"]), [])
        self.assertIsNone(self.remote.sync.get_operation(support.PEER, request_id))
        self.assertEqual(self.remote.sync.pull()["next_since"], before)
        with self.chat.store._connection() as connection:
            connection.execute("DROP TRIGGER qa_fail_import")
        self.assertTrue(self.request("chat.import", params, request_id=request_id)["ok"])

    def test_import_refreshes_visible_history_without_selecting_other_conversation(self):
        self.enable()
        current = self.chat.current_conversation_id
        self.assertTrue(self.request("chat.import", exchange())["ok"])
        support.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.assertEqual(self.chat.current_conversation_id, current)
        params = exchange(current)
        self.assertTrue(self.request("chat.import", params)["ok"])
        support.spin_until(lambda: len(self.chat.messages) == 2)
        self.assertEqual(self.chat.messages[1]["content"], "phone direct answer")
        self.assertFalse(self.chat.busy)

    def test_phone_can_use_exported_configuration_for_one_http_call_then_import_without_second_call(self):
        self.enable()
        with MockApiServer() as api:
            self.configure(api.base_url)
            config = self.request("chat.config")["result"]
            api.enqueue(json.dumps({"reply": "direct model answer", "emotion": "happy"}))
            reply = ApiClient().complete_context(ApiSettings(config["base_url"], config["model"],
                config["timeout_seconds"], config["reasoning_effort"]), config["api_key"],
                [{"role": "system", "content": config["system_prompt"]},
                 {"role": "user", "content": "phone direct question"}])
            params = exchange()
            params["messages"][1]["content"] = reply.reply
            self.assertTrue(self.request("chat.import", params)["ok"])
            support.spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            self.assertEqual(len(api.requests), 1)
            self.assertEqual(api.requests[0]["body"]["messages"][0]["content"], SYSTEM_PROMPT)
            self.assertEqual(self.chat.store.load_messages(params["conversation_id"])[1]["content"], "direct model answer")


if __name__ == "__main__":
    unittest.main()
