"""Character preferences and real chat entry points with owned settings only."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ChatReply, SYSTEM_PROMPT
from desktop_pet.agents.routing import ROUTING_PROMPT, agent_system_prompt
from desktop_pet.character_cards import CharacterCard, CharacterCardError
from desktop_pet.character_prompts import (
    CHARACTER_PROMPT_MARKER, DIRECT_AGENT_PROMPT, character_system_prompt,
    direct_agent_system_prompt,
)
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ContextBudget, ConversationStore
from desktop_pet.conversations.context import estimate_tokens
from desktop_pet.remote.chat_transfer import direct_chat_configuration
from desktop_pet.settings_manager import ApiSettings, SettingsError
from tests.mock_api import MockApiServer
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until


def card(name="Luna", **fields):
    return CharacterCard.from_dict({"name": name, "personality": "Gentle and concise.", **fields})


class CapturingClient:
    def __init__(self):
        self.contexts = []

    def complete_context(self, settings, key, messages, **kwargs):
        self.contexts.append(messages)
        return ChatReply("受控角色回复")


class CharacterProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-character-profile-")
        self.root = Path(self.temporary.name)
        self.settings = isolated_settings(self.root / "settings.ini", MemoryCredentialBackend())
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "owned-model", 4))
        self.settings.save_api_key(LOCAL_TEST_KEY)
        self.store = ConversationStore(self.root / "history.sqlite3")
        self.controllers, self.release_events = [], []
        self.guards = ExitStack()
        self.guards.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("No test process")))
        self.guards.enter_context(patch("openai._base_client.get_platform", return_value="Windows"))
        self.guards.enter_context(patch("openai._base_client.get_architecture", return_value="x64"))

    def tearDown(self):
        for release in self.release_events:
            release.set()
        for chat in self.controllers:
            chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for chat in self.controllers:
            chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.guards.close()
        self.temporary.cleanup()

    def make_chat(self, client=None, **kwargs):
        chat = ChatController(self.settings, client, store=self.store,
            metadata_client_factory=lambda: None, **kwargs)
        self.controllers.append(chat)
        return chat

    def test_default_prompts_and_routing_keep_the_existing_contract(self):
        self.assertEqual(character_system_prompt(None), SYSTEM_PROMPT)
        self.assertEqual(direct_agent_system_prompt(None), DIRECT_AGENT_PROMPT)
        self.assertEqual(agent_system_prompt(SYSTEM_PROMPT), ROUTING_PROMPT)

    def test_persona_macros_and_modes_preserve_host_rules(self):
        selected = CharacterCard.from_dict({"spec": "chara_card_v3", "spec_version": "3.0", "data": {
            "name": "Luna", "nickname": "Moon", "description": "{{char}} speaks to {{user}}.",
            "system_prompt": "{{original}} Keep a gentle voice.", "creator_notes": "CREATOR_ONLY_PRIVATE_NOTE"}})
        prompt = character_system_prompt(selected)
        data, _ = json.JSONDecoder().raw_decode(prompt[len(CHARACTER_PROMPT_MARKER):])
        self.assertEqual(data["name"], "Moon")
        self.assertEqual(data["description"], "Moon speaks to 用户.")
        self.assertNotIn("{{original}}", prompt)
        self.assertNotIn("CREATOR_ONLY_PRIVATE_NOTE", prompt)
        for mode in (agent_system_prompt(prompt), direct_agent_system_prompt(selected)):
            self.assertTrue(mode.startswith(CHARACTER_PROMPT_MARKER))
            self.assertIn('"name":"Moon"', mode)
            self.assertNotIn("You are 鲸鱼娘", mode)
        self.assertIn("agent_request", agent_system_prompt(prompt))

    def test_macro_expansion_is_bounded_before_profile_is_saved(self):
        selected = card("n" * 128, description="{{char}}" * 2000)
        with self.assertRaises(CharacterCardError):
            character_system_prompt(selected)
        chat = self.make_chat(CapturingClient())
        self.assertFalse(chat.set_character_card(selected))
        self.assertIsNone(chat.character_card)
        self.assertIsNone(self.settings.load_character_card())

    def test_phone_prompt_limit_counts_utf16_units_for_non_bmp_characters(self):
        selected = card(description="🐳" * 17000)
        with self.assertRaises(CharacterCardError):
            character_system_prompt(selected)
        self.settings.save_character_card(selected)
        self.assertFalse(direct_chat_configuration(self.settings)["available"])
        self.settings.save_character_card(card(description="🐳" * 15000))
        result = direct_chat_configuration(self.settings)
        self.assertTrue(result["available"])
        self.assertLessEqual(len(result["system_prompt"].encode("utf-16-le")) // 2, 32768)

    def test_preferences_round_trip_and_default_restore(self):
        selected = card(first_mes="Hello {{user}}.", extensions={"custom": [1, 2]})
        self.settings.save_character_card(selected)
        reopened = isolated_settings(self.root / "settings.ini", MemoryCredentialBackend())
        self.assertEqual(reopened.load_character_card().to_dict(), selected.to_dict())
        reopened.save_character_card(None)
        self.assertIsNone(reopened.load_character_card())
        self.assertFalse(reopened._settings.contains("character/card"))

    def test_corrupt_preferences_fall_back_without_erasing_the_saved_value(self):
        self.settings._settings.setValue("character/card", "{corrupt")
        self.assertIsNone(self.settings.load_character_card())
        self.assertEqual(self.settings._settings.value("character/card"), "{corrupt")

    def test_failed_save_preserves_the_previous_character(self):
        chat = self.make_chat(CapturingClient())
        self.assertTrue(chat.set_character_card(card("Alpha")))
        with patch.object(self.settings, "_sync", side_effect=SettingsError("Owned write failure")):
            self.assertFalse(chat.set_character_card(card("Beta")))
        self.assertEqual(chat.character_card.name, "Alpha")
        self.assertEqual(self.settings.load_character_card().name, "Alpha")

    def test_failed_ack_after_real_commit_restores_the_previous_disk_value(self):
        chat = self.make_chat(CapturingClient())
        self.assertTrue(chat.set_character_card(card("Alpha")))
        original_sync = self.settings._sync

        def committed_without_acknowledgement():
            original_sync()
            raise SettingsError("Owned lost acknowledgement")

        with patch.object(self.settings, "_sync", side_effect=committed_without_acknowledgement):
            self.assertFalse(chat.set_character_card(card("Beta")))
        persisted = (self.root / "settings.ini").read_text(encoding="utf-8")
        self.assertIn("Alpha", persisted)
        self.assertNotIn("Beta", persisted)
        self.assertEqual(chat.character_card.name, "Alpha")

    def test_failed_rollback_reports_the_unconfirmed_disk_state(self):
        chat = self.make_chat(CapturingClient())
        self.assertTrue(chat.set_character_card(card("Alpha")))
        original_sync = self.settings._settings.sync

        def committed_without_acknowledgement():
            original_sync()
            raise SettingsError("Owned lost acknowledgement")

        with patch.object(self.settings, "_sync", side_effect=committed_without_acknowledgement), \
                patch.object(self.settings._settings, "sync", side_effect=OSError("Owned rollback refusal")):
            self.assertFalse(chat.set_character_card(card("Beta")))
        self.assertIn("无法确认", chat.error)
        self.assertEqual(chat.character_card.name, "Alpha")
        original_sync()

    def test_manual_budget_error_is_reported_before_apply(self):
        chat = self.make_chat(CapturingClient(), context_budget=ContextBudget())
        self.assertFalse(chat.set_character_card(card(description="角色设定" * 300)))
        self.assertIn("预算", chat.error)
        self.assertIsNone(chat.character_card)
        self.assertIsNone(self.settings.load_character_card())

    def test_apply_checks_the_budget_of_enabled_agent_modes(self):
        selected = card(personality="gentle")
        ordinary_tokens = estimate_tokens(character_system_prompt(selected)) + 12
        chat = self.make_chat(CapturingClient(), context_budget=ContextBudget(system_tokens=ordinary_tokens))
        chat._agent_settings = replace(chat._agent_settings, enabled=False)
        self.assertTrue(chat.set_character_card(selected))
        chat._agent_settings = replace(chat._agent_settings, enabled=True)
        self.assertFalse(chat.set_character_card(selected))
        self.assertIn("预算", chat.error)
        self.assertEqual(chat.character_card.name, "Luna")

    def test_in_flight_turn_keeps_its_character_snapshot(self):
        class HeldMemory:
            def __init__(self):
                self.entered, self.release = threading.Event(), threading.Event()
                self.calls = 0

            def load_settings(self):
                return SimpleNamespace(enabled=True, automatic=False, relevance_threshold=.6)

            def search(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    self.entered.set()
                    self.release.wait(5)
                return []

        memory, client = HeldMemory(), CapturingClient()
        self.release_events.append(memory.release)
        chat = self.make_chat(client, memory_service=memory)
        self.assertTrue(chat.set_character_card(card("Alpha")))
        self.assertTrue(chat.send("第一条受控问题"))
        spin_until(memory.entered.is_set)
        self.assertTrue(chat.set_character_card(card("Beta")))
        memory.release.set()
        spin_until(lambda: not chat.busy)
        self.assertIn('"name":"Alpha"', client.contexts[0][0]["content"])
        self.assertTrue(chat.send("第二条受控问题"))
        spin_until(lambda: not chat.busy)
        self.assertIn('"name":"Beta"', client.contexts[1][0]["content"])

    def test_reentrant_message_listener_cannot_change_the_admitted_character(self):
        for initial in (None, card("Alpha")):
            with self.subTest(initial=initial.name if initial else "default"):
                self.settings.save_character_card(initial)
                client = CapturingClient()
                chat = self.make_chat(client)
                chat.new_conversation()
                switched = []

                def switch_character():
                    if not switched and any(message["role"] == "user" for message in chat.messages):
                        switched.append(True)
                        self.assertTrue(chat.set_character_card(card("Beta")))

                chat.messages_changed.connect(switch_character)
                self.assertTrue(chat.send("受理后更换角色的受控问题"))
                spin_until(lambda: not chat.busy)
                expected = character_system_prompt(initial)
                self.assertEqual(client.contexts[0][0]["content"], expected)
                self.assertEqual(chat.character_card.name, "Beta")

    def test_phone_configuration_revision_changes_with_persona(self):
        original = direct_chat_configuration(self.settings)
        self.settings.save_character_card(card("Luna"))
        current = direct_chat_configuration(self.settings)
        self.assertEqual(current["api_key"], original["api_key"])
        self.assertEqual(current["model"], original["model"])
        self.assertNotEqual(current["revision"], original["revision"])
        self.assertIn('"name":"Luna"', current["system_prompt"])
        self.settings.save_character_card(None)
        self.assertEqual(direct_chat_configuration(self.settings), original)

    def test_phone_persona_sync_is_independent_of_desktop_api_availability(self):
        initial = direct_chat_configuration(self.settings)
        self.settings.save_character_card(card("Luna"))
        role = direct_chat_configuration(self.settings)["persona"]
        self.assertEqual(role["name"], "Luna")
        self.assertNotEqual(role["revision"], initial["persona"]["revision"])
        self.assertNotIn("api_key", role)
        self.settings.clear_api_key()
        unavailable = direct_chat_configuration(self.settings)
        self.assertFalse(unavailable["available"])
        self.assertEqual(unavailable["persona"], role)
        self.assertNotIn("api_key", unavailable)
        self.settings.save_character_card(None)
        restored = direct_chat_configuration(self.settings)
        self.assertFalse(restored["available"])
        self.assertEqual(restored["persona"], initial["persona"])

    def test_normal_chat_sends_persona_through_the_real_local_transport(self):
        with MockApiServer() as server:
            self.settings.save_api_settings(ApiSettings(server.base_url, "owned-model", 4))
            chat = self.make_chat()
            self.assertTrue(chat.set_character_card(card("Luna")))
            server.enqueue('{"reply":"受控回复","emotion":"happy"}')
            self.assertTrue(chat.send("你好"))
            spin_until(lambda: not chat.busy)
            self.assertFalse(chat.error, chat.error)
            self.assertEqual(len(server.requests), 1)
            sent = server.requests[0]["body"]["messages"]
            self.assertEqual(sent[0]["role"], "system")
            self.assertIn('"name":"Luna"', sent[0]["content"])
            self.assertEqual(chat.messages[-1]["content"], "受控回复")

    def test_saved_roles_switch_without_rewriting_chat_and_survive_restart(self):
        alpha = self.settings.save_library_character_card(card("Alpha"), label="温柔角色")
        beta = self.settings.save_library_character_card(card("Beta"), label="活泼角色")
        client = CapturingClient()
        chat = self.make_chat(client)
        chat.new_conversation()
        self.assertTrue(chat.set_character_card(self.settings.load_library_character_card(alpha.id)))
        self.assertTrue(chat.send("第一条角色库问题"))
        spin_until(lambda: not chat.busy)
        conversation_id, messages = chat.current_conversation_id, deepcopy(chat.messages)

        self.assertTrue(chat.set_character_card(self.settings.load_library_character_card(beta.id)))
        self.assertEqual(chat.current_conversation_id, conversation_id)
        self.assertEqual(chat.messages, messages)
        self.assertTrue(chat.send("切换后的第二条问题"))
        spin_until(lambda: not chat.busy)
        self.assertIn('"name":"Alpha"', client.contexts[0][0]["content"])
        self.assertIn('"name":"Beta"', client.contexts[1][0]["content"])

        reopened = isolated_settings(self.root / "settings.ini", MemoryCredentialBackend())
        self.assertEqual({entry.id for entry in reopened.list_character_cards()}, {alpha.id, beta.id})
        self.assertEqual(reopened.load_library_character_card(alpha.id), card("Alpha"))
        self.assertEqual(reopened.load_character_card().name, "Beta")
        restored = ChatController(reopened, CapturingClient(), store=self.store,
                                 metadata_client_factory=lambda: None)
        self.controllers.append(restored)
        self.assertEqual(restored.character_card.name, "Beta")

    def test_deleting_saved_roles_keeps_active_and_inflight_role_copies(self):
        class HeldClient(CapturingClient):
            def __init__(self):
                super().__init__()
                self.entered, self.release = threading.Event(), threading.Event()

            def complete_context(self, settings, key, messages, **kwargs):
                self.contexts.append(messages)
                self.entered.set()
                self.release.wait(5)
                return ChatReply("角色库快照受控回复")

        alpha = self.settings.save_library_character_card(card("Alpha"))
        beta = self.settings.save_library_character_card(card("Beta"))
        client = HeldClient()
        self.release_events.append(client.release)
        chat = self.make_chat(client)
        self.assertTrue(chat.set_character_card(self.settings.load_library_character_card(alpha.id)))
        self.assertTrue(chat.send("等待期间切换和删除角色库条目"))
        spin_until(client.entered.is_set)
        self.assertTrue(chat.set_character_card(self.settings.load_library_character_card(beta.id)))
        self.assertTrue(self.settings.delete_library_character_card(alpha.id))
        self.assertTrue(self.settings.delete_library_character_card(beta.id))
        self.assertEqual(len(self.settings.list_character_cards()), 0)
        self.assertEqual(chat.character_card.name, "Beta")
        self.assertEqual(self.settings.load_character_card().name, "Beta")
        client.release.set()
        spin_until(lambda: not chat.busy)
        self.assertIn('"name":"Alpha"', client.contexts[0][0]["content"])
        self.assertTrue(chat.send("角色库清空后仍使用当前角色"))
        spin_until(lambda: not chat.busy)
        self.assertIn('"name":"Beta"', client.contexts[1][0]["content"])

    def test_library_maintenance_changes_phone_persona_only_when_role_is_applied(self):
        initial = direct_chat_configuration(self.settings)
        alpha = self.settings.save_library_character_card(card("Alpha"))
        beta = self.settings.save_library_character_card(card("Beta"))
        self.assertEqual(direct_chat_configuration(self.settings), initial)
        chat = self.make_chat(CapturingClient())
        self.assertTrue(chat.set_character_card(self.settings.load_library_character_card(alpha.id)))
        applied = direct_chat_configuration(self.settings)
        self.assertNotEqual(applied["revision"], initial["revision"])
        self.settings.rename_library_character_card(alpha.id, "只改库名称")
        self.assertEqual(direct_chat_configuration(self.settings), applied)
        self.settings.delete_library_character_card(alpha.id)
        self.assertEqual(direct_chat_configuration(self.settings), applied)
        self.assertTrue(chat.set_character_card(self.settings.load_library_character_card(beta.id)))
        self.assertNotEqual(direct_chat_configuration(self.settings)["revision"], applied["revision"])
        self.assertIn('"name":"Beta"', direct_chat_configuration(self.settings)["system_prompt"])


if __name__ == "__main__":
    unittest.main()
