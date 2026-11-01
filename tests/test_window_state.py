"""Window restoration uses a temporary INI and synthetic work areas."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QSettings, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from desktop_pet.ui.window_state import WindowPlacement, bounded_rect


class WindowStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_removed_monitor_and_taskbar_are_outside_restored_bounds(self):
        area = QRect(0, 0, 1280, 680)
        result = bounded_rect(QRect(2300, 900, 920, 720), [area])
        self.assertTrue(area.contains(result))
        self.assertEqual(result.size(), QRect(0, 0, 920, 680).size())

    def test_largest_intersection_keeps_negative_coordinate_monitor(self):
        areas = [QRect(0, 0, 1920, 1040), QRect(-1280, 0, 1280, 984)]
        result = bounded_rect(QRect(-1100, 250, 700, 600), areas)
        self.assertEqual(result, QRect(-1100, 250, 700, 600))

    def test_geometry_survives_reopen_without_shifting(self):
        with tempfile.TemporaryDirectory(prefix="aura-window-state-") as directory:
            path = Path(directory) / "settings.ini"
            first = QWidget()
            first.resize(500, 400)
            first.move(100, 110)
            state = WindowPlacement(first, path, "test")
            state.save()
            saved = QSettings(str(path), QSettings.Format.IniFormat).value("ui/windows/test")
            self.assertEqual(saved.topLeft(), first.pos())
            second = QWidget()
            restored = WindowPlacement(second, path, "test")
            self.assertEqual(second.pos(), first.pos())
            self.assertEqual(second.size(), first.size())
            restored.save()
            self.assertEqual(QSettings(str(path), QSettings.Format.IniFormat).value("ui/windows/test"), saved)
            first.deleteLater()
            second.deleteLater()

    def test_pet_restores_position_without_overriding_current_scale(self):
        with tempfile.TemporaryDirectory(prefix="aura-pet-position-") as directory:
            path = Path(directory) / "settings.ini"
            settings = QSettings(str(path), QSettings.Format.IniFormat)
            settings.setValue("ui/windows/pet", QRect(50, 60, 800, 900))
            settings.sync()
            pet = QWidget()
            pet.resize(200, 240)
            WindowPlacement(pet, path, "pet", remember_size=False)
            self.assertEqual(pet.pos(), QPoint(50, 60))
            self.assertEqual(pet.width(), 200)
            self.assertEqual(pet.height(), 240)
            pet.deleteLater()

    def test_transparent_pet_edge_position_survives_hide_show_and_restart(self):
        class Screen(QObject):
            availableGeometryChanged = Signal(QRect)
            area = QRect(0, 0, 800, 600)

        screen = Screen()
        visible = QRectF(100, 120, 160, 240)
        with tempfile.TemporaryDirectory(prefix="aura-pet-edge-position-") as directory:
            path = Path(directory) / "settings.ini"
            first = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
            second = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
            first.resize(360, 460)
            second.resize(360, 460)
            first.move(-100, -120)
            try:
                with (patch.object(QGuiApplication, "screens", return_value=[screen]),
                      patch("desktop_pet.ui.window_state.screen_for_rect", return_value=screen) as choose,
                      patch("desktop_pet.ui.window_state.work_area", side_effect=lambda item: item.area)):
                    state = WindowPlacement(first, path, "pet", remember_size=False,
                                            placement_bounds=lambda: visible)
                    self.assertEqual(first.pos(), QPoint(-100, -120))
                    self.assertEqual(first.size(), QRect(0, 0, 360, 460).size())
                    choose.assert_called_with(QRectF(0, 0, 160, 240))
                    first.show()
                    self.app.processEvents()
                    first.hide()
                    self.assertEqual(first.pos(), QPoint(-100, -120))
                    saved = QSettings(str(path), QSettings.Format.IniFormat).value("ui/windows/pet")
                    self.assertEqual(saved.topLeft(), QPoint(-100, -120))
                    restored = WindowPlacement(second, path, "pet", remember_size=False,
                                               placement_bounds=lambda: visible)
                    second.show()
                    self.app.processEvents()
                    self.assertEqual(second.pos(), QPoint(-100, -120))
                    self.assertEqual(second.size(), first.size())
                    self.assertTrue(QRectF(screen.area).contains(visible.translated(second.pos())))
                    self.assertIsNotNone(state)
                    self.assertIsNotNone(restored)
            finally:
                first.hide()
                second.hide()
                first.deleteLater()
                second.deleteLater()

    def test_transparent_pet_work_area_change_bounds_visible_character_and_keeps_size(self):
        class Screen(QObject):
            availableGeometryChanged = Signal(QRect)
            area = QRect(0, 0, 800, 600)

        screen = Screen()
        visible = QRectF(100, 120, 160, 240)
        with tempfile.TemporaryDirectory(prefix="aura-pet-work-area-") as directory:
            pet = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
            pet.resize(360, 460)
            pet.move(540, 240)
            try:
                with (patch.object(QGuiApplication, "screens", return_value=[screen]),
                      patch("desktop_pet.ui.window_state.screen_for_rect", return_value=screen),
                      patch("desktop_pet.ui.window_state.work_area", side_effect=lambda item: item.area)):
                    state = WindowPlacement(pet, Path(directory) / "settings.ini", "pet",
                                            remember_size=False, placement_bounds=lambda: visible)
                    self.assertEqual(pet.pos(), QPoint(540, 240))
                    # A left appbar and shorter desktop should move the visible
                    # pet, while its transparent margins may remain outside.
                    screen.area = QRect(40, 0, 760, 500)
                    screen.availableGeometryChanged.emit(screen.area)
                    self.assertEqual(pet.pos(), QPoint(540, 140))
                    self.assertEqual(pet.size(), QRect(0, 0, 360, 460).size())
                    self.assertTrue(QRectF(screen.area).contains(visible.translated(pet.pos())))
                    self.assertFalse(screen.area.contains(pet.frameGeometry()))
                    self.assertIsNotNone(state)
            finally:
                pet.deleteLater()

    def test_transparent_pet_fit_reloads_bounds_without_switching_selected_monitor(self):
        class Screen(QObject):
            availableGeometryChanged = Signal(QRect)
            area = QRect(-500, 0, 500, 500)

        screen = Screen()
        bounds = [QRectF(100, 120, 160, 240)]
        with tempfile.TemporaryDirectory(prefix="aura-pet-fit-visible-") as directory:
            pet = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
            pet.resize(360, 460)
            pet.move(-100, 240)
            fitted = []

            def fit(chosen):
                fitted.append(chosen)
                pet.setFixedSize(300, 400)
                bounds[0] = QRectF(80, 100, 140, 200)

            try:
                with (patch.object(QGuiApplication, "screens", return_value=[screen]),
                      patch("desktop_pet.ui.window_state.screen_for_rect", return_value=screen) as choose,
                      patch("desktop_pet.ui.window_state.work_area", side_effect=lambda item: item.area)):
                    state = WindowPlacement(pet, Path(directory) / "settings.ini", "pet",
                                            remember_size=False, fit_to_screen=fit,
                                            placement_bounds=lambda: bounds[0])
                    self.assertTrue(fitted)
                    self.assertTrue(all(item is screen for item in fitted))
                    self.assertEqual(pet.pos(), QPoint(-220, 200))
                    self.assertEqual(pet.size(), QRect(0, 0, 300, 400).size())
                    self.assertTrue(QRectF(screen.area).contains(bounds[0].translated(pet.pos())))
                    self.assertEqual(choose.call_count, len(fitted))
                    self.assertIsNotNone(state)
            finally:
                pet.deleteLater()

    def test_invalid_saved_geometry_falls_back_without_crash(self):
        with tempfile.TemporaryDirectory(prefix="aura-invalid-position-") as directory:
            path = Path(directory) / "settings.ini"
            settings = QSettings(str(path), QSettings.Format.IniFormat)
            settings.setValue("ui/windows/test", "broken rectangle")
            settings.sync()
            widget = QWidget()
            widget.resize(500, 400)
            state = WindowPlacement(widget, path, "test")
            self.assertEqual(widget.width(), 500)
            self.assertTrue(state.window is widget)
            widget.deleteLater()

    def test_large_pet_uses_its_existing_fit_hook_before_clamping_to_small_monitor(self):
        class Screen(QObject):
            availableGeometryChanged = Signal(QRect)
            def __init__(self, area):
                super().__init__()
                self.area = area
            def availableGeometry(self):
                return self.area
        screens = [Screen(QRect(0, 0, 1920, 1040)), Screen(QRect(-800, 0, 800, 580))]
        with tempfile.TemporaryDirectory(prefix="aura-mixed-monitors-") as directory:
            path = Path(directory) / "settings.ini"
            settings = QSettings(str(path), QSettings.Format.IniFormat)
            settings.setValue("ui/windows/pet", QRect(-750, 0, 720, 920))
            settings.sync()
            pet = QWidget()
            pet.setFixedSize(720, 920)
            def fit(screen):
                pet.setFixedSize(min(720, screen.area.width()), min(920, screen.area.height()))
            with patch.object(QGuiApplication, "screens", return_value=screens):
                WindowPlacement(pet, path, "pet", remember_size=False, fit_to_screen=fit)
            self.assertEqual(pet.height(), 580)
            self.assertTrue(screens[1].area.contains(pet.frameGeometry()))
            pet.deleteLater()

    def test_pet_size_change_cannot_redirect_clamping_to_an_unfitted_screen(self):
        class Screen(QObject):
            availableGeometryChanged = Signal(QRect)
            def __init__(self, area):
                super().__init__()
                self.area = area
            def availableGeometry(self):
                return self.area
        screens = [Screen(QRect(1000, 0, 320, 1000)), Screen(QRect(0, 0, 1000, 600))]
        with tempfile.TemporaryDirectory(prefix="aura-unequal-monitors-") as directory:
            path = Path(directory) / "settings.ini"
            settings = QSettings(str(path), QSettings.Format.IniFormat)
            settings.setValue("ui/windows/pet", QRect(800, 0, 320, 920))
            settings.sync()
            pet = QWidget()
            pet.setFixedSize(320, 920)
            def fit(screen):
                pet.setFixedSize(min(720, screen.area.width()), min(920, screen.area.height()))
            with patch.object(QGuiApplication, "screens", return_value=screens):
                WindowPlacement(pet, path, "pet", remember_size=False, fit_to_screen=fit)
            self.assertEqual(pet.width(), 720)
            self.assertTrue(screens[1].area.contains(pet.frameGeometry()))
            pet.deleteLater()

    def test_small_control_panel_scrolls_instead_of_squeezing_buttons_to_strips(self):
        from PySide6.QtWidgets import QPushButton, QScrollArea
        from desktop_pet.ui.debug_panel import DebugPanel
        from tests.test_debug_ui import FakePet, FakeSettings, FakeChat
        panel = DebugPanel(FakePet(), FakeSettings(), FakeChat())
        try:
            panel.setMinimumSize(500, 400)
            panel.resize(636, 476)
            panel.show()
            self.app.processEvents()
            scroll = panel.findChild(QScrollArea, "petControlsScroll")
            preset = panel.findChild(QPushButton, "scalePreset50")
            self.assertGreaterEqual(preset.height(), preset.minimumSizeHint().height())
            self.assertGreaterEqual(panel.expression_status.height(), panel.expression_status.fontMetrics().height())
            self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        finally:
            panel.hide()
            panel.deleteLater()


if __name__ == "__main__":
    unittest.main()
