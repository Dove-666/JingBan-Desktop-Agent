"""Real playback timing after a new drop/size, including later loop passes.

Hidden Qt fixtures retain real balloon PNGs, player clocks and mouse handlers.
No live application input, saved preferences, network or asset edits are used.
"""
from dataclasses import replace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.config import PetConfig, PROJECT_ROOT
from desktop_pet.frame_animation import ClipClock, FrameAnimationPlayer, load_clip
from desktop_pet.pet_window import PetWindow
from desktop_pet.placement import clamp_visible_position


class FrameReanchorTimingTests(unittest.TestCase):
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
        self.area_patch = patch("desktop_pet.pet_window.work_area", return_value=self.area)
        self.area_patch.start()
        self.addCleanup(self.area_patch.stop)
        self.pets, self.players, self.patches = [], [], []

    def tearDown(self):
        for player in self.players:
            player.shutdown()
        for pet in self.pets:
            pet.close()
            pet.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for mock in reversed(self.patches):
            mock.stop()

    def make_player(self, *, source="neutral", once=False):
        pet = PetWindow(PetConfig(image_path=PROJECT_ROOT / f"assets/expressions/{source}.png"))
        pet.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        pet.refresh_timer.stop()
        pet.set_scale_percent(200)
        pet.move(clamp_visible_position(QPoint(100000, -100000), pet.character_placement_bounds, self.area))
        player = FrameAnimationPlayer(pet, parent=pet)
        self.pets.append(pet)
        self.players.append(player)
        decoder = patch("desktop_pet.frame_animation.load_clip", return_value=self.clip)
        inline = patch.object(player.runner, "submit", side_effect=lambda work, done: done(work(), ""))
        for mock in (decoder, inline):
            mock.start()
            self.patches.append(mock)
        self.assertTrue(player.play("idle_balloon_float", once=once))
        return pet, player

    def drag_to_top_right(self, pet):
        local = next(point for x in (.4, .5, .6) for y in (.5, .6, .7)
                     if pet.character_contains(point := pet.character_transform.map(QPointF(
                         pet.sprite_rect.x() + pet._hit_image.width()*x,
                         pet.sprite_rect.y() + pet._hit_image.height()*y)).toPoint()))
        start, end = pet.mapToGlobal(local), self.area.topRight() + QPoint(-10, 10)
        for kind, position, held in ((QEvent.Type.MouseButtonPress, start, True),
                                    (QEvent.Type.MouseMove, end, True),
                                    (QEvent.Type.MouseButtonRelease, end, False)):
            event = QMouseEvent(kind, QPointF(pet.mapFromGlobal(position)), QPointF(position),
                                Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton,
                                Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier)
            with patch("desktop_pet.pet_window.screen_for_point", return_value=None):
                QApplication.sendEvent(pet, event)

    @staticmethod
    def x_turns(positions):
        signs = [1 if b > a else -1 for a, b in zip(positions, positions[1:]) if b != a]
        return sum(a != b for a, b in zip(signs, signs[1:]))

    def test_actual_drag_at_eighty_percent_does_not_compress_the_first_resumed_step(self):
        pet, player = self.make_player(once=True)
        player.advance((30+.8) / self.clip.manifest.fps)
        self.assertEqual(player._clock.index, 30)
        self.drag_to_top_right(pet)
        paused_clock = player._clock.elapsed
        dropped = pet.frame_home_position
        for _ in range(50):
            before = QPoint(pet.pos())
            pet._advance_frame(.016)
            if player._clock.elapsed != paused_clock:
                self.assertLessEqual(abs(pet.x()-before.x()), 6,
                                     "The old frame remainder compressed its placement into one GUI tick")
                self.assertAlmostEqual(player._clock.elapsed, 30/self.clip.manifest.fps + .016)
                self.assertEqual(pet.frame_home_position, dropped)
                break
        else:
            self.fail("Playback never resumed after the actual mouse release")

    def test_resize_restarts_the_current_png_interval_without_changing_the_new_home(self):
        pet, player = self.make_player(once=True)
        player.advance((30+.8) / self.clip.manifest.fps)
        pet.set_scale_percent(150)
        home = QPoint(pet.pos())
        player.advance(0)
        self.assertEqual(pet.pos(), home)
        self.assertAlmostEqual(player._clock.elapsed, 30/self.clip.manifest.fps)
        player.advance(.016)
        self.assertLessEqual(abs(pet.x()-home.x()), 6)
        self.assertEqual(pet.frame_home_position, home)

    def assert_later_cycles_are_stable(self, anchor):
        pet, player = self.make_player()
        player.advance((anchor+.8)/self.clip.manifest.fps)
        self.drag_to_top_right(pet)
        groups = [[]]
        previous = player._clock.elapsed
        previous_index = player._clock.index
        for _ in range(2200):
            before = QPoint(pet.pos())
            pet._advance_frame(.016)
            current = player._clock.elapsed
            current_index = player._clock.index
            if current < previous-1e-9 and current_index < previous_index:
                groups.append([])
            previous = current
            previous_index = current_index
            groups[-1].append(pet.x())
            if len(groups) >= 2:
                self.assertLessEqual(abs(pet.x()-before.x()), 6,
                                     "A previous pass's drop caused a PNG-boundary teleport")
            if len(groups) == 4:
                break
        else:
            self.fail("Three manual preview cycles did not complete")
        for cycle in (groups[1], groups[2]):
            self.assertLessEqual(self.x_turns(cycle), 1,
                                 "A previous pass's drop caused a second departure in a later loop")

    def test_later_loops_do_not_repeat_a_mid_clip_drop_anchor(self):
        self.assert_later_cycles_are_stable(25)

    def test_later_loops_do_not_replay_a_current_only_clearance_exception(self):
        # Frame 55 suddenly releases room. Replaying its one-time drag anchor
        # used to teleport the window 127px in each subsequent loop.
        self.assert_later_cycles_are_stable(55)

    def test_entry_bridge_reanchor_retimes_and_later_cycles_exclude_bridge_images(self):
        pet, player = self.make_player(source="happy")
        self.assertGreater(player._clip_offset, 0)
        player.advance(.12)
        self.assertEqual(player._phase, "enter")
        index = pet._sequence_index
        pet.set_scale_percent(150)
        home = QPoint(pet.pos())
        player.advance(0)
        self.assertEqual(pet.pos(), home)
        self.assertAlmostEqual(player._transition_elapsed,
                               self.clip.manifest.transition_ms/1000 * index/(player._clip_offset-1))
        previous = player._clock.elapsed
        for _ in range(600):
            pet._advance_frame(.016)
            if player._phase == "play" and player._clock.elapsed < previous:
                break
            previous = player._clock.elapsed
        else:
            self.fail("The entry bridge never reached a subsequent native cycle")
        self.assertEqual(pet._sequence_cycle_start, player._clip_offset)
        self.assertGreaterEqual(pet._sequence_index, player._clip_offset)
        # Compare against a native-only playback at the same size/home. The
        # bridge must not alter its future placement even though it is cached.
        expected = PetWindow(PetConfig(image_path=PROJECT_ROOT / "assets/expressions/happy.png"))
        expected.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        expected.refresh_timer.stop()
        self.pets.append(expected)
        expected.set_scale_percent(150)
        expected.move(pet.frame_home_position)
        expected.set_frame_sequence(self.clip.frames, self.clip.manifest.anchor,
                                    frame_times=tuple(i/self.clip.manifest.fps for i in range(len(self.clip.frames))))
        for actual, native in zip(pet._sequence_positions[player._clip_offset:], expected._sequence_positions):
            self.assertLessEqual((actual-native).manhattanLength(), 1e-6)

    def test_loop_boundary_epsilon_resets_elapsed_as_well_as_frame_index(self):
        clock = ClipClock(replace(self.clip.manifest, loop=True))
        for _ in range(round(clock.manifest.duration*60)):
            clock.advance(1/60)
        self.assertEqual(clock.index, 0)
        self.assertAlmostEqual(clock.elapsed, 0, places=9)


if __name__ == "__main__":
    unittest.main()
