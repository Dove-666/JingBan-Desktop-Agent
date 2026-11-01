"""Static pose fades become finer without changing real clips or alpha."""
from dataclasses import replace
from pathlib import Path
import unittest

from PySide6.QtGui import QColor, QImage

from desktop_pet.frame_animation import ClipManifest, LoadedClip
from desktop_pet.static_idle_smoothing import smooth_static_idle


def image(color, width=3):
    result = QImage(width, 2, QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(QColor(*color))
    return result


def clip(frames, *, static=True, fps=12):
    manifest = ClipManifest("idle_tsundere", tuple(Path(f"{i}.png") for i in range(len(frames))),
                            fps, True, "idle", (.5, .9375), 250, static)
    return LoadedClip(manifest, tuple(frames))


class StaticIdleSmoothingTests(unittest.TestCase):
    def test_only_marked_twelve_fps_static_poses_are_changed_and_output_is_idempotent(self):
        frames = (image((255, 0, 0, 255)), image((0, 0, 255, 255)))
        source = clip(frames)
        output = smooth_static_idle(source)
        self.assertIs(smooth_static_idle(output), output)
        for untouched in (clip(frames, static=False), clip(frames, fps=18), clip(frames, fps=24)):
            self.assertIs(smooth_static_idle(untouched), untouched)

    def test_opaque_midpoint_does_not_make_a_transparent_seam(self):
        output = smooth_static_idle(clip((image((255, 0, 0, 255)), image((0, 0, 255, 255)))))
        self.assertEqual(output.frames[1].pixelColor(1, 1).getRgb(), (128, 0, 128, 255))

    def test_transparent_color_does_not_bleed_and_semialpha_uses_premultiplied_average(self):
        start = image((255, 0, 0, 128))
        end = image((0, 0, 255, 64))
        output = smooth_static_idle(clip((start, end)))
        midpoint = output.frames[1]
        self.assertEqual(midpoint.pixelColor(1, 1).alpha(), 96)
        self.assertEqual(midpoint.pixel(1, 1), 0x60400020)
        transparent = image((0, 255, 0, 0))
        faded = smooth_static_idle(clip((start, transparent))).frames[1]
        self.assertEqual(faded.pixelColor(1, 1).getRgb(), (255, 0, 0, 64))

    def test_large_opaque_images_keep_color_and_opacity_across_chunk_boundaries(self):
        source = clip((image((255, 255, 255, 255), width=5001),
                       image((0, 0, 255, 255), width=5001)))
        midpoint = smooth_static_idle(source).frames[1]
        for y in (0, 1):
            for x in (0, 4095, 4096, 5000):
                self.assertEqual(midpoint.pixelColor(x, y).getRgb(), (128, 128, 255, 255))

    def test_exact_pose_hold_and_total_duration_are_preserved_with_shared_pixels(self):
        neutral = image((0, 0, 0, 0))
        entry = image((30, 50, 90, 170))
        pose = image((40, 80, 160, 255))
        source = clip((neutral, entry, entry, *([pose] * 24), entry, entry, neutral))
        output = smooth_static_idle(source)
        self.assertEqual((len(output.frames), output.manifest.fps), (60, 24))
        self.assertEqual(output.manifest.duration, source.manifest.duration)
        self.assertEqual(sum(frame == pose for frame in output.frames) / output.manifest.fps, 2.0)
        self.assertIs(output.frames[0], neutral)
        self.assertIs(output.frames[-1], neutral)
        self.assertIs(output.frames[53], pose, "Last hold slot must not become a fade")
        self.assertEqual(output.manifest.paths, tuple(path for path in source.manifest.paths for _ in range(2)))
        self.assertIs(output.frames[6], pose)
        self.assertIs(output.frames[7], pose)

    def test_single_bridge_frames_are_not_mistaken_for_pose_holds(self):
        frames = tuple(image((n * 50, 20, 40, 255)) for n in range(4))
        output = smooth_static_idle(clip(frames))
        self.assertNotEqual(output.frames[3], frames[1])
        self.assertNotEqual(output.frames[5], frames[2])

    def test_trusted_three_slot_endpoints_use_even_symmetric_weights_and_exact_hold(self):
        neutral, pose = image((0, 0, 0, 255)), image((255, 255, 255, 255))
        early, late = image((50, 50, 50, 255)), image((220, 220, 220, 255))
        source = clip((neutral, early, late, *([pose] * 24), late, early, neutral))
        before = tuple(frame.copy() for frame in source.frames)
        output = smooth_static_idle(source)
        self.assertEqual([frame.pixelColor(0, 0).red() for frame in output.frames[:6]],
                         [0, 32, 80, 128, 176, 224])
        self.assertEqual([frame.pixelColor(0, 0).red() for frame in output.frames[-6:]],
                         [224, 176, 128, 80, 32, 0])
        self.assertTrue(all(frame is pose for frame in output.frames[6:54]))
        self.assertEqual(len(output.frames[6:54]) / output.manifest.fps, 2.0)
        self.assertEqual(output.manifest.duration, 2.5)
        self.assertTrue(all(frame.pixelColor(0, 0).alpha() == 255 for frame in output.frames))
        for entry_index, exit_index in ((1, 58), (2, 57), (3, 56), (4, 55), (5, 54)):
            self.assertIs(output.frames[entry_index], output.frames[exit_index])
        self.assertEqual(source.frames, before)

    def test_other_static_layouts_keep_generic_midpoint_behavior(self):
        neutral, pose = image((0, 0, 0, 255)), image((255, 255, 255, 255))
        bridge = image((20, 20, 20, 255))
        source = clip((neutral, bridge, *([pose] * 3), bridge, neutral))
        output = smooth_static_idle(source)
        self.assertEqual(output.frames[1].pixelColor(0, 0).red(), 10)
        self.assertEqual(output.frames[3].pixelColor(0, 0).red(), 138)
        self.assertEqual(len(output.frames), 14)

    def test_frame_budget_invalid_geometry_and_malformed_count_leave_source_unchanged(self):
        a, b = image((255, 0, 0, 255)), image((0, 0, 255, 255))
        for source in (clip((a, b) * 25), clip((a, image((1, 2, 3, 255), width=4))),
                       replace(clip((a, b)), frames=(a,))):
            self.assertIs(smooth_static_idle(source), source)

    def test_original_images_manifest_and_transition_metadata_are_unchanged(self):
        a, b = image((255, 0, 0, 255)), image((0, 0, 255, 255))
        before = (a.copy(), b.copy())
        source = replace(clip((a, b)), entry_frames=(a,), static_frame=b)
        manifest = source.manifest
        output = smooth_static_idle(source)
        self.assertEqual(source.frames, before)
        self.assertIs(source.manifest, manifest)
        self.assertEqual(source.manifest.fps, 12)
        self.assertIs(output.entry_frames, source.entry_frames)
        self.assertIs(output.static_frame, source.static_frame)
        self.assertIs(output.frames[0], a)
        self.assertIs(output.frames[2], b)


if __name__ == "__main__":
    unittest.main()
