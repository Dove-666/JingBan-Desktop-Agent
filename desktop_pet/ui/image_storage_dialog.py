"""Native metadata-only image management with host-owned asynchronous work."""
from __future__ import annotations

from datetime import datetime
import weakref

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QProgressBar, QPushButton, QStackedWidget, QStyledItemDelegate, QStyle,
    QStyleOptionViewItem, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from .theme import color, style_widget


_ERRORS = {
    "busy": "图片或任务仍在使用，或退出状态尚未确认，请稍后重试。",
    "changed": "图片或引用记录已变化，请刷新后重新确认。",
    "closed": "应用正在关闭，本次操作未继续。",
    "delete": "删除结果未能确认，请刷新图片列表后查看。",
    "storage": "无法读取图片存储，请检查本地存储后重试。",
    "unavailable": "图片存储暂时不可用，请稍后重试。",
}


def _size_text(size):
    if size < 1024:
        return f"{size} B"
    divisor, unit = (1024, "KiB") if size < 1048576 else (1048576, "MiB")
    amount = f"{size / divisor:.1f}".rstrip("0").rstrip(".")
    return f"{amount} {unit}"


def _usage_text(usage):
    used, limit = usage["used_bytes"], usage["limit_bytes"]
    return f"{_size_text(used)} / {_size_text(limit)}"


def _button(text, name, callback):
    button = QPushButton(text)
    button.setObjectName(name)
    button.setAccessibleName(text)
    button.setMinimumHeight(36)
    button.setAutoDefault(False)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.clicked.connect(callback)
    return button


class _MetadataDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(140, max(66, option.fontMetrics.height() * 2 + 22))

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = opt.widget
        widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        row = index.data(Qt.ItemDataRole.UserRole)
        if not row:
            return
        rect = option.rect.adjusted(12, 8, -12, -8)
        line_height = rect.height() // 2
        painter.save()
        painter.setFont(option.font)
        painter.setPen(QColor(color("text")))
        title = painter.fontMetrics().elidedText(row["filename"], Qt.TextElideMode.ElideMiddle, rect.width())
        painter.drawText(rect.adjusted(0, 0, 0, -line_height), Qt.AlignmentFlag.AlignVCenter, title)
        painter.setPen(QColor(color("muted")))
        detail = painter.fontMetrics().elidedText(row["detail"], Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect.adjusted(0, line_height, 0, 0), Qt.AlignmentFlag.AlignVCenter, detail)
        painter.restore()


