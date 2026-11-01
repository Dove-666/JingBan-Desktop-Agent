"""Work-area placement follows visible art, not a padded transparent canvas.

Fixtures are temporary images and mouse events are delivered directly to Qt.
These checks never move the native desktop pointer or alter saved pet settings.
"""

import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_config import AnimationConfig
from desktop_pet.config import PetConfig
from desktop_pet.config import PROJECT_ROOT
from desktop_pet.frame_animation import load_clip
from desktop_pet.pet_window import PetWindow
from desktop_pet.placement import clamp_visible_position


def alpha_bounds(image: QImage) -> QRectF:
    """Independent pixel-level oracle, used only by these regression tests."""
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    raw = memoryview(rgba.constBits())
    xmin, ymin, xmax, ymax = rgba.width(), rgba.height(), -1, -1
    for y in range(rgba.height()):
        row = raw[y * rgba.bytesPerLine():y * rgba.bytesPerLine() + rgba.width() * 4]
        occupied = [x for x, value in enumerate(row[3::4]) if value]
        if occupied:
            xmin, xmax = min(xmin, occupied[0]), max(xmax, occupied[-1])
            ymin, ymax = min(ymin, y), max(ymax, y)
    if xmax < xmin:
        return QRectF()
    return QRectF(xmin, ymin, xmax - xmin + 1, ymax - ymin + 1)


class VisiblePlacementTests(unittest.TestCase):
    def test_fractional_alpha_bounds_reach_all_four_negative_monitor_edges(self):
        area = QRect(-1920, -150, 1920, 1040)
        footprint = QRectF(90.25, 100.5, 200.5, 350.25)
        left_top = clamp_visible_position(QPoint(-10000, -10000), footprint, area)
        right_bottom = clamp_visible_position(QPoint(10000, 10000), footprint, area)
        self.assertEqual(left_top, QPoint(-2010, -250))
        self.assertEqual(right_bottom, QPoint(-291, 439))
        for pos in (left_top, right_bottom):
            placed = footprint.translated(pos)
            self.assertGreaterEqual(placed.left(), area.left())
            self.assertGreaterEqual(placed.top(), area.top())
            self.assertLessEqual(placed.right(), area.x() + area.width())
            self.assertLessEqual(placed.bottom(), area.y() + area.height())
        self.assertLess(footprint.translated(left_top).left() - area.left(), 1)
        self.assertLess(footprint.translated(left_top).top() - area.top(), 1)
        self.assertLess(area.x() + area.width() - footprint.translated(right_bottom).right(), 1)
        self.assertLess(area.y() + area.height() - footprint.translated(right_bottom).bottom(), 1)

    def test_position_inside_visible_work_area_is_preserved_even_if_window_is_outside(self):
        area = QRect(0, 0, 1920, 1040)
        footprint = QRectF(150, 80, 180, 320)
        desired = QPoint(-150, -80)
        self.assertEqual(clamp_visible_position(desired, footprint, area), desired)

    def test_empty_and_oversized_footprints_have_a_safe_deterministic_result(self):
        area = QRect(-800, 20, 800, 600)
        for footprint in (QRectF(), QRectF(10, 20, 3000, 3000)):
            with self.subTest(footprint=footprint):
                result = clamp_visible_position(QPoint(20000, 20000), footprint, area)
                self.assertIsInstance(result, QPoint)
                self.assertEqual(result, clamp_visible_position(result, footprint, area))
                self.assertTrue(math.isfinite(result.x()) and math.isfinite(result.y()))


class PetEdgeDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-edge-drag-")
        self.root = Path(self.temp.name)
        self.image_path = self.root / "padded-character-with-hole.png"
        image = self.frame(QRect(220, 180, 560, 700), hole=True)
        self.assertTrue(image.save(str(self.image_path), "PNG"))
        self.windows = []
        # A work area with a top offset and 64 px of reserved taskbar space.
        # Negative monitor coordinates deliberately expose origin assumptions.
        self.available = QRect(-1920, -150, 1920, 1016)

    def tearDown(self):
        for pet in self.windows:
            pet.close()
            pet.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    @staticmethod
    def frame(rect, *, hole=False):
        image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillRect(rect, QColor("#479fc7"))
        if hole:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(QRect(360, 360, 120, 240), Qt.GlobalColor.transparent)
        painter.end()
        return image

    def make_pet(self, *, animated=False):
        animation = AnimationConfig() if animated else AnimationConfig(
            idle_amplitude=0, breathing_amplitude=0,
            happy_amplitude=0, surprised_amplitude=0,
        )
        with patch("desktop_pet.pet_window.work_area", return_value=self.available):
            pet = PetWindow(PetConfig(image_path=self.image_path, animation=animation))
        pet.refresh_timer.stop()
        self.windows.append(pet)
        return pet

    def send(self, pet, kind, global_pos, *, pressed=False):
        button = Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton
        event = QMouseEvent(
            kind, QPointF(pet.mapFromGlobal(global_pos)), QPointF(global_pos),
            button, Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(pet, event)

    def visible_local(self, pet):
        bounds = alpha_bounds(pet._hit_image)
        bounds.translate(pet.sprite_rect.topLeft())
        return pet.character_transform.mapRect(bounds)

    def visible_point(self, pet):
        # The canvas center falls in an intentional alpha hole. This point is
        # visibly painted on the character's right side, outside that hole.
        base = QPointF(pet.sprite_rect.x() + pet._hit_image.width() * .65,
                       pet.sprite_rect.y() + pet._hit_image.height() * .55)
        point = pet.character_transform.map(base).toPoint()
        self.assertTrue(pet.character_contains(point))
        return pet.mapToGlobal(point)

    def drag_to(self, pet, destination):
        start = self.visible_point(pet)
        self.send(pet, QEvent.Type.MouseButtonPress, start, pressed=True)
        self.assertIsNotNone(pet._press_global)
        with patch("desktop_pet.pet_window.work_area", return_value=self.available), \
                patch("desktop_pet.pet_window.screen_for_point", return_value=None):
            self.send(pet, QEvent.Type.MouseMove, destination, pressed=True)
            self.assertTrue(pet._dragged)
            self.send(pet, QEvent.Type.MouseButtonRelease, destination)
        self.assertIsNone(pet._press_global)
        self.assertFalse(pet.bubble.isVisible(), "Dragging must not also count as a click")

    def test_large_canvas_transparent_margins_do_not_prevent_dragging_to_any_edge(self):
        for scale in (50, 100, 200):
            for horizontal, vertical in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                with self.subTest(scale=scale, horizontal=horizontal, vertical=vertical):
                    pet = self.make_pet()
                    with patch("desktop_pet.pet_window.work_area", return_value=self.available):
                        pet.set_scale_percent(scale)
                    pet.move(self.available.center() - pet.rect().center())
                    self.drag_to(pet, self.available.center() + QPoint(horizontal * 10000, vertical * 10000))
                    visible = self.visible_local(pet).translated(pet.pos())
                    area = QRectF(self.available)
                    self.assertTrue(area.adjusted(-1, -1, 1, 1).contains(visible))
                    x_gap = visible.left() - area.left() if horizontal < 0 else area.right() - visible.right()
                    y_gap = visible.top() - area.top() if vertical < 0 else area.bottom() - visible.bottom()
                    self.assertGreaterEqual(x_gap, -1)
                    self.assertGreaterEqual(y_gap, -1)
                    self.assertLessEqual(x_gap, 1.5)
                    self.assertLessEqual(y_gap, 1.5)
                    self.assertFalse(self.available.contains(pet.geometry()),
                                     "Only transparent padding may extend outside the work area")

    def test_placement_envelope_contains_all_default_animation_phases(self):
        pet = self.make_pet(animated=True)
        stable = QRectF(pet.character_placement_bounds)
        self.assertLess(stable.width(), pet.width() * .8)
        self.assertLess(stable.height(), pet.height() * .8)
        for trigger in (None, "play_happy", "play_surprised", "start_dragging", "finish_dragging"):
            if trigger:
                getattr(pet.animation, trigger)()
            for _ in range(80):
                pet.animation.advance(.02)
                self.assertEqual(pet.character_placement_bounds, stable)
                self.assertTrue(stable.adjusted(-1, -1, 1, 1).contains(self.visible_local(pet)),
                                (trigger, stable, self.visible_local(pet)))

    def test_transparent_hole_is_not_made_draggable_by_the_visible_bounding_box(self):
        pet = self.make_pet()
        base = QPointF(pet.sprite_rect.x() + pet._hit_image.width() * (400 / 1024),
                       pet.sprite_rect.y() + pet._hit_image.height() * (480 / 1024))
        local = pet.character_transform.map(base).toPoint()
        self.assertFalse(pet.character_contains(local))
        self.send(pet, QEvent.Type.MouseButtonPress, pet.mapToGlobal(local), pressed=True)
        self.assertIsNone(pet._press_global)
        self.assertFalse(pet._dragged)

    def test_sequence_placement_uses_cached_frame_bounds_without_cropping_or_relayout(self):
        pet = self.make_pet()
        frames = (self.frame(QRect(120, 180, 240, 650)),
                  self.frame(QRect(600, 240, 300, 620)))
        pet.set_frame_sequence(frames)
        original_size = pet._sprite.size()
        bounds = tuple(QRectF(b) for b in pet._sequence_bounds)
        first = self.visible_local(pet)
        with patch.object(pet, "_layout_character", side_effect=AssertionError("per-frame relayout")), \
                patch("desktop_pet.pet_window.load_character", side_effect=AssertionError("per-frame disk read")):
            pet.set_frame_index(1)
            second = self.visible_local(pet)
            self.assertEqual(pet.character_placement_bounds, bounds[1])
            self.assertEqual(pet._sprite.size(), original_size)
            self.assertGreater(second.left(), first.left())
            for index in (0, 1, 0, 1):
                pet.set_frame_index(index)
                self.assertEqual(pet.character_placement_bounds, bounds[index])
        self.assertNotEqual(bounds[0], bounds[1])
        for envelope, visible in zip(bounds, (first, second)):
            self.assertTrue(envelope.adjusted(-1, -1, 1, 1).contains(visible))
            self.assertLess(envelope.width(), pet.sprite_rect.width())

    def test_all_transparent_character_does_not_produce_invalid_placement(self):
        path = self.root / "empty.png"
        image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        self.assertTrue(image.save(str(path), "PNG"))
        with patch("desktop_pet.pet_window.work_area", return_value=self.available):
            pet = PetWindow(PetConfig(image_path=path))
        pet.refresh_timer.stop()
        self.windows.append(pet)
        footprint = pet.character_placement_bounds
        self.assertTrue(math.isfinite(footprint.x()) and math.isfinite(footprint.y()))
        self.assertTrue(math.isfinite(footprint.width()) and math.isfinite(footprint.height()))
        with patch("desktop_pet.pet_window.work_area", return_value=self.available):
            pet.set_scale_percent(200)
            pet._work_area_changed()
        self.assertFalse(pet.character_contains(pet.sprite_rect.center()))

    @staticmethod
    def painted_local(pet):
        """Read only this test widget's rendered RGBA, never desktop pixels."""
        image = QImage(pet.size(), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        pet.render(painter, QPoint())
        painter.end()
        return alpha_bounds(image)

    def test_real_project_clips_frozen_current_pixels_touch_every_edge_then_recover_smoothly(self):
        self.available = QRect(-1600, -200, 1600, 1000)
        maximum_legacy_gap = 0
        maximum_visible_gap = maximum_recovery_step = cases = 0
        for name in ("think_scratch_head", "snarl_cute"):
            clip = load_clip(PROJECT_ROOT / "assets" / "character" / "animations", name)
            pet = self.make_pet(animated=True)
            pet.set_frame_sequence(clip.frames, clip.manifest.anchor)
            with patch("desktop_pet.pet_window.work_area", return_value=self.available), \
                    patch("desktop_pet.pet_window.screen_for_point", return_value=None):
                for scale in (50, 100, 200):
                    pet.set_scale_percent(scale)
                    for index in (0, len(clip.frames) // 2, len(clip.frames) - 1):
                        pet.set_frame_index(index)
                        pet.animation.advance(.73)
                        for horizontal, vertical in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                            with self.subTest(clip=name, scale=scale, frame=index, x=horizontal, y=vertical):
                                pet.move(self.available.center() - pet.rect().center())
                                # Recompute from the current pose each time;
                                # idle continues smoothly between drags.
                                point = None
                                for y in range(pet._hit_image.height() // 4, pet._hit_image.height(), 3):
                                    for x in range(pet._hit_image.width() // 4, pet._hit_image.width(), 3):
                                        if pet._hit_image.pixelColor(x, y).alpha() > 200:
                                            point = pet.character_transform.map(QPointF(
                                                pet.sprite_rect.x() + x + .5, pet.sprite_rect.y() + y + .5)).toPoint()
                                            if pet.character_contains(point):
                                                break
                                            point = None
                                    if point is not None:
                                        break
                                self.assertIsNotNone(point)
                                start = pet.mapToGlobal(point)
                                self.send(pet, QEvent.Type.MouseButtonPress, start, pressed=True)
                                self.assertIsNotNone(pet._press_global)
                                frozen_index = pet._sequence_index
                                frozen_pose = pet.animation.pose
                                end = self.available.center() + QPoint(horizontal * 10000, vertical * 10000)
                                self.send(pet, QEvent.Type.MouseMove, end, pressed=True)
                                pet._advance_frame(.1)
                                self.assertEqual(pet._sequence_index, frozen_index)
                                self.assertEqual(pet.animation.pose, frozen_pose)
                                painted = self.painted_local(pet).translated(pet.pos())
                                area = QRectF(self.available)
                                self.assertTrue(area.adjusted(-1, -1, 1, 1).contains(painted))
                                gaps = (painted.left() - area.left() if horizontal < 0 else area.right() - painted.right(),
                                        painted.top() - area.top() if vertical < 0 else area.bottom() - painted.bottom())
                                self.assertLessEqual(max(gaps), 1.5, "Frozen frame retains the old union margin")
                                maximum_visible_gap = max(maximum_visible_gap, *gaps)
                                cases += 1
                                legacy_bounds = QRectF()
                                for bounds in pet._sequence_bounds:
                                    legacy_bounds = legacy_bounds.united(bounds)
                                legacy = clamp_visible_position(pet.pos(), legacy_bounds, self.available)
                                maximum_legacy_gap = max(maximum_legacy_gap, (legacy - pet.pos()).manhattanLength())
                                released = QPoint(pet.pos())
                                self.send(pet, QEvent.Type.MouseButtonRelease, end)
                                self.assertEqual(pet.pos(), released, "Release must not jump directly to the union")
                                distance = (pet._edge_recovery[1] - pet._edge_recovery[0]).manhattanLength() if pet._edge_recovery else 0
                                # smoothstep's derivative is bounded by 1.5.
                                # Allow integer rounding in both coordinates.
                                step_limit = math.ceil(1.5 * distance * .02 / pet.config.animation.return_duration) + 2
                                previous = QPoint(pet.pos())
                                for _ in range(25):
                                    pet._advance_frame(.02)
                                    painted = self.painted_local(pet).translated(pet.pos())
                                    self.assertTrue(area.adjusted(-1, -1, 1, 1).contains(painted),
                                                    "A returning painted frame crossed the taskbar/work area")
                                    step = (pet.pos() - previous).manhattanLength()
                                    maximum_recovery_step = max(maximum_recovery_step, step)
                                    self.assertLessEqual(step, step_limit,
                                                         "Smooth recovery jumped instead of easing")
                                    previous = QPoint(pet.pos())
                                safe = pet.character_placement_bounds.translated(pet.pos())
                                self.assertTrue(area.adjusted(-1, -1, 1, 1).contains(safe))
                                # Future frames follow their own safe planned
                                # placement, retaining the same resting anchor.
                                home = pet.frame_home_position
                                for next_index in (0, len(clip.frames) - 1):
                                    pet.set_frame_index(next_index)
                                    self.assertEqual(pet.frame_home_position, home)
                                    self.assertTrue(area.adjusted(-1, -1, 1, 1).contains(
                                        self.painted_local(pet).translated(pet.pos())))
                                pet.set_frame_index(index)
            pet.close()
        self.assertGreater(maximum_legacy_gap, 30, "Fixture did not exercise the reported large union padding")
        type(self).real_clip_metrics = {"cases": cases, "max_frozen_visible_edge_gap_px": maximum_visible_gap,
                                       "max_old_union_displacement_px": maximum_legacy_gap,
                                       "max_recovery_step_per_20ms_px": maximum_recovery_step}

    def test_drag_resize_on_smaller_negative_monitor_preserves_grab_and_work_area_changes_remain_safe(self):
        pet = self.make_pet(animated=True)
        start = self.visible_point(pet)
        self.send(pet, QEvent.Type.MouseButtonPress, start, pressed=True)
        old_grab = pet._drag_grab_local()
        self.available = QRect(-900, -300, 180, 220)
        destination = self.available.center()
        with patch("desktop_pet.pet_window.work_area", return_value=self.available), \
                patch("desktop_pet.pet_window.screen_for_point", return_value=None):
            self.send(pet, QEvent.Type.MouseMove, destination, pressed=True)
            self.assertNotEqual(pet._drag_grab_local(), old_grab)
            self.assertEqual(pet.pos() + pet._drag_grab_local(), destination)
            self.assertTrue(QRectF(self.available).adjusted(-1, -1, 1, 1).contains(
                self.painted_local(pet).translated(pet.pos())))
            self.available = QRect(-900, -300, 200, 210)
            pet._work_area_changed()
            self.assertTrue(QRectF(self.available).adjusted(-1, -1, 1, 1).contains(
                self.painted_local(pet).translated(pet.pos())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
