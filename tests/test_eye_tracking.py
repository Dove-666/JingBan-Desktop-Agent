"""Synthetic eye rigs verify gaze math, clipping and existing window behavior."""

import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QContextMenuEvent, QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig
from desktop_pet.eye_tracking import (
    EyeTracker, TRACKED_EXPRESSION_STEMS, clamped_gaze, cursor_in_source, load_eye_rig, smooth_gaze,
)
from desktop_pet.pet_window import PetWindow


def transparent(size=QSize(1024, 1024)):
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    return image


class EyeTrackingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-eye-rig-tests-")
        self.root = Path(self.directory.name)
        self.neutral = self.root / "neutral.png"
        self.rig_path = self.root / "rig.json"
        base = transparent()
        painter = QPainter(base)
        painter.fillRect(QRect(220, 220, 580, 580), QColor("#c8dcfa"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.white)
        for left in (340, 570):
            painter.drawEllipse(QRect(left, 360, 96, 64))
        painter.end()
        self.assertTrue(base.save(str(self.root / "base.png"), "PNG"))
        neutral = base.copy()
        eyes = []
        for name, left in (("left", 340), ("right", 570)):
            iris = transparent(QSize(32, 48))
            iris.fill(QColor("#2255cc"))
            iris_name, aperture_name = f"{name}_iris.png", f"{name}_aperture.png"
            self.assertTrue(iris.save(str(self.root / iris_name), "PNG"))
            aperture = transparent()
            painter = QPainter(aperture)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(Qt.GlobalColor.white)
            painter.drawEllipse(QRect(left, 360, 96, 64))
            painter.end()
            self.assertTrue(aperture.save(str(self.root / aperture_name), "PNG"))
            origin = [left + 32, 368]
            layer = transparent()
            painter = QPainter(layer)
            painter.drawImage(QPoint(*origin), iris)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.drawImage(QPoint(), aperture)
            painter.end()
            painter = QPainter(neutral)
            painter.drawImage(QPoint(), layer)
            painter.end()
            eyes.append({"name": name, "center": [left + 48, 392], "iris": iris_name,
                         "iris_origin": origin, "aperture": aperture_name, "travel": [18, 10]})
        self.assertTrue(neutral.save(str(self.neutral), "PNG"))
        self.data = {"canvas": [1024, 1024], "source_sha256": hashlib.sha256(self.neutral.read_bytes()).hexdigest(),
                     "base": "base.png", "eyes": eyes, "response_radius": [220, 180]}
        self.write_rig(self.data)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def write_rig(self, data):
        self.rig_path.write_text(json.dumps(data), encoding="utf-8")

    def make_window(self, tracker=None, image_path=None):
        factory = (lambda: tracker) if tracker is not None else (lambda: EyeTracker(self.rig_path, self.neutral))
        with patch("desktop_pet.pet_window.EyeTracker", side_effect=factory), \
                patch("desktop_pet.pet_window.work_area", return_value=QRect(0, 0, 4096, 4096)):
            window = PetWindow(PetConfig(image_path=image_path or self.neutral))
        window.refresh_timer.stop()
        self.windows.append(window)
        return window

    def make_expression_bank(self):
        expressions, rigs = self.root / "multi" / "expressions", self.root / "multi" / "eyes"
        expressions.mkdir(parents=True)
        for index, name in enumerate(TRACKED_EXPRESSION_STEMS):
            folder = rigs / name
            folder.mkdir(parents=True)
            source, base = QImage(str(self.neutral)), QImage(str(self.root / "base.png"))
            color = QColor(40 + index * 25, 100, 180)
            source.setPixelColor(250, 250, color)
            base.setPixelColor(250, 250, color)
            self.assertTrue(source.save(str(expressions / f"{name}.png"), "PNG"))
            self.assertTrue(base.save(str(folder / "base.png"), "PNG"))
            for filename in ("left_iris.png", "right_iris.png", "left_aperture.png", "right_aperture.png"):
                shutil.copyfile(self.root / filename, folder / filename)
            data = copy.deepcopy(self.data)
            data["source_sha256"] = hashlib.sha256((expressions / f"{name}.png").read_bytes()).hexdigest()
            for eye in data["eyes"]:
                eye["travel"] = [18 - index, 10 - index]
            (folder / "rig.json").write_text(json.dumps(data), encoding="utf-8")
        # The closed mouth has identical pixels; the closed-eye happy picture
        # has a distinct fingerprint and deliberately has no candidate rig.
        shutil.copyfile(expressions / "neutral.png", expressions / "talk_closed.png")
        happy = QImage(str(expressions / "neutral.png"))
        happy.setPixelColor(250, 250, QColor("#aa00dd"))
        self.assertTrue(happy.save(str(expressions / "happy.png"), "PNG"))
        return expressions, rigs

    @staticmethod
    def tracker_for_bank(expressions, rigs):
        with patch("desktop_pet.eye_tracking.DEFAULT_RIG_PATH", rigs / "neutral" / "rig.json"), \
                patch("desktop_pet.eye_tracking.DEFAULT_NEUTRAL_PATH", expressions / "neutral.png"):
            return EyeTracker()

    @staticmethod
    def global_for_source(window, source):
        rect = window.sprite_rect
        local = QPointF(rect.x() + source.x() * rect.width() / 1024,
                        rect.y() + source.y() * rect.height() / 1024)
        return window.character_transform.map(local) + QPointF(window.mapToGlobal(QPoint()))

    def test_ellipse_clamp_and_exponential_response_are_bounded_and_frame_rate_independent(self):
        self.assertEqual(clamped_gaze(110, 0, 220, 180), (.5, 0))
        self.assertEqual(clamped_gaze(2200, 0, 220, 180), (1, 0))
        diagonal = clamped_gaze(2200, 1800, 220, 180)
        self.assertAlmostEqual(math.hypot(*diagonal), 1)
        self.assertAlmostEqual(diagonal[0], diagonal[1])
        at_30, at_60 = (0, 0), (0, 0)
        for _ in range(30):
            at_30 = smooth_gaze(at_30, diagonal, 1 / 30)
        for _ in range(60):
            at_60 = smooth_gaze(at_60, diagonal, 1 / 60)
        for actual, expected in zip(at_30, at_60):
            self.assertAlmostEqual(actual, expected, places=12)
        self.assertLessEqual(math.hypot(*at_30), 1)
        with self.assertRaises(ValueError):
            smooth_gaze((0, 0), (1, 0), -1)

    def test_cursor_inverse_uses_logical_geometry_with_pose_scale_and_negative_monitor_origin(self):
        rect, canvas = QRectF(17, 49, 320, 320), QSize(1024, 1024)
        pose = QTransform()
        pose.translate(100, -27)
        pose.scale(1.13, 1.13)
        origin, expected = QPointF(-1440, 220), QPointF(618.5, 392.25)
        local = QPointF(rect.x() + expected.x() * rect.width() / 1024,
                        rect.y() + expected.y() * rect.height() / 1024)
        result = cursor_in_source(pose.map(local) + origin, origin, rect, pose, canvas)
        self.assertAlmostEqual(result.x(), expected.x(), places=9)
        self.assertAlmostEqual(result.y(), expected.y(), places=9)
        self.assertIsNone(cursor_in_source(origin, origin, QRectF(), pose, canvas))

    def test_missing_corrupt_and_stale_rigs_fail_back_without_replacing_art(self):
        self.assertIsNone(load_eye_rig(self.root / "missing.json", self.neutral))
        self.rig_path.write_text("not json", encoding="utf-8")
        self.assertIsNone(load_eye_rig(self.rig_path, self.neutral))
        changed = dict(self.data, source_sha256="0" * 64)
        self.write_rig(changed)
        self.assertIsNone(load_eye_rig(self.rig_path, self.neutral))
        changed = dict(self.data, base="missing-base.png")
        self.write_rig(changed)
        self.assertIsNone(load_eye_rig(self.rig_path, self.neutral))
        tracker = EyeTracker(self.rig_path, self.neutral)
        tracker.bind_source(QImage(str(self.neutral)))
        self.assertFalse(tracker.active)

    def test_soft_alpha_and_invisible_rgb_match_the_windows_pixmap_representation(self):
        straight = QImage(str(self.neutral)).convertToFormat(QImage.Format.Format_RGBA8888)
        straight.setPixelColor(100, 100, QColor(123, 45, 67, 72))
        straight.setPixelColor(101, 100, QColor(200, 140, 70, 0))
        self.assertTrue(straight.save(str(self.neutral), "PNG"))
        self.data["source_sha256"] = hashlib.sha256(self.neutral.read_bytes()).hexdigest()
        self.write_rig(self.data)
        tracker = EyeTracker(self.rig_path, self.neutral)
        self.assertIsNotNone(tracker.rig)
        tracker.bind_source(QPixmap.fromImage(straight).toImage())
        self.assertTrue(tracker.active)

    def test_cached_render_reconstructs_center_and_changes_only_clipped_eye_pixels(self):
        tracker = EyeTracker(self.rig_path, self.neutral)
        tracker.bind_source(QImage(str(self.neutral)))
        self.assertTrue(tracker.active)
        rig = tracker.rig

        def render(gaze):
            image = transparent()
            painter = QPainter(image)
            rig.paint(painter, gaze)
            painter.end()
            return image

        center = render((0, 0))
        self.assertEqual(center, QImage(str(self.neutral)).convertToFormat(center.format()))
        # Repeated frame updates and painting cannot read images, JSON or hash
        # pixels; they use only the rig and small cached aperture buffers.
        with patch("desktop_pet.eye_tracking._read_image", side_effect=AssertionError("Frame I/O")), \
                patch("desktop_pet.eye_tracking.image_fingerprint", side_effect=AssertionError("Frame hash")):
            tracker.advance(.2, rig.face_center + QPointF(2200, -1800))
            moved = render(tracker.gaze)
        self.assertNotEqual(center, moved)
        outside_center, outside_moved = center.copy(), moved.copy()
        for image in (outside_center, outside_moved):
            painter = QPainter(image)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            for eye in rig.eyes:
                painter.fillRect(QRect(eye.aperture_origin, eye.aperture.size()), Qt.GlobalColor.transparent)
            painter.end()
        self.assertEqual(outside_center, outside_moved)
        for eye in rig.eyes:
            for y in range(eye.aperture.height()):
                for x in range(eye.aperture.width()):
                    if eye.aperture.pixelColor(x, y).alpha() == 0:
                        self.assertEqual(eye._layer.pixelColor(x, y).alpha(), 0)

    def test_window_tracks_across_scales_and_pose_without_altering_hit_or_source_images(self):
        window = self.make_window()
        self.assertTrue(window.eye_tracking_active)
        tracker = window._eye_tracking
        for percent in (50, 75, 100, 125, 150, 200):
            with self.subTest(scale=percent), \
                    patch("desktop_pet.pet_window.work_area", return_value=QRect(0, 0, 4096, 4096)):
                window.set_scale_percent(percent)
                window.animation.advance(.13)
                tracker.set_enabled(True)
                source_key, hit_key = window._source.cacheKey(), window._hit_image.cacheKey()
                bounds = window.character_placement_bounds
                target = tracker.rig.face_center + QPointF(tracker.rig.response_radius.x(), 0)
                window._advance_eye_tracking(.25, self.global_for_source(window, target))
                self.assertAlmostEqual(tracker.gaze[0], 1 - math.exp(-3), places=8)
                self.assertAlmostEqual(tracker.gaze[1], 0, places=8)
                self.assertEqual(window._source.cacheKey(), source_key)
                self.assertEqual(window._hit_image.cacheKey(), hit_key)
                self.assertEqual(window.character_placement_bounds, bounds)

    def test_other_art_toggle_sequence_and_drag_suspend_tracking(self):
        window = self.make_window()
        tracker = window._eye_tracking
        right = tracker.rig.face_center + QPointF(2200, 0)
        left = tracker.rig.face_center - QPointF(2200, 0)
        window._advance_eye_tracking(.2, self.global_for_source(window, right))
        frozen = tracker.gaze
        window.animation.start_dragging()
        window._advance_eye_tracking(1, self.global_for_source(window, left))
        self.assertEqual(tracker.gaze, frozen)
        window.animation.finish_dragging()
        self.assertEqual(window.animation.state, AnimationState.RETURNING)
        window._advance_eye_tracking(1, self.global_for_source(window, left))
        self.assertEqual(tracker.gaze, frozen)
        window.animation.advance(1)
        window._advance_eye_tracking(.2, self.global_for_source(window, left))
        self.assertLess(tracker.gaze[0], 0)
        window.set_frame_sequence((QImage(str(self.neutral)),))
        self.assertFalse(window.eye_tracking_active)
        sequence_gaze = tracker.gaze
        window._advance_eye_tracking(1, self.global_for_source(window, right))
        self.assertEqual(tracker.gaze, sequence_gaze)
        window.clear_frame_sequence()
        self.assertTrue(window.eye_tracking_active)
        window.set_eye_tracking_enabled(False)
        self.assertFalse(window.eye_tracking_active)
        window.set_eye_tracking_enabled(True)
        self.assertTrue(window.eye_tracking_active)
        original = window._source
        different = original.toImage().copy()
        different.setPixelColor(250, 250, QColor("#ff3322"))
        window.set_character_source(QPixmap.fromImage(different))
        self.assertFalse(window.eye_tracking_active)
        window.set_character_source(original)
        self.assertTrue(window.eye_tracking_active)

    def test_expression_rigs_are_selected_from_cached_pixels_and_closed_mouth_reuses_neutral(self):
        expressions, rigs = self.make_expression_bank()
        with patch("desktop_pet.eye_tracking.load_eye_rig", wraps=load_eye_rig) as load:
            tracker = self.tracker_for_bank(expressions, rigs)
        self.assertEqual(load.call_count, len(TRACKED_EXPRESSION_STEMS))
        self.assertEqual({Path(call.args[1]).stem for call in load.call_args_list}, set(TRACKED_EXPRESSION_STEMS))
        self.assertEqual(set(tracker.rigs), set(TRACKED_EXPRESSION_STEMS))
        sources = {path.stem: QPixmap(str(path)).toImage() for path in expressions.glob("*.png")}
        with patch("desktop_pet.eye_tracking.load_eye_rig", side_effect=AssertionError("Switch rig I/O")), \
                patch("desktop_pet.eye_tracking._read_image", side_effect=AssertionError("Switch image I/O")):
            tracker.bind_source(sources["neutral"])
            tracker.advance(.15, tracker.rig.face_center + QPointF(2200, 0))
            direction = tracker.gaze
            for index, name in enumerate(TRACKED_EXPRESSION_STEMS):
                tracker.bind_source(sources[name])
                self.assertTrue(tracker.active, name)
                self.assertEqual(tracker.rig_name, name)
                self.assertIs(tracker.rig, tracker.rigs[name])
                self.assertEqual(tracker.rig.eyes[0].travel, QPointF(18 - index, 10 - index))
                self.assertEqual(tracker.gaze, direction)
            tracker.bind_source(sources["talk_closed"])
            self.assertEqual(tracker.rig_name, "neutral")
            self.assertIs(tracker.rig, tracker.rigs["neutral"])
            self.assertEqual(tracker.gaze, direction)
            tracker.bind_source(sources["happy"])
            self.assertFalse(tracker.active)
            self.assertIsNone(tracker.rig)
            self.assertTrue(tracker.has_rigs)
            tracker.bind_source(sources["talk_open"])
            self.assertEqual(tracker.gaze, direction)

    def test_one_bad_expression_rig_cannot_disable_others_or_redirect_source_provenance(self):
        expressions, rigs = self.make_expression_bank()
        (rigs / "surprised" / "rig.json").write_text("broken JSON", encoding="utf-8")
        sleepy_path = rigs / "sleepy" / "rig.json"
        sleepy = json.loads(sleepy_path.read_text(encoding="utf-8"))
        sleepy["source_sha256"] = hashlib.sha256((expressions / "neutral.png").read_bytes()).hexdigest()
        sleepy["source_file"] = str(expressions / "neutral.png")
        sleepy_path.write_text(json.dumps(sleepy), encoding="utf-8")
        (rigs / "angry" / "rig.json").unlink()
        # A valid rig remains tied to its explicit source, despite unrelated
        # source_file metadata. It cannot borrow a different PNG's hash.
        thinking_path = rigs / "thinking" / "rig.json"
        thinking = json.loads(thinking_path.read_text(encoding="utf-8"))
        thinking["source_file"] = "../../untrusted/elsewhere.png"
        thinking_path.write_text(json.dumps(thinking), encoding="utf-8")
        tracker = self.tracker_for_bank(expressions, rigs)
        self.assertEqual(set(tracker.rigs), {"neutral", "thinking", "talk_open"})
        for name in ("neutral", "surprised", "sleepy", "angry", "thinking", "talk_open", "neutral"):
            tracker.bind_source(QImage(str(expressions / f"{name}.png")))
            self.assertEqual(tracker.active, name in {"neutral", "thinking", "talk_open"}, name)

    def test_global_toggle_stays_available_on_closed_eyes_and_multiple_rigs_obey_drag_sequence_gates(self):
        expressions, rigs = self.make_expression_bank()
        tracker = self.tracker_for_bank(expressions, rigs)
        window = self.make_window(tracker, expressions / "neutral.png")
        window._advance_eye_tracking(.2, self.global_for_source(window, tracker.rig.face_center + QPointF(2200, 0)))
        direction = tracker.gaze
        window.set_character_source(QPixmap(str(expressions / "surprised.png")))
        self.assertEqual(tracker.rig_name, "surprised")
        self.assertEqual(tracker.gaze, direction)
        window.set_eye_tracking_enabled(False)
        window.set_character_source(QPixmap(str(expressions / "happy.png")))
        self.assertFalse(window.eye_tracking_active)
        self.assertFalse(tracker.enabled)
        self.assertTrue(tracker.has_rigs)
        # The global opt-out remains reachable while the current expression
        # has no eye rig. Trigger the real checkable action with synthetic Qt
        # coordinates; no physical pointer movement is needed.
        location = self.global_for_source(window, QPointF(250, 250)).toPoint()
        menu_event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, window.mapFromGlobal(location), location)
        window.contextMenuEvent(menu_event)
        toggles = [action for action in window._menu.actions()
                   if action.isCheckable() and action.text() == "眼睛跟随鼠标"]
        self.assertEqual(len(toggles), 1)
        self.assertFalse(toggles[0].isChecked())
        toggles[0].trigger()
        self.assertTrue(tracker.enabled)
        self.assertFalse(window.eye_tracking_active)
        window._menu.close()
        window.set_character_source(QPixmap(str(expressions / "angry.png")))
        self.assertTrue(window.eye_tracking_active)
        self.assertEqual(tracker.rig_name, "angry")
        window._advance_eye_tracking(.2, self.global_for_source(window, tracker.rig.face_center + QPointF(2200, 0)))
        frozen = tracker.gaze
        window.animation.start_dragging()
        window._advance_eye_tracking(1, self.global_for_source(window, tracker.rig.face_center - QPointF(2200, 0)))
        self.assertEqual(tracker.gaze, frozen)
        window.animation.finish_dragging()
        window._advance_eye_tracking(1, self.global_for_source(window, tracker.rig.face_center - QPointF(2200, 0)))
        self.assertEqual(tracker.gaze, frozen)
        window.animation.advance(1)
        window.set_frame_sequence((QImage(str(expressions / "talk_open.png")),))
        self.assertFalse(window.eye_tracking_active)
        window._advance_eye_tracking(1, self.global_for_source(window, tracker.rig.face_center - QPointF(2200, 0)))
        self.assertEqual(tracker.gaze, frozen)
        window.clear_frame_sequence()
        self.assertTrue(window.eye_tracking_active)
        self.assertEqual(tracker.rig_name, "angry")
        self.assertEqual(tracker.gaze, frozen)


if __name__ == "__main__":
    unittest.main()
