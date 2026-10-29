"""Command-line startup and visible asset error reporting."""

import argparse
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .animation_config import AnimationConfig
from .application_controller import ApplicationController
from .config import DEFAULT_IMAGE, PetConfig
from .pet_window import PetImageError


def parse_config(argv: list[str] | None = None) -> PetConfig:
    parser = argparse.ArgumentParser(description="AURA 鲸鱼娘 · 基础 2D 桌宠")
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="透明 PNG 路径")
    parser.add_argument("--width", type=int, default=360, help="窗口宽度，默认 360")
    parser.add_argument("--height", type=int, default=460, help="窗口高度，默认 460")
    parser.add_argument("--character-size", type=int, default=340, help="角色最大边长，默认 340")
    parser.add_argument("--bubble-seconds", type=float, default=3.0, help="气泡显示秒数")
    parser.add_argument("--fps", type=int, default=AnimationConfig().fps, help="动画帧率，1–120，默认 30")
    parser.add_argument("--debug-panel", action="store_true", help="启动时同时打开调试面板")
    args = parser.parse_args(argv)
    try:
        return PetConfig(
            image_path=args.image, window_width=args.width, window_height=args.height,
            character_size=args.character_size,
            bubble_duration_ms=round(args.bubble_seconds * 1000),
            animation=AnimationConfig(fps=args.fps),
            show_debug_panel=args.debug_panel,
        )
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))


def main(argv: list[str] | None = None) -> int:
    config = parse_config(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("AURA Desktop Pet")
    app.setApplicationDisplayName("鲸鱼娘桌宠")
    # Panel windows hide on close. Only closing the pet ends this session.
    app.setQuitOnLastWindowClosed(False)
    try:
        session = ApplicationController(config)
    except PetImageError as exc:
        print(f"桌宠启动失败：{exc}", file=sys.stderr)
        QMessageBox.critical(None, "桌宠启动失败", str(exc))
        return 2
    app.setWindowIcon(QIcon(str(config.image_path)))
    session.finished.connect(app.quit)
    app.aboutToQuit.connect(session.shutdown)
    session.show()
    if config.show_debug_panel:
        session.open_debug_panel()
    return app.exec()
