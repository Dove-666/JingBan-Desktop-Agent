"""Hidden real panels with shared gate and exclusively owned in-memory images."""

from __future__ import annotations

import base64
import sys
import threading
import traceback
import unittest
from unittest.mock import patch

from PySide6.QtCore import QByteArray, QBuffer, QEvent, QIODevice, QThreadPool, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ApiError
from desktop_pet.async_tasks import TaskRunner
from desktop_pet.image_storage import ImageStorageGate, ImageStorageGateError
from desktop_pet.ui.assistant_panel import AssistantChatPanel
from desktop_pet.ui.chat_panel import ChatPanel, IMAGE_STAGE_ERROR
from tests.test_assistant_ui import AssistantUiChat, wait_until


class OwnedGateChat(AssistantUiChat):
    image_storage_changed = Signal()

    def __init__(self, gate):
        super().__init__()
        self.image_storage_gate = gate
        self._closed = False
        self._deleted_conversations = set()
        self._invalidating_conversations = set()
        self.notifications = 0
        self.send_snapshots = []
        self.send_token = None
        self.gate_owner = gate.create_owner()

    def notify_image_storage_changed(self):
        self.notifications += 1
        self.image_storage_changed.emit()

    def send(self, text, **options):
        if not self.accept_sends:
            return False
        try:
            self.send_token = self.image_storage_gate.activity(self.gate_owner)
        except ImageStorageGateError:
            return False
        self.send_snapshots.append(self.image_storage_gate.snapshot())
        self.sent.append(text)
        return True

    def shutdown(self):
        self._closed = True
        self.image_storage_gate.close_owner(self.gate_owner)
        self.notify_image_storage_changed()


class OwnedRunner:
    """Delay the real callable and its GUI callback independently.

    Optional submit failures model Qt accepting entry before start raises. The
    single explicitly owned Python thread performs no disk or network IO.
    """

    def __init__(self, entered):
        self.entered = entered
        self._closed = False
        self._tasks = {}
        self.mode = "queued"
        self.thread = None
        self.done = threading.Event()
        self.function = self.callback = self.result = None
        self.error = ""

    def submit(self, function, callback):
        self.function, self.callback = function, callback
        self._tasks["owned"] = (function, callback)
        if self.mode in {"none_before", "raise_before"}:
            self._tasks.clear()
            if self.mode == "raise_before":
                raise RuntimeError("OWNED_SUBMISSION_BEFORE_ENTRY")
            return None
        if self.mode.endswith("after_entered"):
            self.start()
            if not self.entered.wait(2):
                raise AssertionError("owned importer did not enter")
        elif self.mode.endswith("after_finished"):
            self.start()
            if not self.done.wait(2):
                raise AssertionError("owned importer did not finish")
        if self.mode.startswith("raise_"):
            raise RuntimeError("OWNED_SUBMISSION_AFTER_ENTRY")
        return None if self.mode.startswith("none_") else "owned"

    def start(self):
        def run():
            try:
                self.result = self.function()
            except Exception:
                self.error = "Owned worker failed"
            finally:
                self.done.set()
        self.thread = threading.Thread(target=run, name="aura-owned-panel-image-test", daemon=True)
        self.thread.start()

    def deliver(self):
        if not self.done.is_set():
            raise AssertionError("callback cannot precede owned physical completion")
        self._tasks.clear()
        if not self._closed:
            self.callback(self.result, self.error)

    def shutdown(self, **kwargs):
        self._closed = True


