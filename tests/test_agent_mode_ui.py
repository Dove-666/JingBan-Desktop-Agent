"""Explicit desktop mode stays selected across turns and guards stale UI sends."""

import unittest

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.assistant_panel import AssistantChatPanel
from desktop_pet.ui.chat_panel import ChatPanel
from tests.test_session_ui import SessionChat


class ModeChat(SessionChat):
    def __init__(self):
        super().__init__()
        self.modes = {}
        self.sent = []

    @property
    def agent_mode(self):
        return self.modes.get(self.current_conversation_id, False)

    def set_agent_mode(self, enabled):
        if self.busy or self.history_loading:
            return False
        self.modes[self.current_conversation_id] = enabled
        self.conversation_changed.emit()
        return True

    def send(self, text, **options):
        self.sent.append((self.current_conversation_id, text, options))
        return True


class AgentModeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = ModeChat()
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            panel.close()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def panel(self, web=False):
        panel = (AssistantChatPanel if web else ChatPanel)(self.chat)
        self.panels.append(panel)
        return panel

    def test_native_agent_mode_is_explicit_persistent_and_per_conversation(self):
        panel = self.panel()
        panel.input.setPlainText("打开电脑上的文件")
        panel._send()
        self.assertEqual(self.chat.sent[-1][2], {"agent_mode": False})
        panel.agent_mode_selector.setCurrentIndex(1)
        for text in ("创建一份文档", "继续"):
            panel.input.setPlainText(text)
            panel._send()
            self.assertEqual(self.chat.sent[-1][2], {"agent_mode": True})
            self.assertEqual(panel.agent_mode_selector.currentIndex(), 1)
        self.chat.open_conversation("old")
        self.assertEqual(panel.agent_mode_selector.currentIndex(), 0)
        self.chat.open_conversation("current")
        self.assertEqual(panel.agent_mode_selector.currentIndex(), 1)

    def test_bridge_uses_selected_agent_mode_and_rejects_stale_choice(self):
        panel = self.panel(web=True)
        self.assertTrue(panel.web_state()["canAgentMode"])
        self.assertFalse(panel.dispatch_web_action("setAgentMode", {"conversationId": "old", "enabled": True}))
        self.assertTrue(panel.dispatch_web_action("setAgentMode", {"conversationId": "current", "enabled": True}))
        self.assertTrue(panel.web_state()["agentMode"])
        for invalid in (False, "true"):
            self.assertFalse(panel.dispatch_web_action("send", {
                "conversationId": "current", "text": "继续", "agentMode": invalid}))
        self.assertEqual(self.chat.sent, [])
        for text in ("创建一份文档", "继续"):
            self.assertTrue(panel.dispatch_web_action("send", {
                "conversationId": "current", "text": text, "agentMode": True}))
            self.assertEqual(self.chat.sent[-1][2], {"agent_mode": True})
            self.assertTrue(panel.web_state()["agentMode"])

    def test_mode_cannot_change_during_an_active_request(self):
        panel = self.panel(web=True)
        self.chat.set_agent_mode(True)
        self.chat.busy = True
        self.chat.busy_changed.emit(True)
        self.assertFalse(panel.agent_mode_selector.isEnabled())
        self.assertFalse(panel.dispatch_web_action("setAgentMode", {"conversationId": "current", "enabled": False}))
        self.assertTrue(self.chat.agent_mode)

    def test_rendered_selector_keeps_followups_in_agent_and_restores_mode(self):
        from tests.test_assistant_ui import click_dom, make_panel, set_dom_text, wait_js
        panel = make_panel(self.chat)
        self.panels.append(panel)
        wait_js(panel, "document.querySelector('[data-testid=chat-mode-chat]')?.getAttribute('aria-pressed') === 'true'")
        click_dom(panel, "[data-testid=chat-mode-agent]")
        wait_js(panel, "document.querySelector('[data-testid=chat-mode-agent]')?.getAttribute('aria-pressed') === 'true'")
        for text in ("创建一份文档", "继续"):
            set_dom_text(panel, "#message-input", text)
            click_dom(panel, "[data-testid=send-button]")
            wait_js(panel, "document.querySelector('#message-input').value === ''")
            self.assertEqual(self.chat.sent[-1][2], {"agent_mode": True})
        self.chat.open_conversation("old")
        wait_js(panel, "document.querySelector('[data-testid=chat-mode-chat]')?.getAttribute('aria-pressed') === 'true'")
        self.chat.open_conversation("current")
        wait_js(panel, "document.querySelector('[data-testid=chat-mode-agent]')?.getAttribute('aria-pressed') === 'true'")
        click_dom(panel, "[data-testid=chat-mode-chat]")
        wait_js(panel, "document.querySelector('[data-testid=chat-mode-chat]')?.getAttribute('aria-pressed') === 'true'")
        set_dom_text(panel, "#message-input", "打开电脑上的文件")
        click_dom(panel, "[data-testid=send-button]")
        wait_js(panel, "document.querySelector('#message-input').value === ''")
        self.assertEqual(self.chat.sent[-1][2], {"agent_mode": False})


if __name__ == "__main__":
    unittest.main()
