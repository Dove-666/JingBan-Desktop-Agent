"""Role-library flows with owned UI, files and a controller snapshot boundary."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox, QScrollArea, QVBoxLayout, QWidget

from desktop_pet.character_cards import CharacterCard
from desktop_pet.settings_manager import SettingsError, SettingsManager
from desktop_pet.ui.character_card_panel import CharacterCardPanel
from tests.test_character_card_panel import CharacterController


class LibrarySettings:
    def __init__(self):
        self.entries, self.cards = {}, {}
        self.loads, self.fail = [], None
        self.counter = 0

    def _check(self, method):
        if self.fail == method:
            raise SettingsError("角色库暂时无法保存，请检查目录权限。")

    def list_character_cards(self):
        self._check("list")
        return tuple(self.entries.values())

    def save_library_character_card(self, card, label=None):
        self._check("save")
        digest = hashlib.sha256(json.dumps(card.to_dict(), sort_keys=True).encode()).hexdigest()
        existing = next((entry for entry in self.entries.values() if entry.digest == digest), None)
        if existing:
            return existing
        self.counter += 1
        identity = "entry-" + str(self.counter)
        entry = SimpleNamespace(id=identity, label=label or card.name, name=card.name,
                                digest=digest, created_at="", updated_at="")
        self.entries[identity], self.cards[identity] = entry, card
        return entry

    def load_library_character_card(self, identity):
        self._check("load")
        self.loads.append(identity)
        return self.cards[identity]

    def rename_library_character_card(self, identity, label):
        self._check("rename")
        old = self.entries[identity]
        entry = SimpleNamespace(**{**vars(old), "label": label})
        self.entries[identity] = entry
        return entry

    def delete_library_character_card(self, identity):
        self._check("delete")
        self.cards.pop(identity, None)
        return self.entries.pop(identity, None) is not None


class CharacterLibraryPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-role-library-panel-")
        self.root = Path(self.temporary.name)
        self.widgets, self.guards = [], ExitStack()
        self.settings = LibrarySettings()
        def forbidden(*args, **kwargs):
            raise AssertionError("Role-library UI does not open network, processes or external URLs")
        for target in ((socket.socket, "connect"), (socket, "getaddrinfo"),
                       (subprocess, "Popen"), (QDesktopServices, "openUrl")):
            self.guards.enter_context(patch.object(*target, side_effect=forbidden))

    def tearDown(self):
        for widget in self.widgets:
            widget.close(); widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.guards.close(); self.temporary.cleanup()

    @staticmethod
    def card(name="海月", **fields):
        return CharacterCard.from_dict({"name": name, "description": "合成背景", **fields})

    def panel(self, active=None):
        controller = CharacterController(active)
        panel = CharacterCardPanel(self.settings, controller)
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        panel.resize(420, 950); panel.show()
        self.widgets.append(panel); self.app.processEvents()
        return panel, controller

    def select(self, panel, entry):
        panel.library_combo.setCurrentIndex(panel.library_combo.findData(entry.id))
        self.app.processEvents()

    def import_card(self, panel, card):
        path = self.root / "imported.json"
        path.write_text(json.dumps(card.to_dict()), encoding="utf-8")
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "角色卡")):
            panel.import_button.click()
        self.app.processEvents()

    def test_save_current_role_without_switching_and_duplicate_save_selects_same_entry(self):
        card = self.card()
        panel, controller = self.panel(card)
        panel.save_button.click(); panel.save_button.click()
        self.assertEqual(len(self.settings.entries), 1)
        self.assertEqual(controller.saved, [])
        self.assertEqual(controller.character_card, card)
        self.assertEqual(panel.library_combo.currentData(), next(iter(self.settings.entries)))

    def test_library_is_listed_without_loading_documents_until_selection(self):
        first = self.settings.save_library_character_card(self.card("第一角色"))
        second = self.settings.save_library_character_card(self.card("第二角色"))
        panel, controller = self.panel()
        self.assertEqual(self.settings.loads, [])
        self.select(panel, second)
        self.assertEqual(self.settings.loads, [second.id])
        self.assertEqual(panel.name_label.text(), "第二角色")
        self.assertEqual(controller.saved, [])
        panel.apply_button.click()
        self.assertEqual(controller.character_card.name, "第二角色")
        self.select(panel, first); panel.apply_button.click()
        self.assertEqual(controller.character_card.name, "第一角色")
        panel.reset_button.click()
        self.assertIsNone(controller.character_card)

    def test_import_then_save_does_not_apply_and_reopened_panel_can_select_it(self):
        panel, controller = self.panel(self.card("当前角色"))
        imported = self.card("导入角色", personality="耐心")
        self.import_card(panel, imported)
        panel.save_button.click()
        self.assertEqual(controller.saved, [])
        reopened, second_controller = self.panel(controller.character_card)
        entry = next(iter(self.settings.entries.values()))
        self.select(reopened, entry)
        self.assertEqual(reopened._preview_card, imported)
        reopened.apply_button.click()
        self.assertEqual(second_controller.character_card, imported)

    def test_rename_only_library_label_preserves_persona_and_unapplied_preview(self):
        card = self.card()
        entry = self.settings.save_library_character_card(card)
        panel, controller = self.panel()
        self.select(panel, entry)
        with patch.object(QInputDialog, "getText", return_value=("我的温柔角色", True)):
            panel.rename_button.click()
        self.assertEqual(panel.library_combo.currentText(), "我的温柔角色")
        self.assertEqual(panel.name_label.text(), card.name)
        self.assertEqual(self.settings.cards[entry.id], card)
        self.assertEqual(controller.saved, [])

    def test_delete_requires_confirmation_and_keeps_current_role(self):
        card = self.card()
        entry = self.settings.save_library_character_card(card)
        panel, controller = self.panel(card)
        self.select(panel, entry)
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.No):
            panel.delete_button.click()
        self.assertIn(entry.id, self.settings.entries)
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes):
            panel.delete_button.click()
        self.assertEqual(self.settings.entries, {})
        self.assertEqual(controller.character_card, card)
        self.assertEqual(controller.saved, [])
        self.assertEqual(panel.name_label.text(), card.name)

    def test_library_failures_leave_preview_and_current_role_intact(self):
        entry = self.settings.save_library_character_card(self.card("库中角色"))
        panel, controller = self.panel(self.card("当前角色"))
        self.select(panel, entry)
        for operation in ("save", "rename", "delete", "load"):
            with self.subTest(operation=operation):
                self.settings.fail = operation
                before = panel._preview_card
                if operation == "save": panel.save_button.click()
                elif operation == "rename":
                    with patch.object(QInputDialog, "getText", return_value=("新名称", True)):
                        panel.rename_button.click()
                elif operation == "delete":
                    with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes):
                        panel.delete_button.click()
                else:
                    panel.library_combo.setCurrentIndex(0)
                    self.select(panel, entry)
                self.assertEqual(panel._preview_card, before)
                self.assertEqual(controller.character_card.name, "当前角色")
                self.assertTrue(panel.status_label.property("error"))
                self.assertEqual(controller.saved, [])
                self.settings.fail = None

    def test_external_role_change_preserves_unapplied_import(self):
        panel, controller = self.panel(self.card("原角色"))
        imported = self.card("未应用角色")
        self.import_card(panel, imported)
        controller.set_character_card(self.card("外部角色"))
        self.assertEqual(panel._preview_card, imported)
        panel.save_button.click()
        self.assertEqual(next(iter(self.settings.cards.values())), imported)

    def test_library_open_failure_keeps_current_preview_and_retryable_save(self):
        self.settings.fail = "list"
        card = self.card("当前角色")
        panel, controller = self.panel(card)
        self.assertEqual(panel._preview_card, card)
        self.assertTrue(panel.status_label.property("error"))
        self.assertEqual(controller.saved, [])
        self.settings.fail = None
        panel.save_button.click()
        self.assertEqual(len(self.settings.entries), 1)
        self.assertFalse(panel.status_label.property("error"))

    def test_default_role_can_refresh_after_initial_library_read_failure(self):
        entry = self.settings.save_library_character_card(self.card("库中角色"))
        self.settings.fail = "list"
        panel, controller = self.panel()
        self.assertFalse(panel.library_combo.isEnabled())
        self.assertFalse(panel.save_button.isEnabled())
        self.settings.fail = None
        panel.refresh_button.click()
        self.assertTrue(panel.library_combo.isEnabled())
        self.assertNotEqual(panel.library_combo.findData(entry.id), -1)
        self.assertEqual(self.settings.loads, [])
        self.assertEqual(controller.saved, [])

    def test_refresh_preserves_unapplied_import_and_current_role(self):
        panel, controller = self.panel(self.card("当前角色"))
        imported = self.card("未保存角色")
        self.import_card(panel, imported)
        self.settings.save_library_character_card(self.card("新加入角色"))
        source = panel.source_label.text()
        panel.refresh_button.click()
        self.assertEqual(panel._preview_card, imported)
        self.assertEqual(panel.source_label.text(), source)
        self.assertEqual(self.settings.loads, [])
        self.assertEqual(controller.saved, [])

    def test_refresh_updates_saved_label_without_reloading_selected_card(self):
        entry = self.settings.save_library_character_card(self.card("库中角色"))
        panel, controller = self.panel()
        self.select(panel, entry)
        original = panel._preview_card
        self.settings.rename_library_character_card(entry.id, "新的库名称")
        panel.refresh_button.click()
        self.assertEqual(panel.library_combo.currentData(), entry.id)
        self.assertEqual(panel.library_combo.currentText(), "新的库名称")
        self.assertEqual(panel._preview_card, original)
        self.assertIn("新的库名称", panel.source_label.text())
        self.assertEqual(self.settings.loads, [entry.id])
        self.assertEqual(controller.saved, [])

    def test_apply_failure_keeps_selected_saved_role_for_retry(self):
        entry = self.settings.save_library_character_card(self.card("已保存角色"))
        panel, controller = self.panel(self.card("当前角色"))
        self.select(panel, entry)
        controller.refuse_save = True
        controller.error = "角色设定超出当前上下文预算。"
        panel.apply_button.click()
        self.assertEqual(panel.library_combo.currentData(), entry.id)
        self.assertEqual(panel._preview_card.name, "已保存角色")
        self.assertEqual(controller.character_card.name, "当前角色")
        self.assertTrue(panel.apply_button.isEnabled())
        self.assertEqual(panel.status_label.text(), controller.error)
        controller.refuse_save = False
        panel.apply_button.click()
        self.assertEqual(controller.character_card.name, "已保存角色")

    def test_real_preferences_library_roundtrip_keeps_active_ini_unchanged(self):
        path = self.root / "owned-settings.ini"
        preferences = SettingsManager(QSettings(str(path), QSettings.Format.IniFormat), credential_store=object())
        original = self.card("已应用角色")
        preferences.save_character_card(original)
        ini_before = path.read_bytes()
        self.settings = preferences
        panel, controller = self.panel(original)
        imported = self.card("保存角色", personality="认真且温柔")
        self.import_card(panel, imported)
        panel.save_button.click()
        self.assertEqual(path.read_bytes(), ini_before)
        self.assertEqual(controller.saved, [])
        self.settings = SettingsManager(QSettings(str(path), QSettings.Format.IniFormat), credential_store=object())
        reopened, second = self.panel(self.settings.load_character_card())
        entries = self.settings.list_character_cards()
        self.assertEqual(len(entries), 1)
        self.select(reopened, entries[0])
        self.assertEqual(reopened._preview_card, imported)
        reopened.apply_button.click()
        self.assertEqual(second.character_card, imported)

    def test_markup_names_and_delete_dialog_are_plain_text(self):
        name = '<img src="file:///not-opened.png">'
        entry = self.settings.save_library_character_card(self.card(name))
        panel, _ = self.panel()
        self.select(panel, entry)
        dialogs = []
        def cancel(box):
            dialogs.append((box.text(), box.textFormat()))
            return QMessageBox.StandardButton.No
        with patch.object(QMessageBox, "exec", cancel):
            panel.delete_button.click()
        self.assertIn(name, dialogs[0][0])
        self.assertEqual(dialogs[0][1], Qt.TextFormat.PlainText)
        self.assertEqual(panel.name_label.textFormat(), Qt.TextFormat.PlainText)

    def test_library_controls_fit_narrow_panel_and_long_saved_name(self):
        entry = self.settings.save_library_character_card(self.card("很长的角色名字" * 12))
        panel, _ = self.panel()
        self.select(panel, entry)
        for width in (340, 600, 900, 340):
            panel.resize(width, 1000); self.app.processEvents()
            self.assertEqual(panel.width(), width)
            for control in (panel.library_combo, panel.rename_button, panel.delete_button,
                            panel.refresh_button, panel.import_button, panel.save_button,
                            panel.apply_button, panel.reset_button):
                left = control.mapTo(panel, control.rect().topLeft()).x()
                self.assertGreaterEqual(left, 0)
                self.assertLessEqual(left + control.width(), panel.width())


if __name__ == "__main__":
    unittest.main()
