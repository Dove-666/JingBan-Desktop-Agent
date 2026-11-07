"""Explicit local Agent preflight keeps unaccepted composer input untouched.

Controller, persistence, native acceptance, image staging and closure records
are real. Providers and launched worker bodies are exclusively owned fixtures.
"""
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QByteArray, QBuffer, QEvent, QIODevice
from PySide6.QtGui import QImage

from desktop_pet.agents.service import _ERROR_MESSAGES
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from desktop_pet.ui.chat_panel import ChatPanel
from tests import test_agent_shutdown_completion as owned
from tests.test_agent_cleanup_queue_status import OwnedProcess, OwnedTree
from tests.test_chat_commit_ack_recovery import FixedReply


class LocalAgentPendingAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        owned.AgentShutdownCompletionTests.setUpClass()

    def setUp(self):
        self.case = owned.AgentShutdownCompletionTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.chat, self.store, self.service = self.case.chat, self.case.store, self.case.service
        self.panels = []
        self.addCleanup(self.close_panels)

    def close_panels(self):
        for panel in self.panels:
            if isinstance(panel, AssistantChatPanel):
                for timer in (panel._web_start_timer, panel._web_timeout, panel._web_publish_timer,
                              panel._web_stream_timer, panel._web_execution_timer):
                    timer.stop()
            panel.close()
            panel.deleteLater()
        self.case.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.case.app.processEvents()

    def panel(self, cls=ChatPanel):
        panel = cls(self.chat)
        if isinstance(panel, AssistantChatPanel):
            panel._web_start_timer.stop()
        self.panels.append(panel)
        self.case.wait(lambda: panel._draft_owner._loaded.is_set())
        return panel

    def stage(self, panel):
        image = QImage(2, 2, QImage.Format.Format_ARGB32)
        image.fill(0xff4488cc)
        data = QByteArray()
        buffer = QBuffer(data)
        self.assertTrue(buffer.open(QIODevice.OpenModeFlag.WriteOnly))
        self.assertTrue(image.save(buffer, "PNG"))
        buffer.close()
        self.assertTrue(panel._stage_image_data({"filename": "owned-pending.png", "mimeType": "image/png",
            "dataBase64": bytes(data.toBase64()).decode("ascii")}, self.case.cid))
        self.case.wait(lambda: not panel._image_preparing and panel._current_staged_image() is not None)
        return panel._current_staged_image()["image_id"]

    def assert_unaccepted(self, cid=None):
        self.assertEqual(self.store.load_messages(cid or self.case.cid), [])
        self.assertFalse(self.chat.busy)
        self.assertEqual(self.case.calls, 0)
        self.assertEqual(self.chat.error, _ERROR_MESSAGES["busy"])

    def test_pending_explicit_send_rejects_before_configuration_or_archive(self):
        with patch.object(self.service, "execution_resources_pending", return_value=True) as query, \
                patch.object(self.chat, "_configuration", wraps=self.chat._configuration) as configure:
            self.assertFalse(self.chat.send("Owned explicit local Agent", agent_mode=True))
            query.assert_called_once_with()
            configure.assert_not_called()
        self.assert_unaccepted()

    def test_query_error_and_non_boolean_values_are_conservatively_rejected(self):
        class FalsePredicate:
            def __bool__(self):
                return False

        for value in (None, 0, 1, "false", [], {}, FalsePredicate()):
            with self.subTest(value=value), patch.object(self.service, "execution_resources_pending", return_value=value):
                self.assertFalse(self.chat.send("Owned explicit local Agent", agent_mode=True))
                self.assert_unaccepted()
        with patch.object(self.service, "execution_resources_pending", side_effect=RuntimeError("OWNED_PRIVATE_QUERY_ERROR")):
            self.assertFalse(self.chat.send("Owned explicit local Agent", agent_mode=True))
        self.assert_unaccepted()
        self.assertNotIn("OWNED_PRIVATE", self.chat.error)
        class UnreadableService:
            @property
            def execution_resources_pending(self):
                raise RuntimeError("OWNED_PRIVATE_DESCRIPTOR_ERROR")

        with patch.object(self.chat, "_agent_service", UnreadableService()):
            self.assertFalse(self.chat.send("Owned explicit local Agent", agent_mode=True))
        self.assert_unaccepted()

    def test_missing_optional_query_keeps_explicit_agent_compatibility(self):
        with patch.object(self.service, "execution_resources_pending", None):
            self.assertTrue(self.chat.send("Owned local Agent compatibility", agent_mode=True))
            self.case.wait_gui()
        self.assertEqual(self.case.calls, 1)
        self.assertEqual(len([row for row in self.store.load_messages(self.case.cid) if row["role"] == "assistant"]), 1)

    def test_exact_false_snapshot_keeps_normal_explicit_agent_acceptance(self):
        with patch.object(self.service, "execution_resources_pending", return_value=False):
            self.assertTrue(self.chat.send("Owned local Agent compatibility", agent_mode=True))
            self.case.wait_gui()
        self.assertEqual(self.case.calls, 1)

    def test_native_rejection_keeps_raw_text_shared_draft_and_staged_image(self):
        panel = self.panel()
        self.assertTrue(self.chat.set_agent_mode(True))
        image_id = self.stage(panel)
        raw = "  未发送文字\n保留空格与图片  "
        panel.input.setPlainText(raw)
        before = panel._draft_owner.snapshot(self.case.cid)
        with patch.object(self.service, "execution_resources_pending", return_value=True):
            panel.send_button.click()
        self.assert_unaccepted()
        self.assertEqual(panel.input.toPlainText(), raw)
        self.assertEqual(panel._draft_owner.snapshot(self.case.cid), before)
        self.assertEqual(panel._current_staged_image()["image_id"], image_id)

    def test_web_dispatch_uses_conversation_mode_and_keeps_draft_and_image(self):
        panel = self.panel(AssistantChatPanel)
        self.assertTrue(self.chat.set_agent_mode(True))
        image_id = self.stage(panel)
        raw = "  网页草稿\n保留原文  "
        self.assertTrue(panel.dispatch_web_action("draft", {"conversationId": self.case.cid, "text": raw}))
        before = panel._draft_owner.snapshot(self.case.cid)
        with patch.object(self.service, "execution_resources_pending", return_value=True):
            self.assertFalse(panel.dispatch_web_action("send", {"conversationId": self.case.cid,
                "text": raw, "agentMode": True}))
        self.assert_unaccepted()
        self.assertEqual(panel._draft_owner.snapshot(self.case.cid), before)
        self.assertEqual(panel._current_staged_image()["image_id"], image_id)

    def test_wrong_scope_or_mode_web_send_does_not_touch_current_draft(self):
        panel = self.panel(AssistantChatPanel)
        self.assertTrue(self.chat.set_agent_mode(True))
        raw = "Current owned draft"
        self.assertTrue(panel.dispatch_web_action("draft", {"conversationId": self.case.cid, "text": raw}))
        before = panel._draft_owner.snapshot(self.case.cid)
        with patch.object(self.service, "execution_resources_pending", return_value=True) as query:
            self.assertFalse(panel.dispatch_web_action("send", {"conversationId": str(uuid4()), "text": raw, "agentMode": True}))
            self.assertFalse(panel.dispatch_web_action("send", {"conversationId": self.case.cid, "text": raw, "agentMode": False}))
            query.assert_not_called()
        self.assertEqual(panel._draft_owner.snapshot(self.case.cid), before)
        self.assertEqual(self.store.load_messages(self.case.cid), [])
        self.assertEqual(self.case.calls, 0)

    def test_other_conversation_explicit_send_cannot_consume_admission(self):
        other = str(uuid4())
        with patch.object(self.service, "execution_resources_pending", return_value=True):
            self.assertFalse(self.chat.send("Owned other conversation", conversation_id=other, agent_mode=True))
        self.assert_unaccepted()
        self.assertEqual(self.store.load_messages(other), [])

    def test_ordinary_chat_skips_the_agent_query_and_remains_usable(self):
        client = FixedReply()
        self.chat._client = client
        with patch.object(self.service, "execution_resources_pending", side_effect=AssertionError("AGENT_QUERY_MUST_BE_SKIPPED")) as query:
            self.assertTrue(self.chat.send("你好", agent_mode=False))
            query.assert_not_called()
        self.case.wait_gui()
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.case.calls, 0)

    def test_repeated_pending_sends_then_exact_closure_allow_one_original_new_task(self):
        process, tree = OwnedProcess(), OwnedTree()
        self.case.trees.append(tree)
        handle = self.service._worker_lifecycle.register(process, uuid4().hex, self.case.cid)
        self.service._worker_lifecycle.attach_tree(handle, tree)
        self.service._worker_lifecycle.mark_dispatched(handle)
        self.service._worker_handles[process.pid] = handle
        self.service._processes[process.pid] = (threading.Event(), tree)
        self.assertIsNone(self.service._settle_worker(handle, timeout_seconds=0))
        self.assertTrue(self.service.execution_resources_pending())
        close_calls = tree.close_calls
        for _ in range(2):
            self.assertFalse(self.chat.send("Owned task after cleanup", agent_mode=True))
            self.assert_unaccepted()
        self.assertEqual(tree.close_calls, close_calls)
        self.assertIsNone(self.service.worker_closure(handle))
        tree.confirmed = True
        self.service.reconcile_worker_closures(timeout_seconds=0)
        self.assertFalse(self.service.execution_resources_pending())
        self.assertTrue(self.chat.send("Owned task after cleanup", agent_mode=True))
        self.case.wait_gui()
        self.assertEqual(self.case.calls, 1)
        self.assertEqual([row["role"] for row in self.store.load_messages(self.case.cid)], ["user", "assistant"])

    def test_still_active_main_worker_keeps_existing_busy_rejection(self):
        self.case.hold_worker()
        self.assertTrue(self.chat.send("Owned active task", agent_mode=True))
        self.case.wait(self.case.worker_entered.is_set)
        before = self.store.load_messages(self.case.cid)
        self.assertTrue(self.service.execution_resources_pending())
        self.assertFalse(self.chat.send("Owned second task", agent_mode=True))
        self.assertEqual(self.store.load_messages(self.case.cid), before)
        self.assertEqual(self.case.calls, 1)
        self.case.worker_release.set()
        self.case.wait_gui()


if __name__ == "__main__":
    unittest.main()
