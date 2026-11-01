"""Independent controls, credential settings, and synchronized chat windows."""

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)

from .. import APP_REVISION
from .assistant_panel import ASSISTANT_STYLE, AssistantChatPanel as ChatPanel
from .styles import PANEL_STYLE
from .settings_controls import SettingsControlsMixin, _hint
from .window_chrome import WindowChrome




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


class DebugPanel(SettingsControlsMixin, _HideOnCloseWindow):
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
        self.revision_label = QLabel(f"日常体验版 {APP_REVISION.rsplit('-', 1)[-1].upper()}")
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



class ChatWindow(_HideOnCloseWindow):
    def __init__(self, chat_controller, parent=None, memory_controller=None, memory_opener=None, settings_opener=None,
                 settings_factory=None) -> None:
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setProperty("auraCustomChrome", True)
        self._restore_maximized = False
        self.setObjectName("chatWindow")
        self.setWindowTitle("和鲸鱼娘聊天")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(420, 420)
        self.resize(1060, 760)
        self.setStyleSheet(PANEL_STYLE + ASSISTANT_STYLE + "\nQWidget#chatWindow { background: transparent; }\n")
        layout = QVBoxLayout(self)
        margin = WindowChrome.FRAME_MARGIN
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(0)
        self._settings_factory = settings_factory
        self.settings_page = None
        self.pages = QStackedWidget(self)
        self.pages.setObjectName("mainPages")
        self.chat_panel = ChatPanel(chat_controller, memory_controller=memory_controller,
                                    memory_opener=memory_opener, settings_opener=settings_opener,
                                    settings_page_opener=self.show_settings if settings_factory is not None else None)
        self.pages.addWidget(self.chat_panel)
        layout.addWidget(self.pages)
        self.chat_panel._native_header_row.setContentsMargins(0, 0, 132, 0)
        self.chrome = WindowChrome(self)
        self.chrome.add_drag_region(self.chat_panel.title_label)

    def paintEvent(self, _event):
        painter = QPainter(self)
        # Clear old corner pixels after a resize or a maximized/normal change.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff"))
        radius = 0 if self.isMaximized() or self.isFullScreen() else WindowChrome.CORNER_RADIUS
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)

    def nativeEvent(self, event_type, message):
        chrome = getattr(self, "chrome", None)
        result = chrome.native_event(event_type, message) if chrome is not None else None
        return result if result is not None else super().nativeEvent(event_type, message)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self._restore_maximized = bool(event.oldState() & Qt.WindowState.WindowMaximized)
            else:
                self._restore_maximized = self.isMaximized()
            if self.layout() is not None:
                margin = 0 if self.isMaximized() or self.isFullScreen() else WindowChrome.FRAME_MARGIN
                self.layout().setContentsMargins(margin, margin, margin, margin)
        super().changeEvent(event)

    def present(self):
        if self.isMinimized():
            if self._restore_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def show_settings(self, section="general"):
        if self._settings_factory is None:
            return False
        if self.settings_page is None:
            self.settings_page = self._settings_factory(self)
            self.settings_page.set_window_chrome(True)
            self.chrome.add_drag_region(self.settings_page.section_title)
            self.chrome.add_drag_region(self.settings_page.compact_header)
            self.settings_page.closed.connect(self.show_chat)
            self.settings_page.conversation_requested.connect(self._open_settings_conversation)
            self.pages.addWidget(self.settings_page)
        self.settings_page.show_section(section)
        self.pages.setCurrentWidget(self.settings_page)
        self.chrome.raise_controls()
        self.setWindowTitle("设置 · 鲸鱼娘")
        return True

    def show_chat(self):
        # Keep the same web view, history, scroll position and per-session drafts.
        self.pages.setCurrentWidget(self.chat_panel)
        self.chrome.raise_controls()
        self.setWindowTitle("和鲸鱼娘聊天")

    def _open_settings_conversation(self, conversation_id):
        if conversation_id:
            self.show_chat()
            self.chat_panel.controller.open_conversation(conversation_id)
