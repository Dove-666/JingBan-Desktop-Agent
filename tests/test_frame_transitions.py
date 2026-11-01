"""Real Qt playback checks for optional entry and stop crossfades.

The red static character and blue clips are temporary synthetic images.  They
never enter the product asset folders or use the user's settings or devices.
"""
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.frame_animation import (
    FrameAnimationPlayer, FrameAssetError, load_clip, read_manifest,
)
from tests import test_frame_animation as frame_helpers


class FrameTransitionTests(unittest.TestCase):
    # Reuse the temporary-window/settings helpers without inheriting all of
    # FrameAnimationTests' cases into this suite a second time.
    make_clip = frame_helpers.FrameAnimationTests.make_clip
    write_manifest = frame_helpers.FrameAnimationTests.write_manifest
    pet = frame_helpers.FrameAnimationTests.pet
    controller = frame_helpers.FrameAnimationTests.controller
    tearDown = frame_helpers.FrameAnimationTests.tearDown

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        frame_helpers.FrameAnimationTests.setUp(self)
        self._save_rectangle(self.original, QColor("#ff0000"), 180)
        folder = self.clip_dir / "think_scratch_head" / "frames"
        self._save_rectangle(folder / "0001.png", QColor("#0000ff"), 380)
        self._save_rectangle(folder / "0002.png", QColor("#0088ff"), 380)
        self.data.update(transition_ms=250, loop=True)
        self.write_manifest(self.data)

    @staticmethod
    def _save_rectangle(path, color, left):
        image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillRect(left, 220, 460, 580, color)
        painter.end()
        if not image.save(str(path), "PNG"):
            raise AssertionError("Could not save temporary synthetic image")

    def _start(self, player=None):
        player = player or FrameAnimationPlayer(self.pet(), self.clip_dir)
        self.assertTrue(player.play("think_scratch_head"))
        frame_helpers.wait_until(lambda: player._clip is not None)
        # Resolve the procedural return separately; no real timer drives these
        # tests, so a paused entry cannot consume its transition time here.
        player.pet.animation.advance(1)
        player.advance(0)
        self.assertTrue(player._installed)
        return player

    @staticmethod
    def _shown(pet):
        if pet._sequence_frames:
            return pet._sequence_frames[pet._sequence_index]
        return pet._source.toImage()

    def _assert_blue(self, image):
        color = image.pixelColor(512, 512)
        self.assertEqual(color.red(), 0)
        self.assertEqual(color.blue(), 255)
        self.assertEqual(color.alpha(), 255)
        self.assertEqual(image.pixelColor(250, 512).alpha(), 0)

    def _assert_blended(self, image):
        overlap = image.pixelColor(512, 512)
        self.assertGreater(overlap.red(), 0)
        self.assertGreater(overlap.blue(), 0)
        self.assertGreaterEqual(overlap.alpha(), 250)
        # Check transparency as well as an overlapping opaque color: a source
        # only pixel fades out while a target only pixel fades in.
        for x in (250, 750):
            alpha = image.pixelColor(x, 512).alpha()
            self.assertGreater(alpha, 0)
            self.assertLess(alpha, 255)
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

    def test_manifest_transition_is_optional_bounded_and_strictly_integer(self):
        data = dict(self.data)
        del data["transition_ms"]
        self.write_manifest(data)
        self.assertEqual(read_manifest(self.clip_dir, data["name"]).transition_ms, 0)
        for duration in (0, 250, 1000):
            with self.subTest(valid=duration):
                self.write_manifest({**data, "transition_ms": duration})
                self.assertEqual(read_manifest(self.clip_dir, data["name"]).transition_ms, duration)
        for duration in (-1, 1001, True, 1.5, "250", None):
            with self.subTest(invalid=duration):
                self.write_manifest({**data, "transition_ms": duration})
                with self.assertRaises(FrameAssetError):
                    read_manifest(self.clip_dir, data["name"])

    def test_omitted_transition_keeps_raw_playback_and_immediate_stop(self):
        data = dict(self.data)
        del data["transition_ms"]
        self.write_manifest(data)
        player = self._start()
        self.assertEqual(len(player.pet._sequence_frames), 2)
        self._assert_blue(self._shown(player.pet))
        player.advance(.09)
        self.assertEqual(player.pet._sequence_index, 1)
        player.stop(smooth=True)
        self.assertFalse(player.pet._sequence_frames)
        self.assertIsNone(player._clip)

    def test_entry_preserves_source_then_blends_color_and_alpha_into_first_frame(self):
        player = self._start()
        pet = player.pet
        source_key = pet._source.cacheKey()
        self.assertEqual(self._shown(pet).pixelColor(512, 512), QColor("red"))
        with patch("desktop_pet.frame_animation.load_clip", side_effect=AssertionError("per-frame I/O")), \
                patch.object(pet, "_layout_character", side_effect=AssertionError("per-frame scaling")):
            player.advance(.125)
            self._assert_blended(self._shown(pet))
            player.advance(.125)
        self._assert_blue(self._shown(pet))
        self.assertEqual(self._shown(pet), load_clip(self.clip_dir, "think_scratch_head").frames[0])
        self.assertEqual(pet._source.cacheKey(), source_key)

    def test_loop_wraps_only_original_frames_and_never_replays_entry(self):
        player = self._start()
        player.advance(.25)
        raw = load_clip(self.clip_dir, "think_scratch_head").frames
        raw_indices = set()
        with patch("desktop_pet.frame_animation.load_clip", side_effect=AssertionError("per-frame I/O")), \
                patch.object(player.pet, "_layout_character", side_effect=AssertionError("per-frame scaling")):
            for _ in range(8):
                player.advance(.09)
                shown = self._shown(player.pet)
                self.assertTrue(any(shown == image for image in raw))
                raw_indices.add(player.pet._sequence_index)
                self._assert_blue(self._shown(player.pet))
        self.assertEqual(len(raw_indices), 2)
        self.assertIsNotNone(player._clip)

    def test_controller_smooth_stop_starts_at_current_frame_and_restores_selected_static(self):
        static = self.root / "character" / "static"
        static.mkdir(parents=True)
        self.assertTrue(QImage(str(self.original)).save(str(static / "tsundere.png"), "PNG"))
        controller, _ = self.controller()
        self.assertTrue(controller.set_static_character("tsundere"))
        pet, player = controller.pet, controller.asset_player
        source_key = pet._source.cacheKey()
        self._start(player)
        player.advance(.25)
        player.advance(.09)
        self.assertEqual(self._shown(pet), load_clip(self.clip_dir, "think_scratch_head").frames[1])
        current = self._shown(pet).copy()
        controller.stop_asset_animation()
        self.assertTrue(pet._sequence_frames)
        self.assertEqual(self._shown(pet), current)
        player.advance(.125)
        self._assert_blended(self._shown(pet))
        player.advance(.125)
        self.assertFalse(pet._sequence_frames)
        self.assertFalse(pet._sequence_sprites)
        self.assertFalse(pet._sequence_alpha_rects)
        self.assertIsNone(player._clip)
        self.assertIsNone(player._clock)
        self.assertEqual(pet._source.cacheKey(), source_key)
        self.assertEqual(controller.appearance, "tsundere")
        self.assertEqual(self._shown(pet).pixelColor(512, 512), QColor("red"))

    def test_default_stop_remains_immediate_even_during_entry_or_exit(self):
        for phase in ("entry", "exit"):
            with self.subTest(phase=phase):
                player = self._start()
                if phase == "exit":
                    player.advance(.25)
                    player.stop(smooth=True)
                player.advance(.08)
                self.assertTrue(player.pet._sequence_frames)
                player.stop()
                self.assertFalse(player.pet._sequence_frames)
                self.assertIsNone(player._clip)
                self.assertIsNone(player._clock)

    def test_repeated_smooth_stop_does_not_restart_or_extend_exit(self):
        player = self._start()
        player.advance(.25)
        player.stop(smooth=True)
        player.advance(.13)
        current = self._shown(player.pet).copy()
        player.stop(smooth=True)
        self.assertEqual(self._shown(player.pet), current)
        player.advance(.13)
        self.assertFalse(player.pet._sequence_frames)
        self.assertIsNone(player._clip)

    def test_drag_and_procedural_return_freeze_both_entry_and_exit(self):
        player = self._start()
        pet = player.pet
        for phase in ("entry", "exit"):
            with self.subTest(phase=phase):
                if phase == "exit":
                    player.advance(.25)
                    player.stop(smooth=True)
                player.advance(.08)
                current = self._shown(pet).copy()
                index, sprite = pet._sequence_index, pet._sprite.cacheKey()
                pet.animation.start_dragging()
                player.advance(10)
                self.assertEqual(self._shown(pet), current)
                self.assertEqual(pet._sequence_index, index)
                self.assertEqual(pet._sprite.cacheKey(), sprite)
                pet.animation.finish_dragging()
                self.assertEqual(pet.animation.state, AnimationState.RETURNING)
                player.advance(10)
                self.assertEqual(self._shown(pet), current)
                self.assertEqual(pet._sprite.cacheKey(), sprite)
                pet.animation.advance(1)
                self.assertEqual(pet.animation.state, AnimationState.IDLE)
                player.advance(0)
                self.assertEqual(self._shown(pet), current)
                player.advance(.18)
                if phase == "entry":
                    self._assert_blue(self._shown(pet))
                else:
                    self.assertFalse(pet._sequence_frames)

    def test_smooth_stop_queued_during_drag_waits_for_return_then_fades(self):
        player = self._start()
        pet = player.pet
        player.advance(.25)
        player.advance(.09)
        current = self._shown(pet).copy()
        pet.animation.start_dragging()
        player.stop(smooth=True)
        player.advance(10)
        self.assertEqual(self._shown(pet), current)
        pet.animation.finish_dragging()
        player.advance(10)
        self.assertEqual(self._shown(pet), current)
        pet.animation.advance(1)
        player.advance(0)
        self.assertEqual(self._shown(pet), current)
        player.advance(.125)
        self._assert_blended(self._shown(pet))
        player.advance(.125)
        self.assertFalse(pet._sequence_frames)

    def test_cancelled_worker_and_shutdown_cannot_install_late_entry(self):
        pet = self.pet()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        clip = load_clip(self.clip_dir, "think_scratch_head")
        callbacks = []
        with patch.object(player.runner, "submit", side_effect=lambda function, callback: callbacks.append(callback)):
            self.assertTrue(player.play("think_scratch_head"))
            player.stop(smooth=True)
            callbacks.pop()(clip, "")
            player.advance(1)
            self.assertIsNone(player._clip)
            self.assertFalse(pet._sequence_frames)
            self.assertTrue(player.play("think_scratch_head"))
            player.shutdown()
            callbacks.pop()(clip, "")
            player.advance(1)
            self.assertIsNone(player._clip)
            self.assertIsNone(player._clock)
            self.assertFalse(pet._sequence_frames)
            self.assertFalse(player.play("think_scratch_head"))
        active = self._start()
        active.advance(.08)
        active.stop(smooth=True)
        active.shutdown()
        active.advance(1)
        self.assertFalse(active.pet._sequence_frames)
        self.assertFalse(active.pet._sequence_sprites)
        self.assertIsNone(active._clip)
        self.assertIsNone(active._clock)


if __name__ == "__main__":
    unittest.main(verbosity=2)
