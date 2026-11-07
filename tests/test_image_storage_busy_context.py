"""Count-only contextual failures with an owned gate and hidden native view."""

import threading
import unittest

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from desktop_pet.image_storage import ImageStorageGate
from desktop_pet.ui.image_storage_dialog import ImageStorageDialog, _ERRORS
from tests.test_image_storage_dialog import OwnedManager


STAGES = "还有{count}处待发图片，请在其他对话或调试面板的「聊天测试」中发送或移除后重试。"
ACTIVITY = "仍有操作正在处理或收尾，请稍后重试。"
CLEARED = "待发图片占用已解除，请重新确认。"


class CountChat(QObject):
    image_storage_content_changed = Signal(str)
    image_storage_changed = Signal()

    def __init__(self, gate):
        super().__init__()
        self.image_storage_gate = gate
        self.image_storage_epoch = 0


class ImageStorageBusyContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.gate = ImageStorageGate()
        self.owner = self.gate.create_owner()
        self.manager = OwnedManager()
        self.previous_chat = self.manager.chat
        self.manager.chat = CountChat(self.gate)
        self.manager.chat.image_storage_manager = self.manager
        self.dialog = ImageStorageDialog(self.manager)
        self.dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.threads = []
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.gate.close_owner(self.owner)
        self.dialog.done(0)
        self.dialog.deleteLater()
        self.manager.chat.deleteLater()
        self.previous_chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def stage(self):
        activity = self.gate.activity(self.owner, stage_on_success=True)
        self.gate.run(activity, lambda: None)
        return self.gate.handoff_to_stage(activity)

    def changed(self):
        self.manager.chat.image_storage_changed.emit()

    def fail_busy(self):
        self.dialog._error({"ok": False, "code": "busy"})

    def test_two_one_zero_stages_update_only_the_existing_failure(self):
        first, second = self.stage(), self.stage()
        self.fail_busy()
        self.assertEqual(self.dialog.status.text(), STAGES.format(count=2))
        calls = list(self.manager.calls)
        self.gate.release(first)
        self.changed()
        self.assertEqual(self.dialog.status.text(), STAGES.format(count=1))
        self.gate.release(second)
        self.changed()
        self.assertEqual(self.dialog.status.text(), CLEARED)
        self.assertEqual(self.manager.calls, calls)

    def test_activity_has_priority_and_token_count_is_not_displayed_as_tasks(self):
        stage = self.stage()
        physical = self.gate.activity(self.owner)
        completion = self.gate.activity(self.owner)
        self.fail_busy()
        self.assertEqual(self.dialog.status.text(), ACTIVITY)
        self.assertNotIn("2", self.dialog.status.text())
        self.gate.release(physical)
        self.gate.release(completion)
        self.changed()
        self.assertEqual(self.dialog.status.text(), STAGES.format(count=1))
        self.gate.release(stage)
        self.changed()
        self.assertEqual(self.dialog.status.text(), CLEARED)

    def test_running_owner_close_waits_for_real_finally_before_text_changes(self):
        worker_owner = self.gate.create_owner()
        token = self.gate.activity(worker_owner)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def operation():
            entered.set()
            release.wait(2)
        thread = threading.Thread(target=lambda: self.gate.run(token, operation),
                                  name="aura-owned-busy-context", daemon=True)
        self.threads.append(thread)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.fail_busy()
        self.gate.close_owner(worker_owner)
        self.changed()
        self.assertEqual(self.gate.snapshot()["activity_count"], 1)
        self.assertEqual(self.dialog.status.text(), ACTIVITY)
        release.set()
        thread.join(1)
        self.changed()
        self.assertEqual(self.gate.snapshot()["activity_count"], 0)
        self.assertEqual(self.dialog.status.text(), _ERRORS["busy"])

    def test_zero_gate_counts_preserve_unknown_service_or_queue_message(self):
        self.fail_busy()
        self.assertEqual(self.dialog.status.text(), _ERRORS["busy"])
        self.changed()
        self.assertEqual(self.dialog.status.text(), _ERRORS["busy"])

    def test_changes_do_not_replace_success_or_another_error(self):
        self.dialog.status.setText("Owned ready status")
        self.stage()
        self.changed()
        self.assertEqual(self.dialog.status.text(), "Owned ready status")
        self.dialog._error({"code": "storage"})
        self.changed()
        self.assertEqual(self.dialog.status.text(), _ERRORS["storage"])

    def test_inflight_view_request_keeps_its_progress_status(self):
        self.stage()
        self.fail_busy()
        self.dialog._busy = True
        self.dialog.status.setText("正在确认引用记录…")
        self.stage()
        self.changed()
        self.assertEqual(self.dialog.status.text(), "正在确认引用记录…")
        self.dialog._busy = False

    def test_new_request_resets_a_previous_staged_failure(self):
        stage = self.stage()
        self.fail_busy()
        self.dialog.refresh_page()
        self.gate.release(stage)
        self.changed()
        self.assertEqual(self.dialog.status.text(), "")
        self.fail_busy()
        self.assertEqual(self.dialog.status.text(), _ERRORS["busy"])

    def test_closed_view_ignores_late_count_changes(self):
        self.stage()
        self.fail_busy()
        original = self.dialog.status.text()
        self.dialog.done(0)
        self.stage()
        self.changed()
        self.assertEqual(self.dialog.status.text(), original)

    def test_legacy_missing_gate_and_malformed_counts_keep_fixed_fallback(self):
        self.manager.chat.image_storage_gate = None
        self.fail_busy()
        self.assertEqual(self.dialog.status.text(), _ERRORS["busy"])
        class InvalidSnapshot:
            def snapshot(self):
                return {"stage_count": "PRIVATE_FILE", "activity_count": 0}
        self.manager.chat.image_storage_gate = InvalidSnapshot()
        self.fail_busy()
        self.assertEqual(self.dialog.status.text(), _ERRORS["busy"])


if __name__ == "__main__":
    unittest.main()
