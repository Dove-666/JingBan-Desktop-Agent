"""Synthetic Win32 capture boundaries; never read desktop pixels or user titles."""
import ctypes
from ctypes import wintypes
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib


spec = importlib.util.spec_from_file_location('aura_windows_screen_capture',
    Path(__file__).resolve().parents[1] / 'agent_backend/ui_tars/windows-screen-capture.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class NativeCaptureTests(unittest.TestCase):
    def test_host_pid_crosses_venv_redirector_without_inferring_launcher_dpi(self):
        request = {'mode': 'probe', 'inputPid': 12345}
        desktop = Mock()
        desktop.metadata.return_value = {'bounds': [0, 0, 64, 48]}
        with patch.object(capture.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))), \
                patch.object(capture, 'DesktopCapture', return_value=desktop) as create, patch('builtins.print'):
            capture.main()
        create.assert_called_once_with(12345)
        desktop.metadata.assert_called_once()
        desktop.capture.assert_not_called()
        request['inputPid'] = True
        with patch.object(capture.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))), \
                patch.object(capture, 'DesktopCapture') as create, self.assertRaises(capture.CaptureFailure):
            capture.main()
        create.assert_not_called()

    def test_png_contains_real_rgb_pixels_with_top_down_rows_and_no_alpha_blackout(self):
        # RGB32 screen DC alpha is often zero; it must not make an otherwise
        # valid GDI image transparent when encoded for the model.
        raw = bytes([0, 0, 255, 0, 0, 255, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])
        png = capture.png_bgra(raw, 2, 2)
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        offset, compressed = 8, b''
        while offset < len(png):
            length = int.from_bytes(png[offset:offset + 4], 'big')
            if png[offset + 4:offset + 8] == b'IDAT':
                compressed += png[offset + 8:offset + 8 + length]
            offset += length + 12
        self.assertEqual(zlib.decompress(compressed), b'\0\xff\0\0\0\xff\0\0\0\0\xff\xff\xff\xff')

    def test_black_frames_are_rejected_without_rejecting_ordinary_dark_ui(self):
        self.assertTrue(capture.near_black(bytes([0, 0, 0, 0]) * 64 * 48, 64, 48))
        self.assertFalse(capture.near_black(bytes([25, 20, 16, 0]) * 64 * 48, 64, 48))

    def desktop(self, *, session=1, state=0, desktop_name='Default'):
        target = capture.DesktopCapture.__new__(capture.DesktopCapture)
        def session_id(_pid, output):
            output._obj.value = session
            return 1
        target.kernel = SimpleNamespace(ProcessIdToSessionId=session_id, GetCurrentProcessId=lambda: 1)
        state_buffer = wintypes.DWORD(state)
        def session_state(_server, _session, _kind, output, length):
            output._obj.value = ctypes.addressof(state_buffer)
            length._obj.value = ctypes.sizeof(state_buffer)
            return 1
        target.wts = SimpleNamespace(WTSQuerySessionInformationW=session_state, WTSFreeMemory=Mock())
        def name(_desktop, _kind, output, _length, _needed):
            output.value = desktop_name
            return 1
        target.user = SimpleNamespace(OpenInputDesktop=Mock(return_value=1), CloseDesktop=Mock(),
                                      GetUserObjectInformationW=name)
        return target

    def test_disconnected_and_noninteractive_sessions_are_blocked_before_desktop_access(self):
        for target in (self.desktop(session=0), self.desktop(state=4)):
            with self.subTest(target=target), self.assertRaises(capture.CaptureFailure) as caught:
                target.available()
            self.assertEqual(caught.exception.code, 'DESKTOP_SESSION_UNAVAILABLE')
            target.user.OpenInputDesktop.assert_not_called()

    def test_secure_desktop_is_never_switched_or_captured(self):
        target = self.desktop(desktop_name='Winlogon')
        with self.assertRaises(capture.CaptureFailure) as caught:
            target.available()
        self.assertEqual(caught.exception.code, 'DESKTOP_LOCKED')
        target.user.OpenInputDesktop.assert_called_once_with(0, False, 1)
        target.user.CloseDesktop.assert_called_once_with(1)
        self.assertFalse(hasattr(target.user, 'SwitchDesktop'))

    def test_default_interactive_desktop_is_read_only(self):
        target = self.desktop()
        target.available()
        target.wts.WTSFreeMemory.assert_called_once()
        target.user.CloseDesktop.assert_called_once_with(1)

    def bitmap(self, *, copy_success=True, changed=False):
        target = capture.DesktopCapture.__new__(capture.DesktopCapture)
        image = ctypes.create_string_buffer(bytes([40, 60, 80, 0]) * 64 * 48)
        def create_bitmap(_screen, _header, _usage, pixels, _section, _offset):
            pixels._obj.value = ctypes.addressof(image)
            return 2
        target.user = SimpleNamespace(GetDC=Mock(return_value=1), ReleaseDC=Mock())
        target.gdi = SimpleNamespace(CreateCompatibleDC=Mock(return_value=3), CreateDIBSection=create_bitmap,
            SelectObject=Mock(return_value=4), BitBlt=Mock(return_value=copy_success), GdiFlush=Mock(return_value=1),
            DeleteObject=Mock(), DeleteDC=Mock())
        meta = {'bounds': [0, 0, 64, 48], 'monitors': [[0, 0, 64, 48]], 'primary': True, 'backend': 'win32-gdi'}
        after = {**meta, 'bounds': [64, 0, 128, 48]} if changed else meta
        target.metadata = Mock(side_effect=[meta, after])
        target._test_image = image  # Keep the synthetic buffer alive through string_at.
        return target

    def test_failed_gdi_and_changed_monitor_frames_are_discarded_and_handles_released(self):
        for target, code in ((self.bitmap(copy_success=False), 'SCREENSHOT_FAILED'),
                             (self.bitmap(changed=True), 'SCREENSHOT_DISPLAY_UNAVAILABLE')):
            with self.subTest(code=code), self.assertRaises(capture.CaptureFailure) as caught:
                target.capture()
            self.assertEqual(caught.exception.code, code)
            target.gdi.DeleteObject.assert_called_once_with(2)
            target.gdi.DeleteDC.assert_called_once_with(3)
            target.user.ReleaseDC.assert_called_once_with(None, 1)

    def test_native_capture_encodes_only_the_requested_monitor_buffer(self):
        target = self.bitmap()
        result = target.capture()
        self.assertEqual((result['width'], result['height']), (64, 48))
        target.gdi.BitBlt.assert_called_once_with(3, 0, 0, 64, 48, 1, 0, 0, 0x40CC0020)
        self.assertEqual(target.metadata.call_count, 2)


if __name__ == '__main__':
    unittest.main()
