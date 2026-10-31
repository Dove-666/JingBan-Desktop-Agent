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
        self.panel.tabs.setCurrentIndex(1)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_provider_switch_explains_local_privacy_and_retains_remote_values(self):
        self.panel.embedding_base_input.setText("https://embedding.example.test/v1")
        self.panel.embedding_provider_combo.setCurrentIndex(1)
        self.assertTrue(self.panel.remote_embedding_fields.isHidden())
        self.assertFalse(self.panel.local_embedding_fields.isHidden())
        self.assertIn("提炼仍使用", self.panel.local_model_hint.text())
        self.assertIn("512", self.panel.local_model_combo.currentText())
        self.panel.embedding_provider_combo.setCurrentIndex(0)
        self.assertEqual(self.panel.embedding_base_input.text(), "https://embedding.example.test/v1")

    def test_saving_local_choice_never_enables_memory_or_copies_chat_key(self):
        self.panel.embedding_provider_combo.setCurrentIndex(1)
        self.panel._save_settings()
        saved = self.controller.settings
        self.assertEqual(saved.embedding_provider, "fastembed")
        self.assertEqual(saved.effective_embedding_dimensions, 512)
        self.assertFalse(saved.enabled)
        self.assertFalse(saved.automatic)
        self.assertEqual(self.controller.calls[-1][2:], ("", ""))

    def test_model_download_requires_explicit_click_and_controls_are_busy_safe(self):
        self.panel.embedding_provider_combo.setCurrentIndex(1)
        self.assertEqual(self.controller.calls, [])
        self.assertFalse(self.panel.prepare_model_button.isEnabled())
        self.panel._save_settings()
        self.controller.calls.clear()
        self.assertTrue(self.panel.prepare_model_button.isEnabled())
        self.panel.prepare_model_button.click()
        self.assertEqual(self.controller.calls, [("prepare_local_model",)])
        self.controller.busy = True
        self.controller.busy_changed.emit(True)
        self.assertFalse(self.panel.prepare_model_button.isEnabled())
        self.assertFalse(self.panel.embedding_provider_combo.isEnabled())

    def test_unsaved_model_change_cannot_prepare_previously_saved_model(self):
        self.panel.embedding_provider_combo.setCurrentIndex(1)
        self.panel._save_settings()
        self.panel.local_model_combo.setCurrentIndex(1)
        self.assertFalse(self.panel.prepare_model_button.isEnabled())
        self.assertFalse(self.panel.local_preparation_hint.isHidden())
        self.panel.prepare_model_button.click()
        self.assertNotIn(("prepare_local_model",), self.controller.calls)

    def test_reusing_chat_hides_independent_fields_and_does_not_save_hidden_keys(self):
        self.assertTrue(self.panel.llm_key_input.isHidden())
        self.panel.llm_key_input.setText("hidden-unused-test-token")
        self.panel.embedding_key_input.setText("hidden-unused-embedding-token")
        self.panel.embedding_provider_combo.setCurrentIndex(1)
        self.panel._save_settings()
        self.assertEqual(self.controller.calls[-1][2:], ("", ""))

    def test_advanced_parameters_collapse_without_hiding_save(self):
        self.assertTrue(self.panel.advanced_fields.isHidden())
        self.assertFalse(self.panel.save_button.isHidden())
        self.panel.advanced_button.click()
        self.assertFalse(self.panel.advanced_fields.isHidden())
        self.assertEqual(self.panel.advanced_button.text(), "收起高级参数")

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
        self.assertIn("1205", self.panel.memory_count_label.text())
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

    def test_invalid_url_keeps_unsaved_key_and_does_not_submit(self):
        self.panel.embedding_base_input.setText("invalid-url")
        self.panel.embedding_key_input.setText("fixture-unsaved-key")
        self.assertFalse(self.panel._save_settings())
        self.assertEqual(self.panel.embedding_key_input.text(), "fixture-unsaved-key")
        self.assertEqual(self.controller.calls, [])
        self.assertIn("http(s)", self.panel.error_label.text())

    def test_busy_state_disables_fields_that_success_would_replace(self):
        self.controller.busy = True
        self.controller.busy_changed.emit(True)
        for field in (self.panel.embedding_model_input, self.panel.embedding_base_input, self.panel.embedding_key_input,
                      self.panel.enabled_checkbox, self.panel.automatic_checkbox, self.panel.threshold_input):
            self.assertFalse(field.isEnabled(), field.objectName())
        self.controller.busy = False
        self.controller.busy_changed.emit(False)
        self.assertTrue(self.panel.embedding_model_input.isEnabled())
        self.assertTrue(self.panel.enabled_checkbox.isEnabled())

    def test_details_show_provenance_and_emit_source_conversation_without_reactivating_expired_record(self):
        self.panel.tabs.setCurrentIndex(0)
        self.panel.resize(700, 700)
        self.controller.memories = [{"id": "expired", "memory": "用户偏好小字体。", "expiration_date": "2000-01-01",
            "created_at": "2026-01-01", "updated_at": "2026-02-02", "metadata": {
                "conversation_id": "source-conversation", "trigger": "automatic", "effective_at": "2026-02-02"}}]
        self.controller.memories_changed.emit()
        self.panel.memory_list.setCurrentRow(0)
        text = self.panel.memory_details_label.text()
        for expected in ("自动提炼", "source-conversation", "2000-01-01", "2026-01-01", "不会重新启用"):
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
        self.panel.tabs.setCurrentIndex(0)
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
        self.assertIn("缓存待刷新", self.panel.memory_count_label.text())
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
