"""Agent permissions; all model requests use the application's shared API."""

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ..agents import AgentSettings, AgentValidationError
from ..settings_manager import SettingsError
from .settings_widgets import SettingsToggle
from .theme import style_widget
from .styles import PANEL_STYLE


class AgentSettingsPanel(QWidget):
    """Edit permissions without another model form, credential store or probe."""

    def __init__(self, settings_manager, chat_controller=None, parent=None, *, embedded=False):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.chat_controller = chat_controller
        self._settings = AgentSettings()
        self._loading = True
        self._dirty = False
        self.setObjectName("agentSettingsPanel")
        if not embedded:
            style_widget(self, PANEL_STYLE)
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
        layout.setContentsMargins(*( (0, 2, 6, 12) if embedded else (16, 12, 16, 18) ))
        layout.setSpacing(14)

        general = QGroupBox("任务权限")
        general_layout = QVBoxLayout(general)
        general_layout.setSpacing(11)
        self.enabled_checkbox = self._checkbox("启用电脑 Agent", "agentEnabled")
        self.tools_checkbox = self._checkbox("允许使用任务工具", "agentToolsEnabled")
        self.computer_checkbox = self._checkbox("允许电脑操作", "agentComputerEnabled")
        for checkbox in (self.enabled_checkbox, self.tools_checkbox, self.computer_checkbox):
            general_layout.addWidget(checkbox)
            checkbox.toggled.connect(self._mark_dirty)
        layout.addWidget(general)
        layout.addStretch()

        self.save_button = QPushButton("保存设置")
        self.save_button.setObjectName("saveAgentSettings")
        self.save_button.clicked.connect(self._save)
        self.status_label = QLabel()
        self.status_label.setObjectName("agentSettingsStatus")
        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        footer = QHBoxLayout()
        footer.addWidget(self.save_button)
        footer.addWidget(self.status_label, 1)
        outer.addLayout(footer)
        outer.addWidget(self.error_label)

        signal = getattr(chat_controller, "agent_settings_changed", None)
        if signal is not None and callable(getattr(signal, "connect", None)):
            signal.connect(self._external_settings_changed)
        self.reload_settings()

    @staticmethod
    def _checkbox(text, name):
        checkbox = SettingsToggle(text)
        checkbox.setObjectName(name)
        checkbox.setAccessibleName(text)
        return checkbox

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
            self._show_error("无法读取电脑 Agent 设置，请重新保存。")
        self.enabled_checkbox.setChecked(self._settings.enabled)
        self.tools_checkbox.setChecked(self._settings.tools_enabled)
        self.computer_checkbox.setChecked(self._settings.computer_enabled)
        self._dirty = False
        self._loading = False
        self.status_label.setText("已保存")

    def _mark_dirty(self, *_args):
        if not self._loading:
            self._dirty = True
            self.status_label.setText("尚未保存")

    def _current_settings(self):
        settings = replace(self._settings,
            enabled=self.enabled_checkbox.isChecked(), tools_enabled=self.tools_checkbox.isChecked(),
            computer_enabled=self.computer_checkbox.isChecked(), timeout_seconds=0.0, max_steps=0)
        settings.validate()
        return settings

    def _save(self):
        self._show_error("")
        try:
            settings = self._current_settings()
            self.settings_manager.save_agent_settings(settings)
            self._settings = settings
            self._dirty = False
            self.status_label.setText("已保存")
        except (AgentValidationError, SettingsError) as exc:
            self._show_error(str(exc))
        except Exception:
            self._show_error("设置未能保存，请检查本地配置后重试。")
        self._notify_settings_changed()

    def _notify_settings_changed(self):
        method = getattr(self.chat_controller, "refresh_agent_settings", None)
        if callable(method):
            try:
                method()
            except Exception:
                # A refresh failure must not undo a completed settings write.
                pass

    def _external_settings_changed(self, *_args):
        if not self._dirty:
            self.reload_settings()

    def _show_error(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
