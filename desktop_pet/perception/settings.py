"""Non-secret, explicitly opt-in preferences for screen perception."""

from dataclasses import dataclass
import math


DEFAULT_PRIVACY_KEYWORDS = (
    "password", "passcode", "verification code", "one-time code", "otp",
    "sign in", "login", "bank", "payment", "private", "incognito",
    "密码", "口令", "验证码", "登录", "银行", "支付", "私密", "隐私",
)
DEFAULT_EXCLUDED_APPS = (
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe",
    "lastpass.exe", "authy desktop.exe", "credentialuibroker.exe",
)
COMMENTARY_MODES = frozenset({"quiet", "companion"})


@dataclass(frozen=True)
class PerceptionSettings:
    enabled: bool = False
    allow_remote: bool = False
    observation_interval_seconds: int = 10
    analysis_interval_seconds: int = 60
    change_threshold: float = 0.08
    speech_cooldown_seconds: int = 180
    max_speeches_per_hour: int = 6
    commentary_mode: str = "companion"
    idle_pause_seconds: int = 600
    privacy_keywords: tuple[str, ...] = DEFAULT_PRIVACY_KEYWORDS
    excluded_apps: tuple[str, ...] = DEFAULT_EXCLUDED_APPS

    def validate(self) -> None:
        if not isinstance(self.commentary_mode, str) or self.commentary_mode not in COMMENTARY_MODES:
            raise ValueError("主动评论模式必须为 quiet 或 companion。")
        if not isinstance(self.enabled, bool) or not isinstance(self.allow_remote, bool):
            raise ValueError("感知启用与远程分析选项必须为布尔值。")
        integer_ranges = (
            ("观察间隔", self.observation_interval_seconds, 5, 30),
            ("分析间隔", self.analysis_interval_seconds, 30, 600),
            ("发言冷却时间", self.speech_cooldown_seconds, 30, 1800),
            ("每小时主动发言次数", self.max_speeches_per_hour, 1, 30),
            ("空闲暂停时间", self.idle_pause_seconds, 60, 7200),
        )
        for label, value, lower, upper in integer_ranges:
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{label}必须为 {lower}～{upper} 范围内的整数。")
        if (
            isinstance(self.change_threshold, bool)
            or not isinstance(self.change_threshold, (int, float))
            or not math.isfinite(self.change_threshold)
            or not 0.01 <= self.change_threshold <= 1
        ):
            raise ValueError("画面变化阈值必须为 0.01～1 范围内的有限数值。")
        for label, values in (("隐私关键词", self.privacy_keywords), ("排除应用", self.excluded_apps)):
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                for value in values
            ):
                raise ValueError(f"{label}必须为不含空项或控制字符的字符串元组。")


@dataclass(frozen=True)
class CameraSettings(PerceptionSettings):
    """Independent consent and device selection; screen approval never enables it."""

    device_id: str = ""

    def validate(self) -> None:
        super().validate()
        if (not isinstance(self.device_id, str) or len(self.device_id) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in self.device_id)):
            raise ValueError("摄像头设备标识无效。")

