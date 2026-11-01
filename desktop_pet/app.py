"""Command-line startup and visible asset error reporting."""

import argparse
import sys
from pathlib import Path

from .web_rendering import configure_webengine_rendering

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .animation_config import AnimationConfig
from . import APP_REVISION
from .application_controller import ApplicationController
from .config import DEFAULT_IMAGE, PetConfig
from .pet_window import PetImageError
from .settings_manager import SettingsError, SettingsManager
from .single_instance import SingleInstance, SingleInstanceError


def parse_config(argv: list[str] | None = None) -> PetConfig:
    parser = argparse.ArgumentParser(description="AURA 鲸鱼娘 · 基础 2D 桌宠")
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="透明 PNG 路径")
    parser.add_argument("--width", type=int, default=360, help="窗口宽度，默认 360")
    parser.add_argument("--height", type=int, default=460, help="窗口高度，默认 460")
    parser.add_argument("--character-size", type=int, default=340, help="角色最大边长，默认 340")
    parser.add_argument("--bubble-seconds", type=float, default=3.0, help="气泡显示秒数")
    parser.add_argument("--fps", type=int, default=AnimationConfig().fps, help="动画帧率，1–120，默认 30")
    parser.add_argument("--debug-panel", action="store_true", help="启动时同时打开调试面板")
    parser.add_argument("--silent", action="store_true", default=None, help="仅显示托盘图标；托盘不可用时显示桌宠")
    parser.add_argument("--autostart", action="store_true", help="由登录自启动运行；已有实例时不唤醒窗口")
    args = parser.parse_args(argv)
    try:
        return PetConfig(
            image_path=args.image, window_width=args.width, window_height=args.height,
            character_size=args.character_size,
            bubble_duration_ms=round(args.bubble_seconds * 1000),
            animation=AnimationConfig(fps=args.fps),
            show_debug_panel=args.debug_panel,
            silent_start=args.silent,
            autostart=args.autostart,
        )
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))


def main(argv: list[str] | None = None, *, settings: SettingsManager | None = None) -> int:
    # Also cover integrations that invoke this entry point without main.py.
    configure_webengine_rendering()
    config = parse_config(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("AURA Desktop Pet")
    app.setApplicationDisplayName("鲸鱼娘桌宠")
    app.setApplicationVersion(APP_REVISION)
    # Panel windows hide on close. Only closing the pet ends this session.
    app.setQuitOnLastWindowClosed(False)
    instance = None
    session = None
    try:
        settings = settings if settings is not None else SettingsManager()
        instance = SingleInstance(settings.config_path, app)
        activation = ("debug" if config.show_debug_panel else
                      "silent" if config.silent_start is True or config.autostart else "chat")
        if not instance.start(activation):
            return 0
        # No pet, conversation, perception worker or Mem0 service is created
        # until this process has acquired its own settings-file lifetime lock.
        session = ApplicationController(config, settings=settings)
        def activate(command):
            if command == "debug":
                session.show_pet()
                session.open_debug_panel()
            elif command == "chat":
                session.show_pet()
                session.open_chat_window()
        instance.activation_requested.connect(activate)
        app.setWindowIcon(QIcon(str(config.image_path)))
        session.finished.connect(app.quit)
        app.aboutToQuit.connect(session.shutdown)
        session.show()
        if config.show_debug_panel:
            session.open_debug_panel()
        return app.exec()
    except PetImageError as exc:
        print(f"桌宠启动失败：{exc}", file=sys.stderr)
        QMessageBox.critical(None, "桌宠启动失败", str(exc))
        return 2
    except (SettingsError, SingleInstanceError) as exc:
        print(f"桌宠启动失败：{exc}", file=sys.stderr)
        QMessageBox.critical(None, "桌宠启动失败", str(exc))
        return 3
    finally:
        try:
            if session is not None:
                session.shutdown()
        finally:
            if instance is not None:
                instance.release()
