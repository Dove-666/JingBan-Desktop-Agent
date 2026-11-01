"""The shipped production React/Qt bridge exposes safe image attachments."""

import json
import base64
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QByteArray, QBuffer, QEvent, QIODevice, QThreadPool
from PySide6.QtGui import QColor, QDesktopServices, QImage
from PySide6.QtWidgets import QApplication, QLabel

from desktop_pet.api_client import ApiError
from desktop_pet.chat_images import IMAGE_ERRORS
from tests.test_assistant_ui import AssistantUiChat, bridge_action, click_dom, eval_js, make_panel, set_dom_text, wait_js
from tests.test_remote_controller import image_params, spin_until


class WebImageChat(AssistantUiChat):
    def __init__(self):
        super().__init__()
        self.messages = [{"id": "picture", "conversation_id": "current", "role": "user", "content": "查看手机发来的图片",
                          "metadata": {"image_id": "a" * 64, "request_status": "complete",
                            "image_path": "C:/PRIVATE_PATH/image.png", "filename": '<img src="file:///PRIVATE_FILE">',
                            "image_url": "data:image/png;base64,PRIVATE_DATA", "api_key": "PRIVATE_KEY"}}]
        self.image_calls = []
        self.image = image_params()
        self.image_error = None
        self.started = threading.Event()
        self.blocker = None

    def get_message_image(self, cid, mid, image_id):
        self.image_calls.append((cid, mid, image_id, threading.get_ident()))
        self.started.set()
        if self.blocker is not None:
            self.blocker.wait(4)
        if self.image_error is not None:
            raise ApiError(self.image_error, IMAGE_ERRORS[self.image_error])
        return self.image


class ChatImageWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = WebImageChat()
        self.panel = make_panel(self.chat)

    def tearDown(self):
        if self.chat.blocker is not None:
            self.chat.blocker.set()
        if self.panel._image_preview_dialog is not None:
            self.panel._image_preview_dialog.reject()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.panel.hide()
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def remount(self):
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.panel.hide()
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.chat.started.clear()
        self.chat.image_calls.clear()
        self.panel = make_panel(self.chat)

    def wait_thumbnail(self):
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=image-thumbnail]')?.complete && document.querySelector('[data-testid=image-thumbnail]')?.naturalWidth > 0)")

    def test_shipped_renderer_automatically_displays_safe_thumbnail_without_any_click(self):
        self.wait_thumbnail()
        self.assertEqual(eval_js(self.panel, "document.querySelectorAll('[data-testid=image-thumbnail]').length"), 1)
        for _ in range(3):
            self.panel.web_state()
        self.assertEqual(len(self.chat.image_calls), 1)
        self.assertNotEqual(self.chat.image_calls[0][3], threading.get_ident())
        state = self.panel.web_state()
        self.assertEqual(state["messages"][0]["metadata"]["image_id"], "a" * 64)
        thumb = state["messages"][0]["metadata"]["image_thumbnail"]
        self.assertTrue(thumb["src"].startswith("data:image/png;base64,"))
        self.assertEqual((thumb["width"], thumb["height"]), (24, 16))
        serialized = json.dumps(state)
        dom = eval_js(self.panel, "document.querySelector('.message').innerHTML")
        for private in ("PRIVATE_PATH", "PRIVATE_FILE", "PRIVATE_DATA", "PRIVATE_KEY", "file:///"):
            self.assertNotIn(private, serialized)
            self.assertNotIn(private, dom)

    def test_real_composer_picker_previews_and_image_only_send_retains_stage_until_accepted(self):
        self.wait_thumbnail()
        metadata = {"id": "b" * 64, "filename": "public.png", "width": 24, "height": 16, "size": 100}
        calls = []
        original_send = self.chat.send

        def send(text, **options):
            calls.append((text, options))
            return original_send(text)

        with patch.object(self.chat, "import_local_image", lambda path: (metadata, self.chat.image), create=True), \
                patch.object(self.chat, "send", send), \
                patch("desktop_pet.ui.chat_panel.QFileDialog.getOpenFileName", return_value=("C:/PRIVATE_DIRECTORY/public.png", "")):
            click_dom(self.panel, "[data-testid=composer-image-picker]")
            wait_js(self.panel, "Boolean(document.querySelector('[data-testid=composer-attachment] img')?.naturalWidth > 0)")
            self.assertNotIn("PRIVATE_DIRECTORY", eval_js(self.panel, "document.querySelector('.composer').innerHTML"))
            self.chat.accept_sends = False
            click_dom(self.panel, "[data-testid=send-button]")
            spin_until(lambda: bool(calls))
            self.assertEqual(calls[-1], ("请描述这张图片。", {"image_id": "b" * 64}))
            self.assertIsNotNone(self.panel._current_staged_image())
            self.chat.accept_sends = True
            click_dom(self.panel, "[data-testid=send-button]")
            wait_js(self.panel, "!document.querySelector('[data-testid=composer-attachment]')")
            self.assertIsNone(self.panel._current_staged_image())

    def test_composer_remove_keeps_draft_and_web_payload_cannot_supply_file_paths(self):
        self.wait_thumbnail()
        metadata = {"id": "b" * 64, "filename": "public.png", "width": 24, "height": 16, "size": 100}
        with patch.object(self.chat, "import_local_image", lambda path: (metadata, self.chat.image), create=True), \
                patch("desktop_pet.ui.chat_panel.QFileDialog.getOpenFileName", return_value=("C:/PRIVATE_DIRECTORY/public.png", "")) as picker:
            for payload in ({"conversationId": "old"}, {"conversationId": "current", "path": "C:/PRIVATE_FILE"}):
                self.assertFalse(bridge_action(self.panel, "selectImage", **payload)["ok"])
            picker.assert_not_called()
            click_dom(self.panel, "[data-testid=composer-image-picker]")
            wait_js(self.panel, "Boolean(document.querySelector('[data-testid=composer-attachment]')) && !document.querySelector('[data-testid=remove-composer-image]')?.disabled")
            set_dom_text(self.panel, "[data-testid=message-input]", "保留未发送问题")
            click_dom(self.panel, "[data-testid=remove-composer-image]")
            wait_js(self.panel, "!document.querySelector('[data-testid=composer-attachment]')")
            self.assertEqual(eval_js(self.panel, "document.querySelector('[data-testid=message-input]').value"), "保留未发送问题")

    def test_real_dom_button_opens_native_preview_from_worker_and_keeps_web_ui(self):
        self.wait_thumbnail()
        main_thread = threading.get_ident()
        with patch.object(QDesktopServices, "openUrl") as external:
            click_dom(self.panel, "[data-testid=image-attachment] button")
            spin_until(lambda: self.panel._image_preview_dialog is not None)
            dialog = self.panel._image_preview_dialog
            status = dialog.findChild(QLabel, "chatImagePreviewStatus")
            spin_until(lambda: status.text() == "图片附件")
            self.assertFalse(dialog.findChild(QLabel, "chatImagePreviewImage").pixmap().isNull())
            self.assertEqual(self.chat.image_calls[-1][:3], ("current", "picture", "a" * 64))
            self.assertNotEqual(self.chat.image_calls[-1][3], main_thread)
            self.assertEqual(len(self.chat.image_calls), 2)
            self.assertFalse(self.panel._native_mode)
            self.assertTrue(self.panel.splitter.isHidden())
            external.assert_not_called()

    def test_forged_bridge_actions_cannot_select_another_message_hash_or_conversation(self):
        self.wait_thumbnail()
        calls = len(self.chat.image_calls)
        for payload in ({"conversationId": "old", "messageId": "picture"},
                        {"conversationId": "current", "messageId": "a" * 64},
                        {"conversationId": "current", "messageId": "../private.png"},
                        {"conversationId": "current", "messageId": "unknown"}):
            self.assertFalse(bridge_action(self.panel, "openImage", **payload)["ok"])
            self.assertFalse(bridge_action(self.panel, "loadImageThumbnail", **payload)["ok"])
        self.assertFalse(bridge_action(self.panel, "loadImageThumbnail", conversationId="current", messageId="picture", imageId="b" * 64)["ok"])
        self.assertEqual(len(self.chat.image_calls), calls)
        self.assertIsNone(self.panel._image_preview_dialog)

    def test_invalid_and_cross_session_metadata_show_unavailable_without_reference(self):
        for value in ("C:/PRIVATE_PATH/image.png", "F" * 64, {}, None):
            self.chat.messages[0]["metadata"]["image_id"] = value
            self.chat.messages_changed.emit()
            wait_js(self.panel, "Boolean(document.querySelector('[data-testid=image-attachment-unavailable]'))")
            self.assertFalse(eval_js(self.panel, "Boolean(document.querySelector('[data-testid=image-attachment]'))"))
            self.assertNotIn("image_id", self.panel.web_state()["messages"][0]["metadata"])
        self.chat.messages[0]["metadata"]["image_id"] = "a" * 64
        self.chat.messages[0]["conversation_id"] = "old"
        self.chat.messages_changed.emit()
        self.assertNotIn("image_id", self.panel.web_state()["messages"][0]["metadata"])
        self.assertFalse(bridge_action(self.panel, "openImage", conversationId="current", messageId="picture")["ok"])

    def test_three_safe_error_codes_are_visible_in_actual_renderer(self):
        for code, expected in IMAGE_ERRORS.items():
            self.chat.messages[0]["metadata"].update(request_status="failed", image_error_code=code,
                                                    error_message="PRIVATE_PROVIDER_BODY")
            self.chat.messages_changed.emit()
            wait_js(self.panel, "document.querySelector('[data-testid=image-attachment-error]')?.textContent === " + json.dumps(expected))
            self.assertNotIn("PRIVATE_PROVIDER_BODY", json.dumps(self.panel.web_state()))
        self.chat.messages[0]["metadata"]["image_error_code"] = "PRIVATE_PROVIDER_BODY"
        self.chat.messages_changed.emit()
        wait_js(self.panel, "!document.querySelector('[data-testid=image-attachment-error]')")

    def test_missing_image_and_conversation_switch_use_native_safe_error_and_discard(self):
        self.wait_thumbnail()
        self.chat.image_error = "image_missing"
        click_dom(self.panel, "[data-testid=image-attachment] button")
        spin_until(lambda: self.panel._image_preview_dialog is not None)
        dialog = self.panel._image_preview_dialog
        status = dialog.findChild(QLabel, "chatImagePreviewStatus")
        spin_until(lambda: status.text() == IMAGE_ERRORS["image_missing"])
        self.chat.open_conversation("old")
        self.assertIsNone(self.panel._image_preview_dialog)

    def test_inflight_thumbnail_requests_are_deduplicated_and_cached_without_redecode(self):
        self.wait_thumbnail()
        blocker = threading.Event()
        self.chat.blocker = blocker
        self.remount()
        spin_until(self.chat.started.is_set)
        for _ in range(12):
            self.assertTrue(bridge_action(self.panel, "loadImageThumbnail", conversationId="current", messageId="picture")["ok"])
        self.assertEqual(len(self.chat.image_calls), 1)
        blocker.set()
        self.wait_thumbnail()
        for _ in range(12):
            self.assertTrue(bridge_action(self.panel, "loadImageThumbnail", conversationId="current", messageId="picture")["ok"])
        spin_until(lambda: not self.panel._thumbnail_inflight)
        self.assertEqual(len(self.chat.image_calls), 1)

    def test_missing_thumbnail_automatically_shows_only_a_static_error_without_retry(self):
        self.wait_thumbnail()
        self.chat.image_error = "image_missing"
        self.remount()
        wait_js(self.panel, "document.querySelector('[data-testid=image-thumbnail-error]')?.textContent === " + json.dumps(IMAGE_ERRORS["image_missing"]))
        self.assertEqual(len(self.chat.image_calls), 1)
        for _ in range(3):
            self.panel._publish_web_state()
            self.app.processEvents()
        self.assertEqual(len(self.chat.image_calls), 1)
        self.assertNotIn("image_thumbnail", self.panel.web_state()["messages"][0]["metadata"])
        self.assertFalse(eval_js(self.panel, "Boolean(document.querySelector('[data-testid=image-thumbnail]'))"))

    def test_large_image_is_bounded_and_source_png_metadata_is_not_copied(self):
        self.wait_thumbnail()
        image = QImage(2000, 1000, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#75b8f4"))
        image.setText("private", "PRIVATE_KEY PRIVATE_PATH PRIVATE_DATA")
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, "PNG"))
        original = base64.b64encode(bytes(data)).decode("ascii")
        self.chat.image = {"data_base64": original}
        self.remount()
        self.wait_thumbnail()
        thumb = self.panel.web_state()["messages"][0]["metadata"]["image_thumbnail"]
        self.assertEqual((thumb["width"], thumb["height"]), (640, 320))
        self.assertNotIn(original, json.dumps(self.panel.web_state()))
        decoded = QImage.fromData(base64.b64decode(thumb["src"].split(",", 1)[1]))
        self.assertEqual(decoded.textKeys(), [])
        self.assertLessEqual(eval_js(self.panel, "document.querySelector('[data-testid=image-thumbnail]').getBoundingClientRect().width"), 360)
        self.assertNotIn("PRIVATE_KEY", json.dumps(self.panel.web_state()))

    def test_late_thumbnail_after_conversation_switch_never_enters_new_state_or_dom(self):
        self.wait_thumbnail()
        blocker = threading.Event()
        self.chat.blocker = blocker
        self.remount()
        spin_until(self.chat.started.is_set)
        # WebImageChat inherits SessionChat's synchronous history switch.
        self.assertTrue(self.chat.open_conversation("old"))
        self.assertEqual(self.chat.current_conversation_id, "old")
        blocker.set()
        spin_until(lambda: not self.panel._thumbnail_inflight)
        self.assertEqual(self.panel.web_state()["conversationId"], "old")
        self.assertTrue(all("image_thumbnail" not in row["metadata"] for row in self.panel.web_state()["messages"]))
        self.assertFalse(any(key[0] == "current" for key in self.panel._thumbnail_cache))
        wait_js(self.panel, "!document.querySelector('[data-testid=image-thumbnail]')")

    def test_late_thumbnail_after_message_deletion_is_discarded(self):
        self.wait_thumbnail()
        blocker = threading.Event()
        self.chat.blocker = blocker
        self.remount()
        spin_until(self.chat.started.is_set)
        self.chat.messages = []
        self.chat.messages_changed.emit()
        blocker.set()
        spin_until(lambda: not self.panel._thumbnail_inflight)
        self.assertEqual(self.panel.web_state()["messages"], [])
        self.assertEqual(self.panel._thumbnail_cache_size, 0)
        wait_js(self.panel, "!document.querySelector('[data-testid=image-thumbnail]')")

    def test_cache_entry_and_byte_budgets_remain_bounded_without_a_render_retry_loop(self):
        self.wait_thumbnail()
        self.chat.messages = [{**self.chat.messages[0], "id": f"picture-{index}"} for index in range(3)]
        with patch("desktop_pet.ui.assistant_panel.THUMBNAIL_CACHE_ENTRIES", 2), patch("desktop_pet.ui.assistant_panel.THUMBNAIL_CACHE_BYTES", 700):
            self.remount()
            wait_js(self.panel, "document.querySelectorAll('[data-testid=image-thumbnail]').length === 3 && Array.from(document.querySelectorAll('[data-testid=image-thumbnail]')).every(image => image.complete && image.naturalWidth > 0)")
            spin_until(lambda: not self.panel._thumbnail_inflight and not self.panel._thumbnail_queue)
            self.assertLessEqual(len(self.panel._thumbnail_cache), 2)
            self.assertLessEqual(self.panel._thumbnail_cache_size, 700)
            for _ in range(3):
                self.panel._publish_web_state()
                self.app.processEvents()
            self.assertEqual(len(self.chat.image_calls), 3)

    def test_scrolling_far_away_releases_dom_pixels_and_reentry_uses_cached_thumbnail(self):
        self.wait_thumbnail()
        original = self.chat.messages[0]
        self.chat.messages = [original] + [{"id": f"text-{index}", "conversation_id": "current", "role": "assistant",
            "content": "一段用于检验长会话按需图片显示的合成文字。 " * 12} for index in range(30)]
        self.chat.messages_changed.emit()
        wait_js(self.panel, "document.querySelectorAll('[data-message-id]').length === 31")
        eval_js(self.panel, "(() => { const v = document.querySelector('.thread-viewport'); v.scrollTop = v.scrollHeight; return true; })()")
        wait_js(self.panel, "!document.querySelector('[data-testid=image-thumbnail]')")
        calls = len(self.chat.image_calls)
        eval_js(self.panel, "(() => { document.querySelector('.thread-viewport').scrollTop = 0; return true; })()")
        self.wait_thumbnail()
        self.assertEqual(len(self.chat.image_calls), calls)


if __name__ == "__main__":
    unittest.main()
