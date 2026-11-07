"""Queue-owned terminal messages never advertise an unsupported chat retry."""
from copy import deepcopy
from types import MethodType
import unittest
from unittest.mock import Mock

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import AGENT_REPLAY_STATUS, ChatController
from desktop_pet.image_storage import ImageStorageGate
from desktop_pet.ui.chat_panel import ChatPanel
from tests.test_assistant_ui import AssistantUiChat
from tests import test_host_queue_review as queue_fixture


QUEUE_GUIDANCE = "请查看原排队任务的状态，或发送新的任务消息。"


class QueueRetryAffordanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.fixture = queue_fixture.HostQueueIndependentReviewTests(methodName="runTest")
        self.fixture.setUp()
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            panel.close()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.fixture.doCleanups()

    def queue_message(self, mode):
        ack = self.fixture.admit(text="Owned task with no allowed worker launch")
        if mode == "cancelled":
            self.fixture.coordinator.cancel(self.fixture.cid, lambda cid: False)
        else:
            self.fixture.policy["key"] = "DIFFERENT_OWNED_SYNTHETIC_KEY"
            self.fixture.coordinator.pump()
        job = self.fixture.queue.get(ack["job_id"])
        message = self.fixture.store.read_message(self.fixture.cid, ack["message_id"])
        self.assertEqual(job.state, "cancelled" if mode == "cancelled" else "blocked")
        self.assertIsNotNone(job.worker_closed_at)
        self.assertEqual(self.fixture.starts, [])
        self.assertIn("agent_queue_origin", message["metadata"])
        self.assertNotIn("agent_execution_started", message["metadata"])
        return ack, message

    def chat(self, message):
        chat = AssistantUiChat()
        chat.current_conversation_id = chat._current_id = self.fixture.cid
        chat.messages = [deepcopy(message)]
        chat._requests = {}
        chat._recoverable_messages = {}
        chat._retry_messages = {}
        chat._unconfirmed_reply_commits = {}
        chat._closed = chat._busy = chat._history_loading = False
        chat._invalidating_conversations = set()
        chat._deleted_conversations = set()
        chat._image_storage_gate = ImageStorageGate()
        chat._image_storage_owner = chat._image_storage_gate.create_owner()
        chat._configuration = Mock(return_value=("owned-api", "owned-key", "owned-budget"))
        chat._begin_chat = Mock()
        for name in ("_recover_messages", "message_retry_state", "retry", "_retry_admitted", "_set_error",
                     "_image_storage_admission"):
            setattr(chat, name, MethodType(getattr(ChatController, name), chat))
        chat.notify_image_storage_changed = lambda: None
        chat._recover_messages(self.fixture.cid, chat.messages)
        return chat

    def ordinary_message(self, status):
        return {"id": "owned-ordinary-message", "role": "user", "content": "Owned chat question",
                "metadata": {"request_status": status}}

    def assert_queue_rejected(self, chat, message):
        state = chat.message_retry_state(self.fixture.cid, message["id"])
        self.assertEqual(state, {"available": False, "status": QUEUE_GUIDANCE})
        self.assertFalse(chat.retry(self.fixture.cid, message["id"]))
        chat._configuration.assert_not_called()
        chat._begin_chat.assert_not_called()
        self.assertEqual(chat._error, "请查看排队任务的状态，或发送新的任务消息。")
        self.assertEqual(chat._image_storage_gate.snapshot()["activity_count"], 0)

    def test_host_cancelled_before_dispatch_has_no_ordinary_retry(self):
        _, message = self.queue_message("cancelled")
        self.assert_queue_rejected(self.chat(message), message)

    def test_host_policy_blocked_before_dispatch_has_no_ordinary_retry(self):
        _, message = self.queue_message("blocked")
        self.assert_queue_rejected(self.chat(message), message)

    def test_real_native_link_disappears_for_host_queue_terminal_messages(self):
        for mode in ("cancelled", "blocked"):
            with self.subTest(mode=mode):
                _, message = self.queue_message(mode)
                chat = self.chat(message)
                panel = ChatPanel(chat)
                self.panels.append(panel)
                panel._render_messages()
                self.assertNotIn("aura-retry:", panel.transcript.toHtml())
                if mode == "cancelled":
                    self.assertIn(QUEUE_GUIDANCE, panel.transcript.toPlainText())

    def test_ordinary_failed_chat_retains_working_retry(self):
        message = self.ordinary_message("failed")
        chat = self.chat(message)
        self.assertEqual(chat.message_retry_state(self.fixture.cid, message["id"]), {"available": True, "status": ""})
        self.assertTrue(chat.retry(self.fixture.cid, message["id"]))
        self.assertEqual(chat._begin_chat.call_count, 1)
        self.assertEqual(chat._begin_chat.call_args.args[0]["id"], message["id"])

    def test_ordinary_cancelled_chat_retains_native_link_and_working_retry(self):
        message = self.ordinary_message("cancelled")
        chat = self.chat(message)
        panel = ChatPanel(chat)
        self.panels.append(panel)
        panel._render_messages()
        self.assertIn("aura-retry:", panel.transcript.toHtml())
        self.assertTrue(chat.retry(self.fixture.cid, message["id"]))
        self.assertEqual(chat._begin_chat.call_count, 1)

    def test_started_agent_protection_precedes_queue_guidance(self):
        _, message = self.queue_message("cancelled")
        message["metadata"]["agent_execution_started"] = True
        chat = self.chat(message)
        self.assertEqual(chat.message_retry_state(self.fixture.cid, message["id"]),
                         {"available": False, "status": AGENT_REPLAY_STATUS})
        self.assertFalse(chat.retry(self.fixture.cid, message["id"]))
        chat._begin_chat.assert_not_called()

    def test_active_request_closing_protection_precedes_queue_guidance(self):
        _, message = self.queue_message("cancelled")
        chat = self.chat(message)
        chat._requests[(self.fixture.cid, message["id"])] = {"metadata": message["metadata"], "phase": "retrieving_memory"}
        self.assertEqual(chat.message_retry_state(self.fixture.cid, message["id"]), {
            "available": False, "status": "上次记忆检索仍在结束；结束后可重试，也可继续发送新消息。"})
        self.assertFalse(chat.retry(self.fixture.cid, message["id"]))
        chat._configuration.assert_not_called()
        chat._begin_chat.assert_not_called()

    def test_non_null_untrusted_origin_does_not_grant_execution(self):
        for origin in ({}, {"job_id": "invented", "peer_id": "invented"}, "invented", False, 0, []):
            with self.subTest(origin_type=type(origin).__name__):
                message = self.ordinary_message("failed")
                message["metadata"]["agent_queue_origin"] = origin
                self.assert_queue_rejected(self.chat(message), message)

    def test_unknown_message_and_old_conversation_do_not_dispatch(self):
        message = self.ordinary_message("failed")
        chat = self.chat(message)
        self.assertFalse(chat.message_retry_state(self.fixture.cid, "missing")["available"])
        self.assertFalse(chat.retry(self.fixture.cid, "missing"))
        self.assertFalse(chat.retry("different-cid", message["id"]))
        chat._configuration.assert_not_called()
        chat._begin_chat.assert_not_called()

    def test_new_host_request_is_distinct_and_old_receipt_is_not_replayed(self):
        first, message = self.queue_message("cancelled")
        chat = self.chat(message)
        self.assert_queue_rejected(chat, message)
        original_job = self.fixture.queue.get(first["job_id"])
        second = self.fixture.admit(text="Owned fresh task with new user request identity")
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first["message_id"], second["message_id"])
        self.assertEqual(self.fixture.queue.get(first["job_id"]), original_job)
        self.assertEqual(self.fixture.queue.get(second["job_id"]).state, "queued")
        self.assertEqual(self.fixture.starts, [])
        self.assertFalse(chat.retry(self.fixture.cid, message["id"]))
        chat._begin_chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
