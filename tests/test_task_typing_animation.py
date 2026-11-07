"""Working requests own a looping typing clip until their terminal state."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.frame_animation import FrameAssetError, LoadedClip
from tests import test_idle_behavior_integration as fixture


class TaskTypingAnimationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    setUp = fixture.IdleBehaviorIntegrationTests.setUp
    tearDown = fixture.IdleBehaviorIntegrationTests.tearDown
    _rectangle = staticmethod(fixture.IdleBehaviorIntegrationTests._rectangle)
    _submit_inline = staticmethod(fixture.IdleBehaviorIntegrationTests._submit_inline)
    tick = fixture.IdleBehaviorIntegrationTests.tick
    assert_face = fixture.IdleBehaviorIntegrationTests.assert_face
    start_random_clip = fixture.IdleBehaviorIntegrationTests.start_random_clip

    def _manifest(self, directory, name):
        manifest = fixture.IdleBehaviorIntegrationTests._manifest(self, directory, name)
        if name == "idle_typing_work":
            manifest = replace(manifest, paths=tuple(self.root / f"typing-{index}.png" for index in range(68)))
        return manifest

    def _loaded_clip(self, directory, name):
        if name != "idle_typing_work":
            return fixture.IdleBehaviorIntegrationTests._loaded_clip(self, directory, name)
        if not hasattr(self, "typing_frames"):
            poses = [frame.copy() for frame in self.clip_frames]
            for frame in poses:
                painter = QPainter(frame)
                painter.fillRect(180, 620, 664, 16, QColor("#a8c2d8"))
                painter.end()
            self.typing_frames = (self.clip_frames[0], *(poses[index % 2] for index in range(66)),
                                  self.clip_frames[1])
        return LoadedClip(self._manifest(directory, name), self.typing_frames)

    def start_work(self):
        self.controller.set_task_working(True)
        self.assertTrue(self.controller._work_clip_owned)
        self.assertEqual(self.player.name, "idle_typing_work")
        self.assertTrue(self.player._clip.manifest.loop)

    def finish_work(self):
        self.controller.set_task_working(False)
        self.tick(.26)
        self.tick(0)
        self.assertFalse(self.player.busy)
        self.assertFalse(self.pet._sequence_frames)

    def test_work_loops_beyond_idle_budget_and_decodes_only_once(self):
        with patch("desktop_pet.frame_animation.load_clip", wraps=self._loaded_clip) as decoder:
            self.start_work()
            active = self.player._clip
            self.tick(.26)
            for _ in range(180):
                self.tick(.1)
            self.assertIs(self.player._clip, active)
            self.assertTrue(self.player.playing)
            self.assertTrue(self.player._clip.manifest.loop)
            self.assertFalse(self.player._clock.finished)
            decoder.assert_called_once()
            self.controller.set_task_working(True)
            decoder.assert_called_once()
        self.finish_work()
        self.assert_face("neutral")

    def test_progress_emotions_do_not_restart_or_interrupt_work(self):
        self.start_work()
        clip, generation = self.player._clip, self.player._generation
        for emotion in ("thinking", "happy", "thinking", "surprised"):
            self.assertTrue(self.controller.apply_context_expression(emotion, source="chat"))
            self.tick(.1)
            self.assertIs(self.player._clip, clip)
            self.assertEqual(self.player._generation, generation)
        self.controller.set_task_working(False)
        self.controller.apply_context_expression("happy", source="chat")
        self.tick(.26)
        self.tick(0)
        self.assertFalse(self.player.busy)
        self.assert_face("happy")

    def test_desk_stays_present_after_entry_across_multiple_work_cycles(self):
        self.start_work()
        self.tick(.26)
        clip = self.player._clip
        self.assertEqual(self.player._phase, "play")
        marker = (200, 625)
        self.assertEqual(self.typing_frames[0].pixelColor(*marker).alpha(), 0)
        self.assertEqual(self.typing_frames[-1].pixelColor(*marker).alpha(), 0)
        colors = set()
        for _ in range(len(clip.frames) * 3 + 1):
            self.tick(1 / clip.manifest.fps)
            frame = self.pet._sequence_frames[self.pet._sequence_index]
            self.assertGreaterEqual(frame.pixelColor(*marker).alpha(), 254)
            self.assertEqual(self.player._phase, "play")
            colors.add(frame.pixelColor(512, 512).rgba())
        self.assertGreater(len(colors), 1, "The retained typing images must keep animating")
        self.finish_work()

    def test_manual_typing_preview_keeps_the_full_source_clip(self):
        self.assertTrue(self.controller.play_asset_animation("idle_typing_work"))
        clip = self.player._clip
        self.assertEqual(len(clip.frames), 68)
        self.assertEqual(clip.frames[0].pixelColor(200, 625).alpha(), 0)
        self.assertEqual(clip.frames[-1].pixelColor(200, 625).alpha(), 0)

    def test_work_takes_over_an_existing_random_idle_after_its_exit(self):
        self.start_random_clip()
        self.controller.set_task_working(True)
        self.assertTrue(self.controller._work_animation_pending)
        self.assertEqual(self.player._phase, "exit")
        self.tick(.26)
        self.assertTrue(self.controller._work_clip_owned)
        self.assertEqual(self.player.name, "idle_typing_work")
        self.assertTrue(self.player._clip.manifest.loop)
        self.finish_work()

    def test_dragging_and_release_recovery_freeze_work_clock(self):
        self.start_work()
        self.tick(.3)
        self.pet.animation.start_dragging()
        elapsed, pixels = self.player._clock.elapsed, self.pet._hit_image.copy()
        self.tick(20)
        self.assertEqual(self.player._clock.elapsed, elapsed)
        self.assertEqual(self.pet._hit_image, pixels)
        self.pet.animation.finish_dragging()
        self.tick(.01)
        self.assertEqual(self.pet.animation.state, AnimationState.RETURNING)
        self.assertEqual(self.player._clock.elapsed, elapsed)
        self.tick(self.pet.config.animation.return_duration + .01)
        self.assertTrue(self.player.playing)
        self.assertTrue(self.player._clip.manifest.loop)
        self.finish_work()

    def test_drag_before_start_defers_loading_until_recovery(self):
        self.pet.animation.start_dragging()
        self.controller.set_task_working(True)
        self.assertFalse(self.player.busy)
        self.assertTrue(self.controller._work_animation_pending)
        self.tick(2)
        self.assertFalse(self.player.busy)
        self.pet.animation.finish_dragging()
        self.tick(self.pet.config.animation.return_duration + .01)
        self.assertEqual(self.player.name, "idle_typing_work")
        self.finish_work()

    def test_hidden_pet_defers_work_and_end_before_show_cancels_it(self):
        self.pet.hide()
        self.controller.set_task_working(True)
        self.tick(20)
        self.assertFalse(self.player.busy)
        self.pet.show()
        self.tick(.01)
        self.assertEqual(self.player.name, "idle_typing_work")
        self.finish_work()
        self.pet.hide()
        self.controller.set_task_working(True)
        self.controller.set_task_working(False)
        self.pet.show()
        self.tick(.01)
        self.assertFalse(self.player.busy)

    def test_end_while_loading_discards_the_late_clip(self):
        pending = []
        with patch.object(self.player.runner, "submit", side_effect=lambda work, done: pending.append((work, done))):
            self.controller.set_task_working(True)
            self.assertTrue(self.player.loading)
            self.controller.set_task_working(False)
            work, done = pending.pop()
            done(work(), "")
            self.tick(0)
        self.assertFalse(self.player.busy)
        self.assertFalse(self.controller._work_clip_owned)
        self.assertFalse(self.pet._sequence_frames)

    def test_late_cancelled_load_cannot_end_a_new_work_request(self):
        pending = []
        with patch.object(self.player.runner, "submit", side_effect=lambda work, done: pending.append((work, done))):
            self.controller.set_task_working(True)
            self.controller.set_task_working(False)
            self.controller.set_task_working(True)
            self.assertEqual(len(pending), 1)
            work, done = pending.pop()
            done(work(), "")
            self.tick(.01)
            self.assertEqual(len(pending), 1)
            work, done = pending.pop()
            done(work(), "")
            self.assertTrue(self.controller._work_clip_owned)
            self.assertTrue(self.player._clip.manifest.loop)
        self.finish_work()

    def test_missing_work_asset_is_attempted_once_per_task(self):
        with patch("desktop_pet.frame_animation.read_manifest", side_effect=FrameAssetError("missing")) as reader:
            self.controller.set_task_working(True)
            for _ in range(100):
                self.tick(1)
            reader.assert_called_once()
            self.assertFalse(self.player.busy)
            self.controller.set_task_working(False)
            self.controller.set_task_working(True)
            self.assertEqual(reader.call_count, 2)

    def test_manual_stop_and_preview_take_priority_for_this_request(self):
        self.start_work()
        self.controller.stop_asset_animation()
        self.tick(.26)
        self.tick(20)
        self.assertFalse(self.player.busy)
        self.assertTrue(self.controller._task_working)
        self.assertTrue(self.controller.play_asset_animation("idle_puff_cheeks"))
        manual = self.player._clip
        self.tick(20)
        self.assertIs(self.player._clip, manual)
        self.controller.set_task_working(False)
        self.assertIs(self.player._clip, manual)

    def test_connection_busy_alone_does_not_start_work_or_random_clips(self):
        with patch.object(self.player, "play", wraps=self.player.play) as play:
            self.controller.set_conversation_busy(True)
            self.tick(30)
            play.assert_not_called()

    def test_custom_character_is_preserved_during_work(self):
        self.controller._uses_project_character = False
        original = self.pet._source.toImage().copy()
        self.controller.set_task_working(True)
        self.tick(30)
        self.assertFalse(self.player.busy)
        self.assertEqual(self.pet._source.toImage(), original)


if __name__ == "__main__":
    unittest.main()
