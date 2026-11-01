"""Temporary balloon-like clips must not move the user's resting placement.

All windows stay hidden. Mouse events are sent directly to Qt; these tests do
not read application preferences, move the native pointer, or use the network.
"""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_config import AnimationConfig
from desktop_pet.config import PetConfig, PROJECT_ROOT
from desktop_pet.frame_animation import (
    FrameAnimationPlayer, LoadedClip, load_clip, read_manifest,
)
from desktop_pet.pet_window import PetWindow
from desktop_pet.placement import clamp_visible_position


class ClipPositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-clip-position-")
        self.root = Path(self.temp.name)
        self.area = QRect(0, 0, 1920, 1040)
        self.windows = []
        self.players = []
        self.bounds_patch = patch("desktop_pet.pet_window.work_area", return_value=self.area)
        self.bounds_patch.start()
        self.addCleanup(self.bounds_patch.stop)
        self.source = self.root / "rest.png"
        body = self.make_frame(balloon=False)
        self.assertTrue(body.save(str(self.source), "PNG"))
        self.directory = self.root / "animations"
        folder = self.directory / "idle_balloon_float"
        folder.mkdir(parents=True)
        for index, balloon in enumerate((False, True, False), start=1):
            self.assertTrue(self.make_frame(balloon=balloon).save(str(folder / f"{index:04d}.png"), "PNG"))
        data = {
            "schema_version": 1, "name": "idle_balloon_float",
            "canvas": {"width": 1024, "height": 1024},
            "fps": 12, "frame_count": 3,
            "frames": ["0001.png", "0002.png", "0003.png"],
            "anchor": {"x": .5, "y": .9375},
            "loop": True, "completion": "idle", "transition_ms": 250,
        }
        (folder / "animation.json").write_text(json.dumps(data), encoding="utf-8")

    def tearDown(self):
        for player in self.players:
            player.shutdown()
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    @staticmethod
    def make_frame(*, balloon):
        image = QImage(1024, 1024, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillRect(QRect(340, 350, 300, 500), QColor("#288bd2"))
        if balloon:
            painter.fillRect(QRect(720, 80, 240, 240), QColor("#65d8ff"))
        painter.end()
        return image

    def pet(self, *, real_character=False, scale=100):
        animation = AnimationConfig(idle_amplitude=0, breathing_amplitude=0,
                                    happy_amplitude=0, surprised_amplitude=0)
        pet = PetWindow(PetConfig(image_path=self.source, animation=animation))
        pet.refresh_timer.stop()
        if real_character:
            pet.set_character_source(QPixmap(str(PROJECT_ROOT / "assets/expressions/neutral.png")))
        pet.set_scale_percent(scale)
        self.windows.append(pet)
        return pet

    def at_edge(self, pet, edge="top_right"):
        requested = {"top_right": QPoint(10000, -10000),
                     "top_left": QPoint(-10000, -10000),
                     "bottom_right": QPoint(10000, 10000)}[edge]
        pet.move(clamp_visible_position(requested, pet.character_placement_bounds, self.area))
        return QPoint(pet.pos())

    def player(self, pet):
        player = FrameAnimationPlayer(pet, self.directory, pet)
        self.players.append(player)
        # Decode synchronously in this isolated test, retaining the actual
        # production play/once/generation/bridge/advance paths.
        inline = patch.object(player.runner, "submit", side_effect=lambda work, done: done(work(), ""))
        inline.start()
        self.addCleanup(inline.stop)
        return player

    def settle(self, pet, player, seconds=2):
        for _ in range(round(seconds / .02)):
            # Follow the actual GUI tick, including post-clip edge recovery.
            # The player's existing frame_tick connection advances it once.
            pet._advance_frame(.02)

    @staticmethod
    def global_canvas_center(pet):
        return pet.character_transform.map(QPointF(pet.sprite_rect.center())) + QPointF(pet.pos())

    def test_sequence_install_and_clear_preserve_original_placement_near_edges(self):
        clip = load_clip(self.directory, "idle_balloon_float")
        for edge in ("top_right", "top_left", "bottom_right"):
            with self.subTest(edge=edge):
                pet = self.pet()
                home = self.at_edge(pet, edge)
                pet.set_frame_sequence(clip.frames, clip.manifest.anchor)
                pet.clear_frame_sequence()
                self.assertEqual(pet.pos(), home, "Temporary prop bounds changed the resting placement")

    def test_one_shot_finishes_at_the_pre_clip_position(self):
        pet = self.pet()
        home = self.at_edge(pet)
        player = self.player(pet)
        self.assertTrue(player.play("idle_balloon_float", once=True))
        self.settle(pet, player)
        self.assertFalse(player.busy)
        self.assertEqual(pet.pos(), home)

    def test_explicit_stop_restores_the_pre_clip_position(self):
        pet = self.pet()
        home = self.at_edge(pet)
        player = self.player(pet)
        self.assertTrue(player.play("idle_balloon_float"))
        self.settle(pet, player, .4)
        player.stop()
        self.settle(pet, player)
        self.assertEqual(pet.pos(), home)

    def test_abrupt_neutral_endpoint_away_from_home_fades_before_relocating(self):
        pet = self.pet()
        home = self.at_edge(pet)
        player = self.player(pet)
        self.assertTrue(player.play("idle_balloon_float", once=True))
        # This fixture has no neutral settling tail: its huge prop disappears
        # in one PNG switch. A neutral image alone must not bypass placement.
        player.advance(.25)
        self.assertEqual(player._phase, "exit")
        self.assertNotEqual(pet.pos(), home)
        previous = QPoint(pet.pos())
        previous_visible = not pet._visible_alpha_rect.isEmpty()
        for _ in range(20):
            pet._advance_frame(.02)
            visible = not pet._visible_alpha_rect.isEmpty()
            if pet.pos() != previous:
                self.assertFalse(visible and previous_visible)
            previous, previous_visible = QPoint(pet.pos()), visible
        self.assertFalse(player.busy)
        self.assertEqual(pet.pos(), home)
        self.assertIsNone(pet._edge_recovery)

    def test_repeated_clips_do_not_accumulate_position_drift(self):
        pet = self.pet(scale=200)
        home = self.at_edge(pet)
        player = self.player(pet)
        for repetition in range(4):
            with self.subTest(repetition=repetition):
                self.assertTrue(player.play("idle_balloon_float", once=True))
                self.settle(pet, player)
                self.assertFalse(player.busy)
                self.assertEqual(pet.pos(), home)

    def test_smooth_exit_relocates_only_while_invisible_and_has_no_post_exit_glide(self):
        pet = self.pet()
        home = self.at_edge(pet)
        home_center = self.global_canvas_center(pet)
        player = self.player(pet)
        self.assertTrue(player.play("idle_balloon_float"))
        self.settle(pet, player, .4)
        previous = self.global_canvas_center(pet)
        player.stop(smooth=True)
        self.assertLessEqual((self.global_canvas_center(pet) - previous).manhattanLength(), 1)
        previous_visible = not pet._visible_alpha_rect.isEmpty()
        for _ in range(70):
            pet._advance_frame(.02)
            current = self.global_canvas_center(pet)
            visible = not pet._visible_alpha_rect.isEmpty()
            if (current - previous).manhattanLength() > 1:
                self.assertFalse(visible and previous_visible,
                                 "A visible character moved during the stop transition")
            previous = current
            previous_visible = visible
        self.assertFalse(player.busy)
        self.assertEqual(pet.pos(), home)
        self.assertLessEqual((self.global_canvas_center(pet) - home_center).manhattanLength(), 1)
        self.assertIsNone(pet._edge_recovery)

    def test_user_drag_during_a_clip_keeps_the_new_drop_position(self):
        pet = self.pet()
        original_home = self.at_edge(pet)
        player = self.player(pet)
        self.assertTrue(player.play("idle_balloon_float"))
        self.settle(pet, player, .4)
        local = pet.character_transform.map(QPointF(
            pet.sprite_rect.x() + pet._hit_image.width() * .48,
            pet.sprite_rect.y() + pet._hit_image.height() * .58,
        )).toPoint()
        self.assertTrue(pet.character_contains(local))
        start = pet.mapToGlobal(local)
        end = self.area.center()
        for kind, position, held in (
            (QEvent.Type.MouseButtonPress, start, True),
            (QEvent.Type.MouseMove, end, True),
            (QEvent.Type.MouseButtonRelease, end, False),
        ):
            button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
            event = QMouseEvent(kind, QPointF(pet.mapFromGlobal(position)), QPointF(position), button,
                                Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier)
            with patch("desktop_pet.pet_window.screen_for_point", return_value=None):
                QApplication.sendEvent(pet, event)
        self.settle(pet, player, .6)
        dropped = QPoint(pet.pos())
        self.assertNotEqual(dropped, original_home)
        player.stop(smooth=True)
        self.settle(pet, player)
        self.assertEqual(pet.pos(), dropped, "Finishing a clip undid the user's drag")

    def test_installed_balloon_returns_to_the_same_place_at_two_sizes(self):
        directory = PROJECT_ROOT / "assets/character/animations"
        manifest = read_manifest(directory, "idle_balloon_float")
        paths = tuple(manifest.paths[index] for index in (0, len(manifest.paths) // 2, len(manifest.paths) - 1))
        frames = tuple(QImage(str(path)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                       for path in paths)
        self.assertTrue(all(not frame.isNull() for frame in frames))
        clip = LoadedClip(replace(manifest, paths=paths), frames)
        for scale in (100, 200):
            with self.subTest(scale=scale):
                pet = self.pet(real_character=True, scale=scale)
                home = self.at_edge(pet)
                player = self.player(pet)
                with patch("desktop_pet.frame_animation.read_manifest", return_value=clip.manifest), \
                        patch("desktop_pet.frame_animation.load_clip", return_value=clip):
                    self.assertTrue(player.play("idle_balloon_float", once=True))
                self.settle(pet, player)
                self.assertFalse(player.busy)
                self.assertEqual(pet.pos(), home, "Installed balloon artwork changed the resting position")

    def test_next_clip_starts_at_home_without_waiting_for_a_static_glide(self):
        pet = self.pet()
        home = self.at_edge(pet)
        player = self.player(pet)
        self.assertTrue(player.play("idle_balloon_float"))
        self.settle(pet, player, .4)
        player.stop(smooth=True)
        self.settle(pet, player, .32)
        self.assertFalse(player.busy)
        self.assertEqual(pet.pos(), home)
        self.assertIsNone(pet._edge_recovery)
        # Finishing the fade also finishes placement; a new clip never adopts
        # an intermediate position from a separate static glide.
        self.assertTrue(player.play("idle_balloon_float", once=True))
        self.settle(pet, player)
        self.assertFalse(player.busy)
        self.assertEqual(pet.pos(), home, "The next clip adopted an intermediate glide position as home")

    def test_resize_during_a_clip_discards_the_stale_pre_resize_home(self):
        pet = self.pet()
        old_home = self.at_edge(pet)
        player = self.player(pet)
        self.assertTrue(player.play("idle_balloon_float"))
        self.settle(pet, player, .4)
        pet.set_scale_percent(150)
        resized_home = QPoint(pet.pos())
        self.assertNotEqual(resized_home, old_home)
        player.stop(smooth=True)
        self.settle(pet, player)
        self.assertEqual(pet.pos(), resized_home, "Clip completion undid the user's new size placement")

    def test_work_area_change_during_a_clip_discards_the_stale_home(self):
        pet = self.pet()
        old_home = self.at_edge(pet)
        player = self.player(pet)
        self.assertTrue(player.play("idle_balloon_float"))
        self.settle(pet, player, .4)
        new_area = QRect(120, 80, 1240, 800)
        with patch("desktop_pet.pet_window.work_area", return_value=new_area):
            pet._work_area_changed()
            refitted_home = QPoint(pet.pos())
            self.assertNotEqual(refitted_home, old_home)
            player.stop(smooth=True)
            self.settle(pet, player)
            self.assertEqual(pet.pos(), refitted_home,
                             "Clip completion replayed a home from an obsolete monitor layout")
            self.assertTrue(new_area.toRectF().adjusted(-1, -1, 1, 1).contains(
                pet.character_placement_bounds.translated(pet.pos())))


if __name__ == "__main__":
    unittest.main()
