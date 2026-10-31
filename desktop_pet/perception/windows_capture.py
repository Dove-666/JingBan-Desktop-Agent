"""Capture only the visible, monitor-clipped region of a foreground HWND.

Window DCs can be black for DirectComposition/Chromium/OpenGL windows. Read
the composed screen DC directly into a region-sized DIB, never a full-desktop
bitmap. All coordinates here are native physical pixels. Overlapping windows
are masked; protected/excluded content is not bypassed.
"""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from functools import lru_cache
import sys

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QRegion

from .capture_quality import is_near_black


class CaptureUnavailable(RuntimeError):
    """Safe, static explanation of a local capture failure."""


def intersect_rect(first, second):
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    return (left, top, right, bottom) if left < right and top < bottom else None


def select_visible_rect(window, monitors):
    """Choose the largest actual monitor intersection; retain negative origins."""
    intersections = [rect for monitor in monitors if (rect := intersect_rect(window, monitor))]
    return max(intersections, key=lambda rect: (rect[2] - rect[0]) * (rect[3] - rect[1]), default=None)


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("header", _BitmapInfoHeader), ("colors", wintypes.DWORD * 1)]


@lru_cache(maxsize=1)
def _api():
    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    user.GetDC.argtypes, user.GetDC.restype = [wintypes.HWND], wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetForegroundWindow.restype = wintypes.HWND
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsIconic.argtypes = [wintypes.HWND]
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwm.DwmGetWindowAttribute.restype = wintypes.LONG
    gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
    gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi.CreateDIBSection.restype = wintypes.HANDLE
    gdi.SelectObject.argtypes, gdi.SelectObject.restype = [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE
    gdi.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi.BitBlt.restype = wintypes.BOOL
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    return user, gdi, dwm


@contextmanager
def _physical_coordinates(user):
    old = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PM_AWARE_V2
    if not old:
        raise CaptureUnavailable("无法确定屏幕物理坐标，已跳过截图。")
    try:
        yield
    finally:
        user.SetThreadDpiAwarenessContext(old)


def _bounds(hwnd, user, dwm):
    rect = wintypes.RECT()
    if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    return rect.left, rect.top, rect.right, rect.bottom


def _monitors(user):
    results = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    @callback_type
    def collect(_monitor, _dc, rect, _data):
        value = rect.contents
        results.append((value.left, value.top, value.right, value.bottom))
        return True

    user.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
    if not user.EnumDisplayMonitors(None, None, collect, 0):
        raise CaptureUnavailable("无法读取显示器边界，已跳过截图。")
    return results


def _overlays(hwnd, bounds, user, dwm):
    """Mask every visible top-level window above the target, including the pet."""
    results, target_found = [], []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(other, _data):
        if other == hwnd:
            target_found.append(True)
            return False
        if not user.IsWindowVisible(other) or user.IsIconic(other):
            return True
        cloaked = wintypes.DWORD()
        if dwm.DwmGetWindowAttribute(other, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            return True
        other_bounds = _bounds(other, user, dwm)
        if other_bounds:
            overlap = intersect_rect(bounds, other_bounds)
            if overlap:
                results.append(overlap)
        return True

    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows(collect, 0)
    if not target_found:
        raise CaptureUnavailable("前台窗口已不可见，已跳过截图。")
    return results


def _copy_region(bounds, user, gdi):
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    if not 0 < width * height <= 32_000_000:
        raise CaptureUnavailable("可见窗口尺寸不适合截图。")
    screen_dc = user.GetDC(None)
    memory_dc = bitmap = previous = None
    try:
        if not screen_dc:
            raise CaptureUnavailable("无法访问可见屏幕画面。")
        memory_dc = gdi.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            raise CaptureUnavailable("无法创建区域截图缓冲。")
        info = _BitmapInfo()
        info.header = _BitmapInfoHeader(biSize=ctypes.sizeof(_BitmapInfoHeader), biWidth=width,
                                        biHeight=-height, biPlanes=1, biBitCount=32, biCompression=0)
        pixels = ctypes.c_void_p()
        bitmap = gdi.CreateDIBSection(screen_dc, ctypes.byref(info), 0, ctypes.byref(pixels), None, 0)
        if not bitmap or not pixels.value:
            raise CaptureUnavailable("无法分配区域截图缓冲。")
        previous = gdi.SelectObject(memory_dc, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            previous = None
            raise CaptureUnavailable("无法准备区域截图缓冲。")
        if not gdi.BitBlt(memory_dc, 0, 0, width, height, screen_dc, left, top, 0x40CC0020):
            raise CaptureUnavailable("可见窗口截图失败，未外发。")
        if not gdi.GdiFlush():
            raise CaptureUnavailable("可见窗口截图尚未完成，未外发。")
        raw = ctypes.string_at(pixels.value, width * height * 4)
        return QImage(raw, width, height, width * 4, QImage.Format.Format_RGB32).copy()
    finally:
        if previous:
            gdi.SelectObject(memory_dc, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if memory_dc:
            gdi.DeleteDC(memory_dc)
        if screen_dc:
            user.ReleaseDC(None, screen_dc)


def mask_overlays(image, bounds, overlays):
    """Cover foreign/topmost pixels before any image leaves the capture module."""
    visible = QRegion(QRect(0, 0, image.width(), image.height()))
    painter = QPainter(image)
    try:
        for overlay in overlays:
            overlap = intersect_rect(bounds, overlay)
            if overlap:
                left, top, right, bottom = overlap
                rect = QRect(left - bounds[0], top - bounds[1], right - left, bottom - top)
                painter.fillRect(rect, QColor("#808080"))
                visible = visible.subtracted(QRegion(rect))
    finally:
        painter.end()
    if visible.isEmpty():
        raise CaptureUnavailable("目标窗口完全被其他窗口遮挡，未外发。")
    return image


def capture_visible_window(hwnd: int) -> QImage:
    if sys.platform != "win32":
        raise CaptureUnavailable("当前平台不支持可见窗口截图。")
    user, gdi, dwm = _api()
    with _physical_coordinates(user):
        if user.GetForegroundWindow() != hwnd:
            raise CaptureUnavailable("目标已不是前台窗口，未截图。")
        original_bounds = _bounds(hwnd, user, dwm)
        bounds = select_visible_rect(original_bounds, _monitors(user)) if original_bounds else None
        if bounds is None:
            raise CaptureUnavailable("目标窗口没有可见的屏幕区域。")
        before = _overlays(hwnd, bounds, user, dwm)
        image = _copy_region(bounds, user, gdi)
        after = _overlays(hwnd, bounds, user, dwm)
        if user.GetForegroundWindow() != hwnd or _bounds(hwnd, user, dwm) != original_bounds:
            raise CaptureUnavailable("截图期间窗口已移动或切换，已丢弃画面。")
    image = mask_overlays(image, bounds, before + after)
    excluded = [(left - bounds[0], top - bounds[1], right - bounds[0], bottom - bounds[1])
                for left, top, right, bottom in before + after]
    if is_near_black(image, excluded):
        raise CaptureUnavailable("截图可见区域近乎全黑，可能为暗场或无法捕获的内容；本次未调用模型。")
    if max(image.width(), image.height()) > 640:
        image = image.scaled(640, 640, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return image
