"""Hidden real panel/Qt signal contract with owned in-memory conversations.

No renderer, visible window, network, model, clipboard, or real profile is used.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, Signal
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.assistant_panel import AssistantChatPanel
from tests.test_session_ui import SessionChat


class StreamChat(SessionChat):
    streaming_reply_changed = Signal()

    @property
    def messages(self):
        self.history_reads = getattr(self, "history_reads", 0) + 1
        return deepcopy(self._owned_messages)

    @messages.setter
    def messages(self, value):
        self._owned_messages = value

    def __init__(self):
        super().__init__()
        self.streaming_reply = None
        self.history_reads = 0


class ChatStreamBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = StreamChat()
        self.panel = AssistantChatPanel(self.chat)
        self.fulls, self.patches = [], []
        self.panel.bridge.stateChanged.connect(lambda payload: self.fulls.append(json.loads(payload)))
        self.panel.bridge.streamingChanged.connect(lambda payload: self.patches.append(json.loads(payload)))
        self.initial = json.loads(self.panel.bridge.getState())

    def tearDown(self):
        self.panel._web_publish_timer.stop()
        self.panel._web_stream_timer.stop()
        self.panel._web_start_timer.stop()
        self.panel._web_timeout.stop()
        self.assertFalse(self.panel.isVisible())
        self.assertIsNone(self.panel.web_view)
        self.panel.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def full(self):
        self.panel._web_publish_timer.stop()
        self.panel._publish_web_state()

    def stream(self):
        self.panel._web_stream_timer.stop()
        self.panel._publish_web_stream()

    def ready(self, **data):
        return json.loads(self.panel.bridge.dispatch("ready", json.dumps(data)))["ok"]

    def negotiate(self):
        self.assertTrue(self.ready(draftDocument=self.initial["draftDocument"], streamProtocol=1))
        self.full()

    def start(self, request="1"):
        self.chat.busy = True
        self.chat.streaming_reply = {"conversationId": self.chat.current_conversation_id,
                                     "requestId": request, "text": "first", "state": "streaming"}
        self.chat.streaming_reply_changed.emit()
        self.full()

    def delta(self, text="next"):
        self.chat.streaming_reply["text"] = text
        self.chat.streaming_reply_changed.emit()

    def test_negotiated_text_delta_does_not_read_or_serialize_history(self):
        self.negotiate(); self.start()
        before = self.chat.history_reads
        full_count = len(self.fulls)
        with patch.object(self.panel, "web_state", side_effect=AssertionError("full-state traversal")):
            self.delta(); self.stream()
        self.assertEqual(self.chat.history_reads, before)
        self.assertEqual(len(self.fulls), full_count)
        self.assertEqual(len(self.patches), 1)
        frame = self.patches[0]
        self.assertEqual(set(frame), {"version", "document", "conversationId", "revision", "sequence", "streamingReply"})
        self.assertEqual(frame["streamingReply"]["text"], "next")
        self.assertEqual(frame["revision"], self.fulls[-1]["streamCursor"]["revision"])
        self.assertLess(len(json.dumps(frame).encode()), 400)

    def test_legacy_ready_and_unknown_capability_keep_full_updates(self):
        for data in ({}, {"draftDocument": self.initial["draftDocument"], "streamProtocol": True},
                     {"draftDocument": self.initial["draftDocument"], "streamProtocol": 2}):
            self.assertTrue(self.ready(**data)); self.full(); self.start()
            self.delta(); self.full()
            self.assertFalse(self.panel._web_stream_enabled)
            self.assertEqual(self.fulls[-1]["streamingReply"]["text"], "next")
        self.assertEqual(self.patches, [])

    def test_wrong_document_cannot_enable_capability(self):
        self.assertFalse(self.ready(draftDocument="0" * 32, streamProtocol=1))
        self.assertFalse(self.panel._web_stream_enabled)

    def test_unchanged_full_notifications_still_deduplicate(self):
        self.negotiate()
        count = len(self.fulls)
        for _ in range(5):
            self.chat.messages_changed.emit(); self.full()
        self.assertEqual(len(self.fulls), count)
        self.assertTrue(self.ready()); self.full()
        self.assertEqual(len(self.fulls), count + 1)
        self.full()
        self.assertEqual(len(self.fulls), count + 1)

    def test_pending_full_blocks_patch_and_replays_latest_preview(self):
        self.negotiate(); self.start()
        self.delta("second")
        self.chat.error = "owned failure"
        self.chat.error_changed.emit(self.chat.error)
        self.stream()
        self.assertEqual(self.patches, [])
        self.full()
        self.assertEqual(self.fulls[-1]["error"], "owned failure")
        self.assertEqual(self.fulls[-1]["streamingReply"]["text"], "second")

    def test_stream_events_coalesce_to_latest_text_and_sequence(self):
        self.negotiate(); self.start()
        before = self.panel._web_stream_sequence
        for index in range(20): self.delta(str(index))
        self.stream()
        self.assertEqual(len(self.patches), 1)
        self.assertEqual(self.patches[0]["sequence"], before + 20)
        self.assertEqual(self.patches[0]["streamingReply"]["text"], "19")

    def test_interrupt_clear_new_request_and_cid_are_full_barriers(self):
        self.negotiate(); self.start()
        revision = self.fulls[-1]["streamCursor"]["revision"]
        self.chat.streaming_reply["state"] = "interrupted"
        self.chat.streaming_reply_changed.emit(); self.stream(); self.full()
        self.assertGreater(self.fulls[-1]["streamCursor"]["revision"], revision)
        self.chat.streaming_reply = None
        self.chat.streaming_reply_changed.emit(); self.full()
        self.assertIsNone(self.fulls[-1]["streamingReply"])
        self.start("2")
        self.assertEqual(self.fulls[-1]["streamingReply"]["requestId"], "2")
        self.chat.current_conversation_id = "other"
        self.chat.conversation_changed.emit(); self.stream(); self.full()
        self.assertEqual(self.fulls[-1]["conversationId"], "other")
        self.assertEqual(self.patches, [])

    def test_get_state_is_one_fresh_barrier_without_a_followup_publish(self):
        self.negotiate(); self.start(); self.delta()
        previous = self.panel._web_revision
        count = len(self.fulls)
        value = json.loads(self.panel.bridge.getState())
        self.assertGreater(value["streamCursor"]["revision"], previous)
        self.assertEqual(value["streamingReply"]["text"], "next")
        self.assertFalse(self.panel._web_publish_timer.isActive())
        self.assertFalse(self.panel._web_stream_timer.isActive())
        self.full()
        self.assertEqual(len(self.fulls), count)

    def test_reload_rotates_document_and_disables_pending_patch(self):
        self.negotiate(); self.start(); self.delta()
        old = self.panel._web_document_token
        with patch.object(self.panel, "_ensure_web"):
            self.panel._reload_web()
        self.assertNotEqual(self.panel._web_document_token, old)
        self.assertFalse(self.panel._web_stream_enabled)
        self.assertFalse(self.panel._web_stream_timer.isActive())
        value = json.loads(self.panel.bridge.getState())
        self.assertEqual(value["streamCursor"]["document"], value["draftDocument"])
        self.assertEqual(value["streamingReply"]["text"], "next")
        self.assertFalse(self.ready(draftDocument=old, streamProtocol=1))

    def test_same_document_failure_keeps_monotonic_counters(self):
        self.negotiate(); self.start(); self.delta(); self.stream()
        revision, sequence = self.panel._web_revision, self.panel._web_stream_sequence
        self.panel._web_failed()
        self.assertFalse(self.panel._web_stream_enabled)
        self.assertEqual(self.panel._web_revision, revision)
        self.assertEqual(self.panel._web_stream_sequence, sequence)


if __name__ == "__main__":
    unittest.main()
