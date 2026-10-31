"""Small, always reachable desktop controls; no duplicate application state."""

from PySide6.QtCore import QObject
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class DesktopTray(QObject):
    def __init__(self, session, image_path, parent=None):
        super().__init__(parent)
        self.session = session
        self.icon = QSystemTrayIcon(QIcon(str(image_path)), self)
        self.icon.setToolTip("AURA 鲸鱼娘 · 双击打开聊天")
        self.menu = QMenu(session.pet)
        self.chat_action = self.menu.addAction("和鲸鱼娘聊天", session.open_chat_window)
        self.settings_action = self.menu.addAction("打开控制台", session.open_debug_panel)
        if hasattr(session, "open_remote_settings"):
            self.remote_action = self.menu.addAction("手机远程连接", session.open_remote_settings)
        self.memory_action = self.menu.addAction("管理长期记忆", session.open_memory_window)
        self.menu.addSeparator()
        self.visibility_action = self.menu.addAction("暂时隐藏桌宠", session.toggle_pet_visibility)
        self.pause_action = self.menu.addAction("暂停屏幕感知（本次运行）")
        self.pause_action.setCheckable(True)
        self.pause_action.toggled.connect(session.perception.set_paused)
        self.camera_action = None
        if hasattr(session, "camera"):
            self.camera_action = self.menu.addAction("摄像头识别")
            self.camera_action.setCheckable(True)
            self.camera_action.toggled.connect(session.camera.set_enabled)
            self.camera_settings_action = self.menu.addAction("摄像头设置", session.open_camera_settings)
            session.camera.settings_changed.connect(lambda _settings: self.refresh())
        self.menu.addSeparator()
        self.menu.addAction("退出桌宠", session.shutdown)
        self.menu.aboutToShow.connect(self.refresh)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        session.perception.paused_changed.connect(lambda _paused: self.refresh())
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        if self.available:
            self.icon.show()
        self.refresh()

    def refresh(self):
        self.visibility_action.setText("暂时隐藏桌宠" if self.session.pet.isVisible() else "显示桌宠")
        self.pause_action.blockSignals(True)
        self.pause_action.setChecked(self.session.perception.paused)
        self.pause_action.blockSignals(False)
        if self.camera_action is not None:
            self.camera_action.blockSignals(True)
            self.camera_action.setChecked(self.session.camera.settings.enabled)
            self.camera_action.blockSignals(False)
        suffix = "屏幕感知已暂停" if self.session.perception.paused else "双击打开聊天"
        self.icon.setToolTip("AURA 鲸鱼娘 · " + suffix)

    def _activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.session.open_chat_window()

    def shutdown(self):
        self.icon.hide()
        self.menu.close()

