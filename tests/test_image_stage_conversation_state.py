"""Hidden real panel actions with owned images and deterministic worker gates."""
import base64
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.assistant_panel import AssistantChatPanel
from tests.test_assistant_ui import AssistantUiChat, wait_until


class ImageStageConversationStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.owner = tempfile.TemporaryDirectory(prefix="aura-owned-image-cid-")
        self.root = Path(self.owner.name).resolve()
        self.path = self.root / "owned.png"
        image = QImage(24, 16, QImage.Format.Format_ARGB32)
        image.fill(QColor("#75b8f4"))
        self.assertTrue(image.save(str(self.path), "PNG"))
        self.metadata = {"id": "a" * 64, "filename": "Owned.png", "width": 24, "height": 16,
                         "size": self.path.stat().st_size}
        self.image = {"data_base64": base64.b64encode(self.path.read_bytes()).decode("ascii")}
        self.chat = AssistantUiChat()
        self.panel = AssistantChatPanel(self.chat)
        self.entered, self.release = threading.Event(), threading.Event()
        self.import_calls = 0

    def tearDown(self):
        self.release.set()
        wait_until(lambda: not self.panel._image_stage_runner._tasks
                   and QThreadPool.globalInstance().activeThreadCount() == 0)
        for timer in (self.panel._web_publish_timer, self.panel._web_stream_timer,
                      self.panel._web_start_timer, self.panel._web_timeout): timer.stop()
        self.assertFalse(self.panel.isVisible())
        self.assertIsNone(self.panel.web_view)
        self.panel.deleteLater(); self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete); self.app.processEvents()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith("aura-owned-image-cid-"))
        self.owner.cleanup()

    def stage(self, *, held=False, failure=False):
        self.entered.clear(); self.release.clear()
        def read(path):
            self.assertEqual(Path(path), self.path)
            self.import_calls += 1; self.entered.set()
            if held: self.release.wait(3)
            if failure: raise ValueError("OWNED_SYNTHETIC_IMPORT_FAILURE")
            return self.metadata, self.image
        self.chat.import_local_image = read
        self.assertTrue(self.panel._stage_local_image(str(self.path), "current"))
        wait_until(self.entered.is_set)
        if not held: self.finish()

    def finish(self):
        self.release.set()
        wait_until(lambda: not self.panel._image_stage_runner._tasks and not self.panel._image_preparing)

    def test_error_only_appears_in_origin_and_returns_when_reopened(self):
        self.stage(failure=True)
        original = self.panel.web_state()["imageError"]
        self.assertTrue(original)
        self.chat.open_conversation("old")
        self.assertEqual(self.panel.web_state()["imageError"], "")
        self.chat.open_conversation("current")
        self.assertEqual(self.panel.web_state()["imageError"], original)

    def test_a_import_does_not_block_b_plain_text_and_a_image_survives(self):
        self.stage(held=True); self.chat.open_conversation("old")
        state = self.panel.web_state()
        self.assertFalse(state["imagePreparing"])
        self.assertTrue(state["imageImportBusy"])
        self.assertTrue(self.panel.dispatch_web_action("send", {"conversationId": "old", "text": "Owned B draft"}))
        self.assertEqual(self.chat.sent, ["Owned B draft"])
        self.assertTrue(self.panel._image_preparing)
        self.finish(); self.chat.open_conversation("current")
        self.assertEqual(self.panel._current_staged_image()["image_id"], "a" * 64)
        self.assertEqual(self.import_calls, 1)

    def test_b_remove_and_send_do_not_clear_a_error(self):
        self.stage(failure=True); original = self.panel.web_state()["imageError"]
        self.chat.open_conversation("old")
        self.panel._staged_images["old"] = {"conversationId": "old", "image_id": "b" * 64,
            **{key: self.metadata[key] for key in ("filename", "width", "height", "size")}}
        self.assertTrue(self.panel._remove_staged_image())
        self.assertTrue(self.panel.dispatch_web_action("send", {"conversationId": "old", "text": "Owned B text"}))
        self.chat.open_conversation("current")
        self.assertEqual(self.panel.web_state()["imageError"], original)

    def test_background_failure_is_visible_when_returning_to_origin(self):
        self.stage(held=True, failure=True); self.chat.open_conversation("old"); self.finish()
        self.assertEqual(self.panel.web_state()["imageError"], "")
        self.chat.open_conversation("current")
        self.assertTrue(self.panel.web_state()["imageError"])
        self.assertIsNone(self.panel._current_staged_image())

    def test_clear_delete_keep_worker_busy_and_discard_late_result(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                try:
                    self.chat.open_conversation("current"); self.stage(held=True)
                    self.chat._deleted_conversations = {"current"} if deleted else set()
                    self.panel._invalidate_staged_image("current")
                    self.assertTrue(self.panel._image_preparing)
                    self.assertFalse(self.panel.web_state()["imagePreparing"])
                    if not deleted:
                        self.assertTrue(self.panel.dispatch_web_action("send", {
                            "conversationId": "current", "text": "Owned cleared A text"}))
                    self.chat.open_conversation("old")
                    self.assertTrue(self.panel.dispatch_web_action("send", {
                        "conversationId": "old", "text": "Owned B text during invalidated A import"}))
                    calls = self.import_calls
                    self.assertFalse(self.panel._stage_local_image(str(self.path), "old"))
                    self.assertEqual(self.import_calls, calls)
                    self.finish(); self.chat.open_conversation("current")
                    self.assertIsNone(self.panel._current_staged_image())
                    self.assertEqual(self.panel.web_state()["imageError"], "")
                finally:
                    self.finish(); self.chat._deleted_conversations = set()

    def test_b_removes_its_image_during_a_import_without_cancelling_a(self):
        self.stage(held=True); self.chat.open_conversation("old")
        self.panel._staged_images["old"] = {"conversationId": "old", "image_id": "b" * 64,
            **{key: self.metadata[key] for key in ("filename", "width", "height", "size")}}
        self.panel._image_stage_changed()
        self.assertTrue(self.panel.image_remove_button.isEnabled())
        self.assertTrue(self.panel._remove_staged_image())
        self.assertTrue(self.panel._image_preparing)
        self.finish(); self.chat.open_conversation("current")
        self.assertEqual(self.panel._current_staged_image()["image_id"], "a" * 64)
        self.assertNotIn("old", self.panel._staged_images)

    def test_b_image_selector_stays_disabled_until_a_worker_finishes(self):
        self.stage(held=True); self.chat.open_conversation("old")
        self.assertFalse(self.panel.image_select_button.isEnabled())
        with patch("desktop_pet.ui.chat_panel.QFileDialog.getOpenFileName") as dialog:
            self.assertFalse(self.panel._select_image()); dialog.assert_not_called()
        self.finish()
        self.assertTrue(self.panel.image_select_button.isEnabled())


if __name__ == "__main__": unittest.main()
