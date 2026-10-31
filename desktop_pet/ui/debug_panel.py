"""Independent controls, credential settings, and synchronized chat windows."""

import hashlib

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QSlider, QTabWidget, QVBoxLayout, QWidget,
)

from ..settings_manager import ApiSettings
from .. import APP_REVISION
from .assistant_panel import ASSISTANT_STYLE, AssistantChatPanel as ChatPanel
from .styles import PANEL_STYLE


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class _HideOnCloseWindow(QWidget):
    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Keep utility-window title bars consistent with the white surfaces.
        import sys
        if sys.platform == "win32":
            import ctypes
            use_dark_titlebar = ctypes.c_int(0)
            handle = ctypes.c_void_p(int(self.winId()))
            for attribute in (20, 19):
                result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    handle, attribute, ctypes.byref(use_dark_titlebar), ctypes.sizeof(use_dark_titlebar))
                if result == 0:
                    break

    def closeEvent(self, event) -> None:
        self.hide()
        event.ignore()

    def present(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()


class DebugPanel(_HideOnCloseWindow):
    def __init__(self, pet_controller, settings_manager, chat_controller, parent=None,
                 perception_controller=None, memory_controller=None, memory_opener=None, settings_opener=None,
                 camera_controller=None, remote_controller=None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.pet_controller = pet_controller
        self.settings_manager = settings_manager
        self.chat_controller = chat_controller
        self.setObjectName("debugPanel")
        self.setWindowTitle(f"鲸鱼娘 · 调试面板 · {APP_REVISION}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(620, 580)
        self.resize(730, 740)
        self.setStyleSheet(PANEL_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        title = QLabel("鲸鱼娘控制台")
        title.setObjectName("title")
        heading = QHBoxLayout()
        heading.addWidget(title, 1)
        self.revision_label = QLabel("日常体验版 R8")
        self.revision_label.setObjectName("runtimeRevision")
        self.revision_label.setToolTip(APP_REVISION)
        heading.addWidget(self.revision_label)
        layout.addLayout(heading)
        subtitle = QLabel("调整角色、配置聊天服务，随时回到桌面继续互动。")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("debugTabs")
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._make_pet_tab(), "角色控制")
        self.tabs.addTab(self._make_api_tab(), "API 设置")
        self.chat_panel = ChatPanel(chat_controller, memory_controller=memory_controller,
                                    memory_opener=memory_opener, settings_opener=settings_opener)
        self.tabs.addTab(self.chat_panel, "聊天测试")
        self.perception_panel = None
        if perception_controller is not None:
            from .perception_panel import PerceptionPanel
            self.perception_panel = PerceptionPanel(perception_controller)
            self.tabs.addTab(self.perception_panel, "屏幕感知")
        self.camera_panel = None
        if camera_controller is not None:
            from .camera_panel import CameraPanel
            self.camera_panel = CameraPanel(camera_controller)
            self.tabs.addTab(self.camera_panel, "摄像头")
        self.remote_panel = None
        if remote_controller is not None:
            from .remote_panel import RemotePanel
            self.remote_panel = RemotePanel(remote_controller)
            self.tabs.addTab(self.remote_panel, "远程连接")
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
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("petControlsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        contents = QWidget()
        scroll.setWidget(contents)
        layout = QVBoxLayout(contents)
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
        reading_group = QGroupBox("评论气泡阅读")
        reading_layout = QVBoxLayout(reading_group)
        reading_layout.addWidget(_hint("字号独立于角色大小，调整后自动保存。关联回复可悬停暂停、翻页或点击正文打开对话。"))
        reading_row = QHBoxLayout()
        self.reading_slider = QSlider(Qt.Orientation.Horizontal)
        self.reading_slider.setObjectName("readingFontSlider")
        self.reading_slider.setAccessibleName("评论气泡字号")
        self.reading_slider.setRange(12, 22)
        self.reading_slider.setValue(getattr(self.pet_controller, "reading_font_points", 14))
        self.reading_value = QLabel(f"{self.reading_slider.value()} pt")
        self.reading_value.setObjectName("readingFontValue")
        reading_row.addWidget(self.reading_slider, 1)
        reading_row.addWidget(self.reading_value)
        reading_layout.addLayout(reading_row)
        reading_reset = QPushButton("恢复默认字号")
        reading_reset.setObjectName("resetReadingFont")
        reading_layout.addWidget(reading_reset, alignment=Qt.AlignmentFlag.AlignLeft)
        setter = getattr(self.pet_controller, "set_reading_font_points", None)
        self.reading_slider.setEnabled(callable(setter))
        reading_reset.setEnabled(callable(setter))
        if callable(setter):
            self.reading_slider.valueChanged.connect(setter)
            reading_reset.clicked.connect(lambda: setter(14))
        self.reading_slider.valueChanged.connect(lambda value: self.reading_value.setText(f"{value} pt"))
        signal = getattr(self.pet_controller, "reading_font_changed", None)
        if signal is not None:
            signal.connect(self.reading_slider.setValue)
        layout.addWidget(reading_group)
        expression_group = QGroupBox("表情与动作")
        expression_layout = QVBoxLayout(expression_group)
        expression_layout.setSpacing(12)
        self.auto_expression_checkbox = QCheckBox("API 按语境选择表情")
        self.auto_expression_checkbox.setObjectName("autoExpressionEnabled")
        self.auto_expression_checkbox.setAccessibleName("API按语境选择表情")
        self.auto_expression_checkbox.setChecked(bool(getattr(self.pet_controller, "auto_expression_enabled", True)))
        auto_setter = getattr(self.pet_controller, "set_auto_expression_enabled", None)
        self.auto_expression_checkbox.setEnabled(callable(auto_setter))
        if callable(auto_setter):
            self.auto_expression_checkbox.toggled.connect(auto_setter)
        auto_signal = getattr(self.pet_controller, "auto_expression_changed", None)
        if auto_signal is not None:
            auto_signal.connect(self._auto_expression_changed)
        expression_layout.addWidget(self.auto_expression_checkbox)
        expression_layout.addWidget(_hint("聊天、屏幕和摄像头分析时由 API 选择表情；关闭后可手动选择，不额外发起请求。"))
        self.auto_expression_status = _hint("")
        self.auto_expression_status.setObjectName("autoExpressionStatus")
        expression_layout.addWidget(self.auto_expression_status)
        self._auto_expression_status_changed(getattr(self.pet_controller, "auto_expression_status", ""))
        auto_status_signal = getattr(self.pet_controller, "auto_expression_status_changed", None)
        if auto_status_signal is not None:
            auto_status_signal.connect(self._auto_expression_status_changed)
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
        if hasattr(self.pet_controller, "animation_catalog") and hasattr(self.pet_controller, "static_catalog"):
            layout.addWidget(self._make_asset_controls())
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

    def _make_asset_controls(self) -> QWidget:
        group = QGroupBox("角色素材与序列帧")
        layout = QVBoxLayout(group)
        layout.setSpacing(12)
        self.appearance_combo = QComboBox()
        self.appearance_combo.setObjectName("staticCharacterCombo")
        self.appearance_combo.setAccessibleName("静态角色素材，独立于API表情")
        layout.addWidget(self.appearance_combo)
        self.appearance_status = _hint(getattr(self.pet_controller, "appearance_status", ""))
        self.appearance_status.setObjectName("staticCharacterStatus")
        layout.addWidget(self.appearance_status)
        row = QHBoxLayout()
        self.asset_animation_combo = QComboBox()
        self.asset_animation_combo.setObjectName("assetAnimationCombo")
        self.asset_animation_combo.setAccessibleName("素材动作")
        row.addWidget(self.asset_animation_combo, 1)
        play = QPushButton("播放动作")
        play.setObjectName("playAssetAnimationButton")
        play.clicked.connect(lambda: self.pet_controller.play_asset_animation(self.asset_animation_combo.currentData()))
        row.addWidget(play)
        stop = QPushButton("停止 / 恢复")
        stop.setObjectName("stopAssetAnimationButton")
        stop.clicked.connect(self.pet_controller.stop_asset_animation)
        row.addWidget(stop)
        layout.addLayout(row)
        self.asset_animation_status = _hint("")
        self.asset_animation_status.setObjectName("assetAnimationStatus")
        layout.addWidget(self.asset_animation_status)
        refresh = QPushButton("重新检查素材")
        refresh.setObjectName("refreshCharacterAssetsButton")
        refresh.clicked.connect(self._refresh_character_assets)
        layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(_hint("选择动作后点击播放。待机动作会循环，点击“停止 / 恢复”回到当前表情。拖动时暂停，松开恢复后继续。"))
        self._refresh_character_assets()
        self.appearance_combo.currentIndexChanged.connect(self._select_appearance)
        self.asset_animation_combo.currentIndexChanged.connect(self._selected_asset_status)
        self.pet_controller.appearance_changed.connect(self._appearance_changed)
        self.pet_controller.asset_animation_changed.connect(lambda name, status: self.asset_animation_status.setText(status))
        return group

    def _refresh_character_assets(self):
        selected = self.asset_animation_combo.currentData()
        self.appearance_combo.blockSignals(True)
        self.appearance_combo.clear()
        for code, label, status in self.pet_controller.static_catalog():
            self.appearance_combo.addItem(label, code)
            self.appearance_combo.setItemData(self.appearance_combo.count() - 1, status, Qt.ItemDataRole.ToolTipRole)
        self.appearance_combo.setCurrentIndex(self.appearance_combo.findData(self.pet_controller.appearance))
        self.appearance_combo.blockSignals(False)
        self.asset_animation_combo.blockSignals(True)
        self.asset_animation_combo.clear()
        for code, label, status in self.pet_controller.animation_catalog():
            self.asset_animation_combo.addItem(label, code)
            self.asset_animation_combo.setItemData(self.asset_animation_combo.count() - 1, status, Qt.ItemDataRole.ToolTipRole)
        index = self.asset_animation_combo.findData(selected)
        self.asset_animation_combo.setCurrentIndex(max(0, index))
        self.asset_animation_combo.blockSignals(False)
        self._selected_asset_status()

    def _selected_asset_status(self):
        self.asset_animation_status.setText(self.asset_animation_combo.currentData(Qt.ItemDataRole.ToolTipRole) or "暂无动作素材。")

    def _select_appearance(self):
        code = self.appearance_combo.currentData()
        if code:
            self.pet_controller.set_static_character(code)

    def _appearance_changed(self, code, status):
        self.appearance_combo.blockSignals(True)
        self.appearance_combo.setCurrentIndex(self.appearance_combo.findData(code))
        self.appearance_combo.blockSignals(False)
        self.appearance_status.setText(status)

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

    def _auto_expression_changed(self, enabled: bool) -> None:
        blocked = self.auto_expression_checkbox.blockSignals(True)
        self.auto_expression_checkbox.setChecked(bool(enabled))
        self.auto_expression_checkbox.blockSignals(blocked)

    def _auto_expression_status_changed(self, status: str) -> None:
        self.auto_expression_status.setText(str(status))
        self.auto_expression_status.setVisible(bool(status))

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
        self.base_url_input.setAccessibleName("聊天 API 根地址（Base URL）")
        self.base_url_input.setAccessibleDescription("填写服务商提供的完整 API 根地址。")
        self.base_url_input.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("Base URL", self.base_url_input)
        key_row = QHBoxLayout()
        self.key_input = QLineEdit()
        self.key_input.setObjectName("apiKey")
        self.key_input.setAccessibleName("聊天 API Key")
        self.key_input.setAccessibleDescription("输入新密钥；留空继续使用系统凭据库中已保存的密钥。此输入框不显示已保存的密钥。")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("输入新密钥；留空保留已保存密钥")
        self.key_input.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        key_row.addWidget(self.key_input, 1)
        self.show_key_button = QPushButton("显示")
        self.show_key_button.setObjectName("showApiKeyButton")
        self.show_key_button.setAccessibleName("显示 API Key")
        self.show_key_button.setAccessibleDescription("只临时显示当前输入框中的密钥，不读取或显示已保存的密钥。")
        self.show_key_button.setCheckable(True)
        self.show_key_button.toggled.connect(self._toggle_key)
        key_row.addWidget(self.show_key_button)
        key_label = QLabel("API Key")
        key_label.setBuddy(self.key_input)
        form.addRow(key_label, key_row)
        self.model_input = QLineEdit()
        self.model_input.setObjectName("apiModel")
        self.model_input.setAccessibleName("聊天模型名称")
        self.model_input.setAccessibleDescription("填写服务商提供的可用模型名称。")
        self.model_input.setPlaceholderText("服务商提供的模型名称")
        form.addRow("模型名称", self.model_input)
        layout.addWidget(group)
        layout.addWidget(_hint("填写服务商的 API 根地址和可用模型。连接测试会发送一次真实的短聊天请求，可能产生少量费用。"))
        self.credential_label = QLabel()
        self.credential_label.setObjectName("status")
        self.credential_label.setWordWrap(True)
        self.credential_label.setTextFormat(Qt.TextFormat.PlainText)
        self.credential_label.setAccessibleName("聊天服务密钥保存状态")
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
        self._test_signature = None
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
        self.show_key_button.setAccessibleName("隐藏 API Key" if visible else "显示 API Key")

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
        if self.settings_manager.credentials_available:
            try:
                saved = bool(self.settings_manager.get_api_key())
                status += ("\n聊天密钥已保存；输入框留空会继续使用它。" if saved
                           else "\n尚未保存聊天密钥，请输入并保存后再聊天。")
            except Exception:
                status += "\n无法读取聊天密钥，请检查 Windows 系统凭据库；不会回退到明文保存。"
        else:
            status += "\n仍可输入临时密钥测试连接；聊天需在凭据库恢复并保存密钥后使用。"
        self.credential_label.setText(status)

    def _connection_fields_edited(self) -> None:
        self._connection_fields_revision += 1
        self._show_api_error("")
        self.connection_label.setText("配置已编辑，尚未测试当前配置。")

    def _connection_status_changed(self, status: str) -> None:
        changed = self._test_revision is not None and self._test_revision != self._connection_fields_revision
        if changed and self._test_signature is not None:
            try:
                config = self._current_api_settings()
                key = self.key_input.text().strip() or self.settings_manager.get_api_key()
                changed = self._api_test_signature(config, key) != self._test_signature
            except Exception:
                changed = True
        if changed:
            status += "\n当前配置或密钥已修改，此结果对应测试发起时的配置。"
        self.connection_label.setText(status)

    @staticmethod
    def _api_test_signature(config, key):
        # Saving a tested temporary key clears its field but leaves the actual
        # request configuration unchanged. Keep only a digest for comparison.
        return config, hashlib.sha256(key.encode("utf-8")).digest()

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
            self._test_signature = self._api_test_signature(config, key)
            self.chat_controller.test_connection(config, key)
        except Exception as exc:
            self.connection_label.setText("连接未测试：无法读取当前配置或凭据。")
            self._show_api_error(str(exc))
            self._refresh_credential_status()

    def _clear_key(self) -> None:
        if QMessageBox.question(
            self, "清除已保存的密钥", "从系统凭据库删除聊天 API Key？取消将保留当前密钥。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._show_api_error("")
        # The saved credential changes even when the temporary key field is
        # empty. Invalidate any pending test before its late result arrives.
        self._test_signature = None
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
    def __init__(self, chat_controller, parent=None, memory_controller=None, memory_opener=None, settings_opener=None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("chatWindow")
        self.setWindowTitle("和鲸鱼娘聊天")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(420, 420)
        self.resize(1060, 760)
        self.setStyleSheet(PANEL_STYLE + ASSISTANT_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.chat_panel = ChatPanel(chat_controller, memory_controller=memory_controller,
                                    memory_opener=memory_opener, settings_opener=settings_opener)
        layout.addWidget(self.chat_panel)
