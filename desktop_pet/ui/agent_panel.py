"""Embedded, on-demand Agent settings; opening this page does not run a backend."""

from dataclasses import asdict, replace
from pathlib import Path
import shutil

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ..agents import AgentSettings, AgentValidationError
from ..agents.settings import DEFAULT_PYTHON, GUI_PROVIDERS
from .settings_controls import _hint
from .styles import PANEL_STYLE


_ROOT = Path(__file__).resolve().parents[2]
_PROVIDER_LABELS = {
    "ui-tars-1.5": "UI-TARS 1.5", "ui-tars-1.0": "UI-TARS 1.0",
    "doubao-1.5-15b": "Doubao 1.5 · 15B", "doubao-1.5-20b": "Doubao 1.5 · 20B",
}


class AgentSettingsPanel(QWidget):
    """Edit public preferences and a write-only GUI credential draft.

    The host SettingsPage may call clear_credential_inputs when changing pages.
    Focus changes inside the form preserve the draft so a Save click can use it;
    leaving the window or hiding the panel clears it and restores password mode.
    """

    def __init__(self, settings_manager, chat_controller=None, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.chat_controller = chat_controller
        self._settings = AgentSettings()
        self._loading = True
        self._dirty = False
        self.setObjectName("agentSettingsPanel")
        self.setStyleSheet(PANEL_STYLE)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("agentSettingsScroll")
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)
        contents = QWidget()
        contents.setMinimumWidth(0)
        contents.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.scroll.setWidget(contents)
        layout = QVBoxLayout(contents)
        layout.setContentsMargins(16, 12, 16, 18)
        layout.setSpacing(14)

        general = QGroupBox("电脑 Agent")
        general_layout = QVBoxLayout(general)
        general_layout.setSpacing(11)
        self.enabled_checkbox = self._checkbox("启用电脑 Agent", "agentEnabled")
        self.tools_checkbox = self._checkbox("允许使用任务工具", "agentToolsEnabled")
        self.computer_checkbox = self._checkbox("允许电脑操作", "agentComputerEnabled")
        for checkbox in (self.enabled_checkbox, self.tools_checkbox, self.computer_checkbox):
            general_layout.addWidget(checkbox)
        general_layout.addWidget(_hint("普通聊天不会启动任务后台。需要完成任务时才调用助手，结束后退出；任务工具沿用「模型与 API」中的聊天模型。"))
        limits = self._form()
        self.duration_status = QLabel("不设上限")
        self.duration_status.setObjectName("agentTaskDuration")
        self.duration_status.setAccessibleName("任务执行时长不设上限")
        self.duration_status.setWordWrap(True)
        self.steps_status = QLabel("不设上限")
        self.steps_status.setObjectName("agentMaxSteps")
        self.steps_status.setAccessibleName("执行步数不设上限")
        self.steps_status.setWordWrap(True)
        limits.addRow("执行时长", self.duration_status)
        limits.addRow("执行步数", self.steps_status)
        general_layout.addLayout(limits)
        general_layout.addWidget(_hint("任务不设总时长和步数上限；可随时点击停止。单次请求或操作异常仍会及时结束。"))
        layout.addWidget(general)

        gui = QGroupBox("电脑操作模型")
        gui_layout = QVBoxLayout(gui)
        gui_layout.setSpacing(11)
        gui_layout.addWidget(_hint("电脑操作需要支持图像输入和所选动作格式的模型。可以与聊天使用同一服务，此处配置和密钥单独保存；未配置时仍可使用任务工具。"))
        form = self._form()
        self.gui_provider_combo = QComboBox()
        self.gui_provider_combo.setObjectName("agentGuiProvider")
        self.gui_provider_combo.setAccessibleName("GUI 模型动作格式")
        self.gui_provider_combo.setMinimumWidth(0)
        self.gui_provider_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.gui_provider_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for provider in GUI_PROVIDERS:
            self.gui_provider_combo.addItem(_PROVIDER_LABELS[provider], provider)
        self.gui_profile_combo = QComboBox()
        self.gui_profile_combo.setObjectName("agentGuiCoordinateProfile")
        self.gui_profile_combo.setAccessibleName("电脑操作模型类型")
        self.gui_profile_combo.addItem("UI-TARS 专用模型", "native")
        self.gui_profile_combo.addItem("通用视觉模型（像素坐标）", "pixel")
        self.gui_profile_combo.setMinimumWidth(0)
        self.gui_profile_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.gui_base_url_input = QLineEdit()
        self.gui_base_url_input.setObjectName("agentGuiBaseUrl")
        self.gui_base_url_input.setAccessibleName("GUI 模型 API 地址")
        self.gui_base_url_input.setPlaceholderText("https://服务商地址/v1")
        self.gui_model_input = QLineEdit()
        self.gui_model_input.setObjectName("agentGuiModel")
        self.gui_model_input.setAccessibleName("GUI 模型名称")
        self.gui_model_input.setPlaceholderText("服务商提供的 GUI Model Name")
        for field in (self.gui_base_url_input, self.gui_model_input):
            field.setMinimumWidth(0)
            field.setMaxLength(2048)
        form.addRow("动作格式", self.gui_provider_combo)
        form.addRow("模型类型", self.gui_profile_combo)
        form.addRow("API 地址（Base URL）", self.gui_base_url_input)
        form.addRow("模型名称（Model Name）", self.gui_model_input)
        key_row = QHBoxLayout()
        self.gui_key_input = QLineEdit()
        self.gui_key_input.setObjectName("agentGuiApiKey")
        self.gui_key_input.setAccessibleName("新的 GUI 模型 API Key")
        self.gui_key_input.setAccessibleDescription("仅输入新密钥。已保存的密钥不会读取到此输入框。")
        self.gui_key_input.setPlaceholderText("输入新密钥；留空保留已保存密钥")
        self.gui_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.gui_key_input.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.gui_key_input.setMaxLength(4096)
        self.gui_key_input.setMinimumWidth(0)
        key_row.addWidget(self.gui_key_input, 1)
        self.show_key_button = QPushButton("显示")
        self.show_key_button.setObjectName("showAgentGuiKey")
        self.show_key_button.setAccessibleName("临时显示新输入的 GUI 密钥")
        self.show_key_button.setCheckable(True)
        self.show_key_button.toggled.connect(self._toggle_key)
        key_row.addWidget(self.show_key_button)
        form.addRow("API Key", key_row)
        gui_layout.addLayout(form)
        key_buttons = QHBoxLayout()
        self.save_key_button = QPushButton("保存 GUI 密钥")
        self.save_key_button.setObjectName("saveAgentGuiKey")
        self.save_key_button.clicked.connect(self._save_gui_key)
        self.clear_key_button = QPushButton("清除已保存密钥")
        self.clear_key_button.setObjectName("clearAgentGuiKey")
        self.clear_key_button.clicked.connect(self._clear_gui_key)
        key_buttons.addWidget(self.save_key_button)
        key_buttons.addWidget(self.clear_key_button)
        key_buttons.addStretch()
        gui_layout.addLayout(key_buttons)
        self.credential_status_label = _hint("")
        self.credential_status_label.setObjectName("agentGuiCredentialStatus")
        gui_layout.addWidget(self.credential_status_label)
        layout.addWidget(gui)

        backend_group = QGroupBox("安装状态")
        backend_layout = QVBoxLayout(backend_group)
        self.backend_status_label = _hint("")
        self.backend_status_label.setObjectName("agentBackendStatus")
        self.backend_status_label.setMinimumWidth(0)
        self.backend_status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        backend_layout.addWidget(self.backend_status_label)
        backend_layout.addWidget(_hint("这里只检查安装文件，不启动后端、不读取屏幕、不测试模型连接。文件齐全后，实际任务仍需有效的模型配置。"))
        self.refresh_backend_button = QPushButton("重新检查安装文件")
        self.refresh_backend_button.setObjectName("refreshAgentBackendStatus")
        self.refresh_backend_button.clicked.connect(self.refresh_backend_status)
        backend_layout.addWidget(self.refresh_backend_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(backend_group)

        self.save_button = QPushButton("保存设置")
        self.save_button.setObjectName("saveAgentSettings")
        self.save_button.clicked.connect(self._save)
        layout.addWidget(self.save_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.status_label = _hint("尚未保存")
        self.status_label.setObjectName("agentSettingsStatus")
        layout.addWidget(self.status_label)
        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        layout.addStretch()

        for field in (self.enabled_checkbox, self.tools_checkbox, self.computer_checkbox):
            field.toggled.connect(self._mark_dirty)
        self.gui_provider_combo.currentIndexChanged.connect(self._mark_dirty)
        self.gui_profile_combo.currentIndexChanged.connect(self._mark_dirty)
        self.gui_base_url_input.textChanged.connect(self._mark_dirty)
        self.gui_model_input.textChanged.connect(self._mark_dirty)
        signal = getattr(chat_controller, "agent_settings_changed", None)
        if signal is not None and callable(getattr(signal, "connect", None)):
            signal.connect(self._external_settings_changed)
        self.reload_settings()

    @staticmethod
    def _checkbox(text, name):
        checkbox = QCheckBox(text)
        checkbox.setObjectName(name)
        checkbox.setAccessibleName(text)
        return checkbox

    @staticmethod
    def _form():
        form = QFormLayout()
        form.setSpacing(9)
        # Stacked labels keep the form usable inside the compact 450 px page.
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return form

    def reload_settings(self):
        self._loading = True
        try:
            settings = self.settings_manager.load_agent_settings()
            if not isinstance(settings, AgentSettings):
                raise ValueError
            settings.validate()
            self._settings = settings
        except Exception:
            self._settings = AgentSettings(enabled=False)
            self._show_error("无法读取电脑 Agent 设置；请检查配置后重新保存。")
        settings = self._settings
        self.enabled_checkbox.setChecked(settings.enabled)
        self.tools_checkbox.setChecked(settings.tools_enabled)
        self.computer_checkbox.setChecked(settings.computer_enabled)
        self.duration_status.setText("不设上限" if settings.timeout_seconds == 0 else "不设上限（保存后生效）")
        self.steps_status.setText("不设上限" if settings.max_steps == 0 else "不设上限（保存后生效）")
        self.gui_provider_combo.setCurrentIndex(self.gui_provider_combo.findData(settings.gui_provider))
        self.gui_profile_combo.setCurrentIndex(self.gui_profile_combo.findData(settings.gui_coordinate_profile))
        self.gui_base_url_input.setText(settings.gui_base_url)
        self.gui_model_input.setText(settings.gui_model)
        self.clear_credential_inputs()
        self._dirty = False
        self._loading = False
        self.status_label.setText("当前保存的设置；普通聊天不会启动任务后台。")
        self.refresh_backend_status()
        self._refresh_credential_status()

    def _mark_dirty(self, *_args):
        if not self._loading:
            self._dirty = True
            self.status_label.setText("尚未保存")

    def _current_settings(self):
        result = replace(self._settings,
            enabled=self.enabled_checkbox.isChecked(), tools_enabled=self.tools_checkbox.isChecked(),
            computer_enabled=self.computer_checkbox.isChecked(), timeout_seconds=0.0,
            max_steps=0, gui_provider=self.gui_provider_combo.currentData(),
            gui_coordinate_profile=self.gui_profile_combo.currentData(),
            gui_base_url=self.gui_base_url_input.text().strip(), gui_model=self.gui_model_input.text().strip())
        result.validate()
        return result

    def _validate_pending(self, settings, key):
        if (not isinstance(key, str) or len(key) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in key)):
            raise AgentValidationError("GUI 密钥不能包含换行或控制字符。")
        if key and any(isinstance(value, str) and key in value for value in asdict(settings).values()):
            raise AgentValidationError("非密钥设置中包含新 API Key，请只在密钥框填写密钥。")

    def _save(self):
        self._show_error("")
        public_saved = False
        try:
            settings = self._current_settings()
            key = self.gui_key_input.text().strip()
            self._validate_pending(settings, key)
            self.settings_manager.save_agent_settings(settings, pending_api_key=key)
            public_saved = True
            self._settings = settings
            self.duration_status.setText("不设上限")
            self.steps_status.setText("不设上限")
            if key:
                self.settings_manager.save_agent_gui_api_key(key)
            self.clear_credential_inputs()
            self._dirty = False
            self.status_label.setText("设置已保存；助手会在需要完成任务时启动。")
            self._notify_settings_changed()
        except AgentValidationError as exc:
            self._show_error(str(exc))
        except Exception:
            self.status_label.setText("设置已保存，GUI 密钥未能保存。" if public_saved else "设置未保存")
            self._show_error("未能保存 GUI 密钥，请检查系统凭据管理器后重试。" if public_saved
                             else "未能保存电脑 Agent 设置，请检查配置和本地权限。")
        self._refresh_credential_status()
        self.refresh_backend_status()

    def _save_gui_key(self):
        self._show_error("")
        try:
            settings = self._current_settings()
            key = self.gui_key_input.text().strip()
            if not key:
                raise AgentValidationError("请先输入新的 GUI 模型 API Key。")
            self._validate_pending(settings, key)
            self.settings_manager.save_agent_gui_api_key(key)
            self.clear_credential_inputs()
            self.status_label.setText("GUI 密钥已保存；模型和任务参数需点击「保存设置」。")
            self._notify_settings_changed()
        except AgentValidationError as exc:
            self._show_error(str(exc))
        except Exception:
            self._show_error("未能保存 GUI 密钥，请检查系统凭据管理器后重试。")
        self._refresh_credential_status()

    def _clear_gui_key(self):
        self._show_error("")
        try:
            self.settings_manager.clear_agent_gui_api_key()
            self.clear_credential_inputs()
            self.status_label.setText("已清除保存的 GUI 密钥；普通聊天和任务工具配置保持可用。")
            self._notify_settings_changed()
        except Exception:
            self._show_error("无法确认 GUI 密钥已清除，请检查系统凭据管理器。")
        self._refresh_credential_status()

    def _notify_settings_changed(self):
        method = getattr(self.chat_controller, "refresh_agent_settings", None)
        if callable(method):
            try:
                method()
            except Exception:
                # Preferences/vault writes already succeeded. A UI refresh is
                # not grounds for changing their successful save result.
                pass

    def _external_settings_changed(self, *_args):
        # A controller refresh is not permission to discard this form's drafts.
        if not self._dirty:
            self.refresh_backend_status()
        self._refresh_credential_status()

    def _refresh_credential_status(self):
        try:
            status = getattr(self.settings_manager, "agent_gui_credential_status", "GUI 密钥仅保存在系统凭据管理器中。")
            if callable(status):
                status = status()
            self.credential_status_label.setText(str(status))
        except Exception:
            self.credential_status_label.setText("无法读取系统凭据状态；未读取或显示已保存密钥。")

    def refresh_backend_status(self, *_args):
        settings = self._settings
        python = Path(settings.python_executable or DEFAULT_PYTHON)
        backend = _ROOT / "agent_backend" / "nanobot"
        sdk = python.parent.parent / "Lib" / "site-packages" / "nanobot" / "agent" / "loop.py"
        tools_ready = python.is_file() and (backend / "worker.py").is_file() and sdk.is_file()
        gui = _ROOT / "agent_backend" / "ui_tars"
        gui_ready = ((gui / "worker.mjs").is_file()
                     and (gui / "node_modules" / "@ui-tars" / "sdk" / "package.json").is_file()
                     and bool(shutil.which("node")))
        self.backend_status_label.setText(
            ("任务后台：安装文件齐全，尚未运行验证。" if tools_ready else "任务后台：安装文件尚不完整。")
            + "\n" + ("电脑操作：安装文件齐全，需要兼容的视觉模型。" if gui_ready else "电脑操作：安装文件尚不完整。"))

    def _toggle_key(self, show):
        self.gui_key_input.setEchoMode(QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password)
        self.show_key_button.setText("隐藏" if show else "显示")

    def clear_credential_inputs(self):
        if hasattr(self, "gui_key_input"):
            self.gui_key_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.gui_key_input.clear()
            self.show_key_button.setChecked(False)

    def _show_error(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def event(self, event):
        if event.type() == QEvent.Type.WindowDeactivate:
            self.clear_credential_inputs()
        return super().event(event)

    def hideEvent(self, event):
        self.clear_credential_inputs()
        super().hideEvent(event)
