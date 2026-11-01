"""A new drop or size must preserve the instant within the displayed PNG.

Uses the installed balloon clip in hidden, isolated Qt windows. No saved
preferences, live app input, pointer movement, or network access is involved.
"""
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.config import PetConfig, PROJECT_ROOT
from desktop_pet.frame_animation import load_clip
from desktop_pet.pet_window import PetWindow
from desktop_pet.placement import clamp_visible_position


class SequenceReanchorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.clip = load_clip(PROJECT_ROOT / "assets/character/animations", "idle_balloon_float")

    @classmethod
    def tearDownClass(cls):
        cls.clip = None

    def setUp(self):
        self.area = QRect(0, 0, 1920, 1040)
        self.bounds_patch = patch("desktop_pet.pet_window.work_area", return_value=self.area)
        self.bounds_patch.start()
        self.addCleanup(self.bounds_patch.stop)
        self.pet = PetWindow(PetConfig(image_path=PROJECT_ROOT / "assets/expressions/neutral.png"))
        self.pet.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.pet.refresh_timer.stop()
        self.pet.set_scale_percent(200)
        home = clamp_visible_position(QPoint(10000, -10000), self.pet.character_placement_bounds, self.area)
        self.pet.move(home)
        self.pet.set_frame_sequence(self.clip.frames, self.clip.manifest.anchor,
                                    frame_times=tuple(i / self.clip.manifest.fps
                                                      for i in range(len(self.clip.frames))))
        self.pet.set_frame_index(30)
        self.pet.set_frame_progress(.8)

    def tearDown(self):
        self.pet.clear_frame_sequence()
        self.pet.close()
        self.pet.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def assert_current_fraction_keeps_home_and_next_png_is_continuous(self, fraction):
        home = self.pet.frame_home_position
        self.pet.set_frame_progress(fraction)
        self.assertEqual(self.pet.pos(), home, "Resuming the old frame fraction moved the new drop")
        self.pet.set_frame_progress(.99999)
        before_switch = QPoint(self.pet.pos())
        self.pet.set_frame_index(31)
        self.assertLessEqual((self.pet.pos() - before_switch).manhattanLength(), 1,
                             "Reanchoring deferred its jump until the next PNG")

    def test_real_balloon_frame_30_at_eighty_percent_reanchors_without_a_jump(self):
        # This exact pose used to move (-14,+1) on the very first resumed tick.
        self.pet.move(clamp_visible_position(QPoint(10000, -10000),
                                            self.pet.character_placement_bounds, self.area))
        self.pet._remember_sequence_home()
        self.assertGreater((self.pet._sequence_positions[31] -
                            self.pet._sequence_positions[30]).manhattanLength(), 10)
        self.assert_current_fraction_keeps_home_and_next_png_is_continuous(.8)

    def test_resize_reanchors_the_current_frame_instead_of_replaying_its_fraction(self):
        self.pet.set_scale_percent(150)
        resized_home = QPoint(self.pet.pos())
        self.assertEqual(self.pet.frame_home_position, resized_home)
        self.assert_current_fraction_keeps_home_and_next_png_is_continuous(.8)

    def test_actual_drag_preserves_the_fraction_until_recovery_completes(self):
        local = next(
            point for x in (.4, .5, .6) for y in (.5, .6, .7)
            if self.pet.character_contains(point := self.pet.character_transform.map(QPointF(
                self.pet.sprite_rect.x() + self.pet._hit_image.width() * x,
                self.pet.sprite_rect.y() + self.pet._hit_image.height() * y)).toPoint())
        )
        start = self.pet.mapToGlobal(local)
        end = self.area.topRight() + QPoint(-10, 10)
        for kind, position, held in (
            (QEvent.Type.MouseButtonPress, start, True),
            (QEvent.Type.MouseMove, end, True),
            (QEvent.Type.MouseButtonRelease, end, False),
        ):
            button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
            event = QMouseEvent(kind, QPointF(self.pet.mapFromGlobal(position)), QPointF(position),
                                button, Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier)
            with patch("desktop_pet.pet_window.screen_for_point", return_value=None):
                QApplication.sendEvent(self.pet, event)
        self.pet._advance_frame(self.pet.config.animation.return_duration + .01)
        self.assertEqual(self.pet._sequence_index, 30)
        self.assert_current_fraction_keeps_home_and_next_png_is_continuous(.8)

    def test_second_reanchor_uses_raw_time_fraction_and_a_new_clip_resets_it(self):
        self.pet._remember_sequence_home()
        self.pet.set_frame_progress(.9)
        self.pet._remember_sequence_home()
        home = QPoint(self.pet.pos())
        self.pet.set_frame_progress(.9)
        self.assertEqual(self.pet.pos(), home, "A second resize reused the first anchor's fraction")
        self.pet.set_frame_sequence(self.clip.frames, self.clip.manifest.anchor)
        self.assertEqual(self.pet._sequence_fraction, 0)
        self.assertEqual(self.pet._sequence_anchor_fraction, 0)

    def test_full_balloon_cycle_has_no_large_jump_when_a_png_releases_clearance(self):
        self.pet.clear_frame_sequence()
        home = clamp_visible_position(QPoint(10000, -10000), self.pet.character_placement_bounds, self.area)
        self.pet.move(home)
        self.pet.set_frame_sequence(self.clip.frames, self.clip.manifest.anchor,
                                    frame_times=tuple(i / self.clip.manifest.fps
                                                      for i in range(len(self.clip.frames))))
        previous = QPoint(self.pet.pos())
        steps = {}
        for tick in range(1, round(self.clip.manifest.duration * 60)):
            stamp = tick / 60
            index = min(len(self.clip.frames)-1, int((stamp + 1e-10) * self.clip.manifest.fps))
            if index != self.pet._sequence_index:
                self.pet.set_frame_index(index)
            self.pet.set_frame_progress(stamp * self.clip.manifest.fps - index)
            steps[tick] = (self.pet.pos() - previous).manhattanLength()
            previous = QPoint(self.pet.pos())
        self.assertLessEqual(max(steps.values()), 6, "A PNG boundary interrupted the 60 Hz placement path")
        # These were 20px vertical/horizontal jumps despite a nominally smooth
        # position plan, as the preceding image's clamp delayed the return.
        self.assertLessEqual(steps[235], 6)
        self.assertLessEqual(steps[275], 6)
        self.pet.clear_frame_sequence()
        self.assertEqual(self.pet.pos(), home)

    def test_drop_can_use_current_png_space_that_the_previous_png_did_not_have(self):
        self.pet.set_frame_index(55)
        self.pet.set_frame_progress(.8)
        self.pet.move(clamp_visible_position(QPoint(10000, -10000),
                                            self.pet.character_placement_bounds, self.area))
        home = QPoint(self.pet.pos())
        previous_allowed = clamp_visible_position(home, self.pet._sequence_bounds[54], self.area)
        self.assertNotEqual(previous_allowed, home, "Fixture must expose space released by the prior PNG")
        self.pet._remember_sequence_home()
        self.pet.set_frame_progress(.8)
        self.assertEqual(self.pet.pos(), home, "Past PNG constraints moved a newly dropped current pose")


if __name__ == "__main__":
    unittest.main()
