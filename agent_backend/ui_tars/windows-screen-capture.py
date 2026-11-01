"""Fresh, read-only Win32 monitor captures over private stdio. No files or titles.

The selected monitor contains the foreground window (primary is the fallback).
Native physical coordinates, including negative origins, accompany every frame.
No cached bitmap, PrintWindow, secure desktop switch, or hidden-window capture.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import struct
import sys
import zlib


class CaptureFailure(Exception):
    def __init__(self, code):
        self.code = code


class BitmapHeader(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
                ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                ("image_size", wintypes.DWORD), ("x_resolution", wintypes.LONG),
                ("y_resolution", wintypes.LONG), ("colors", wintypes.DWORD), ("important", wintypes.DWORD)]


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                ("work", wintypes.RECT), ("flags", wintypes.DWORD)]


def near_black(raw, width, height):
    """A black/unreadable frame is never sent to the model, even if GDI succeeded."""
    sampled = dark = 0
    for y in range(32):
        row = min(height - 1, int((y + .5) * height / 32)) * width * 4
        for x in range(48):
            offset = row + min(width - 1, int((x + .5) * width / 48)) * 4
            sampled += 1
            dark += max(raw[offset:offset + 3]) <= 12
    return dark / sampled >= .985


def png_bgra(raw, width, height):
    """Encode opaque RGB PNG using only the isolated interpreter's stdlib."""
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    pixels = bytearray(width * height * 3)
    pixels[0::3], pixels[1::3], pixels[2::3] = raw[2::4], raw[1::4], raw[0::4]
    rows = b''.join(b'\0' + pixels[row:row + width * 3] for row in range(0, len(pixels), width * 3))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 3)) + chunk(b'IEND', b''))


