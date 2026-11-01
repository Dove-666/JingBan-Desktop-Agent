"""Embedded, controller-backed settings inside the ordinary chat window."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from .settings_controls import SettingsControlsMixin, _hint
from .styles import PANEL_STYLE
from ..settings_manager import SettingsError


SETTINGS_STYLE = PANEL_STYLE + """
QWidget#settingsPage, QWidget#settingsContent, QWidget#settingsSectionBody {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #ffffff, stop:1 #f7fbff);
}
QFrame#settingsSidebar {
    background: qlineargradient(x1:0, y1:0, x2:0.8, y2:1,
        stop:0 #f6fbff, stop:1 #eef5fc);
    border-right: 1px solid #e0ebf5;
}
QLabel#settingsSidebarTitle { font-size: 22px; font-weight: 600; color: #243c55; }
QLabel#settingsSectionTitle { font-size: 22px; font-weight: 600; color: #243c55; }
QPushButton#backToChatButton, QPushButton#compactBackToChatButton {
    text-align: left; background: transparent; border: 1px solid transparent;
    border-radius: 9px; padding: 9px 8px; color: #526e88;
}
QPushButton#backToChatButton:hover, QPushButton#compactBackToChatButton:hover {
    background: #e7f1fb; border-color: #d6e6f5;
}
QPushButton#backToChatButton:focus, QPushButton#compactBackToChatButton:focus {
    border-color: #8fb5d9;
}
QLineEdit#settingsSearch {
    background: #ffffff; border: 1px solid #deebf7; border-radius: 17px;
    padding: 9px 12px; color: #334f6b; selection-background-color: #dcecff;
}
QLineEdit#settingsSearch:hover { border-color: #c2d8ed; }
QLineEdit#settingsSearch:focus { background: #ffffff; border-color: #8fb5d9; }
QListWidget#settingsNavigation { background: transparent; border: none; outline: none;
    padding: 0; font-size: 14px; }
