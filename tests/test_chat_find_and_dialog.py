"""Find within a saved conversation and protect modal keyboard interactions."""
import json
import unittest

from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests.test_assistant_ui import (
    AssistantUiChat, click_dom, eval_js, make_panel, set_dom_text, wait_js,
)


class ChatFindAndDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = AssistantUiChat()
        self.chat.messages = [
            {"id": f"turn-{index}", "role": "assistant",
             "content": (f"第{index}段。蓝鲸项目需要继续研究。" if index in (2, 19, 37)
                         else f"第{index}段。与查找关键词无关的文字。") + "较长的内容用于验证滚动位置。" * 5}
            for index in range(45)
        ]
        self.panel = make_panel(self.chat)

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def key(self, key, **options):
        return eval_js(self.panel, "(() => { const target=document.activeElement; target.dispatchEvent("
                       "new KeyboardEvent('keydown', " + json.dumps({"key": key, "bubbles": True,
                       "cancelable": True, **options}) + ")); return true; })()")

    def open_find(self, query="蓝鲸"):
        self.key("f", ctrlKey=True, shiftKey=True)
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=message-search]'))")
        set_dom_text(self.panel, "[data-testid=message-search]", query)

    def open_rename(self):
        click_dom(self.panel, "[data-conversation-id=current] .row-actions button")
        click_dom(self.panel, ".row-popover button")
        wait_js(self.panel, "Boolean(document.querySelector('dialog[open] #rename-title'))")

    def test_find_navigates_complete_conversation_wraps_and_preserves_draft(self):
        set_dom_text(self.panel, "#message-input", "还没发送的草稿")
        self.open_find()
        wait_js(self.panel, "document.querySelector('.message-find-count')?.textContent === '1 / 3'")
        wait_js(self.panel, "document.querySelector('.message-find-current')?.dataset.messageId === 'turn-2'")
        self.assertGreater(eval_js(self.panel, "document.querySelector('.thread-viewport').scrollTop"), 0)
        self.key("Enter")
        wait_js(self.panel, "document.querySelector('.message-find-current')?.dataset.messageId === 'turn-19'")
        click_dom(self.panel, "button[aria-label='下一条匹配消息']")
        wait_js(self.panel, "document.querySelector('.message-find-current')?.dataset.messageId === 'turn-37'")
        click_dom(self.panel, "button[aria-label='下一条匹配消息']")
        wait_js(self.panel, "document.querySelector('.message-find-current')?.dataset.messageId === 'turn-2'")
        click_dom(self.panel, "button[aria-label='上一条匹配消息']")
        wait_js(self.panel, "document.querySelector('.message-find-current')?.dataset.messageId === 'turn-37'")
        self.key("Escape")
        wait_js(self.panel, "!document.querySelector('[data-testid=message-search]')")
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"), "还没发送的草稿")
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(len(self.chat.messages), 45)

    def test_no_match_narrow_layout_and_query_are_literal(self):
        self.panel.resize(420, 520)
        wait_js(self.panel, "innerWidth === 420")
        self.open_find("[不当作正则.*]")
        wait_js(self.panel, "document.querySelector('.message-find-count')?.textContent === '无匹配'")
        self.assertTrue(eval_js(self.panel, "document.querySelector('button[aria-label=\"下一条匹配消息\"]').disabled"))
        self.assertEqual(eval_js(self.panel, "document.documentElement.scrollWidth"), 420)
        self.assertFalse(any(call[0] == "search" and call[1] for call in self.chat.calls))

    def test_find_closes_on_conversation_switch_and_does_not_replace_history_search(self):
        self.open_find()
        self.chat.open_conversation("old")
        wait_js(self.panel, "!document.querySelector('[data-testid=message-search]')")
        self.key("f", ctrlKey=True)
        wait_js(self.panel, "document.activeElement === document.querySelector('[data-testid=conversation-search]')")

    def test_rename_modal_blocks_background_shortcuts(self):
        set_dom_text(self.panel, "#message-input", "保留当前会话草稿")
        self.open_rename()
        self.key("n", ctrlKey=True)
        self.key("f", ctrlKey=True)
        self.key("f", ctrlKey=True, shiftKey=True)
        QTest.qWait(100)
        self.assertFalse(any(call[0] == "new" for call in self.chat.calls))
        self.assertTrue(eval_js(self.panel, "document.activeElement === document.querySelector('#rename-title')"))
        self.assertTrue(eval_js(self.panel, "Boolean(document.querySelector('dialog[open]'))"))
        self.assertFalse(eval_js(self.panel, "Boolean(document.querySelector('[data-testid=message-search]'))"))

    def test_rename_chinese_composition_enter_is_prevented(self):
        self.open_rename()
        prevented = eval_js(self.panel, """(() => {
            const field=document.querySelector('#rename-title');
            field.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:'鲸'}));
            const event=new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true,isComposing:true});
            field.dispatchEvent(event);
            field.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'鲸'}));
            return event.defaultPrevented;
        })()""")
        self.assertTrue(prevented)
        self.assertFalse(any(call[0] == "rename" for call in self.chat.calls))
        self.assertTrue(eval_js(self.panel, "Boolean(document.querySelector('dialog[open]'))"))

    def test_native_escape_cancel_and_save_restore_editor_focus_and_typing(self):
        for close_action in ("escape", "cancel", "save"):
            with self.subTest(close_action=close_action):
                self.open_rename()
                if close_action == "escape":
                    target = self.panel.web_view.focusProxy() or self.panel.web_view
                    QTest.keyClick(target, Qt.Key.Key_Escape)
                else:
                    click_dom(self.panel, "dialog .secondary-button" if close_action == "cancel" else "dialog .primary-button")
                wait_js(self.panel, "!document.querySelector('dialog[open]') && document.activeElement === document.querySelector('#message-input')")
                target = self.panel.web_view.focusProxy() or self.panel.web_view
                QTest.keyClicks(target, "xyz")
                wait_js(self.panel, "document.querySelector('#message-input').value.endsWith('xyz')")
                set_dom_text(self.panel, "#message-input", "")

    def test_native_tab_cycles_inside_narrow_rename_dialog(self):
        self.panel.resize(420, 520)
        wait_js(self.panel, "innerWidth === 420")
        click_dom(self.panel, "[data-testid=history-toggle]")
        self.open_rename()
        eval_js(self.panel, "(() => {document.querySelector('dialog .primary-button').focus(); return true;})()")
        target = self.panel.web_view.focusProxy() or self.panel.web_view
        QTest.keyClick(target, Qt.Key.Key_Tab)
        wait_js(self.panel, "document.activeElement === document.querySelector('dialog .dialog-heading button')")
        QTest.keyClick(target, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        wait_js(self.panel, "document.activeElement === document.querySelector('dialog .primary-button')")
        self.assertEqual(self.chat.sent, [])


if __name__ == "__main__":
    unittest.main()
