"""Automatic-expression controls use in-memory models without APIs or assets."""

import unittest

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QApplication, QPushButton

from desktop_pet.ui.debug_panel import DebugPanel
from tests.test_debug_ui import FakeChat, FakePet, FakeSettings


class AutomaticExpressionPet(FakePet):
    auto_expression_changed = Signal(bool)
    auto_expression_status_changed = Signal(str)

    def __init__(self, enabled=True):
        super().__init__()
        self.auto_expression_enabled = enabled
        self.auto_expression_status = "由 API 在已有请求中选择表情。" if enabled else "自动表情已关闭，可手动选择。"
        self.automatic_calls = []
        self.save_error = False

    def set_auto_expression_enabled(self, enabled):
        self.automatic_calls.append(enabled)
        self.auto_expression_enabled = enabled
        self.auto_expression_status = "由 API 在已有请求中选择表情。" if enabled else "自动表情已关闭，可手动选择。"
        self.auto_expression_changed.emit(enabled)
        self.auto_expression_status_changed.emit(self.auto_expression_status)
        if self.save_error:
            self.error_occurred.emit("自动表情已调整，但设置未能保存到本地。")


class AutomaticExpressionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.pet = AutomaticExpressionPet()
        self.chat = FakeChat()
        self.panels = []
        self.panel = self.make_panel()

    def make_panel(self, pet=None):
        panel = DebugPanel(pet or self.pet, FakeSettings(), self.chat)
        self.panels.append(panel)
        panel.show()
        self.app.processEvents()
        return panel

    def tearDown(self):
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_initial_enabled_and_accessible_control_do_not_call_setter_or_api(self):
        checkbox = self.panel.auto_expression_checkbox
        self.assertTrue(checkbox.isChecked())
        self.assertTrue(checkbox.isEnabled())
        self.assertEqual(checkbox.objectName(), "autoExpressionEnabled")
        self.assertEqual(checkbox.accessibleName(), "API按语境选择表情")
        self.assertEqual(self.panel.auto_expression_status.text(), self.pet.auto_expression_status)
        self.assertEqual(self.pet.automatic_calls, [])
        self.assertEqual(self.chat.test_calls, [])
        self.assertEqual(self.chat.messages, [])

    def test_saved_disabled_state_is_reflected_when_opening_another_panel(self):
        pet = AutomaticExpressionPet(False)
        panel = self.make_panel(pet)
        self.assertFalse(panel.auto_expression_checkbox.isChecked())
        self.assertIn("关闭", panel.auto_expression_status.text())
        self.assertEqual(pet.automatic_calls, [])

    def test_two_panels_share_toggle_state_without_reentrant_writes(self):
        other = self.make_panel()
        self.panel.auto_expression_checkbox.click()
        self.assertEqual(self.pet.automatic_calls, [False])
        self.assertFalse(other.auto_expression_checkbox.isChecked())
        self.assertEqual(other.auto_expression_status.text(), self.pet.auto_expression_status)
        other.auto_expression_checkbox.click()
        self.assertEqual(self.pet.automatic_calls, [False, True])
        self.assertTrue(self.panel.auto_expression_checkbox.isChecked())
        self.assertEqual(self.chat.test_calls, [])
        self.assertEqual(self.chat.messages, [])

    def test_external_model_signal_syncs_views_without_invoking_setter(self):
        other = self.make_panel()
        self.pet.auto_expression_enabled = False
        self.pet.auto_expression_changed.emit(False)
        self.assertFalse(self.panel.auto_expression_checkbox.isChecked())
        self.assertFalse(other.auto_expression_checkbox.isChecked())
        self.assertEqual(self.pet.automatic_calls, [])

    def test_manual_expression_remains_available_after_automatic_mode_is_off(self):
        other = self.make_panel()
        self.panel.auto_expression_checkbox.click()
        self.panel.expression_combo.setCurrentIndex(self.panel.expression_combo.findData("angry"))
        self.assertEqual(self.pet.expression, "angry")
        self.assertEqual(other.expression_combo.currentData(), "angry")
        self.panel.findChild(QPushButton, "testExpressionButton").click()
        self.assertEqual(self.pet.test_count, 1)
        self.assertEqual(self.pet.automatic_calls, [False])
        self.assertEqual(self.chat.test_calls, [])
        self.assertEqual(self.chat.messages, [])

    def test_failed_persistence_is_visible_without_reversing_runtime_toggle(self):
        other = self.make_panel()
        self.pet.save_error = True
        self.panel.auto_expression_checkbox.click()
        self.assertFalse(self.pet.auto_expression_enabled)
        for panel in (self.panel, other):
            self.assertFalse(panel.auto_expression_checkbox.isChecked())
            self.assertFalse(panel.pet_error_label.isHidden())
            self.assertIn("未能保存", panel.pet_error_label.text())
        self.assertEqual(self.pet.automatic_calls, [False])

    def test_status_is_plain_text_and_empty_status_hides_the_line(self):
        text = "<b>合成 API 状态</b>"
        self.pet.auto_expression_status_changed.emit(text)
        self.assertEqual(self.panel.auto_expression_status.text(), text)
        self.assertEqual(self.panel.auto_expression_status.textFormat(), Qt.TextFormat.PlainText)
        self.assertFalse(self.panel.auto_expression_status.isHidden())
        self.pet.auto_expression_status_changed.emit("")
        self.assertTrue(self.panel.auto_expression_status.isHidden())

    def test_legacy_controller_without_automatic_api_keeps_manual_controls(self):
        legacy = FakePet()
        panel = self.make_panel(legacy)
        self.assertTrue(panel.auto_expression_checkbox.isChecked())
        self.assertFalse(panel.auto_expression_checkbox.isEnabled())
        self.assertTrue(panel.auto_expression_status.isHidden())
        panel.expression_combo.setCurrentIndex(panel.expression_combo.findData("happy"))
        self.assertEqual(legacy.expression, "happy")


if __name__ == "__main__":
    unittest.main()