QListWidget#settingsNavigation::item { min-height: 24px; padding: 9px 12px;
    border: 1px solid transparent; border-radius: 10px; color: #506a84;
    margin-bottom: 3px;
}
QListWidget#settingsNavigation::item:hover { background: #edf5fd; border-color: #dce9f6; }
QListWidget#settingsNavigation::item:selected {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #eaf4ff, stop:1 #e3effc);
    border-color: #c5dbf0; color: #285d8e; font-weight: 600;
}
QFrame#generalSettingsCard, QFrame#contextBudgetCard, QFrame#observationSettingsCard,
QFrame#startupSettingsCard {
    border: 1px solid #dde8f3; border-radius: 14px;
    background: qlineargradient(x1:0, y1:0, x2:0.9, y2:1,
        stop:0 #ffffff, stop:1 #fbfdff);
}
QLabel#settingsRowTitle { font-size: 14px; font-weight: 600; color: #2b4661; }
QLabel#settingsFontValue { min-width: 36px; color: #526f8d; }
QLabel#hint, QLabel#subtitle, QLabel#settingsSearchEmpty { color: #60768c; }
QLabel#settingsSearchEmpty { padding: 8px; }
QPushButton, QToolButton {
    background: #fbfdff; border: 1px solid #d7e5f2; border-radius: 9px;
    color: #3a5c7b;
}
QPushButton:hover, QToolButton:hover { background: #edf6ff; border-color: #b8d2eb; }
QPushButton:pressed, QToolButton:pressed { background: #dfedfb; border-color: #a6c6e5; }
QPushButton:focus, QToolButton:focus { border: 1px solid #7ea9d2; }
QPushButton:disabled, QToolButton:disabled {
    background: #f8fbfe; border-color: #e6eef6; color: #8b9eb1;
}
QPushButton#primaryButton { background: #356da7; border-color: #356da7; color: #ffffff; }
QPushButton#primaryButton:hover { background: #285d8e; border-color: #285d8e; }
QPushButton#resetChatFontButton, QPushButton#resetBubbleFontButton {
    background: #f5faff; border-color: #dce9f5; padding: 6px 11px;
}
QPushButton#resetChatFontButton:hover, QPushButton#resetBubbleFontButton:hover {
    background: #e8f3ff; border-color: #bfd8ef;
}
QPushButton#resetChatFontButton:disabled, QPushButton#resetBubbleFontButton:disabled {
    color: #8b9eb1; background: #f8fbfe; border-color: #e6eef6;
}
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #ffffff; border: 1px solid #d8e5f1; border-radius: 9px;
    color: #2c465f; selection-background-color: #dcecff; selection-color: #254f78;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color: #bfd5e9; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #83add4;
}
QComboBox QAbstractItemView {
    background: #ffffff; border: 1px solid #d7e5f2; color: #2c465f;
    selection-background-color: #e6f1fd; selection-color: #285d8e;
}
QComboBox#compactSettingsNavigation { background: #f7fbff; border-color: #dce8f4; }
QLabel#status { background: #f1f7fd; border: 1px solid #e1edf7; color: #4c6883; }
QGroupBox {
    background: #ffffff; border-color: #dde8f3; border-radius: 13px; color: #2b4661;
}
QGroupBox::title { color: #2b4661; }
QTabWidget::pane { border-color: #dde8f3; background: #fbfdff; }
QTabBar::tab { background: #f5f9fd; color: #60768c; }
QTabBar::tab:selected { background: #e7f2fd; color: #285d8e; }
QTabBar::tab:hover:!selected { background: #edf5fd; }
QSlider::groove:horizontal { background: #e4eef8; height: 5px; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #8cb5dd; border-radius: 2px; }
QSlider::handle:horizontal {
    background: #ffffff; border: 2px solid #79a3cd; border-radius: 9px;
    width: 15px; margin: -6px 0;
}
QSlider::handle:horizontal:hover { background: #f0f8ff; border-color: #4c87bf; }
QSlider::sub-page:horizontal:disabled { background: #c7d9e9; }
QScrollArea, QScrollArea > QWidget > QWidget { background: #f9fcff; }
QScrollBar:vertical { background: transparent; width: 7px; }
QScrollBar::handle:vertical { background: #c9dcec; border-radius: 3px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #a7c7e4; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QMenu, QToolTip { background: #fbfdff; border: 1px solid #d5e5f3; color: #365572; }
QMenu::item:selected { background: #e7f2fd; }
"""


class SettingsPage(QWidget, SettingsControlsMixin):
    """Reuse existing settings panels without creating another utility window.

    Only the selected section is constructed. Merely opening settings never
    enables an observer, changes preferences or starts a model connection test.
    """

    closed = Signal()
    conversation_requested = Signal(str)

    SECTIONS = (
        ("general", "常规", "启动 开机 自启动 自启 静默 托盘 字体 字号 阅读 聊天 气泡 上下文 预算 Token 输出 暂停 观察"),
        ("character", "角色", "桌宠 大小 表情 动作 素材 缩放"),
        ("api", "模型与 API", "模型 服务 密钥 连接 Base URL 思考 强度 推理 reasoning effort"),
        ("agent", "电脑 Agent", "Agent agent nanobot UI-TARS 工具 电脑 操作 自动 任务 助手 模型 密钥"),
        ("perception", "屏幕感知", "观察 隐私 截图 主动 评论"),
        ("camera", "摄像头", "相机 视频 设备 权限 隐私"),
        ("memory", "长期记忆", "Mem0 搜索 偏好 配置 删除"),
        ("remote", "远程连接", "手机 配对 设备 网络"),
    )

    def __init__(self, pet_controller, settings_manager, chat_controller, parent=None,
                 perception_controller=None, camera_controller=None,
                 remote_controller=None, memory_controller=None, startup_manager=None):
        super().__init__(parent)
        self.pet_controller = pet_controller
        self.settings_manager = settings_manager
        self.chat_controller = chat_controller
        self.perception_controller = perception_controller
        self.camera_controller = camera_controller
        self.remote_controller = remote_controller
        self.memory_controller = memory_controller
        self.startup_manager = startup_manager
        self.perception_panel = None
        self.camera_panel = None
        self.remote_panel = None
        self.memory_panel = None
        self._pages = {}
        self._section_key = ""
        self._items = {}
        self.setObjectName("settingsPage")
        self.setStyleSheet(SETTINGS_STYLE)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("settingsSidebar")
        self.sidebar.setFixedWidth(216)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(18, 8, 18, 20)
        sidebar_layout.setSpacing(10)
        self.back_button = self._make_back_button("backToChatButton")
        sidebar_layout.addWidget(self.back_button)
        sidebar_title = QLabel("设置")
        sidebar_title.setObjectName("settingsSidebarTitle")
        sidebar_layout.addWidget(sidebar_title)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("settingsSearch")
        self.search_input.setPlaceholderText("搜索分类")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setAccessibleName("搜索设置分类")
        sidebar_layout.addWidget(self.search_input)
        self.navigation = QListWidget()
        self.navigation.setObjectName("settingsNavigation")
        self.navigation.setAccessibleName("设置分类")
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for key, title, keywords in self.SECTIONS:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setData(Qt.ItemDataRole.UserRole + 1, f"{title} {keywords} {key}".casefold())
            self.navigation.addItem(item)
            self._items[key] = item
        sidebar_layout.addWidget(self.navigation, 1)
        self.search_empty_label = QLabel("未找到设置分类")
        self.search_empty_label.setObjectName("settingsSearchEmpty")
        self.search_empty_label.setWordWrap(True)
        self.search_empty_label.hide()
        sidebar_layout.addWidget(self.search_empty_label)
        outer.addWidget(self.sidebar)

        self.content = QWidget()
        self.content.setObjectName("settingsContent")
        self.content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(30, 10, 28, 20)
        content_layout.setSpacing(12)
        self.compact_header = QWidget()
        compact_layout = QHBoxLayout(self.compact_header)
        compact_layout.setContentsMargins(0, 0, 0, 0)
        self.compact_back_button = self._make_back_button("compactBackToChatButton")
        compact_layout.addWidget(self.compact_back_button)
        self.section_combo = QComboBox()
        self.section_combo.setObjectName("compactSettingsNavigation")
        self.section_combo.setAccessibleName("设置分类")
        for key, title, _keywords in self.SECTIONS:
            self.section_combo.addItem(title, key)
        compact_layout.addWidget(self.section_combo, 1)
        self.compact_header.hide()
        content_layout.addWidget(self.compact_header)
        self.section_title = QLabel()
        self.section_title.setObjectName("settingsSectionTitle")
        self.section_title.setTextFormat(Qt.TextFormat.PlainText)
        content_layout.addWidget(self.section_title)
        self.section_stack = QStackedWidget()
        self.section_stack.setObjectName("settingsSections")
        # A section's wide controls must not enlarge the whole chat window.
        # Their own scroll areas handle overflow on unusually small screens.
        self.section_stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        content_layout.addWidget(self.section_stack, 1)
        outer.addWidget(self.content, 1)

        self.navigation.currentItemChanged.connect(self._navigation_changed)
        self.section_combo.currentIndexChanged.connect(self._combo_changed)
        self.search_input.textChanged.connect(self._filter_sections)
        self.show_section("general")

    @property
    def section_key(self):
        return self._section_key

    def _make_back_button(self, name):
        button = QPushButton("←  返回聊天")
        button.setObjectName(name)
        button.setAccessibleName("返回聊天")
        button.clicked.connect(self._return_to_chat)
        return button

    def _return_to_chat(self):
        self._hide_sensitive_inputs()
        self.closed.emit()

    def _navigation_changed(self, current, _previous):
        if current is not None:
            self.show_section(current.data(Qt.ItemDataRole.UserRole))

    def _combo_changed(self, index):
        key = self.section_combo.itemData(index)
        if key:
            self.show_section(key)

    def _filter_sections(self, text):
        needle = text.strip().casefold()
        visible = 0
        for index in range(self.navigation.count()):
            item = self.navigation.item(index)
            matches = not needle or needle in item.data(Qt.ItemDataRole.UserRole + 1)
            item.setHidden(not matches)
            visible += bool(matches)
        self.search_empty_label.setVisible(not visible)

    def show_section(self, key="general"):
        key = str(key or "general")
        if key not in self._items:
            key = "general"
        if key != self._section_key:
            self._hide_sensitive_inputs()
        if key not in self._pages:
            page = self._build_section(key)
            page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
            self._pages[key] = page
            self.section_stack.addWidget(page)
        self._section_key = key
        self.section_title.setText(next(title for section, title, _ in self.SECTIONS if section == key))
        self.section_stack.setCurrentWidget(self._pages[key])
        self._pages[key].show()
        item = self._items[key]
        if item.isHidden():
            self.search_input.clear()
        blocked = self.navigation.blockSignals(True)
        self.navigation.setCurrentItem(item)
        self.navigation.blockSignals(blocked)
        blocked = self.section_combo.blockSignals(True)
        self.section_combo.setCurrentIndex(self.section_combo.findData(key))
        self.section_combo.blockSignals(blocked)
        if key == "memory" and self.memory_panel is not None:
            # Listing saved memories is controller-backed and asynchronous;
            # it does not trigger extraction or a model connection test.
            self.memory_panel._refresh()

    def _build_section(self, key):
        if key == "agent":
            if self.settings_manager is None:
                return self._unavailable_section("电脑 Agent 设置暂不可用。")
            from .agent_panel import AgentSettingsPanel
            return AgentSettingsPanel(self.settings_manager, self.chat_controller)
        if key == "general":
            return self._make_general_section()
        if key == "character":
            if self.pet_controller is None:
                return self._unavailable_section("角色控制暂不可用。")
            return self._make_pet_tab()
        if key == "api":
            if self.settings_manager is None or self.chat_controller is None:
                return self._unavailable_section("模型配置暂不可用。")
            return self._make_api_tab()
        if key == "perception" and self.perception_controller is not None:
            from .perception_panel import PerceptionPanel
            self.perception_panel = PerceptionPanel(self.perception_controller)
            return self.perception_panel
        if key == "camera" and self.camera_controller is not None:
            from .camera_panel import CameraPanel
            self.camera_panel = CameraPanel(self.camera_controller)
            return self.camera_panel
        if key == "remote" and self.remote_controller is not None:
            from .remote_panel import RemotePanel
            self.remote_panel = RemotePanel(self.remote_controller)
            return self.remote_panel
        if key == "memory" and self.memory_controller is not None:
            from .memory_panel import MemoryPanel
            self.memory_panel = MemoryPanel(self.memory_controller, embedded=True)
            self.memory_panel.conversation_requested.connect(self.conversation_requested)
            return self.memory_panel
        return self._unavailable_section("当前运行中尚未接入此功能。")

    def _make_general_section(self):
        scroll = QScrollArea()
        scroll.setObjectName("generalSettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("settingsSectionBody")
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(16)
        layout.addWidget(self._make_startup_controls())
        card = QFrame()
        card.setObjectName("generalSettingsCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(18)
        self.chat_font_slider, self.chat_font_value = self._font_row(
            card_layout, "聊天字号", "调整聊天正文的阅读大小。",
            getattr(self.chat_controller, "reading_font_size", 16), 16,
            getattr(self.chat_controller, "set_reading_font_size", None),
            "chatReadingFontSlider", "resetChatFontButton",
        )
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("color: #ededed;")
        card_layout.addWidget(divider)
        self.bubble_font_slider, self.bubble_font_value = self._font_row(
            card_layout, "气泡字号", "独立于角色大小，适用于桌宠的评论气泡。",
            getattr(self.pet_controller, "reading_font_points", 14), 14,
            getattr(self.pet_controller, "set_reading_font_points", None),
            "bubbleReadingFontSlider", "resetBubbleFontButton",
        )
        layout.addWidget(card)
        layout.addWidget(self._make_observation_controls())
        layout.addWidget(self._make_context_budget_controls())
        layout.addStretch()
        chat_signal = getattr(self.chat_controller, "reading_font_changed", None)
        if chat_signal is not None:
            chat_signal.connect(lambda value: self._sync_font(self.chat_font_slider, self.chat_font_value, value))
        bubble_signal = getattr(self.pet_controller, "reading_font_changed", None)
        if bubble_signal is not None:
            bubble_signal.connect(lambda value: self._sync_font(self.bubble_font_slider, self.bubble_font_value, value))
        return scroll

    def _make_startup_controls(self):
        card = QFrame()
        card.setObjectName("startupSettingsCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        title = QLabel("启动")
        title.setObjectName("settingsRowTitle")
        layout.addWidget(title)
        self.autostart_checkbox = QCheckBox("开机自动启动")
        self.autostart_checkbox.setObjectName("launchAtLoginCheckBox")
        self.autostart_checkbox.setToolTip("登录当前 Windows 账户后自动运行鲸鱼娘。")
        layout.addWidget(self.autostart_checkbox)
        self.silent_start_checkbox = QCheckBox("开机时静默启动")
        self.silent_start_checkbox.setObjectName("silentStartCheckBox")
        layout.addWidget(self.silent_start_checkbox)
        layout.addWidget(_hint("开机自动启动时仅显示右下角托盘图标；手动打开程序会直接显示桌宠。更改在下次自动启动生效。"))
        self.startup_status_label = QLabel()
        self.startup_status_label.setObjectName("startupSettingsStatus")
        self.startup_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.startup_status_label.setWordWrap(True)
        self.startup_status_label.hide()
        layout.addWidget(self.startup_status_label)
        available = self.startup_manager is not None and bool(getattr(self.startup_manager, "available", False))
        self._autostart_enabled = False
        if available:
            try:
                self._autostart_enabled = bool(self.startup_manager.is_enabled())
            except Exception:
                available = False
                self._show_startup_status("无法读取 Windows 自启动状态，请检查账户权限。", error=True)
        else:
            self._show_startup_status("当前启动方式不支持 Windows 开机自启动。", error=True)
        self.autostart_checkbox.setChecked(self._autostart_enabled)
        self.autostart_checkbox.setEnabled(available)
        if not available:
            self.autostart_checkbox.setToolTip(self.startup_status_label.text())
        loader = getattr(self.settings_manager, "load_silent_start", None)
        self._silent_start_enabled = bool(loader()) if callable(loader) else False
        self.silent_start_checkbox.setChecked(self._silent_start_enabled)
        self.silent_start_checkbox.setEnabled(callable(getattr(self.settings_manager, "save_silent_start", None)))
        self.autostart_checkbox.toggled.connect(self._set_autostart)
        self.silent_start_checkbox.toggled.connect(self._set_silent_start)
        return card

    def _show_startup_status(self, message, *, error=False):
        self.startup_status_label.setText(message)
        self.startup_status_label.setStyleSheet("color: #a12622;" if error else "color: #666666;")
        self.startup_status_label.setVisible(bool(message))

    @staticmethod
    def _restore_checked(checkbox, checked):
        blocked = checkbox.blockSignals(True)
        checkbox.setChecked(checked)
        checkbox.blockSignals(blocked)

    def _set_autostart(self, enabled):
        try:
            self.startup_manager.set_enabled(enabled)
        except Exception as exc:
            self._restore_checked(self.autostart_checkbox, self._autostart_enabled)
            self._show_startup_status(str(exc) if isinstance(exc, SettingsError) else
                                      "开机自启动未能保存，请检查 Windows 当前账户权限。", error=True)
            return
        self._autostart_enabled = enabled
        self._show_startup_status("已开启开机自启动。" if enabled else "已关闭开机自启动。")

    def _set_silent_start(self, enabled):
        try:
            self.settings_manager.save_silent_start(enabled)
        except Exception:
            self._restore_checked(self.silent_start_checkbox, self._silent_start_enabled)
            self._show_startup_status("静默启动设置未能保存，请检查配置目录权限。", error=True)
            return
        self._silent_start_enabled = enabled
        self._show_startup_status("已保存，下次开机自动启动生效。")

    def _make_observation_controls(self):
        card = QFrame()
        card.setObjectName("observationSettingsCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("观察")
        title.setObjectName("settingsRowTitle")
        heading.addWidget(title, 1)
        self.pause_all_button = QPushButton("暂停全部观察")
        self.pause_all_button.setObjectName("pauseAllObservations")
        self.pause_all_button.setAccessibleName("暂停屏幕感知和摄像头观察")
        self.pause_all_button.clicked.connect(self._pause_all_observations)
        heading.addWidget(self.pause_all_button)
        layout.addLayout(heading)
        self.visual_status_label = _hint("")
        self.visual_status_label.setObjectName("visualObservationStatus")
        layout.addWidget(self.visual_status_label)
        self.visual_error_label = QLabel()
        self.visual_error_label.setObjectName("visualObservationError")
        self.visual_error_label.setWordWrap(True)
        self.visual_error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.visual_error_label.hide()
        layout.addWidget(self.visual_error_label)
        for _name, controller in self._observation_controllers():
            for signal_name in ("paused_changed", "settings_changed", "status_changed"):
                signal = getattr(controller, signal_name, None)
                if signal is not None:
                    signal.connect(self._refresh_observation_controls)
        self._refresh_observation_controls()
        return card

    def _observation_controllers(self):
        return [(name, controller) for name, controller in (
            ("屏幕感知", self.perception_controller), ("摄像头", self.camera_controller),
        ) if controller is not None]

    @staticmethod
    def _observation_enabled(controller):
        settings = getattr(controller, "settings", None)
        return bool(getattr(settings, "enabled", False))

    def _refresh_observation_controls(self, *_args):
        states = []
        can_pause = False
        for name, controller in self._observation_controllers():
            enabled = self._observation_enabled(controller)
            paused = bool(getattr(controller, "paused", False))
            state = "已关闭" if not enabled else "已暂停" if paused else "运行中"
            states.append(f"{name}：{state}")
            can_pause = can_pause or (enabled and not paused and callable(getattr(controller, "set_paused", None)))
        self.visual_status_label.setText(" · ".join(states) if states else "当前未接入观察功能。")
        self.pause_all_button.setEnabled(can_pause)

    def _pause_all_observations(self):
        failed = []
        self.visual_error_label.clear()
        self.visual_error_label.hide()
        for name, controller in self._observation_controllers():
            if not self._observation_enabled(controller) or getattr(controller, "paused", False):
                continue
            setter = getattr(controller, "set_paused", None)
            if not callable(setter):
                failed.append(name)
                continue
            try:
                setter(True)
                if not getattr(controller, "paused", False):
                    failed.append(name)
            except Exception:
                failed.append(name)
        if failed:
            self.visual_error_label.setText("、".join(failed) + "未能暂停，请进入对应设置检查。")
            self.visual_error_label.show()
        self._refresh_observation_controls()

    def _make_context_budget_controls(self):
        card = QFrame()
        card.setObjectName("contextBudgetCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        title = QLabel("上下文：跟随模型")
        title.setObjectName("settingsRowTitle")
        layout.addWidget(title)
        layout.addWidget(_hint("请求会沿用当前模型的上下文能力，输出长度由模型服务决定，不再手动设置固定 Token 预算或历史条数。"))
        layout.addWidget(_hint("每条回复下方的“本次上下文”可查看实际携带的消息、参考资料和已脱敏文本。模型未声明窗口时，以服务端实际限制为准。"))
        # Retain these attributes for older integrations, without exposing
        # artificial limits or a save action in the user-facing settings.
        self.context_total_input = QSpinBox(card)
        self.context_total_input.setObjectName("contextTotalTokens")
        self.context_total_input.setRange(2048, 262144)
        self.context_reserve_input = QSpinBox(card)
        self.context_reserve_input.setObjectName("contextOutputReserve")
        self.context_reserve_input.setRange(128, 65536)
        self.context_usage_label = _hint(str(getattr(self.chat_controller, "context_usage", "")))
        self.context_usage_label.setObjectName("contextUsage")
        self.context_usage_label.setTextFormat(Qt.TextFormat.PlainText)
        self.context_usage_label.setVisible(bool(self.context_usage_label.text()))
        layout.addWidget(self.context_usage_label)
        self.context_budget_error = QLabel()
        self.context_budget_error.setObjectName("contextBudgetError")
        self.context_budget_error.setWordWrap(True)
        self.context_budget_error.setTextFormat(Qt.TextFormat.PlainText)
        self.context_budget_error.hide()
        layout.addWidget(self.context_budget_error)
        self.context_budget_save_button = QPushButton("跟随模型", card)
        self.context_budget_save_button.setObjectName("saveContextBudget")
        self.context_budget_status = _hint("上下文与输出长度由模型服务管理。")
        self.context_budget_status.setObjectName("contextBudgetStatus")
        layout.addWidget(self.context_budget_status)
        self._budget_form_dirty = False
        self._sync_context_budget_inputs()
        signal = getattr(self.chat_controller, "context_usage_changed", None)
        if signal is not None:
            signal.connect(self._context_usage_changed)
        return card

    def _sync_context_budget_inputs(self):
        budget = getattr(self.chat_controller, "context_budget", None)
        for field in (self.context_total_input, self.context_reserve_input):
            field.setEnabled(False)
            field.hide()
        self.context_budget_save_button.setEnabled(False)
        self.context_budget_save_button.hide()
        if budget is not None:
            # Retain an existing controller-valid value outside the old dialog's
            # usual ranges, so viewing/saving another field cannot replace it.
            for field, value in ((self.context_total_input, budget.total_tokens),
                                 (self.context_reserve_input, budget.output_reserve)):
                if type(value) is int:
                    field.setRange(min(field.minimum(), value), max(field.maximum(), value))
                    blocked = field.blockSignals(True)
                    field.setValue(value)
                    field.blockSignals(blocked)

    def _context_budget_edited(self, *_args):
        # Compatibility hook only. Hidden legacy fields cannot change policy.
        self._sync_context_budget_inputs()

    def _context_usage_changed(self, text):
        self.context_usage_label.setText(str(text))
        self.context_usage_label.setVisible(bool(text))
        self._sync_context_budget_inputs()

    def _save_context_budget(self):
        # Older callers may retain this callback; it must not restore manual
        # request/output limits after the UI has moved to model-managed context.
        self._sync_context_budget_inputs()

    def _font_row(self, layout, title, description, value, default, setter, slider_name, reset_name):
        heading = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("settingsRowTitle")
        heading.addWidget(title_label, 1)
        reset = QPushButton("恢复默认")
        reset.setObjectName(reset_name)
        heading.addWidget(reset)
        layout.addLayout(heading)
        layout.addWidget(_hint(description))
        controls = QHBoxLayout()
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setObjectName(slider_name)
        slider.setAccessibleName(title)
        slider.setRange(12, 22)
        slider.setSingleStep(1)
        slider.setValue(value if type(value) is int and 12 <= value <= 22 else default)
        value_label = QLabel(f"{slider.value()}")
        value_label.setObjectName("settingsFontValue")
        value_label.setAccessibleName(f"当前{title}")
        controls.addWidget(slider, 1)
        controls.addWidget(value_label)
        layout.addLayout(controls)
        slider.setEnabled(callable(setter))
        reset.setEnabled(callable(setter))
        if callable(setter):
            slider.valueChanged.connect(setter)
            reset.clicked.connect(lambda: slider.setValue(default))
        slider.valueChanged.connect(lambda current: value_label.setText(str(current)))
        return slider, value_label

    @staticmethod
    def _sync_font(slider, label, value):
        if type(value) is int:
            blocked = slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(blocked)
            label.setText(str(slider.value()))

    @staticmethod
    def _unavailable_section(message):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.addWidget(_hint(message))
        layout.addStretch()
        return page

    def _hide_sensitive_inputs(self):
        agent_page = self._pages.get("agent")
        clear_agent_credentials = getattr(agent_page, "clear_credential_inputs", None)
        if clear_agent_credentials is not None:
            clear_agent_credentials()
        show_key = getattr(self, "show_key_button", None)
        if show_key is not None:
            show_key.setChecked(False)
        if self.memory_panel is not None:
            self.memory_panel.clear_credential_inputs()

    def hideEvent(self, event):
        self._hide_sensitive_inputs()
        super().hideEvent(event)

    def set_window_chrome(self, enabled):
        self._has_window_chrome = bool(enabled)
        self._update_navigation_layout()

    def _update_navigation_layout(self):
        compact = self.width() < 720
        self.sidebar.setVisible(not compact)
        self.compact_header.setVisible(compact)
        self.content.layout().setContentsMargins(18 if compact else 30, 8 if compact else 10,
                                                18 if compact else 28, 20)
        reserve = 132 if compact and getattr(self, "_has_window_chrome", False) else 0
        self.compact_header.layout().setContentsMargins(0, 0, reserve, 0)
        self.section_title.setContentsMargins(0, 0,
            132 if not compact and getattr(self, "_has_window_chrome", False) else 0, 0)

    def resizeEvent(self, event):
        self._update_navigation_layout()
        super().resizeEvent(event)

    def closeEvent(self, event):
        self._return_to_chat()
        event.ignore()
