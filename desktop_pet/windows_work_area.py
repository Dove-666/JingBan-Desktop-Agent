"""Read-only taskbar exclusion for Qt's logical, per-monitor work areas.

ABM_GETTASKBARPOS reports the system taskbar even when auto-hide makes Qt's
availableGeometry equal geometry. EnumDisplayMonitors/GetMonitorInfoW match the
native monitor by device name. Native offsets are divided by the QScreen DPR
and added to that screen's Qt origin; dividing virtual-desktop coordinates
directly would be wrong on mixed-DPI Windows desktops. Bounds round inward.

Call after QApplication has established its normal per-monitor DPI awareness.
ABM_GETAUTOHIDEBAREX also handles registered secondary Explorer taskbars. This
does not change taskbar state, appbar registration, or process DPI settings.
"""

import ctypes
from ctypes import wintypes
from functools import lru_cache
import logging
import math
import sys

from PySide6.QtCore import QRect
from PySide6.QtGui import QScreen


_logger = logging.getLogger(__name__)
_reported: set[str] = set()


class _AppBarData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT), ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def _warn_once(message: str) -> None:
    if message not in _reported:
        _reported.add(message)
        _logger.warning("Taskbar work-area query: %s", message)


@lru_cache(maxsize=1)
def _windows_api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(_AppBarData)]
    shell32.SHAppBarMessage.restype = ctypes.c_size_t  # UINT_PTR, including HWND
    return user32, shell32, callback_type


def _monitor_rect(screen: QScreen, user32, callback_type) -> wintypes.RECT:
    matches = []

    @callback_type
    def collect(handle, _dc, _rect, _data):
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            _warn_once(f"GetMonitorInfoW failed (error {ctypes.get_last_error()})")
        elif info.szDevice.casefold() == screen.name().casefold():
            matches.append(info.rcMonitor)
        return True

    if not user32.EnumDisplayMonitors(None, None, collect, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if not matches:
        raise ValueError(f"no native monitor matches Qt device {screen.name()!r}")
    return matches[0]


def _taskbar_depths(monitor: wintypes.RECT, user32, shell32) -> dict[int, int]:
    """Return native thickness by ABE_LEFT/TOP/RIGHT/BOTTOM (0/1/2/3)."""
    depths = {}
    bar = _AppBarData(cbSize=ctypes.sizeof(_AppBarData))
    if shell32.SHAppBarMessage(0x05, ctypes.byref(bar)):  # ABM_GETTASKBARPOS
        rect = bar.rc
        if (rect.left < monitor.right and rect.right > monitor.left
                and rect.top < monitor.bottom and rect.bottom > monitor.top):
            candidates = (
                rect.right - monitor.left, rect.bottom - monitor.top,
                monitor.right - rect.left, monitor.bottom - rect.top,
            )
            if bar.uEdge in range(4):
                depths[bar.uEdge] = candidates[bar.uEdge]
    else:
        _warn_once("ABM_GETTASKBARPOS failed; retaining Qt's available geometry")

    # Unlike the primary taskbar query, this explicitly targets one monitor.
    # A hidden window may be translated off-screen: use its thickness, not its
    # current position. Ignore third-party appbars returned by the same API.
    for edge in range(4):
        query = _AppBarData(
            cbSize=ctypes.sizeof(_AppBarData), uEdge=edge, rc=monitor,
        )
        handle = shell32.SHAppBarMessage(0x0B, ctypes.byref(query))
        if not handle:
            continue  # No auto-hide appbar on this edge is a normal result.
        name = ctypes.create_unicode_buffer(64)
        if not user32.GetClassNameW(handle, name, len(name)):
            _warn_once(f"GetClassNameW failed (error {ctypes.get_last_error()})")
            continue
        if name.value not in {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
            continue
        rect = wintypes.RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(rect)):
            _warn_once(f"GetWindowRect failed (error {ctypes.get_last_error()})")
            continue
        depth = rect.right - rect.left if edge in (0, 2) else rect.bottom - rect.top
        depths[edge] = max(depths.get(edge, 0), depth)
    return depths


def work_area(screen: QScreen) -> QRect:
    """Return Qt's work area additionally excluding hidden Windows taskbars.

    Any unsupported platform, failed monitor query, or incompatible native/Qt
    DPI mapping falls back to a copy of availableGeometry, with failures logged
    once. Standard visible secondary taskbars remain covered by Qt itself.
    """
    available = QRect(screen.availableGeometry())
    if sys.platform != "win32":
        return available
    try:
        user32, shell32, callback_type = _windows_api()
        monitor = _monitor_rect(screen, user32, callback_type)
        geometry = screen.geometry()
        ratio = screen.devicePixelRatio()
        native_width = monitor.right - monitor.left
        native_height = monitor.bottom - monitor.top
        if ratio <= 0 or (
            abs(native_width - geometry.width() * ratio) > max(2, ratio)
            or abs(native_height - geometry.height() * ratio) > max(2, ratio)
        ):
            raise ValueError(f"native monitor and Qt DPR disagree for {screen.name()!r}")
        left, top = available.x(), available.y()
        right, bottom = left + available.width(), top + available.height()
        for edge, depth in _taskbar_depths(monitor, user32, shell32).items():
            extent = native_width if edge in (0, 2) else native_height
            if not 0 < depth < extent:
                _warn_once(f"ignoring invalid taskbar thickness {depth} on edge {edge}")
                continue
            if edge == 0:
                left = max(left, geometry.x() + math.ceil(depth / ratio))
            elif edge == 1:
                top = max(top, geometry.y() + math.ceil(depth / ratio))
            elif edge == 2:
                right = min(right, geometry.x() + math.floor((native_width - depth) / ratio))
            else:
                bottom = min(bottom, geometry.y() + math.floor((native_height - depth) / ratio))
        result = QRect(left, top, right - left, bottom - top)
        if result.isEmpty():
            raise ValueError("taskbar query produced an empty work area")
        return result
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError) as exc:
        _warn_once(str(exc))
        return available
