"""Embedded, controller-backed settings inside the ordinary chat window."""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from .theme import ThemeManager, current_theme, style_widget, theme_manager
from .settings_controls import SettingsControlsMixin, _hint
from .styles import PANEL_STYLE
from .settings_widgets import SettingsToggle
from .. import APP_VERSION
from ..settings_manager import SettingsError


SETTINGS_STYLE = PANEL_STYLE + """
QWidget#settingsPage, QWidget#settingsContent, QWidget#settingsSectionBody {
    background: #ffffff;
}
QFrame#settingsSidebar { background: #f6f7f9; border-right: 1px solid #eceff3; }
QLabel#settingsSidebarTitle { font-size: 19px; font-weight: 600; color: #263445; padding: 3px 8px; }
QLabel#settingsSectionTitle { font-size: 21px; font-weight: 600; color: #263445; padding-bottom: 6px; }
QPushButton#backToChatButton, QPushButton#compactBackToChatButton {
    text-align: left; background: transparent; border: 1px solid transparent;
    border-radius: 8px; padding: 7px 8px; color: #586778;
}
QPushButton#backToChatButton:hover, QPushButton#compactBackToChatButton:hover { background: #e9edf2; }
QPushButton#backToChatButton:focus, QPushButton#compactBackToChatButton:focus { border-color: #87a8cb; }
QLineEdit#settingsSearch { background: #ffffff; border: 1px solid #e4e8ee;
    border-radius: 8px; padding: 7px 10px; color: #334155; }
QListWidget#settingsNavigation { background: transparent; border: none; outline: none; padding: 0; font-size: 13px; }
QListWidget#settingsNavigation::item { min-height: 22px; padding: 7px 10px;
    border: 1px solid transparent; border-radius: 8px; color: #586778; margin-bottom: 3px; }
QListWidget#settingsNavigation::item:hover { background: #eceff3; }
QListWidget#settingsNavigation::item:selected { background: #e6edf5; color: #2d567c; font-weight: 600; }
QListWidget#settingsNavigation:focus { border: none; outline: none; }
QListWidget#settingsNavigation::item:focus { border-color: #a8b2bf; }
QFrame#generalSettingsCard, QFrame#observationSettingsCard, QFrame#startupSettingsCard, QFrame#appearanceSettingsCard {
    border: 1px solid #e6eaf0; border-radius: 12px; background: #ffffff;
}
QLabel#settingsRowTitle { font-size: 13px; font-weight: 600; color: #344054; }
QLabel#settingsGroupTitle { font-size: 12px; font-weight: 600; color: #667386; }
QLabel#settingsFontValue { min-width: 24px; color: #526477; }
QLabel#hint, QLabel#subtitle, QLabel#settingsSearchEmpty { color: #667386; font-size: 12px; }
QLabel#settingsSearchEmpty { padding: 8px; }
QPushButton, QToolButton { background: #f8fafc; border: 1px solid #e1e6ed;
    border-radius: 7px; padding: 7px 12px; color: #43556a; }
QPushButton:hover, QToolButton:hover { background: #edf2f8; border-color: #bdcfe1; }
QPushButton:pressed, QToolButton:pressed { background: #e3ebf4; }
QPushButton:focus, QToolButton:focus { border: 1px solid #749cc5; }
QPushButton:disabled, QToolButton:disabled { color: #8c98a7; background: #f8fafc; border-color: #edf0f4; }
QPushButton#primaryButton, QPushButton#saveAgentSettings { background: #4d82b8; border-color: #4d82b8; color: #ffffff; }
QPushButton#primaryButton:hover, QPushButton#saveAgentSettings:hover { background: #3d70a4; }
QPushButton#resetChatFontButton, QPushButton#resetBubbleFontButton {
    background: transparent; border-color: transparent; padding: 5px 7px; color: #62768c; }
QPushButton#resetChatFontButton:hover, QPushButton#resetBubbleFontButton:hover { background: #edf2f8; }
QToolButton#settingsDisclosureButton { background: transparent; border: 1px solid transparent;
    padding: 7px 4px; text-align: left; color: #586b80; }
QToolButton#settingsDisclosureButton:hover { background: #f3f6f9; }
QToolButton#settingsDisclosureButton:focus { border-color: #87a8cb; }
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #ffffff; border: 1px solid #dfe5ec; border-radius: 7px; color: #344054;
    selection-background-color: #dceafa; selection-color: #244766; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #87a8cb; }
QComboBox QAbstractItemView { background: #ffffff; border: 1px solid #dfe5ec; color: #344054;
    selection-background-color: #e6edf5; selection-color: #244766; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow { image: url("@settings-chevron@"); width: 12px; height: 8px; }
QLabel#status { background: #f5f7fa; border: none; color: #586b80; padding: 8px; }
QGroupBox { background: #ffffff; border: 1px solid #e6eaf0; border-radius: 10px;
    margin-top: 10px; padding: 14px 14px 12px; color: #344054; }
QGroupBox::title { color: #344054; left: 12px; }
QTabWidget::pane { border-color: #e6eaf0; background: #ffffff; }
QTabBar::tab { background: #f6f8fa; color: #667386; padding: 8px 14px; }
QTabBar::tab:selected { background: #e6edf5; color: #2d567c; }
QSlider::groove:horizontal { background: #e8edf3; height: 4px; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #7fa3c8; border-radius: 2px; }
QSlider::handle:horizontal { background: #ffffff; border: 2px solid #7fa3c8;
    border-radius: 8px; width: 12px; height: 12px; margin: -6px 0; }
QSlider::handle:horizontal:hover { border-color: #4d82b8; }
QSlider::sub-page:horizontal:disabled { background: #ccd7e2; }
QScrollArea, QScrollArea > QWidget > QWidget { background: #ffffff; }
QScrollBar:vertical { background: transparent; width: 6px; }
QScrollBar::handle:vertical { background: #d0d8e2; border-radius: 3px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #acbacb; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QMenu, QToolTip { background: #ffffff; border: 1px solid #dfe5ec; color: #43556a; }
QMenu::item:selected { background: #e6edf5; }
QWidget#settingsPage[auraTheme="dark"], QWidget#settingsContent[auraTheme="dark"] {
    background: #10151d;
}
QWidget#settingsPage[auraTheme="dark"] QWidget#settingsSectionBody {
    background: #10151d;
}
QWidget#settingsPage[auraTheme="dark"] QFrame#settingsSidebar {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #17212e, stop:0.65 #141c28, stop:1 #101822);
    border-right: 1px solid #29384a;
}
QWidget#settingsPage[auraTheme="dark"] QFrame#generalSettingsCard,
QWidget#settingsPage[auraTheme="dark"] QFrame#observationSettingsCard,
QWidget#settingsPage[auraTheme="dark"] QFrame#startupSettingsCard,
QWidget#settingsPage[auraTheme="dark"] QFrame#appearanceSettingsCard {
    background: qlineargradient(x1:0, y1:0, x2:0.3, y2:1, stop:0 #1b2634, stop:1 #151e2a);
    border: 1px solid #2c3b4f;
}
QWidget#settingsPage[auraTheme="dark"] QScrollArea,
QWidget#settingsPage[auraTheme="dark"] QScrollArea > QWidget > QWidget { background: #10151d; }
QWidget#settingsPage[auraTheme="dark"] QGroupBox { background: #18222f; }
QWidget#settingsPage[auraTheme="dark"] QComboBox::down-arrow { image: url("@settings-chevron-dark@"); }
""".replace("@settings-chevron@", (Path(__file__).resolve().parents[2] / "assets/ui/chevron-down.svg").as_posix()).replace(
    "@settings-chevron-dark@", (Path(__file__).resolve().parents[2] / "assets/ui/chevron-down-dark.svg").as_posix())


