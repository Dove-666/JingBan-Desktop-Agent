"""Shared, compact pet and tray controls; no duplicate application state."""

from dataclasses import replace
from PySide6.QtCore import QObject
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon
from .ui.styles import MENU_STYLE
from .branding import APP_NAME, application_icon_path
from .ui.theme import style_widget


class DesktopTray(QObject):
    def __init__(self, session, image_path, parent=None):
        super().__init__(parent)
        self.session = session
        self.icon = QSystemTrayIcon(QIcon(str(application_icon_path())), self)
        self.icon.setToolTip(f"{APP_NAME} · 双击打开聊天")
        self.menu = QMenu(session.pet)
        style_widget(self.menu, MENU_STYLE)
        self.settings_action = self.menu.addAction("设置", session.open_settings)
        self.remote_action = self.menu.addAction("远程连接", session.open_remote_settings)
        self.visibility_action = self.menu.addAction("隐藏桌宠", session.toggle_pet_visibility)
        self.screen_action = self.menu.addAction("", self._toggle_screen_perception)
        self.exit_action = self.menu.addAction("退出", session.shutdown)
        self.menu.aboutToShow.connect(self.refresh)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        session.perception.paused_changed.connect(lambda _paused: self.refresh())
        session.perception.settings_changed.connect(lambda _settings: self.refresh())
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        if self.available:
            self.icon.show()
        self.refresh()

    def refresh(self):
        self.visibility_action.setText("隐藏桌宠" if self.session.pet.isVisible() else "显示桌宠")
        self.visibility_action.setEnabled(self.available)
        controller = self.session.perception
        active = controller.settings.enabled and not controller.paused
        self.screen_action.setText("关闭屏幕感知" if active else "开启屏幕感知")
        self.screen_action.setEnabled(not getattr(controller, "_closed", False))

    def _toggle_screen_perception(self):
        controller = self.session.perception
        if getattr(controller, "_closed", False):
            return
        if controller.settings.enabled and not controller.paused:
            # Stop capture immediately even when saving the preference fails.
            controller.revoke_permissions(local=True)
        elif controller.settings.enabled or controller.update_settings(replace(controller.settings, enabled=True)):
            controller.set_paused(False)
        self.refresh()

    def _activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.session.open_chat_window()

    def shutdown(self):
        self.icon.hide()
        self.menu.close()
        self.menu.setEnabled(False)
