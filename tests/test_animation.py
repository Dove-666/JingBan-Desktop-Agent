"""Deterministic animation tests; these do not depend on wall-clock timers."""

import math
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_config import AnimationConfig
from desktop_pet.animation_controller import (
    AnimationController, AnimationPose, AnimationState,
)
from desktop_pet.config import PetConfig
from desktop_pet.pet_window import PetWindow, load_character


class AnimationControllerTests(unittest.TestCase):
    def setUp(self):
        self.config = AnimationConfig()
        self.controller = AnimationController(self.config)

    def assert_pose_close(self, actual, expected, places=10):
        self.assertAlmostEqual(actual.offset_y, expected.offset_y, places=places)
        self.assertAlmostEqual(actual.scale, expected.scale, places=places)

    def assert_pose_in_envelope(self, pose):
        self.assertTrue(math.isfinite(pose.offset_y))
        self.assertTrue(math.isfinite(pose.scale))
        self.assertGreaterEqual(pose.offset_y, -self.config.max_upward_offset - 1e-9)
        self.assertLessEqual(pose.offset_y, self.config.max_downward_offset + 1e-9)
        self.assertGreater(pose.scale, 0)
        self.assertLessEqual(pose.scale, self.config.max_scale + 1e-9)

    def test_initial_pose_is_idle_and_immutable(self):
        self.assertEqual(self.controller.state, AnimationState.IDLE)
        self.assert_pose_close(self.controller.pose, AnimationPose(0.0, 1.0))
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            self.controller.pose.offset_y = 100

    def test_idle_floats_and_breathes_gently_inside_declared_bounds(self):
        poses = [self.controller.pose]
        for _ in range(math.ceil(max(self.config.idle_period, self.config.breathing_period) * 120)):
            self.controller.advance(1 / 60)
            pose = self.controller.pose
            self.assert_pose_in_envelope(pose)
            self.assertLessEqual(abs(pose.offset_y), self.config.idle_amplitude + 1e-9)
            self.assertLessEqual(abs(pose.scale - 1), self.config.breathing_amplitude + 1e-9)
            poses.append(pose)
        self.assertEqual(self.controller.state, AnimationState.IDLE)
        self.assertGreater(max(p.offset_y for p in poses), self.config.idle_amplitude * 0.9)
        self.assertLess(min(p.offset_y for p in poses), -self.config.idle_amplitude * 0.9)
        self.assertGreater(max(p.scale for p in poses) - min(p.scale for p in poses), 0.001)
        self.assertLess(max(abs(a.offset_y - b.offset_y) for a, b in zip(poses, poses[1:])), 0.1)
        self.assertLess(max(abs(a.scale - b.scale) for a, b in zip(poses, poses[1:])), 0.0002)

    def test_happy_and_surprised_jump_up_then_return_to_idle(self):
        for trigger, state, duration in (
            ("play_happy", AnimationState.HAPPY, self.config.happy_duration),
            ("play_surprised", AnimationState.SURPRISED, self.config.surprised_duration),
        ):
            with self.subTest(state=state):
                controller = AnimationController(self.config)
                before = controller.pose
                self.assertTrue(getattr(controller, trigger)())
                self.assertEqual(controller.state, state)
                self.assert_pose_close(controller.pose, before)
                poses = []
                for _ in range(120):
                    controller.advance(duration / 120)
                    self.assert_pose_in_envelope(controller.pose)
                    poses.append(controller.pose)
                self.assertLess(min(p.offset_y for p in poses), -self.config.idle_amplitude)
                controller.advance(1e-8)
                self.assertEqual(controller.state, AnimationState.IDLE)
                previous = controller.pose
                controller.advance(1e-5)
                self.assertLess(abs(controller.pose.offset_y - previous.offset_y), 0.01)
                self.assertLess(abs(controller.pose.scale - previous.scale), 0.0001)

    def test_action_interruptions_preserve_visible_pose(self):
        self.controller.advance(0.63)
        previous = self.controller.pose
        self.assertTrue(self.controller.play_happy())
        self.assert_pose_close(self.controller.pose, previous)
        self.controller.advance(self.config.happy_duration * 0.4)
        previous = self.controller.pose
        self.assertTrue(self.controller.play_surprised())
        self.assertEqual(self.controller.state, AnimationState.SURPRISED)
        self.assert_pose_close(self.controller.pose, previous)
        self.controller.advance(1e-5)
        self.assertLess(abs(self.controller.pose.offset_y - previous.offset_y), 0.01)

    def test_drag_freezes_current_pose_and_rejects_action_triggers(self):
        self.controller.play_happy()
        self.controller.advance(self.config.happy_duration * 0.4)
        previous = self.controller.pose
        self.controller.start_dragging()
        self.assertEqual(self.controller.state, AnimationState.DRAGGING)
        self.assert_pose_close(self.controller.pose, previous)
        self.assertFalse(self.controller.play_happy())
        self.assertFalse(self.controller.play_surprised())
        for seconds in (0, 0.001, 0.1, 5.0):
            self.controller.advance(seconds)
            self.assert_pose_close(self.controller.pose, previous)
            self.assertEqual(self.controller.state, AnimationState.DRAGGING)
        self.controller.start_dragging()
        self.assert_pose_close(self.controller.pose, previous)

    def test_release_blends_back_to_idle_without_a_position_jump(self):
        self.controller.play_happy()
        self.controller.advance(self.config.happy_duration * 0.3)
        self.controller.start_dragging()
        frozen = self.controller.pose
        self.controller.finish_dragging()
        self.assertEqual(self.controller.state, AnimationState.RETURNING)
        self.assert_pose_close(self.controller.pose, frozen)
        self.controller.advance(1e-5)
        self.assertLess(abs(self.controller.pose.offset_y - frozen.offset_y), 0.01)
        for _ in range(100):
            self.controller.advance(self.config.return_duration / 100)
            self.assert_pose_in_envelope(self.controller.pose)
        self.assertEqual(self.controller.state, AnimationState.IDLE)
        self.assertLessEqual(abs(self.controller.pose.offset_y), self.config.idle_amplitude + 1e-9)
        previous = self.controller.pose
        self.controller.advance(1e-5)
        self.assertLess(abs(self.controller.pose.offset_y - previous.offset_y), 0.01)

    def test_drag_can_interrupt_return_without_resetting_pose(self):
        self.controller.play_surprised()
        self.controller.advance(self.config.surprised_duration / 3)
        self.controller.start_dragging()
        self.controller.finish_dragging()
        self.controller.advance(self.config.return_duration / 2)
        previous = self.controller.pose
        self.controller.start_dragging()
        self.assert_pose_close(self.controller.pose, previous)
        self.controller.advance(2)
        self.assert_pose_close(self.controller.pose, previous)

    def test_advance_uses_elapsed_seconds_instead_of_frame_count(self):
        for action, duration in ((None, 2.0), ("play_happy", 1.9), ("play_surprised", 1.9)):
            with self.subTest(action=action):
                coarse = AnimationController(self.config)
                fine = AnimationController(self.config)
                if action:
                    getattr(coarse, action)()
                    getattr(fine, action)()
                coarse.advance(duration)
                for _ in range(190):
                    fine.advance(duration / 190)
                self.assertEqual(coarse.state, fine.state)
                self.assert_pose_close(coarse.pose, fine.pose, places=8)

    def test_long_frame_advances_past_return_to_idle(self):
        self.controller.play_happy()
        self.controller.advance(0.13)
        self.controller.start_dragging()
        self.controller.finish_dragging()
        self.controller.advance(120)
        self.assertEqual(self.controller.state, AnimationState.IDLE)
        self.assert_pose_in_envelope(self.controller.pose)
        previous = self.controller.pose
        self.controller.advance(0.1)
        self.assertNotEqual(self.controller.pose, previous)

    def test_return_has_same_result_for_fine_and_coarse_elapsed_time(self):
        coarse = AnimationController(self.config)
        fine = AnimationController(self.config)
        for controller in (coarse, fine):
            controller.play_happy()
            controller.advance(0.14)
            controller.start_dragging()
            controller.advance(2)
            controller.finish_dragging()
        coarse.advance(1.3)
        for _ in range(130):
            fine.advance(0.01)
        self.assertEqual(coarse.state, AnimationState.IDLE)
        self.assertEqual(coarse.state, fine.state)
        self.assert_pose_close(coarse.pose, fine.pose, places=8)

    def test_zero_elapsed_time_leaves_state_and_pose_unchanged(self):
        for trigger in (None, "play_happy", "play_surprised", "start_dragging"):
            with self.subTest(trigger=trigger):
                controller = AnimationController(self.config)
                if trigger:
                    getattr(controller, trigger)()
                state, pose = controller.state, controller.pose
                controller.advance(0)
                self.assertEqual(controller.state, state)
                self.assert_pose_close(controller.pose, pose)

    def test_invalid_elapsed_time_is_rejected_without_corrupting_state(self):
        self.controller.play_happy()
        self.controller.advance(0.1)
        state, pose = self.controller.state, self.controller.pose
        for seconds in (-0.1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    self.controller.advance(seconds)
                self.assertEqual(self.controller.state, state)
                self.assert_pose_close(self.controller.pose, pose)

    def test_invalid_config_floats_are_rejected_before_use(self):
        fields = (
            "idle_amplitude", "idle_period", "breathing_amplitude", "breathing_period",
            "happy_amplitude", "happy_duration", "surprised_amplitude", "surprised_duration",
            "return_duration",
        )
        for field in fields:
            for value in (float("nan"), float("inf"), -float("inf"), -1):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        replace(self.config, **{field: value})

    def test_invalid_durations_and_frame_rates_are_rejected(self):
        for field in ("idle_period", "breathing_period", "happy_duration", "surprised_duration", "return_duration"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    replace(self.config, **{field: 0})
        for value in (0, -1, 121, 1.5, True, float("nan"), float("inf")):
            with self.subTest(fps=value):
                with self.assertRaises(ValueError):
                    replace(self.config, fps=value)

    def test_zero_amplitudes_keep_neutral_pose_through_all_states(self):
        controller = AnimationController(replace(
            self.config, idle_amplitude=0, breathing_amplitude=0,
            happy_amplitude=0, surprised_amplitude=0,
        ))
        for trigger in (None, "play_happy", "play_surprised", "start_dragging", "finish_dragging"):
            if trigger:
                getattr(controller, trigger)()
            controller.advance(0.1)
            self.assert_pose_close(controller.pose, AnimationPose(0, 1))


class AnimatedPetWindowTests(unittest.TestCase):
    """Test paint/hit geometry without moving the native desktop pointer."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.assets = tempfile.TemporaryDirectory(prefix="aura-animation-tests-")
        cls.image_path = Path(cls.assets.name) / "thin-opaque-stripe.png"
        image = QImage(400, 200, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        # A thin stripe makes a stale, untransformed hit test observably wrong.
        painter.fillRect(QRect(40, 94, 320, 12), QColor("#2c8fb8"))
        painter.end()
        if not image.save(str(cls.image_path), "PNG"):
            raise RuntimeError("Could not create the animation PNG fixture")

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.windows = []
        self.config = PetConfig(image_path=self.image_path)

    def tearDown(self):
        for pet in self.windows:
            pet.close()
            pet.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def make_pet(self, config=None):
        pet = PetWindow(config or self.config)
        self.windows.append(pet)
        pet.refresh_timer.stop()
        return pet

    def send_mouse(self, pet, event_type, global_pos, button, buttons):
        event = QMouseEvent(
            event_type, QPointF(pet.mapFromGlobal(global_pos)), QPointF(global_pos),
            button, buttons, Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(pet, event)

    def test_animation_changes_drawing_rect_without_moving_window(self):
        pet = self.make_pet()
        geometry = pet.geometry()
        baseline = pet.sprite_rect
        initial = pet.animated_sprite_rect
        pet.animation.advance(0.8)
        self.assertEqual(pet.geometry(), geometry)
        self.assertEqual(pet.sprite_rect, baseline)
        self.assertNotEqual(pet.animated_sprite_rect, initial)
        self.assertAlmostEqual(
            pet.animated_sprite_rect.width() / pet.animated_sprite_rect.height(),
            baseline.width() / baseline.height(), places=10,
        )

    def test_alpha_hit_testing_follows_transformed_character(self):
        pet = self.make_pet(replace(
            self.config, animation=replace(self.config.animation, happy_amplitude=20),
        ))
        baseline_center = pet.sprite_rect.center()
        self.assertTrue(pet.character_contains(baseline_center))
        pet.animation.play_happy()
        pet.animation.advance(self.config.animation.happy_duration * 0.3)
        transformed_center = pet.character_transform.map(QPointF(baseline_center)).toPoint()
        transformed_transparent = pet.character_transform.map(QPointF(pet.sprite_rect.topLeft())).toPoint()
        self.assertGreater(abs(transformed_center.y() - baseline_center.y()), 12)
        self.assertTrue(pet.character_contains(transformed_center))
        self.assertFalse(pet.character_contains(baseline_center))
        self.assertFalse(pet.character_contains(transformed_transparent))

    def test_small_window_keeps_all_animation_poses_inside_window(self):
        pet = self.make_pet(replace(
            self.config, window_width=160, window_height=200, character_size=1000,
            animation=replace(self.config.animation, happy_amplitude=70, surprised_amplitude=60),
        ))
        bounds = QRectF(pet.rect())
        for trigger in (None, "play_happy", "play_surprised", "start_dragging", "finish_dragging"):
            if trigger:
                getattr(pet.animation, trigger)()
            for _ in range(100):
                pet.animation.advance(1 / 60)
                rendered = pet.animated_sprite_rect
                self.assertGreater(rendered.width(), 0)
                self.assertGreater(rendered.height(), 0)
                self.assertTrue(bounds.contains(rendered), (trigger, bounds, rendered))

    def test_extreme_jumps_remain_visible_in_small_window_after_interruptions(self):
        pet = self.make_pet(replace(
            self.config, window_width=160, window_height=200, character_size=1000,
            animation=replace(self.config.animation, happy_amplitude=70, surprised_amplitude=60),
        ))
        self.assertGreaterEqual(max(pet.sprite_rect.width(), pet.sprite_rect.height()), 32)
        bounds = QRectF(pet.rect())
        drawing_bounds = pet._drawing_area.adjusted(-1e-9, -1e-9, 1e-9, 1e-9)
        canvas = QImage(pet.size(), QImage.Format.Format_ARGB32)
        canvas.fill(Qt.GlobalColor.transparent)
        # Restart and replace jumps before they finish, then drag and recover.
        for trigger, frames in (
            (None, 120), ("play_happy", 12), ("play_happy", 18),
            ("play_surprised", 10), ("start_dragging", 30), ("finish_dragging", 80),
        ):
            if trigger:
                getattr(pet.animation, trigger)()
            for _ in range(frames):
                pet.animation.advance(0.01)
                rendered = pet.animated_sprite_rect
                self.assertGreaterEqual(max(rendered.width(), rendered.height()), 32)
                self.assertTrue(bounds.contains(rendered), (trigger, bounds, rendered))
                self.assertTrue(drawing_bounds.contains(rendered), (trigger, drawing_bounds, rendered))
                center = pet.character_transform.map(QPointF(pet.sprite_rect.center())).toPoint()
                painter = QPainter(canvas)
                pet.render(painter, QPoint())
                painter.end()
                self.assertGreater(canvas.pixelColor(center).alpha(), 0, (trigger, rendered, center))
        self.assertEqual(pet.animation.state, AnimationState.IDLE)

    def test_frame_updates_and_paints_keep_the_original_image_cached(self):
        with patch("desktop_pet.pet_window.load_character", wraps=load_character) as loader:
            pet = self.make_pet()
            source_key = pet._source.cacheKey()
            canvas = QImage(pet.size(), QImage.Format.Format_ARGB32)
            for _ in range(8):
                canvas.fill(Qt.GlobalColor.transparent)
                pet.animation.advance(1 / 30)
                pet._on_frame()
                painter = QPainter(canvas)
                pet.render(painter, QPoint())
                painter.end()
            self.assertEqual(loader.call_count, 1)
            self.assertEqual(pet._source.cacheKey(), source_key)

    def test_repainting_erases_vacated_pixels_to_transparent_alpha(self):
        pet = self.make_pet(replace(
            self.config, animation=replace(self.config.animation, happy_amplitude=20),
        ))
        canvas = QImage(pet.size(), QImage.Format.Format_ARGB32)
        canvas.fill(Qt.GlobalColor.transparent)
        original_center = pet.sprite_rect.center()
        painter = QPainter(canvas)
        pet.render(painter, QPoint())
        painter.end()
        self.assertEqual(canvas.pixelColor(original_center).alpha(), 255)
        pet.animation.play_happy()
        pet.animation.advance(self.config.animation.happy_duration * 0.3)
        animated_center = pet.character_transform.map(QPointF(original_center)).toPoint()
        # Reuse the canvas without clearing it: painting must erase the old pose.
        painter = QPainter(canvas)
        pet.render(painter, QPoint())
        painter.end()
        self.assertEqual(canvas.pixelColor(original_center).alpha(), 0)
        self.assertEqual(canvas.pixelColor(animated_center).alpha(), 255)

    def test_mouse_press_drag_and_release_control_animation_state(self):
        pet = self.make_pet()
        pet.animation.advance(0.6)
        start = pet.mapToGlobal(pet.character_transform.map(QPointF(pet.sprite_rect.center())).toPoint())
        self.send_mouse(pet, QEvent.Type.MouseButtonPress, start,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
        self.assertEqual(pet.animation.state, AnimationState.DRAGGING)
        frozen = pet.animation.pose
        pet.animation.advance(0.5)
        self.assertEqual(pet.animation.pose, frozen)
        destination = start + QPoint(-35, -20)
        self.send_mouse(pet, QEvent.Type.MouseMove, destination,
                        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        self.assertTrue(pet._dragged)
        self.assertEqual(pet.animation.pose, frozen)
        self.send_mouse(pet, QEvent.Type.MouseButtonRelease, destination,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
        self.assertEqual(pet.animation.state, AnimationState.RETURNING)
        self.assertFalse(pet.bubble.isVisible())
        pet.animation.advance(self.config.animation.return_duration + 0.1)
        self.assertEqual(pet.animation.state, AnimationState.IDLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
