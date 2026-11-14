"""Optional Mem0 controls, deliberately separate from conversation history."""

from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractButton, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QLayout, QPlainTextEdit, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from .theme import style_widget
from .styles import PANEL_STYLE
from .settings_widgets import SettingsToggle


def _plain_label(text="", name="hint"):
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class MemoryEditDialog(QDialog):
    submitted = Signal(str)

    def __init__(self, memory, parent=None):
        super().__init__(parent)
        self.memory_id = str(memory.get("id", ""))
        self.original = str(memory.get("memory", ""))
        self.setObjectName("memoryEditDialog")
        self.setWindowTitle("编辑 / 纠正长期记忆")
        # Keep the page reachable while a failed edit remains open for retry.
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(560, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(_plain_label("填写你希望鲸鱼娘记住的内容（最多 1200 字）。修改后保留更正历史。"))
        from ..conversations.context import memory_is_active
        if not memory_is_active(memory):
            layout.addWidget(_plain_label("此记录已失效。编辑只修改内容，保留原有效期和失效标记，不会重新启用。"))
        self.text_input = QPlainTextEdit(self.original)
        self.text_input.setObjectName("memoryEditText")
        layout.addWidget(self.text_input, 1)
        self.error_label = _plain_label("", "error")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("保存纠正")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        for button in self.buttons.buttons():
            button.setMinimumHeight(32)
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _submit(self):
        text = self.text_input.toPlainText().strip()
        if not 1 <= len(text) <= 1200:
            self.show_error("请填写 1～1200 字的完整事实；当前输入已保留。")
            return
        if text == self.original:
            self.reject()
            return
        self.show_error("")
        self.submitted.emit(text)

    def show_error(self, text):
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    def set_busy(self, busy):
        for button in self.buttons.buttons():
            button.setEnabled(not busy)
        self.save_button.setText("正在保存…" if busy else "保存纠正")
        self.text_input.setReadOnly(busy)


class MemoryPanel(QWidget):
    """All persistence, SDK work and credential access belong to the controller."""

    conversation_requested = Signal(str)

    def __init__(self, controller, parent=None, *, embedded=False):
        super().__init__(parent, Qt.WindowType.Widget if embedded else Qt.WindowType.Window)
        self._embedded = embedded
        self.controller = controller
        self._edit_dialog = None
        self.setObjectName("memoryPanel")
        self.setWindowTitle("鲸伴 · 长期记忆")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        if not embedded:
            style_widget(self, PANEL_STYLE)
            self.setMinimumSize(590, 500)
            self.resize(700, 700)
        layout = QVBoxLayout(self)
        if not embedded:
            heading = _plain_label("长期记忆", "title")
            layout.addWidget(heading)
        else:
            layout.setContentsMargins(0, 0, 0, 0)
        self.automatic_checkbox = SettingsToggle("自动记忆")
        self.automatic_checkbox.setObjectName("automaticMemory")
        self.automatic_checkbox.setToolTip("记住你表达的偏好和重要事项；关闭后不再新增，已有记忆仍可使用。")
        self.automatic_checkbox.toggled.connect(self._set_automatic)
        layout.addWidget(self.automatic_checkbox)
        layout.addWidget(_plain_label("记住你的偏好和重要事项。关闭后，已有记忆仍可使用。"))
        layout.addWidget(self._make_memory_list(), 1)
        self.status_label = _plain_label("", "memoryOperationStatus")
        self.status_label.hide()
        layout.addWidget(self.status_label)
        error_row = QHBoxLayout()
        self.error_label = _plain_label("", "error")
        error_row.addWidget(self.error_label, 1)
        self.retry_button = QPushButton("重新准备")
        self.retry_button.setObjectName("retryMemorySetup")
        self.retry_button.clicked.connect(self._retry_setup)
        self.retry_button.hide()
        error_row.addWidget(self.retry_button)
        layout.addLayout(error_row)
        controller.memories_changed.connect(self._render_memories)
        for name, callback in (("history_loaded", self._show_history),
                               ("memory_update_finished", self._memory_update_finished),
                               ("settings_changed", self._settings_changed),
                               ("busy_changed", self._busy_changed),
                               ("error_changed", self._show_error),
                               ("automatic_pause_changed", self._runtime_pause_changed),
                               ("setup_state_changed", self._setup_state_changed)):
            signal = getattr(controller, name, None)
            if signal is not None:
                signal.connect(callback)
        self._settings_changed(controller.settings)
        self._render_memories()
        self._busy_changed(getattr(controller, "busy", False))
        self._show_error(getattr(controller, "error", ""))
        for control in self.findChildren(QAbstractButton) + self.findChildren(QLineEdit):
            control.setMinimumHeight(32)
        style_widget(self, ("" if embedded else PANEL_STYLE) + """
            QWidget#memoryPanel { background: #ffffff; }
            QListWidget#memoryList { background: #ffffff; border: 1px solid #e1e7ee; border-radius: 10px; outline: none; }
            QListWidget#memoryList::item { color: #283d50; padding: 12px 14px; border: none; border-radius: 7px; }
            QListWidget#memoryList::item:selected { background: #eaf2fa; color: #203e5b; }
            QListWidget#memoryList::item:hover:!selected { background: #f5f8fb; }
            QLabel#memoryCount, QLabel#memoryOperationStatus { color: #718294; font-size: 12px; background: transparent; border: none; }
            QPushButton#clearMemoriesButton { color: #a44a4a; }
        """)

    def _make_memory_list(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        self.memories_scroll = QScrollArea()
        self.memories_scroll.setObjectName("memoryContentsScroll")
        self.memories_scroll.setWidgetResizable(True)
        self.memories_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(self.memories_scroll)
        contents = QWidget()
        self.memories_scroll.setWidget(contents)
        layout = QVBoxLayout(contents)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        search = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setObjectName("memorySearch")
        self.search_input.setPlaceholderText("搜索记忆")
        self.search_input.setMaxLength(4000)
        self.search_input.setToolTip("搜索已保存的记忆内容。")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.returnPressed.connect(self._refresh)
        search.addWidget(self.search_input, 1)
        self.refresh_button = QPushButton("搜索")
        self.refresh_button.setObjectName("refreshMemoriesButton")
        self.refresh_button.clicked.connect(self._refresh)
        search.addWidget(self.refresh_button)
        layout.addLayout(search)
        self.memory_list = QListWidget()
        self.memory_list.setObjectName("memoryList")
        self.memory_list.setWordWrap(True)
        self.memory_list.setAccessibleName("长期记忆列表")
        self.memory_list.setMinimumHeight(180)
        self.memory_list.setSpacing(3)
        self.memory_list.currentItemChanged.connect(self._selection_changed)
        layout.addWidget(self.memory_list, 1)
        self.empty_label = _plain_label()
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setMinimumHeight(180)
        self.empty_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.empty_label, 1)
        self.pager = QWidget()
        pager = QHBoxLayout(self.pager)
        pager.setContentsMargins(0, 0, 0, 0)
        self.memory_count_label = _plain_label()
        self.memory_count_label.setObjectName("memoryCount")
        pager.addWidget(self.memory_count_label, 1)
        self.previous_button = QPushButton("上一页")
        self.previous_button.setObjectName("previousMemoriesButton")
        self.previous_button.clicked.connect(controller_call(self.controller, "previous_page"))
        self.next_button = QPushButton("下一页")
        self.next_button.setObjectName("nextMemoriesButton")
        self.next_button.clicked.connect(controller_call(self.controller, "next_page"))
        pager.addWidget(self.previous_button)
        pager.addWidget(self.next_button)
        layout.addWidget(self.pager)
        details = QGroupBox("详情")
        self.memory_details_group = details
        details.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        detail_layout = QVBoxLayout(details)
        detail_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.memory_details_label = _plain_label("选择记录查看来源、时间和有效期。")
        self.memory_details_label.setObjectName("memoryDetails")
        detail_layout.addWidget(self.memory_details_label)
        self.source_button = QPushButton("查看来源会话")
        self.source_button.setObjectName("memorySourceConversation")
        self.source_button.clicked.connect(self._open_source_conversation)
        detail_layout.addWidget(self.source_button, alignment=Qt.AlignmentFlag.AlignLeft)
        details.hide()
        layout.addWidget(details)
        row = QHBoxLayout()
        self.edit_button = QPushButton("修改记忆")
        self.edit_button.setObjectName("editMemoryButton")
        self.edit_button.clicked.connect(self._edit_memory)
        row.addWidget(self.edit_button)
        self.history_button = QPushButton("更正历史")
        self.history_button.setObjectName("memoryHistoryButton")
        self.history_button.clicked.connect(self._load_history)
        row.addWidget(self.history_button)
        self.delete_button = QPushButton("删除记忆")
        self.delete_button.setObjectName("deleteMemoryButton")
        self.delete_button.clicked.connect(self._delete_memory)
        row.addWidget(self.delete_button)
        row.addStretch()
        self.clear_button = QPushButton("清空长期记忆")
        self.clear_button.setObjectName("clearMemoriesButton")
        self.clear_button.clicked.connect(self._clear_memories)
        row.addWidget(self.clear_button)
        self.memory_actions = QWidget()
        self.memory_actions.setLayout(row)
        detail_layout.addWidget(self.memory_actions)
        self.clear_button.setParent(contents)
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(self.clear_button)
        layout.addLayout(footer)
        return tab

    def _settings_changed(self, settings):
        requested = getattr(self.controller, "automatic_requested", None)
        preparing = getattr(self.controller, "setup_state", "") == "preparing"
        checked = bool(getattr(self.controller, "automatic_enabled", settings.enabled and settings.automatic
                               and not getattr(self.controller, "automatic_paused", False)))
        if preparing or getattr(self.controller, "busy", False):
            checked = requested if type(requested) is bool else (True if preparing else checked)
        self.automatic_checkbox.blockSignals(True)
        self.automatic_checkbox.setChecked(checked)
        self.automatic_checkbox.blockSignals(False)
        self.automatic_checkbox.setEnabled(not getattr(self.controller, "busy", False) or checked)
        self._update_delete_button()

    def _set_automatic(self, enabled):
        setter = getattr(self.controller, "set_automatic_enabled", None)
        if not callable(setter) or setter(bool(enabled)) is False:
            self._settings_changed(self.controller.settings)
            self._show_error("记忆设置暂时无法保存，请稍后重试。")

    def _runtime_pause_changed(self, _paused):
        self._settings_changed(self.controller.settings)

    def _retry_setup(self):
        message = str(getattr(self.controller, "setup_error", "") or getattr(self.controller, "error", ""))
        if getattr(self.controller, "automatic_requested", None) is False and "关闭" in message:
            self.controller.set_automatic_enabled(False)
            return
        retry = getattr(self.controller, "prepare_automatic_memory", None)
        if not callable(retry) or retry() is False:
            self._refresh()

    def _setup_state_changed(self, *_args):
        self._settings_changed(self.controller.settings)
        self._busy_changed(getattr(self.controller, "busy", False))
        self._show_error(getattr(self.controller, "error", ""))

    def _render_memories(self):
        from ..conversations.context import memory_is_active
        selected = self.memory_list.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.memory_list.clear()
        memories = getattr(self.controller, "memories", [])
        stale = getattr(self.controller, "memory_list_stale", False)
        preferred_id = getattr(self.controller, "memory_preferred_id", None)
        query = getattr(self.controller, "memory_query", self.search_input.text().strip())
        self.empty_label.setText(
            "没有找到相关记忆，换个关键词试试。" if query else
            "还没有记忆。你可以在聊天时告诉鲸鱼娘你的偏好。"
        )
        for memory in memories:
            memory_id = str(memory.get("id", ""))
            content = str(memory.get("memory") or memory.get("content") or "")
            item = QListWidgetItem(content if memory_is_active(memory) else "已失效 · " + content)
            item.setData(Qt.ItemDataRole.UserRole, memory_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, content)
            item.setData(Qt.ItemDataRole.UserRole + 2, memory)
            item.setToolTip("<qt>" + escape(content) + "</qt>")
            self.memory_list.addItem(item)
            if memory_id == selected_id:
                self.memory_list.setCurrentItem(item)
        if self.memory_list.currentItem() is None and preferred_id:
            for index in range(self.memory_list.count()):
                item = self.memory_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == preferred_id:
                    self.memory_list.setCurrentItem(item)
                    break
        if stale and not memories:
            self.empty_label.setText("删除已完成；当前页暂无缓存记录，请刷新以加载其余记忆。")
        self.empty_label.setVisible(not memories)
        self.memory_list.setVisible(bool(memories))
        total = getattr(self.controller, "memory_total", len(memories))
        matched = getattr(self.controller, "memory_matched_total", len(memories))
        offset = getattr(self.controller, "memory_offset", 0)
        summary = f"{matched} 条匹配记忆" if query else f"{total} 条记忆"
        if stale:
            summary += " · 待刷新"
        self.memory_count_label.setText(summary)
        self.memory_count_label.setVisible(bool(total or query or stale))
        self.previous_button.setVisible(offset > 0)
        self.next_button.setVisible(bool(getattr(self.controller, "memory_has_more", False)))
        self._update_delete_button()
        self._selection_changed()

    def _selected_memory(self):
        item = self.memory_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole + 2) if item is not None else None

    def _selection_changed(self, *_args):
        self._update_delete_button()
        if not hasattr(self, "memory_details_label"):
            return
        memory = self._selected_memory()
        has_source = bool(memory and (memory.get("metadata") or {}).get("conversation_id"))
        self.source_button.setEnabled(has_source)
        self.source_button.setVisible(has_source)
        self.memory_details_group.setVisible(memory is not None)
        if not memory:
            self.memory_details_label.setText("选择记录查看来源、时间和有效期。")
            self.memory_details_label.setMinimumHeight(0)
            return
        metadata = memory.get("metadata") or {}
        source = "手动纠正" if metadata.get("correction_source") == "manual_correction" else "用户明确改口" if metadata.get("correction_source") == "explicit_user_correction" else "自动提炼" if metadata.get("trigger") != "explicit" and metadata.get("trigger") else "用户明确事实" if metadata.get("source") == "explicit_user_fact" else "未记录"
        def short_time(value):
            return str(value).replace("T", " ")[:16]
        lines = ["来源：" + source]
        if memory.get("created_at"):
            lines.append("创建：" + short_time(memory["created_at"]))
        if memory.get("updated_at"):
            lines.append("更新：" + short_time(memory["updated_at"]))
        if metadata.get("effective_at"):
            lines.append("生效：" + short_time(metadata["effective_at"]))
        if memory.get("expiration_date"):
            lines.append("有效期至：" + short_time(memory["expiration_date"]))
        from ..conversations.context import memory_is_active
        if not memory_is_active(memory):
            lines.append("已失效，修改内容不会重新启用。")
        self.memory_details_label.setText("\n".join(lines))
        self.memory_details_label.setMinimumHeight(self.memory_details_label.fontMetrics().lineSpacing() * len(lines) + 8)
        self.memory_details_group.layout().invalidate()
        self.memory_details_group.layout().activate()
        self.memory_details_group.updateGeometry()
        self.memories_scroll.widget().layout().activate()

    def _open_source_conversation(self):
        memory = self._selected_memory()
        conversation = (memory.get("metadata") or {}).get("conversation_id") if memory else None
        if isinstance(conversation, str) and conversation.strip():
            self.conversation_requested.emit(conversation.strip())

    def _update_delete_button(self):
        if hasattr(self, "delete_button"):
            self.delete_button.setEnabled(
                self.memory_list.currentItem() is not None and not getattr(self.controller, "busy", False)
            )
            self.edit_button.setEnabled(self.delete_button.isEnabled())
            self.history_button.setEnabled(self.delete_button.isEnabled())
            idle = not getattr(self.controller, "busy", False)
            total = getattr(self.controller, "memory_total", len(getattr(self.controller, "memories", [])))
            reset_available = bool(total or getattr(self.controller, "can_reset_memories", False))
            self.clear_button.setVisible(reset_available)
            self.clear_button.setEnabled(idle and reset_available)
            self.clear_button.setText("清空长期记忆" if total else "重置记忆库")
            self.previous_button.setEnabled(idle and getattr(self.controller, "memory_offset", 0) > 0)
            self.next_button.setEnabled(idle and getattr(self.controller, "memory_has_more", False))

    def _refresh(self):
        if not getattr(self.controller, "busy", False):
            self.controller.refresh(self.search_input.text().strip())

    def _delete_memory(self):
        item = self.memory_list.currentItem()
        if item is None or getattr(self.controller, "busy", False):
            return
        # The confirmation runs a nested event loop; a refresh can destroy the
        # native list item while it is open. Preserve the logical target now.
        memory_id = str(item.data(Qt.ItemDataRole.UserRole))
        answer = QMessageBox.question(
            self, "删除长期记忆", "确定删除这条记忆？聊天记录仍然保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.delete_memory(memory_id)

    def _edit_memory(self):
        memory = self._selected_memory()
        if memory is None or getattr(self.controller, "busy", False):
            return
        if self._edit_dialog is not None:
            self._edit_dialog.show()
            self._edit_dialog.raise_()
            return
        dialog = MemoryEditDialog(memory, self)
        self._edit_dialog = dialog
        def submit(text):
            if self.controller.update_memory(dialog.memory_id, text, expected_text=dialog.original) is False:
                dialog.show_error("当前有后台操作，请稍后重试；输入已保留。")
        dialog.submitted.connect(submit)
        def finished():
            if self._edit_dialog is dialog:
                self._edit_dialog = None
            dialog.deleteLater()
        dialog.finished.connect(finished)
        dialog.show()

    def _memory_update_finished(self, memory_id, success, error):
        dialog = self._edit_dialog
        if dialog is None or dialog.memory_id != memory_id:
            return
        if success:
            dialog.accept()
        else:
            dialog.show_error(self._readable_error(error) + "\n纠正尚未保存，输入已保留，请稍后重试。")

    def _load_history(self):
        item = self.memory_list.currentItem()
        if item is not None and not getattr(self.controller, "busy", False):
            self.controller.load_history(str(item.data(Qt.ItemDataRole.UserRole)))

    def _show_history(self, events):
        dialog = QDialog(self)
        dialog.setWindowTitle("记忆更正历史")
        dialog.resize(620, 430)
        layout = QVBoxLayout(dialog)
        layout.addWidget(_plain_label("查看这条记忆的历次修改。聊天时使用最新的有效内容。"))
        content = QPlainTextEdit()
        content.setReadOnly(True)
        parts = []
        for event in events:
            timestamp = event.get("updated_at") or event.get("created_at") or "未记录时间"
            event_name = {"UPDATE": "修改", "ADD": "新增", "DELETE": "删除"}.get(str(event.get("event", "")).upper(), "记录")
            lines = [f"{event_name} · {timestamp}"]
            if event.get("old_memory"):
                lines.append("旧值（已替代）：" + str(event["old_memory"]))
            if event.get("new_memory"):
                lines.append("当时保存的值：" + str(event["new_memory"]))
            parts.append("\n".join(lines))
        content.setPlainText("\n\n".join(parts) or "这条记忆还没有更正历史。")
        layout.addWidget(content, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _clear_memories(self):
        if getattr(self.controller, "busy", False):
            return
        empty = not getattr(self.controller, "memory_total", len(getattr(self.controller, "memories", [])))
        answer = QMessageBox.question(
            self, "重置记忆库" if empty else "清空长期记忆",
            ("重置长期记忆的模型绑定？聊天记录保留，其他用户的记忆不会删除。" if empty
             else "确定清空所有长期记忆？聊天记录仍然保留，此操作无法撤销。"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.clear_memories()

    def _busy_changed(self, busy):
        for field in (self.refresh_button, self.search_input, self.clear_button, self.retry_button):
            field.setEnabled(not busy)
        # Turning memory off must remain possible while extraction is running.
        self.automatic_checkbox.setEnabled(not busy or self.automatic_checkbox.isChecked())
        self.status_label.setText("正在准备记忆…" if getattr(self.controller, "setup_state", "") in ("preparing", "repairing") else "正在处理…")
        self.status_label.setVisible(bool(busy))
        if not busy:
            self._settings_changed(self.controller.settings)
        self._update_delete_button()
        if self._edit_dialog is not None:
            self._edit_dialog.set_busy(busy)

    @staticmethod
    def _readable_error(message):
        simple = str(message or "")
        technical = ("Embedding", "Mem0", "Token", "API Key", "密钥", "memory_settings", "FastEmbed", "模型")
        actionable = "重启后可能恢复" in simple or ("请先" in simple and "聊天" in simple)
        if not actionable and any(term.casefold() in simple.casefold() for term in technical):
            return "记忆暂时不可用，请重新准备后再试。聊天仍可正常使用。"
        if len(simple) > 160:
            return "记忆操作未完成，请稍后重试。聊天仍可正常使用。"
        return simple

    def _show_error(self, message):
        message = message or getattr(self.controller, "setup_error", "")
        if message:
            self._settings_changed(self.controller.settings)
        simple = self._readable_error(message)
        self.error_label.setText(simple)
        self.error_label.setVisible(bool(simple))
        self.retry_button.setText("重试关闭" if "重启后可能恢复" in simple else "重新准备")
        self.retry_button.setVisible(bool(simple) and callable(getattr(self.controller, "prepare_automatic_memory", None)))

    def present(self):
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self._refresh()

    def closeEvent(self, event):
        self.hide()
        event.ignore()

    def clear_credential_inputs(self):
        """Compatibility hook for the host; this page has no credential inputs."""
        return None


def controller_call(controller, name):
    """Discard Qt's checked argument rather than sending it into SDK operations."""
    return lambda checked=False: getattr(controller, name)()
