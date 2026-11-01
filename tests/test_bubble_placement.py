"""Independent geometry tests; no windows, monitors, input devices or network."""

import math
import unittest

from PySide6.QtCore import QPointF, QRectF, QSizeF

from desktop_pet._vendor.pyqttooltip_geometry import get_tooltip_rect
from desktop_pet.bubble_placement import SIDES, place_bubble


class BubblePlacementTests(unittest.TestCase):
    def setUp(self):
        self.available = QRectF(0, 0, 1600, 1000)
        self.speaker = QRectF(700, 450, 160, 280)
        self.size = QSizeF(280, 110)

    def assert_rect_close(self, actual, expected):
        for name in ("x", "y", "width", "height"):
            self.assertAlmostEqual(getattr(actual, name)(), getattr(expected, name)(), places=6)

    def assert_point_close(self, actual, expected):
        self.assertAlmostEqual(actual.x(), expected.x(), places=6)
        self.assertAlmostEqual(actual.y(), expected.y(), places=6)

    def test_new_speeches_vary_near_the_visible_head_and_repeated_calls_are_deterministic(self):
        placements = [place_bubble(self.available, self.speaker, self.size, sequence=index)
                      for index in range(8)]
        self.assertGreaterEqual(len({item.side for item in placements}), 5)
        for sequence, item in enumerate(placements):
            with self.subTest(sequence=sequence):
                self.assertIn(item.side, SIDES)
                self.assertTrue(self.available.contains(item.rect))
                self.assertFalse(item.rect.intersects(self.speaker))
                self.assertTrue(self.speaker.contains(item.target))
                dx = max(item.rect.left() - item.target.x(), 0, item.target.x() - item.rect.right())
                dy = max(item.rect.top() - item.target.y(), 0, item.target.y() - item.rect.bottom())
                self.assertLess(math.hypot(dx, dy), self.speaker.height() / 2)
                self.assertEqual(item, place_bubble(self.available, self.speaker, self.size, sequence=sequence))

    def test_same_speech_holds_its_side_through_small_pose_motion_but_next_speech_can_change(self):
        first = place_bubble(self.available, self.speaker, self.size, sequence=0)
        previous = first
        for delta in (1.2, -1.8, 2.4, -.7, 0):
            moved = self.speaker.translated(0, delta)
            current = place_bubble(self.available, moved, self.size, sequence=0, previous=previous)
            self.assertEqual(current.side, first.side)
            self.assertTrue(self.available.contains(current.rect))
            self.assertFalse(current.rect.intersects(moved))
            previous = current
        next_speech = place_bubble(self.available, self.speaker, self.size, sequence=1, previous=first)
        self.assertNotEqual(next_speech.side, first.side)

    def test_bottom_preference_stays_near_the_head_when_clear_but_bottom_is_available_as_a_fallback(self):
        spacious = place_bubble(QRectF(0, 0, 2400, 1600), QRectF(900, 500, 320, 560),
                                 QSizeF(400, 170), sequence=7)
        self.assertNotEqual(spacious.side, "bottom", "Variation must not force a feet-level subtitle")
        # A full-width character at the top leaves only a strip below it.
        area, speaker = QRectF(0, 0, 500, 600), QRectF(0, 0, 500, 210)
        bottom_only = place_bubble(area, speaker, QSizeF(300, 120), sequence=0)
        self.assertEqual(bottom_only.side, "bottom")
        self.assertTrue(area.contains(bottom_only.rect))
        self.assertGreaterEqual(bottom_only.rect.top(), speaker.bottom())
        self.assertFalse(bottom_only.rect.intersects(speaker))

    def test_screen_edge_invalidates_held_side_and_uses_clear_opposite_space(self):
        first = place_bubble(self.available, self.speaker, self.size, sequence=0)
        self.assertEqual(first.side, "top-right")
        near_right_edge = QRectF(1450, 450, 100, 280)
        next_placement = place_bubble(self.available, near_right_edge, self.size,
                                      sequence=0, previous=first)
        self.assertNotEqual(next_placement.side, first.side)
        self.assertTrue(self.available.contains(next_placement.rect))
        self.assertFalse(next_placement.rect.intersects(near_right_edge))
        self.assertLessEqual(next_placement.rect.right(), near_right_edge.left())

    def test_negative_monitor_coordinates_translate_position_and_tail_target_exactly(self):
        offset = QPointF(-1920, -1200)
        anchor, head = QPointF(775, 500), QRectF(735, 470, 90, 75)
        original = place_bubble(self.available, self.speaker, self.size, anchor=anchor, head=head, sequence=3)
        moved = place_bubble(self.available.translated(offset), self.speaker.translated(offset), self.size,
                             anchor=anchor + offset, head=head.translated(offset), sequence=3)
        self.assertEqual(original.side, moved.side)
        self.assert_rect_close(moved.rect, original.rect.translated(offset))
        self.assert_point_close(moved.target, original.target + offset)

    def test_scaled_character_bubble_and_tail_spacing_preserve_the_same_geometry(self):
        reference = place_bubble(self.available, self.speaker, self.size, sequence=5)
        for scale in (.5, .75, 1.25, 1.5, 2):
            with self.subTest(scale=scale):
                def rect_scaled(rect):
                    return QRectF(rect.x() * scale, rect.y() * scale,
                                  rect.width() * scale, rect.height() * scale)
                placement = place_bubble(rect_scaled(self.available), rect_scaled(self.speaker),
                                         QSizeF(self.size.width() * scale, self.size.height() * scale),
                                         gap=10 * scale, tail_margin=11 * scale, sequence=5)
                self.assertEqual(placement.side, reference.side)
                self.assert_rect_close(placement.rect, rect_scaled(reference.rect))
                self.assert_point_close(placement.target, reference.target * scale)

    def test_reserved_tray_or_popup_region_overrides_per_speech_preference(self):
        blocked_right_side = QRectF(860, 0, 740, 1000)
        preferred = place_bubble(self.available, self.speaker, self.size, sequence=0)
        self.assertTrue(preferred.rect.intersects(blocked_right_side))
        safe = place_bubble(self.available, self.speaker, self.size,
                            obstacles=(blocked_right_side,), sequence=0, previous=preferred)
        self.assertFalse(safe.rect.intersects(blocked_right_side))
        self.assertFalse(safe.rect.intersects(self.speaker))
        self.assertTrue(self.available.contains(safe.rect))

    def test_resized_bubble_and_smaller_work_area_recompute_without_losing_target_or_dimensions(self):
        first = place_bubble(self.available, self.speaker, self.size, sequence=1)
        bigger = QSizeF(440, 240)
        resized = place_bubble(self.available, self.speaker, bigger, sequence=1, previous=first)
        self.assertEqual(resized.rect.size(), bigger)
        self.assertEqual(resized.side, first.side)
        self.assertEqual(resized.target, first.target)
        smaller_work_area = QRectF(500, 400, 600, 450)
        constrained = place_bubble(smaller_work_area, self.speaker, bigger,
                                   sequence=1, previous=resized)
        self.assertTrue(smaller_work_area.contains(constrained.rect))
        self.assertEqual(constrained.rect.size(), bigger)
        self.assertTrue(constrained.constrained)
        self.assertTrue(self.speaker.contains(constrained.target))

    def test_explicit_head_anchor_is_preserved_and_invalid_anchor_is_clamped_to_visible_body(self):
        anchor = QPointF(790, 488)
        head = QRectF(745, 460, 80, 80)
        placement = place_bubble(self.available, self.speaker, self.size, anchor=anchor, head=head)
        self.assertEqual(placement.target, anchor)
        clamped = place_bubble(self.available, self.speaker, self.size, anchor=QPointF(9999, -9999))
        self.assertEqual(clamped.target, self.speaker.topRight())
        partially_outside = QRectF(-50, 400, 160, 280)
        visible_target = place_bubble(self.available, partially_outside, self.size, anchor=QPointF(-10, 450))
        self.assertTrue(partially_outside.intersected(self.available).contains(visible_target.target))

    def test_tiny_work_area_reports_unavoidable_overlap_and_oversized_input_preserves_measured_size(self):
        area, speaker = QRectF(-100, -65, 100, 65), QRectF(-75, -55, 60, 50)
        fitted = place_bubble(area, speaker, QSizeF(94, 59))
        self.assertTrue(area.contains(fitted.rect))
        self.assertTrue(fitted.constrained)
        self.assertTrue(speaker.contains(fitted.target))
        oversized = place_bubble(area, speaker, QSizeF(150, 90))
        self.assertEqual(oversized.rect.topLeft(), area.topLeft())
        self.assertEqual(oversized.rect.size(), QSizeF(150, 90))
        self.assertTrue(oversized.constrained)

    def test_empty_speaker_has_finite_fallback_but_invalid_work_area_is_rejected(self):
        fallback = place_bubble(self.available, QRectF(), self.size)
        self.assertTrue(self.available.contains(fallback.rect))
        self.assertTrue(self.available.contains(fallback.target))
        with self.assertRaises(ValueError):
            place_bubble(QRectF(), self.speaker, self.size)

    def test_adapted_upstream_cardinal_geometry_centers_wide_bubbles_using_height(self):
        reference, size = QRectF(-400, 150, 80, 120), QSizeF(300, 70)
        for side in ("left", "right"):
            with self.subTest(side=side):
                rect = get_tooltip_rect(reference, side, size, triangle_size=9)
                self.assertAlmostEqual(rect.center().y(), reference.center().y())
                self.assertEqual(rect.height(), size.height())
                self.assertEqual(rect.width(), size.width() + 9)
        above = get_tooltip_rect(reference, "top", size, triangle_size=9)
        below = get_tooltip_rect(reference, "bottom", size, triangle_size=9)
        self.assertEqual(above.center().x(), reference.center().x())
        self.assertEqual(above.bottom(), reference.top())
        self.assertEqual(below.top(), reference.bottom())
        self.assertEqual(above.size(), QSizeF(300, 79))
        self.assertEqual(below.size(), QSizeF(300, 79))


if __name__ == "__main__":
    unittest.main()
