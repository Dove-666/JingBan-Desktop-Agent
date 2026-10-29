"""Independent controls, credential settings, and synchronized chat windows."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSlider, QTabWidget, QVBoxLayout, QWidget,
)

from ..settings_manager import ApiSettings
from .chat_panel import ChatPanel
from .styles import PANEL_STYLE


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class _HideOnCloseWindow(QWidget):
    def closeEvent(self, event) -> None:
        self.hide()
        event.ignore()

    def present(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()


class DebugPanel(_HideOnCloseWindow):
    def __init__(self, pet_controller, settings_manager, chat_controller, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.pet_controller = pet_controller
        self.settings_manager = settings_manager
        self.chat_controller = chat_controller
        self.setObjectName("debugPanel")
        self.setWindowTitle("鲸鱼娘 · 调试面板")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(620, 580)
        self.resize(730, 740)
        self.setStyleSheet(PANEL_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        title = QLabel("鲸鱼娘控制台")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("调整角色、配置聊天服务，随时回到桌面继续互动。")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("debugTabs")
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._make_pet_tab(), "角色控制")
        self.tabs.addTab(self._make_api_tab(), "API 设置")
        self.chat_panel = ChatPanel(chat_controller)
        self.tabs.addTab(self.chat_panel, "聊天测试")
        layout.addWidget(_hint("关闭此面板会保留桌宠和当前会话。退出桌宠请使用角色右键菜单。"))

    def show_chat_tab(self) -> None:
        self.tabs.setCurrentWidget(self.chat_panel)
        self.present()

    def hideEvent(self, event) -> None:
        # Reopening settings must not expose a temporary key left in reveal mode.
        # Keep the draft itself so closing the panel never discards user input.
        self.show_key_button.setChecked(False)
        super().hideEvent(event)

    def _make_pet_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(15)
        size_group = QGroupBox("角色大小")
        size_layout = QVBoxLayout(size_group)
        size_layout.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(_hint("相对于启动时的默认大小，实时调整并自动保存。"), 1)
        self.scale_label = QLabel()
        self.scale_label.setObjectName("scaleValue")
        header.addWidget(self.scale_label)
        size_layout.addLayout(header)
        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setObjectName("scaleSlider")
        self.scale_slider.setRange(50, 200)
        self.scale_slider.setTickInterval(25)
        self.scale_slider.setSingleStep(1)
        self.scale_slider.setPageStep(25)
        self.scale_slider.setAccessibleName("桌宠大小百分比")
        size_layout.addWidget(self.scale_slider)
        presets = QHBoxLayout()
        for value in (50, 75, 100, 125, 150, 200):
            button = QPushButton(f"{value}%")
            button.setObjectName(f"scalePreset{value}")
            button.setMinimumWidth(0)
            button.clicked.connect(lambda checked=False, percent=value: self.pet_controller.set_scale(percent))
            presets.addWidget(button)
        size_layout.addLayout(presets)
        reset = QPushButton("恢复默认大小")
        reset.setObjectName("resetScaleButton")
        reset.clicked.connect(self.pet_controller.reset_scale)
        size_layout.addWidget(reset, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(size_group)
        expression_group = QGroupBox("表情与动作")
        expression_layout = QVBoxLayout(expression_group)
        expression_layout.setSpacing(12)
        row = QHBoxLayout()
        self.expression_combo = QComboBox()
        self.expression_combo.setObjectName("expressionCombo")
        self.expression_combo.setAccessibleName("角色表情")
        for code, label, status in self.pet_controller.expression_catalog():
            self.expression_combo.addItem(f"{label}  ·  {code}", code)
            self.expression_combo.setItemData(self.expression_combo.count() - 1, status, Qt.ItemDataRole.ToolTipRole)
        row.addWidget(self.expression_combo, 1)
        test = QPushButton("测试当前表情")
        test.setObjectName("testExpressionButton")
        test.clicked.connect(self.pet_controller.test_expression)
        row.addWidget(test)
        expression_layout.addLayout(row)
        self.expression_status = QLabel()
        self.expression_status.setObjectName("status")
        self.expression_status.setTextFormat(Qt.TextFormat.PlainText)
        self.expression_status.setWordWrap(True)
        expression_layout.addWidget(self.expression_status)
        expression_layout.addWidget(_hint("缺少独立表情 PNG 时，只切换逻辑状态，并以轻量动作或气泡反馈；原图不会被修改。"))
        layout.addWidget(expression_group)
        self.pet_error_label = QLabel()
        self.pet_error_label.setObjectName("error")
        self.pet_error_label.setAccessibleName("角色控制错误")
        self.pet_error_label.setWordWrap(True)
        self.pet_error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.pet_error_label.hide()
        layout.addWidget(self.pet_error_label)
        layout.addStretch()
        self.scale_slider.valueChanged.connect(self.pet_controller.set_scale)
        self.pet_controller.scale_changed.connect(self._scale_changed)
        self.expression_combo.currentIndexChanged.connect(self._select_expression)
        self.pet_controller.expression_changed.connect(self._expression_changed)
        if hasattr(self.pet_controller, "error_occurred"):
            self.pet_controller.error_occurred.connect(self._show_pet_error)
        self._scale_changed(self.pet_controller.scale_percent)
        self._expression_changed(self.pet_controller.expression, self.pet_controller.expression_status)
        return tab

    def _scale_changed(self, percent: int) -> None:
        self._show_pet_error("")
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(percent)
        self.scale_slider.blockSignals(False)
        self.scale_label.setText(f"{percent}%")

    def _show_pet_error(self, message: str) -> None:
        self.pet_error_label.setText(message)
        self.pet_error_label.setVisible(bool(message))

    def _select_expression(self, index: int) -> None:
        code = self.expression_combo.itemData(index)
        if code:
            self.pet_controller.set_expression(code)

    def _expression_changed(self, code: str, status: str) -> None:
        index = self.expression_combo.findData(code)
        self.expression_combo.blockSignals(True)
        self.expression_combo.setCurrentIndex(index)
        self.expression_combo.blockSignals(False)
        self.expression_status.setText(status)

    def _make_api_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        contents = QWidget()
        scroll.setWidget(contents)
        layout = QVBoxLayout(contents)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        group = QGroupBox("兼容聊天服务")
        form = QFormLayout(group)
        form.setSpacing(13)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.base_url_input = QLineEdit()
        self.base_url_input.setObjectName("apiBaseUrl")
        self.base_url_input.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("Base URL", self.base_url_input)
        key_row = QHBoxLayout()
        self.key_input = QLineEdit()
        self.key_input.setObjectName("apiKey")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("输入新密钥；留空保留已保存密钥")
        self.key_input.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        key_row.addWidget(self.key_input, 1)
        self.show_key_button = QPushButton("显示")
        self.show_key_button.setObjectName("showApiKeyButton")
        self.show_key_button.setCheckable(True)
        self.show_key_button.toggled.connect(self._toggle_key)
        key_row.addWidget(self.show_key_button)
        form.addRow("API Key", key_row)
        self.model_input = QLineEdit()
        self.model_input.setObjectName("apiModel")
        self.model_input.setPlaceholderText("服务商提供的模型名称")
        form.addRow("模型名称", self.model_input)
        layout.addWidget(group)
        layout.addWidget(_hint("填写服务商的 API 根地址和可用模型。连接测试会发送一次真实的短聊天请求，可能产生少量费用。"))
        self.credential_label = QLabel()
        self.credential_label.setObjectName("status")
        self.credential_label.setWordWrap(True)
        self.credential_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.credential_label)
        buttons = QHBoxLayout()
        self.save_api_button = QPushButton("保存配置")
        self.save_api_button.setObjectName("saveApiButton")
        self.save_api_button.clicked.connect(self._save_api)
        buttons.addWidget(self.save_api_button)
        self.test_connection_button = QPushButton("测试连接")
        self.test_connection_button.setObjectName("testConnectionButton")
        self.test_connection_button.clicked.connect(self._test_connection)
        buttons.addWidget(self.test_connection_button)
        buttons.addStretch()
        self.clear_key_button = QPushButton("清除 API Key")
        self.clear_key_button.setObjectName("clearApiKeyButton")
        self.clear_key_button.clicked.connect(self._clear_key)
        buttons.addWidget(self.clear_key_button)
        layout.addLayout(buttons)
        self.connection_label = QLabel(getattr(self.chat_controller, "connection_status", "尚未测试连接"))
        self.connection_label.setObjectName("status")
        self.connection_label.setWordWrap(True)
        self.connection_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.connection_label)
        self.api_error_label = QLabel()
        self.api_error_label.setObjectName("error")
        self.api_error_label.setWordWrap(True)
        self.api_error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.api_error_label.hide()
        layout.addWidget(self.api_error_label)
        layout.addWidget(_hint("密钥仅保存到系统凭据库，不会写入普通配置文件。聊天使用已保存的配置和密钥；输入框中尚未保存的密钥只用于连接测试。"))
        layout.addStretch()
        try:
            config = self.settings_manager.load_api_settings()
            self.base_url_input.setText(config.base_url)
            self.model_input.setText(config.model)
            self._timeout_seconds = config.timeout_seconds
        except Exception as exc:
            self._timeout_seconds = 30.0
            self._show_api_error(str(exc))
        self._refresh_credential_status()
        self._connection_fields_revision = 0
        self._test_revision = None
        for field in (self.base_url_input, self.model_input, self.key_input):
            field.textChanged.connect(self._connection_fields_edited)
        self.chat_controller.connection_status_changed.connect(self._connection_status_changed)
        self.chat_controller.error_changed.connect(self._show_api_error)
        self.chat_controller.busy_changed.connect(lambda busy: self.test_connection_button.setEnabled(not busy))
        self.test_connection_button.setEnabled(not self.chat_controller.busy)
        return tab

    def _toggle_key(self, visible: bool) -> None:
        self.key_input.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.show_key_button.setText("隐藏" if visible else "显示")

    def _current_api_settings(self):
        return ApiSettings(
            base_url=self.base_url_input.text().strip(), model=self.model_input.text().strip(),
            timeout_seconds=self._timeout_seconds,
        )

    def _show_api_error(self, message: str) -> None:
        # A credential backend must not accidentally reflect a newly entered key.
        entered_key = self.key_input.text().strip()
        if entered_key:
            message = message.replace(entered_key, "[密钥已隐藏]")
        self.api_error_label.setText(message)
        self.api_error_label.setVisible(bool(message))

    def _refresh_credential_status(self) -> None:
        status = self.settings_manager.credential_status
        if not self.settings_manager.credentials_available:
            status += "\n仍可输入临时密钥测试连接；聊天需在凭据库恢复并保存密钥后使用。"
        self.credential_label.setText(status)

    def _connection_fields_edited(self) -> None:
        self._connection_fields_revision += 1
        self._show_api_error("")
        self.connection_label.setText("配置已编辑，尚未测试当前配置。")

    def _connection_status_changed(self, status: str) -> None:
        if self._test_revision is not None and self._test_revision != self._connection_fields_revision:
            status += "\n当前配置或密钥已修改，此结果对应测试发起时的配置。"
        self.connection_label.setText(status)

    def _save_api(self) -> None:
        self._show_api_error("")
        ordinary_settings_saved = False
        try:
            config = self._current_api_settings()
            key = self.key_input.text().strip()
            self.settings_manager.save_api_settings(config, pending_api_key=key)
            ordinary_settings_saved = True
            if key:
                self.settings_manager.save_api_key(key)
                self.key_input.clear()
                self.show_key_button.setChecked(False)
            self.connection_label.setText("配置已保存。点击「测试连接」确认服务是否可用。")
        except Exception as exc:
            if ordinary_settings_saved:
                self.connection_label.setText("普通配置已保存，密钥未保存。")
                self._show_api_error("普通配置已保存，密钥未保存。\n" + str(exc))
            else:
                self._show_api_error(str(exc))
        self._refresh_credential_status()

    def _test_connection(self) -> None:
        self._show_api_error("")
        if self.chat_controller.busy:
            return
        try:
            config = self._current_api_settings()
            key = self.key_input.text().strip() or self.settings_manager.get_api_key()
            self._test_revision = self._connection_fields_revision
            self.chat_controller.test_connection(config, key)
        except Exception as exc:
            self.connection_label.setText("连接未测试：无法读取当前配置或凭据。")
            self._show_api_error(str(exc))
            self._refresh_credential_status()

    def _clear_key(self) -> None:
        self._show_api_error("")
        # The saved credential changes even when the temporary key field is
        # empty. Invalidate any pending test before its late result arrives.
        self._connection_fields_edited()
        try:
            self.settings_manager.clear_api_key()
            self.key_input.clear()
            self.show_key_button.setChecked(False)
            self.connection_label.setText("系统凭据库中的 API Key 已删除。")
        except Exception as exc:
            self.connection_label.setText("无法确认 API Key 已删除，请检查错误并重新测试连接。")
            self._show_api_error(str(exc))
        self._refresh_credential_status()


class ChatWindow(_HideOnCloseWindow):
    def __init__(self, chat_controller, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("chatWindow")
        self.setWindowTitle("和鲸鱼娘聊天")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(460, 480)
        self.resize(580, 640)
        self.setStyleSheet(PANEL_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 7)
        self.chat_panel = ChatPanel(chat_controller)
        layout.addWidget(self.chat_panel)
