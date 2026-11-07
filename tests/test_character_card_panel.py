"""Character-card settings with owned files and an in-memory controller."""

import base64
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog, QScrollArea, QVBoxLayout, QWidget

from desktop_pet.character_cards import CharacterCard
from desktop_pet.ui.character_card_panel import CharacterCardPanel
from desktop_pet.ui.styles import PANEL_STYLE
from desktop_pet.ui.theme import style_widget


class UnusedSettings:
    def load_character_card(self):
        raise AssertionError("The controller supplies the current character")

    def save_character_card(self, card):
        raise AssertionError("The controller owns persistence")

    def get_api_key(self):
        raise AssertionError("Role preview does not use model credentials")


class CharacterController(QObject):
    character_changed = Signal(object)

    def __init__(self, card=None):
        super().__init__()
        self.character_card = card
        self.saved = []
        self.refuse_save = False
        self.error = ""

    def set_character_card(self, card):
        self.saved.append(card)
        if self.refuse_save:
            return False
        self.character_card = card
        self.character_changed.emit(card)
        return True


class CharacterCardPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-character-card-panel-")
        self.root = Path(self.temporary.name)
        self.widgets = []
        self.guards = ExitStack()

        def forbidden(*args, **kwargs):
            raise AssertionError("Character panel tests use no process, network or external URL")

        for target in ((socket.socket, "connect"), (socket, "getaddrinfo"),
                       (subprocess, "Popen"), (QDesktopServices, "openUrl")):
            self.guards.enter_context(patch.object(*target, side_effect=forbidden))

    def tearDown(self):
        for panel in self.widgets:
            panel.close()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.guards.close()
        self.temporary.cleanup()

    def panel(self, card=None):
        controller = CharacterController(card)
        panel = CharacterCardPanel(UnusedSettings(), controller)
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        panel.resize(420, 760)
        panel.show()
        self.widgets.append(panel)
        self.app.processEvents()
        return panel, controller

    def card(self, name="合成角色", **fields):
        return CharacterCard.from_dict({"name": name, "description": "合成背景", **fields})

    def json_file(self, name="角色卡.json", **fields):
        path = self.root / name
        value = {"name": "导入角色", "personality": "耐心且有幽默感", "description": "合成背景",
                 "scenario": "一起整理书桌", "first_mes": "今天想聊些什么？", **fields}
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def import_file(self, panel, path):
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "角色卡")):
            panel.import_button.click()
        self.app.processEvents()

    def test_initial_current_card_and_default_state_are_controller_owned(self):
        panel, controller = self.panel()
        self.assertEqual(panel.name_label.text(), "鲸鱼娘")
        self.assertFalse(panel.apply_button.isEnabled())
        self.assertFalse(panel.reset_button.isEnabled())
        self.assertTrue(panel.status_label.isHidden())
        self.assertEqual(controller.saved, [])

        card = self.card("已有角色", personality="认真")
        existing, _ = self.panel(card)
        self.assertIn("已有角色", existing.current_label.text())
        self.assertIn("认真", existing.preview_text.toPlainText())
        self.assertFalse(existing.apply_button.isEnabled())
        self.assertTrue(existing.reset_button.isEnabled())

    def test_import_previews_fields_without_applying_or_changing_source_bytes(self):
        original = self.card("已有角色")
        panel, controller = self.panel(original)
        path = self.json_file()
        before = path.read_bytes()
        self.import_file(panel, path)
        self.assertEqual(controller.character_card, original)
        self.assertEqual(controller.saved, [])
        self.assertIn("已有角色", panel.current_label.text())
        self.assertEqual(panel.name_label.text(), "导入角色")
        for value in ("性格", "耐心且有幽默感", "背景", "合成背景", "场景", "一起整理书桌", "开场白"):
            self.assertIn(value, panel.preview_text.toPlainText())
        self.assertTrue(panel.apply_button.isEnabled())
        self.assertEqual(path.read_bytes(), before)

    def test_apply_saves_once_and_disables_duplicate_apply(self):
        panel, controller = self.panel()
        self.import_file(panel, self.json_file())
        panel.apply_button.click()
        self.assertEqual(len(controller.saved), 1)
        self.assertEqual(controller.character_card.name, "导入角色")
        self.assertIn("导入角色", panel.current_label.text())
        self.assertFalse(panel.apply_button.isEnabled())
        self.assertFalse(panel.status_label.property("error"))
        panel.apply_button.click()
        panel._apply_card()
        self.assertEqual(len(controller.saved), 1)

    def test_save_failure_keeps_current_card_and_pending_preview_for_retry(self):
        original = self.card("已有角色")
        panel, controller = self.panel(original)
        self.import_file(panel, self.json_file())
        controller.refuse_save = True
        panel.apply_button.click()
        self.assertEqual(controller.character_card, original)
        self.assertIn("已有角色", panel.current_label.text())
        self.assertEqual(panel.name_label.text(), "导入角色")
        self.assertTrue(panel.apply_button.isEnabled())
        self.assertTrue(panel.status_label.property("error"))
        controller.refuse_save = False
        panel.apply_button.click()
        self.assertEqual(controller.character_card.name, "导入角色")
        self.assertEqual(len(controller.saved), 2)

    def test_reset_commits_default_once_and_removes_loaded_preview(self):
        panel, controller = self.panel(self.card("已有角色"))
        self.import_file(panel, self.json_file())
        panel.reset_button.click()
        self.assertEqual(controller.saved, [None])
        self.assertIsNone(controller.character_card)
        self.assertEqual(panel.name_label.text(), "鲸鱼娘")
        self.assertFalse(panel.apply_button.isEnabled())
        self.assertFalse(panel.reset_button.isEnabled())
        self.assertTrue(panel.source_label.isHidden())
        panel._reset_card()
        self.assertEqual(controller.saved, [None])

    def test_public_budget_error_is_plain_bounded_and_preserves_pending_card(self):
        original = self.card("已有角色")
        panel, controller = self.panel(original)
        self.import_file(panel, self.json_file())
        controller.refuse_save = True
        controller.error = "系统提示词超过分配预算，请提高系统提示词预算。"
        panel.apply_button.click()
        self.assertEqual(panel.status_label.text(), controller.error)
        self.assertEqual(panel.status_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(controller.character_card, original)
        self.assertEqual(panel.name_label.text(), "导入角色")
        self.assertTrue(panel.apply_button.isEnabled())
        controller.error = "<b>公开错误</b>" + "合成错误说明" * 150
        panel.apply_button.click()
        self.assertLessEqual(len(panel.status_label.text()), 600)
        self.assertIn("<b>公开错误</b>", panel.status_label.text())
        self.assertTrue(panel.status_label.text().endswith("…"))
        controller.error = None
        panel.apply_button.click()
        self.assertIn("检查本地设置", panel.status_label.text())
        self.assertEqual(controller.character_card, original)

    def test_failed_reset_keeps_current_and_pending_card(self):
        original = self.card("已有角色")
        panel, controller = self.panel(original)
        self.import_file(panel, self.json_file())
        controller.refuse_save = True
        panel.reset_button.click()
        self.assertEqual(controller.character_card, original)
        self.assertEqual(panel.name_label.text(), "导入角色")
        self.assertIn("已有角色", panel.current_label.text())
        self.assertTrue(panel.reset_button.isEnabled())
        self.assertTrue(panel.status_label.property("error"))

    def test_cancel_and_invalid_import_preserve_pending_preview(self):
        panel, controller = self.panel(self.card("已有角色"))
        self.import_file(panel, self.json_file())
        before = (panel.name_label.text(), panel.preview_text.toPlainText(), panel.source_label.text())
        self.import_file(panel, "")
        self.assertEqual(before, (panel.name_label.text(), panel.preview_text.toPlainText(), panel.source_label.text()))
        invalid = self.root / "无效卡.json"
        invalid.write_text("{malformed", encoding="utf-8")
        self.import_file(panel, invalid)
        self.assertEqual(before, (panel.name_label.text(), panel.preview_text.toPlainText(), panel.source_label.text()))
        self.assertEqual(controller.saved, [])
        self.assertTrue(panel.status_label.property("error"))

    def test_imported_markup_stays_literal_and_details_default_collapsed(self):
        panel, controller = self.panel()
        markup = '<img src="file:///owned-not-opened.png"><script>example()</script>'
        self.import_file(panel, self.json_file(name="含标记.json", personality=markup,
            creator_notes=markup, mes_example="{{user}}：你好\n{{char}}：欢迎"))
        self.assertIn(markup, panel.preview_text.toPlainText())
        self.assertIn(markup, panel.details_text.toPlainText())
        self.assertTrue(panel.preview_text.isReadOnly())
        self.assertTrue(panel.details_text.isReadOnly())
        self.assertFalse(panel.details_section.isExpanded())
        self.assertFalse(panel.details_section.isHidden())
        for label in (panel.name_label, panel.current_label, panel.source_label, panel.status_label):
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        panel.details_section.button.click()
        self.assertTrue(panel.details_section.isExpanded())
        self.assertEqual(controller.saved, [])

    def test_external_change_refreshes_saved_preview_but_preserves_pending_import(self):
        panel, controller = self.panel(self.card("最初角色"))
        controller.set_character_card(self.card("外部角色"))
        self.assertEqual(panel.name_label.text(), "外部角色")
        self.import_file(panel, self.json_file())
        controller.set_character_card(self.card("另一个角色"))
        self.assertIn("另一个角色", panel.current_label.text())
        self.assertEqual(panel.name_label.text(), "导入角色")
        self.assertTrue(panel.apply_button.isEnabled())

    def test_png_picker_imports_metadata_without_applying(self):
        value = json.dumps({"name": "PNG角色", "description": "透明容器测试"}).encode()

        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
               + chunk(b"tEXt", b"chara\x00" + base64.b64encode(value))
               + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")) + chunk(b"IEND", b""))
        path = self.root / "角色.png"
        path.write_bytes(png)
        panel, controller = self.panel()
        self.import_file(panel, path)
        self.assertEqual(panel.name_label.text(), "PNG角色")
        self.assertTrue(panel.apply_button.isEnabled())
        self.assertEqual(controller.saved, [])

    def test_long_name_and_narrow_panel_keep_actions_inside_the_panel(self):
        panel, _ = self.panel(self.card("长名字" * 30, personality="合成性格\n" * 100))
        panel.resize(340, 760)
        self.app.processEvents()
        self.assertEqual(panel.width(), 340)
        for button in (panel.import_button, panel.apply_button, panel.reset_button):
            left = button.mapTo(panel, button.rect().topLeft()).x()
            self.assertGreaterEqual(left, 0)
            self.assertLessEqual(left + button.width(), panel.width())

    def test_embedded_card_stays_compact_after_details_and_window_resizing(self):
        window = QWidget()
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        style_widget(window, PANEL_STYLE)
        self.widgets.append(window)
        outer = QVBoxLayout(window)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        controller = CharacterController()
        panel = CharacterCardPanel(UnusedSettings(), controller)
        layout.addWidget(panel)
        layout.addStretch()
        self.import_file(panel, self.json_file(mes_example="示例对话", creator_notes="作者备注"))
        window.show()
        for width in (340, 600, 900, 340):
            with self.subTest(width=width):
                window.resize(width, 760)
                panel.details_section.setExpanded(True)
                self.app.processEvents()
                panel.details_section.setExpanded(False)
                self.app.processEvents()
                preview_bottom = panel.preview_text.mapTo(panel, panel.preview_text.rect().bottomLeft()).y()
                disclosure_top = panel.details_section.button.mapTo(panel, panel.details_section.button.rect().topLeft()).y()
                disclosure_bottom = panel.details_section.button.mapTo(panel, panel.details_section.button.rect().bottomLeft()).y()
                actions_top = panel.apply_button.mapTo(panel, panel.apply_button.rect().topLeft()).y()
                self.assertLessEqual(disclosure_top - preview_bottom, 40)
                self.assertLessEqual(actions_top - disclosure_bottom, 40)


if __name__ == "__main__":
    unittest.main()
