"""Host retry guidance survives native and shipped React presentation."""
import json
import unittest

from PySide6.QtCore import QEvent, QThreadPool, Qt
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from desktop_pet.ui.chat_panel import ChatPanel
from tests.test_agent_failure_ui import FailedTaskChat
from tests.test_assistant_ui import eval_js, wait_js, wait_until
from tests.test_chat_controller import MemorySettings
from tests import test_host_queue_review as queue_fixture


class NoProvider:
    def __init__(self):
        self.calls = 0

    def complete_context(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("NO_PROVIDER_CALL_ALLOWED")

    complete = complete_context

    def shutdown(self):
        pass


class QueueRetryGuidanceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.fixture = queue_fixture.HostQueueIndependentReviewTests(methodName="runTest")
        self.fixture.setUp()
        self.ack = self.fixture.admit(text="Owned task blocked before worker dispatch")
        self.fixture.policy["key"] = "DIFFERENT_OWNED_POLICY_KEY"
        self.fixture.coordinator.pump()
        self.provider = NoProvider()
        self.chat = ChatController(MemorySettings(), self.provider, store=self.fixture.store,
                                   metadata_client_factory=lambda: None)
        wait_until(lambda: not self.chat.history_loading and not self.chat._runner._tasks)
        self.chat.open_conversation(self.fixture.cid)
        wait_until(lambda: not self.chat.history_loading and any(
            message["id"] == self.ack["message_id"] for message in self.chat.messages))
        self.guidance = self.chat.message_retry_state(self.fixture.cid, self.ack["message_id"])["status"]
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            if isinstance(panel, AssistantChatPanel):
                panel._shutdown_image_staging()
            panel.close()
        self.chat.shutdown()
        wait_until(lambda: not QThreadPool.globalInstance().activeThreadCount()
                   and (getattr(self.chat, "_local_draft_owner", None) is None
                        or self.chat._local_draft_owner._stopped.is_set()), timeout=15)
        for panel in self.panels:
            panel.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.fixture.starts, [])
        self.fixture.doCleanups()

    def panel(self, chat=None):
        panel = AssistantChatPanel(chat or self.chat)
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        panel.resize(980, 760)
        self.panels.append(panel)
        panel.show()
        wait_until(lambda: panel._frontend_ready, timeout=25)
        wait_js(panel, "Boolean(document.querySelector('[data-testid=message-request-state]'))")
        return panel

    def test_native_blocked_task_displays_exact_host_guidance(self):
        panel = ChatPanel(self.chat)
        self.panels.append(panel)
        panel._render_messages()
        self.assertIn(self.guidance, panel.transcript.toPlainText())
        self.assertNotIn("请先检查已完成的操作", panel.transcript.toPlainText())
        self.assertNotIn("aura-retry:", panel.transcript.toHtml())

    def test_compiled_blocked_task_displays_exact_host_guidance(self):
        panel = self.panel()
        text = eval_js(panel, "document.querySelector('[data-testid=message-request-state]').innerText")
        self.assertIn(self.guidance, text)
        self.assertNotIn("请先检查已完成的操作", text)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('[data-testid=message-request-state] button').length"), 0)

    def test_no_action_banner_uses_primary_guidance_once(self):
        panel = self.panel()
        primary = "请查看排队任务的状态，或发送新的任务消息。"
        self.chat._set_error(primary, "", self.fixture.cid, self.ack["message_id"])
        wait_js(panel, "Boolean(document.querySelector('.error-banner'))")
        wait_js(panel, "document.querySelector('.error-banner').innerText.includes(" + json.dumps(primary) + ")")
        self.assertEqual(eval_js(panel, "document.querySelectorAll('.error-banner small.retry-explanation').length"), 0)
        self.assertEqual(eval_js(panel, "document.querySelector('.error-banner').innerText"), primary)

    def test_retry_banner_identical_guidance_is_not_duplicated(self):
        panel = self.panel()
        for primary in (self.guidance, "已停止等待回复。" + self.guidance):
            with self.subTest(primary=primary):
                self.chat._set_error(primary, "retry", self.fixture.cid, self.ack["message_id"])
                wait_js(panel, "Boolean(document.querySelector('.error-banner'))")
                wait_js(panel, "document.querySelector('.error-banner').innerText.includes(" + json.dumps(primary) + ")")
                self.assertEqual(eval_js(panel, "document.querySelectorAll('.error-banner small.retry-explanation').length"), 0)
                self.assertEqual(eval_js(panel, "document.querySelector('.error-banner').innerText").count(self.guidance), 1)

    def test_retry_banner_distinct_host_status_remains_visible(self):
        panel = self.panel()
        self.chat._set_error("服务端请求已中断。", "retry", self.fixture.cid, self.ack["message_id"])
        wait_js(panel, "Boolean(document.querySelector('.error-banner small.retry-explanation'))")
        self.assertEqual(eval_js(panel, "document.querySelector('.error-banner small.retry-explanation').innerText"), self.guidance)

    def test_native_started_agent_retains_host_replay_warning(self):
        chat = FailedTaskChat("timeout", started=True)
        warning = chat.message_retry_state(chat.current_conversation_id, chat.messages[0]["id"])["status"]
        panel = ChatPanel(chat)
        self.panels.append(panel)
        panel._render_messages()
        self.assertIn(warning, panel.transcript.toPlainText())
        self.assertNotIn("aura-retry:", panel.transcript.toHtml())

    def test_compiled_started_agent_retains_host_replay_warning(self):
        chat = FailedTaskChat("timeout", started=True)
        warning = chat.message_retry_state(chat.current_conversation_id, chat.messages[0]["id"])["status"]
        panel = self.panel(chat)
        text = eval_js(panel, "document.querySelector('[data-testid=message-request-state]').innerText")
        self.assertIn(warning, text)
        self.assertEqual(eval_js(panel, "document.querySelectorAll('[data-testid=message-request-state] button').length"), 0)


if __name__ == "__main__":
    unittest.main()
