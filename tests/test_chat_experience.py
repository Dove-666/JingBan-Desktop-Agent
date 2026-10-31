"""Chat reading, navigation and dismissal without providers or persistent data."""

import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop_pet.ui.chat_panel import ChatPanel
from desktop_pet.ui.styles import PANEL_STYLE
from tests.test_session_ui import SessionChat


class ChatExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = SessionChat()
        self.panel = ChatPanel(self.chat)
        self.panel.setStyleSheet(PANEL_STYLE)
        self.panel.resize(900, 660)
        self.panel.show()
        self.panel.activateWindow()
        self.panel.input.setFocus()
        self.app.processEvents()

    def tearDown(self):
        self.panel.tools_menu.close()
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def shortcut(self, widget, key, modifier=Qt.KeyboardModifier.NoModifier):
        widget.setFocus()
        self.app.processEvents()
        QTest.keyClick(widget, key, modifier)
        self.app.processEvents()

    def test_ctrl_n_starts_independent_conversation_and_preserves_previous_draft(self):
        self.panel.input.setPlainText("原会话尚未发送的草稿")
        self.shortcut(self.panel.input, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.chat.calls[-1], ("new",))
        self.assertEqual(self.panel.input.toPlainText(), "")
        self.chat.open_conversation("current")
        self.assertEqual(self.panel.input.toPlainText(), "原会话尚未发送的草稿")

    def test_ctrl_f_expands_collapsed_history_and_selects_query(self):
        self.panel.resize(660, 560)
        self.app.processEvents()
        self.assertTrue(self.panel.sidebar.isHidden())
        self.panel.search_input.setText("之前的搜索词")
        self.shortcut(self.panel.input, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertFalse(self.panel.sidebar.isHidden())
        self.assertTrue(self.panel.search_input.hasFocus())
        self.assertEqual(self.panel.search_input.selectedText(), "之前的搜索词")

    def test_escape_returns_to_editor_without_erasing_query_draft_or_messages(self):
        self.panel.input.setPlainText("这条草稿不能丢")
        self.panel.search_input.setText("保留搜索")
        before = list(self.chat.messages)
        self.shortcut(self.panel.search_input, Qt.Key.Key_Escape)
        self.assertTrue(self.panel.input.hasFocus())
        self.assertEqual(self.panel.input.toPlainText(), "这条草稿不能丢")
        self.assertEqual(self.panel.search_input.text(), "保留搜索")
        self.assertEqual(self.chat.messages, before)
        self.assertFalse(any(call[0] in ("new", "delete") for call in self.chat.calls))

    def test_escape_dismisses_event_details_without_losing_context_message(self):
        self.chat.messages = [{"id": "event-message", "role": "assistant", "source": "proactive_screen",
                               "content": "这段报错可以一起看看。",
                               "screen_event": {"context_summary": "编辑器显示 Python 报错。"}}]
        self.chat.messages_changed.emit()
        self.panel.focus_message("event-message")
        self.assertFalse(self.panel.event_details.isHidden())
        self.shortcut(self.panel.input, Qt.Key.Key_Escape)
        self.assertTrue(self.panel.event_details.isHidden())
        self.assertEqual(len(self.chat.messages), 1)
        self.assertIn("来自屏幕感知", self.panel.transcript.toPlainText())

    def test_narrow_layout_hides_history_but_keeps_reading_and_manual_access(self):
        self.panel.resize(660, 520)
        self.app.processEvents()
        self.assertTrue(self.panel.sidebar.isHidden())
        self.assertGreater(self.panel.transcript.width(), 550)
        self.assertLessEqual(self.panel.minimumSizeHint().width(), 660)
        self.panel.history_toggle.click()
        self.app.processEvents()
        self.assertFalse(self.panel.sidebar.isHidden())
        self.assertGreater(self.panel.transcript.width(), 300)
        self.assertEqual(self.chat.current_conversation_id, "current")

    def test_automatic_history_returns_when_window_widens(self):
        self.panel.resize(660, 550)
        self.app.processEvents()
        self.assertTrue(self.panel.sidebar.isHidden())
        self.panel.resize(920, 660)
        self.app.processEvents()
        self.assertFalse(self.panel.sidebar.isHidden())
        self.assertEqual(self.panel.conversation_list.currentItem().data(Qt.ItemDataRole.UserRole), "current")

    def test_existing_tool_buttons_live_in_menu_and_clear_still_requires_confirmation(self):
        self.assertTrue(self.panel.clear_button.isHidden())
        self.panel.tools_menu.popup(self.panel.tools_button.mapToGlobal(self.panel.tools_button.rect().bottomLeft()))
        self.app.processEvents()
        self.assertTrue(self.panel.clear_button.isVisible())
        before = list(self.chat.messages)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirm:
            self.panel.clear_button.click()
        confirm.assert_called_once()
        self.assertEqual(self.chat.messages, before)
        self.assertFalse(self.panel.tools_menu.isVisible())

    def test_shared_memory_opener_is_used_without_creating_second_window(self):
        calls = []
        panel = ChatPanel(self.chat, memory_opener=lambda: calls.append("open-shared-memory"))
        try:
            self.assertTrue(panel.memory_button.isEnabled())
            panel.memory_button.click()
            panel.memory_button.click()
            self.assertEqual(calls, ["open-shared-memory", "open-shared-memory"])
            self.assertIsNone(panel.memory_panel)
        finally:
            panel.close()
            panel.deleteLater()

    def test_loading_and_empty_search_states_offer_next_action(self):
        self.chat.messages = []
        self.chat.conversations = []
        self.chat.history_loading = True
        self.chat.conversations_changed.emit()
        self.chat.history_busy_changed.emit(True)
        self.assertIn("正在读取这段对话", self.panel.transcript.toPlainText())
        self.assertIn("正在读取历史", self.panel.history_empty_label.text())
        self.chat.history_loading = False
        self.chat.history_busy_changed.emit(False)
        self.assertIn("今天想聊些什么", self.panel.transcript.toPlainText())
        self.panel.search_input.setText("找不到的词")
        self.chat.conversations_changed.emit()
        self.assertIn("没有找到匹配", self.panel.history_empty_label.text())
        self.assertIn("清除搜索", self.panel.history_empty_label.text())

    def test_reader_keeps_scroll_position_until_user_returns_to_latest(self):
        self.chat.messages = [{"id": f"message-{index}", "role": "assistant",
                               "content": "这是一段较长的聊天消息。\n可以继续阅读，换行正常。" * 3}
                              for index in range(35)]
        self.chat.messages_changed.emit()
        self.app.processEvents()
        scroll = self.panel.transcript.verticalScrollBar()
        self.assertGreater(scroll.maximum(), 500)
        scroll.setValue(120)
        self.chat.messages.append({"id": "new-message", "role": "assistant", "content": "一条新回复"})
        self.chat.messages_changed.emit()
        self.app.processEvents()
        self.assertEqual(scroll.value(), 120)
        scroll.setValue(scroll.maximum())
        self.chat.messages.append({"id": "latest-message", "role": "assistant", "content": "接下来一条"})
        self.chat.messages_changed.emit()
        self.app.processEvents()
        self.assertEqual(scroll.value(), scroll.maximum())

    def test_loading_blocks_new_shortcut_without_losing_draft(self):
        self.panel.input.setPlainText("加载时保留")
        self.chat.history_loading = True
        self.chat.history_busy_changed.emit(True)
        self.shortcut(self.panel.input, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        self.assertNotIn(("new",), self.chat.calls)
        self.assertEqual(self.panel.input.toPlainText(), "加载时保留")

    def test_local_reading_fonts_remain_large_under_shared_stylesheet(self):
        self.panel.ensurePolished()
        self.assertGreaterEqual(self.panel.transcript.font().pixelSize(), 16)
        self.assertGreaterEqual(self.panel.input.font().pixelSize(), 15)
        self.assertIn("估算输入", self.panel.tools_button.toolTip())

    def test_request_status_remains_visible_in_narrow_footer(self):
        self.panel.resize(660, 520)
        self.chat.busy = True
        self.chat.busy_changed.emit(True)
        self.app.processEvents()
        self.assertTrue(self.panel.status_label.isVisible())
        self.assertGreater(self.panel.status_label.width(), 180)
        self.assertIn("正在回复", self.panel.status_label.text())
        self.assertFalse(self.panel.send_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
