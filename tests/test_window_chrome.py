"""Frameless-window interaction through Qt and the shipped WebEngine frontend.

All preferences, archives and character images use the temporary fixture. Mouse
events target synthetic Qt widgets/DOM nodes, never the physical system cursor.
"""

import json
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests import test_settings_navigation as navigation_fixture
from tests.test_application import spin_until
from tests.test_assistant_ui import click_dom, eval_js, set_dom_text, wait_js


class WindowChromeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    setUp = navigation_fixture.EmbeddedSettingsNavigationTests.setUp
    tearDown = navigation_fixture.EmbeddedSettingsNavigationTests.tearDown

    def open_ready_chat(self):
        self.session.open_chat_window()
        window = self.session.chat_window
        panel = window.chat_panel
        spin_until(lambda: panel._frontend_ready, timeout=20)
        wait_js(panel, "Boolean(document.querySelector('[data-testid=message-input]'))")
        return window, panel

    def pointer_event(self, panel, selector, event_type="pointerdown", button=0):
        expression = """(() => {
            const node = document.querySelector(SELECTOR);
            if (!node) throw new Error('Missing interaction target');
            const bounds = node.getBoundingClientRect();
            node.dispatchEvent(new PointerEvent(TYPE, {bubbles:true, cancelable:true,
                button:BUTTON, buttons:BUTTON === 0 ? 1 : 2, pointerId:1,
                pointerType:'mouse', isPrimary:true,
                clientX:bounds.left + bounds.width / 2,
                clientY:bounds.top + bounds.height / 2}));
            return true;
        })()""".replace("SELECTOR", json.dumps(selector)).replace("TYPE", json.dumps(event_type))
        return eval_js(panel, expression.replace("BUTTON", str(button)))

    def set_web_draft(self, panel):
        draft = "窗口切换以后，这段草稿也要留在这里。"
        set_dom_text(panel, "[data-testid=message-input]", draft)
        cid = self.session.chat.current_conversation_id
        spin_until(lambda: panel.web_state()["drafts"].get(cid) == draft)
        return cid, draft

    def assert_web_draft(self, panel, cid, draft):
        self.assertEqual(self.session.chat.current_conversation_id, cid)
        self.assertEqual(panel.web_state()["drafts"][cid], draft)
        self.assertEqual(eval_js(panel, "document.querySelector('[data-testid=message-input]').value"), draft)

    def seed_history_row(self, panel):
        cid = self.session.chat.current_conversation_id
        self.session.chat.store.append_message(
            cid, "user", "用于验证窗口顶部与侧栏菜单避让的合成会话。", "user", message_id="chrome-layout-user")
        self.session.chat.store.rename_conversation(
            cid, "很长的合成会话标题：窗口按钮不能挡住它，窄屏也要能打开完整的会话菜单。" * 3)
        self.assertTrue(self.session.chat.open_conversation(cid))
        self.session.chat.search_conversations("")
        spin_until(lambda: not self.session.chat.history_loading
                   and any(item["id"] == cid for item in self.session.chat.conversations))
        selector = "[data-conversation-id=" + json.dumps(cid) + "]"
        wait_js(panel, "Boolean(document.querySelector(" + json.dumps(selector) + "))")
        return selector

    def ensure_sidebar_open(self, panel):
        visible = """(() => {
            const node = document.querySelector('.sidebar');
            const r = node.getBoundingClientRect();
            return getComputedStyle(node).visibility !== 'hidden'
                && r.width > 0 && r.left >= -1;
        })()"""
        if not eval_js(panel, visible):
            click_dom(panel, "[data-testid=history-toggle]")
        wait_js(panel, visible)

    def focus_row_menu_trigger(self, panel, row_selector):
        self.ensure_sidebar_open(panel)
        link = row_selector + " .conversation-link"
        eval_js(panel, "(() => {document.querySelector(" + json.dumps(link) + ").focus(); return true;})()")
        trigger = row_selector + " .row-actions > button"
        wait_js(panel, "document.querySelector(" + json.dumps(trigger) + ").getBoundingClientRect().width > 0")
        return trigger

    def test_frameless_chat_has_reachable_native_window_controls(self):
        window, panel = self.open_ready_chat()
        self.assertTrue(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertFalse(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(panel.web_state()["windowChrome"])
        controls = window.chrome.controls
        self.assertTrue(controls.isVisible())
        self.assertEqual((controls.width(), controls.height()), (132, 36))
        self.assertTrue(window.rect().contains(controls.geometry()))
        for name, label in (("windowMinimize", "最小化"), ("windowMaximize", "最大化"),
                            ("windowClose", "关闭窗口")):
            button = controls.findChild(type(window.chrome.close_button), name)
            self.assertIsNotNone(button)
            self.assertEqual(button.accessibleName(), label)
            self.assertTrue(button.isVisible())

    def test_maximize_and_restore_preserve_webengine_and_unsent_draft(self):
        window, panel = self.open_ready_chat()
        web_view = panel.web_view
        cid, draft = self.set_web_draft(panel)
        window.chrome.maximize_button.click()
        self.app.processEvents()
        self.assertTrue(window.isMaximized())
        self.assertEqual(window.chrome.maximize_button.accessibleName(), "还原窗口")
        self.assertEqual(window.layout().contentsMargins().right(), 0)
        window.chrome.maximize_button.click()
        self.app.processEvents()
        self.assertFalse(window.isMaximized())
        self.assertEqual(window.chrome.maximize_button.accessibleName(), "最大化")
        self.assertGreater(window.layout().contentsMargins().right(), 0)
        self.assertIs(panel.web_view, web_view)
        self.assert_web_draft(panel, cid, draft)

    def test_minimize_close_and_reopen_keep_same_webengine_and_conversation(self):
        window, panel = self.open_ready_chat()
        web_view = panel.web_view
        cid, draft = self.set_web_draft(panel)
        window.chrome.minimize_button.click()
        self.app.processEvents()
        self.assertTrue(window.isMinimized())
        self.session.open_chat_window()
        self.app.processEvents()
        self.assertFalse(window.isMinimized())
        self.assertTrue(window.isVisible())
        window.chrome.close_button.click()
        self.assertFalse(window.isVisible())
        self.assertFalse(self.session._shutting_down)
        self.session.open_chat_window()
        self.app.processEvents()
        self.assertIs(self.session.chat_window, window)
        self.assertIs(panel.web_view, web_view)
        self.assert_web_draft(panel, cid, draft)

    def test_web_header_and_sidebar_route_drag_and_double_click_to_host(self):
        window, panel = self.open_ready_chat()
        with patch.object(window.chrome, "start_move", return_value=True) as move:
            self.pointer_event(panel, ".chat-header")
            spin_until(lambda: move.call_count == 1)
            self.pointer_event(panel, ".sidebar-top .wordmark")
            spin_until(lambda: move.call_count == 2)
        self.pointer_event(panel, ".chat-header", "dblclick")
        spin_until(window.isMaximized)
        self.pointer_event(panel, ".chat-header", "dblclick")
        spin_until(lambda: not window.isMaximized())
        self.assertIs(window.pages.currentWidget(), panel)

    def test_interactive_header_and_sidebar_controls_and_right_click_do_not_start_drag(self):
        window, panel = self.open_ready_chat()
        row_selector = self.seed_history_row(panel)
        trigger = self.focus_row_menu_trigger(panel, row_selector)
        with patch.object(window.chrome, "start_move", return_value=True) as move, \
                patch.object(window.chrome, "toggle_maximized") as maximize:
            self.pointer_event(panel, trigger + " svg")
            click_dom(panel, trigger)
            wait_js(panel, "Boolean(document.querySelector('.row-popover'))")
            self.pointer_event(panel, ".row-popover button")
            self.pointer_event(panel, trigger + " svg", "dblclick")
            click_dom(panel, trigger)
            wait_js(panel, "!document.querySelector('.row-popover')")
            self.pointer_event(panel, ".sidebar-controls button svg")
            click_dom(panel, ".sidebar-controls button")
            wait_js(panel, "Boolean(document.querySelector('[data-testid=conversation-search]'))")
            self.pointer_event(panel, ".chat-header", button=2)
            click_dom(panel, ".close-sidebar")
            wait_js(panel, "document.querySelector('[data-testid=history-toggle]').getBoundingClientRect().width > 0")
            self.pointer_event(panel, "[data-testid=history-toggle] svg")
            self.pointer_event(panel, "[data-testid=history-toggle] svg", "dblclick")
            click_dom(panel, "[data-testid=history-toggle]")
            self.ensure_sidebar_open(panel)
            self.app.processEvents()
            QTest.qWait(80)
            move.assert_not_called()
            maximize.assert_not_called()

    def test_minimal_header_and_sidebar_row_menu_remain_clear_of_native_caption(self):
        window, panel = self.open_ready_chat()
        row_selector = self.seed_history_row(panel)
        for width in (420, 580, 1060):
            with self.subTest(width=width):
                window.resize(width, 650)
                self.app.processEvents()
                wait_js(panel, f"innerWidth <= {width} && innerWidth >= {width - 16}")
                self.assertEqual(eval_js(panel, """document.querySelectorAll(
                    '.chat-header [data-testid=tools-button], .chat-header .new-conversation, '
                    + '.chat-header button[title*="新对话"]').length"""), 0)
                self.assertEqual(eval_js(panel, "document.querySelectorAll('.chat-header button:not([data-testid=history-toggle])').length"), 0)
                trigger = self.focus_row_menu_trigger(panel, row_selector)
                click_dom(panel, trigger)
                wait_js(panel, "Boolean(document.querySelector('.row-popover'))")
                labels = eval_js(panel, "Array.from(document.querySelectorAll('.row-popover button')).map(node => node.textContent)")
                self.assertEqual(len(labels), 5, "The sidebar row owns rename, delete, find, export and clear")
                for keyword in ("重命名", "删除", "查找", "导出", "清空"):
                    self.assertTrue(any(keyword in label for label in labels), f"Missing row action: {keyword}")
                boxes = eval_js(panel, """Array.from(document.querySelectorAll(
                    '.header-title, .chat-header > button, .sidebar-top button, .row-actions > button, .row-popover'))
                    .filter(node => getComputedStyle(node).visibility !== 'hidden' && node.getClientRects().length)
                    .map(node => {
                    const r=node.getBoundingClientRect();
                    return {kind:node.className,x:r.x,y:r.y,width:r.width,height:r.height};
                })""")
                offset = panel.web_view.mapTo(window, QPoint(0, 0))
                native = QRectF(window.chrome.controls.geometry())
                self.assertGreaterEqual(len(boxes), 5)
                self.assertTrue(any(box["kind"] == "header-title" for box in boxes))
                self.assertTrue(any("row-popover" in box["kind"] for box in boxes))
                for box in boxes:
                    web = QRectF(box["x"] + offset.x(), box["y"] + offset.y(),
                                 box["width"], box["height"])
                    self.assertFalse(native.intersects(web), f"Native caption obscures {box}")
                click_dom(panel, trigger)
                wait_js(panel, "!document.querySelector('.row-popover')")

    def test_settings_uses_same_caption_controls_and_returns_to_preserved_web_chat(self):
        window, panel = self.open_ready_chat()
        web_view = panel.web_view
        cid, draft = self.set_web_draft(panel)
        self.session.open_settings("general")
        page = window.settings_page
        self.assertTrue(window.chrome.controls.isVisible())
        center = window.chrome.controls.mapTo(window, window.chrome.close_button.geometry().center())
        self.assertIs(window.childAt(center), window.chrome.close_button)
        with patch.object(window.chrome, "start_move", return_value=True) as move:
            QTest.mousePress(page.section_title, Qt.MouseButton.LeftButton)
            QTest.mouseRelease(page.section_title, Qt.MouseButton.LeftButton)
            self.assertEqual(move.call_count, 1)
            page.back_button.click()
            self.assertEqual(move.call_count, 1)
        self.assertIs(window.pages.currentWidget(), panel)
        self.assertIs(panel.web_view, web_view)
        self.assertTrue(window.chrome.controls.isVisible())
        self.assert_web_draft(panel, cid, draft)


if __name__ == "__main__":
    unittest.main()
