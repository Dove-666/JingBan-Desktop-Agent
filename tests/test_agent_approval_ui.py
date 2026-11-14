"""Approval choices through the shipped React/Qt bridge using only local doubles."""

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Signal
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.chat_panel import ChatPanel
from tests.test_agent_mode_ui import ModeChat
from tests.test_assistant_ui import click_dom, eval_js, make_panel, set_dom_text, wait_js


class ApprovalChat(ModeChat):
    agent_settings_changed = Signal()

    def __init__(self):
        super().__init__()
        self.agent_approval_mode = "auto"
        self.approval_calls = []
        self.reject_approval = False

    def set_agent_approval_mode(self, mode):
        self.approval_calls.append(mode)
        if self.busy or self.history_loading or mode not in ("ask", "auto", "full") or self.reject_approval:
            return False
        self.agent_approval_mode = mode
        self.agent_settings_changed.emit()
        return True


class AgentApprovalUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = ApprovalChat()
        self.panel = make_panel(self.chat)
        self.extra_panels = []

    def tearDown(self):
        for panel in [self.panel, *self.extra_panels]:
            panel.hide()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def enable_agent(self):
        click_dom(self.panel, "[data-testid=chat-mode-agent]")
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=agent-approval-trigger]'))")

    def open_menu(self):
        click_dom(self.panel, "[data-testid=agent-approval-trigger]")
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=agent-approval-menu]'))")

    def key(self, key):
        eval_js(self.panel, "document.activeElement.dispatchEvent(new KeyboardEvent('keydown', "
                + json.dumps({"key": key, "bubbles": True, "cancelable": True}) + "))")

    def test_hidden_in_chat_and_bridge_rejects_stale_invalid_and_busy_changes(self):
        action = self.panel.dispatch_web_action
        self.assertFalse(eval_js(self.panel, "Boolean(document.querySelector('[data-testid=agent-approval-trigger]'))"))
        self.assertFalse(action("setAgentApprovalMode", {"conversationId": "current", "mode": "full"}))
        self.enable_agent()
        for payload in ({"conversationId": "old", "mode": "full"},
                        {"conversationId": "current", "mode": "unknown"},
                        {"conversationId": "current", "mode": True},
                        {"conversationId": "current", "mode": "full", "extra": 1}):
            self.assertFalse(action("setAgentApprovalMode", payload))
        self.chat.busy = True
        self.chat.busy_changed.emit(True)
        wait_js(self.panel, "document.querySelector('[data-testid=agent-approval-trigger]')?.disabled === true")
        self.assertFalse(action("setAgentApprovalMode", {"conversationId": "current", "mode": "full"}))
        self.assertEqual(self.chat.approval_calls, [])
        self.assertEqual(self.chat.agent_approval_mode, "auto")

    def test_selection_updates_shared_mode_without_replacing_draft_or_sending(self):
        self.enable_agent()
        set_dom_text(self.panel, "#message-input", "尚未发送的草稿")
        eval_js(self.panel, "(() => {window.qaApprovalEditor=document.querySelector('#message-input'); return true;})()")
        self.open_menu()
        click_dom(self.panel, "[data-testid=agent-approval-full]")
        wait_js(self.panel, "document.querySelector('[data-testid=agent-approval-trigger]')?.dataset.mode === 'full'")
        self.assertEqual(self.chat.agent_approval_mode, "full")
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"), "尚未发送的草稿")
        self.assertTrue(eval_js(self.panel, "window.qaApprovalEditor === document.querySelector('#message-input')"))
        self.assertEqual(self.panel.web_state()["drafts"]["current"], "尚未发送的草稿")
        self.assertEqual(self.chat.sent, [])
        self.open_menu()
        self.assertEqual(eval_js(self.panel, "document.querySelector('[aria-checked=true][role=menuitemradio]').dataset.mode"), "full")

    def test_failed_save_keeps_previous_mode_and_draft(self):
        self.enable_agent()
        set_dom_text(self.panel, "#message-input", "保存失败也要保留")
        self.chat.reject_approval = True
        self.open_menu()
        click_dom(self.panel, "[data-testid=agent-approval-full]")
        wait_js(self.panel, "Boolean(document.querySelector('.agent-approval-error'))")
        self.assertEqual(eval_js(self.panel, "document.querySelector('[data-testid=agent-approval-trigger]').dataset.mode"), "auto")
        self.assertEqual(eval_js(self.panel, "document.querySelector('#message-input').value"), "保存失败也要保留")
        self.assertEqual(self.chat.agent_approval_mode, "auto")
        self.assertEqual(self.chat.sent, [])

    def test_keyboard_navigation_and_escape_restore_trigger_focus_without_changes(self):
        self.enable_agent()
        self.open_menu()
        wait_js(self.panel, "document.activeElement.dataset.testid === 'agent-approval-auto'")
        for key, target in (("ArrowDown", "full"), ("ArrowDown", "ask"), ("End", "full"), ("Home", "ask")):
            self.key(key)
            self.assertEqual(eval_js(self.panel, "document.activeElement.dataset.testid"), "agent-approval-" + target)
        self.key("Escape")
        wait_js(self.panel, "!document.querySelector('[data-testid=agent-approval-menu]')")
        self.assertEqual(eval_js(self.panel, "document.activeElement.dataset.testid"), "agent-approval-trigger")
        self.assertEqual(self.chat.approval_calls, [])

    def test_light_and_dark_menu_fit_narrow_view_and_busy_closes_open_menu(self):
        self.enable_agent()
        theme = {"value": "light"}
        original = self.panel.web_state
        with patch.object(self.panel, "web_state", side_effect=lambda: {**original(), "theme": theme["value"]}):
            for mode in ("light", "dark"):
                theme["value"] = mode
                self.panel._queue_web_state()
                wait_js(self.panel, f"document.documentElement.dataset.theme === '{mode}'")
                for width in (420, 1000):
                    self.panel.resize(width, 720)
                    self.app.processEvents()
                    wait_js(self.panel, f"innerWidth <= {width}")
                    self.open_menu()
                    bounds = eval_js(self.panel, """(() => {
                        const menu=document.querySelector('.agent-approval-menu'), box=menu.getBoundingClientRect();
                        return {fits:box.left>=0 && box.right<=innerWidth && box.top>=0 && box.bottom<=innerHeight,
                            background:getComputedStyle(menu).backgroundColor,
                            color:getComputedStyle(menu).color,
                            overflow:document.documentElement.scrollWidth>innerWidth};
                    })()""")
                    self.assertTrue(bounds["fits"], (mode, width, bounds))
                    self.assertFalse(bounds["overflow"], (mode, width, bounds))
                    self.assertNotEqual(bounds["background"], "rgba(0, 0, 0, 0)")
                    self.assertNotEqual(bounds["background"], bounds["color"])
                    self.key("Escape")
        self.open_menu()
        self.chat.busy = True
        self.chat.busy_changed.emit(True)
        wait_js(self.panel, "!document.querySelector('[data-testid=agent-approval-menu]')")
        self.assertEqual(self.chat.approval_calls, [])

    def test_native_fallback_shares_mode_keeps_draft_and_disables_while_busy(self):
        panel = ChatPanel(self.chat)
        self.extra_panels.append(panel)
        self.assertTrue(panel.agent_approval_selector.isHidden())
        panel.agent_approval_selector.setCurrentIndex(panel.agent_approval_selector.findData("full"))
        self.assertEqual(self.chat.approval_calls, [])
        self.assertEqual(panel.agent_approval_selector.currentData(), "auto")
        self.chat.set_agent_mode(True)
        panel.input.setPlainText("native draft")
        panel.agent_approval_selector.setCurrentIndex(panel.agent_approval_selector.findData("ask"))
        self.assertEqual(self.chat.agent_approval_mode, "ask")
        self.assertEqual(panel.input.toPlainText(), "native draft")
        wait_js(self.panel, "document.querySelector('[data-testid=agent-approval-trigger]')?.dataset.mode === 'ask'")
        self.chat.busy = True
        self.chat.busy_changed.emit(True)
        self.assertFalse(panel.agent_approval_selector.isEnabled())
        self.assertEqual(self.chat.sent, [])


if __name__ == "__main__":
    unittest.main()
