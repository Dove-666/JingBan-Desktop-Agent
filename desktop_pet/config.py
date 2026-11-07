"""Adjust the prototype here, or override sizes using command-line options."""

from dataclasses import dataclass, field
from pathlib import Path

from .animation_config import AnimationConfig
from .runtime_paths import resource_root


PROJECT_ROOT = resource_root()
DEFAULT_IMAGE = PROJECT_ROOT / "assets" / "whale.png"


@dataclass(frozen=True)
class PetConfig:
    image_path: Path = DEFAULT_IMAGE
    window_width: int = 360
    window_height: int = 460
    character_size: int = 340
    bubble_text: str = "你好呀，我来啦！"
    bubble_duration_ms: int = 3000
    animation: AnimationConfig = field(default_factory=AnimationConfig)
    padding: int = 8
    bubble_height: int = 54
    show_debug_panel: bool = False
    silent_start: bool | None = None
    autostart: bool = False
    profile_dir: Path | None = None
    qa_exit_after_ms: int | None = None

    def __post_init__(self) -> None:
        if self.silent_start is not None and type(self.silent_start) is not bool:
            raise ValueError("静默启动设置必须为开启或关闭。")
        if type(self.autostart) is not bool:
            raise ValueError("自动启动标识必须为开启或关闭。")
        if self.profile_dir is not None and not isinstance(self.profile_dir, Path):
            raise ValueError("独立配置目录必须是有效路径。")
        if (self.qa_exit_after_ms is not None
                and (type(self.qa_exit_after_ms) is not int or self.qa_exit_after_ms < 250)):
            raise ValueError("自动退出等待时间至少为 250 毫秒。")
        if self.window_width < 160 or self.window_height < 200:
            raise ValueError("窗口宽度至少为 160，高度至少为 200。")
        if self.character_size < 32:
            raise ValueError("角色大小至少为 32。")
        if self.bubble_duration_ms < 100:
            raise ValueError("气泡持续时间至少为 0.1 秒。")

    @property
    def refresh_fps(self) -> int:
        return self.animation.fps

