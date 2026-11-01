"""Opt-in, foreground-window-only observation. No image or title is written to disk."""

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
import math
from pathlib import PureWindowsPath
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from .settings import PerceptionSettings
from .capture_quality import is_near_black
from .windows_capture import CaptureUnavailable, capture_visible_window


@dataclass(frozen=True)
class ForegroundInfo:
    hwnd: int = 0
    pid: int = 0
    process_name: str = ""
    title: str = ""
    idle_seconds: float = 0.0
    available: bool = False


@dataclass(frozen=True)
class Observation:
    info: ForegroundInfo
    image: QImage | None = None
    blocked_reason: str = ""
    blocked_code: str = ""
    capture_method: str = ""
    source: str = "screen"


def windows_foreground(*, expected_hwnd: int | None = None,
                       expected_pid: int | None = None) -> ForegroundInfo:
    """Read metadata/last-input timestamp, never keyboard contents or hook events."""
    if sys.platform != "win32":
        return ForegroundInfo()
    try:
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsIconic.argtypes = [wintypes.HWND]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetTickCount.restype = wintypes.DWORD
        hwnd = user.GetForegroundWindow()
        if not hwnd or not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd):
            return ForegroundInfo()
        if expected_hwnd is not None and int(hwnd) != expected_hwnd:
            return ForegroundInfo()
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if expected_pid is not None and pid.value != expected_pid:
            return ForegroundInfo()
        text = ctypes.create_unicode_buffer(512)
        user.GetWindowTextW(hwnd, text, len(text))
        handle = kernel.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ForegroundInfo()
        try:
            size = wintypes.DWORD(32768)
            name = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                return ForegroundInfo()
            process = PureWindowsPath(name.value).name
        finally:
            kernel.CloseHandle(handle)

        class LastInput(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        last = LastInput(cbSize=ctypes.sizeof(LastInput))
        # If inactivity cannot be established, fail closed for proactive speech.
        idle = ((kernel.GetTickCount() - last.dwTime) & 0xffffffff) / 1000 if user.GetLastInputInfo(ctypes.byref(last)) else float("inf")
        return ForegroundInfo(int(hwnd), pid.value, process, text.value, idle, True)
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return ForegroundInfo()


def _is_aura_window(info: ForegroundInfo, own_pid: int) -> bool:
    if info.pid == own_pid:
        return True
    # Another checkout/older instance can still be running during an update.
    # Do not capture its chat or revealed API fields. Match this application's
    # window identity together with its host, rather than excluding Python tools.
    host = PureWindowsPath(info.process_name).name.casefold()
    if host not in {"python.exe", "pythonw.exe", "python", "pythonw", "aura-desktop-agent.exe"}:
        return False
    title = info.title.strip()
    return (title == "AURA · 鲸鱼娘桌宠"
            or title.startswith("鲸鱼娘 · 调试面板 · ")
            or title in {"和鲸鱼娘聊天", "设置 · 鲸鱼娘", "鲸鱼娘 · 长期记忆"}
            or title.endswith(" - 鲸鱼娘桌宠"))


def privacy_reason(info: ForegroundInfo, settings: PerceptionSettings, own_pid: int,
                   *, enforce_idle_pause: bool = True) -> str:
    if not info.available or not info.hwnd or not info.process_name or not info.title.strip():
        return "无法确认前台窗口，已暂停截图和外发。"
    if _is_aura_window(info, own_pid):
        return "前台是 AURA 自身窗口，跳过以避免自我观察。"
    if (isinstance(info.idle_seconds, bool) or not isinstance(info.idle_seconds, (int, float))
            or info.idle_seconds < 0 or not math.isfinite(info.idle_seconds)):
        return "无法确认用户输入活动，暂停观察。"
    process = info.process_name.casefold()
    if any(word.casefold() in process for word in settings.excluded_apps if word.strip()):
        return "前台应用位于排除列表，未截图。"
    title = info.title.casefold()
    if any(word.casefold() in title for word in settings.privacy_keywords if word.strip()):
        return "窗口标题命中隐私过滤，未截图；标题不显示。"
    if enforce_idle_pause and info.idle_seconds >= settings.idle_pause_seconds:
        return "用户长时间无输入活动，暂停观察与主动互动。"
    return ""


def capture_foreground(info: ForegroundInfo) -> QImage:
    """Read composed, visible pixels within the foreground window's boundary."""
    return capture_visible_window(info.hwnd)


class ScreenObserver:
    def __init__(self, metadata_provider=None, capture=None, own_pid=None):
        self._metadata = metadata_provider or windows_foreground
        self._capture = capture or capture_foreground
        self._own_pid = os.getpid() if own_pid is None else own_pid

    def _info(self) -> ForegroundInfo:
        try:
            info = self._metadata()
            if not isinstance(info, ForegroundInfo) or not isinstance(info.process_name, str) or not isinstance(info.title, str):
                return ForegroundInfo()
            return info
        except Exception:
            return ForegroundInfo()

    def current_safety(self, settings: PerceptionSettings) -> str:
        return privacy_reason(self._info(), settings, self._own_pid)

    def _blocked_code(self, info, settings, *, explicit=False):
        if not privacy_reason(info, settings, self._own_pid, enforce_idle_pause=not explicit):
            return ""
        return "own_window" if info.available and _is_aura_window(info, self._own_pid) else "privacy"

    def current_blocked_code(self, settings: PerceptionSettings) -> str:
        return self._blocked_code(self._info(), settings)

    def is_current(self, observation: Observation, settings: PerceptionSettings) -> bool:
        return self._is_current(observation, settings)

    def is_current_explicit(self, observation: Observation, settings: PerceptionSettings) -> bool:
        """Revalidate a manual request, without treating local idle as inactivity."""
        return self._is_current(observation, settings, explicit=True)

    def _is_current(self, observation, settings, *, explicit=False):
        if explicit and not settings.enabled:
            return False
        info = self._info()
        return ((info.hwnd, info.pid, info.title) ==
                (observation.info.hwnd, observation.info.pid, observation.info.title)
                and not privacy_reason(info, settings, self._own_pid, enforce_idle_pause=not explicit))

    def observe(self, settings: PerceptionSettings) -> Observation:
        return self._observe(settings)

    def observe_explicit(self, settings: PerceptionSettings) -> Observation:
        """An explicit remote user request skips only the valid local idle limit.

        Invalid/missing activity metadata and all window/privacy/image checks
        still fail closed. Remote authorization is checked by the caller.
        """
        return self._observe(settings, explicit=True)

    def _observe(self, settings, *, explicit=False):
        if not settings.enabled:
            return Observation(ForegroundInfo(), blocked_reason="屏幕感知已关闭。", blocked_code="disabled")
        info = self._info()
        blocked = privacy_reason(info, settings, self._own_pid, enforce_idle_pause=not explicit)
        if blocked:
            return Observation(info, blocked_reason=blocked, blocked_code=self._blocked_code(info, settings, explicit=explicit))
        try:
            image = self._capture(info)
            after = self._info()
            if ((after.hwnd, after.pid, after.title) != (info.hwnd, info.pid, info.title)
                    or privacy_reason(after, settings, self._own_pid, enforce_idle_pause=not explicit)):
                code = "own_window" if self._blocked_code(after, settings, explicit=explicit) == "own_window" else "context_changed"
                return Observation(info, blocked_reason="截图期间前台情境发生变化，已丢弃画面。", blocked_code=code)
            if not isinstance(image, QImage) or image.isNull():
                return Observation(info, blocked_reason="窗口截图不可用，已安全跳过。", blocked_code="capture_unavailable")
            if is_near_black(image):
                return Observation(info, blocked_reason="截图画面近乎全黑，可能为暗场或无法捕获的内容；本次未调用模型。",
                                   blocked_code="capture_unavailable")
            if max(image.width(), image.height()) > 640:
                image = image.scaled(640, 640, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            return Observation(info, image, capture_method="可见窗口区域（遮挡已遮蔽）")
        except CaptureUnavailable as exc:
            return Observation(info, blocked_reason=str(exc), blocked_code="capture_unavailable")
        except Exception:
            return Observation(info, blocked_reason="无法获取前台画面，已安全跳过；未外发。", blocked_code="capture_unavailable")
