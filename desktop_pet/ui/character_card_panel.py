"""Save local character cards, preview them and switch through the controller."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QStyle, QToolButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ..character_cards import CharacterCardError, load_character_card
from ..settings_manager import SettingsError
from .settings_widgets import CollapsibleSection
from .theme import style_widget


CARD_STYLE = """
QFrame#characterCardSurface { background: #ffffff; border: 1px solid #e6eaf0; border-radius: 12px; }
QLabel#characterCardTitle { color: #344054; font-size: 14px; font-weight: 600; }
QLabel#characterCardName { color: #263445; font-size: 18px; font-weight: 600; }
QLabel#characterCardCurrent, QLabel#characterCardSource, QLabel#characterCardHint,
QLabel#characterCardWarning { color: #667386; font-size: 12px; }
QPlainTextEdit#characterCardPreview, QPlainTextEdit#characterCardDetails {
    background: #f8fafc; border: 1px solid #e6eaf0; border-radius: 8px;
    padding: 10px; color: #344054; font-size: 13px; selection-background-color: #dceafa;
}
QPlainTextEdit#characterCardPreview:focus, QPlainTextEdit#characterCardDetails:focus {
    border-color: #87a8cb;
}
QComboBox#characterLibrary { background: #f8fafc; border: 1px solid #e1e6ed;
    border-radius: 7px; padding: 7px 10px; color: #43556a; }
QComboBox#characterLibrary:focus { border-color: #749cc5; }
QComboBox#characterLibrary::drop-down { border: none; background: transparent; width: 24px; }
QToolButton#refreshCharacterLibrary { background: #f8fafc; border: 1px solid #e1e6ed;
    border-radius: 7px; padding: 4px; }
