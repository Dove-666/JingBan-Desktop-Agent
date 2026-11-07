"""Shared character and API settings controls for standalone or embedded views.

The host provides ``pet_controller``, ``settings_manager``, and
``chat_controller`` before creating either settings section. This mixin has no
Qt base class, so its widgets and controller bindings can be reused inside the
chat window without opening another top-level window.
"""

import hashlib
import weakref

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from ..settings_manager import ApiSettings
from .theme import style_widget
from .settings_widgets import CollapsibleSection, SettingsToggle
from .reasoning_controls import ReasoningControls


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label



class SettingsControlsMixin:
    """Build and bind existing character and API controls on a QWidget host."""

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
        embedded = getattr(self, "_embedded_preferences", False)
        layout.setContentsMargins(*( (0, 2, 6, 12) if embedded else (18, 18, 18, 18) ))
        layout.setSpacing(15)
        if callable(getattr(getattr(self, "chat_controller", None), "set_character_card", None)):
            from .character_card_panel import CharacterCardPanel
            self.character_card_panel = CharacterCardPanel(self.settings_manager, self.chat_controller)
            layout.addWidget(self.character_card_panel)
        size_group = QGroupBox("角色大小")
        size_layout = QVBoxLayout(size_group)
        size_layout.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(_hint("调整桌宠在桌面上的大小。"), 1)
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
        for value in ((75, 100, 150) if embedded else (50, 75, 100, 125, 150, 200)):
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
        if embedded:
            reset.hide()  # The 100% preset already restores the default size.
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
        if embedded:
            # Reading preferences have a single home on the General page.
            reading_group.hide()
        expression_group = QWidget()
        expression_group.setObjectName("characterBehaviorCard")
        expression_group.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        style_widget(expression_group, """
QWidget#characterBehaviorCard { background: #ffffff; border: 1px solid #e6eaf0; border-radius: 12px; }
QWidget#characterBehaviorCard > QLabel#characterBehaviorTitle { color: #344054; font-size: 14px; font-weight: 600; border: none; }
QWidget#characterBehaviorCard QLabel#characterControlLabel { color: #667386; font-weight: 400; border: none; }
QWidget#characterBehaviorCard QComboBox { padding: 7px 10px; min-height: 24px; }
QWidget#characterBehaviorCard QPushButton { padding: 7px 12px; min-height: 24px; }
QWidget#characterBehaviorCard QPushButton#playAssetAnimationButton { background: #253449; color: white; border-color: #253449; }
QWidget#characterBehaviorCard QPushButton#playAssetAnimationButton:hover { background: #172334; }
QWidget#characterBehaviorCard QPushButton#stopAssetAnimationButton { background: transparent; }
QWidget#characterBehaviorCard QPushButton#stopAssetAnimationButton:hover { background: #edf2f8; }
QWidget#characterBehaviorCard QToolButton#settingsDisclosureButton { background: transparent; border: 1px solid transparent;
    text-align: left; color: #667386; padding: 5px 0; }
QWidget#characterBehaviorCard QToolButton#settingsDisclosureButton:hover { background: #f3f6f9; }
QWidget#characterBehaviorCard QToolButton#settingsDisclosureButton:focus { border-color: #87a8cb; }
""")
        expression_layout = QVBoxLayout(expression_group)
        expression_layout.setContentsMargins(18, 16, 18, 12)
        expression_layout.setSpacing(12)
        title = QLabel("表情与动作")
        title.setObjectName("characterBehaviorTitle")
        expression_layout.addWidget(title)
        self.auto_expression_checkbox = SettingsToggle("自动切换表情")
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
        self.auto_expression_checkbox.setToolTip("根据聊天和观察内容切换表情；关闭后可手动选择。")
        self.auto_expression_status = _hint("")
        self.auto_expression_status.setObjectName("autoExpressionStatus")
        self.auto_expression_status.setParent(expression_group)
        self._auto_expression_status_changed(getattr(self.pet_controller, "auto_expression_status", ""))
        auto_status_signal = getattr(self.pet_controller, "auto_expression_status_changed", None)
        if auto_status_signal is not None:
            auto_status_signal.connect(self._auto_expression_status_changed)
        selectors = QFormLayout()
        selectors.setContentsMargins(0, 0, 0, 0)
        selectors.setHorizontalSpacing(16)
        selectors.setVerticalSpacing(12)
        selectors.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        selectors.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        selectors.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        row_widget = QWidget()
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.expression_combo = QComboBox()
        self.expression_combo.setMinimumWidth(0)
        self.expression_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.expression_combo.setObjectName("expressionCombo")
        self.expression_combo.setAccessibleName("角色表情")
        for code, label, status in self.pet_controller.expression_catalog():
            self.expression_combo.addItem(label, code)
            self.expression_combo.setItemData(self.expression_combo.count() - 1, status, Qt.ItemDataRole.ToolTipRole)
        row.addWidget(self.expression_combo, 1)
        test = QPushButton("预览")
        test.setObjectName("testExpressionButton")
        test.setAccessibleName("预览当前表情")
        test.setToolTip("在桌宠上预览当前表情")
        test.clicked.connect(self.pet_controller.test_expression)
        row.addWidget(test)
        label = QLabel("表情")
        label.setObjectName("characterControlLabel")
        label.setBuddy(self.expression_combo)
        selectors.addRow(label, row_widget)
        expression_layout.addLayout(selectors)
        self.expression_status = QLabel()
        self.expression_status.setObjectName("status")
        self.expression_status.setTextFormat(Qt.TextFormat.PlainText)
        self.expression_status.setWordWrap(True)
        details = CollapsibleSection("更多")
        details.setObjectName("characterBehaviorDetails")
        details_layout = QVBoxLayout(details.content)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.addWidget(self.auto_expression_status)
        details_layout.addWidget(self.expression_status)
        if hasattr(self.pet_controller, "animation_catalog"):
            self._make_asset_controls(selectors, details_layout)
            focus_order = [self.auto_expression_checkbox, self.expression_combo, test,
                           self.asset_animation_combo,
                           self.asset_animation_play_button, self.asset_animation_stop_button,
                           details.button]
            for before, after in zip(focus_order, focus_order[1:]):
                QWidget.setTabOrder(before, after)
        expression_layout.addWidget(details)
        layout.addWidget(expression_group)
        self.pet_error_label = QLabel()
        self.pet_error_label.setObjectName("error")
        self.pet_error_label.setAccessibleName("角色控制错误")
        self.pet_error_label.setWordWrap(True)
        self.pet_error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.pet_error_label.hide()
        layout.addWidget(self.pet_error_label)
        if hasattr(self, "asset_animation_combo"):
            self._update_asset_animation_buttons()
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

    def _make_asset_controls(self, selectors: QFormLayout, details_layout: QVBoxLayout) -> None:
        row_widget = QWidget()
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.asset_animation_combo = QComboBox()
        self.asset_animation_combo.setMinimumWidth(0)
        self.asset_animation_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.asset_animation_combo.setObjectName("assetAnimationCombo")
        self.asset_animation_combo.setAccessibleName("待机动作")
        row.addWidget(self.asset_animation_combo, 1)
        play = QPushButton("播放")
        play.setObjectName("playAssetAnimationButton")
        play.setAccessibleName("播放所选待机动作")
        play.setMinimumWidth(68)
        self.asset_animation_play_button = play
        play.clicked.connect(lambda: self.pet_controller.play_asset_animation(self.asset_animation_combo.currentData()))
        row.addWidget(play)
        stop = QPushButton("停止")
        stop.setObjectName("stopAssetAnimationButton")
        stop.setAccessibleName("停止动作并恢复角色")
        stop.setMinimumWidth(68)
        self.asset_animation_stop_button = stop
        stop.setToolTip("结束播放，恢复原来的角色外观")
        stop.clicked.connect(self.pet_controller.stop_asset_animation)
        row.addWidget(stop)
        label = QLabel("待机动作")
        label.setObjectName("characterControlLabel")
        label.setBuddy(self.asset_animation_combo)
        label.setToolTip("闲着时随机播放；也可以在这里手动预览")
        selectors.addRow(label, row_widget)
        self.asset_animation_status = _hint("")
        self.asset_animation_status.setObjectName("assetAnimationStatus")
        details_layout.addWidget(self.asset_animation_status)
        refresh = QPushButton("重新检查素材")
        refresh.setObjectName("refreshCharacterAssetsButton")
        refresh.clicked.connect(self._refresh_character_assets)
        details_layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        self._refresh_character_assets()
        self.asset_animation_combo.currentIndexChanged.connect(self._selected_asset_status)
        self.pet_controller.asset_animation_changed.connect(self._asset_animation_changed)

    def _update_asset_animation_buttons(self):
        player = getattr(self.pet_controller, "asset_player", None)
        if player is not None:
            loading = bool(player.loading)
            self.asset_animation_play_button.setText("加载…" if loading else "播放")
            self.asset_animation_play_button.setEnabled(not loading)
            self.asset_animation_stop_button.setEnabled(bool(player.busy))

    def _asset_animation_changed(self, name, status):
        self.asset_animation_status.setText(status)
        self._update_asset_animation_buttons()
        player = getattr(self.pet_controller, "asset_player", None)
        if player is not None and hasattr(self, "pet_error_label"):
            # A failed play request must remain visible with diagnostics closed.
            self._show_pet_error(str(status) if name and not player.busy else "")

    def _refresh_character_assets(self):
        selected = self.asset_animation_combo.currentData()
        self.asset_animation_combo.blockSignals(True)
        self.asset_animation_combo.clear()
        for code, label, status in self.pet_controller.animation_catalog():
            # The row already says "待机动作"; retain stable action IDs and
            # omit repetitive category prefixes from this local selector only.
            display = label.partition(" · ")[2] or label
            self.asset_animation_combo.addItem(display, code)
            self.asset_animation_combo.setItemData(self.asset_animation_combo.count() - 1, status, Qt.ItemDataRole.ToolTipRole)
        index = self.asset_animation_combo.findData(selected)
        self.asset_animation_combo.setCurrentIndex(max(0, index))
        self.asset_animation_combo.blockSignals(False)
        self._selected_asset_status()

    def _selected_asset_status(self):
        self.asset_animation_status.setText(self.asset_animation_combo.currentData(Qt.ItemDataRole.ToolTipRole) or "暂无动作素材。")

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
        tab.setObjectName("apiSettingsTab")
        style_widget(tab, """
QWidget#apiSettingsTab QPushButton#saveApiButton {
    background: #356da7; border: 1px solid #356da7; color: white; font-weight: 600;
}
QWidget#apiSettingsTab QPushButton#saveApiButton:hover { background: #285d8e; border-color: #285d8e; }
QWidget#apiSettingsTab QPushButton#saveApiButton:disabled {
    background: #e6eef6; border-color: #e6eef6; color: #71859b;
}
QWidget#apiSettingsTab QPushButton#clearApiKeyButton {
    background: transparent; border-color: transparent; color: #9b493e;
}
QWidget#apiSettingsTab QPushButton#clearApiKeyButton:hover {
    background: #fff2ef; border-color: #f0d3ca;
}
QWidget#apiSettingsTab QPushButton#clearApiKeyButton:focus { border-color: #9b493e; }
QWidget#apiSettingsTab QToolButton#apiAdvancedButton {
    background: transparent; border: 1px solid transparent; text-align: left; padding: 9px 4px;
}
QWidget#apiSettingsTab QToolButton#apiAdvancedButton:hover { background: #edf5fd; }
QWidget#apiSettingsTab QToolButton#apiAdvancedButton:focus { border-color: #83add4; }
""")
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        contents = QWidget()
        scroll.setWidget(contents)
        layout = QVBoxLayout(contents)
        embedded = getattr(self, "_embedded_preferences", False)
        layout.setContentsMargins(*( (0, 2, 6, 12) if embedded else (18, 18, 18, 18) ))
        layout.setSpacing(14)
        group = QGroupBox("全局模型")
        form = QFormLayout(group)
        form.setSpacing(13)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        if embedded:
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.base_url_input = QLineEdit()
        self.base_url_input.setObjectName("apiBaseUrl")
        self.base_url_input.setAccessibleName("全局 API 根地址（Base URL）")
        self.base_url_input.setAccessibleDescription("填写服务商提供的完整 API 根地址。")
        self.base_url_input.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("Base URL", self.base_url_input)
        key_row = QHBoxLayout()
        self.key_input = QLineEdit()
        self.key_input.setObjectName("apiKey")
        self.key_input.setAccessibleName("全局 API Key")
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
        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setObjectName("apiModelCombo")
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.setMaxVisibleItems(12)
        self.model_combo.setMinimumContentsLength(10)
        self.model_combo.setMinimumWidth(0)
        self.model_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model_combo.setAccessibleName("聊天模型选择")
        self.model_combo.setAccessibleDescription("从获取的模型列表中选择，或直接输入模型名称。")
        self.model_input = self.model_combo.lineEdit()
        self.model_input.setObjectName("apiModel")
        self.model_input.setAccessibleName("聊天模型名称")
        self.model_input.setAccessibleDescription("从列表选择或手动填写服务商提供的模型名称；输入文字可筛选已获取的模型。")
        self.model_input.setPlaceholderText("输入或选择模型")
        completer = self.model_combo.completer()
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        model_row.addWidget(self.model_combo, 1)
        self.fetch_models_button = QPushButton("获取模型" if embedded else "获取模型列表")
        self.fetch_models_button.setObjectName("fetchModelsButton")
        self.fetch_models_button.setAccessibleDescription("使用当前地址和密钥查询模型列表；不发送聊天，不保存配置。")
        self.fetch_models_button.clicked.connect(self._fetch_models)
        model_row.addWidget(self.fetch_models_button)
        model_label = QLabel("模型名称")
        model_label.setBuddy(self.model_input)
        form.addRow(model_label, model_row)
        self.models_status_label = QLabel()
        self.models_status_label.setObjectName("modelListStatus")
        self.models_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.models_status_label.setWordWrap(True)
        self.models_status_label.setAccessibleName("模型列表获取状态")
        self.models_status_label.hide()
        form.addRow("", self.models_status_label)
        self._models_request_token = None
        self._models_loaded_source = None
        try:
            reasoning_config = self.settings_manager.load_api_settings()
        except Exception:
            reasoning_config = ApiSettings()
        self.reasoning_controls = ReasoningControls(self.settings_manager, reasoning_config)
        self.reasoning_effort_combo = self.reasoning_controls.value_combo
        layout.addWidget(group)
        self.api_advanced_button = QToolButton()
        self.api_advanced_button.setObjectName("apiAdvancedButton")
        self.api_advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.api_advanced_button.setArrowType(Qt.ArrowType.RightArrow)
        self.api_advanced_button.setCheckable(True)
        self.api_advanced_button.setMinimumWidth(0)
        self.api_advanced_button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.api_advanced_button.setAccessibleName("展开高级模型设置")
        layout.addWidget(self.api_advanced_button)
        self.api_advanced_content = QWidget()
        advanced_layout = QVBoxLayout(self.api_advanced_content)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.addWidget(self.reasoning_controls)
        self.api_advanced_content.hide()
        layout.addWidget(self.api_advanced_content)
        self.api_advanced_button.toggled.connect(self._toggle_api_advanced)
        self.reasoning_controls.changed.connect(self._refresh_api_advanced_label)
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
        self.test_connection_button.setToolTip("测试当前填写的配置，不保存；可能产生少量费用。")
        self.test_connection_button.clicked.connect(self._test_connection)
        buttons.addWidget(self.test_connection_button)
        buttons.addStretch()
        self.clear_key_button = QPushButton("清除密钥" if embedded else "清除 API Key")
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
        layout.addStretch()
        try:
            config = self.settings_manager.load_api_settings()
            self.base_url_input.setText(config.base_url)
            self.model_input.setText(config.model)
            self._timeout_seconds = config.timeout_seconds
            self.reasoning_controls.set_identity(config.base_url, config.model, initial=config, refresh=True)
        except Exception as exc:
            self._timeout_seconds = 30.0
            self._show_api_error(str(exc))
        self._refresh_credential_status()
        self._refresh_api_advanced_label()
        self._connection_fields_revision = 0
        self._test_revision = None
        self._test_signature = None
        for field in (self.base_url_input, self.model_input, self.key_input):
            field.textChanged.connect(self._connection_fields_edited)
        for field in (self.base_url_input, self.key_input):
            field.textChanged.connect(self._models_source_edited)
        self.reasoning_controls.changed.connect(self._connection_fields_edited)
        for field in (self.base_url_input, self.model_input):
            field.textChanged.connect(self._reasoning_identity_changed)
        self.chat_controller.connection_status_changed.connect(self._connection_status_changed)
        self.chat_controller.error_changed.connect(self._show_api_error)
        self.chat_controller.busy_changed.connect(lambda busy: self.test_connection_button.setEnabled(not busy))
        self.test_connection_button.setEnabled(not self.chat_controller.busy)
        return tab

    def _toggle_api_advanced(self, expanded):
        self.api_advanced_content.setVisible(expanded)
        self.api_advanced_button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.api_advanced_button.setAccessibleName("收起高级模型设置" if expanded else "展开高级模型设置")

    def _refresh_api_advanced_label(self, _index=None):
        value = self.reasoning_effort_combo.currentText()
        self.api_advanced_button.setText("高级设置 · 思考：" + (value if len(value) <= 18 else "自定义值"))
        self.api_advanced_button.setToolTip(value)

    def _reasoning_identity_changed(self):
        self._cache_current_reasoning_metadata()
        self.reasoning_controls.set_identity(self.base_url_input.text(), self.model_input.text())
        self._refresh_api_advanced_label()

    def _cache_current_reasoning_metadata(self):
        model = self.model_input.text().strip()
        metadata = getattr(self, "_reasoning_model_metadata", {}).get(model)
        saver = getattr(self.settings_manager, "save_reasoning_metadata", None)
        if (callable(saver) and isinstance(metadata, dict) and
                metadata.get("base_url", "").rstrip("/") == self.base_url_input.text().strip().rstrip("/")):
            try:
                saver(metadata, base_url=self.base_url_input.text().strip(), model=model)
            except (ValueError, TypeError):
                pass

    def _models_source_signature(self):
        key = self.key_input.text().strip() or self.settings_manager.get_api_key()
        return (self.base_url_input.text().strip().rstrip("/"),
                hashlib.sha256(key.encode("utf-8")).digest())

    def _replace_model_choices(self, models):
        # Refresh choices without changing a manually entered/selected model,
        # invalidating its connection test, or accidentally choosing the first ID.
        text = self.model_input.text()
        with QSignalBlocker(self.model_combo), QSignalBlocker(self.model_input):
            self.model_combo.clear()
            self.model_combo.addItems(models)
            self.model_combo.setCurrentIndex(-1)
            self.model_input.setText(text)

    def _models_source_edited(self):
        if self._models_loaded_source is None:
            return
        try:
            unchanged = self._models_source_signature() == self._models_loaded_source
        except Exception:
            unchanged = False
        if not unchanged:
            self._replace_model_choices([])
            self._models_loaded_source = None
            self._reasoning_model_metadata = {}
            self.models_status_label.setText("地址或密钥已变更，请重新获取模型列表。")
            self.models_status_label.show()

    def _fetch_models(self):
        if self._models_request_token is not None:
            return
        try:
            config = self._current_api_settings()
            config.validate(require_model=False)
        except Exception:
            self.models_status_label.setText("请先填写有效的 API Base URL。模型名称可以留空。")
            self.models_status_label.show()
            return
        try:
            key = self.key_input.text().strip() or self.settings_manager.get_api_key()
        except Exception:
            self.models_status_label.setText("无法读取已保存的密钥，请检查系统凭据库或输入临时 API Key。")
            self.models_status_label.show()
            return
        if not key:
            self.models_status_label.setText("请先输入 API Key，或保存密钥后再获取模型列表。")
            self.models_status_label.show()
            return
        request = getattr(self.chat_controller, "fetch_models", None)
        if not callable(request):
            self.models_status_label.setText("当前服务无法获取模型列表，仍可手动填写模型名称。")
            self.models_status_label.show()
            return
        token = object()
        signature = (config.base_url.rstrip("/"), hashlib.sha256(key.encode("utf-8")).digest())
        self._models_request_token = token
        self.fetch_models_button.setEnabled(False)
        self.fetch_models_button.setText("获取中…")
        self.models_status_label.setText("正在获取模型列表…")
        self.models_status_label.show()
        owner = weakref.ref(self)

        def finished(models, error):
            panel = owner()
            if panel is not None and isValid(panel):
                panel._models_finished(token, signature, models, error)

        if not request(config, key, finished):
            self._models_finished(token, signature, [], "暂时无法获取，请稍后重试；也可手动填写模型名称。")

    def _models_finished(self, token, signature, models, error):
        if token is not self._models_request_token:
            return
        self._models_request_token = None
        self.fetch_models_button.setEnabled(True)
        self.fetch_models_button.setText("获取模型" if getattr(self, "_embedded_preferences", False) else "获取模型列表")
        try:
            current = self._models_source_signature() == signature
        except Exception:
            current = False
        if not current:
            self.models_status_label.setText("地址或密钥已变更，已忽略旧列表。请重新获取。")
        elif error:
            self.models_status_label.setText(error)
        else:
            self._replace_model_choices(models)
            self._models_loaded_source = signature
            self._reasoning_model_metadata = getattr(models, "reasoning_metadata", {})
            self._cache_current_reasoning_metadata()
            self.reasoning_controls.set_identity(self.base_url_input.text(), self.model_input.text(), refresh=True)
            self._refresh_api_advanced_label()
            self.models_status_label.setText(f"已获取 {len(models)} 个模型，可下拉选择或输入筛选；仍支持手动填写。")
        self.models_status_label.show()

    def _toggle_key(self, visible: bool) -> None:
        self.key_input.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.show_key_button.setText("隐藏" if visible else "显示")
        self.show_key_button.setAccessibleName("隐藏 API Key" if visible else "显示 API Key")

    def _current_api_settings(self):
        return self.reasoning_controls.api_settings(self.base_url_input.text().strip(),
            self.model_input.text().strip(), self._timeout_seconds)

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
        if getattr(self, "_embedded_preferences", False):
            # Keep detailed vault diagnostics available without a second banner.
            self.credential_label.setToolTip(status)
            self.credential_label.setText(status.split("\n", 1)[-1] if "\n" in status else status)

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
        # A QSettings sync failure does not prove its in-memory values stayed
        # unchanged. Publish the actual current policy even after a failed save.
        refresh = getattr(self.chat_controller, "refresh_agent_queue_policy", None)
        if callable(refresh):
            refresh()

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
        self._models_source_edited()
        refresh = getattr(self.chat_controller, "refresh_agent_queue_policy", None)
        if callable(refresh):
            refresh()