class DesktopCapture:
    def __init__(self, input_pid):
        if sys.platform != 'win32':
            raise CaptureFailure('DESKTOP_SESSION_UNAVAILABLE')
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.gdi = ctypes.WinDLL('gdi32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.wts = ctypes.WinDLL('wtsapi32', use_last_error=True)
        self.shcore = ctypes.WinDLL('shcore', use_last_error=True)
        user, gdi = self.user, self.gdi
        user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        if not user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)):
            raise CaptureFailure('SCREENSHOT_DISPLAY_UNAVAILABLE')
        user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user.OpenInputDesktop.restype = wintypes.HANDLE
        user.CloseDesktop.argtypes = [wintypes.HANDLE]
        user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        user.GetForegroundWindow.restype = wintypes.HWND
        user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user.MonitorFromWindow.restype = wintypes.HANDLE
        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        user.GetDC.argtypes, user.GetDC.restype = [wintypes.HWND], wintypes.HDC
        user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
        gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BitmapHeader), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        gdi.CreateDIBSection.restype = wintypes.HANDLE
        gdi.SelectObject.argtypes, gdi.SelectObject.restype = [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE
        gdi.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        gdi.DeleteObject.argtypes, gdi.DeleteDC.argtypes = [wintypes.HANDLE], [wintypes.HDC]
        self.wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        self.wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        self.kernel.OpenProcess.argtypes, self.kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.shcore.GetProcessDpiAwareness.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
        self.shcore.GetProcessDpiAwareness.restype = wintypes.LONG
        # NutJS runs in the parent Node process. Its cursor API uses that
        # process's logical coordinate domain, which can differ from GDI pixels.
        # Windows venv redirectors can insert a Python launcher process between
        # this helper and Node. The host supplies its actual PID over stdin;
        # os.getppid() would then query the wrong process's DPI awareness.
        parent = self.kernel.OpenProcess(0x1000, False, input_pid)
        awareness = ctypes.c_int(-1)
        try:
            if not parent or self.shcore.GetProcessDpiAwareness(parent, ctypes.byref(awareness)) != 0 or awareness.value not in (0, 1, 2):
                raise CaptureFailure('SCREENSHOT_DISPLAY_UNAVAILABLE')
            self.input_awareness = awareness.value
        finally:
            if parent:
                self.kernel.CloseHandle(parent)

    def available(self):
        session = wintypes.DWORD()
        if not self.kernel.ProcessIdToSessionId(self.kernel.GetCurrentProcessId(), ctypes.byref(session)) or not session.value:
            raise CaptureFailure('DESKTOP_SESSION_UNAVAILABLE')
        state, length = ctypes.c_void_p(), wintypes.DWORD()
        if not self.wts.WTSQuerySessionInformationW(None, session.value, 8, ctypes.byref(state), ctypes.byref(length)):
            raise CaptureFailure('DESKTOP_SESSION_UNAVAILABLE')
        try:
            if length.value < ctypes.sizeof(wintypes.DWORD) or ctypes.cast(state, ctypes.POINTER(wintypes.DWORD)).contents.value != 0:
                raise CaptureFailure('DESKTOP_SESSION_UNAVAILABLE')
        finally:
            self.wts.WTSFreeMemory(state)
        desktop = self.user.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS only
        if not desktop:
            raise CaptureFailure('DESKTOP_LOCKED' if ctypes.get_last_error() == 5 else 'DESKTOP_SESSION_UNAVAILABLE')
        try:
            name, needed = ctypes.create_unicode_buffer(256), wintypes.DWORD()
            if not self.user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
                raise CaptureFailure('DESKTOP_SESSION_UNAVAILABLE')
            if name.value.casefold() != 'default':
                raise CaptureFailure('DESKTOP_LOCKED')
        finally:
            self.user.CloseDesktop(desktop)

    def monitor_rectangles(self):
        monitors = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                          ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        @callback_type
        def collect(handle, _dc, rect, _data):
            value = rect.contents
            monitors.append([value.left, value.top, value.right, value.bottom])
            return True
        self.user.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
        if not self.user.EnumDisplayMonitors(None, None, collect, 0) or not monitors:
            raise CaptureFailure('SCREENSHOT_DISPLAY_UNAVAILABLE')
        return sorted(monitors)

    def monitor_info(self, selected):
        info = MonitorInfo(size=ctypes.sizeof(MonitorInfo))
        if not selected or not self.user.GetMonitorInfoW(selected, ctypes.byref(info)):
            raise CaptureFailure('SCREENSHOT_DISPLAY_UNAVAILABLE')
        rect = info.monitor
        bounds = [rect.left, rect.top, rect.right, rect.bottom]
        return bounds, bool(info.flags & 1)

    def metadata(self):
        self.available()
        monitors = self.monitor_rectangles()
        selected = self.user.MonitorFromWindow(self.user.GetForegroundWindow(), 1)
        bounds, primary = self.monitor_info(selected)
        left, top, right, bottom = bounds
        if bounds not in monitors or not 0 < (right - left) * (bottom - top) <= 32_000_000:
            raise CaptureFailure('SCREENSHOT_DISPLAY_UNAVAILABLE')
        context = {0: -1, 1: -2, 2: -4}[self.input_awareness]
        old = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(context))
        if not old:
            raise CaptureFailure('SCREENSHOT_DISPLAY_UNAVAILABLE')
        try:
            input_monitors = self.monitor_rectangles()
            input_bounds, _primary = self.monitor_info(selected)
        finally:
            self.user.SetThreadDpiAwarenessContext(old)
        if input_bounds not in input_monitors:
            raise CaptureFailure('SCREENSHOT_DISPLAY_UNAVAILABLE')
        return {'bounds': bounds, 'monitors': monitors, 'primary': primary, 'backend': 'win32-gdi',
                'inputBounds': input_bounds, 'inputMonitors': input_monitors, 'dpiAwareness': self.input_awareness}

    def capture(self):
        before = self.metadata()
        # The pinned libnut pointer driver normalizes absolute movement by
        # the primary display and does not set MOUSEEVENTF_VIRTUALDESK. Never
        # give the model a secondary image paired with that input driver.
        if not before['primary'] or before['bounds'][:2] != [0, 0] or before.get('inputBounds', before['bounds'])[:2] != [0, 0]:
            raise CaptureFailure('SCREENSHOT_DISPLAY_UNAVAILABLE')
        left, top, right, bottom = before['bounds']
        width, height = right - left, bottom - top
        user, gdi = self.user, self.gdi
        screen, memory, bitmap, previous = user.GetDC(None), None, None, None
        try:
            if not screen:
                raise CaptureFailure('SCREENSHOT_FAILED')
            memory = gdi.CreateCompatibleDC(screen)
            if not memory:
                raise CaptureFailure('SCREENSHOT_FAILED')
            header = BitmapHeader(size=ctypes.sizeof(BitmapHeader), width=width, height=-height, planes=1, bits=32)
            pixels = ctypes.c_void_p()
            bitmap = gdi.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(pixels), None, 0)
            if not bitmap or not pixels.value:
                raise CaptureFailure('SCREENSHOT_FAILED')
            previous = gdi.SelectObject(memory, bitmap)
            if not previous or previous == ctypes.c_void_p(-1).value:
                previous = None
                raise CaptureFailure('SCREENSHOT_FAILED')
            if not gdi.BitBlt(memory, 0, 0, width, height, screen, left, top, 0x40CC0020) or not gdi.GdiFlush():
                raise CaptureFailure('SCREENSHOT_FAILED')
            raw = ctypes.string_at(pixels.value, width * height * 4)
        finally:
            if previous:
                gdi.SelectObject(memory, previous)
            if bitmap:
                gdi.DeleteObject(bitmap)
            if memory:
                gdi.DeleteDC(memory)
            if screen:
                user.ReleaseDC(None, screen)
        if before != self.metadata():
            raise CaptureFailure('SCREENSHOT_DISPLAY_UNAVAILABLE')
        if near_black(raw, width, height):
            raise CaptureFailure('SCREENSHOT_BLACK_FRAME')
        return {**before, 'width': width, 'height': height, 'scaleFactor': 1,
                'base64': base64.b64encode(png_bgra(raw, width, height)).decode('ascii')}


def main():
    request = json.loads(sys.stdin.buffer.read(1025).decode('utf-8'))
    if (not isinstance(request, dict) or set(request) != {'mode', 'inputPid'}
            or request['mode'] not in {'probe', 'capture'} or type(request['inputPid']) is not int
            or not 0 < request['inputPid'] <= 0xffffffff):
        raise CaptureFailure('SCREENSHOT_FAILED')
    capture = DesktopCapture(request['inputPid'])
    result = capture.metadata() if request['mode'] == 'probe' else capture.capture()
    print(json.dumps({'ok': True, **result}, separators=(',', ':')), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exception:
        code = exception.code if isinstance(exception, CaptureFailure) else 'SCREENSHOT_FAILED'
        print(json.dumps({'ok': False, 'code': code}), flush=True)
        raise SystemExit(1)
