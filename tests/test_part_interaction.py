"""Cached ear/tail interaction with synthetic art and real Qt mouse events.

All fixture PNGs are temporary. Tests never move the system pointer, open chat,
or change user preferences; regular body clicks are observed via click_handler.
"""

import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QContextMenuEvent, QImage, QMouseEvent, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_config import AnimationConfig
from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig
from desktop_pet.eye_tracking import EyeTracker
from desktop_pet.part_interaction import PartInteraction
from desktop_pet.pet_window import PetWindow


def transparent(size=QSize(1024, 1024)):
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    return image


def make_part_fixture(root):
    """Opaque body occludes roots; one ear has a genuine transparent hole."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    body = transparent()
    painter = QPainter(body)
    painter.fillRect(QRect(340, 380, 340, 500), QColor("#c8dcfa"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.white)
    for left in (390, 550):
        painter.drawEllipse(QRect(left, 470, 80, 60))
    painter.end()
    body_no_iris = body.copy()
    eyes = []
    for name, left in (("left", 390), ("right", 550)):
        iris = transparent(QSize(24, 38))
        iris.fill(QColor("#173a9c"))
        iris.save(str(root / f"{name}_iris.png"))
        aperture = transparent()
        painter = QPainter(aperture)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.white)
        painter.drawEllipse(QRect(left, 470, 80, 60))
        painter.end()
        aperture.save(str(root / f"{name}_aperture.png"))
        origin = [left + 28, 481]
        painter = QPainter(body)
        painter.drawImage(QPoint(*origin), iris)
        painter.end()
        eyes.append({"name": name, "center": [left + 40, 500], "iris": f"{name}_iris.png",
                     "iris_origin": origin, "aperture": f"{name}_aperture.png", "travel": [15, 8]})
    parts = []
    source, eye_base, cutout = transparent(), transparent(), transparent()
    for name, origin, pivot, size, color in (
            ("left_ear", [200, 320], [350, 385], QSize(160, 100), "#225599"),
            ("right_ear", [665, 320], [675, 385], QSize(160, 100), "#3366aa"),
            ("tail", [650, 650], [665, 760], QSize(180, 180), "#3377aa")):
        image = transparent(size)
        painter = QPainter(image)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        if name == "tail":
            painter.drawEllipse(image.rect())
        else:
            painter.drawRect(QRect(0, 15, 160, 65))
            if name == "left_ear":
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
                painter.fillRect(QRect(15, 25, 30, 30), Qt.GlobalColor.transparent)
        painter.end()
        image.save(str(root / f"{name}.png"))
        for target in (source, eye_base, cutout):
            painter = QPainter(target)
            painter.drawImage(QPoint(*origin), image)
            painter.end()
        parts.append({"name": name, "image": f"{name}.png", "origin": origin, "pivot": pivot,
                      "amplitude_degrees": 6 if name != "tail" else 4, "z": "behind"})
    painter = QPainter(source)
    painter.drawImage(QPoint(), body)
    painter.end()
    painter = QPainter(eye_base)
    painter.drawImage(QPoint(), body_no_iris)
    painter.end()
    source_path = root / "neutral.png"
    source.save(str(source_path))
    body.save(str(root / "base.png"))
    cutout.save(str(root / "cutout_mask.png"))
    eye_base.save(str(root / "eye_base.png"))
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    data = {"canvas": [1024, 1024], "source_sha256": source_sha,
            "base": "base.png", "cutout_mask": "cutout_mask.png", "parts": parts}
    rig_path = root / "rig.json"
    rig_path.write_text(json.dumps(data), encoding="utf-8")
    (root / "eyes.json").write_text(json.dumps({"canvas": [1024, 1024], "source_sha256": source_sha,
        "base": "eye_base.png", "eyes": eyes, "response_radius": [220, 180]}), encoding="utf-8")
    return rig_path, source_path, data


def paint_parts(interaction, *, eye_rig=None, gaze=(0, 0)):
    image = transparent()
    painter = QPainter(image)
    try:
        if not interaction.paint(painter, eye_rig=eye_rig, gaze=gaze):
            raise AssertionError("Valid interaction declined to render")
    finally:
        painter.end()
    return image


def capture(widget):
    image = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        widget.render(painter, QPoint())
    finally:
        painter.end()
    return image


def global_for_source(window, source):
    sprite, canvas = window.sprite_rect, window._source.size()
    point = QPointF(sprite.x() + source.x() * sprite.width() / canvas.width(),
                    sprite.y() + source.y() * sprite.height() / canvas.height())
    return window.character_transform.map(point) + QPointF(window.mapToGlobal(QPoint()))


def send_mouse(window, kind, global_position, *, pressed=False):
    point = QPointF(global_position)
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    event = QMouseEvent(kind, point - QPointF(window.mapToGlobal(QPoint())), point, button,
                        Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(window, event)


class PartInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-part-tests-")
        self.root = Path(self.directory.name)
        self.rig_path, self.source_path, self.data = make_part_fixture(self.root)
        self.windows = []
        self.available = QRect(-1600, -200, 4096, 3000)

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def interaction(self):
        result = PartInteraction(self.rig_path, self.source_path)
        result.bind_source(QPixmap(str(self.source_path)).toImage())
        return result

    def window(self, *, eyes=False, animated=False):
        interaction = self.interaction()
        eye_tracker = EyeTracker(self.root / "eyes.json", self.source_path) if eyes else EyeTracker(self.root / "missing.json", self.source_path)
        animation = AnimationConfig() if animated else AnimationConfig(idle_amplitude=0, breathing_amplitude=0,
                                                                       happy_amplitude=0, surprised_amplitude=0)
        with patch("desktop_pet.pet_window.PartInteraction", return_value=interaction), \
                patch("desktop_pet.pet_window.EyeTracker", return_value=eye_tracker), \
                patch("desktop_pet.pet_window.work_area", return_value=self.available):
            window = PetWindow(PetConfig(image_path=self.source_path, animation=animation))
        window.refresh_timer.stop()
        window.move(-700, 180)
        self.windows.append(window)
        return window

    def test_source_identity_and_missing_corrupt_or_stale_assets_fail_back(self):
        interaction = self.interaction()
        self.assertTrue(interaction.active)
        changed = QImage(str(self.source_path))
        changed.setPixelColor(500, 600, QColor("#ff0000"))
        interaction.bind_source(changed)
        self.assertFalse(interaction.active)
        interaction.bind_source(QPixmap(str(self.source_path)).toImage())
        self.assertTrue(interaction.active)
        for data in (None, dict(self.data, source_sha256="0" * 64), dict(self.data, base="missing.png"),
                     dict(self.data, cutout_mask="missing-mask.png")):
            with self.subTest(data=data):
                self.rig_path.write_text("broken" if data is None else json.dumps(data), encoding="utf-8")
                self.assertFalse(self.interaction().active)

    def test_alpha_holes_and_occluded_roots_do_not_swallow_body_clicks(self):
        interaction = self.interaction()
        self.assertEqual(interaction.hit_test(QPointF(260, 370)), "left_ear")
        self.assertEqual(interaction.hit_test(QPointF(760, 370)), "right_ear")
        self.assertEqual(interaction.hit_test(QPointF(770, 740)), "tail")
        for point in (QPointF(220, 360), QPointF(350, 390), QPointF(500, 600), QPointF(-10, 20)):
            self.assertFalse(interaction.hit_test(point), point)
        interaction.trigger("left_ear")
        interaction.advance(.08)
        part = next(part for part in interaction.rig.parts if part.name == "left_ear")
        moved_point = part.transform(interaction.angle("left_ear")).map(QPointF(260, 370))
        self.assertEqual(interaction.hit_test(moved_point), "left_ear")
        self.assertTrue(interaction.contains(moved_point))

    def test_motion_is_bounded_suspended_repeatable_and_recovers_exactly(self):
        interaction = self.interaction()
        neutral = paint_parts(interaction)
        self.assertEqual(neutral, QImage(str(self.source_path)).convertToFormat(neutral.format()))
        for name in ("left_ear", "right_ear", "tail"):
            amplitude = 4 if name == "tail" else 6
            interaction.trigger(name)
            self.assertEqual(paint_parts(interaction), neutral, "Trigger's zero-angle frame must have no reconstruction pop")
            peak = 0
            for _ in range(600):
                interaction.advance((.9 if name == "tail" else .65) / 600)
                peak = max(peak, abs(interaction.angle(name)))
            self.assertGreaterEqual(peak, .97 * amplitude)
            self.assertLessEqual(peak, amplitude + 1e-9)
            for _ in range(100):
                interaction.trigger(name)
                interaction.advance(.07)
                self.assertTrue(math.isfinite(interaction.angle(name)))
                self.assertLessEqual(abs(interaction.angle(name)), amplitude + 1e-9)
            before = interaction.angle(name)
            interaction.advance(1e200, suspended=True)
            self.assertEqual(interaction.angle(name), before)
            interaction.advance(1e200)
            self.assertEqual(interaction.angle(name), 0)
            self.assertFalse(interaction.animating)
            self.assertEqual(paint_parts(interaction), neutral)
        for invalid in (-.1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                interaction.advance(invalid)
        interaction.trigger("tail")
        interaction.advance(.1)
        interaction.reset()
        self.assertEqual(interaction.angle("tail"), 0)
        self.assertEqual(paint_parts(interaction), neutral)

    def test_only_target_sweep_region_changes_and_frames_do_not_read_files(self):
        interaction = self.interaction()
        before = paint_parts(interaction)
        interaction.trigger("left_ear")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("Frame disk read")), \
                patch.object(Path, "read_text", side_effect=AssertionError("Frame JSON read")):
            interaction.advance(.08)
            after = paint_parts(interaction)
        self.assertNotEqual(before, after)
        # Independent conservative sweep box, comfortably beyond the 6-degree
        # excursion of this 160x100 ear about its documented root.
        for image in (before, after):
            painter = QPainter(image)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(QRect(180, 285, 205, 150), Qt.GlobalColor.transparent)
            painter.end()
        self.assertEqual(before, after)

    def test_frame_rate_changes_do_not_change_the_same_elapsed_pose(self):
        slow, fast = self.interaction(), self.interaction()
        for interaction in (slow, fast):
            interaction.trigger("tail")
        for _ in range(9):
            slow.advance(1 / 30)
        for _ in range(18):
            fast.advance(1 / 60)
        self.assertAlmostEqual(slow.angle("tail"), fast.angle("tail"), places=10)

    def test_soft_patch_mask_keeps_opaque_body_and_preserves_blank_eye_sclera(self):
        mask = QImage(str(self.root / "cutout_mask.png"))
        # Inside the active ear's repair bbox+2, just below its unrotated
        # alpha: no opaque behind-part can hide incorrect body interpolation.
        mask.setPixelColor(350, 401, QColor(255, 255, 255, 128))
        self.assertTrue(mask.save(str(self.root / "cutout_mask.png")))
        interaction = self.interaction()
        tracker = EyeTracker(self.root / "eyes.json", self.source_path)
        tracker.bind_source(QImage(str(self.source_path)))
        self.assertTrue(tracker.active)
        interaction.trigger("left_ear")
        interaction.advance(.08)
        image = paint_parts(interaction, eye_rig=tracker.rig, gaze=(1, 0))
        # This opaque body pixel has no behind-part beneath it. A 50% patch
        # must interpolate to alpha=255, rather than punch a translucent hole.
        self.assertEqual(image.pixelColor(350, 401), QColor("#c8dcfa"))
        # The old centered iris covered this pixel. With a rightward gaze it
        # must reveal the white eye base, never the interaction base's old iris.
        self.assertEqual(image.pixelColor(420, 500), QColor(Qt.GlobalColor.white))

    def test_inactive_part_is_preserved_from_source_even_if_its_split_differs(self):
        # Model a Photoshop split's approximation at an AA seam. Clicking the
        # left ear must not reconstruct this untouched right-ear pixel at all.
        split = QImage(str(self.root / "right_ear.png"))
        split.setPixelColor(100, 40, QColor("#ff00aa"))
        self.assertTrue(split.save(str(self.root / "right_ear.png")))
        interaction = self.interaction()
        before = paint_parts(interaction)
        interaction.trigger("left_ear")
        self.assertEqual(paint_parts(interaction), before)
        interaction.advance(.08)
        after = paint_parts(interaction)
        self.assertEqual(after.pixelColor(765, 360), before.pixelColor(765, 360))
        self.assertNotEqual(after, before)

    def test_part_clicks_across_scales_use_logical_coordinates_and_leave_body_behavior(self):
        window = self.window(animated=True)
        handled = []
        parts = []
        window.click_handler = handled.append
        window.part_clicked.connect(parts.append)
        with patch("desktop_pet.pet_window.work_area", return_value=self.available), \
                patch("desktop_pet.pet_window.screen_for_point", return_value=None), \
                patch("desktop_pet.pet_window.screen_for_rect", return_value=None):
            for percent_index, percent in enumerate((50, 75, 100, 125, 150, 200)):
                window.set_scale_percent(percent)
                window.animation.advance(.13)
                for part_index, (name, source) in enumerate((("left_ear", QPointF(260, 370)), ("right_ear", QPointF(760, 370)), ("tail", QPointF(770, 740)))):
                    with self.subTest(percent=percent, name=name):
                        window._part_interaction.reset()
                        next_click = window._next_click_happy
                        point = global_for_source(window, source)
                        send_mouse(window, QEvent.Type.MouseButtonPress, point, pressed=True)
                        send_mouse(window, QEvent.Type.MouseButtonRelease, point)
                        self.assertTrue(window._part_interaction.animating)
                        window._part_interaction.advance(.08)
                        self.assertNotEqual(window._part_interaction.angle(name), 0)
                        self.assertEqual(handled, [])
                        self.assertEqual(parts[-1], name)
                        self.assertEqual(len(parts), (percent_index * 3) + part_index + 1)
                        self.assertEqual(window._next_click_happy, next_click)
                        self.assertFalse(window.bubble.isVisible())
            window._part_interaction.reset()
            point = global_for_source(window, QPointF(500, 700))
            send_mouse(window, QEvent.Type.MouseButtonPress, point, pressed=True)
            send_mouse(window, QEvent.Type.MouseButtonRelease, point)
            self.assertEqual(handled, ["happy"])
            self.assertEqual(len(parts), 18)

    def test_drag_from_part_never_triggers_and_active_motion_resumes_on_release(self):
        window = self.window()
        handled = []
        parts = []
        window.click_handler = handled.append
        window.part_clicked.connect(parts.append)
        interaction = window._part_interaction
        interaction.trigger("tail")
        interaction.advance(.1)
        frozen = interaction.angle("tail")
        start = global_for_source(window, QPointF(260, 370))
        send_mouse(window, QEvent.Type.MouseButtonPress, start, pressed=True)
        window._advance_frame(.1)
        self.assertEqual(interaction.angle("tail"), frozen)
        with patch("desktop_pet.pet_window.work_area", return_value=self.available), \
                patch("desktop_pet.pet_window.screen_for_point", return_value=None), \
                patch("desktop_pet.pet_window.screen_for_rect", return_value=None):
            end = start + QPointF(75, 40)
            send_mouse(window, QEvent.Type.MouseMove, end, pressed=True)
            self.assertTrue(window._dragged)
            send_mouse(window, QEvent.Type.MouseButtonRelease, end)
            self.assertEqual(window.animation.state, AnimationState.RETURNING)
            self.assertEqual(interaction.angle("tail"), frozen)
            window._advance_frame(.1)
        self.assertNotEqual(interaction.angle("tail"), frozen)
        self.assertEqual(interaction.angle("left_ear"), 0)
        self.assertEqual(handled, [])
        self.assertEqual(parts, [])
        self.assertFalse(window.bubble.isVisible())

    def test_unsupported_source_and_sequence_frames_bypass_parts(self):
        window = self.window()
        self.assertTrue(window.part_interaction_active)
        window.set_frame_sequence((QImage(str(self.source_path)),))
        self.assertFalse(window.part_interaction_active)
        window.clear_frame_sequence()
        self.assertTrue(window.part_interaction_active)
        original = window._source
        changed = original.toImage().copy()
        changed.setPixelColor(500, 600, QColor("#cc2233"))
        window.set_character_source(QPixmap.fromImage(changed))
        self.assertFalse(window.part_interaction_active)
        window.set_character_source(original)
        self.assertTrue(window.part_interaction_active)

    def test_sequence_clicks_emit_parts_without_rig_animation_or_painting_over_video(self):
        window = self.window()
        handled, parts = [], []
        window.click_handler = handled.append
        window.part_clicked.connect(parts.append)
        frame = QImage(str(self.source_path))
        painter = QPainter(frame)
        painter.fillRect(QRect(450, 650, 50, 60), QColor("#ff11aa"))
        painter.end()
        window.set_frame_sequence((frame,))
        window.sequence_part_hits_enabled = True
        self.assertFalse(window.part_interaction_active)
        with patch("desktop_pet.pet_window.work_area", return_value=self.available), \
                patch("desktop_pet.pet_window.screen_for_point", return_value=None), \
                patch("desktop_pet.pet_window.screen_for_rect", return_value=None), \
                patch.object(window._part_interaction, "trigger", side_effect=AssertionError("Rig must not animate video")), \
                patch.object(window._part_interaction, "paint", side_effect=AssertionError("Rig must not cover video")):
            baseline = capture(window)
            for name, source in (("left_ear", QPointF(260, 370)), ("right_ear", QPointF(760, 370)),
                                 ("tail", QPointF(770, 740))):
                with self.subTest(part=name):
                    point = global_for_source(window, source)
                    self.assertEqual(window._part_at(window.mapFromGlobal(point.toPoint())), name)
                    send_mouse(window, QEvent.Type.MouseButtonPress, point, pressed=True)
                    send_mouse(window, QEvent.Type.MouseButtonRelease, point)
                    self.assertEqual(parts[-1], name)
                    self.assertFalse(window.part_interaction_active)
                    self.assertFalse(window._part_interaction.animating)
                    window.animation.advance(1)
                    self.assertEqual(capture(window), baseline)
            self.assertEqual(parts, ["left_ear", "right_ear", "tail"])
            self.assertEqual(handled, [])
            body = global_for_source(window, QPointF(500, 700))
            send_mouse(window, QEvent.Type.MouseButtonPress, body, pressed=True)
            send_mouse(window, QEvent.Type.MouseButtonRelease, body)
            self.assertEqual(handled, ["happy"])
            self.assertEqual(parts, ["left_ear", "right_ear", "tail"])

    def test_sequence_part_hits_require_frame_alpha_static_mask_and_enabled_interaction(self):
        window = self.window()
        handled, parts = [], []
        window.click_handler = handled.append
        window.part_clicked.connect(parts.append)
        frame = QImage(str(self.source_path))
        painter = QPainter(frame)
        # A true video-frame hole in a normally solid part must pass through.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(QRect(245, 355, 40, 35), Qt.GlobalColor.transparent)
        # Conversely an opaque frame pixel cannot invent a canonical ear mask.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(QRect(215, 350, 15, 25), QColor("#ff22bb"))
        painter.end()
        window.set_frame_sequence((frame,))
        window.sequence_part_hits_enabled = True
        with patch("desktop_pet.pet_window.work_area", return_value=self.available), \
                patch("desktop_pet.pet_window.screen_for_point", return_value=None), \
                patch("desktop_pet.pet_window.screen_for_rect", return_value=None):
            hole = global_for_source(window, QPointF(260, 370))
            self.assertIsNone(window._part_at(window.mapFromGlobal(hole.toPoint())))
            send_mouse(window, QEvent.Type.MouseButtonPress, hole, pressed=True)
            self.assertIsNone(window._press_global)
            send_mouse(window, QEvent.Type.MouseButtonRelease, hole)
            self.assertEqual((parts, handled), ([], []))

            mask_hole = global_for_source(window, QPointF(220, 360))
            self.assertIsNone(window._part_at(window.mapFromGlobal(mask_hole.toPoint())))
            send_mouse(window, QEvent.Type.MouseButtonPress, mask_hole, pressed=True)
            send_mouse(window, QEvent.Type.MouseButtonRelease, mask_hole)
            self.assertEqual((parts, handled), ([], ["happy"]))
            window.animation.advance(1)

            ear = QPointF(760, 370)
            for sequence_hits, interactions in ((False, True), (True, False)):
                with self.subTest(sequence_hits=sequence_hits, interactions=interactions):
                    window.sequence_part_hits_enabled = sequence_hits
                    window.set_part_interaction_enabled(interactions)
                    point = global_for_source(window, ear)
                    self.assertIsNone(window._part_at(window.mapFromGlobal(point.toPoint())))
                    send_mouse(window, QEvent.Type.MouseButtonPress, point, pressed=True)
                    send_mouse(window, QEvent.Type.MouseButtonRelease, point)
                    self.assertEqual(parts, [])
                    window.animation.advance(1)
            self.assertEqual(handled, ["happy", "surprised", "happy"])

    def test_context_menu_toggle_restores_static_pixels_and_regular_click_behavior(self):
        window = self.window()
        handled = []
        window.click_handler = handled.append
        window.set_part_interaction_enabled(False)
        original = capture(window)
        window.set_part_interaction_enabled(True)
        window._part_interaction.trigger("tail")
        window._part_interaction.advance(.08)
        location = global_for_source(window, QPointF(500, 650)).toPoint()
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, window.mapFromGlobal(location), location)
        window.contextMenuEvent(event)
        action = next(action for action in window._menu.actions() if action.text() == "耳尾点击互动")
        self.assertTrue(action.isCheckable())
        self.assertTrue(action.isChecked())
        action.trigger()
        self.assertFalse(action.isChecked())
        self.assertFalse(window.part_interaction_active)
        self.assertFalse(window._part_interaction.animating)
        self.assertEqual(capture(window), original)
        window._menu.close()
        point = global_for_source(window, QPointF(260, 370))
        with patch("desktop_pet.pet_window.work_area", return_value=self.available), \
                patch("desktop_pet.pet_window.screen_for_point", return_value=None), \
                patch("desktop_pet.pet_window.screen_for_rect", return_value=None):
            send_mouse(window, QEvent.Type.MouseButtonPress, point, pressed=True)
            send_mouse(window, QEvent.Type.MouseButtonRelease, point)
        self.assertEqual(handled, ["happy"])
        action.trigger()
        self.assertTrue(action.isChecked())
        self.assertTrue(window.part_interaction_active)

    def test_eyes_and_parts_animate_together_without_mutating_source_or_hit_image(self):
        window = self.window(eyes=True)
        self.assertTrue(window.eye_tracking_active)
        self.assertTrue(window.part_interaction_active)
        before = capture(window)
        source_key, hit_key = window._source.cacheKey(), window._hit_image.cacheKey()
        window._part_interaction.trigger("left_ear")
        window._part_interaction.advance(.08)
        target = window._eye_tracking.rig.face_center + QPointF(2200, 0)
        window._advance_eye_tracking(.2, global_for_source(window, target))
        combined = capture(window)
        self.assertNotEqual(before, combined)
        self.assertGreater(window._eye_tracking.gaze[0], 0)
        self.assertNotEqual(window._part_interaction.angle("left_ear"), 0)
        self.assertEqual(window._source.cacheKey(), source_key)
        self.assertEqual(window._hit_image.cacheKey(), hit_key)
        window._part_interaction.reset()
        window._eye_tracking.gaze = (0, 0)
        self.assertEqual(capture(window), before)


if __name__ == "__main__":
    unittest.main()