class PanelImageStorageGateTests(unittest.TestCase):
    last_constructor_exceptions = []
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.gate = ImageStorageGate()
        self.chat = OwnedGateChat(self.gate)
        self.panel = AssistantChatPanel(self.chat)
        self.app.processEvents()  # Settle constructor's existing draft-ready timer.
        self.panels = [self.panel]
        self.extra_chats = []
        self.runners = []
        self.release = threading.Event()
        self.entered = threading.Event()
        self.import_calls = 0
        self.fail_import = None
        image = QImage(23, 17, QImage.Format.Format_ARGB32)
        image.fill(QColor("#75b8f4"))
        output = QByteArray()
        buffer = QBuffer(output)
        self.assertTrue(buffer.open(QIODevice.OpenModeFlag.WriteOnly))
        self.assertTrue(image.save(buffer, "PNG"))
        self.encoded = base64.b64encode(bytes(output)).decode("ascii")
        self.metadata = {"id": "a" * 64, "filename": "Owned.png", "width": 23, "height": 17, "size": len(output)}
        self.chat.import_local_image = self.importer
        self.install_runner()

    def tearDown(self):
        self.release.set()
        for runner in self.runners:
            if runner.thread is not None:
                runner.thread.join(3)
                self.assertFalse(runner.thread.is_alive())
        for panel in self.panels:
            shutdown = getattr(panel, "_shutdown_image_staging", None)
            if callable(shutdown): shutdown()
            for name in ("_web_publish_timer", "_web_stream_timer", "_web_start_timer", "_web_timeout"):
                timer = getattr(panel, name, None)
                if timer is not None: timer.stop()
            self.assertFalse(panel.isVisible())
            self.assertIsNone(getattr(panel, "web_view", None))
            panel.deleteLater()
        self.chat.shutdown()
        self.chat.deleteLater()
        for chat in self.extra_chats:
            chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def install_runner(self, panel=None):
        panel = panel or self.panel
        panel._image_stage_runner.shutdown()
        runner = OwnedRunner(self.entered)
        panel._image_stage_runner = runner
        self.runners.append(runner)
        return runner

    @property
    def runner(self):
        return self.panel._image_stage_runner

    def importer(self, _owned_path):
        self.import_calls += 1
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError("owned importer release timed out")
        if self.fail_import is not None:
            raise self.fail_import
        return self.metadata, {"data_base64": self.encoded}

    def stage(self, *, finish=True):
        self.entered.clear()
        self.release.clear()
        self.assertTrue(self.panel._stage_local_image("owned-memory-only", self.chat.current_conversation_id))
        self.runner.start()
        self.assertTrue(self.entered.wait(2))
        if finish:
            self.finish()

    def finish(self, *, deliver=True):
        self.release.set()
        self.assertTrue(self.runner.done.wait(2))
        if self.runner.thread is not None:
            self.runner.thread.join(2)
        if deliver:
            self.runner.deliver()
        self.app.processEvents()

    def snapshot(self):
        return self.gate.snapshot()

    def test_maintenance_rejects_import_before_mutating_draft_or_valid_attachment(self):
        self.panel._staged_images["current"] = {"image_id": "b" * 64, "conversationId": "current", "filename": "Old.png"}
        self.panel.input.setPlainText("Owned unsent draft")
        old = dict(self.panel._staged_images["current"])
        owner = self.gate.create_owner()
        token = self.gate.maintenance(owner)
        try:
            self.chat.notify_image_storage_changed()
            self.assertFalse(self.panel._stage_local_image("not-opened", "current"))
            self.assertEqual(self.panel._staged_images["current"], old)
            self.assertEqual(self.panel.input.toPlainText(), "Owned unsent draft")
            self.assertEqual(self.import_calls, 0)
            self.assertIsNone(self.runner.function)
        finally:
            self.gate.release(token)

    def test_pending_import_reserves_gate_before_worker_entry(self):
        self.assertTrue(self.panel._stage_local_image("owned-memory-only", "current"))
        self.assertEqual(self.snapshot()["activity_count"], 1)
        self.assertEqual(self.snapshot()["stage_count"], 0)
        self.assertEqual(self.import_calls, 0)

    def test_physical_completion_keeps_activity_until_gui_handoff_to_stage(self):
        self.stage(finish=False)
        self.finish(deliver=False)
        self.assertEqual(self.snapshot()["activity_count"], 1)
        self.assertEqual(self.snapshot()["stage_count"], 0)
        self.assertTrue(self.panel._image_preparing)
        self.runner.deliver()
        self.assertEqual(self.snapshot()["activity_count"], 0)
        self.assertEqual(self.snapshot()["stage_count"], 1)
        self.assertEqual(self.panel._current_staged_image()["image_id"], "a" * 64)

    def test_each_panel_has_distinct_owner_but_shares_gate(self):
        native = ChatPanel(self.chat)
        self.app.processEvents()
        self.panels.append(native)
        self.install_runner(native)
        self.assertIs(native._image_storage_gate, self.panel._image_storage_gate)
        self.assertIsNot(native._image_storage_owner, self.panel._image_storage_owner)

    def test_all_cid_stage_tokens_survive_switch_and_remove_only_current(self):
        self.stage()
        self.chat.open_conversation("old")
        self.install_runner()
        self.stage()
        self.assertEqual(self.snapshot()["stage_count"], 2)
        self.assertTrue(self.panel._remove_staged_image())
        self.assertEqual(self.snapshot()["stage_count"], 1)
        self.assertIn("current", self.panel._staged_images)
        self.chat.open_conversation("current")
        self.panel._invalidate_staged_image("current")
        self.assertEqual(self.snapshot()["stage_count"], 0)

    def test_accepted_replacement_handoffs_new_stage_before_releasing_old(self):
        self.stage()
        old_token = self.panel._image_stage_tokens["current"]
        self.install_runner()
        self.stage(finish=False)
        self.assertEqual(self.snapshot()["stage_count"], 1)
        self.assertEqual(self.snapshot()["activity_count"], 1)
        release = self.gate.release
        counts = []
        def observe(token):
            if token is old_token:
                counts.append(self.snapshot()["stage_count"])
            return release(token)
        with patch.object(self.gate, "release", side_effect=observe):
            self.finish()
        self.assertEqual(counts, [2])
        self.assertEqual(self.snapshot()["stage_count"], 1)

    def test_accepted_replacement_failure_preserves_existing_pop_semantics_without_ghost_token(self):
        self.stage()
        self.install_runner()
        self.fail_import = ApiError("image_unavailable", "Owned invalid image")
        self.stage()
        self.assertIsNone(self.panel._current_staged_image())
        self.assertTrue(self.panel._image_stage_error)
        self.assertEqual(self.snapshot()["activity_count"], 0)
        self.assertEqual(self.snapshot()["stage_count"], 0)

    def test_import_exception_retires_from_worker_finally_without_gui_callback(self):
        for failure in (ApiError("image_unavailable", "Owned invalid image"), ValueError("Owned decode failed")):
            with self.subTest(kind=type(failure).__name__):
                self.install_runner()
                self.fail_import = failure
                self.stage(finish=False)
                self.finish(deliver=False)
                self.assertEqual(self.snapshot()["activity_count"], 0)
                self.runner.deliver()
                self.assertEqual(self.snapshot()["stage_count"], 0)

    def test_clear_or_delete_running_import_only_discards_until_physical_finally(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                self.install_runner()
                self.stage(finish=False)
                if deleted:
                    self.chat._deleted_conversations.add("current")
                self.panel._invalidate_staged_image("current")
                self.assertEqual(self.snapshot()["activity_count"], 1)
                self.assertTrue(self.panel._image_preparing)
                self.finish(deliver=False)
                self.assertEqual(self.snapshot()["activity_count"], 0)
                self.runner.deliver()
                self.assertIsNone(self.panel._current_staged_image())
                self.assertEqual(self.snapshot()["stage_count"], 0)
                self.chat._deleted_conversations.clear()

    def test_idle_shutdown_releases_all_stages_without_hiding_as_shutdown(self):
        self.stage()
        self.panel.hide()
        self.assertEqual(self.snapshot()["stage_count"], 1)
        self.chat.shutdown()
        self.assertEqual(self.snapshot()["stage_count"], 0)
        self.assertFalse(self.panel._stage_local_image("not-opened", "current"))

    def test_shutdown_running_import_keeps_blocker_until_worker_finally_and_never_restages(self):
        self.stage(finish=False)
        self.chat.shutdown()
        self.assertEqual(self.snapshot()["activity_count"], 1)
        self.finish(deliver=False)
        self.assertEqual(self.snapshot()["activity_count"], 0)
        self.runner.deliver()
        self.assertEqual(self.snapshot()["stage_count"], 0)
        self.assertIsNone(self.panel._current_staged_image())

    def test_submit_none_or_raise_before_entry_cancels_reservation_and_preserves_text(self):
        for mode in ("none_before", "raise_before"):
            with self.subTest(mode=mode):
                self.install_runner()
                self.runner.mode = mode
                self.panel.input.setPlainText("Owned draft")
                self.assertFalse(self.panel._stage_local_image("not-opened", "current"))
                self.assertEqual(self.import_calls, 0)
                self.assertEqual(self.snapshot()["activity_count"], 0)
                self.assertFalse(self.panel._image_preparing)
                self.assertEqual(self.panel.input.toPlainText(), "Owned draft")

    def test_submit_none_or_raise_after_entry_cannot_release_physical_activity_early(self):
        for mode in ("none_after_entered", "raise_after_entered"):
            with self.subTest(mode=mode):
                self.install_runner()
                self.runner.mode = mode
                self.entered.clear()
                self.release.clear()
                self.assertFalse(self.panel._stage_local_image("owned-memory-only", "current"))
                self.assertTrue(self.entered.is_set())
                self.assertEqual(self.snapshot()["activity_count"], 1)
                self.assertTrue(self.panel._image_preparing)
                self.finish(deliver=False)
                wait_until(lambda: not self.panel._image_preparing, timeout=2)
                self.assertEqual(self.snapshot()["activity_count"], 0)
                self.assertEqual(self.snapshot()["stage_count"], 0)

    def test_submit_none_or_raise_after_physical_finish_releases_awaiting_stage(self):
        for mode in ("none_after_finished", "raise_after_finished"):
            with self.subTest(mode=mode):
                self.install_runner()
                self.runner.mode = mode
                self.release.set()
                self.assertFalse(self.panel._stage_local_image("owned-memory-only", "current"))
                self.assertEqual(self.snapshot()["activity_count"], 0)
                self.assertEqual(self.snapshot()["stage_count"], 0)
                self.assertFalse(self.panel._image_preparing)

    def test_send_retains_stage_until_controller_has_admitted_own_activity(self):
        self.stage()
        self.assertTrue(self.panel.dispatch_web_action("send", {"conversationId": "current", "text": "Owned request"}))
        self.assertEqual(self.chat.send_snapshots[-1]["activity_count"], 1)
        self.assertEqual(self.chat.send_snapshots[-1]["stage_count"], 1)
        self.assertEqual(self.snapshot()["activity_count"], 1)
        self.assertEqual(self.snapshot()["stage_count"], 0)

    def test_rejected_send_keeps_attachment_stage_and_draft(self):
        self.stage()
        self.chat.accept_sends = False
        self.panel.input.setPlainText("Owned rejected draft")
        self.assertFalse(self.panel.dispatch_web_action("send", {"conversationId": "current", "text": "Owned rejected draft"}))
        self.assertEqual(self.snapshot()["stage_count"], 1)
        self.assertEqual(self.panel.input.toPlainText(), "Owned rejected draft")

    def test_maintenance_projects_busy_without_changing_controller_or_stop_authority(self):
        owner = self.gate.create_owner()
        token = self.gate.maintenance(owner)
        try:
            self.chat.can_stop = False
            self.chat.notify_image_storage_changed()
            state = self.panel.web_state()
            self.assertTrue(state["busy"])
            self.assertTrue(state["imageImportBusy"])
            self.assertEqual(state["status"], "正在管理图片…")
            self.assertFalse(state["canStop"])
            self.assertFalse(state["requestInCurrent"])
            self.assertFalse(self.chat.busy)
            self.assertFalse(self.panel.send_button.isEnabled())
            self.assertFalse(self.panel.image_select_button.isEnabled())
            self.assertFalse(self.panel.dispatch_web_action("send", {"conversationId": "current", "text": "Owned blocked request"}))
            self.chat.can_stop = True
            self.assertTrue(self.panel.web_state()["canStop"])
        finally:
            self.gate.release(token)
            self.chat.notify_image_storage_changed()
        self.assertFalse(self.panel.web_state()["busy"])

    def test_cancel_pending_then_late_worker_never_calls_importer(self):
        self.assertTrue(self.panel._stage_local_image("owned-memory-only", "current"))
        self.panel._invalidate_staged_image("current")
        self.assertEqual(self.snapshot()["activity_count"], 0)
        self.runner.start()
        self.finish()
        self.assertEqual(self.import_calls, 0)
        self.assertEqual(self.snapshot()["stage_count"], 0)
        self.assertIsNone(self.panel._current_staged_image())

    def test_closed_owner_late_gui_callback_cannot_create_stage(self):
        self.stage(finish=False)
        self.finish(deliver=False)
        self.gate.close_owner(self.panel._image_storage_owner)
        self.runner.deliver()
        self.assertEqual(self.snapshot()["activity_count"], 0)
        self.assertEqual(self.snapshot()["stage_count"], 0)
        self.assertIsNone(self.panel._current_staged_image())

    def test_widget_destruction_keeps_running_import_until_real_finally(self):
        self.stage(finish=False)
        panel, runner = self.panel, self.runner
        self.panels.remove(panel)
        panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertEqual(self.snapshot()["activity_count"], 1)
        self.release.set()
        self.assertTrue(runner.done.wait(2))
        runner.thread.join(2)
        self.assertEqual(self.snapshot()["activity_count"], 0)
        self.assertEqual(self.snapshot()["stage_count"], 0)

    def test_legacy_chat_without_gate_keeps_existing_import_path(self):
        legacy = AssistantUiChat()
        self.extra_chats.append(legacy)
        legacy.import_local_image = self.importer
        panel = AssistantChatPanel(legacy)
        self.app.processEvents()
        self.panels.append(panel)
        runner = self.install_runner(panel)
        self.release.set()
        self.assertIsNone(panel._image_storage_gate)
        self.assertTrue(panel._stage_local_image("owned-memory-only", "current"))
        runner.start()
        self.assertTrue(runner.done.wait(2))
        runner.thread.join(2)
        runner.deliver()
        self.assertEqual(panel._current_staged_image()["image_id"], "a" * 64)

    def test_base_exception_submission_before_entry_cancels_then_propagates(self):
        class OwnedAbort(BaseException): pass
        with patch.object(self.runner, "submit", side_effect=OwnedAbort("owned abort")):
            with self.assertRaises(OwnedAbort):
                self.panel._stage_local_image("not-opened", "current")
        self.assertEqual(self.snapshot()["activity_count"], 0)
        self.assertFalse(self.panel._image_preparing)
        self.assertEqual(self.import_calls, 0)

    def test_base_exception_after_entry_cannot_release_running_import_early(self):
        class OwnedAbort(BaseException): pass
        submit = self.runner.submit
        def entered_then_abort(function, callback):
            submit(function, callback)
            self.runner.start()
            self.assertTrue(self.entered.wait(2))
            raise OwnedAbort("owned abort after entry")
        self.release.clear()
        with patch.object(self.runner, "submit", side_effect=entered_then_abort):
            with self.assertRaises(OwnedAbort):
                self.panel._stage_local_image("owned-memory-only", "current")
        self.assertEqual(self.snapshot()["activity_count"], 1)
        self.assertTrue(self.panel._image_preparing)
        self.finish(deliver=False)
        wait_until(lambda: not self.panel._image_preparing, timeout=2)
        self.assertEqual(self.snapshot()["activity_count"], 0)
        self.assertEqual(self.snapshot()["stage_count"], 0)

    def test_constructor_deleted_before_queued_draft_restore_has_no_qt_exception(self):
        exceptions = []
        def capture(kind, value, tb):
            exceptions.append("".join(traceback.format_exception(kind, value, tb)))
        with patch.object(sys, "excepthook", capture):
            candidate = ChatPanel(self.chat)
            candidate.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
        type(self).last_constructor_exceptions = exceptions
        self.assertEqual(exceptions, [], "queued draft restore called a destroyed panel")

    def test_live_hidden_panel_still_receives_queued_draft_restore(self):
        observed = []
        class ObservedPanel(ChatPanel):
            def _drafts_loaded(inner_self):
                observed.append("restored")
                super()._drafts_loaded()
        snapshot = self.panel._draft_owner.snapshot("current")
        self.panel._draft_owner.set_text("current", "Owned hidden draft", epoch=snapshot.epoch)
        candidate = ObservedPanel(self.chat)
        self.panels.append(candidate)
        candidate.hide()
        self.assertEqual(observed, [])
        self.app.processEvents()
        self.assertEqual(observed, ["restored"])
        self.assertEqual(candidate.input.toPlainText(), "Owned hidden draft")

    def test_actual_taskrunner_physical_drain_still_holds_until_queued_loaded(self):
        self.panel._image_stage_runner = TaskRunner(self.panel)
        self.release.clear()
        self.assertTrue(self.panel._stage_local_image("owned-memory-only", "current"))
        self.assertTrue(self.entered.wait(2))
        self.assertEqual(self.snapshot()["activity_count"], 1)
        self.release.set()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(2000))
        # No event processing above: physical finally has ended, Qt loaded is
        # still queued. Success must not expose an unprotected gap here.
        self.assertEqual(self.snapshot()["activity_count"], 1)
        self.assertEqual(self.snapshot()["stage_count"], 0)
        self.assertTrue(self.panel._image_preparing)
        wait_until(lambda: not self.panel._image_preparing, timeout=2)
        self.assertEqual(self.snapshot()["activity_count"], 0)
        self.assertEqual(self.snapshot()["stage_count"], 1)
        self.assertEqual(QThreadPool.globalInstance().activeThreadCount(), 0)

    def test_actual_taskrunner_shutdown_no_callback_still_retires_from_physical_finally(self):
        self.panel._image_stage_runner = TaskRunner(self.panel)
        self.release.clear()
        self.assertTrue(self.panel._stage_local_image("owned-memory-only", "current"))
        self.assertTrue(self.entered.wait(2))
        self.chat.shutdown()
        self.assertTrue(self.panel._image_stage_runner._closed)
        self.assertEqual(self.snapshot()["activity_count"], 1)
        self.release.set()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(2000))
        self.assertEqual(self.snapshot()["activity_count"], 0)
        self.app.processEvents()
        self.assertEqual(self.snapshot()["stage_count"], 0)
        self.assertIsNone(self.panel._current_staged_image())
        self.assertEqual(QThreadPool.globalInstance().activeThreadCount(), 0)


if __name__ == "__main__":
    unittest.main()
