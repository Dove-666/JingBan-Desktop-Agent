"""Command-line startup and visible asset error reporting."""

import argparse
import sys
from pathlib import Path

from .web_rendering import configure_webengine_rendering

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .animation_config import AnimationConfig
from .branding import APP_NAME, LEGACY_APPLICATION_ID
from . import APP_REVISION
from .application_controller import ApplicationController
from .config import DEFAULT_IMAGE, PetConfig
from .pet_window import PetImageError
from .settings_manager import SettingsError, SettingsManager
from .single_instance import SingleInstance, SingleInstanceError
from .runtime_diagnostics import RuntimeDiagnostics
from .runtime_paths import launch_profile_environment
from .ui.theme import ThemeManager


def parse_config(argv: list[str] | None = None) -> PetConfig:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} · 桌面伙伴")
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="透明 PNG 路径")
    parser.add_argument("--width", type=int, default=360, help="窗口宽度，默认 360")
    parser.add_argument("--height", type=int, default=460, help="窗口高度，默认 460")
    parser.add_argument("--character-size", type=int, default=340, help="角色最大边长，默认 340")
    parser.add_argument("--bubble-seconds", type=float, default=3.0, help="气泡显示秒数")
    parser.add_argument("--fps", type=int, default=AnimationConfig().fps, help="动画帧率，1–120，默认 30")
    parser.add_argument("--debug-panel", action="store_true", help="启动时同时打开调试面板")
    parser.add_argument("--silent", action="store_true", default=None, help="仅显示托盘图标；托盘不可用时显示桌宠")
    parser.add_argument("--autostart", action="store_true", help="由登录自启动运行；已有实例时不唤醒窗口")
    parser.add_argument("--profile-dir", type=Path, help="使用独立的配置与聊天记录目录")
    parser.add_argument("--qa-exit-after-ms", type=int, help=argparse.SUPPRESS)
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
            profile_dir=args.profile_dir.expanduser().resolve() if args.profile_dir is not None else None,
            qa_exit_after_ms=args.qa_exit_after_ms,
        )
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))


def main(argv: list[str] | None = None, *, settings: SettingsManager | None = None) -> int:
    # Also cover integrations that invoke this entry point without main.py.
    configure_webengine_rendering()
    config = parse_config(argv)
    with launch_profile_environment(getattr(config, "profile_dir", None)):
        return _run(config, settings)


def _run(config: PetConfig, settings: SettingsManager | None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(LEGACY_APPLICATION_ID)
    app.setApplicationDisplayName(APP_NAME)
    from . import APP_VERSION
    app.setApplicationVersion(APP_VERSION)
    # Panel windows hide on close. Only closing the pet ends this session.
    app.setQuitOnLastWindowClosed(False)
    instance = None
    session = None
    theme = None
    diagnostic = None
    phase = "session"
    def cleanup(action, code):
        try:
            action()
        except BaseException as error:
            if diagnostic is not None:
                diagnostic.record("exception", code=code, stage="cleanup", error=error)
            raise
    try:
        if settings is None:
            profile_dir = getattr(config, "profile_dir", None)
            settings = SettingsManager(profile_dir=profile_dir) if profile_dir is not None else SettingsManager()
        instance = SingleInstance(settings.config_path, app)
        activation = ("debug" if config.show_debug_panel else
                      "silent" if config.silent_start is True or config.autostart else "chat")
        if not instance.start(activation):
            return 0
        try:
            diagnostic = RuntimeDiagnostics(Path(settings.config_path).parent, APP_REVISION)
            diagnostic.install_hooks()
            diagnostic.record("startup")
        except Exception:
            if diagnostic is not None:
                diagnostic.close()
            diagnostic = None
        phase = "theme"
        theme = ThemeManager(settings)
        # No pet, conversation, perception worker or Mem0 service is created
        # until this process has acquired its own settings-file lifetime lock.
        phase = "session"
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
        if diagnostic is not None:
            diagnostic.bind(session.finished, "quit_signal", source="session_finished")
            diagnostic.bind(app.aboutToQuit, "about_to_quit", source="qt_about_to_quit")
        session.finished.connect(app.quit)
        app.aboutToQuit.connect(session.shutdown)
        phase = "show"
        session.show()
        if config.show_debug_panel:
            session.open_debug_panel()
        if getattr(config, "qa_exit_after_ms", None) is not None:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(config.qa_exit_after_ms, session.shutdown)
        phase = "event_loop"
        result = app.exec()
        if diagnostic is not None:
            diagnostic.record("event_loop_return", exit_code=result)
        return result
    except PetImageError as exc:
        if diagnostic is not None:
            diagnostic.record("exception", code="STARTUP_IMAGE_ERROR", stage=phase, error=exc)
        print(f"{APP_NAME}启动失败：{exc}", file=sys.stderr)
        QMessageBox.critical(None, f"{APP_NAME}启动失败", str(exc))
        return 2
    except (SettingsError, SingleInstanceError) as exc:
        if diagnostic is not None:
            diagnostic.record("exception", code="STARTUP_CONFIG_ERROR", stage=phase, error=exc)
        print(f"{APP_NAME}启动失败：{exc}", file=sys.stderr)
        QMessageBox.critical(None, f"{APP_NAME}启动失败", str(exc))
        return 3
    except BaseException as error:
        if diagnostic is not None:
            diagnostic.record("exception", code="MAIN_UNCAUGHT", stage=phase, error=error)
        raise
    finally:
        try:
            try:
                if session is not None:
                    cleanup(session.shutdown, "CLEANUP_SESSION")
            finally:
                if instance is not None:
                    cleanup(instance.release, "CLEANUP_INSTANCE")
                if theme is not None:
                    cleanup(theme.close, "CLEANUP_THEME")
        finally:
            if diagnostic is not None:
                diagnostic.close()
