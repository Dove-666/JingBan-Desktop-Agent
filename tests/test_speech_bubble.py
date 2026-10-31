"""Real Qt regressions for readable, complete, independently placed comments.

These tests use only the local PNG and synthetic text, never the user's API,
credentials, mouse, or screen contents.
"""

import math
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QTextBoundaryFinder, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.config import PetConfig
from desktop_pet.pet_controller import PetController
from desktop_pet.pet_window import PetWindow
from desktop_pet.windows_work_area import work_area


COMMENT_80 = ("画面里的小猫正追着蓝色玩具跑来跑去，看起来玩得特别开心。" * 4)[:80]
COMMENT_160 = ("这段视频里的海浪轻轻拍打沙滩，远处还有一艘慢慢驶过的小船，蓝色的海水看起来很清爽。" * 5)[:160]


class LocalScaleSettings:
    """Only the PetController scale protocol; no disk or OS credential access."""

    def load_scale(self):
        return 100

    def save_scale(self, value):
        self.value = value


class SpeechBubbleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
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

    def assert_complete_layout(self, text, *, full_text=None):
        """Every input character has a visible line and measured drawing room."""
        bubble = self.bubble
        self.assertEqual(bubble._text, text if full_text is None else full_text)
        self.assertEqual(bubble._layout.text(), text.replace("\n", "\u2028"))
        self.assertGreater(bubble._layout.lineCount(), 0)
        lines = [bubble._layout.lineAt(i) for i in range(bubble._layout.lineCount())]
        self.assertEqual(lines[0].textStart(), 0)
        # QTextLine reports UTF-16 offsets, including two code units for emoji.
        code_units = len(text.encode("utf-16-le")) // 2
        self.assertEqual(lines[-1].textStart() + lines[-1].textLength(), code_units)
        self.assertEqual(sum(line.textLength() for line in lines), code_units)
        drawing_room = bubble._text_rect
        self.assertTrue(QRectF(bubble.rect()).contains(drawing_room))
        self.assertLessEqual(bubble._layout.boundingRect().width(), drawing_room.width() + 1)
        self.assertLessEqual(bubble._layout.boundingRect().height(), drawing_room.height() + 1)
        for line in lines:
            self.assertTrue(line.isValid())
            self.assertLessEqual(line.naturalTextWidth(), drawing_room.width() + 1)

    def assert_complete_comment(self, text, *, available=None):
        """Inspect every displayed page and preserve the entire source string."""
        pages = list(self.bubble._pages)
        self.assertEqual("".join(pages), text)
        finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, text)
        consumed = 0
        for index, page in enumerate(pages):
            with self.subTest(page=index):
                self.assertEqual(self.bubble._page_index, index)
                self.assert_complete_layout(page, full_text=text)
                consumed += len(page.encode("utf-16-le")) // 2
                finder.setPosition(consumed)
                self.assertTrue(finder.isAtBoundary(), "A page splits an emoji or combining sequence")
                if available is not None:
                    self.assertTrue(available.contains(self.bubble.frameGeometry()))
                if index + 1 < len(pages):
                    self.bubble._next_page()

    def test_readable_font_and_bubble_budget_are_independent_of_character_scale(self):
        for percent in (50, 75, 100, 125, 150, 200):
            with self.subTest(scale_percent=percent):
                self.pet.set_scale_percent(percent)
                self.bubble.say(COMMENT_80, 10000)
                self.app.processEvents()
                self.assertEqual(self.bubble.font().pointSizeF(), 14)
                self.assertLessEqual(self.bubble.width(), 300)
                self.assertLessEqual(self.bubble.height(), 230)
                self.assert_complete_comment(COMMENT_80)

    def test_eighty_and_one_hundred_sixty_chinese_characters_are_not_truncated(self):
        self.assertEqual(len(COMMENT_80), 80)
        self.assertEqual(len(COMMENT_160), 160)
        for text in (COMMENT_80, COMMENT_160):
            with self.subTest(length=len(text)):
                self.bubble.say(text, 10000)
                self.app.processEvents()
                self.assert_complete_comment(text)
                self.assertGreater(self.bubble._layout.lineCount(), 1)

    def test_comment_height_grows_with_wrapped_text(self):
        self.bubble.say("你好呀！", 10000)
        short_height = self.bubble.height()
        self.bubble.say(COMMENT_160, 10000)
        self.assertGreater(self.bubble.height(), short_height)
        self.assert_complete_comment(COMMENT_160)

    def test_comment_window_is_transparent_and_never_accepts_mouse_or_focus(self):
        self.bubble.say(COMMENT_80, 10000)
        flags = self.bubble.windowFlags()
        self.assertTrue(self.bubble.isWindow())
        self.assertIs(self.bubble.parentWidget(), self.pet)
        self.assertTrue(flags & Qt.WindowType.Tool)
        self.assertTrue(flags & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(flags & Qt.WindowType.WindowTransparentForInput)
        self.assertTrue(self.bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertTrue(self.bubble.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(self.bubble.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))

    def test_linked_comment_opens_its_conversation_and_ordinary_feedback_restores_passthrough(self):
        spy = QSignalSpy(self.bubble.conversation_requested)
        self.bubble.say("打开这条评论", 10000,
                        conversation_link={"conversation_id": "local-conversation", "message_id": "local-message"})
        self.assertFalse(self.bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertFalse(self.bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.assertTrue(self.bubble.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        point = self.bubble.rect().center()
        event = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(point),
                            QPointF(self.bubble.mapToGlobal(point)), Qt.MouseButton.LeftButton,
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.bubble, event)
        self.assertEqual(spy.count(), 1)
        self.assertEqual(spy.at(0), ["local-conversation", "local-message"])
        self.bubble.say("普通反馈", 10000)
        self.assertTrue(self.bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertTrue(self.bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput)

    def test_speaking_does_not_move_or_shrink_the_character(self):
        original_window = QRect(self.pet.frameGeometry())
        original_sprite = QRect(self.pet.sprite_rect)
        global_sprite = self.pet.mapToGlobal(original_sprite.topLeft())
        self.bubble.say(COMMENT_160, 10000)
        self.app.processEvents()
        self.assertEqual(self.pet.frameGeometry(), original_window)
        self.assertEqual(self.pet.sprite_rect, original_sprite)
        self.assertEqual(self.pet.mapToGlobal(self.pet.sprite_rect.topLeft()), global_sprite)

    def test_moving_and_scaling_pet_repositions_an_existing_comment(self):
        self.pet.move(300, 300)
        self.app.processEvents()
        self.bubble.say(COMMENT_80, 10000)
        previous = QRect(self.bubble.frameGeometry())
        self.pet.move(self.pet.pos() + QPoint(70, 25))
        self.app.processEvents()
        self.assertNotEqual(self.bubble.frameGeometry(), previous)
        self.assertEqual(self.bubble._text, COMMENT_80)
        self.assertTrue(self.bubble.isVisible())
        self.pet.set_scale_percent(75)
        self.app.processEvents()
        self.assertTrue(self.bubble.isVisible())
        self.assert_complete_comment(COMMENT_80)
        available = work_area(self.pet.screen())
        self.assertTrue(available.contains(self.bubble.frameGeometry()))

    def test_top_edge_uses_room_below_or_beside_the_character(self):
        available = QRect(0, 0, 1280, 900)
        with patch("desktop_pet.bubble.work_area", return_value=available):
            self.pet.move(460, round(8 - self.pet.visible_character_rect.top()))
            self.app.processEvents()
            self.bubble.say(COMMENT_160, 10000)
            self.app.processEvents()
            self.assertTrue(available.contains(self.bubble.frameGeometry()))
            visible = self.pet.visible_character_rect.translated(QPointF(self.pet.mapToGlobal(QPoint())))
            body = self.bubble._body_rect.translated(QPointF(self.bubble.pos()))
            self.assertFalse(visible.intersects(body))
            self.assert_complete_comment(COMMENT_160)

    def test_negative_coordinate_monitor_clamps_the_whole_bubble(self):
        available = QRect(-1600, -900, 1600, 860)
        with patch("desktop_pet.bubble.work_area", return_value=available):
            visible = self.pet.visible_character_rect
            self.pet.move(round(available.left() + 10 - visible.left()),
                          round(available.top() + 20 - visible.top()))
            self.app.processEvents()
            self.bubble.say(COMMENT_160, 10000)
            self.bubble.reposition()
            self.app.processEvents()
            self.assertTrue(available.contains(self.bubble.frameGeometry()))
            self.assert_complete_comment(COMMENT_160)

    def test_small_work_area_pages_without_dropping_or_clipping_text(self):
        available = QRect(-320, -180, 320, 180)
        with patch("desktop_pet.bubble.work_area", return_value=available):
            self.bubble.say(COMMENT_160, 10000)
            self.assertGreater(len(self.bubble._pages), 1)
            self.assertTrue(self.bubble._page_timer.isActive())
            self.assert_complete_comment(COMMENT_160, available=available)

    def test_emoji_comments_page_at_qt_utf16_boundaries_without_clipping(self):
        text = "🐳海浪👨‍👩‍👧‍👦和小船👍🏽真漂亮e\u0301。" * 30
        available = QRect(-320, -180, 320, 180)
        with patch("desktop_pet.bubble.work_area", return_value=available):
            self.bubble.say(text, 60000)
            self.assertGreater(len(self.bubble._pages), 1)
            self.assert_complete_comment(text, available=available)

    def test_multiline_comments_page_without_extra_line_overflow(self):
        available = QRect(-320, -180, 320, 180)
        for text in ("第一段评论。\n" * 60, "第一段。\n\n第二段。\n\n" * 30):
            with self.subTest(blank_lines="\n\n" in text):
                with patch("desktop_pet.bubble.work_area", return_value=available):
                    self.bubble.say(text, 60000)
                    self.assertGreater(len(self.bubble._pages), 1)
                    self.assert_complete_comment(text, available=available)

    def test_work_area_shrink_starts_pages_and_extends_reading_time_once(self):
        text = COMMENT_80
        with patch("desktop_pet.bubble.work_area", return_value=QRect(0, 0, 1280, 900)):
            self.bubble.say(text, 10000)
            original_page_count = len(self.bubble._pages)
        with patch("desktop_pet.bubble.work_area", return_value=QRect(0, 0, 320, 90)):
            self.bubble.reposition()
            self.assertGreater(len(self.bubble._pages), original_page_count)
            self.assertEqual("".join(self.bubble._pages), text)
            self.assertTrue(self.bubble._page_timer.isActive())
            remaining_pages = self.bubble._pages[self.bubble._page_index:]
            reading_time = sum(max(4000, len(page) * 140) for page in remaining_pages)
            self.assertGreaterEqual(self.bubble.hide_timer.remainingTime(), reading_time - 250)

            # Position-only updates must not restart either reading timer.
            previous_hide = self.bubble.hide_timer.remainingTime()
            previous_page = self.bubble._page_timer.remainingTime()
            cached_layout = self.bubble._layout
            QTest.qWait(100)
            for offset in (QPoint(10, 10), QPoint(20, 15), QPoint(30, 20)):
                self.pet.move(QPoint(300, 300) + offset)
                self.app.processEvents()
            self.assertIs(self.bubble._layout, cached_layout)
            self.assertLess(self.bubble.hide_timer.remainingTime(), previous_hide - 50)
            self.assertLess(self.bubble._page_timer.remainingTime(), previous_page - 50)

    def test_moving_the_pet_reuses_the_measured_text_layout(self):
        self.pet.move(300, 300)
        self.bubble.say(COMMENT_160, 10000)
        cached_layout = self.bubble._layout
        for offset in (QPoint(10, 10), QPoint(20, 15), QPoint(30, 20)):
            self.pet.move(QPoint(300, 300) + offset)
            self.app.processEvents()
            self.assertIs(self.bubble._layout, cached_layout)
            self.assert_complete_layout(self.bubble._pages[self.bubble._page_index], full_text=COMMENT_160)

    def test_rescaling_mid_comment_keeps_the_current_reading_offset_in_the_visible_page(self):
        text = "".join(f"第{index:03d}段：鲸鱼娘继续读这一段完整内容。" for index in range(80))
        with patch("desktop_pet.bubble.work_area", return_value=QRect(0, 0, 420, 150)):
            self.pet.set_scale_percent(100)
            self.bubble.say(text, 60000)
            self.assertGreater(len(self.bubble._pages), 4)
            for _ in range(3):
                self.bubble._next_page()
            old_index = self.bubble._page_index
            read_offset = sum(len(page) for page in self.bubble._pages[:old_index])
            self.assertGreater(read_offset, 0)
            self.bubble.set_font_points(22)
            self.app.processEvents()
            index = self.bubble._page_index
            start = sum(len(page) for page in self.bubble._pages[:index])
            end = start + len(self.bubble._pages[index])
            self.assertLessEqual(start, read_offset)
            self.assertGreater(end, read_offset)
            self.assertNotEqual(index, old_index, "The old numeric page index was reused after repagination")
            self.assertEqual("".join(self.bubble._pages), text)
            self.assert_complete_layout(self.bubble._pages[index], full_text=text)

    def test_linked_paginated_reply_pauses_on_hover_and_footer_can_move_both_directions(self):
        text = COMMENT_160 * 8
        self.bubble.say(text, 1000, conversation_link={"conversation_id": "local", "message_id": "message"})
        self.assertGreater(len(self.bubble._pages), 2)
        opening = QSignalSpy(self.bubble.conversation_requested)
        QApplication.sendEvent(self.bubble, QEvent(QEvent.Type.Enter))
        self.assertTrue(self.bubble._hover_paused)
        self.assertFalse(self.bubble.hide_timer.isActive())
        self.assertFalse(self.bubble._page_timer.isActive())
        original_page = self.bubble._page_index
        QTest.qWait(80)
        self.assertTrue(self.bubble.isVisible())
        self.assertEqual(self.bubble._page_index, original_page)
        for attribute, expected in (("_next_page_rect", 1), ("_previous_page_rect", 0)):
            point = getattr(self.bubble, attribute).center()
            event = QMouseEvent(QEvent.Type.MouseButtonRelease, point,
                                QPointF(self.bubble.mapToGlobal(point.toPoint())), Qt.MouseButton.LeftButton,
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(self.bubble, event)
            self.assertEqual(self.bubble._page_index, expected)
            self.assertFalse(self.bubble.hide_timer.isActive())
            self.assertFalse(self.bubble._page_timer.isActive())
            self.assert_complete_layout(self.bubble._pages[expected], full_text=text)
        self.assertEqual(opening.count(), 0, "Page controls must not open the conversation")
        QApplication.sendEvent(self.bubble, QEvent(QEvent.Type.Leave))
        self.assertFalse(self.bubble._hover_paused)
        self.assertTrue(self.bubble.hide_timer.isActive())
        self.assertTrue(self.bubble._page_timer.isActive())
        self.assertEqual("".join(self.bubble._pages), text)

    def test_font_controls_preserve_character_and_complete_every_page_at_largest_font(self):
        original = QRect(self.pet.frameGeometry()), QRect(self.pet.sprite_rect)
        for points in (12, 14, 18, 22):
            self.bubble.set_font_points(points)
            self.bubble.say(COMMENT_160 * 2, 60000)
            self.assertEqual(self.bubble.font_points, points)
            self.assertEqual(self.bubble.font().pointSizeF(), points)
            self.assertEqual((self.pet.frameGeometry(), self.pet.sprite_rect), original)
            self.assert_complete_comment(COMMENT_160 * 2)

    def test_parent_hide_dismisses_comment_and_stops_expiry(self):
        self.bubble.say(COMMENT_80, 10000)
        self.assertTrue(self.bubble.isVisible())
        self.pet.hide()
        self.app.processEvents()
        self.assertFalse(self.bubble.isVisible())
        self.assertFalse(self.bubble.hide_timer.isActive())
        self.pet.show()
        self.app.processEvents()
        self.assertFalse(self.bubble.isVisible())

    def test_parent_close_dismisses_comment_and_stops_expiry(self):
        self.bubble.say(COMMENT_80, 10000)
        self.pet.close()
        self.app.processEvents()
        self.assertFalse(self.bubble.isVisible())
        self.assertFalse(self.bubble.hide_timer.isActive())

    def test_repeated_say_replaces_text_and_restarts_expiry(self):
        self.bubble.say("第一条", 220)
        QTest.qWait(120)
        self.bubble.say(COMMENT_80, 1000)
        QTest.qWait(150)
        self.assertTrue(self.bubble.isVisible())
        self.assertGreater(self.bubble.hide_timer.remainingTime(), 500)
        self.assert_complete_comment(COMMENT_80)

    def test_expiry_hides_comment_without_closing_pet(self):
        self.bubble.say("你好呀！", 120)
        QTest.qWait(250)
        self.assertFalse(self.bubble.isVisible())
        self.assertTrue(self.pet.isVisible())

    def test_controller_preserves_full_comment_and_allows_more_reading_time(self):
        controller = PetController(self.pet, LocalScaleSettings())
        controller.respond("短句", "neutral")
        short_duration = self.bubble.hide_timer.interval()
        controller.respond(COMMENT_160, "happy")
        self.assert_complete_comment(COMMENT_160)
        self.assertGreater(self.bubble.hide_timer.interval(), short_duration)
        self.assertGreaterEqual(self.bubble.hide_timer.interval(), 15000)


if __name__ == "__main__":
    unittest.main()
