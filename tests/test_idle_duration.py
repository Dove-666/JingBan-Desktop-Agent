"""Common idle timing with shared static pixels and unchanged source footage."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest

from PySide6.QtGui import QColor, QImage

from desktop_pet.config import PROJECT_ROOT
from desktop_pet.frame_animation import (
    ClipClock, ClipManifest, LoadedClip, prepare_idle_duration, read_manifest,
)
from desktop_pet.idle_behavior import DEFAULT_IDLE_ACTIONS, IDLE_DURATION_SECONDS


def image(color):
    result = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(QColor(color))
    return result


class IdleDurationTests(unittest.TestCase):
    def setUp(self):
        self.neutral = image("#4e84a0")
        self.pose = image("#92c9e5")
        self.final = image("#294d88")

    def clip(self, *, first=None, last=None, entry=False, static=False):
        first = self.neutral if first is None else first
        last = self.neutral if last is None else last
        frames = (first, *([self.pose] * 58), last)
        manifest = ClipManifest(
            "idle_tsundere", tuple(Path(f"{index}.png") for index in range(60)),
            24, False, "idle", (.5, .9375), 250, static,
        )
        return LoadedClip(manifest, frames, (self.neutral, first) if entry else (), self.neutral)

    def assert_six_second_budget(self, clip):
        bridge = clip.manifest.transition_ms / 1000
        entry = bridge if clip.entry_frames else 0
        exit_duration = bridge if clip.frames[-1] != clip.static_frame else 0
        self.assertAlmostEqual(entry + clip.manifest.duration + exit_duration, IDLE_DURATION_SECONDS)

    def test_dynamic_keeps_every_source_frame_and_reserves_only_real_bridges(self):
        for entry, different_end in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(entry=entry, different_end=different_end):
                original = self.clip(first=self.pose if entry else None,
                                     last=self.final if different_end else None, entry=entry)
                output = prepare_idle_duration(original, IDLE_DURATION_SECONDS)
                self.assert_six_second_budget(output)
                self.assertIs(output.frames, original.frames)
                self.assertIs(output.manifest.paths, original.manifest.paths)
                self.assertIs(output.entry_frames, original.entry_frames)
                self.assertEqual(original.manifest.duration, 2.5)
                self.assertEqual(original.manifest.fps, 24)
                clock = ClipClock(output.manifest)
                displayed = [clock.advance(0)]
                for _ in range(len(output.frames) - 1):
                    displayed.append(clock.advance(1 / output.manifest.fps))
                self.assertEqual(displayed, list(range(len(output.frames))))
                self.assertFalse(clock.finished)
                clock.advance(1 / output.manifest.fps)
                self.assertTrue(clock.finished)

    def test_static_extends_only_the_shared_pose_hold(self):
        original = self.clip(static=True)
        output = prepare_idle_duration(original, IDLE_DURATION_SECONDS)
        self.assert_six_second_budget(output)
        self.assertEqual(output.manifest.fps, 24)
        self.assertEqual(len(output.frames), 144)
        self.assertIs(output.frames[0], self.neutral)
        self.assertIs(output.frames[-1], self.neutral)
        self.assertTrue(all(frame is self.pose for frame in output.frames[1:-1]))
        self.assertEqual(len(output.frames), len(output.manifest.paths))
        self.assertEqual(len({id(frame) for frame in output.frames}), 2)
        self.assertEqual((len(original.frames), original.manifest.duration), (60, 2.5))

    def test_invalid_or_impossible_duration_is_rejected(self):
        original = self.clip(entry=True, last=self.final)
        for seconds in (True, False, 0, -.1, float("nan"), float("inf"), "6", None, .25):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    prepare_idle_duration(original, seconds)

    def test_no_transition_still_uses_the_complete_common_duration(self):
        original = self.clip(last=self.final)
        original = replace(original, manifest=replace(original.manifest, transition_ms=0), static_frame=None)
        output = prepare_idle_duration(original, IDLE_DURATION_SECONDS)
        self.assertEqual(output.manifest.duration, IDLE_DURATION_SECONDS)
        self.assertIs(output.frames, original.frames)

    def test_every_installed_idle_can_use_the_same_duration_without_mutating_its_manifest(self):
        directory = PROJECT_ROOT / "assets/character/animations"
        self.assertIn("idle_sleepy", DEFAULT_IDLE_ACTIONS)
        self.assertIn("idle_tsundere", DEFAULT_IDLE_ACTIONS)
        for name in DEFAULT_IDLE_ACTIONS:
            with self.subTest(name=name):
                manifest_path = directory / name / "animation.json"
                before = manifest_path.read_bytes()
                manifest = read_manifest(directory, name)
                # Metadata remains real. Shared fixture pixels keep this timing
                # inventory check independent from full 1024px video decoding.
                frames = (self.neutral, *([self.pose] * (len(manifest.paths) - 2)), self.final)
                fixture = LoadedClip(replace(manifest, loop=False, completion="idle"),
                                     frames, (), self.neutral)
                output = prepare_idle_duration(fixture, IDLE_DURATION_SECONDS)
                self.assert_six_second_budget(output)
                self.assertEqual(manifest_path.read_bytes(), before)
                self.assertIs(output.frames[0], frames[0])
                self.assertIs(output.frames[-1], frames[-1])

    def test_sleepy_static_asset_preserves_its_source_pose_and_neutral_endpoints(self):
        directory = PROJECT_ROOT / "assets/character/animations"
        manifest = read_manifest(directory, "idle_sleepy")
        data = json.loads((directory / "idle_sleepy/animation.json").read_text(encoding="utf-8"))
        source = PROJECT_ROOT / "assets/expressions/sleepy.png"
        pose = QImage(str(source)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        neutral = QImage(str(PROJECT_ROOT / "assets/expressions/neutral.png")).convertToFormat(
            QImage.Format.Format_ARGB32_Premultiplied)
        self.assertFalse(pose.isNull())
        self.assertTrue(manifest.static_pose_idle)
        self.assertEqual(data["origin"]["source_pose_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(data["origin"].get("video_generation_attempts", 0), 0)
        self.assertEqual(QImage(str(manifest.paths[0])).convertToFormat(pose.format()), neutral)
        self.assertEqual(QImage(str(manifest.paths[-1])).convertToFormat(pose.format()), neutral)
        for index in (3, 15, 26):
            self.assertEqual(QImage(str(manifest.paths[index])).convertToFormat(pose.format()), pose)
        self.assertNotEqual(pose, neutral)


if __name__ == "__main__":
    unittest.main()
