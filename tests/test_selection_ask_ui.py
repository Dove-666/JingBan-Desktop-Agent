"""Plain-text selection questions without capture, credentials or model APIs."""

import unittest

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from desktop_pet.selection.context import SelectionContext
from desktop_pet.ui.selection_ask_dialog import SelectionAskDialog


class SelectionAskDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.context = SelectionContext(
            text="<script>not executable</script>\n第二行合成选区",
            title="<b>合成网页标题</b>", url="https://example.invalid/page?private=discard",
            page_text="<img src='https://example.invalid/image'>\n合成页面背景",
            captured_at="2026-10-31T12:00:00+08:00", app_name="Synthetic Browser",
        )
        self.dialog = SelectionAskDialog(self.context)
        self.sent = []
        self.dialog.ask_requested.connect(lambda question, context: self.sent.append((question, context)))
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_plain_previews_source_and_privacy_notice_never_render_markup(self):
        self.assertEqual(self.dialog.selection_preview.toPlainText(), self.context.text)
        self.assertEqual(self.dialog.page_preview.toPlainText(), self.context.page_text)
        self.assertEqual(self.dialog.source_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("<b>", self.dialog.source_label.text())
        self.assertEqual(self.dialog.url_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertFalse(self.dialog.url_label.openExternalLinks())
        self.assertNotIn("private", self.dialog.url_label.text())
        self.assertIn("不启用连续截图", self.dialog.privacy_label.text())
        self.assertFalse(self.dialog.page_preview.isVisible())
        QTest.mouseClick(self.dialog.page_toggle, Qt.MouseButton.LeftButton)
        self.assertTrue(self.dialog.page_preview.isVisible())
        QTest.mouseClick(self.dialog.selection_toggle, Qt.MouseButton.LeftButton)
        self.assertFalse(self.dialog.selection_preview.isVisible())

    def test_nonempty_question_required_and_enter_emits_exact_original_context_once(self):
        self.assertFalse(self.dialog.send_button.isEnabled())
        self.dialog.input.setPlainText("  \n ")
        QTest.keyClick(self.dialog.input, Qt.Key.Key_Return)
        self.assertEqual(self.sent, [])
        self.dialog.input.setPlainText(" 请解释所选内容 ")
        self.assertTrue(self.dialog.send_button.isEnabled())
        QTest.keyClick(self.dialog.input, Qt.Key.Key_Return)
        QTest.keyClick(self.dialog.input, Qt.Key.Key_Return)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "请解释所选内容")
        self.assertIs(self.sent[0][1], self.context)
        self.assertTrue(self.dialog.isVisible(), "Only the accepting app hides the composer")
        self.assertFalse(self.dialog.send_button.isEnabled())

    def test_shift_enter_and_input_method_composition_do_not_send(self):
        self.dialog.input.setPlainText("问题")
        self.dialog.input.moveCursor(self.dialog.input.textCursor().MoveOperation.End)
        QTest.keyClick(self.dialog.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        self.assertIn("\n", self.dialog.input.toPlainText())
        self.assertEqual(self.sent, [])
        self.app.sendEvent(self.dialog.input, QInputMethodEvent("鲸", []))
        QTest.keyClick(self.dialog.input, Qt.Key.Key_Return)
        self.assertEqual(self.sent, [])
        self.app.sendEvent(self.dialog.input, QInputMethodEvent("", []))
        QTest.keyClick(self.dialog.input, Qt.Key.Key_Return)
        self.assertEqual(len(self.sent), 1)

    def test_shortcuts_only_fill_draft_and_error_retains_it_for_retry(self):
        for button in self.dialog.quick_buttons:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            self.assertTrue(self.dialog.input.toPlainText().strip())
            self.assertEqual(self.sent, [])
        draft = self.dialog.input.toPlainText()
        QTest.mouseClick(self.dialog.send_button, Qt.MouseButton.LeftButton)
        self.dialog.set_busy(True)
        self.assertTrue(self.dialog.input.isReadOnly())
        self.assertTrue(all(not button.isEnabled() for button in self.dialog.quick_buttons))
        self.dialog.show_error("<b>合成连接失败</b>")
        self.assertEqual(self.dialog.input.toPlainText(), draft)
        self.assertTrue(self.dialog.error_label.isVisible())
        self.assertEqual(self.dialog.error_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertTrue(self.dialog.send_button.isEnabled())
        self.assertFalse(self.dialog.input.isReadOnly())
        QTest.mouseClick(self.dialog.send_button, Qt.MouseButton.LeftButton)
        self.assertEqual(len(self.sent), 2)

    def test_dict_context_and_explicit_replacement_remain_scoped_to_the_snapshot(self):
        payload = {"text": "另一个选区", "title": "第二页", "page_text": "第二页背景"}
        self.dialog.set_context(payload)
        self.assertEqual(self.dialog.selection_preview.toPlainText(), "另一个选区")
        self.assertEqual(self.dialog.page_preview.toPlainText(), "第二页背景")
        self.assertFalse(self.dialog.url_label.isVisible())
        self.dialog.input.setPlainText("只解释第二页")
        self.dialog._send()
        self.assertIs(self.sent[0][1], payload)
        self.assertNotIn("合成网页", self.dialog.source_label.text())

    def test_page_extraction_notes_are_visible_plain_hints_not_errors(self):
        context = {"text": "合成选中文字", "notes": ["<b>网页正文暂时不可读</b>"]}
        self.dialog.set_context(context)
        self.assertTrue(self.dialog.notes_label.isVisible())
        self.assertEqual(self.dialog.notes_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.dialog.notes_label.text(), "<b>网页正文暂时不可读</b>")
        self.assertFalse(self.dialog.error_label.isVisible())
        self.dialog.set_context(self.context)
        self.assertFalse(self.dialog.notes_label.isVisible())

    def test_close_does_not_close_another_window_and_full_chat_is_explicit(self):
        pet = QWidget()
        pet.show()
        requests = []
        self.dialog.chat_requested.connect(lambda: requests.append(True))
        QTest.mouseClick(self.dialog.chat_button, Qt.MouseButton.LeftButton)
        self.assertEqual(requests, [True])
        self.assertEqual(self.sent, [])
        self.dialog.close()
        self.app.processEvents()
        self.assertTrue(pet.isVisible())
        self.assertEqual(self.sent, [])
        pet.close()
        pet.deleteLater()

    def test_small_window_keeps_question_and_send_reachable_by_scrolling_context(self):
        self.dialog.resize(400, 400)
        self.dialog.page_toggle.setChecked(True)
        self.app.processEvents()
        self.assertTrue(self.dialog.input.isVisible())
        self.assertTrue(self.dialog.send_button.isVisible())
        self.assertTrue(self.dialog.rect().contains(self.dialog.send_button.geometry()))
        self.assertGreater(self.dialog.context_scroll.verticalScrollBar().maximum(), 0)
        self.assertGreaterEqual(self.dialog.input.font().pixelSize(), 16)


if __name__ == "__main__":
    unittest.main()
