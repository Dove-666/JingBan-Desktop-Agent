"""Exercise the unified character controls with temporary art and preferences.

No production preference path, system credential, or physical mouse is used.
The timer is stopped so animation bounds can be checked deterministically.
"""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPointF, QRectF, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig
from desktop_pet.expression_manager import EXPRESSIONS, ExpressionManager
from desktop_pet.pet_controller import PetController
from desktop_pet.pet_window import PetWindow
from desktop_pet.settings_manager import CredentialStore, SettingsManager


class MemoryVault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        self.values[(service, account)] = password

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class PetControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-character-controls-")
        self.root = Path(self.directory.name)
        self.original_path = self.root / "whale.png"
        self.write_image(self.original_path, 400, 200, "#378ea5")
        self.original_hash = self.file_hash(self.original_path)
        self.expression_path = self.root / "expressions"
        self.expression_path.mkdir()
        self.preferences = self.root / "preferences.ini"
        self.vault = CredentialStore(backend=MemoryVault())
        self.settings = self.new_settings()
        self.manager = ExpressionManager(self.expression_path)
        self.pet = PetWindow(PetConfig(
            image_path=self.original_path,
            window_width=240, window_height=300, character_size=220,
        ))
        self.pet.refresh_timer.stop()
        self.controller = PetController(self.pet, self.settings, self.manager)
        self.original_cache_key = self.pet._source.cacheKey()

    def tearDown(self):
        self.assertEqual(self.file_hash(self.original_path), self.original_hash)
        self.pet.close()
        self.pet.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def new_settings(self):
        return SettingsManager(
            QSettings(str(self.preferences), QSettings.Format.IniFormat), self.vault,
        )

    @staticmethod
    def file_hash(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def write_image(path, width, height, color):
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillRect(
            width // 10, height // 10, width * 8 // 10, height * 8 // 10,
            QColor(color),
        )
        painter.end()
        if not image.save(str(path), "PNG"):
            raise RuntimeError("Could not save the synthetic expression fixture")

    def assert_sprite_is_bounded_and_clickable(self):
        rect = self.pet.animated_sprite_rect
        bounds = QRectF(self.pet.rect()).adjusted(-0.001, -0.001, 0.001, 0.001)
        self.assertTrue(bounds.contains(rect), f"{rect} outside {bounds}")
        self.assertTrue(self.pet.character_contains(rect.center()))
        # The synthetic art has a transparent border; scaling must preserve it.
        transparent = QPointF(rect.left() + rect.width() * .025,
                             rect.top() + rect.height() * .025)
        self.assertFalse(self.pet.character_contains(transparent))

    def test_common_scale_steps_resize_window_and_sprite_proportionally(self):
        base_window = self.pet.size()
        base_sprite = self.pet.sprite_rect.size()
        spy = QSignalSpy(self.controller.scale_changed)
        for percent in (50, 75, 100, 125, 150, 200):
            with self.subTest(percent=percent):
                self.controller.set_scale(percent)
                factor = percent / 100
                self.assertEqual(self.controller.scale_percent, percent)
                self.assertEqual(self.pet.width(), round(base_window.width() * factor))
                self.assertEqual(self.pet.height(), round(base_window.height() * factor))
                self.assertLessEqual(abs(self.pet.sprite_rect.width() - base_sprite.width() * factor), 1)
                self.assertLessEqual(abs(self.pet.sprite_rect.height() - base_sprite.height() * factor), 1)
                self.assertLessEqual(abs(self.pet.sprite_rect.width() - 2 * self.pet.sprite_rect.height()), 1)
                self.assertEqual(self.new_settings().load_scale(), percent)
                self.assert_sprite_is_bounded_and_clickable()
                self.assertTrue(self.pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
                self.assertTrue(self.pet.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertEqual(spy.count(), 6)
        self.controller.reset_scale()
        self.assertEqual(self.pet.size(), base_window)
        self.assertEqual(self.pet.sprite_rect.size(), base_sprite)
        self.assertEqual(self.new_settings().load_scale(), 100)

    def test_actions_remain_bounded_and_hit_tested_at_every_common_scale(self):
        for percent in (50, 75, 100, 125, 150, 200):
            for code in ("happy", "surprised"):
                with self.subTest(percent=percent, expression=code):
                    self.controller.set_scale(percent)
                    self.controller.set_expression(code)
                    for _ in range(100):
                        self.pet.animation.advance(.01)
                        self.assert_sprite_is_bounded_and_clickable()
                    self.assertEqual(self.pet.animation.state, AnimationState.IDLE)

    def test_saved_scale_is_applied_by_new_controller(self):
        self.controller.set_scale(150)
        extra_pet = PetWindow(self.pet.config)
        extra_pet.refresh_timer.stop()
        try:
            restored = PetController(extra_pet, self.new_settings(), self.manager)
            self.assertEqual(restored.scale_percent, 150)
            self.assertEqual(extra_pet.size(), self.pet.size())
            self.assertEqual(extra_pet.sprite_rect.size(), self.pet.sprite_rect.size())
        finally:
            extra_pet.close()
            extra_pet.deleteLater()

    def test_invalid_scale_is_rejected_without_altering_live_or_saved_size(self):
        size = self.pet.size()
        spy = QSignalSpy(self.controller.error_occurred)
        for value in (49, 201, True, 100.5, "100"):
            self.controller.set_scale(value)
            self.assertEqual(self.pet.size(), size)
            self.assertEqual(self.controller.scale_percent, 100)
            self.assertEqual(self.new_settings().load_scale(), 100)
        self.assertEqual(spy.count(), 5)

    def test_all_missing_expression_states_are_explicit_and_keep_original_art(self):
        self.assertEqual(set(EXPRESSIONS), {
            "neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talking",
        })
        spy = QSignalSpy(self.controller.expression_changed)
        for code in EXPRESSIONS:
            with self.subTest(expression=code):
                self.controller.set_expression(code)
                self.assertEqual(self.controller.expression, code)
                self.assertIn("表情素材尚未提供", self.controller.expression_status)
                self.assertIn("未改变面部表情", self.controller.expression_status)
                self.assertEqual(self.pet._source.cacheKey(), self.original_cache_key)
        self.assertEqual(spy.count(), 7)
        self.assertEqual(len(self.controller.expression_catalog()), 7)
        self.assertEqual(set(self.manager.missing_assets()), {
            "neutral.png", "happy.png", "surprised.png", "sleepy.png", "angry.png",
            "thinking.png", "talk_open.png", "talk_closed.png",
        })

    def test_available_expression_art_loads_directly_and_is_cached(self):
        path = self.expression_path / "happy.png"
        self.write_image(path, 300, 180, "#f39871")
        expression_hash = self.file_hash(path)
        self.controller.set_expression("happy")
        first = self.pet._source
        self.assertEqual((first.width(), first.height()), (300, 180))
        self.assertTrue(first.hasAlphaChannel())
        self.assertEqual(first.toImage().pixelColor(0, 0).alpha(), 0)
        self.assertNotEqual(first.cacheKey(), self.original_cache_key)
        self.assertIn("已加载 happy.png", self.controller.expression_status)
        with patch("desktop_pet.expression_manager.QImageReader", side_effect=AssertionError("Cached art was reread")):
            self.controller.test_expression()
            self.assertEqual(self.pet._source.cacheKey(), first.cacheKey())
        self.assertEqual(self.file_hash(path), expression_hash)
        self.controller.set_expression("neutral")
        self.assertEqual(self.pet._source.cacheKey(), self.original_cache_key)

    def test_existing_neutral_art_is_applied_on_startup_without_a_bubble(self):
        path = self.expression_path / "neutral.png"
        self.write_image(path, 260, 190, "#7165c2")
        extra_pet = PetWindow(self.pet.config)
        extra_pet.refresh_timer.stop()
        base_key = extra_pet._source.cacheKey()
        try:
            controller = PetController(extra_pet, self.new_settings(), self.manager)
            self.assertEqual(controller.expression, "neutral")
            self.assertIn("已加载 neutral.png", controller.expression_status)
            self.assertNotEqual(extra_pet._source.cacheKey(), base_key)
            self.assertEqual((extra_pet._source.width(), extra_pet._source.height()), (260, 190))
            self.assertTrue(extra_pet.bubble.isHidden())
            self.assertFalse(extra_pet.bubble.hide_timer.isActive())
            self.assertEqual(extra_pet.bubble._text, "")
            self.assertEqual(extra_pet.animation.state, AnimationState.IDLE)
            # The default material is still the original PNG if neutral art is
            # removed later; constructing with an expression must not replace it.
            path.unlink()
            controller.set_expression("neutral")
            self.assertEqual(extra_pet._source.cacheKey(), base_key)
            self.assertIn("尚未提供", controller.expression_status)
        finally:
            extra_pet.close()
            extra_pet.deleteLater()

    def test_expression_can_be_added_replaced_corrupted_and_removed_while_running(self):
        path = self.expression_path / "happy.png"
        self.controller.set_expression("happy")
        self.assertIn("尚未提供", self.controller.expression_status)
        self.write_image(path, 300, 180, "#f39871")
        self.controller.test_expression()
        first_key = self.pet._source.cacheKey()
        self.assertIn("已加载", self.controller.expression_status)
        previous_time = path.stat().st_mtime_ns
        self.write_image(path, 320, 160, "#6ac69e")
        os.utime(path, ns=(previous_time + 1_000_000_000, previous_time + 1_000_000_000))
        self.controller.test_expression()
        self.assertEqual((self.pet._source.width(), self.pet._source.height()), (320, 160))
        self.assertNotEqual(self.pet._source.cacheKey(), first_key)
        path.write_bytes(b"This is a corrupt PNG fixture")
        self.controller.test_expression()
        self.assertIn("素材无法读取", self.controller.expression_status)
        self.assertEqual(self.pet._source.cacheKey(), self.original_cache_key)
        path.unlink()
        self.controller.test_expression()
        self.assertIn("尚未提供", self.controller.expression_status)
        self.assertEqual(self.pet._source.cacheKey(), self.original_cache_key)

    def test_unknown_expression_falls_back_to_neutral(self):
        self.controller.set_expression("untrusted-model-value")
        self.assertEqual(self.controller.expression, "neutral")
        self.assertEqual(self.pet._source.cacheKey(), self.original_cache_key)

    def test_clicks_route_through_controller_and_report_unified_expression(self):
        spy = QSignalSpy(self.controller.expression_changed)
        self.assertEqual(self.pet.click_handler, self.controller.set_expression)
        for expected in ("happy", "surprised"):
            center = self.pet.animated_sprite_rect.center()
            global_center = QPointF(self.pet.mapToGlobal(center.toPoint()))
            press = QMouseEvent(
                QEvent.Type.MouseButtonPress, center, global_center,
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            release = QMouseEvent(
                QEvent.Type.MouseButtonRelease, center, global_center,
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(self.pet, press)
            QApplication.sendEvent(self.pet, release)
            self.assertEqual(self.controller.expression, expected)
            self.assertEqual(spy.at(spy.count() - 1)[0], expected)
        self.assertEqual(spy.count(), 2)

    def test_expression_changes_never_unfreeze_dragging(self):
        self.pet.animation.advance(1.1)
        self.pet.animation.start_dragging()
        frozen = self.pet.animation.pose
        for code in EXPRESSIONS:
            with self.subTest(expression=code):
                self.controller.set_expression(code)
                self.pet.animation.advance(.5)
                self.assertEqual(self.controller.expression, code)
                self.assertEqual(self.pet.animation.state, AnimationState.DRAGGING)
                self.assertEqual(self.pet.animation.pose, frozen)
        self.pet.animation.finish_dragging()
        self.assertEqual(self.pet.animation.state, AnimationState.RETURNING)
        self.pet.animation.advance(1)
        self.assertEqual(self.pet.animation.state, AnimationState.IDLE)


if __name__ == "__main__":
    unittest.main()
