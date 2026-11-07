"""Hidden panel reads with in-memory pictures and owned gate/clock fixtures."""

import json
import threading
import unittest

from PySide6.QtCore import QEvent, QThreadPool, Signal
from PySide6.QtWidgets import QApplication

from desktop_pet.image_storage import ImageStorageGate, ImageStorageGateError
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from tests.test_assistant_ui import wait_until
from tests.test_panel_image_storage_gate import OwnedGateChat, OwnedRunner
from tests.test_remote_controller import image_params


class EpochChat(OwnedGateChat):
    image_storage_content_changed = Signal(str)

    def __init__(self, gate):
        super().__init__(gate)
        self.epoch = 0
        self.image = image_params()
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.reads = 0
        self.read_lock = threading.Lock()
        self.active_reads = self.max_active_reads = 0
        self.messages = [{"id": "picture", "conversation_id": "current", "role": "user",
                          "content": "Owned picture", "metadata": {"image_id": "a" * 64}}]

    @property
    def image_storage_epoch(self):
        return self.epoch

    def get_message_image(self, cid, mid, image_id):
        with self.read_lock:
            self.reads += 1
            self.active_reads += 1
            self.max_active_reads = max(self.max_active_reads, self.active_reads)
        try:
            self.entered.set()
            if not self.release.wait(3):
                raise AssertionError("owned image read timed out")
            return self.image
        finally:
            with self.read_lock:
                self.active_reads -= 1

    def mutate(self, *, notify=True):
        self.epoch += 1
        if notify:
            self.image_storage_content_changed.emit("a" * 64)


class ImageCacheEpochTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.gate = ImageStorageGate()
        self.chat = EpochChat(self.gate)
        self.panel = AssistantChatPanel(self.chat)
        self.app.processEvents()
        self.panel._refresh_web_image_references()
        self.reference = self.panel._image_messages["picture"]
        self.ready = []
        self.owned_runners = []
        self.panel.bridge.thumbnailReady.connect(lambda value: self.ready.append(json.loads(value)))

    def tearDown(self):
        self.chat.release.set()
        for runner in self.owned_runners:
            if runner.thread is not None:
                runner.thread.join(3)
                self.assertFalse(runner.thread.is_alive())
        self.assertTrue(QThreadPool.globalInstance().waitForDone(3000))
        self.app.processEvents()
        if self.panel._image_preview_dialog is not None:
            self.panel._image_preview_dialog.reject()
        self.panel._shutdown_image_staging()
        for timer in (self.panel._web_publish_timer, self.panel._web_stream_timer,
                      self.panel._web_start_timer, self.panel._web_timeout):
            timer.stop()
        self.panel.deleteLater()
        self.chat.shutdown()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def held_thumbnail(self):
        self.chat.release.clear()
        self.assertTrue(self.panel._request_thumbnail(self.reference))
        self.assertTrue(self.chat.entered.wait(2))

    def finish(self):
        self.chat.release.set()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(3000))
        self.app.processEvents()

    def test_state_observes_epoch_before_queued_signal_and_drops_cached_pixels(self):
        thumbnail = {"src": "data:image/png;base64," + self.chat.image["data_base64"], "width": 24, "height": 16}
        self.panel._cache_thumbnail(self.reference, thumbnail, "")
        self.chat.mutate(notify=False)
        metadata = self.panel.web_state()["messages"][0]["metadata"]
        self.assertNotIn("image_thumbnail", metadata)
        self.assertEqual(metadata.get("image_revision"), 1)
        self.assertEqual(self.panel._thumbnail_cache_size, 0)

    def test_held_thumbnail_read_blocks_maintenance_until_real_completion(self):
        self.held_thumbnail()
        owner = self.gate.create_owner()
        try:
            with self.assertRaises(ImageStorageGateError) as caught:
                self.gate.maintenance(owner)
            self.assertEqual(caught.exception.code, "IMAGE_STORAGE_IN_USE")
        finally:
            self.gate.close_owner(owner)
        self.finish()
        self.assertEqual(self.gate.snapshot()["activity_count"], 0)

    def test_mutation_discards_held_thumbnail_without_clearing_inflight_capacity(self):
        self.held_thumbnail()
        self.assertEqual(len(self.panel._thumbnail_inflight), 1)
        self.chat.mutate()
        self.assertEqual(len(self.panel._thumbnail_inflight), 1)
        self.finish()
        self.assertNotIn(self.reference, self.panel._thumbnail_cache)
        self.assertEqual(self.ready, [])
        self.assertEqual(len(self.panel._thumbnail_inflight), 0)

    def test_preview_read_blocks_maintenance_and_mutation_rejects_late_pixels(self):
        self.chat.release.clear()
        self.panel._open_image_preview("picture")
        self.assertTrue(self.chat.entered.wait(2))
        owner = self.gate.create_owner()
        try:
            with self.assertRaises(ImageStorageGateError):
                self.gate.maintenance(owner)
        finally:
            self.gate.close_owner(owner)
        self.chat.mutate()
        self.assertIsNone(self.panel._image_preview_dialog)
        self.assertTrue(self.panel._image_preview_loading)
        self.finish()
        self.assertFalse(self.panel._image_preview_loading)
        self.assertIsNone(self.panel._image_preview_dialog)

    def test_revision_is_emitted_with_new_ready_and_same_hash_reloads(self):
        self.assertTrue(self.panel._request_thumbnail(self.reference))
        wait_until(lambda: len(self.ready) == 1)
        self.assertEqual(self.ready[0].get("imageRevision"), 0)
        self.chat.mutate()
        self.assertTrue(self.panel._request_thumbnail(self.reference))
        wait_until(lambda: len(self.ready) == 2)
        self.assertEqual(self.ready[-1].get("imageRevision"), 1)
        self.assertEqual(self.chat.reads, 2)

    def test_shutdown_running_read_stays_registered_until_physical_finally(self):
        self.held_thumbnail()
        self.chat.shutdown()
        self.assertGreater(self.gate.snapshot()["activity_count"], 0)
        self.finish()
        self.assertEqual(self.gate.snapshot()["activity_count"], 0)
        self.assertEqual(self.ready, [])

    def test_maintenance_rejects_new_read_without_queue_or_retry_loop(self):
        owner = self.gate.create_owner()
        token = self.gate.maintenance(owner)
        try:
            self.assertFalse(self.panel._request_thumbnail(self.reference))
            self.assertEqual(self.chat.reads, 0)
            self.assertEqual(self.panel._thumbnail_queue, {})
            self.assertEqual(self.panel._thumbnail_inflight, {})
        finally:
            self.gate.release(token)

    def test_read_submit_failures_before_entry_after_entry_and_after_finish_release_once(self):
        for mode in ("none_before", "raise_before", "none_after_entered", "raise_after_entered",
                     "none_after_finished", "raise_after_finished"):
            with self.subTest(mode=mode):
                self.panel._thumbnail_runner.shutdown()
                runner = OwnedRunner(self.chat.entered)
                runner.mode = mode
                self.owned_runners.append(runner)
                self.panel._thumbnail_runner = runner
                self.chat.mutate()
                self.chat.entered.clear()
                self.ready.clear()
                self.chat.release.clear()
                if mode.endswith("after_finished"):
                    self.chat.release.set()
                self.assertTrue(self.panel._request_thumbnail(self.reference))
                if mode.endswith("after_entered"):
                    self.assertGreater(self.gate.snapshot()["activity_count"], 0)
                    self.assertEqual(len(self.panel._thumbnail_inflight), 1)
                self.chat.release.set()
                if runner.thread is not None:
                    self.assertTrue(runner.done.wait(2))
                    runner.thread.join(2)
                wait_until(lambda: not self.panel._thumbnail_inflight, timeout=2)
                self.assertEqual(self.gate.snapshot()["activity_count"], 0)
                self.assertEqual(len(self.ready), 1)
                if runner.thread is not None:
                    runner.deliver()  # A duplicate late callback has no effect.
                    self.assertEqual(len(self.ready), 1)

    def test_completed_physical_read_keeps_completion_token_until_gui_delivery(self):
        self.held_thumbnail()
        self.chat.release.set()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(3000))
        self.assertEqual(self.gate.snapshot()["activity_count"], 1)
        self.assertEqual(len(self.panel._thumbnail_inflight), 1)
        self.app.processEvents()
        self.assertEqual(self.gate.snapshot()["activity_count"], 0)
        self.assertEqual(len(self.panel._thumbnail_inflight), 0)

    def test_epoch_clear_preserves_two_real_inflight_slots_and_discards_old_queue(self):
        self.chat.messages.extend([
            {"id": mid, "conversation_id": "current", "role": "user", "content": "Owned duplicate reference",
             "metadata": {"image_id": "a" * 64}} for mid in ("picture2", "picture3")])
        self.panel._refresh_web_image_references()
        references = list(self.panel._image_messages.values())
        self.chat.release.clear()
        for reference in references:
            self.assertTrue(self.panel._request_thumbnail(reference))
        wait_until(lambda: self.chat.reads == 2, timeout=2)
        self.assertEqual(len(self.panel._thumbnail_inflight), 2)
        self.assertEqual(len(self.panel._thumbnail_queue), 1)
        self.chat.mutate()
        self.assertEqual(len(self.panel._thumbnail_inflight), 2)
        self.assertEqual(len(self.panel._thumbnail_queue), 0)
        self.assertTrue(self.panel._request_thumbnail(references[0]))
        self.assertEqual(self.chat.reads, 2)
        self.chat.release.set()
        wait_until(lambda: not self.panel._thumbnail_inflight and not self.panel._thumbnail_queue, timeout=3)
        self.assertLessEqual(self.chat.max_active_reads, 2)
        self.assertEqual(self.chat.reads, 3)
        self.assertEqual(len(self.ready), 1)
        self.assertEqual(self.ready[0]["imageRevision"], 1)


if __name__ == "__main__":
    unittest.main()
