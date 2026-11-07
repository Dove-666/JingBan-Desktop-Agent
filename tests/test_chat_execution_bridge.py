"""Owned hidden panels exercise typed trace updates and complete barriers."""
from dataclasses import FrozenInstanceError
import json
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, Signal
from PySide6.QtWidgets import QApplication

from desktop_pet.execution_trace import ExecutionTrace, ExecutionViewUpdate
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from tests.test_chat_stream_bridge import StreamChat


class ExecutionViewChat(StreamChat):
    messages_view_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.trace = ExecutionTrace('owned')
        self.messages = [{'id': 'active', 'conversation_id': self.current_conversation_id,
            'role': 'user', 'source': 'user', 'content': 'Owned task',
            'metadata': {'request_status': 'pending', 'execution': self.trace.snapshot()}}]

    def publish(self, *, full=False):
        snapshot = self.trace.snapshot()
        self._owned_messages[0]['metadata']['execution'] = snapshot
        update = None if full else ExecutionViewUpdate(self.current_conversation_id, 'active', json.dumps(snapshot))
        self.messages_view_changed.emit(update)
        self.messages_changed.emit()


class ChatExecutionBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = ExecutionViewChat()
        self.panel = AssistantChatPanel(self.chat)
        self.fulls, self.patches = [], []
        self.panel.bridge.stateChanged.connect(lambda value: self.fulls.append(json.loads(value)))
        self.panel.bridge.executionChanged.connect(lambda value: self.patches.append(json.loads(value)))
        self.initial = json.loads(self.panel.bridge.getState())

    def tearDown(self):
        for timer in (self.panel._web_publish_timer, self.panel._web_stream_timer,
                      self.panel._web_execution_timer, self.panel._web_start_timer, self.panel._web_timeout):
            timer.stop()
        self.panel.close()
        self.panel.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertFalse(any(widget.isVisible() for widget in self.app.topLevelWidgets()))

    def full(self):
        self.panel._web_publish_timer.stop()
        self.panel._publish_web_state()

    def progress(self):
        self.panel._web_execution_timer.stop()
        self.panel._publish_web_execution()

    def ready(self, **data):
        return json.loads(self.panel.bridge.dispatch('ready', json.dumps(data)))['ok']

    def negotiate(self):
        self.assertTrue(self.ready(draftDocument=self.initial['draftDocument'], executionProtocol=1, streamProtocol=1))
        self.full()

    def update(self, label='Owned update'):
        self.chat.trace.event('phase:starting', 'progress', label)
        self.chat.publish()

    def test_typed_execution_skips_full_state_and_hidden_native_history_reads(self):
        self.negotiate()
        before = self.chat.history_reads
        count = len(self.fulls)
        legacy = []
        self.chat.messages_changed.connect(lambda: legacy.append(True))
        with patch.object(self.panel, 'web_state', side_effect=AssertionError('NO_FULL_HISTORY')):
            self.update()
            self.progress()
        self.assertEqual(self.chat.history_reads, before)
        self.assertEqual(len(self.fulls), count)
        self.assertEqual(legacy, [True])
        self.assertEqual(len(self.patches), 1)
        frame = self.patches[0]
        self.assertEqual(set(frame), {'version', 'document', 'revision', 'sequence', 'conversationId',
                                     'messageId', 'contextDigest', 'execution'})
        self.assertNotIn('context', frame['execution'])
        self.assertEqual(frame['execution']['events'][0]['label'], 'Owned update')
        self.assertEqual(frame['contextDigest'], self.fulls[-1]['messages'][0]['metadata']['execution_context_digest'])

    def test_legacy_and_invalid_capabilities_use_full_snapshots(self):
        for data in ({}, {'executionProtocol': True}, {'executionProtocol': 2}):
            self.assertTrue(self.ready(draftDocument=self.initial['draftDocument'], **data))
            self.full()
            self.update(str(data))
            self.progress()
            self.full()
            self.assertFalse(self.panel._web_execution_enabled)
            self.assertEqual(self.fulls[-1]['messages'][0]['metadata']['execution']['events'][0]['label'], str(data))
        self.assertEqual(self.patches, [])

    def test_wrong_nonce_does_not_negotiate_execution(self):
        self.assertFalse(self.ready(draftDocument='0' * 32, executionProtocol=1))
        self.assertFalse(self.panel._web_execution_enabled)

    def test_context_change_new_attempt_and_terminal_are_full_barriers(self):
        self.negotiate()
        previous = self.fulls[-1]['executionCursor']['revision']
        self.chat.trace.context([{'role': 'user', 'content': 'OWNED NEW CONTEXT'}], object(), 'owned')
        self.update('With context')
        self.progress()
        self.assertEqual(self.patches, [])
        self.full()
        self.assertGreater(self.fulls[-1]['executionCursor']['revision'], previous)
        self.assertEqual(self.fulls[-1]['messages'][0]['metadata']['execution']['context']['messages'][0]['content'], 'OWNED NEW CONTEXT')
        self.chat.trace = ExecutionTrace('owned next attempt')
        self.update('Next attempt')
        self.progress()
        self.full()
        self.chat.trace.finish('completed')
        self.chat.publish()
        self.progress()
        self.full()
        self.assertEqual(self.patches, [])
        self.assertEqual(self.fulls[-1]['messages'][0]['metadata']['execution']['state'], 'completed')

    def test_actual_full_notification_cancels_the_queued_progress_packet(self):
        self.negotiate()
        self.update()
        self.chat._owned_messages[0]['content'] = 'New owned message content'
        self.chat.publish(full=True)
        self.progress()
        self.full()
        self.assertEqual(self.patches, [])
        self.assertEqual(self.fulls[-1]['messages'][0]['content'], 'New owned message content')
        self.assertEqual(self.fulls[-1]['messages'][0]['metadata']['execution']['events'][0]['label'], 'Owned update')

    def test_coalescing_keeps_one_latest_snapshot_and_independent_text_sequence(self):
        self.negotiate()
        before = self.panel._web_execution_sequence
        text_sequence = self.panel._web_stream_sequence
        for index in range(20):
            self.update(str(index))
        self.progress()
        self.assertEqual(len(self.patches), 1)
        self.assertEqual(self.patches[0]['execution']['events'][0]['label'], '19')
        self.assertEqual(self.patches[0]['sequence'], before + 20)
        self.assertEqual(self.panel._web_stream_sequence, text_sequence)

    def test_compatibility_mode_still_renders_the_new_trace_in_native_text(self):
        self.negotiate()
        self.panel._activate_native()
        self.update('Owned native recovery')
        self.assertTrue(self.panel._native_mode)
        self.assertIn('Owned native recovery', self.panel.transcript.toPlainText())
        self.progress()
        self.assertEqual(self.patches, [])

    def test_reload_discards_execution_pending_and_rotates_its_document(self):
        self.negotiate()
        self.update()
        before = self.panel._web_document_token
        with patch.object(self.panel, '_ensure_web'):
            self.panel._reload_web()
        self.assertNotEqual(self.panel._web_document_token, before)
        self.assertFalse(self.panel._web_execution_enabled)
        self.assertIsNone(self.panel._web_execution_pending)
        self.assertFalse(self.panel._web_execution_timer.isActive())
        value = json.loads(self.panel.bridge.getState())
        self.assertEqual(value['executionCursor']['document'], value['draftDocument'])
        self.assertEqual(value['executionCursor']['revision'], value['streamCursor']['revision'])

    def test_notification_payload_is_immutable_after_its_source_changes(self):
        snapshot = self.chat.trace.snapshot()
        notification = ExecutionViewUpdate('current', 'active', json.dumps(snapshot))
        self.assertRaises(FrozenInstanceError, setattr, notification, 'message_id', 'other')
        snapshot['events'][0]['label'] = 'Mutated after notification'
        self.assertNotEqual(json.loads(notification.execution_json)['events'][0]['label'], snapshot['events'][0]['label'])


if __name__ == '__main__':
    unittest.main()
