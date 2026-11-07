"""Native manager with owned SQLite/images and real Qt worker completion."""
from dataclasses import replace
import base64
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QThread, QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.agents import AgentSettings
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.remote.attachments import ImageInbox
from tests.test_assistant_ui import wait_until
from tests.test_chat_controller import MemorySettings, RecordingClient


class Settings(MemorySettings):
    def load_agent_settings(self):
        return replace(AgentSettings(), enabled=False)


class AmbiguousRunner:
    """An owned physical thread enters before submit returns None or raises."""
    def __init__(self, entered, *, raise_after=False):
        self.entered, self.raise_after = entered, raise_after
        self.thread = None
        self.finished = threading.Event()

    def submit(self, function, callback):
        def run():
            try:
                function()
            finally:
                self.finished.set()
        self.thread = threading.Thread(target=run, name='owned-image-manager-submit')
        self.thread.start()
        if not self.entered.wait(3):
            raise AssertionError('owned delete never entered')
        if self.raise_after:
            raise RuntimeError('OWNED_START_RAISED_AFTER_ENTRY')
        return None

    def shutdown(self):
        pass


class ImageStorageManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        workspace = Path(__file__).resolve().parents[1]/'agent_workspace'
        self.directory = tempfile.TemporaryDirectory(prefix='manager-owned-', dir=workspace)
        self.path = Path(self.directory.name).resolve()
        self.assertTrue(self.path.is_relative_to(workspace.resolve()))
        self.chat = ChatController(Settings(), RecordingClient(), store=ConversationStore(self.path/'archive.sqlite3'),
                                   metadata_client_factory=lambda: None)
        wait_until(lambda: not self.chat.history_loading and not self.chat._runner._tasks)
        self.inbox = ImageInbox(self.path/'inbox')
        self.chat.set_image_inbox(self.inbox)
        self.manager = self.chat.image_storage_manager
        self.events, self.threads = [], []
        image = QImage(21, 13, QImage.Format.Format_ARGB32)
        image.fill(QColor('#7eb1ef'))
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        self.assertTrue(buffer.open(QIODevice.OpenModeFlag.WriteOnly))
        self.assertTrue(image.save(buffer, 'PNG'))
        self.params = {'filename': 'owned.png', 'mime_type': 'image/png',
                       'data_base64': base64.b64encode(bytes(encoded)).decode('ascii')}
        self.metadata = self.inbox.upload(self.params)
        self.image_id = self.metadata['id']

    def tearDown(self):
        for event in self.events:
            event.set()
        for thread in self.threads:
            thread.join(4)
            self.assertFalse(thread.is_alive())
        self.manager.shutdown()
        self.chat.shutdown()
        draft_owner = getattr(self.chat, '_local_draft_owner', None)
        wait_until(lambda: not QThreadPool.globalInstance().activeThreadCount()
                   and (draft_owner is None or draft_owner._stopped.is_set()))
        self.assertFalse(any(w.isVisible() for w in self.app.topLevelWidgets()))
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def result(self, operation):
        received = []
        operation(lambda result, error: received.append((result, error)))
        wait_until(lambda: bool(received))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1], '')
        return received[0][0]

    def review(self):
        value = self.result(lambda callback: self.manager.review_delete(self.image_id, callback))
        self.assertTrue(value['ok'], value)
        return value['confirmation_id']

    def test_usage_row_does_not_scan_archive_or_load_images(self):
        with patch.object(self.chat.store, 'image_reference_summary', side_effect=AssertionError('NO_ARCHIVE_SCAN_FOR_TOTALS')), \
                patch.object(self.inbox, 'get', side_effect=AssertionError('NO_IMAGE_BYTES_FOR_TOTALS')):
            result = self.result(self.manager.load_usage)
        self.assertTrue(result['ok'])
        self.assertEqual(result['usage']['count'], 1)

    def test_callback_runs_on_qt_thread(self):
        threads, results = [], []
        def finished(result, error):
            threads.append(QThread.currentThread() == self.app.thread())
            results.append(result)
        self.manager.load_usage(finished)
        wait_until(lambda: bool(results))
        self.assertEqual(threads, [True])

    def test_page_and_reference_queries_run_off_qt_without_image_bytes(self):
        original_list, original_usage = self.inbox.list, self.inbox.usage
        threads = []
        def page(*args):
            threads.append(QThread.currentThread() != self.app.thread())
            return original_list(*args)
        def usage():
            threads.append(QThread.currentThread() != self.app.thread())
            return original_usage()
        self.chat.store.append_message(self.chat.current_conversation_id, 'user', 'owned text', 'user',
                                       metadata={'image_id': self.image_id})
        with patch.object(self.inbox, 'list', page), patch.object(self.inbox, 'usage', usage), \
                patch.object(self.inbox, 'get', side_effect=AssertionError('NO_IMAGE_BYTES_IN_LIST')):
            result = self.result(lambda callback: self.manager.load_page(0, callback))
        self.assertTrue(result['ok'])
        self.assertEqual(threads, [True, True])
        self.assertEqual(result['usage']['count'], 1)
        self.assertEqual(result['usage']['used_bytes'], self.metadata['size'])
        self.assertEqual(result['references'][self.image_id], {'message_count': 1, 'conversation_count': 1})
        self.assertNotIn('owned text', str(result))
        self.assertNotIn('data_base64', str(result))

    def test_delete_preserves_archive_and_advances_epoch_before_maintenance_release(self):
        self.chat.store.append_message(self.chat.current_conversation_id, 'user', 'owned text', 'user',
                                       metadata={'image_id': self.image_id})
        before = self.chat.store.load_messages(self.chat.current_conversation_id)
        ticket = self.review()
        observed = []
        original_mark = self.chat.mark_image_storage_deleted
        def mark(image_id):
            original_mark(image_id)
            observed.append((self.chat.image_storage_epoch, self.chat.image_storage_gate.snapshot()['maintenance_active']))
        with patch.object(self.chat, 'mark_image_storage_deleted', mark):
            result = self.result(lambda callback: self.manager.delete(ticket, callback))
        self.assertTrue(result['ok'])
        self.assertEqual(observed, [(1, True)])
        self.assertEqual(self.inbox.usage()['count'], 0)
        self.assertEqual(self.chat.store.load_messages(self.chat.current_conversation_id), before)
        self.assertFalse(self.chat.image_storage_gate.snapshot()['maintenance_active'])
        second = self.result(lambda callback: self.manager.delete(ticket, callback))
        self.assertEqual(second['code'], 'changed')

    def test_changed_reference_confirmation_does_not_delete(self):
        ticket = self.review()
        self.chat.store.append_message(self.chat.current_conversation_id, 'user', 'new reference', 'user',
                                       metadata={'image_id': self.image_id})
        with patch.object(self.inbox, 'delete', wraps=self.inbox.delete) as delete:
            result = self.result(lambda callback: self.manager.delete(ticket, callback))
        self.assertEqual(result['code'], 'changed')
        self.assertEqual(delete.call_count, 0)
        self.assertEqual(self.inbox.usage()['count'], 1)

    def test_staged_activity_rejects_without_consuming_confirmation(self):
        ticket = self.review()
        gate = self.chat.image_storage_gate
        owner = gate.create_owner()
        token = gate.activity(owner, stage_on_success=True)
        gate.run(token, lambda: None)
        stage = gate.handoff_to_stage(token)
        try:
            value = self.result(lambda callback: self.manager.delete(ticket, callback))
            self.assertEqual(value['code'], 'busy')
            self.assertIn(ticket, self.manager._tickets)
        finally:
            gate.release(stage)
            gate.close_owner(owner)
        self.assertTrue(self.result(lambda callback: self.manager.delete(ticket, callback))['ok'])

    def test_resource_unknown_or_busy_fails_closed_without_delete(self):
        for target, method, value in [(self.chat._agent_service, 'execution_resources_pending', True),
                                     (self.chat._agent_service, 'execution_resources_pending', None),
                                     (self.chat._agent_queue.queue, 'has_image_maintenance_blocker', True)]:
            with self.subTest(method=method, value=value):
                ticket = self.review()
                with patch.object(target, method, return_value=value), patch.object(self.inbox, 'delete', wraps=self.inbox.delete) as delete:
                    result = self.result(lambda callback: self.manager.delete(ticket, callback))
                self.assertEqual(result['code'], 'busy')
                self.assertEqual(delete.call_count, 0)
        ticket = self.review()
        with patch.object(self.chat._agent_service, 'execution_resources_pending', side_effect=OSError('private path')):
            result = self.result(lambda callback: self.manager.delete(ticket, callback))
        self.assertEqual(result['code'], 'busy')
        self.assertNotIn('private path', str(result))

    def test_failed_delete_also_invalidates_before_release_and_cannot_replay_ticket(self):
        ticket = self.review()
        with patch.object(self.inbox, 'delete', side_effect=OSError('PRIVATE_STORAGE_DETAIL')) as delete:
            result = self.result(lambda callback: self.manager.delete(ticket, callback))
        self.assertEqual(result['code'], 'delete')
        self.assertEqual(delete.call_count, 1)
        self.assertEqual(self.chat.image_storage_epoch, 1)
        self.assertNotIn('PRIVATE_STORAGE_DETAIL', str(result))
        self.assertEqual(self.result(lambda callback: self.manager.delete(ticket, callback))['code'], 'changed')

    def test_closed_host_does_not_touch_storage(self):
        ticket = self.review()
        self.chat.shutdown()
        with patch.object(self.inbox, 'delete', wraps=self.inbox.delete) as delete:
            value = self.result(lambda callback: self.manager.delete(ticket, callback))
        self.assertEqual(value['code'], 'closed')
        self.assertEqual(delete.call_count, 0)

    def test_cancelled_confirmation_is_not_authority(self):
        ticket = self.review()
        self.manager.discard_confirmation(ticket)
        self.assertEqual(self.result(lambda callback: self.manager.delete(ticket, callback))['code'], 'changed')

    def test_start_none_before_entry_retires_pending_maintenance(self):
        ticket = self.review()
        with patch.object(self.manager._runner, 'submit', return_value=None), patch.object(self.inbox, 'delete', wraps=self.inbox.delete) as delete:
            result = self.result(lambda callback: self.manager.delete(ticket, callback))
        self.assertEqual(result['code'], 'delete')
        self.assertEqual(delete.call_count, 0)
        self.assertFalse(self.chat.image_storage_gate.snapshot()['maintenance_active'])

    def test_start_failure_after_entry_keeps_lease_and_delivers_actual_result_once(self):
        for raise_after in (False, True):
            with self.subTest(raise_after=raise_after):
                self.inbox.upload(self.params)
                ticket = self.review()
                entered, release = threading.Event(), threading.Event()
                self.events.append(release)
                original_delete = self.inbox.delete
                def held(image_id):
                    entered.set()
                    if not release.wait(5): raise AssertionError('OWNED_DELETE_NOT_RELEASED')
                    return original_delete(image_id)
                runner = AmbiguousRunner(entered, raise_after=raise_after)
                old_runner = self.manager._runner
                received = []
                self.manager._runner = runner
                try:
                    with patch.object(self.inbox, 'delete', held):
                        accepted = self.manager.delete(ticket, lambda result, error: received.append(result))
                        self.threads.append(runner.thread)
                        self.assertFalse(accepted)
                        self.assertTrue(self.chat.image_storage_gate.snapshot()['maintenance_active'])
                        self.assertEqual(received, [])
                        release.set()
                        runner.thread.join(3)
                        self.assertFalse(runner.thread.is_alive())
                        wait_until(lambda: bool(received))
                    self.assertEqual(len(received), 1)
                    self.assertTrue(received[0]['ok'])
                    self.assertFalse(self.chat.image_storage_gate.snapshot()['maintenance_active'])
                finally:
                    release.set()
                    self.manager._runner = old_runner


if __name__ == '__main__':
    unittest.main()
