"""Synthetic, temporary PNG clips exercise playback without generated assets.

Nothing in these tests touches production settings, credentials or the native
mouse/foreground. Synthetic frames are never installed in product assets.
"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QThreadPool
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QPushButton

from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig
from desktop_pet.expression_manager import ExpressionManager, EXPRESSIONS
from desktop_pet.frame_animation import (
    ClipClock, FrameAssetError, FrameAnimationPlayer, load_clip, read_manifest,
)
from desktop_pet.pet_controller import PetController
from desktop_pet.pet_window import PetWindow
from desktop_pet.ui.debug_panel import DebugPanel
from tests.test_debug_ui import FakeChat, FakeSettings


def wait_until(predicate, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.002)
    raise AssertionError("Temporary clip worker did not finish")


class FrameAnimationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-frame-unit-")
        self.root = Path(self.temp.name)
        self.clip_dir = self.root / "animations"
        self.windows = []
        self.original = self.root / "custom.png"
        image = QImage(160, 240, QImage.Format.Format_ARGB32)
        image.fill(QColor("#176aa2"))
        image.save(str(self.original), "PNG")
        self.data = self.make_clip()

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        wait_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def make_clip(self, name="think_scratch_head", **changes):
        folder = self.clip_dir / name
        (folder / "frames").mkdir(parents=True, exist_ok=True)
        for i in (1, 2):
            image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.fillRect(200 + (i - 1) * 400, 200, 200, 600, QColor("#5ab6d5"))
            painter.end()
            image.save(str(folder / "frames" / f"{i:04d}.png"), "PNG")
        data = {"schema_version": 1, "name": name, "canvas": {"width": 1024, "height": 1024},
                "fps": 12, "frame_count": 2, "frames": ["frames/0001.png", "frames/0002.png"],
                "anchor": {"x": .5, "y": .9375}, "loop": False, "completion": "idle"}
        data.update(changes)
        self.write_manifest(data, name)
        return data

    def write_manifest(self, data, name="think_scratch_head"):
        (self.clip_dir / name / "animation.json").write_text(json.dumps(data), encoding="utf-8")

    def pet(self, *, default_image=False):
        pet = PetWindow(PetConfig() if default_image else PetConfig(image_path=self.original))
        pet.refresh_timer.stop()
        self.windows.append(pet)
        return pet

    def controller(self, pet=None):
        settings = FakeSettings()
        settings.load_scale = lambda: 100
        settings.save_scale = lambda value: None
        expressions = ExpressionManager(self.root / "expressions", character_directory=self.root / "character")
        controller = PetController(pet or self.pet(), settings, expressions,
                                   animation_directory=self.clip_dir)
        return controller, settings

    def install(self, player):
        player._loaded(player._generation, "think_scratch_head", load_clip(self.clip_dir, "think_scratch_head"), "")
        player.pet.animation.advance(1)
        player.advance(0)
        self.assertTrue(player._installed)

    def test_valid_rgba_manifest_and_narrow_pattern(self):
        clip = load_clip(self.clip_dir, "think_scratch_head")
        self.assertEqual(len(clip.frames), 2)
        self.assertTrue(clip.frames[0].hasAlphaChannel())
        data = dict(self.data)
        del data["frames"]
        data["frame_pattern"] = "frames/{index:04d}.png"
        self.write_manifest(data)
        self.assertEqual(read_manifest(self.clip_dir, "think_scratch_head").paths, clip.manifest.paths)

    def test_missing_assets_are_explicit_and_never_fake_ready(self):
        pet = self.pet()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        self.assertFalse(player.play("snarl_cute"))
        self.assertIn("尚未制作", player.status)
        self.assertFalse(pet._sequence_frames)
        self.assertIn("尚未制作", player.catalog()[1][2])

    def test_invalid_metadata_paths_and_unfinished_production_are_rejected(self):
        bad = ({"schema_version": True}, {"fps": True}, {"fps": 60}, {"fps": float("nan")}, {"frame_count": 1},
               {"canvas": {"width": 640, "height": 640}}, {"frames": ["../custom.png", "frames/0002.png"]},
               {"frames": ["frames/0001.png", "frames/0001.png"]}, {"anchor": {"x": 2, "y": .5}},
               {"loop": "false"}, {"completion": "execute"}, {"qa_passed": False},
               {"complete": False}, {"origin": {"synthetic": True}}, {"duration_ms": 999})
        for change in bad:
            with self.subTest(change=change):
                self.write_manifest({**self.data, **change})
                with self.assertRaises(FrameAssetError):
                    load_clip(self.clip_dir, "think_scratch_head")

    def test_missing_corrupt_rgb_and_wrong_canvas_frames_are_rejected(self):
        path = self.clip_dir / "think_scratch_head" / "frames" / "0002.png"
        path.unlink()
        with self.assertRaises(FrameAssetError):
            load_clip(self.clip_dir, "think_scratch_head")
        path.write_bytes(b"broken")
        with self.assertRaises(FrameAssetError):
            load_clip(self.clip_dir, "think_scratch_head")
        for size, fmt in ((1024, QImage.Format.Format_RGB32), (512, QImage.Format.Format_ARGB32)):
            image = QImage(size, size, fmt)
            image.fill(Qt.GlobalColor.white)
            image.save(str(path), "PNG")
            with self.assertRaises(FrameAssetError):
                load_clip(self.clip_dir, "think_scratch_head")

    def test_clock_uses_elapsed_time_and_respects_loop_pause_and_hold(self):
        manifest = read_manifest(self.clip_dir, "think_scratch_head")
        clock = ClipClock(manifest)
        self.assertEqual(clock.advance(.09), 1)
        self.assertEqual(clock.advance(20, paused=True), 1)
        self.assertFalse(clock.finished)
        self.assertEqual(clock.advance(.09), 1)
        self.assertTrue(clock.finished)
        for invalid in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                clock.advance(invalid)
        self.write_manifest({**self.data, "loop": True})
        loop = ClipClock(read_manifest(self.clip_dir, "think_scratch_head"))
        self.assertEqual(loop.advance(1000 / 6 + .09), 1)
        self.assertFalse(loop.finished)

    def test_fully_opaque_or_empty_rgba_is_not_real_transparency(self):
        path = self.clip_dir / "think_scratch_head" / "frames" / "0002.png"
        image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
        for color in (Qt.GlobalColor.white, Qt.GlobalColor.transparent):
            image.fill(color)
            image.save(str(path), "PNG")
            with self.assertRaises(FrameAssetError):
                load_clip(self.clip_dir, "think_scratch_head")

    def test_play_loads_in_worker_and_timer_swaps_cached_hit_images_without_io(self):
        pet = self.pet()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        self.assertTrue(player.play("think_scratch_head"))
        wait_until(lambda: player._clip is not None)
        pet.animation.advance(1)
        player.advance(0)
        geometry = pet.sprite_rect
        scaled_key = pet._sprite.cacheKey()
        first_hit = pet._hit_image.cacheKey()
        with patch("desktop_pet.frame_animation.load_clip", side_effect=AssertionError("per-frame I/O")), \
                patch.object(pet, "_layout_character", side_effect=AssertionError("per-frame scaling")):
            player.advance(.09)
            self.assertNotEqual(pet._sprite.cacheKey(), scaled_key)
            self.assertNotEqual(pet._hit_image.cacheKey(), first_hit)
            self.assertEqual(pet.sprite_rect, geometry)
            self.assertTrue(pet._hit_image.hasAlphaChannel())

    def test_drag_freezes_clip_until_program_return_has_completed(self):
        pet = self.pet()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        self.install(player)
        pet.animation.start_dragging()
        pose, frame = pet.animation.pose, pet._sprite.cacheKey()
        player.advance(10)
        self.assertEqual(pet._sprite.cacheKey(), frame)
        self.assertEqual(pet.animation.pose, pose)
        self.assertFalse(player.play("think_scratch_head"))
        pet.animation.finish_dragging()
        player.advance(10)
        self.assertEqual(player._clock.elapsed, 0)
        pet.animation.advance(1)
        self.assertEqual(pet.animation.state, AnimationState.IDLE)
        player.advance(.09)
        self.assertEqual(pet._sequence_index, 1)

    def test_completion_idle_and_hold_last_stop_restore_static_source(self):
        pet = self.pet()
        baseline = pet._source.cacheKey()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        self.install(player)
        player.advance(1)
        self.assertFalse(pet._sequence_frames)
        self.assertEqual(pet._source.cacheKey(), baseline)
        self.write_manifest({**self.data, "completion": "hold_last"})
        self.install(player)
        player.advance(1)
        self.assertTrue(pet._sequence_frames)
        self.assertEqual(pet._sequence_index, 1)
        self.assertIn("末帧", player.status)
        player.stop()
        self.assertFalse(pet._sequence_frames)
        self.assertEqual(pet._source.cacheKey(), baseline)

    def test_late_api_expression_and_stop_wait_for_drag_and_smooth_return(self):
        controller, _ = self.controller()
        pet, player = controller.pet, controller.asset_player
        self.install(player)
        pet.animation.start_dragging()
        frozen = pet._sprite.cacheKey()
        controller.set_expression("happy", feedback=False)
        self.assertEqual(controller.expression, "happy")
        self.assertEqual(pet._sprite.cacheKey(), frozen)
        player.stop()
        self.assertEqual(pet._sprite.cacheKey(), frozen)
        pet.animation.finish_dragging()
        player.advance(.1)
        controller._finish_deferred_expression(.1)
        self.assertEqual(pet._sprite.cacheKey(), frozen)
        pet.animation.advance(1)
        player.advance(0)
        controller._finish_deferred_expression(0)
        self.assertFalse(pet._sequence_frames)
        self.assertEqual(pet.animation.state, AnimationState.HAPPY)

    def test_resize_recaches_whole_canvas_without_changing_current_frame(self):
        pet = self.pet()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        self.install(player)
        player.advance(.09)
        key = pet._sprite.cacheKey()
        pet.set_scale_percent(200)
        self.assertEqual(pet._sequence_index, 1)
        self.assertNotEqual(pet._sprite.cacheKey(), key)
        self.assertEqual(pet._sprite.width(), pet._sprite.height())
        self.assertTrue(pet.rect().contains(pet.animated_sprite_rect.toAlignedRect()))

    def test_reply_and_stop_arriving_during_release_recovery_keep_frozen_frame_until_idle(self):
        controller, _ = self.controller()
        pet, player = controller.pet, controller.asset_player
        self.install(player)
        player.advance(.09)
        pet.animation.start_dragging()
        pet.animation.finish_dragging()
        frozen = pet._sprite.cacheKey()
        controller.set_expression("surprised", feedback=False)
        player.stop()
        player.advance(.1)
        controller._finish_deferred_expression(.1)
        self.assertEqual(pet.animation.state, AnimationState.RETURNING)
        self.assertEqual(pet._sprite.cacheKey(), frozen)
        self.assertFalse(player.play("think_scratch_head"))
        pet.animation.advance(1)
        player.advance(0)
        controller._finish_deferred_expression(0)
        self.assertFalse(pet._sequence_frames)
        self.assertEqual(pet.animation.state, AnimationState.SURPRISED)

    def test_extreme_anchor_and_small_work_area_keep_full_canvas_visible(self):
        pet = self.pet()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        for x, y in ((0, 0), (1, 1), (.5, .9375)):
            self.write_manifest({**self.data, "anchor": {"x": x, "y": y}})
            self.install(player)
            with patch("desktop_pet.pet_window.work_area", return_value=QRect(0, 0, 160, 200)):
                pet.set_scale_percent(200)
            bounds = pet._drawing_area.adjusted(-1e-8, -1e-8, 1e-8, 1e-8)
            for _ in range(60):
                pet.animation.advance(.02)
                self.assertTrue(bounds.contains(pet.animated_sprite_rect), (x, y, bounds, pet.animated_sprite_rect))
                self.assertTrue(QRectF(pet.rect()).contains(pet.animated_sprite_rect))
            player.stop()

    def test_stopped_or_closed_load_result_is_discarded(self):
        pet = self.pet()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        clip = load_clip(self.clip_dir, "think_scratch_head")
        generation = player._generation
        player.stop()
        player._loaded(generation, "think_scratch_head", clip, "")
        self.assertIsNone(player._clip)
        generation = player._generation
        player.shutdown()
        player._loaded(generation, "think_scratch_head", clip, "")
        self.assertIsNone(player._clip)
        self.assertFalse(player.play("think_scratch_head"))

    def test_repeated_play_while_decoding_does_not_queue_unbounded_raw_frames(self):
        pet = self.pet()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        with patch.object(player.runner, "submit") as submit:
            self.assertTrue(player.play("think_scratch_head"))
            self.assertFalse(player.play("think_scratch_head"))
            player.stop()
            self.assertFalse(player.play("think_scratch_head"))
            self.assertEqual(submit.call_count, 1)
            player._loaded(player._generation - 1, "think_scratch_head", None, "cancelled test")
            self.assertTrue(player.play("think_scratch_head"))

    def test_once_finishes_looping_hold_clip_with_smooth_exit_without_changing_asset(self):
        self.write_manifest({**self.data, "loop": True, "completion": "hold_last", "transition_ms": 200})
        manifest_path = self.clip_dir / "think_scratch_head" / "animation.json"
        original_manifest = manifest_path.read_bytes()
        pet = self.pet()
        baseline = pet._source.cacheKey()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        self.assertFalse(player.busy)
        self.assertTrue(player.play("think_scratch_head", once=True))
        self.assertTrue(player.loading)
        self.assertTrue(player.busy)
        wait_until(lambda: player.playing)
        self.assertFalse(player.loading)
        self.assertFalse(player._clip.manifest.loop)
        self.assertEqual(player._clip.manifest.completion, "idle")
        self.assertEqual(player._phase, "enter")
        pet.animation.advance(1)
        player.advance(.2)
        self.assertEqual(player._phase, "play")
        player.advance(.2)
        self.assertEqual(player._phase, "exit")
        self.assertTrue(player.busy)
        self.assertTrue(pet._sequence_frames)
        player.advance(.1)
        self.assertTrue(player.playing)
        player.advance(.1)
        self.assertFalse(player.busy)
        self.assertFalse(pet._sequence_frames)
        self.assertEqual(pet._source.cacheKey(), baseline)
        self.assertEqual(manifest_path.read_bytes(), original_manifest)

    def test_default_play_retains_looping_manifest_and_stays_busy(self):
        self.write_manifest({**self.data, "loop": True, "completion": "hold_last"})
        pet = self.pet()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        self.assertTrue(player.play("think_scratch_head"))
        wait_until(lambda: player.playing)
        self.assertTrue(player._clip.manifest.loop)
        self.assertEqual(player._clip.manifest.completion, "hold_last")
        pet.animation.advance(1)
        player.advance(100)
        self.assertTrue(player.playing)
        self.assertTrue(player.busy)
        self.assertFalse(player._clock.finished)
        player.stop()
        self.assertFalse(player.busy)

    def test_stale_load_callback_cannot_clear_new_decoder_or_replace_current_clip(self):
        pet = self.pet()
        player = FrameAnimationPlayer(pet, self.clip_dir, pet)
        clip = load_clip(self.clip_dir, "think_scratch_head")
        with patch.object(player.runner, "submit") as submit:
            self.assertTrue(player.play("think_scratch_head"))
            old_generation = player._generation
            old_callback = submit.call_args.args[1]
            player.stop()
            self.assertTrue(player.loading)  # Memory remains owned until this worker returns.
            self.assertFalse(player.play("think_scratch_head", once=True))
            old_callback(clip, "")
            self.assertFalse(player.loading)
            self.assertFalse(player.playing)
            self.assertTrue(player.play("think_scratch_head", once=True))
            new_generation = player._generation
            new_callback = submit.call_args.args[1]
            self.assertGreater(new_generation, old_generation)
            old_callback(clip, "late stale error")
            self.assertTrue(player.loading)
            self.assertIsNone(player._clip)
            self.assertEqual(player._loading_generation, new_generation)
            new_callback(clip, "")
            self.assertFalse(player.loading)
            self.assertTrue(player.playing)
            active = player._clip
            old_callback(None, "even later stale error")
            self.assertIs(player._clip, active)
            self.assertNotIn("stale error", player.status)

    def test_static_appearance_is_independent_from_semantic_api_emotion_and_custom_image(self):
        controller, _ = self.controller()
        character = self.root / "character"
        (character / "static").mkdir(parents=True)
        image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        image.setPixelColor(512, 512, QColor("#c8e5ff"))
        image.save(str(character / "master.png"), "PNG")
        image.save(str(character / "static" / "tsundere.png"), "PNG")
        original_key = controller.pet._source.cacheKey()
        self.assertTrue(controller.set_static_character("tsundere"))
        chosen_key = controller.pet._source.cacheKey()
        self.assertNotEqual(chosen_key, original_key)
        controller.set_expression("happy", feedback=False)
        self.assertEqual(controller.expression, "happy")
        self.assertEqual(controller.pet._source.cacheKey(), chosen_key)
        self.assertNotIn("tsundere", EXPRESSIONS)
        controller.set_static_character("original")
        self.assertEqual(controller.pet._source.cacheKey(), original_key)
        self.assertIn("自定义", controller.appearance_status)

    def test_default_character_uses_normalized_master_and_corrupt_master_falls_back(self):
        character = self.root / "character"
        character.mkdir()
        image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        image.setPixelColor(500, 500, QColor("red"))
        image.save(str(character / "master.png"), "PNG")
        controller, _ = self.controller(self.pet(default_image=True))
        self.assertEqual(controller.pet._source.size(), image.size())
        self.assertIn("master.png", controller.appearance_status)
        (character / "master.png").write_bytes(b"not a PNG")
        fallback, _ = self.controller(self.pet(default_image=True))
        self.assertEqual(fallback.pet._source.cacheKey(), fallback._original.cacheKey())
        self.assertIn("回退", fallback.appearance_status)

    def test_debug_asset_actions_report_missing_and_can_refresh_new_static_art(self):
        controller, settings = self.controller()
        panel = DebugPanel(controller, settings, FakeChat())
        self.windows.append(panel)
        panel.asset_animation_combo.setCurrentIndex(1)
        self.assertIn("尚未制作", panel.asset_animation_status.text())
        panel.findChild(QPushButton, "playAssetAnimationButton").click()
        self.assertIn("尚未制作", panel.asset_animation_status.text())
        self.assertIn("尚未就绪", panel.appearance_combo.itemData(1, Qt.ItemDataRole.ToolTipRole))
        folder = self.root / "character" / "static"
        folder.mkdir(parents=True)
        image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        image.setPixelColor(500, 500, QColor("red"))
        image.save(str(folder / "tsundere.png"), "PNG")
        panel.findChild(QPushButton, "refreshCharacterAssetsButton").click()
        panel.appearance_combo.setCurrentIndex(1)
        self.assertEqual(controller.appearance, "tsundere")
        self.assertIn("已加载", panel.appearance_status.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
