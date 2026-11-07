"""Native view lifecycle with owned metadata and callback-controlled manager."""
import unittest
from types import SimpleNamespace

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.ui.image_storage_dialog import ImageStorageDialog, ImageStorageUsageRow
from desktop_pet.ui.settings_page import SettingsPage


class OwnedChat(QObject):
    image_storage_content_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.image_storage_epoch = 0


class OwnedManager:
    def __init__(self):
        self.chat = OwnedChat()
        self.chat.image_storage_manager = self
        self.calls = []
        self.pending = []
        self.hold = set()
        self.return_value = True
        self.discarded = []
        self.shutdown_calls = 0
        self.rows = [{"id": f"{index:064x}", "filename": ("长文件名鲸鱼娘🌊" * 25 if index == 0 else f"图片{index}") + ".png",
                      "size": 4096, "width": 640, "height": 430, "created_at": "2026-11-06T10:00:00Z"}
                     for index in range(21)]

    def result(self, method, argument):
        if method in ("load_page", "load_usage"):
            result = {"ok": True, "usage": {"count": len(self.rows), "used_bytes": len(self.rows) * 4096,
                                            "limit_bytes": 64 * 1048576}}
            if method == "load_page":
                result.update(items=self.rows[argument:argument + 20], has_more=argument + 20 < len(self.rows),
                              next_offset=argument + 20 if argument + 20 < len(self.rows) else None)
            return result
        if method == "review_delete":
            return {"ok": True, "image": next(row for row in self.rows if row["id"] == argument),
                    "references": {"message_count": 3, "conversation_count": 2}, "confirmation_id": "owned-ticket"}
        return {"ok": True, "id": self.rows[0]["id"]}

    def _call(self, method, argument, callback):
        self.calls.append((method, argument))
        result = self.result(method, argument)
        if method in self.hold:
            self.pending.append((method, callback, result))
        else:
            callback(result, "")
        return self.return_value

    def load_page(self, offset, callback):
        return self._call("load_page", offset, callback)

    def load_usage(self, callback):
        return self._call("load_usage", None, callback)

    def review_delete(self, image_id, callback):
        return self._call("review_delete", image_id, callback)

    def delete(self, confirmation_id, callback):
        return self._call("delete", confirmation_id, callback)

    def discard_confirmation(self, confirmation_id):
        self.discarded.append(confirmation_id)

    def shutdown(self):
        self.shutdown_calls += 1


class ImageStorageDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.manager = OwnedManager()
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            if isinstance(widget, ImageStorageDialog):
                widget.done(0)
            else:
                widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def dialog(self):
        dialog = ImageStorageDialog(self.manager)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.widgets.append(dialog)
        return dialog

    def select(self, dialog):
        dialog.files.setCurrentRow(0)
        self.assertTrue(dialog.delete_button.isEnabled())

    def test_metadata_pagination_full_accessible_name_and_no_decode(self):
        dialog = self.dialog()
        self.assertEqual(dialog.files.count(), 20)
        self.assertEqual(dialog.files.item(0).toolTip(), self.manager.rows[0]["filename"])
        self.assertIn(self.manager.rows[0]["filename"], dialog.files.item(0).data(Qt.ItemDataRole.AccessibleTextRole))
        self.select(dialog)
        dialog.next.click()
        self.assertEqual(dialog.files.count(), 1)
        self.assertIsNone(dialog.selected())
        self.assertFalse(dialog.next.isEnabled())
        self.assertTrue(dialog.previous.isEnabled())
        self.assertEqual(self.manager.calls, [("load_page", 0), ("load_page", 20)])

    def test_confirmation_cancel_enter_never_dispatches_delete(self):
        dialog = self.dialog()
        self.select(dialog)
        dialog.review_selected()
        self.assertIs(dialog.stack.currentWidget(), dialog.confirm_page)
        self.assertEqual(dialog.references.text(), "已知 3 条消息，分布在 2 个会话。")
        self.assertTrue(dialog.cancel_button.isDefault())
        self.assertFalse(dialog.confirm_button.autoDefault())
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        self.assertIs(dialog.stack.currentWidget(), dialog.list_page)
        self.assertEqual(self.manager.discarded, ["owned-ticket"])
        self.assertEqual(dialog.status.text(), "")
        self.assertFalse(any(method == "delete" for method, _ in self.manager.calls))

    def test_false_return_waits_callback_and_delete_failure_does_not_retry(self):
        dialog = self.dialog()
        self.select(dialog)
        dialog.review_selected()
        self.manager.hold.add("delete")
        self.manager.return_value = False
        dialog.confirm_delete()
        dialog.confirm_delete()
        self.assertTrue(dialog._busy)
        self.assertFalse(dialog.confirm_button.isEnabled())
        self.assertEqual(sum(method == "delete" for method, _ in self.manager.calls), 1)
        _, callback, _ = self.manager.pending.pop()
        callback({"ok": False, "code": "delete", "message": "PRIVATE_PATH_CANARY"}, "private raw error")
        self.assertFalse(dialog._busy)
        self.assertIn("删除结果未能确认", dialog.status.text())
        self.assertNotIn("PRIVATE", dialog.status.text())
        self.assertTrue(dialog.refresh.isEnabled())
        callback({"ok": True}, "")
        self.assertEqual(sum(method == "delete" for method, _ in self.manager.calls), 1)
        self.assertIn("删除结果未能确认", dialog.status.text())

    def test_close_then_late_review_discards_ticket_without_shutdown(self):
        dialog = self.dialog()
        self.select(dialog)
        self.manager.hold.add("review_delete")
        dialog.review_selected()
        dialog.done(0)
        _, callback, result = self.manager.pending.pop()
        callback(result, "")
        self.assertEqual(self.manager.discarded, ["owned-ticket"])
        self.assertEqual(self.manager.shutdown_calls, 0)
        self.assertFalse(any(method == "delete" for method, _ in self.manager.calls))

    def test_window_close_from_confirmation_retires_view_without_host_shutdown(self):
        dialog = self.dialog()
        self.select(dialog)
        dialog.review_selected()
        dialog.show()
        dialog.close()
        self.assertFalse(dialog.isVisible())
        self.assertTrue(dialog._closed)
        self.assertEqual(self.manager.discarded, ["owned-ticket"])
        self.assertEqual(self.manager.shutdown_calls, 0)

    def test_stale_epoch_load_requeries_once_and_drops_old_page(self):
        self.manager.hold.add("load_page")
        dialog = self.dialog()
        _, old, old_result = self.manager.pending.pop()
        self.manager.chat.image_storage_epoch += 1
        self.manager.rows.clear()
        old(old_result, "")
        self.assertEqual(len(self.manager.pending), 1)
        self.assertEqual(dialog.files.count(), 0)
        _, fresh, fresh_result = self.manager.pending.pop()
        fresh(fresh_result, "")
        old(old_result, "")
        self.assertEqual(dialog.files.count(), 0)
        self.assertEqual(dialog.status.text(), "暂无图片")
        self.assertEqual(len(self.manager.calls), 2)

    def test_epoch_change_revokes_visible_confirmation_and_refreshes(self):
        dialog = self.dialog()
        self.select(dialog)
        dialog.review_selected()
        self.manager.chat.image_storage_epoch += 1
        self.manager.chat.image_storage_content_changed.emit(self.manager.rows[0]["id"])
        self.assertIs(dialog.stack.currentWidget(), dialog.list_page)
        self.assertIsNone(dialog._confirmation_id)
        self.assertEqual(self.manager.discarded, ["owned-ticket"])

    def test_success_delete_refreshes_current_page_once(self):
        dialog = self.dialog()
        self.select(dialog)
        dialog.review_selected()
        dialog.confirm_delete()
        self.assertEqual(self.manager.calls[-2:], [("delete", "owned-ticket"), ("load_page", 0)])
        self.assertIs(dialog.stack.currentWidget(), dialog.list_page)
        self.assertFalse(dialog._busy)

    def test_error_is_fixed_and_keyboard_refresh_is_reusable(self):
        self.manager.hold.add("load_page")
        dialog = self.dialog()
        _, callback, _ = self.manager.pending.pop()
        callback({"ok": False, "code": "unknown", "message": "PRIVATE_PATH_CANARY"}, "")
        self.assertEqual(dialog.status.text(), "无法读取图片存储，请检查本地存储后重试。")
        self.assertTrue(dialog.refresh.isEnabled())
        self.manager.hold.clear()
        dialog.refresh.click()
        self.assertEqual(dialog.files.count(), 20)

    def test_usage_hidden_dirty_singleflight_only_usage_query(self):
        opened = []
        row = ImageStorageUsageRow(self.manager.chat, opened.append)
        self.widgets.append(row)
        row.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.assertEqual(self.manager.calls, [])
        self.manager.hold.add("load_usage")
        row.show()
        row._refresh()
        self.assertEqual(self.manager.calls, [("load_usage", None)])
        row.hide()
        self.manager.chat.image_storage_epoch += 1
        self.manager.chat.image_storage_content_changed.emit("owned")
        _, callback, result = self.manager.pending.pop()
        callback(result, "")
        self.assertEqual(len(self.manager.calls), 1)
        self.manager.hold.clear()
        row.show()
        self.assertEqual(self.manager.calls, [("load_usage", None), ("load_usage", None)])
        self.assertIn("64 MiB", row.usage.text())
        row.manage.click()
        self.assertEqual(opened, [row.window()])

    def test_legacy_settings_without_manager_stays_usable(self):
        page = SettingsPage(None, None, None)
        self.widgets.append(page)
        page.show_section("general")
        self.assertFalse(page.image_storage_row.manage.isEnabled())
        self.assertIs(page.image_storage_row.parent(), page._pages["general"].widget())

    def test_application_opener_uses_one_dialog_and_does_not_create_chat_window(self):
        host = SimpleNamespace(_shutting_down=False, _image_storage_dialog=None, chat=self.manager.chat,
                               chat_window=None)
        from unittest.mock import patch
        dialog = self.dialog()
        with patch("desktop_pet.ui.image_storage_dialog.ImageStorageDialog", return_value=dialog) as constructor:
            ApplicationController.open_image_storage(host)
            ApplicationController.open_image_storage(host)
        self.assertEqual(constructor.call_count, 1)
        self.assertIs(host._image_storage_dialog, dialog)
        dialog.done(0)
        self.assertIsNone(host._image_storage_dialog)
        self.assertIsNone(host.chat_window)
