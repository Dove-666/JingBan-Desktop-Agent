"""Taskbar boundary regressions using native API fakes; no real mouse or UI."""

import unittest
from unittest.mock import patch

from PySide6.QtCore import QRect

from desktop_pet import windows_work_area as work_areas


class _Screen:
    def __init__(self, geometry, available=None, ratio=1.5):
        self._geometry = geometry
        self._available = available if available is not None else geometry
        self._ratio = ratio

    def name(self):
        return "DISPLAY_TEST"

    def geometry(self):
        return QRect(self._geometry)

    def availableGeometry(self):
        return QRect(self._available)

    def devicePixelRatio(self):
        return self._ratio


class _NativeQueries:
    """Fake only the OS boundary, retaining the production conversion logic."""

    def __init__(self, monitor, taskbar, edge, query_succeeds=True):
        self.monitor = monitor
        self.taskbar = taskbar
        self.edge = edge
        self.query_succeeds = query_succeeds

    @staticmethod
    def _copy_rect(destination, coordinates):
        destination.left, destination.top, destination.right, destination.bottom = coordinates

    def EnumDisplayMonitors(self, _dc, _clip, callback, _data):
        callback(1, None, None, 0)
        return True

    def GetMonitorInfoW(self, _handle, output):
        info = output._obj
        info.szDevice = "DISPLAY_TEST"
        self._copy_rect(info.rcMonitor, self.monitor)
        return True

    def SHAppBarMessage(self, message, output):
        if message == 0x05 and self.query_succeeds:
            data = output._obj
            data.uEdge = self.edge
            self._copy_rect(data.rc, self.taskbar)
            return 1
        return 0  # No secondary auto-hide appbars in these fixtures.


class WorkAreaTests(unittest.TestCase):
    def query(self, screen, native):
        # This exercises the same public function on every host without loading
        # Windows libraries. The callback-type fake acts as a plain decorator.
        with (
            patch.object(work_areas.sys, "platform", "win32"),
            patch.object(work_areas, "_windows_api", return_value=(native, native, lambda f: f)),
        ):
            return work_areas.work_area(screen)

    def test_hidden_bottom_taskbar_is_excluded_when_qt_reports_full_screen(self):
        screen = _Screen(QRect(0, 0, 1707, 1067))
        native = _NativeQueries((0, 0, 2560, 1600), (0, 1528, 2560, 1600), 3)
        self.assertEqual(self.query(screen, native), QRect(0, 0, 1707, 1018))

    def test_negative_monitor_origin_is_preserved_for_left_and_top_taskbars(self):
        screen = _Screen(QRect(-2400, -1600, 1600, 1067))
        monitor = (-2400, -1600, 0, 0)
        cases = (
            (0, (-2400, -1600, -2327, 0), QRect(-2351, -1600, 1551, 1067)),
            (1, (-2400, -1600, 0, -1527), QRect(-2400, -1551, 1600, 1018)),
        )
        for edge, taskbar, expected in cases:
            with self.subTest(edge=edge):
                self.assertEqual(
                    self.query(screen, _NativeQueries(monitor, taskbar, edge)), expected,
                )

    def test_existing_qt_work_area_is_neither_deducted_twice_nor_expanded(self):
        native = _NativeQueries((0, 0, 2560, 1600), (0, 1528, 2560, 1600), 3)
        for available in (QRect(0, 0, 1707, 1018), QRect(12, 17, 1650, 980)):
            with self.subTest(available=available):
                screen = _Screen(QRect(0, 0, 1707, 1067), available)
                result = self.query(screen, native)
                self.assertEqual(result, available)
                self.assertIsNot(result, available)

    def test_taskbar_query_failure_returns_qt_area_and_logs_a_diagnostic(self):
        available = QRect(10, 20, 1660, 1000)
        screen = _Screen(QRect(0, 0, 1707, 1067), available)
        native = _NativeQueries((0, 0, 2560, 1600), (0, 1528, 2560, 1600), 3, False)
        with (
            patch.object(work_areas, "_reported", set()),
            self.assertLogs(work_areas.__name__, level="WARNING") as captured,
        ):
            self.assertEqual(self.query(screen, native), available)
        self.assertTrue(any("ABM_GETTASKBARPOS" in line for line in captured.output))

    def test_incompatible_native_dpi_mapping_safely_returns_qt_area(self):
        screen = _Screen(QRect(0, 0, 1707, 1067), ratio=1.0)
        native = _NativeQueries((0, 0, 2560, 1600), (0, 1528, 2560, 1600), 3)
        with (
            patch.object(work_areas, "_reported", set()),
            self.assertLogs(work_areas.__name__, level="WARNING"),
        ):
            self.assertEqual(self.query(screen, native), screen.availableGeometry())

    def test_non_windows_uses_qt_without_loading_native_libraries(self):
        screen = _Screen(QRect(-100, 30, 1600, 900))
        with (
            patch.object(work_areas.sys, "platform", "linux"),
            patch.object(work_areas, "_windows_api") as native_api,
        ):
            self.assertEqual(work_areas.work_area(screen), screen.availableGeometry())
            native_api.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
