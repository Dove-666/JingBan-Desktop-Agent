"""Persistent prop loops exclude setup/teardown and cache their seam once."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter

from desktop_pet.animation_controller import AnimationState
from desktop_pet.frame_animation import (
    ClipClock, ClipManifest, FrameAnimationPlayer, FrameAssetError, LoadedClip,
    MAX_FRAMES, prepare_loop_segment,
)
from tests import test_frame_animation as fixture


def pose(color, *, desk, size=8):
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.fillRect(size * 3 // 8, size // 8, size // 4, size // 2, QColor(color))
    if desk:
        painter.fillRect(size // 8, size * 5 // 8, size * 3 // 4, max(1, size // 8), QColor("#9cbdd8"))
    painter.end()
    return image


def source_clip(*, size=8, fps=12, count=8, transition=250):
    neutral = pose("#556677", desk=False, size=size)
    first = pose("#3388aa", desk=True, size=size)
    second = pose("#66aadd", desk=True, size=size)
    frames = (neutral, neutral, *(first if index % 2 else second for index in range(count - 4)),
              neutral, neutral)
    manifest = ClipManifest("think_scratch_head", tuple(Path(f"frame-{index}.png") for index in range(count)),
                            fps, True, "hold_last", (.5, .9375), transition)
    return LoadedClip(manifest, frames)


class WorkLoopPreparationTests(unittest.TestCase):
    def test_core_and_seam_keep_the_desk_through_many_cycles(self):
        source = source_clip()
        output = prepare_loop_segment(source, (2, 6))
        core = source.frames[2:6]
        self.assertTrue(all(actual is original for actual, original in zip(output.frames[:4], core)))
        self.assertTrue(output.manifest.loop)
        self.assertEqual(output.manifest.completion, "idle")
        self.assertEqual(output.manifest.fps, source.manifest.fps)
        self.assertEqual(output.manifest.anchor, source.manifest.anchor)
        self.assertEqual(len(output.frames), len(output.manifest.paths))
        self.assertLessEqual(len(output.frames), MAX_FRAMES)
        self.assertGreater(len(output.frames), len(core))
        seam_seconds = (len(output.frames) - len(core)) / output.manifest.fps
        self.assertLessEqual(abs(seam_seconds - .25), 1 / output.manifest.fps)
        self.assertEqual(source.frames[0].pixelColor(1, 5).alpha(), 0)
        self.assertEqual(source.frames[-1].pixelColor(1, 5).alpha(), 0)
        clock = ClipClock(output.manifest)
        colors = set()
        for _ in range(len(output.frames) * 4):
            index = clock.advance(1 / output.manifest.fps)
            frame = output.frames[index]
            self.assertGreaterEqual(frame.pixelColor(1, 5).alpha(), 254)
            colors.add(frame.pixelColor(3, 2).rgba())
        self.assertGreater(len(colors), 1)
        self.assertFalse(clock.finished)
        self.assertEqual((len(source.frames), source.manifest.completion), (8, "hold_last"))

    def test_zero_transition_keeps_exact_core_without_extra_images(self):
        source = source_clip(transition=0)
        output = prepare_loop_segment(source, (2, 6))
        self.assertEqual(len(output.frames), 4)
        self.assertTrue(all(actual is original for actual, original in zip(output.frames, source.frames[2:6])))
        self.assertEqual(output.manifest.paths, source.manifest.paths[2:6])

    def test_identical_endpoints_reuse_images_without_an_unnecessary_seam(self):
        source = source_clip()
        frames = list(source.frames)
        frames[5] = frames[2]
        source = replace(source, frames=tuple(frames))
        output = prepare_loop_segment(source, (2, 6))
        self.assertEqual(len(output.frames), 4)
        self.assertIs(output.frames[0], output.frames[-1])

    def test_invalid_ranges_and_unbounded_seam_are_rejected(self):
        source = source_clip()
        invalid = ((-1, 6), (2, 9), (2, 2), (6, 2), (2, 3), (True, 6), (2, 6.0),
                   (2, "6"), [2, 6], (), "2:6")
        for segment in invalid:
            with self.subTest(segment=segment), self.assertRaises(FrameAssetError):
                prepare_loop_segment(source, segment)
        large = source_clip(count=MAX_FRAMES)
        with self.assertRaises(FrameAssetError):
            prepare_loop_segment(large, (0, MAX_FRAMES - 2))


class WorkLoopPlayerTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FrameAnimationTests.setUpClass.__func__)
    make_clip = fixture.FrameAnimationTests.make_clip
    write_manifest = fixture.FrameAnimationTests.write_manifest
    pet = fixture.FrameAnimationTests.pet
    tearDown = fixture.FrameAnimationTests.tearDown

    def setUp(self):
        fixture.FrameAnimationTests.setUp(self)
        self.source = source_clip(size=1024)
        self.window = self.pet()
        self.player = FrameAnimationPlayer(self.window, self.clip_dir, self.window)
        self.patches = [
            patch("desktop_pet.frame_animation.read_manifest", return_value=self.source.manifest),
            patch("desktop_pet.frame_animation.load_clip", return_value=self.source),
            patch.object(self.player.runner, "submit", side_effect=lambda work, done: done(work(), "")),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def start(self):
        self.assertTrue(self.player.play("think_scratch_head", loop_segment=(2, 6)))
        self.window.animation.advance(1)
        self.player.advance(0)
        self.assertTrue(self.player._installed)
        self.assertEqual(self.player._phase, "enter")
        self.player.advance(.25)
        self.assertEqual(self.player._phase, "play")

    def assert_desk(self):
        image = self.window._sequence_frames[self.window._sequence_index]
        self.assertGreaterEqual(image.pixelColor(128, 640).alpha(), 254)

    def test_entry_occurs_once_and_stop_restores_the_original_source(self):
        baseline = self.window._source.toImage().copy()
        self.start()
        clip = self.player._clip
        self.assertGreater(self.player._clip_offset, 0)
        with patch("desktop_pet.frame_animation.prepare_loop_segment", side_effect=AssertionError("per-cycle preparation")), \
                patch("desktop_pet.frame_animation.load_clip", side_effect=AssertionError("per-cycle decoding")):
            for _ in range(len(clip.frames) * 4):
                self.player.advance(1 / clip.manifest.fps)
                self.assertEqual(self.player._phase, "play")
                self.assertGreaterEqual(self.window._sequence_index, self.player._clip_offset)
                self.assert_desk()
        self.player.stop(smooth=True)
        self.assertEqual(self.player._phase, "exit")
        self.player.advance(.26)
        self.assertFalse(self.player.busy)
        self.assertFalse(self.window._sequence_frames)
        self.assertEqual(self.window._source.toImage(), baseline)

    def test_manual_full_clip_keeps_its_setup_and_teardown_frames(self):
        self.assertTrue(self.player.play("think_scratch_head"))
        self.assertEqual(len(self.player._clip.frames), len(self.source.frames))
        self.assertIs(self.player._clip.frames[0], self.source.frames[0])
        self.assertIs(self.player._clip.frames[-1], self.source.frames[-1])

    def test_invalid_segment_and_single_play_conflict_preserve_the_active_clip(self):
        self.start()
        clip, generation = self.player._clip, self.player._generation
        frame = self.window._sequence_frames[self.window._sequence_index]
        for segment in ((-1, 6), (2, 9), (2, 3), (True, 6), (2, 6.0), [2, 6]):
            with self.subTest(segment=segment):
                self.assertFalse(self.player.play("think_scratch_head", loop_segment=segment))
                self.assertIs(self.player._clip, clip)
                self.assertEqual(self.player._generation, generation)
                self.assertIs(self.window._sequence_frames[self.window._sequence_index], frame)
        self.assertFalse(self.player.play("think_scratch_head", once=True, loop_segment=(2, 6)))
        self.assertFalse(self.player.play("think_scratch_head", once=True, duration_seconds=6, loop_segment=(2, 6)))
        self.assertIs(self.player._clip, clip)

    def test_drag_and_resize_keep_the_same_core_and_never_reenter_setup(self):
        self.start()
        clip = self.player._clip
        self.player.advance(.13)
        self.window.animation.start_dragging()
        elapsed, index = self.player._clock.elapsed, self.window._sequence_index
        self.player.advance(20)
        self.assertEqual(self.player._clock.elapsed, elapsed)
        self.assertEqual(self.window._sequence_index, index)
        self.window.animation.finish_dragging()
        self.player.advance(.1)
        self.assertEqual(self.window.animation.state, AnimationState.RETURNING)
        self.assertEqual(self.player._clock.elapsed, elapsed)
        self.window.animation.advance(1)
        self.player.advance(0)
        self.window.set_scale_percent(200)
        self.player.advance(0)
        for _ in range(len(clip.frames) * 2):
            self.player.advance(1 / clip.manifest.fps)
            self.assertIs(self.player._clip, clip)
            self.assertEqual(self.player._phase, "play")
            self.assertGreaterEqual(self.window._sequence_index, self.player._clip_offset)
            self.assert_desk()


if __name__ == "__main__":
    unittest.main()
