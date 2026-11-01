"""Native attachment actions cannot become arbitrary file or URL access."""

import base64
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QUrl, QThreadPool
from PySide6.QtGui import QColor, QDesktopServices, QImage
from PySide6.QtWidgets import QApplication, QLabel

from desktop_pet.api_client import ApiError
from desktop_pet.chat_images import IMAGE_ERRORS
from desktop_pet.ui.chat_panel import ChatPanel
from tests.test_remote_controller import spin_until
from tests.test_session_ui import SessionChat


class ImageChat(SessionChat):
    def __init__(self):
        super().__init__()
        self.messages = [{"id": "picture", "conversation_id": "current", "role": "user", "content": "看看附件",
                          "metadata": {"image_id": "a" * 64, "request_status": "complete"}}]
        self.image_calls = []
        self.image_error = None
        image = QImage(48, 32, QImage.Format.Format_RGB32)
        image.fill(QColor("#75b8f4"))
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        assert image.save(buffer, "PNG")
        self.image = {"data_base64": base64.b64encode(bytes(data)).decode("ascii")}

    def get_message_image(self, cid, mid, image_id):
        self.image_calls.append((cid, mid, image_id, threading.get_ident()))
        if self.image_error:
            raise ApiError(self.image_error, IMAGE_ERRORS[self.image_error])
        return self.image


class ChatImagePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = ImageChat()
        self.panel = ChatPanel(self.chat)
        self.panel.resize(720, 660)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        if self.panel._image_preview_dialog is not None:
            self.panel._image_preview_dialog.reject()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.panel.hide()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_history_shows_attachment_without_loading_or_embedding_image_data(self):
        for _ in range(3):
            self.panel._render_messages()
        self.assertIn("图片附件 · 查看图片", self.panel.transcript.toPlainText())
        self.assertEqual(self.chat.image_calls, [])
        html = self.panel.transcript.toHtml()
        self.assertIn('href="aura-image:picture"', html)
        self.assertNotIn("<img", html)
        self.assertNotIn("data:image", html)
        self.assertNotIn("a" * 64, html)

    def test_click_opens_native_scaled_preview_using_only_message_reference_in_worker(self):
        main_thread = threading.get_ident()
        with patch.object(QDesktopServices, "openUrl") as external:
            self.panel._open_event(QUrl("aura-image:picture"))
            dialog = self.panel._image_preview_dialog
            self.assertIsNotNone(dialog)
            status = dialog.findChild(QLabel, "chatImagePreviewStatus")
            spin_until(lambda: status.text() == "图片附件")
            image = dialog.findChild(QLabel, "chatImagePreviewImage")
            self.assertFalse(image.pixmap().isNull())
            self.assertLessEqual(dialog.width(), self.panel.screen().availableGeometry().width())
            self.assertLessEqual(dialog.height(), self.panel.screen().availableGeometry().height())
            self.assertEqual(self.chat.image_calls[0][:3], ("current", "picture", "a" * 64))
            self.assertNotEqual(self.chat.image_calls[0][3], main_thread)
            external.assert_not_called()

    def test_unlisted_attachment_actions_cannot_select_hash_paths_or_other_message(self):
        with patch.object(QDesktopServices, "openUrl") as external:
            for target in ("a" * 64, "other-message", "../private.png", "picture?image_id=" + "b" * 64):
                self.panel._open_event(QUrl("aura-image:" + target))
            self.assertIsNone(self.panel._image_preview_dialog)
            self.assertEqual(self.chat.image_calls, [])
            external.assert_not_called()

    def test_invalid_metadata_and_arbitrary_attachment_labels_never_enter_html(self):
        for image_id in ("C:/private/photo.png", "F" * 64, {}, None):
            self.chat.messages[0]["metadata"] = {"image_id": image_id, "filename": '<img src="file:///private">'}
            self.chat.messages_changed.emit()
            self.assertIn("图片附件不可用", self.panel.transcript.toPlainText())
            self.assertNotIn("aura-image:", self.panel.transcript.toHtml())
            self.assertNotIn("file:///private", self.panel.transcript.toHtml())
            self.assertNotIn("C:/private", self.panel.transcript.toHtml())
        self.chat.messages[0]["role"] = "assistant"
        self.chat.messages[0]["metadata"] = {"image_id": "a" * 64}
        self.chat.messages_changed.emit()
        self.assertNotIn("aura-image:", self.panel.transcript.toHtml())

    def test_three_image_errors_show_static_chinese_in_history(self):
        for code, expected in IMAGE_ERRORS.items():
            self.chat.messages[0]["metadata"].update(request_status="failed", image_error_code=code,
                                                    error_message="PRIVATE_PROVIDER_BODY")
            self.chat.messages_changed.emit()
            self.assertIn(expected, self.panel.transcript.toPlainText())
            self.assertNotIn("PRIVATE_PROVIDER_BODY", self.panel.transcript.toHtml())

    def test_missing_image_opens_clear_error_instead_of_external_application(self):
        self.chat.image_error = "image_missing"
        with patch.object(QDesktopServices, "openUrl") as external:
            self.panel._open_event(QUrl("aura-image:picture"))
            dialog = self.panel._image_preview_dialog
            status = dialog.findChild(QLabel, "chatImagePreviewStatus")
            spin_until(lambda: status.text() == IMAGE_ERRORS["image_missing"])
            self.assertTrue(dialog.findChild(QLabel, "chatImagePreviewImage").pixmap().isNull())
            external.assert_not_called()

    def test_switching_conversations_closes_and_discards_loading_preview(self):
        started, release = threading.Event(), threading.Event()
        original = self.chat.get_message_image
        def delayed(*args):
            started.set()
            release.wait(4)
            return original(*args)
        self.chat.get_message_image = delayed
        try:
            self.panel._open_event(QUrl("aura-image:picture"))
            spin_until(started.is_set)
            for _ in range(10):
                self.panel._open_event(QUrl("aura-image:picture"))
            self.assertEqual(len(self.panel._image_runner._tasks), 1)
            self.chat.open_conversation("old")
            self.assertIsNone(self.panel._image_preview_dialog)
        finally:
            release.set()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.assertIsNone(self.panel._image_preview_dialog)


if __name__ == "__main__":
    unittest.main()
