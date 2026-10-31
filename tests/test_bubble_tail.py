"""Synthetic Qt regressions for speech tails that point at their speaker.

Only the repository PNG and synthetic text are used. Monitor work areas and
screen selection are mocked; no screen pixels, input devices, or APIs are read.
"""

import math
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QImage, QMouseEvent, QPainter, QPainterPath
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.config import PetConfig
from desktop_pet.pet_window import PetWindow


SHORT_COMMENT = "鲸鱼娘正在看画面。"
LONG_COMMENT = "画面里的海浪轻轻拍着沙滩，远处还有一艘慢慢驶过的小船。" * 6


class BubbleTailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.available = QRect(0, 0, 1600, 1000)
        self.synthetic_screen = object()
        self.patches = [
            patch("desktop_pet.bubble.work_area", side_effect=lambda _: QRect(self.available)),
            patch("desktop_pet.pet_window.work_area", side_effect=lambda _: QRect(self.available)),
            patch("desktop_pet.bubble.screen_for_point", return_value=self.synthetic_screen),
            patch("desktop_pet.pet_window.screen_for_point", return_value=self.synthetic_screen),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.pet = PetWindow(PetConfig())
        self.pet.refresh_timer.stop()
        self.pet.show()
        self.app.processEvents()
        self.bubble = self.pet.bubble

    def tearDown(self):
        self.pet.close()
        self.pet.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def put_visible_center(self, x, y):
        self.pet.move(QPoint(x, y) - self.pet.visible_character_rect.center().toPoint())
        self.app.processEvents()

    def painted_global_rect(self):
        """Measure actual QWidget pixels, independently of the placement code."""
        image = QImage(self.pet.size(), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        self.pet.render(painter, QPoint())
        painter.end()
        alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
        data, stride = alpha.constBits(), alpha.bytesPerLine()
        left, top, right, bottom = image.width(), image.height(), 0, 0
        for y in range(image.height()):
            row = bytes(data[y * stride:y * stride + image.width()])
            if row.strip(b"\0"):
                left = min(left, image.width() - len(row.lstrip(b"\0")))
                right = max(right, len(row.rstrip(b"\0")))
                top, bottom = min(top, y), y + 1
        self.assertGreater(right, left)
        return QRectF(left, top, right - left, bottom - top).translated(QPointF(self.pet.mapToGlobal(QPoint())))

    def assert_tail_points_at_pet(self):
        bubble = self.bubble
        polygon = bubble._tail_polygon
        self.assertEqual(len(polygon), 3)
        first, second, tip = [QPointF(point) for point in polygon]
        base = (first + second) / 2
        direction = tip - base
        target_direction = bubble._tail_target - base
        length = math.hypot(direction.x(), direction.y())
        self.assertGreater(length, 1)
        # The direction of the triangle, extended beyond its tip, reaches the
        # character rather than a fixed point at the middle of the bubble.
        cross = direction.x() * target_direction.y() - direction.y() * target_direction.x()
        self.assertLessEqual(abs(cross) / length, 0.5)
        dot = direction.x() * target_direction.x() + direction.y() * target_direction.y()
        self.assertGreater(dot, length * length)
        global_target = bubble._tail_target + QPointF(bubble.pos())
        speaker = self.painted_global_rect()
        self.assertTrue(speaker.contains(global_target), "Tail targets transparent canvas outside the painted pet")
        self.assertLess(global_target.y(), speaker.top() + speaker.height() * .65,
                        "Tail should point to the visible head, not the feet")
        local_target = global_target - QPointF(self.pet.mapToGlobal(QPoint()))
        self.assertTrue(self.pet.character_contains(local_target), "Tail target is a transparent hole")
        _, _, expected_target = self.pet.speech_anchor_geometry()
        self.assertLessEqual(math.hypot(global_target.x() - expected_target.x(),
                                        global_target.y() - expected_target.y()), 1)

    def assert_tail_and_text_fit(self, *, allow_overlap=False):
        bubble = self.bubble
        bounds = QRectF(bubble.rect())
        self.assertTrue(self.available.contains(bubble.frameGeometry()))
        self.assertTrue(bounds.contains(bubble._body_rect))
        self.assertTrue(bubble._body_rect.contains(bubble._text_rect))
        for point in bubble._tail_polygon:
            self.assertTrue(bounds.contains(point), f"Tail point clipped: {point}")
        self.assertFalse(bubble._body_rect.contains(bubble._tail_polygon[2]))
        tail_path = QPainterPath()
        tail_path.addPolygon(bubble._tail_polygon)
        tail_path.closeSubpath()
        text_path = QPainterPath()
        text_path.addRect(bubble._text_rect)
        self.assertFalse(tail_path.intersects(text_path))
        self.assertLessEqual(bubble._layout.boundingRect().width(), bubble._text_rect.width() + 1)
        self.assertLessEqual(bubble._layout.boundingRect().height(), bubble._text_rect.height() + 1)
        if not allow_overlap:
            visible = self.painted_global_rect()
            body = bubble._body_rect.translated(QPointF(bubble.pos()))
            # Independently check whether a whole measured card can be put in
            # any clear screen slab. If all four slabs are too small, body
            # fallback is unavoidable, but the face still needs protection.
            area = QRectF(self.available)
            window = QRectF(bubble.frameGeometry())
            clear_space_exists = (
                area.left() + (body.left() - window.left()) + body.width() <= visible.left()
                or area.right() - (window.right() - body.right()) - body.width() >= visible.right()
                or area.top() + (body.top() - window.top()) + body.height() <= visible.top()
                or area.bottom() - (window.bottom() - body.bottom()) - body.height() >= visible.bottom()
            )
            if clear_space_exists:
                self.assertFalse(body.intersects(visible), "The speech body covers the pet despite clear room")
            else:
                self.assertTrue(bubble._placement.constrained)
                self.assertFalse(body.intersects(self.pet.speech_anchor_geometry()[1]),
                                 "Constrained fallback covers the speaker's face")
            dx = max(body.left() - visible.right(), visible.left() - body.right(), 0)
            dy = max(body.top() - visible.bottom(), visible.top() - body.bottom(), 0)
            self.assertLessEqual(math.hypot(dx, dy), max(14, 26 * self.bubble._scale),
                                 "Transparent canvas margins leave the bubble far from its speaker")

    def test_repeated_speeches_use_varied_nearby_positions_with_tails_pointing_to_the_head(self):
        self.put_visible_center(800, 600)
        sides = set()
        for index in range(8):
            self.bubble.say(SHORT_COMMENT, 10000)
            sides.add(self.bubble._placement.side)
            self.assert_tail_points_at_pet()
            self.assert_tail_and_text_fit()
        self.assertGreaterEqual(len(sides), 4)
        self.assertTrue(any("-" in side for side in sides), "Speech never uses a diagonal position")

    def test_head_near_top_edge_uses_available_room_without_covering_the_face(self):
        self.put_visible_center(800, 150)
        self.bubble.say(SHORT_COMMENT, 10000)
        self.assert_tail_points_at_pet()
        self.assert_tail_and_text_fit()

    def test_left_screen_edge_places_bubble_in_open_room_to_the_right(self):
        self.available = QRect(0, 0, 1600, 500)
        self.put_visible_center(100, 250)
        self.bubble.say(SHORT_COMMENT, 10000)
        self.assertGreaterEqual(self.bubble._body_rect.translated(QPointF(self.bubble.pos())).left(),
                                self.painted_global_rect().right())
        self.assert_tail_points_at_pet()
        self.assert_tail_and_text_fit()

    def test_right_screen_edge_places_bubble_in_open_room_to_the_left(self):
        self.available = QRect(0, 0, 1600, 500)
        self.put_visible_center(1500, 250)
        self.bubble.say(SHORT_COMMENT, 10000)
        self.assertLessEqual(self.bubble._body_rect.translated(QPointF(self.bubble.pos())).right(),
                             self.painted_global_rect().left())
        self.assert_tail_points_at_pet()
        self.assert_tail_and_text_fit()

    def test_screen_edge_clamp_retargets_tail_from_fixed_bubble_center(self):
        self.available = QRect(0, 0, 360, 1000)
        self.put_visible_center(100, 600)
        self.bubble.say(LONG_COMMENT, 30000)
        self.assert_tail_points_at_pet()
        self.assert_tail_and_text_fit()
        base = (self.bubble._tail_polygon[0] + self.bubble._tail_polygon[1]) / 2
        # An edge-clamped wide comment has the speaker well off its center.
        # A centered, vertical triangle is the old regression being prevented.
        tip = self.bubble._tail_polygon[2]
        centered_vertical_tail = (abs(base.x() - self.bubble.width() / 2) < 1
                                  and abs(tip.x() - base.x()) < 1)
        self.assertFalse(centered_vertical_tail)

    def test_negative_coordinate_monitor_uses_global_speaker_position(self):
        self.available = QRect(-1600, -1000, 1600, 1000)
        self.put_visible_center(-1450, -400)
        self.bubble.say(LONG_COMMENT, 30000)
        self.assert_tail_points_at_pet()
        self.assert_tail_and_text_fit()

    def test_moving_pet_recomputes_tail_without_recreating_comment(self):
        self.put_visible_center(800, 600)
        self.bubble.say(SHORT_COMMENT, 10000)
        bubble = self.bubble
        native_id = bubble.winId()
        for x, y in ((180, 600), (800, 170), (1450, 650)):
            with self.subTest(center=(x, y)):
                self.put_visible_center(x, y)
                self.assert_tail_points_at_pet()
                self.assert_tail_and_text_fit()
                self.assertEqual(bubble.winId(), native_id)
                self.assertEqual(bubble._text, SHORT_COMMENT)

    def test_animation_frames_follow_the_head_without_changing_speech_side_or_resetting_expiry(self):
        self.put_visible_center(800, 600)
        self.bubble.say(SHORT_COMMENT, 10000)
        side, sequence = self.bubble._placement.side, self.bubble._placement.sequence
        layout, expiry = self.bubble._layout, self.bubble.hide_timer.remainingTime()
        targets = []
        for _ in range(6):
            QTest.qWait(25)
            self.pet._on_frame()
            self.app.processEvents()
            self.assertEqual(self.bubble._placement.side, side)
            self.assertEqual(self.bubble._placement.sequence, sequence)
            self.assertIs(self.bubble._layout, layout)
            self.assert_tail_points_at_pet()
            targets.append(self.bubble._tail_target.y() + self.bubble.pos().y())
        self.assertGreater(max(targets) - min(targets), .05)
        self.assertLess(self.bubble.hide_timer.remainingTime(), expiry - 90)

    def test_rescaling_pet_recomputes_tail_and_preserves_text(self):
        self.put_visible_center(800, 600)
        self.bubble.say(LONG_COMMENT, 30000)
        for percent in (50, 75, 150, 200):
            with self.subTest(percent=percent):
                self.pet.set_scale_percent(percent)
                self.app.processEvents()
                self.assert_tail_points_at_pet()
                self.assert_tail_and_text_fit()
                self.assertEqual(self.bubble._text, LONG_COMMENT)

    def test_dragging_retargets_visible_comment_without_triggering_new_click(self):
        self.put_visible_center(800, 600)
        self.bubble.say(SHORT_COMMENT, 10000)
        original_position = QPoint(self.pet.pos())
        start = self.pet.speech_anchor_geometry()[2].toPoint()
        self.assertTrue(self.pet.character_contains(self.pet.mapFromGlobal(start)))
        end = start + QPoint(-100, -40)
        events = (
            (QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
        )
        for event_type, global_position, button, buttons in events:
            event = QMouseEvent(event_type, QPointF(self.pet.mapFromGlobal(global_position)),
                                QPointF(global_position), button, buttons, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(self.pet, event)
            self.app.processEvents()
        self.assertNotEqual(self.pet.pos(), original_position)
        self.assertEqual(self.bubble._text, SHORT_COMMENT)
        self.assertTrue(self.bubble.isVisible())
        self.assertIsNone(self.pet._press_global)
        self.assert_tail_points_at_pet()
        self.assert_tail_and_text_fit()

    def test_paging_reuses_cached_layouts_and_keeps_the_card_still_while_reaiming_tail(self):
        self.available = QRect(0, 0, 1600, 500)
        self.put_visible_center(1400, 250)
        text = LONG_COMMENT * 4
        self.bubble.say(text, 60000)
        self.assertGreater(len(self.bubble._pages), 1)
        self.assertEqual("".join(self.bubble._pages), text)
        original_frame, sequence = QRect(self.bubble.frameGeometry()), self.bubble._placement.sequence
        with patch.object(self.bubble, "_make_layout", side_effect=AssertionError("Pagination rebuilt text")):
            for page_index in range(len(self.bubble._pages)):
                with self.subTest(page_index=page_index):
                    self.assertEqual(self.bubble._page_index, page_index)
                    self.assert_tail_points_at_pet()
                    self.assert_tail_and_text_fit()
                    self.assertEqual(self.bubble.frameGeometry(), original_frame)
                    self.assertEqual(self.bubble._placement.sequence, sequence)
                    if page_index + 1 < len(self.bubble._pages):
                        self.bubble._next_page()

    def test_tiny_work_area_hides_tail_when_speaker_is_inside_comment(self):
        self.available = QRect(0, 0, 320, 180)
        self.put_visible_center(160, 90)
        self.bubble.say(LONG_COMMENT, 60000)
        local_speaker = self.pet.speech_anchor_geometry()[2] - QPointF(self.bubble.pos())
        self.assertTrue(QRectF(self.bubble.rect()).contains(local_speaker))
        self.assertIsNone(self.bubble._tail_side)
        self.assertEqual(len(self.bubble._tail_polygon), 0)
        self.assertTrue(self.available.contains(self.bubble.frameGeometry()))
        self.assertLessEqual(self.bubble._layout.boundingRect().height(), self.bubble._text_rect.height() + 1)


if __name__ == "__main__":
    unittest.main()
