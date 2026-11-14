"""Local embedding setup stays optional, explicit and responsive."""

from dataclasses import replace
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QRect, QThreadPool, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop_pet.memory.settings import MemorySettings
from desktop_pet.memory_controller import MemoryController
from desktop_pet.ui.memory_panel import MemoryPanel
from tests.test_application import spin_until
from tests.test_memory_controller import FakeMemoryService
from tests.test_session_ui import MemoryControllerFake


class SetupController(MemoryControllerFake):
    def __init__(self):
        super().__init__()
        self.settings = MemorySettings(user_id="00000000-0000-4000-8000-000000000001")

    def prepare_local_model(self):
        self.calls.append(("prepare_local_model",))


class MemorySetupUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.controller = SetupController()
        self.panel = MemoryPanel(self.controller)
        self.panel.resize(700, 650)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_page_exposes_one_automatic_switch_and_no_technical_settings(self):
        from PySide6.QtWidgets import QCheckBox, QComboBox, QTabWidget
        self.assertEqual([widget.text() for widget in self.panel.findChildren(QCheckBox)], ["自动记忆"])
        self.assertEqual(self.panel.findChildren(QComboBox), [])
        self.assertEqual(self.panel.findChildren(QTabWidget), [])
        self.assertTrue(self.panel.memory_details_group.isHidden())
        self.assertTrue(self.panel.status_label.isHidden())
        self.assertEqual(self.controller.calls, [])

    def test_empty_failed_store_has_explicit_reset_but_fresh_empty_store_stays_simple(self):
        self.controller.memories = []
        self.controller.memory_total = 0
        self.controller.can_reset_memories = False
        self.controller.memories_changed.emit()
        self.assertTrue(self.panel.clear_button.isHidden())
        self.controller.can_reset_memories = True
        self.controller.settings_changed.emit(self.controller.settings)
        self.assertFalse(self.panel.clear_button.isHidden())
        self.assertTrue(self.panel.clear_button.isEnabled())
        self.assertEqual(self.panel.clear_button.text(), "重置记忆库")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirm:
            self.panel.clear_button.click()
        self.assertIn("模型绑定", confirm.call_args.args[2])
        self.assertEqual(self.controller.calls, [])
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel.clear_button.click()
        self.assertIn(("clear",), self.controller.calls)

    def test_rejected_toggle_and_async_failure_restore_persisted_choice(self):
        self.controller.set_automatic_enabled = lambda enabled: False
        self.panel.automatic_checkbox.click()
        self.assertFalse(self.panel.automatic_checkbox.isChecked())
        self.assertIn("无法保存", self.panel.error_label.text())
        self.controller.set_automatic_enabled = lambda enabled: True
        self.panel.automatic_checkbox.click()
        self.assertTrue(self.panel.automatic_checkbox.isChecked())
        self.controller.error_changed.emit("模型下载失败")
        self.assertFalse(self.panel.automatic_checkbox.isChecked())
        self.assertIn("聊天仍可正常使用", self.panel.error_label.text())
        self.assertFalse(self.panel.retry_button.isHidden())

    def test_switch_can_stop_active_automatic_work_while_other_controls_are_busy(self):
        self.controller.settings = replace(self.controller.settings, enabled=True, automatic=True)
        self.controller.settings_changed.emit(self.controller.settings)
        self.controller.busy = True
        self.controller.busy_changed.emit(True)
        self.assertTrue(self.panel.automatic_checkbox.isEnabled())
        self.assertFalse(self.panel.refresh_button.isEnabled())
        self.panel.automatic_checkbox.click()
        self.assertFalse(self.controller.settings.automatic)
        self.assertTrue(self.controller.settings.enabled)
        self.assertEqual(self.controller.calls, [("automatic", False)])

    def test_preparation_intent_keeps_switch_available_until_cancelled(self):
        self.controller.setup_state = "preparing"
        self.controller.automatic_requested = True
        self.controller.automatic_paused = True
        self.controller.busy = True
        self.panel._setup_state_changed()
        self.assertTrue(self.panel.automatic_checkbox.isChecked())
        self.assertTrue(self.panel.automatic_checkbox.isEnabled())
        self.controller.automatic_requested = False
        self.controller.automatic_pause_changed.emit(True)
        self.assertFalse(self.panel.automatic_checkbox.isChecked())

    def test_failed_off_save_preserves_actionable_warning_and_retries_off(self):
        self.controller.automatic_requested = False
        self.controller.error = "本次运行已停止自动记忆，但关闭设置未能保存；重启后可能恢复，请重试。"
        self.controller.error_changed.emit(self.controller.error)
        self.assertIn("重启后可能恢复", self.panel.error_label.text())
        self.assertEqual(self.panel.retry_button.text(), "重试关闭")
        self.panel.retry_button.click()
        self.assertEqual(self.controller.calls, [("automatic", False)])

    def test_list_refresh_does_not_hide_failed_setup_recovery(self):
        self.controller.setup_error = "记忆暂时无法准备，请稍后重试；聊天仍可正常使用。"
        self.controller.setup_state = "failed"
        self.controller.error_changed.emit(self.controller.setup_error)
        self.controller.error = ""
        self.controller.error_changed.emit("")
        self.assertFalse(self.panel.error_label.isHidden())
        self.assertIn("暂时无法准备", self.panel.error_label.text())
        self.assertFalse(self.panel.retry_button.isHidden())
        self.panel.retry_button.click()
        self.assertEqual(self.controller.calls, [("prepare",)])
        self.controller.setup_error = ""
        self.controller.setup_state = "ready"
        self.panel._setup_state_changed()
        self.assertTrue(self.panel.error_label.isHidden())
        self.assertTrue(self.panel.retry_button.isHidden())

    def test_list_shows_fact_only_and_details_are_only_visible_after_selection(self):
        self.controller.memories = [{"id": "preference", "memory": "用户喜欢简洁的页面。", "updated_at": "2026-11-01"}]
        self.controller.memories_changed.emit()
        self.assertEqual(self.panel.memory_list.item(0).text(), "用户喜欢简洁的页面。")
        self.assertTrue(self.panel.memory_details_group.isHidden())
        self.panel.memory_list.setCurrentRow(0)
        self.assertFalse(self.panel.memory_details_group.isHidden())
        self.assertTrue(self.panel.edit_button.isEnabled())
        self.assertTrue(self.panel.delete_button.isEnabled())

    def test_refresh_preserves_selection_and_editor_receives_fact_without_display_time(self):
        old = "用户偏好大字体评论气泡。"
        self.controller.memories = [{"id": "chosen", "memory": old, "updated_at": "2026-10-31T00:00:00Z"},
                                    {"id": "other", "memory": "用户偏好简短解释。"}]
        self.controller.memory_query = "偏好"
        self.controller.memory_total = 1205
        self.controller.memory_matched_total = 205
        self.controller.memory_offset = 100
        self.controller.memory_has_more = True
        self.panel.search_input.setText("偏好")
        self.controller.memories_changed.emit()
        self.panel.memory_list.setCurrentRow(0)
        self.controller.memories = [self.controller.memories[1], self.controller.memories[0]]
        self.controller.memories_changed.emit()
        selected = self.panel.memory_list.currentItem()
        self.assertEqual(selected.data(Qt.ItemDataRole.UserRole), "chosen")
        self.assertEqual(self.panel.search_input.text(), "偏好")
        self.assertNotIn("1205", self.panel.memory_count_label.text())
        self.assertIn("205", self.panel.memory_count_label.text())
        self.assertTrue(self.panel.next_button.isEnabled())
        self.assertTrue(self.panel.previous_button.isEnabled())
        self.controller.update_memory = lambda *args, **kwargs: self.controller.calls.append((args, kwargs))
        new = "用户现在偏好小字体评论气泡。"
        self.panel.edit_button.click()
        dialog = self.panel._edit_dialog
        self.assertEqual(dialog.text_input.toPlainText(), old)
        dialog.text_input.setPlainText(new)
        dialog.save_button.click()
        self.assertEqual(self.controller.calls[-1], (("chosen", new), {"expected_text": old}))
        self.panel._memory_update_finished("chosen", False, "暂时无法保存")
        self.assertIs(self.panel._edit_dialog, dialog)
        self.assertEqual(dialog.text_input.toPlainText(), new)
        self.assertIn("输入已保留", dialog.error_label.text())
        self.panel._memory_update_finished("chosen", True, "")
        self.assertIsNone(self.panel._edit_dialog)

    def test_details_show_provenance_and_emit_source_conversation_without_reactivating_expired_record(self):
        self.panel.resize(700, 700)
        self.controller.memories = [{"id": "expired", "memory": "用户偏好小字体。", "expiration_date": "2000-01-01",
            "created_at": "2026-01-01", "updated_at": "2026-02-02", "metadata": {
                "conversation_id": "source-conversation", "trigger": "automatic", "effective_at": "2026-02-02"}}]
        self.controller.memories_changed.emit()
        self.panel.memory_list.setCurrentRow(0)
        text = self.panel.memory_details_label.text()
        for expected in ("自动提炼", "2000-01-01", "2026-01-01", "不会重新启用"):
            self.assertIn(expected, text)
        self.app.processEvents()
        self.assertGreaterEqual(self.panel.memory_details_label.height(), self.panel.memory_details_label.fontMetrics().lineSpacing() * 5)
        self.assertLess(self.panel.memory_details_label.geometry().bottom(), self.panel.source_button.geometry().top())
        self.assertTrue(self.panel.memory_details_group.contentsRect().contains(self.panel.source_button.geometry()))
        requested = QSignalSpy(self.panel.conversation_requested)
        self.panel.source_button.click()
        self.assertEqual(requested.at(0), ["source-conversation"])
        self.panel._edit_memory()
        dialog = self.panel._edit_dialog
        labels = "\n".join(label.text() for label in dialog.findChildren(type(self.panel.memory_details_label)))
        self.assertIn("保留原有效期", labels)
        dialog.reject()

    def test_small_work_area_scrolls_memory_controls_without_squashing_buttons(self):
        from desktop_pet.ui.window_state import WindowPlacement, bounded_rect
        import tempfile
        from pathlib import Path
        self.controller.memories = [{"id": "selected", "memory": "用户偏好简洁界面。", "created_at": "2026-11-01", "updated_at": "2026-11-02", "metadata": {"conversation_id": "source", "trigger": "automatic"}}]
        self.controller.memories_changed.emit()
        self.panel.memory_list.setCurrentRow(0)
        with tempfile.TemporaryDirectory() as directory, patch("desktop_pet.ui.window_state.bounded_rect", side_effect=lambda rect, _areas: bounded_rect(rect, [QRect(0, 0, 640, 400)])):
            placement = WindowPlacement(self.panel, Path(directory) / "window.ini", "memory")
            placement.fit()
            self.app.processEvents()
            self.assertLessEqual(self.panel.width(), 640)
            self.assertLessEqual(self.panel.height(), 400)
            for button in (self.panel.refresh_button, self.panel.previous_button, self.panel.next_button,
                           self.panel.edit_button, self.panel.history_button, self.panel.delete_button, self.panel.clear_button):
                self.assertGreaterEqual(button.height(), 32, button.objectName())
            self.assertGreater(self.panel.memories_scroll.verticalScrollBar().maximum(), 0)
            self.assertGreaterEqual(self.panel.memory_list.height(), 160)

    def test_delete_focus_hint_selects_neighbor_but_does_not_override_surviving_user_selection(self):
        self.controller.memories = [{"id": str(index), "memory": "偏好"} for index in range(3)]
        self.controller.memories_changed.emit()
        self.panel.memory_list.setCurrentRow(1)
        self.controller.memories = [self.controller.memories[0], self.controller.memories[2]]
        self.controller.memory_preferred_id = "2"
        self.controller.memories_changed.emit()
        self.assertEqual(self.panel.memory_list.currentItem().data(Qt.ItemDataRole.UserRole), "2")
        self.assertTrue(self.panel.delete_button.isEnabled())
        self.panel.memory_list.setCurrentRow(0)
        self.controller.memories_changed.emit()
        self.assertEqual(self.panel.memory_list.currentItem().data(Qt.ItemDataRole.UserRole), "0")

    def test_stale_empty_page_reports_confirmed_delete_and_pending_refresh(self):
        self.controller.memory_list_stale = True
        self.controller.memory_query = "偏好"
        self.controller.memory_total = 100
        self.controller.memory_matched_total = 100
        self.controller.memories = []
        self.controller.memories_changed.emit()
        self.assertIn("待刷新", self.panel.memory_count_label.text())
        self.assertIn("删除已完成", self.panel.empty_label.text())
        self.assertIn("刷新", self.panel.empty_label.text())

    def test_delete_confirmation_refresh_uses_original_id_after_native_item_is_destroyed(self):
        self.controller.memories = [{"id": "original", "memory": "原本要删除的记忆"},
                                    {"id": "different", "memory": "另一个记忆"}]
        self.controller.memories_changed.emit()
        self.panel.memory_list.setCurrentRow(0)
        old_item = self.panel.memory_list.currentItem()
        def refresh_during_confirmation(*_args, **_kwargs):
            self.controller.memories = [{"id": "different", "memory": "刷新后选中的另一个记忆"},
                                        {"id": "original", "memory": "原本要删除的记忆"}]
            self.controller.memories_changed.emit()
            self.panel.memory_list.setCurrentRow(0)
            with self.assertRaises(RuntimeError):
                old_item.data(Qt.ItemDataRole.UserRole)
            self.assertEqual(self.panel.memory_list.currentItem().data(Qt.ItemDataRole.UserRole), "different")
            return QMessageBox.StandardButton.Yes
        with patch("desktop_pet.ui.memory_panel.QMessageBox.question", side_effect=refresh_during_confirmation):
            self.panel._delete_memory()
        self.assertEqual(self.controller.calls[-1], ("delete", "original"))


class ModelPreparationControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_preparation_runs_in_worker_without_enabling_memory(self):
        service = FakeMemoryService()
        service.settings = replace(service.settings, embedding_provider="fastembed")
        worker_threads = []
        def prepare():
            worker_threads.append(threading.get_ident())
            return {"model": "BAAI/bge-small-zh-v1.5", "dimensions": 512, "ready": True}
        service.prepare_local_model = prepare
        controller = MemoryController(service)
        spin_until(lambda: not controller.busy)
        try:
            self.assertTrue(controller.prepare_local_model())
            spin_until(lambda: not controller.busy)
            self.assertEqual(len(worker_threads), 1)
            self.assertNotEqual(worker_threads[0], threading.get_ident())
            self.assertIn("已就绪", controller.status)
            self.assertFalse(controller.settings.enabled)
            self.assertFalse(controller.settings.automatic)
        finally:
            controller.shutdown()
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            controller.deleteLater()


if __name__ == "__main__":
    unittest.main()