QToolButton#refreshCharacterLibrary:hover { background: #edf2f8; border-color: #bdcfe1; }
QToolButton#refreshCharacterLibrary:focus { border-color: #749cc5; }
QPushButton { background: #f8fafc; border: 1px solid #e1e6ed; border-radius: 7px;
    padding: 7px 12px; color: #43556a; }
QPushButton:hover { background: #edf2f8; border-color: #bdcfe1; }
QPushButton:pressed { background: #e3ebf4; }
QPushButton:focus { border-color: #749cc5; }
QPushButton:disabled { color: #8c98a7; background: #f8fafc; border-color: #edf0f4; }
QPushButton#applyCharacterCard { background: #253449; border-color: #253449; color: #ffffff; }
QPushButton#applyCharacterCard:hover { background: #172334; }
QPushButton#applyCharacterCard:disabled { background: #e5eaf1; border-color: #e5eaf1; color: #738196; }
QLabel#characterCardStatus { color: #315b4c; font-size: 12px; }
QLabel#characterCardStatus[error="true"] { color: #a12622; }
QToolButton#settingsDisclosureButton { background: transparent; border: 1px solid transparent;
    padding: 7px 4px; text-align: left; color: #586b80; }
QToolButton#settingsDisclosureButton:hover { background: #f3f6f9; }
QToolButton#settingsDisclosureButton:focus { border-color: #87a8cb; }
"""


def _label(text="", name="characterCardHint"):
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


def _text_preview(name, accessible_name, minimum_height):
    preview = QPlainTextEdit()
    preview.setObjectName(name)
    preview.setAccessibleName(accessible_name)
    preview.setReadOnly(True)
    preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
    preview.setMinimumWidth(0)
    preview.setMinimumHeight(minimum_height)
    preview.setMaximumHeight(minimum_height + 70)
    return preview


class CharacterCardPanel(QWidget):
    """The controller owns persistence and saved-character notifications."""

    def __init__(self, settings_manager, chat_controller, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.chat_controller = chat_controller
        self._active_card = chat_controller.character_card
        self._preview_card = self._active_card
        self._library_entries = {}
        self._selected_library_id = None
        self._selected_library_card = None
        self._library_available = all(callable(getattr(settings_manager, name, None)) for name in (
            "list_character_cards", "save_library_character_card", "load_library_character_card",
            "rename_library_character_card", "delete_library_character_card"))
        self.setObjectName("characterCardPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        style_widget(self, CARD_STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        surface = QFrame()
        surface.setObjectName("characterCardSurface")
        surface.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(surface)
        layout = QVBoxLayout(surface)
        self._card_layout = layout
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        layout.addWidget(_label("角色卡", "characterCardTitle"))
        self.current_label = _label("", "characterCardCurrent")
        layout.addWidget(self.current_label)
        layout.addWidget(_label("导入 PNG 或 JSON 角色卡，保存后可随时切换。"))

        self.library_surface = QWidget()
        library_row = QHBoxLayout(self.library_surface)
        library_row.setContentsMargins(0, 0, 0, 0)
        library_row.setSpacing(6)
        self.library_combo = QComboBox()
        self.library_combo.setObjectName("characterLibrary")
        self.library_combo.setAccessibleName("已保存角色")
        self.library_combo.setMinimumWidth(0)
        self.library_combo.setMinimumHeight(36)
        self.library_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.library_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.library_combo.addItem("选择已保存角色…", None)
        self.library_combo.currentIndexChanged.connect(self._select_library_card)
        library_row.addWidget(self.library_combo, 1)
        self.refresh_button = QToolButton()
        self.refresh_button.setObjectName("refreshCharacterLibrary")
        self.refresh_button.setAccessibleName("刷新角色库")
        self.refresh_button.setToolTip("刷新角色库")
        self.refresh_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_button.setFixedSize(32, 36)
        self.refresh_button.clicked.connect(self._refresh_library)
        library_row.addWidget(self.refresh_button)
        self.rename_button = QPushButton("重命名")
        self.rename_button.setObjectName("renameLibraryCharacterCard")
        self.rename_button.setMinimumHeight(36)
        self.rename_button.clicked.connect(self._rename_library_card)
        library_row.addWidget(self.rename_button)
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("deleteLibraryCharacterCard")
        self.delete_button.setMinimumHeight(36)
        self.delete_button.clicked.connect(self._delete_library_card)
        library_row.addWidget(self.delete_button)
        self.library_surface.setVisible(self._library_available)
        layout.addWidget(self.library_surface)

        import_row = QHBoxLayout()
        self.import_button = QPushButton("导入角色卡…")
        self.import_button.setObjectName("importCharacterCard")
        self.import_button.setMinimumHeight(36)
        self.import_button.clicked.connect(self._import_card)
        import_row.addWidget(self.import_button)
        self.save_button = QPushButton("保存到角色库")
        self.save_button.setObjectName("saveLibraryCharacterCard")
        self.save_button.setMinimumHeight(36)
        self.save_button.clicked.connect(self._save_library_card)
        self.save_button.setVisible(self._library_available)
        import_row.addWidget(self.save_button)
        import_row.addStretch()
        layout.addLayout(import_row)

        self.source_label = _label("", "characterCardSource")
        self.source_label.hide()
        layout.addWidget(self.source_label)
        self.name_label = _label("", "characterCardName")
        self.name_label.setAccessibleDescription("预览角色名称")
        layout.addWidget(self.name_label)
        self.preview_text = _text_preview("characterCardPreview", "角色设定预览", 180)
        layout.addWidget(self.preview_text)
        self.warning_label = _label("", "characterCardWarning")
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

        self.details_text = _text_preview("characterCardDetails", "角色卡更多设定", 150)
        self.details_section = CollapsibleSection("更多设定", content=self.details_text)
        layout.addWidget(self.details_section)

        actions = QHBoxLayout()
        self.apply_button = QPushButton("应用角色")
        self.apply_button.setObjectName("applyCharacterCard")
        self.apply_button.setMinimumHeight(36)
        self.apply_button.clicked.connect(self._apply_card)
        actions.addWidget(self.apply_button)
        self.reset_button = QPushButton("恢复鲸鱼娘")
        self.reset_button.setObjectName("resetCharacterCard")
        self.reset_button.setMinimumHeight(36)
        self.reset_button.clicked.connect(self._reset_card)
        actions.addWidget(self.reset_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.status_label = _label("", "characterCardStatus")
        self.status_label.setAccessibleDescription("角色卡操作结果")
        self.status_label.hide()
        layout.addWidget(self.status_label)
        layout.addStretch()

        self.details_section.expandedChanged.connect(self._update_preview_layout)
        chat_controller.character_changed.connect(self._refresh_current)
        self._render_preview()
        self._update_current()
        if self._library_available:
            self._reload_library()

    def _set_status(self, text, *, error=False):
        self.status_label.setText(text)
        self.status_label.setProperty("error", error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.status_label.setVisible(bool(text))

    def _show_save_error(self):
        text = getattr(self.chat_controller, "error", "")
        if not isinstance(text, str) or not text.strip():
            text = "角色未能保存，请检查本地设置后重试。"
        text = text.strip()
        self._set_status(text if len(text) <= 600 else text[:599] + "…", error=True)

    def _update_current(self):
        name = self._active_card.name if self._active_card is not None else "鲸鱼娘"
        self.current_label.setText("当前使用 · " + name)
        self.apply_button.setEnabled(self._preview_card is not None and self._preview_card != self._active_card)
        self.apply_button.setText("切换角色" if self._selected_library_id else "应用角色")
        self.reset_button.setEnabled(self._active_card is not None)
        self.save_button.setEnabled(self._library_available and self._preview_card is not None)
        self.library_combo.setEnabled(bool(self._library_entries))
        selected = self._selected_library_id in self._library_entries
        self.rename_button.setEnabled(selected)
        self.delete_button.setEnabled(selected)

    def _show_library_error(self, error):
        text = str(error) if isinstance(error, SettingsError) else "角色库暂时不可用，请重新打开设置后重试。"
        self._set_status(text[:600], error=True)

    def _clear_library_selection(self):
        blocked = self.library_combo.blockSignals(True)
        self.library_combo.setCurrentIndex(0)
        self.library_combo.blockSignals(blocked)
        self._selected_library_id = None
        self._selected_library_card = None

    def _reload_library(self, selected_id=None, preview_card=None):
        try:
            entries = self.settings_manager.list_character_cards()
        except (SettingsError, ValueError, TypeError, OSError) as error:
            self._show_library_error(error)
            return False
        self._library_entries = {entry.id: entry for entry in entries}
        blocked = self.library_combo.blockSignals(True)
        self.library_combo.clear()
        self.library_combo.addItem("选择已保存角色…", None)
        for entry in entries:
            self.library_combo.addItem(entry.label, entry.id)
        index = self.library_combo.findData(selected_id) if selected_id is not None else 0
        self.library_combo.setCurrentIndex(max(0, index))
        self.library_combo.blockSignals(blocked)
        self._selected_library_id = selected_id if selected_id in self._library_entries else None
        self._selected_library_card = preview_card if self._selected_library_id else None
        self._update_current()
        return True

    def _select_library_card(self, _index):
        identity = self.library_combo.currentData()
        if identity is None:
            self._selected_library_id = None
            self._selected_library_card = None
            self._update_current()
            return
        previous = self._selected_library_id
        try:
            card = self.settings_manager.load_library_character_card(identity)
        except (SettingsError, ValueError, TypeError, OSError) as error:
            blocked = self.library_combo.blockSignals(True)
            self.library_combo.setCurrentIndex(max(0, self.library_combo.findData(previous)))
            self.library_combo.blockSignals(blocked)
            self._show_library_error(error)
            return
        self._selected_library_id = identity
        self._selected_library_card = self._preview_card = card
        self.source_label.setText("角色库 · " + self._library_entries[identity].label)
        self.source_label.show()
        self._render_preview()
        self._update_current()
        self._set_status("")

    def _refresh_library(self):
        previous = self._selected_library_id
        if not self._reload_library(previous, self._selected_library_card):
            return
        if self._selected_library_id is not None:
            self.source_label.setText("角色库 · " + self._library_entries[self._selected_library_id].label)
        elif previous is not None:
            self.source_label.setText("角色预览")
        self._set_status("角色库已刷新。")

    def _save_library_card(self):
        if not self._library_available or self._preview_card is None:
            return
        try:
            entry = self.settings_manager.save_library_character_card(self._preview_card)
        except (SettingsError, ValueError, TypeError, OSError) as error:
            self._show_library_error(error)
            return
        if not self._reload_library(entry.id, self._preview_card):
            return
        self.source_label.setText("角色库 · " + entry.label)
        self.source_label.show()
        self._set_status("已保存到角色库。")

    def _rename_library_card(self):
        identity = self._selected_library_id
        if identity not in self._library_entries:
            return
        label, accepted = QInputDialog.getText(self, "重命名角色卡", "角色库名称", text=self._library_entries[identity].label)
        if not accepted:
            return
        try:
            entry = self.settings_manager.rename_library_character_card(identity, label)
        except (SettingsError, ValueError, TypeError, OSError) as error:
            self._show_library_error(error)
            return
        if not self._reload_library(entry.id, self._preview_card):
            return
        self.source_label.setText("角色库 · " + entry.label)
        self.source_label.show()
        self._set_status("名称已更新。")

    def _delete_library_card(self):
        identity = self._selected_library_id
        if identity not in self._library_entries:
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("删除角色卡")
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText("从角色库删除“" + self._library_entries[identity].label + "”？")
        dialog.setInformativeText("当前使用的角色会保留。")
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        confirmed = dialog.exec() == QMessageBox.StandardButton.Yes
        dialog.deleteLater()
        if not confirmed:
            return
        try:
            self.settings_manager.delete_library_character_card(identity)
        except (SettingsError, ValueError, TypeError, OSError) as error:
            self._show_library_error(error)
            return
        if not self._reload_library():
            return
        self._preview_card = self._active_card
        self.source_label.clear()
        self.source_label.hide()
        self._render_preview()
        self._update_current()
        self._set_status("已从角色库删除。")

    def _update_preview_layout(self):
        self.details_section.layout().activate()
        self._card_layout.activate()
        self.updateGeometry()

    def _refresh_current(self, *_):
        pending_preview = self._preview_card != self._active_card
        self._active_card = self.chat_controller.character_card
        if not pending_preview:
            self._preview_card = self._active_card
            if self._selected_library_card != self._active_card:
                self._clear_library_selection()
                self.source_label.clear()
                self.source_label.hide()
            self._render_preview()
        self._update_current()

    def _render_preview(self):
        card = self._preview_card
        if card is None:
            self.name_label.setText("鲸鱼娘")
            self.preview_text.setPlainText("默认陪伴角色\n\n用简短、自然的中文和你交流。")
            self.details_text.clear()
            self.details_section.hide()
            self.warning_label.clear()
            self.warning_label.hide()
            self._update_preview_layout()
            return
        self.name_label.setText(card.name)
        fields = (("性格", card.personality), ("背景", card.description),
                  ("场景", card.scenario), ("开场白", card.first_mes))
        preview = "\n\n".join(title + "\n" + text for title, text in fields if text)
        self.preview_text.setPlainText(preview or "角色卡已载入，可以应用到对话。")
        advanced = (("示例对话", card.mes_example), ("作者备注", card.creator_notes),
                    ("系统设定", card.system_prompt), ("对话后设定", card.post_history_instructions))
        details = "\n\n".join(title + "\n" + text for title, text in advanced if text)
        if card.alternate_greetings:
            greetings = "\n\n".join(card.alternate_greetings)
            details = "\n\n".join(text for text in (details, "其他开场白\n" + greetings) if text)
        self.details_text.setPlainText(details)
        self.details_section.setVisible(bool(details))
        self.details_section.setExpanded(False)
        self.warning_label.setText("\n".join(card.warnings))
        self.warning_label.setVisible(bool(card.warnings))
        self._update_preview_layout()

    def _import_card(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入角色卡", "", "角色卡 (*.png *.json);;所有文件 (*)")
        if not path:
            return
        try:
            card = load_character_card(path)
        except CharacterCardError as error:
            self._set_status(str(error), error=True)
            return
        except OSError:
            self._set_status("无法读取角色卡，请检查文件后重新导入。", error=True)
            return
        self._preview_card = card
        self._clear_library_selection()
        self.source_label.setText("预览文件 · " + Path(path).name)
        self.source_label.show()
        self._render_preview()
        self._update_current()
        self._set_status("预览已载入，可以保存到角色库或应用角色。")

    def _apply_card(self):
        if self._preview_card is None or self._preview_card == self._active_card:
            return
        if self.chat_controller.set_character_card(self._preview_card) is not True:
            self._show_save_error()
            return
        self._refresh_current()
        self._set_status("角色已应用。")

    def _reset_card(self):
        if self._active_card is None:
            return
        if self.chat_controller.set_character_card(None) is not True:
            self._show_save_error()
            return
        self._active_card = None
        self._preview_card = None
        self._clear_library_selection()
        self.source_label.clear()
        self.source_label.hide()
        self._render_preview()
        self._update_current()
        self._set_status("已恢复鲸鱼娘。")
