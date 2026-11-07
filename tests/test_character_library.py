"""Owned SQLite role libraries: persisted documents and atomic operations."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from PySide6.QtCore import QSettings

from desktop_pet.character_cards import CharacterCard
from desktop_pet.character_library import CharacterLibrary, CharacterLibraryError
from desktop_pet.settings_manager import SettingsError, SettingsManager


def card(name="Luna", description="gentle", **extra):
    return CharacterCard.from_dict({"spec": "chara_card_v3", "spec_version": "3.0", "data": {
        "name": name, "description": description, "extensions": {"vendor": {"kept": [1, 2]}},
        "character_book": {"entries": [{"content": "preserved, not executed"}]}, **extra}})


class ForbiddenCredentials:
    def __getattr__(self, name):
        raise AssertionError("Character storage cannot use credentials: " + name)


class CharacterLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-role-library-")
        self.root = Path(self.temporary.name)
        self.path = self.root / "character_cards.sqlite3"
        self.library = CharacterLibrary(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_empty_read_operations_do_not_create_a_database(self):
        self.assertEqual(self.library.list(), ())
        self.assertFalse(self.library.delete("0" * 32))
        for action in (lambda: self.library.load("0" * 32), lambda: self.library.rename("0" * 32, "New")):
            with self.assertRaises(CharacterLibraryError): action()
        self.assertFalse(self.path.exists())

    def test_save_load_restart_preserves_native_v3_document_and_stable_id(self):
        selected = card(nickname="Moon", assets=[{"uri": "https://example.invalid/no-download"}])
        entry = self.library.save(selected)
        self.assertEqual(UUID(entry.id).version, 4)
        self.assertEqual((entry.label, entry.name), ("Luna", "Luna"))
        reopened = CharacterLibrary(self.path)
        self.assertEqual(reopened.list(), (entry,))
        self.assertEqual(reopened.load(entry.id).to_dict(), selected.to_dict())
        self.assertEqual(reopened.load(entry.id), selected)

    def test_v1_and_v2_original_documents_roundtrip_without_conversion(self):
        for document in ({"name": "Flat", "personality": "quiet", "unknown": {"x": True}},
                         {"spec": "chara_card_v2", "spec_version": "2.0", "data": {
                             "name": "V2", "extensions": {"raw": ["a"]}}, "top_extra": 3}):
            with self.subTest(document=document):
                entry = self.library.save(CharacterCard.from_dict(document))
                self.assertEqual(self.library.load(entry.id).to_dict(), document)

    def test_repeated_document_reuses_id_without_overwriting_renamed_label(self):
        selected = card()
        entry = self.library.save(selected)
        renamed = self.library.rename(entry.id, "Evening")
        reordered = CharacterCard.from_dict(dict(reversed(list(selected.to_dict().items()))))
        duplicate = self.library.save(reordered, label="Ignored replacement")
        self.assertEqual(duplicate, renamed)
        self.assertEqual(len(self.library.list()), 1)

    def test_same_role_name_with_different_personalities_has_distinct_entries(self):
        first = self.library.save(card(description="quiet"))
        second = self.library.save(card(description="lively"))
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.label, second.label)
        self.assertEqual(self.library.load(first.id).description, "quiet")
        self.assertEqual(self.library.load(second.id).description, "lively")

    def test_rename_is_label_only_and_delete_is_idempotent(self):
        selected = card()
        entry = self.library.save(selected)
        renamed = self.library.rename(entry.id, "  Favorite ' role  ")
        self.assertEqual(renamed.label, "Favorite ' role")
        self.assertEqual(self.library.load(entry.id).to_dict(), selected.to_dict())
        self.assertTrue(self.library.delete(entry.id))
        self.assertFalse(self.library.delete(entry.id))
        self.assertEqual(self.library.list(), ())

    def test_summary_list_never_parses_or_loads_card_document(self):
        entry = self.library.save(card())
        with patch.object(CharacterCard, "from_dict", side_effect=AssertionError("metadata only")):
            self.assertEqual(self.library.list(), (entry,))

    def test_invalid_labels_ids_and_card_types_do_not_replace_existing_cards(self):
        entry = self.library.save(card())
        for label in ("", " ", "x" * 129, "bad\nname", "bad\0name", "\ud800", 5):
            with self.subTest(label=repr(label)), self.assertRaises(CharacterLibraryError):
                self.library.rename(entry.id, label)
        for identity in ("../other", "' OR 1=1--", "0" * 31, "0" * 33, 4, None):
            with self.subTest(identity=identity), self.assertRaises(CharacterLibraryError):
                self.library.load(identity)
        with self.assertRaises(CharacterLibraryError): self.library.save({"name": "Not typed"})
        self.assertEqual(self.library.list(), (entry,))

    def test_damaged_library_is_reported_without_erasing_or_recreating_it(self):
        raw = b"not a sqlite database\x00PRIVATE_NATIVE_ERROR"
        self.path.write_bytes(raw)
        for action in (self.library.list, lambda: self.library.save(card())):
            with self.assertRaises(CharacterLibraryError) as caught: action()
            self.assertNotIn("PRIVATE_NATIVE_ERROR", str(caught.exception))
            self.assertNotIn(str(self.path), str(caught.exception))
            self.assertEqual(self.path.read_bytes(), raw)

    def test_other_application_or_future_schema_is_not_adopted(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE private_data(value TEXT)")
            db.execute("INSERT INTO private_data VALUES ('keep')")
        with self.assertRaises(CharacterLibraryError): self.library.save(card())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT value FROM private_data").fetchone(), ("keep",))

    def test_modified_card_payload_is_detected_without_discarding_other_roles(self):
        first = self.library.save(card())
        second = self.library.save(card("Sol"))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE character_cards SET document=? WHERE id=?", ('{"name":"tampered"}', first.id))
        with self.assertRaises(CharacterLibraryError): self.library.load(first.id)
        self.assertEqual(self.library.load(second.id).name, "Sol")
        self.assertEqual(len(self.library.list()), 2)

    def test_duplicate_save_does_not_claim_a_damaged_existing_payload_is_saved(self):
        selected = card()
        entry = self.library.save(selected)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE character_cards SET document=? WHERE id=?", ("damaged", entry.id))
        with self.assertRaises(CharacterLibraryError): self.library.save(selected)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT document FROM character_cards WHERE id=?", (entry.id,)).fetchone()[0], "damaged")

    def test_default_shelf_label_does_not_change_multiline_role_name(self):
        selected = card("Luna\nMoon")
        entry = self.library.save(selected)
        self.assertEqual(entry.name, selected.name)
        self.assertEqual(entry.label, "Luna Moon")
        self.assertEqual(self.library.list(), (entry,))
        self.assertEqual(self.library.load(entry.id), selected)

    def test_commit_failure_rolls_back_rename_delete_and_save(self):
        entry = self.library.save(card())
        real_connect = sqlite3.connect
        class CommitFailure:
            def __init__(self, connection): object.__setattr__(self, "connection", connection)
            def __getattr__(self, name): return getattr(self.connection, name)
            def __setattr__(self, name, value): setattr(self.connection, name, value)
            def commit(self): raise sqlite3.OperationalError("PRIVATE_COMMIT_ERROR")
        for action in (lambda: self.library.rename(entry.id, "Lost"),
                       lambda: self.library.delete(entry.id),
                       lambda: self.library.save(card("New"))):
            with self.subTest(action=action), patch("desktop_pet.character_library.sqlite3.connect",
                    side_effect=lambda *args, **kwargs: CommitFailure(real_connect(*args, **kwargs))):
                with self.assertRaises(CharacterLibraryError) as caught: action()
                self.assertNotIn("PRIVATE_COMMIT_ERROR", str(caught.exception))
            self.assertEqual(self.library.list(), (entry,))
            self.assertEqual(self.library.load(entry.id).name, "Luna")

    def test_storage_limit_fails_without_discarding_an_existing_role(self):
        entry = self.library.save(card())
        with patch("desktop_pet.character_library.MAX_LIBRARY_ENTRIES", 1):
            self.assertEqual(self.library.save(card()), entry)
            with self.assertRaises(CharacterLibraryError): self.library.save(card("New"))
        self.assertEqual(self.library.list(), (entry,))

    def test_settings_facade_is_isolated_and_library_delete_preserves_active_role(self):
        ini = QSettings(str(self.root / "owned.ini"), QSettings.Format.IniFormat)
        settings = SettingsManager(ini, ForbiddenCredentials())
        selected = card()
        settings.save_character_card(selected)
        entry = settings.save_library_character_card(selected)
        self.assertEqual(settings.list_character_cards(), (entry,))
        self.assertEqual(settings.load_library_character_card(entry.id).to_dict(), selected.to_dict())
        self.assertEqual(settings.rename_library_character_card(entry.id, "Shelf").label, "Shelf")
        self.assertTrue(settings.delete_library_character_card(entry.id))
        self.assertEqual(settings.load_character_card().to_dict(), selected.to_dict())
        self.assertTrue(self.path.is_file())
        with self.assertRaises(SettingsError): settings.load_library_character_card(entry.id)


if __name__ == "__main__":
    unittest.main()