class SettingsPage(QWidget, SettingsControlsMixin):
    """Reuse existing settings panels without creating another utility window.

    Only the selected section is constructed. Merely opening settings never
    enables an observer, changes preferences or starts a model connection test.
    """

    closed = Signal()
    conversation_requested = Signal(str)

    SECTIONS = (
        ("general", "常规", "启动 开机 自启动 自启 静默 托盘 字体 字号 阅读 聊天 气泡 暂停 观察 图片 存储"),
        ("character", "角色", "角色卡 角色库 保存角色 切换角色 酒馆 SillyTavern PNG JSON 性格 人设 背景 开场白 桌宠 大小 表情 动作 素材 缩放"),
        ("api", "模型与 API", "模型 服务 密钥 连接 Base URL 思考 强度 推理 reasoning effort"),
        ("agent", "电脑 Agent", "Agent agent 工具 电脑 操作 自动 任务 助手 权限"),
        ("perception", "屏幕感知", "观察 隐私 截图 主动 评论"),
        ("camera", "摄像头", "相机 视频 设备 权限 隐私"),
        ("memory", "长期记忆", "Mem0 搜索 偏好 配置 删除"),
        ("remote", "远程连接", "手机 配对 设备 网络"),
    )

    def __init__(self, pet_controller, settings_manager, chat_controller, parent=None,
                 perception_controller=None, camera_controller=None,
                 remote_controller=None, memory_controller=None, startup_manager=None,
                 image_storage_opener=None):
        super().__init__(parent)
        self.pet_controller = pet_controller
        self.settings_manager = settings_manager
        self.chat_controller = chat_controller
        self.perception_controller = perception_controller
        self.camera_controller = camera_controller
        self.remote_controller = remote_controller
        self.memory_controller = memory_controller
        self.startup_manager = startup_manager
        self.image_storage_opener = image_storage_opener
        self._theme_manager = theme_manager()
        self._embedded_preferences = True
        self.perception_panel = None
        self.camera_panel = None
        self.remote_panel = None
        self.memory_panel = None
        self._pages = {}
        self._section_key = ""
        self._items = {}
        self.setObjectName("settingsPage")
        style_widget(self, SETTINGS_STYLE)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("settingsSidebar")
        self.sidebar.setFixedWidth(196)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(14, 8, 14, 16)
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
        self.section_title.setMinimumHeight(38)
        self.section_title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        content_layout.addWidget(self.section_title)
        self.section_stack = QStackedWidget()
        self.section_stack.setObjectName("settingsSections")
        # A section's wide controls must not enlarge the whole chat window.
        # Their own scroll areas handle overflow on unusually small screens.
        self.section_stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        content_layout.addWidget(self.section_stack, 1)
        outer.addWidget(self.content, 1)

        self.navigation.currentItemChanged.connect(self._navigation_changed)
        self.section_combo.currentIndexChanged.connect(self._combo_changed)
        self.search_input.textChanged.connect(self._filter_sections)
        self.show_section("general")
        if self._theme_manager is not None:
            self._theme_manager.theme_changed.connect(self._sync_theme_choice)

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
            return AgentSettingsPanel(self.settings_manager, self.chat_controller, embedded=True)
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
            self.perception_panel = PerceptionPanel(self.perception_controller, embedded=True)
            return self.perception_panel
        if key == "camera" and self.camera_controller is not None:
            from .camera_panel import CameraPanel
            self.camera_panel = CameraPanel(self.camera_controller, embedded=True)
            return self.camera_panel
        if key == "remote" and self.remote_controller is not None:
            from .remote_panel import RemotePanel
            self.remote_panel = RemotePanel(self.remote_controller, embedded=True)
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
        layout.setSpacing(14)
        layout.addWidget(self._make_appearance_controls())
        layout.addWidget(self._make_startup_controls())
        from .image_storage_dialog import ImageStorageUsageRow
        self.image_storage_row = ImageStorageUsageRow(self.chat_controller, self.image_storage_opener)
        layout.addWidget(self.image_storage_row)
        card = QFrame()
        card.setObjectName("generalSettingsCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 12, 18, 12)
        card_layout.setSpacing(10)
        title = QLabel("阅读")
        title.setObjectName("settingsGroupTitle")
        card_layout.addWidget(title)
        self.chat_font_slider, self.chat_font_value = self._font_row(
            card_layout, "聊天字号", "调整聊天正文的阅读大小。",
            getattr(self.chat_controller, "reading_font_size", 16), 16,
            getattr(self.chat_controller, "set_reading_font_size", None),
            "chatReadingFontSlider", "resetChatFontButton",
        )
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        style_widget(divider, "color: #ededed;")
        card_layout.addWidget(divider)
        self.bubble_font_slider, self.bubble_font_value = self._font_row(
            card_layout, "气泡字号", "独立于角色大小，适用于桌宠的评论气泡。",
            getattr(self.pet_controller, "reading_font_points", 14), 14,
            getattr(self.pet_controller, "set_reading_font_points", None),
            "bubbleReadingFontSlider", "resetBubbleFontButton",
        )
        layout.addWidget(card)
        layout.addWidget(self._make_observation_controls())
        self.version_label = _hint(APP_VERSION)
        self.version_label.setObjectName("appVersion")
        self.version_label.setAccessibleName("版本")
        style_widget(self.version_label, "font-size: 12px; color: #667386;")
        layout.addWidget(self.version_label, alignment=Qt.AlignmentFlag.AlignRight)
        # Keep legacy integration attributes out of the visible preferences.
        self._legacy_context_controls = self._make_context_budget_controls()
        self._legacy_context_controls.setParent(body)
        self._legacy_context_controls.hide()
        layout.addStretch()
        chat_signal = getattr(self.chat_controller, "reading_font_changed", None)
        if chat_signal is not None:
            chat_signal.connect(lambda value: self._sync_font(self.chat_font_slider, self.chat_font_value, value))
        bubble_signal = getattr(self.pet_controller, "reading_font_changed", None)
        if bubble_signal is not None:
            bubble_signal.connect(lambda value: self._sync_font(self.bubble_font_slider, self.bubble_font_value, value))
        return scroll

    def _make_appearance_controls(self):
        card = QFrame()
        card.setObjectName("appearanceSettingsCard")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(18, 12, 18, 12)
        label = QLabel("外观")
        label.setObjectName("settingsRowTitle")
        layout.addWidget(label, 1)
        self.theme_combo = QComboBox()
        self.theme_combo.setObjectName("appearanceTheme")
        self.theme_combo.setAccessibleName("界面外观")
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(current_theme()))
        self.theme_combo.setEnabled(callable(getattr(self.settings_manager, "save_theme", None)))
        self.theme_combo.currentIndexChanged.connect(self._set_theme)
        layout.addWidget(self.theme_combo)
        self.theme_status_label = QLabel()
        self.theme_status_label.setObjectName("error")
        self.theme_status_label.setWordWrap(True)
        self.theme_status_label.hide()
        wrapper = QWidget()
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        outer.addWidget(self.theme_status_label)
        return wrapper

    def _set_theme(self, _index):
        requested = self.theme_combo.currentData()
        try:
            manager = theme_manager()
            if manager is None:
                from PySide6.QtWidgets import QApplication
                manager = ThemeManager(self.settings_manager, parent=QApplication.instance())
                manager.theme_changed.connect(self._sync_theme_choice)
                self._theme_manager = manager
                # Apply the user's selection before attaching views, then send
                # each existing view only its final initial theme.
                manager.set_theme(requested)
                # Directly embedded controllers may have built their widgets
                # before the application theme existed. Bind those once too.
                from .chat_panel import ChatPanel
                from ..bubble import SpeechBubble
                for widget in QApplication.allWidgets():
                    if isinstance(widget, (ChatPanel, SpeechBubble)):
                        manager.theme_changed.connect(widget._theme_changed)
                        widget._theme_changed(manager.theme)
                for window in QApplication.topLevelWidgets():
                    chrome = getattr(window, "chrome", None)
                    if chrome is not None:
                        manager.theme_changed.connect(chrome._theme_changed)
            else:
                manager.set_theme(requested)
        except Exception:
            self._sync_theme_choice(current_theme())
            self.theme_status_label.setText("外观未能保存，请检查配置目录权限后重试。")
            self.theme_status_label.show()
            return
        self.theme_status_label.hide()

    def _sync_theme_choice(self, name):
        blocked = self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(name))
        self.theme_combo.blockSignals(blocked)

    def _make_startup_controls(self):
        card = QFrame()
        card.setObjectName("startupSettingsCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(12)
        title = QLabel("启动")
        title.setObjectName("settingsGroupTitle")
        layout.addWidget(title)
        self.autostart_checkbox = SettingsToggle("开机自动启动")
        self.autostart_checkbox.setObjectName("launchAtLoginCheckBox")
        self.autostart_checkbox.setToolTip("登录当前 Windows 账户后自动运行鲸鱼娘。")
        layout.addWidget(self.autostart_checkbox)
        self.silent_start_checkbox = SettingsToggle("静默启动")
        self.silent_start_checkbox.setToolTip("开机自动启动时仅显示右下角托盘图标；手动打开仍正常显示桌宠。")
        self.silent_start_checkbox.setObjectName("silentStartCheckBox")
        layout.addWidget(self.silent_start_checkbox)
        layout.addWidget(_hint("静默启动时仅显示托盘图标，手动打开仍显示桌宠。"))
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
        style_widget(self.startup_status_label, "color: #a12622;" if error else "color: #666666;")
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
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("观察与隐私")
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
        layout.setContentsMargins(18, 12, 18, 12)
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
        row = QHBoxLayout()
        row.setSpacing(10)
        title_label = QLabel(title)
        title_label.setObjectName("settingsRowTitle")
        title_label.setToolTip(description)
        title_label.setMinimumWidth(62)
        row.addWidget(title_label)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setObjectName(slider_name)
        slider.setAccessibleName(title)
        slider.setToolTip(description)
        slider.setRange(12, 22)
        slider.setSingleStep(1)
        slider.setMinimumWidth(60)
        slider.setValue(value if type(value) is int and 12 <= value <= 22 else default)
        value_label = QLabel(f"{slider.value()}")
        value_label.setObjectName("settingsFontValue")
        value_label.setAccessibleName(f"当前{title}")
        reset = QPushButton("重置")
        reset.setObjectName(reset_name)
        reset.setAccessibleName(f"恢复默认{title}")
        reset.setToolTip(f"恢复默认：{default}")
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        row.addWidget(reset)
        layout.addLayout(row)
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
