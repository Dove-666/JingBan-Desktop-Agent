"""Owned real storage journeys when the final image on a page disappears."""
import base64
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, Qt, QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.remote.attachments import ImageInbox
from desktop_pet.ui.image_storage_dialog import ImageStorageDialog
from tests.test_assistant_ui import wait_until
from tests import test_image_storage_manager as manager_fixture
from tests.test_chat_controller import RecordingClient


class ImageStoragePaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        workspace = Path(__file__).resolve().parents[1] / 'agent_workspace'
        self.directory = tempfile.TemporaryDirectory(prefix='page-shrink-owned-', dir=workspace)
        self.path = Path(self.directory.name).resolve()
        self.assertTrue(self.path.is_relative_to(workspace.resolve()))
        self.client = RecordingClient()
        self.chat = ChatController(manager_fixture.Settings(), self.client,
            store=ConversationStore(self.path / 'archive.sqlite3'), metadata_client_factory=lambda: None)
        wait_until(lambda: not self.chat.history_loading and not self.chat._runner._tasks)
        self.inbox = ImageInbox(self.path / 'inbox')
        self.chat.set_image_inbox(self.inbox)
        self.manager = self.chat.image_storage_manager
        self.widgets = []
        for index in range(21):
            image = QImage(21, 13, QImage.Format.Format_ARGB32)
            image.fill(QColor(index, 110, 210))
            data = QByteArray()
            buffer = QBuffer(data)
            self.assertTrue(buffer.open(QIODevice.OpenModeFlag.WriteOnly))
            self.assertTrue(image.save(buffer, 'PNG'))
            buffer.close()
            self.inbox.upload({'filename': f'owned-{index}.png', 'mime_type': 'image/png',
                               'data_base64': base64.b64encode(bytes(data)).decode('ascii')})

    def tearDown(self):
        for widget in self.widgets:
            widget.done(0)
            widget.deleteLater()
        self.manager.shutdown()
        self.chat.shutdown()
        wait_until(lambda: not QThreadPool.globalInstance().activeThreadCount())
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertFalse(any(widget.isVisible() for widget in self.app.topLevelWidgets()))
        self.assertEqual(self.client.requests, [])
        self.directory.cleanup()

    def dialog_at_last_page(self):
        dialog = ImageStorageDialog(self.manager)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.widgets.append(dialog)
        wait_until(lambda: not dialog._busy)
        self.assertEqual(dialog.files.count(), 20)
        dialog.next.click()
        wait_until(lambda: not dialog._busy)
        self.assertEqual(dialog.files.count(), 1)
        return dialog

    def test_deleting_only_last_page_image_returns_to_existing_images(self):
        dialog = self.dialog_at_last_page()
        dialog.files.setCurrentRow(0)
        deleted_id = dialog.selected()['id']
        dialog.review_selected()
        wait_until(lambda: not dialog._busy)
        self.assertIs(dialog.stack.currentWidget(), dialog.confirm_page)
        dialog.confirm_delete()
        wait_until(lambda: not dialog._busy)
        self.assertEqual(self.inbox.usage()['count'], 20)
        self.assertEqual(dialog.files.count(), 20)
        self.assertEqual(dialog.page_label.text(), '第 1 页 · 20 张')
        self.assertFalse(dialog.previous.isEnabled())
        self.assertFalse(dialog.next.isEnabled())
        self.assertIsNone(dialog.selected())
        self.assertNotIn(deleted_id, [dialog.files.item(i).data(Qt.ItemDataRole.UserRole)['id']
                                     for i in range(dialog.files.count())])

    def test_external_last_page_removal_returns_to_remaining_images(self):
        dialog = self.dialog_at_last_page()
        image_id = dialog.files.item(0).data(Qt.ItemDataRole.UserRole)['id']
        self.inbox.delete(image_id)
        self.chat.mark_image_storage_deleted(image_id)
        wait_until(lambda: not dialog._busy)
        self.assertEqual(dialog.files.count(), 20)
        self.assertEqual(dialog.page_label.text(), '第 1 页 · 20 张')

    def test_empty_storage_returns_to_first_empty_page(self):
        dialog = self.dialog_at_last_page()
        for row in self.inbox.list(0, 50)['items']:
            self.inbox.delete(row['id'])
        self.chat.mark_image_storage_deleted(row['id'])
        wait_until(lambda: not dialog._busy)
        self.assertEqual(dialog.files.count(), 0)
        self.assertEqual(dialog.status.text(), '暂无图片')
        self.assertEqual(dialog.page_label.text(), '第 1 页 · 0 张')
        self.assertFalse(dialog.previous.isEnabled())

    def test_refreshing_nonempty_last_page_preserves_page_and_selection(self):
        dialog = self.dialog_at_last_page()
        dialog.files.setCurrentRow(0)
        selected = dialog.selected()['id']
        dialog.refresh.click()
        wait_until(lambda: not dialog._busy)
        self.assertEqual(dialog.files.count(), 1)
        self.assertEqual(dialog.selected()['id'], selected)
        self.assertEqual(dialog.page_label.text(), '第 2 页 · 21 张')


if __name__ == '__main__':
    unittest.main()
