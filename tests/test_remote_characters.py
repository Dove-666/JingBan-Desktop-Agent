"""Paired role selection through real Qt/SQLite, without model or screen access."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.character_cards import CharacterCard
from desktop_pet.character_library import character_card_digest
from desktop_pet.character_prompts import character_system_prompt
from desktop_pet.remote.character_transfer import character_library_summary
from desktop_pet.settings_manager import ApiSettings
from tests import test_remote_controller as support


def card(name="海月", personality="温柔、简洁"):
    return CharacterCard.from_dict({"name": name, "personality": personality})


class CharacterRpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    setUp = support.RemoteControllerTests.setUp
    tearDown = support.RemoteControllerTests.tearDown
    start = support.RemoteControllerTests.start
    pair = support.RemoteControllerTests.pair
    request = support.RemoteControllerTests.request

    def enable(self):
        self.start()
        self.pair()

    def saved(self, name="海月", personality="温柔、简洁"):
        selected = card(name, personality)
        entry = self.settings.save_library_character_card(selected)
        return selected, entry

    def selection(self, entry):
        state = self.request("character.list")["result"]
        return {"id": entry.id, "digest": entry.digest, "expected_revision": state["revision"]}

    def test_capabilities_require_pairing_and_all_reads_are_ephemeral(self):
        self.start()
        for method in ("character.list", "character.get", "character.select"):
            response = self.request(method)
            self.assertEqual(response["error"]["code"], "unauthorized")
        self.pair()
        capabilities = self.request("status")["result"]["capabilities"]
        self.assertTrue({"character.list", "character.get", "character.select"}.issubset(capabilities))
        for method, params in (("character.list", {}), ("character.get", {"id": "default"}), ("chat.config", {})):
            identity = str(uuid4())
            self.assertTrue(self.request(method, params, request_id=identity)["ok"])
            self.assertIsNone(self.remote.sync.get_operation(support.PEER, identity))

    def test_default_role_and_catalog_work_without_a_model_key(self):
        self.enable()
        response = self.request("character.list")
        self.assertTrue(response["ok"], response)
        state = response["result"]
        self.assertEqual(state["items"], [])
        self.assertEqual(state["total"], 0)
        self.assertIsNone(state["next_offset"])
        self.assertEqual(state["active"]["id"], "default")
        self.assertEqual(state["active"]["name"], "鲸鱼娘")
        default = self.request("character.get", {"id": "default"})["result"]
        self.assertEqual(default["digest"], state["active"]["digest"])
        self.assertEqual(default["persona"], state["active"]["persona"])
        config = self.request("chat.config")["result"]
        self.assertFalse(config["available"])
        self.assertEqual(config["character_library"]["revision"], state["revision"])
        self.assertEqual(config["persona"], state["active"]["persona"])
        self.assertEqual(config["character_library"], character_library_summary(self.settings))

    def test_pagination_is_complete_and_revision_bound_without_prompt_batches(self):
        self.enable()
        saved = [self.saved("角色" + str(index))[1] for index in range(3)]
        first = self.request("character.list", {"limit": 1})["result"]
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["next_offset"], 1)
        second = self.request("character.list", {"offset": 1, "limit": 2, "revision": first["revision"]})["result"]
        self.assertIsNone(second["next_offset"])
        self.assertEqual({item["id"] for item in first["items"] + second["items"]}, {item.id for item in saved})
        self.assertTrue(all(set(item) == {"id", "label", "name", "digest"}
                            for item in first["items"] + second["items"]))
        self.settings.rename_library_character_card(saved[0].id, "更新名称")
        stale = self.request("character.list", {"offset": 1, "revision": first["revision"]})
        self.assertEqual(stale["error"]["code"], "character_conflict")

    def test_role_get_is_exact_and_select_updates_effective_desktop_role_without_changing_api(self):
        selected, entry = self.saved()
        self.settings.save_api_settings(ApiSettings("https://example.invalid/v1", "owned-model", 30))
        self.settings.save_api_key("ROLE_TEST_PRIVATE_KEY")
        before = self.settings.load_api_settings()
        self.enable()
        changes, threads = [], []
        self.chat.character_changed.connect(lambda: (changes.append(self.chat.character_card), threads.append(threading.get_ident())))
        value = self.request("character.get", {"id": entry.id, "digest": entry.digest})["result"]
        self.assertEqual(value["digest"], character_card_digest(selected))
        self.assertEqual(value["persona"]["system_prompt"], character_system_prompt(selected))
        response = self.request("character.select", self.selection(entry))
        self.assertTrue(response["ok"], response)
        self.assertEqual(set(response["result"]), {"version", "applied", "active_id", "revision"})
        self.assertTrue(response["result"]["applied"])
        self.assertEqual(self.chat.character_card, selected)
        self.assertEqual(self.settings.load_character_card(), selected)
        self.assertEqual(changes, [selected])
        self.assertEqual(threads, [threading.get_ident()])
        self.assertEqual(self.settings.load_api_settings(), before)
        self.assertEqual(self.settings.get_api_key(), "ROLE_TEST_PRIVATE_KEY")
        self.assertEqual(self.request("chat.config")["result"]["persona"], value["persona"])
        state = self.request("character.list")["result"]
        self.assertEqual(state["active"]["id"], entry.id)
        self.assertEqual(state["revision"], response["result"]["revision"])

    def test_duplicate_lost_reply_does_not_reapply_after_desktop_selects_another_role(self):
        first, entry = self.saved()
        second, second_entry = self.saved("星澜", "活泼")
        self.enable()
        params, identity = self.selection(entry), str(uuid4())
        count = []
        self.chat.character_changed.connect(lambda: count.append(1))
        applied = self.request("character.select", params, request_id=identity)
        self.assertTrue(applied["ok"])
        self.assertTrue(self.chat.set_character_card(second))
        duplicate = self.request("character.select", params, request_id=identity)
        self.assertTrue(duplicate["ok"])
        self.assertTrue(duplicate["result"]["duplicate"])
        self.assertEqual(duplicate["result"]["state"], "complete")
        self.assertEqual(duplicate["result"]["result"]["result"], applied["result"])
        self.assertEqual(self.chat.character_card, second)
        self.assertEqual(len(count), 2)
        self.assertEqual(self.request("character.list")["result"]["active"]["id"], second_entry.id)

    def test_stale_selection_and_deleted_card_never_overwrite_the_current_role(self):
        selected, entry = self.saved()
        self.enable()
        stale = self.selection(entry)
        self.settings.rename_library_character_card(entry.id, "重命名")
        result = self.request("character.select", stale)
        self.assertEqual(result["error"]["code"], "character_conflict")
        self.assertIsNone(self.chat.character_card)
        missing = self.selection(entry)
        self.settings.delete_library_character_card(entry.id)
        result = self.request("character.get", {"id": entry.id})
        self.assertEqual(result["error"]["code"], "character_missing")
        self.assertEqual(self.request("character.select", missing)["error"]["code"], "character_conflict")
        self.assertIsNone(self.chat.character_card)

    def test_card_changed_between_catalog_and_load_reports_conflict_without_applying(self):
        _, entry = self.saved()
        self.enable()
        with patch.object(self.settings, "load_library_character_card", return_value=card("更新后的卡")):
            result = self.request("character.get", {"id": entry.id, "digest": entry.digest})
            self.assertEqual(result["error"]["code"], "character_conflict")
        self.assertIsNone(self.chat.character_card)

    def test_invalid_writes_never_journal_private_extra_fields_or_accept_bad_hashes(self):
        _, entry = self.saved()
        self.enable()
        valid = self.selection(entry)
        for params in (dict(valid, system_prompt="PRIVATE_ROLE_PROMPT"), dict(valid, api_key="PRIVATE_KEY"),
                       dict(valid, id="../card"), dict(valid, digest="bad"), dict(valid, expected_revision=True)):
            identity = str(uuid4())
            value = self.request("character.select", params, request_id=identity)
            self.assertEqual(value["error"]["code"], "invalid_request")
            self.assertIsNone(self.remote.sync.get_operation(support.PEER, identity))
        for params in ({"limit": True}, {"limit": 101}, {"offset": -1}, {"offset": 2}, {"prompt": "x"}):
            self.assertEqual(self.request("character.list", params)["error"]["code"], "invalid_request")

    def test_prompts_and_credentials_never_enter_operations_or_chat_sync(self):
        token = "SYNTHETIC_ROLE_API_KEY"
        self.settings.save_api_key(token)
        selected, entry = self.saved("角色" + token, "PRIVATE_ROLE_TEXT " + token)
        self.enable()
        state = self.request("character.list")["result"]
        value = self.request("character.get", {"id": entry.id})["result"]
        self.assertNotIn(token, json.dumps(state, ensure_ascii=False))
        self.assertNotIn(token, json.dumps(value, ensure_ascii=False))
        self.assertIn("PRIVATE_ROLE_TEXT", value["persona"]["system_prompt"])
        identity = str(uuid4())
        response = self.request("character.select", self.selection(entry), request_id=identity)
        self.assertTrue(response["ok"], response)
        journal = json.dumps(self.remote.sync.get_operation(support.PEER, identity), ensure_ascii=False)
        self.assertNotIn(token, journal)
        self.assertNotIn("PRIVATE_ROLE_TEXT", journal)
        self.assertNotIn("PRIVATE_ROLE_TEXT", json.dumps(self.request("sync.snapshot")["result"]))
        self.assertNotIn("PRIVATE_ROLE_TEXT", json.dumps(self.request("sync.pull")["result"]))

    def test_reset_and_unsaved_current_card_have_truthful_active_identity(self):
        self.enable()
        selected = card("未入库角色")
        self.assertTrue(self.chat.set_character_card(selected))
        state = self.request("character.list")["result"]
        self.assertIsNone(state["active"]["id"])
        self.assertEqual(state["active"]["name"], selected.name)
        self.assertEqual(state["items"], [])
        default = self.request("character.get", {"id": "default"})["result"]
        response = self.request("character.select", {"id": "default", "digest": default["digest"],
            "expected_revision": state["revision"]})
        self.assertTrue(response["ok"], response)
        self.assertIsNone(self.chat.character_card)
        self.assertIsNone(self.settings.load_character_card())

    def test_revocation_before_queued_qt_selection_prevents_the_write(self):
        _, entry = self.saved()
        self.enable()
        params = self.selection(entry)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.request, "character.select", params)
            # Queue admission can reserve the request, but actual authority is
            # checked again on Qt before changing persistent role preferences.
            import time
            time.sleep(.03)
            self.remote.preferences.revoke(support.PEER)
            support.spin_until(pending.done)
            self.assertEqual(pending.result()["error"]["code"], "unauthorized")
        self.assertIsNone(self.chat.character_card)
        self.assertIsNone(self.settings.load_character_card())


if __name__ == "__main__":
    unittest.main()