class ImageStorageDialog(QDialog):
    """A disappearing view never cancels or retries an admitted deletion."""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self._generation = 0
        self._busy = False
        self._last_error_code = ""
        self._busy_had_staged_images = False
        self._closed = False
        self._offset = 0
        self._next_offset = None
        self._confirmation_id = None
        self._epoch = manager.chat.image_storage_epoch
        self._dirty = False
        self.setWindowTitle("图片存储")
        self.setObjectName("imageStorageDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumSize(360, 430)
        self.resize(560, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        title = QLabel("图片存储")
        title.setObjectName("imageStorageHeading")
        layout.addWidget(title)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.list_page = self._make_list_page()
        self.confirm_page = self._make_confirm_page()
        self.stack.addWidget(self.list_page)
        self.stack.addWidget(self.confirm_page)
        style_widget(self, """
            QDialog#imageStorageDialog { background:#ffffff; color:#344054; }
            QLabel { background:transparent; color:#344054; }
            QLabel#imageStorageHeading { font-size:21px; font-weight:600; }
            QLabel#imageStorageMuted { color:#667386; }
            QFrame#imageStorageConfirmation { background:#f8fafc; border:1px solid #e6eaf0; border-radius:10px; }
            QPushButton { background:#f8fafc; border:1px solid #e1e6ed; border-radius:7px; padding:6px 12px; color:#43556a; }
            QPushButton:hover { background:#edf2f8; }
            QPushButton:focus { border:2px solid #87a8cb; padding:5px 11px; }
            QPushButton:disabled { color:#8c98a7; }
            QPushButton#imageStorageDelete, QPushButton#imageStorageConfirm { color:#a12622; }
            QPushButton#imageStorageDelete:disabled, QPushButton#imageStorageConfirm:disabled { color:#8c98a7; }
            QListWidget { background:#ffffff; border:1px solid #e6eaf0; border-radius:10px; padding:4px; outline:0; }
            QListWidget::item:selected { background:#e6edf5; }
            QListWidget:focus { border-color:#87a8cb; }
            QProgressBar { background:#e8edf3; border:0; border-radius:2px; }
            QProgressBar::chunk { background:#7fa3c8; border-radius:2px; }
        """)
        manager.chat.image_storage_content_changed.connect(self._host_changed)
        activity_signal = getattr(manager.chat, "image_storage_changed", None)
        if activity_signal is not None:
            activity_signal.connect(self._storage_activity_changed)
        self.load_page(0)

    def _make_list_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        row = QHBoxLayout()
        self.usage = QLabel("正在读取…")
        self.usage.setObjectName("imageStorageMuted")
        self.usage.setAccessibleName("图片存储用量")
        row.addWidget(self.usage, 1)
        self.refresh = _button("刷新", "imageStorageRefresh", self.refresh_page)
        row.addWidget(self.refresh)
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        layout.addWidget(self.progress)
        self.files = QListWidget()
        self.files.setAccessibleName("图片文件列表，单选，方向键选择")
        self.files.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.files.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.files.setItemDelegate(_MetadataDelegate(self.files))
        self.files.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.files, 1)
        pager = QHBoxLayout()
        self.previous = _button("上一页", "imageStoragePrevious", lambda: self.load_page(max(0, self._offset - 20)))
        self.next = _button("下一页", "imageStorageNext", lambda: self.load_page(self._next_offset))
        self.page_label = QLabel()
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pager.addWidget(self.previous)
        pager.addWidget(self.page_label, 1)
        pager.addWidget(self.next)
        layout.addLayout(pager)
        self.status = QLabel()
        self.status.setObjectName("imageStorageMuted")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("图片管理状态")
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.delete_button = _button("删除所选", "imageStorageDelete", self.review_selected)
        actions.addWidget(self.delete_button)
        actions.addStretch()
        self.close_button = _button("关闭", "imageStorageClose", self.reject)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        return page

    def _make_confirm_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel("删除这张图片？")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        layout.addStretch()
        card = QFrame()
        card.setObjectName("imageStorageConfirmation")
        body = QVBoxLayout(card)
        body.setContentsMargins(16, 16, 16, 16)
        body.setSpacing(14)
        self.confirm_name = QLabel()
        self.confirm_name.setTextFormat(Qt.TextFormat.PlainText)
        self.confirm_name.setWordWrap(True)
        body.addWidget(self.confirm_name)
        self.references = QLabel()
        self.references.setWordWrap(True)
        body.addWidget(self.references)
        consequence = QLabel("删除此电脑的图片副本。旧聊天可能无法查看或再次识图，文字保留。手机待发可能需重新附图，已缓存图片可能仍保留。")
        consequence.setWordWrap(True)
        body.addWidget(consequence)
        layout.addWidget(card)
        layout.addStretch()
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = _button("取消", "imageStorageCancel", self.cancel_confirmation)
        self.confirm_button = _button("删除图片", "imageStorageConfirm", self.confirm_delete)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)
        return page

    def _callback(self, generation, method, *, epoch=None):
        reference, manager = weakref.ref(self), self.manager

        def finished(result, error):
            widget = reference()
            if (widget is None or not isValid(widget) or widget._closed
                    or widget._generation != generation):
                if isinstance(result, dict) and result.get("confirmation_id"):
                    manager.discard_confirmation(result["confirmation_id"])
                return
            widget._generation += 1  # A repeated callback cannot deliver twice.
            widget._busy = False
            if epoch is not None and manager.chat.image_storage_epoch != epoch:
                if isinstance(result, dict) and result.get("confirmation_id"):
                    manager.discard_confirmation(result["confirmation_id"])
                widget.load_page(widget._offset)
                return
            getattr(widget, method)(result if isinstance(result, dict) else {}, error)
        return finished

    def _request(self, method, *args, receive, epoch=None):
        self._generation += 1
        self._busy = True
        self._last_error_code = ""
        self._busy_had_staged_images = False
        self._update_buttons()
        callback = self._callback(self._generation, receive, epoch=epoch)
        try:
            # The callback owns completion, including accepted work whose submit
            # returned False. A return value never triggers a delete retry.
            getattr(self.manager, method)(*args, callback)
        except Exception:
            callback({"ok": False, "code": "storage"}, "")

    def selected(self):
        items = self.files.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if len(items) == 1 else None

    def _update_buttons(self):
        self.refresh.setEnabled(not self._busy)
        self.previous.setEnabled(not self._busy and self._offset > 0)
        self.next.setEnabled(not self._busy and self._next_offset is not None)
        self.delete_button.setEnabled(not self._busy and self.selected() is not None)
        self.confirm_button.setEnabled(not self._busy and self._confirmation_id is not None)
        self.cancel_button.setEnabled(not self._busy)

    def refresh_page(self):
        self.load_page(self._offset)

    def load_page(self, offset):
        if self._closed or self._busy or offset is None:
            return
        self._offset = offset
        self._dirty = False
        self._epoch = self.manager.chat.image_storage_epoch
        self.status.setText("正在读取…")
        self._request("load_page", offset, receive="_loaded", epoch=self._epoch)

    def _error(self, result):
        self._last_error_code = result.get("code", "")
        self._busy_had_staged_images = False
        self.status.setText(self._busy_context_text() if self._last_error_code == "busy"
                            else _ERRORS.get(self._last_error_code, _ERRORS["storage"]))
        self._update_buttons()

    def _busy_context_text(self):
        try:
            snapshot = self.manager.chat.image_storage_gate.snapshot()
            stages, activities = snapshot["stage_count"], snapshot["activity_count"]
        except Exception:
            return _ERRORS["busy"]
        if (type(stages) is not int or stages < 0
                or type(activities) is not int or activities < 0):
            return _ERRORS["busy"]
        self._busy_had_staged_images |= stages > 0
        if activities:
            return "仍有操作正在处理或收尾，请稍后重试。"
        if stages:
            return f"还有{stages}处待发图片，请在其他对话或调试面板的「聊天测试」中发送或移除后重试。"
        if self._busy_had_staged_images:
            return "待发图片占用已解除，请重新确认。"
        return _ERRORS["busy"]

    def _storage_activity_changed(self):
        if not self._closed and not self._busy and self._last_error_code == "busy":
            self.status.setText(self._busy_context_text())

    def _loaded(self, result, error):
        if not result.get("ok") or error:
            self._error(result)
            return
        usage = result["usage"]
        if not result["items"] and self._offset > 0:
            count = usage["count"]
            if type(count) is int and count >= 0:
                last_offset = max(0, (count - 1) // 20 * 20)
                if last_offset < self._offset:
                    # Deleting the last row on a page must not leave an empty
                    # page implying the whole image library is empty. Each
                    # recovery moves backward, so changing totals cannot loop.
                    self.load_page(last_offset)
                    return
        self.usage.setText(_usage_text(usage))
        self.progress.setValue(min(1000, usage["used_bytes"] * 1000 // max(1, usage["limit_bytes"])))
        self.progress.setAccessibleName("图片存储用量，" + self.usage.text())
        previous = self.selected()
        selected_id = previous["id"] if previous else None
        self.files.clear()
        for metadata in result["items"]:
            row = {key: metadata[key] for key in ("id", "filename", "size", "width", "height", "created_at")}
            try:
                date = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).astimezone().strftime("%m月%d日 %H:%M")
            except (ValueError, TypeError):
                date = "日期未知"
            row["detail"] = f"{_size_text(row['size'])} · {row['width']} × {row['height']} · {date}"
            item = QListWidgetItem(row["filename"])
            item.setData(Qt.ItemDataRole.UserRole, row)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, row["filename"] + "，" + row["detail"])
            item.setToolTip(row["filename"])
            self.files.addItem(item)
            if row["id"] == selected_id:
                self.files.setCurrentItem(item)
        self._next_offset = result["next_offset"] if result["has_more"] else None
        self.page_label.setText(f"第 {self._offset // 20 + 1} 页 · {usage['count']} 张")
        self.status.setText("" if self.files.count() else "暂无图片")
        self._update_buttons()

    def review_selected(self):
        selected = self.selected()
        if self._busy or selected is None:
            return
        self.status.setText("正在确认引用记录…")
        self._request("review_delete", selected["id"], receive="_reviewed", epoch=self.manager.chat.image_storage_epoch)

    def _reviewed(self, result, error):
        if not result.get("ok") or error:
            self._error(result)
            return
        self._confirmation_id = result["confirmation_id"]
        name = result["image"]["filename"]
        # The full filename remains available without letting an unusually long
        # name consume the whole confirmation at a narrow window size.
        self.confirm_name.setText(self.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, 620))
        self.confirm_name.setToolTip(name)
        self.confirm_name.setAccessibleName(name)
        counts = result["references"]
        self.references.setText(f"已知 {counts['message_count']} 条消息，分布在 {counts['conversation_count']} 个会话。")
        self.status.clear()
        self.stack.setCurrentWidget(self.confirm_page)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        self._update_buttons()

    def _discard_confirmation(self):
        if self._confirmation_id is not None:
            self.manager.discard_confirmation(self._confirmation_id)
            self._confirmation_id = None

    def cancel_confirmation(self):
        if self._busy:
            return
        self._discard_confirmation()
        self.cancel_button.setDefault(False)
        self.stack.setCurrentWidget(self.list_page)
        self._update_buttons()
        self.delete_button.setFocus()

    def confirm_delete(self):
        if self._busy or self._confirmation_id is None:
            return
        ticket, self._confirmation_id = self._confirmation_id, None
        self._request("delete", ticket, receive="_deleted")

    def _deleted(self, result, error):
        self.stack.setCurrentWidget(self.list_page)
        self.cancel_button.setDefault(False)
        if not result.get("ok") or error:
            self._error(result)
            self.files.setFocus()
            return
        self.load_page(self._offset)
        self.files.setFocus()

    def _host_changed(self, _image_id):
        if self._closed or self.manager.chat.image_storage_epoch == self._epoch:
            return
        self._dirty = True
        if not self._busy:
            self.cancel_confirmation()
            self.load_page(self._offset)

    def keyPressEvent(self, event):
        if (self.stack.currentWidget() is self.confirm_page
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self.cancel_confirmation()
            event.accept()
            return
        super().keyPressEvent(event)

    def reject(self):
        if self.stack.currentWidget() is self.confirm_page and not self._busy:
            self.cancel_confirmation()
            return
        super().reject()

    def done(self, result):
        self._closed = True
        self._generation += 1
        self._discard_confirmation()
        super().done(result)

    def closeEvent(self, event):
        # Window close and application shutdown close the view in one action.
        # Escape alone retains the confirmation's cancel/back behavior.
        self.done(QDialog.DialogCode.Rejected)
        event.accept()


class ImageStorageUsageRow(QFrame):
    """Settings summary reads only while visible, with one in-flight request."""

    def __init__(self, chat, opener, parent=None):
        super().__init__(parent)
        self.chat, self.opener = chat, opener
        self._busy = False
        self._dirty = True
        self.setObjectName("generalSettingsCard")
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 12, 18, 12)
        title = QLabel("图片存储")
        title.setObjectName("settingsRowTitle")
        row.addWidget(title, 1)
        self.usage = QLabel("正在读取…")
        self.usage.setObjectName("hint")
        self.usage.setAccessibleName("图片存储用量")
        row.addWidget(self.usage)
        self.manage = _button("管理", "imageStorageManage", self._open)
        self.manage.setEnabled(callable(opener))
        row.addWidget(self.manage)
        changed = getattr(chat, "image_storage_content_changed", None)
        if changed is not None:
            changed.connect(self._changed)

    def _open(self):
        if callable(self.opener):
            self.opener(self.window())

    def _changed(self, _image_id):
        self._dirty = True
        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._dirty = True
        self._refresh()

    def _refresh(self):
        if not self.isVisible() or self._busy or not self._dirty:
            return
        try:
            manager = self.chat.image_storage_manager
            epoch = self.chat.image_storage_epoch
        except (AttributeError, RuntimeError):
            self.usage.setText("暂不可用")
            return
        self._busy, self._dirty = True, False
        reference = weakref.ref(self)
        delivered = False

        def loaded(result, error):
            nonlocal delivered
            widget = reference()
            if delivered or widget is None or not isValid(widget):
                return
            delivered = True
            widget._busy = False
            if widget.chat.image_storage_epoch != epoch:
                widget._dirty = True
            elif isinstance(result, dict) and result.get("ok") and not error:
                widget.usage.setText(_usage_text(result["usage"]))
            else:
                widget.usage.setText("暂不可用")
            widget._refresh()
        try:
            manager.load_usage(loaded)
        except Exception:
            loaded({}, "unavailable")
