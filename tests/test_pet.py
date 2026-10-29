"""Exercise Qt behavior with generated assets, without controlling the real mouse.

These tests do not establish Windows native click-through, focus, or z-order
behavior. Those require an interactive desktop smoke test.
"""

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import (
    QColor, QContextMenuEvent, QGuiApplication, QImage, QMouseEvent, QPainter,
)
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.config import PetConfig
from desktop_pet.pet_window import PetImageError, PetWindow, load_character
from desktop_pet.placement import clamp_position
from desktop_pet.windows_work_area import work_area


class ConfigAndPlacementTests(unittest.TestCase):
    def test_invalid_configuration_has_actionable_message(self):
        cases = (
            ({"window_width": 159}, "宽度"),
            ({"window_height": 199}, "高度"),
            ({"character_size": 31}, "角色"),
            ({"bubble_duration_ms": 99}, "气泡"),
        )
        for values, message in cases:
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, message):
                    PetConfig(**values)

    def test_negative_screen_coordinates_and_taskbar_are_respected(self):
        available = QRect(-1920, -150, 1920, 1040)
        size = QSize(360, 460)
        self.assertEqual(
            clamp_position(QPoint(-5000, -5000), size, available),
            QPoint(-1920, -150),
        )
        self.assertEqual(
            clamp_position(QPoint(5000, 5000), size, available),
            QPoint(-360, 430),
        )
        self.assertEqual(
            clamp_position(QPoint(-1000, 100), size, available),
            QPoint(-1000, 100),
        )

    def test_oversized_window_position_stays_at_work_area_origin(self):
        self.assertEqual(
            clamp_position(QPoint(2000, 2000), QSize(3000, 3000), QRect(-800, 20, 800, 600)),
            QPoint(-800, 20),
        )


class PetWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # A test closing its pet must not terminate the shared Qt application.
        cls.app.setQuitOnLastWindowClosed(False)
        cls.assets = tempfile.TemporaryDirectory(prefix="aura-pet-tests-")
        cls.image_path = Path(cls.assets.name) / "non-square-transparent.png"
        image = QImage(400, 200, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillRect(QRect(20, 20, 360, 160), QColor("#2c8fb8"))
        painter.end()
        if not image.save(str(cls.image_path), "PNG"):
            raise RuntimeError("Could not create the temporary PNG test fixture")

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.windows = []
        self.config = PetConfig(image_path=self.image_path, bubble_duration_ms=180)
        self.pet = self.make_pet(self.config)

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def make_pet(self, config):
        window = PetWindow(config)
        self.windows.append(window)
        available = work_area(QGuiApplication.primaryScreen())
        window.move(clamp_position(available.topLeft() + QPoint(100, 100), window.size(), available))
        window.show()
        self.app.processEvents()
        return window

    def wait_until(self, predicate, timeout_ms=1000):
        deadline = time.monotonic() + timeout_ms / 1000
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), f"Condition did not become true within {timeout_ms} ms")

    def mouse_event(self, event_type, global_pos, button, buttons):
        local_pos = self.pet.mapFromGlobal(global_pos)
        event = QMouseEvent(
            event_type, QPointF(local_pos), QPointF(global_pos),
            button, buttons, Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(self.pet, event)
        self.app.processEvents()

    def press(self, global_pos):
        self.mouse_event(
            QEvent.Type.MouseButtonPress, global_pos,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        )

    def move_pressed(self, global_pos):
        # QTest.mouseMove alone does not guarantee a held button in Qt 6.
        self.mouse_event(
            QEvent.Type.MouseMove, global_pos,
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        )

    def release(self, global_pos):
        self.mouse_event(
            QEvent.Type.MouseButtonRelease, global_pos,
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        )

    def click_character(self):
        point = self.pet.mapToGlobal(self.pet.sprite_rect.center())
        self.press(point)
        self.release(point)

    def test_missing_and_corrupt_images_raise_clear_errors(self):
        missing = Path(self.assets.name) / "missing.png"
        with self.assertRaisesRegex(PetImageError, "找不到桌宠图片") as caught:
            load_character(replace(self.config, image_path=missing))
        self.assertIn(str(missing), str(caught.exception))
        self.assertIn("--image", str(caught.exception))

        corrupt = Path(self.assets.name) / "corrupt.png"
        corrupt.write_bytes(b"This is not an image.")
        with self.assertRaisesRegex(PetImageError, "无法读取桌宠图片") as caught:
            load_character(replace(self.config, image_path=corrupt))
        self.assertIn(str(corrupt), str(caught.exception))

    def test_non_square_image_keeps_ratio_and_is_centered(self):
        sprite = self.pet.sprite_rect
        self.assertAlmostEqual(sprite.width() / sprite.height(), 2.0, places=2)
        self.assertEqual(sprite.center(), self.pet.rect().center())
        self.assertLessEqual(max(sprite.width(), sprite.height()), self.config.character_size)
        self.assertTrue(self.pet.rect().contains(sprite))
        self.assertTrue(self.pet.character_contains(sprite.center()))
        self.assertFalse(self.pet.character_contains(sprite.topLeft()))
        self.assertFalse(self.pet.character_contains(QPoint(0, 0)))

    def test_transparent_nonactivating_topmost_window_flags(self):
        flags = self.pet.windowFlags()
        self.assertTrue(flags & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(self.pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(self.pet.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))

    def test_short_click_shows_bubble_then_timer_hides_it(self):
        self.assertFalse(self.pet.bubble.isVisible())
        self.click_character()
        self.assertTrue(self.pet.bubble.isVisible())
        self.assertEqual(self.pet.bubble._text, self.config.bubble_text)
        self.assertTrue(self.pet.bubble.hide_timer.isActive())
        self.wait_until(lambda: not self.pet.bubble.isVisible())

    def test_repeated_click_restarts_bubble_expiry(self):
        self.pet.config = replace(self.config, bubble_duration_ms=400)
        self.click_character()
        QTest.qWait(180)
        self.click_character()
        QTest.qWait(260)
        self.assertTrue(self.pet.bubble.isVisible())
        self.wait_until(lambda: not self.pet.bubble.isVisible())

    def test_click_in_transparent_margin_does_not_show_bubble(self):
        point = self.pet.mapToGlobal(self.pet.sprite_rect.topLeft())
        self.press(point)
        self.release(point)
        self.assertFalse(self.pet.bubble.isVisible())

    def test_drag_moves_window_and_does_not_trigger_click(self):
        origin = self.pet.pos()
        start = self.pet.mapToGlobal(self.pet.sprite_rect.center())
        delta = QPoint(-40, -35)
        self.press(start)
        self.move_pressed(start + delta)
        available = work_area(QGuiApplication.primaryScreen())
        expected = clamp_position(origin + delta, self.pet.size(), available)
        self.assertEqual(self.pet.pos(), expected)
        self.assertNotEqual(self.pet.pos(), origin)
        self.release(start + delta)
        self.assertFalse(self.pet.bubble.isVisible())
        self.assertIsNone(self.pet._press_global)

    def test_drag_out_and_back_is_still_a_drag(self):
        origin = self.pet.pos()
        start = self.pet.mapToGlobal(self.pet.sprite_rect.center())
        self.press(start)
        self.move_pressed(start + QPoint(-45, -30))
        self.assertTrue(self.pet._dragged)
        self.move_pressed(start)
        self.assertEqual(self.pet.pos(), origin)
        self.release(start)
        self.assertFalse(self.pet.bubble.isVisible())

    def test_visible_bubble_does_not_block_dragging(self):
        self.click_character()
        self.assertTrue(self.pet.bubble.isVisible())
        self.assertTrue(self.pet.bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        origin = self.pet.pos()
        start = self.pet.mapToGlobal(self.pet.sprite_rect.center())
        self.press(start)
        self.move_pressed(start + QPoint(-40, -25))
        self.release(start + QPoint(-40, -25))
        self.assertNotEqual(self.pet.pos(), origin)
        self.assertTrue(self.pet.bubble.isVisible())

    def test_drag_is_clamped_to_available_work_area(self):
        available = work_area(QGuiApplication.primaryScreen())
        start = self.pet.mapToGlobal(self.pet.sprite_rect.center())
        self.press(start)
        far_corner = available.bottomRight() + QPoint(20000, 20000)
        self.move_pressed(far_corner)
        self.release(far_corner)
        self.assertTrue(available.contains(self.pet.frameGeometry()))
        self.assertEqual(self.pet.x(), available.right() - self.pet.width() + 1)
        self.assertEqual(self.pet.y(), available.bottom() - self.pet.height() + 1)
        self.assertFalse(self.pet.bubble.isVisible())

    def test_large_requested_window_is_fitted_inside_work_area(self):
        available = work_area(QGuiApplication.primaryScreen())
        window = self.make_pet(replace(
            self.config,
            window_width=available.width() + 500,
            window_height=available.height() + 500,
        ))
        self.assertEqual(window.size(), available.size())
        self.assertTrue(available.contains(window.frameGeometry()))
        self.assertTrue(window.rect().contains(window.sprite_rect))

    def test_refresh_timer_emits_actual_elapsed_ticks(self):
        spy = QSignalSpy(self.pet.frame_tick)
        self.assertTrue(self.pet.refresh_timer.isActive())
        self.wait_until(lambda: spy.count() >= 2)
        self.assertGreater(spy.at(0)[0], 0)
        self.assertGreater(spy.at(1)[0], 0)

    def test_context_menu_exit_closes_pet_and_stops_timers(self):
        self.click_character()
        local = self.pet.sprite_rect.center()
        event = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, local, self.pet.mapToGlobal(local),
        )
        QApplication.sendEvent(self.pet, event)
        self.app.processEvents()
        self.assertIsNotNone(self.pet._menu)
        exit_actions = [action for action in self.pet._menu.actions() if action.text() == "退出桌宠"]
        self.assertEqual(len(exit_actions), 1)
        exit_actions[0].trigger()
        self.app.processEvents()
        self.assertFalse(self.pet.isVisible())
        self.assertFalse(self.pet.bubble.isVisible())
        self.assertFalse(self.pet.refresh_timer.isActive())
        self.assertFalse(self.pet.bubble.hide_timer.isActive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
