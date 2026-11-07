"""Persisted Agent failures stay readable without replaying real actions."""
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.agents.service import _ERROR_MESSAGES
from desktop_pet.ui.chat_panel import ChatPanel
from tests.test_assistant_ui import AssistantUiChat, click_dom, eval_js, make_panel, wait_js


class FailedTaskChat(AssistantUiChat):
    def __init__(self, code="timeout", *, started=True):
        super().__init__()
        self.started = started
        self.retry_calls = []
        self.messages = [{"id": "failed-task", "role": "user", "source": "user",
                          "content": "打开抖音并给主页第一个视频点赞",
                          "metadata": {"request_status": "failed"}}]
        if code is not None:
            self.messages[0]["metadata"].update(
                agent_error_code=code, agent_error="PRIVATE_PROVIDER_EXCEPTION",
                agent_execution_started=started, execution_mode="agent")
        self.error = ""  # The old global banner has already been dismissed.

    def message_retry_state(self, conversation_id, message_id):
        return {"available": not self.started,
                "status": "该任务已启动；请检查已完成的操作，再发新的任务消息。" if self.started else ""}

    def retry(self, *args):
        self.retry_calls.append(args)
        return not self.started


class AgentFailureUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def web(self, chat):
        panel = make_panel(chat)
        self.panels.append(panel)
        wait_js(panel, "Boolean(document.querySelector('[data-testid=message-request-state]'))")
        return panel

    def test_timeout_reason_survives_reopening_without_disabled_retry_or_provider_text(self):
        chat = FailedTaskChat()
        for _ in range(2):
            panel = self.web(chat)
            state = eval_js(panel, "document.querySelector('[data-testid=message-request-state]').textContent")
            self.assertIn("任务超时", state)
            self.assertIn(_ERROR_MESSAGES["timeout"], state)
            self.assertIn(chat.message_retry_state(chat.current_conversation_id, chat.messages[0]["id"])["status"], state)
            self.assertNotIn("回复失败", state)
            self.assertNotIn("PRIVATE_PROVIDER_EXCEPTION", json.dumps(panel.web_state()))
            self.assertEqual(eval_js(panel, "document.querySelectorAll('[data-testid=message-request-state] button').length"), 0)
            self.assertEqual(chat.retry_calls, [])
            panel.hide()
        output = Path(__file__).resolve().parents[1] / "artifacts" / "agent_timeout_20261101"
        output.mkdir(parents=True, exist_ok=True)
        panel.resize(980, 680)
        panel.show()
        self.app.processEvents()
        wait_js(panel, "innerWidth > 900 && Boolean(document.querySelector('[data-testid=agent-message-error]'))")
        QTest.qWait(300)
        self.assertTrue(panel.web_view.grab().save(str(output / "timeout-message.png")))

    def test_other_known_agent_failure_uses_host_reason(self):
        chat = FailedTaskChat("gui_model_failed")
        panel = self.web(chat)
        state = eval_js(panel, "document.querySelector('[data-testid=message-request-state]').textContent")
        self.assertIn("任务未完成", state)
        self.assertIn(_ERROR_MESSAGES["gui_model_failed"], state)

    def test_unknown_agent_error_is_not_forwarded_as_raw_diagnostic(self):
        chat = FailedTaskChat("PRIVATE_UNRECOGNIZED_CODE")
        panel = self.web(chat)
        state = json.dumps(panel.web_state())
        for private in ("PRIVATE_UNRECOGNIZED_CODE", "PRIVATE_PROVIDER_EXCEPTION"):
            self.assertNotIn(private, state)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('[data-testid=agent-message-error]').length"), 0)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('[data-testid=message-request-state] button').length"), 0)

    def test_ordinary_chat_failure_keeps_existing_retry(self):
        chat = FailedTaskChat(None, started=False)
        panel = self.web(chat)
        state = eval_js(panel, "document.querySelector('[data-testid=message-request-state]').textContent")
        self.assertIn("回复失败", state)
        self.assertNotIn("任务超时", state)
        click_dom(panel, "[data-testid=message-request-state] button")
        self.assertEqual(len(chat.retry_calls), 1)

    def test_native_timeout_and_unknown_codes_use_same_safe_projection(self):
        chat = FailedTaskChat()
        panel = ChatPanel(chat)
        self.panels.append(panel)
        panel._render_messages()
        text = panel.transcript.toPlainText()
        self.assertIn("任务超时", text)
        self.assertIn(_ERROR_MESSAGES["timeout"], text)
        self.assertNotIn("回复失败", text)
        self.assertNotIn("重试回复", text)
        chat.messages = deepcopy(chat.messages)
        chat.messages[0]["metadata"]["agent_error_code"] = "PRIVATE_UNRECOGNIZED_CODE"
        panel._render_messages()
        text = panel.transcript.toPlainText()
        self.assertNotIn("PRIVATE_UNRECOGNIZED_CODE", text)
        self.assertNotIn("PRIVATE_PROVIDER_EXCEPTION", text)
        self.assertNotIn("任务超时", text)


if __name__ == "__main__":
    unittest.main()
