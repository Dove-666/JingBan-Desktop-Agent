"""Screen-space continuity of idle clips and intentional early interruption.

Temporary pixels exercise placement constraints independently of the planner.
The installed-art check samples seven real frames per clip, retaining exact
neutral endpoints. No live settings, services, pointer input or APIs are used.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_config import AnimationConfig
from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig, PROJECT_ROOT
from desktop_pet.frame_animation import FrameAnimationPlayer, LoadedClip, load_clip, read_manifest
from desktop_pet.idle_behavior import DEFAULT_IDLE_ACTIONS
from desktop_pet.pet_window import PetWindow
from desktop_pet.placement import clamp_visible_position


def rendered_geometry(pet):
    """Measure actual painted pixels, independently of cached placement boxes."""
    image = QImage(pet.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    pet.render(painter, QPoint())
    painter.end()
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    width, height, stride = alpha.width(), alpha.height(), alpha.bytesPerLine()
    raw = alpha.constBits()
    left, top, right, bottom, maximum = width, height, 0, 0, 0
    for y in range(height):
        row = bytes(raw[y * stride:y * stride + width])
        if not row.strip(b"\0"):
            continue
        maximum = max(maximum, max(row))
        left = min(left, width - len(row.lstrip(b"\0")))
        right = max(right, len(row.rstrip(b"\0")))
        top, bottom = min(top, y), y + 1
    opacity = pet.windowOpacity()
    visible_alpha = maximum * opacity
    bounds = QRectF(left, top, right - left, bottom - top) if visible_alpha > 1 else QRectF()
    return bounds, visible_alpha


class IdleTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.fixture = tempfile.TemporaryDirectory(prefix="aura-idle-continuity-")
        cls.root = Path(cls.fixture.name)
        cls.original = cls.root / "rest.png"
        cls.animations = cls.root / "animations"
        folder = cls.animations / "idle_balloon_float"
        folder.mkdir(parents=True)
        cls.synthetic_frames = []
        # The prop arrives and leaves progressively. This gives the placement
        # path real advance notice rather than fabricating a one-frame jump.
        progress = (0, .12, .3, .55, .8, 1, 1, .8, .55, .3, .12, 0, 0)
        for index, weight in enumerate(progress, 1):
            image = QImage(1024, 1024, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.fillRect(QRect(340, 350, 300, 500), QColor("#288bd2"))
            if weight:
                painter.fillRect(QRect(round(650 + 70 * weight), round(330 - 230 * weight),
                                      round(30 + 190 * weight), round(40 + 160 * weight)), QColor("#65d8ff"))
            painter.end()
            path = folder / f"{index:04d}.png"
            if not image.save(str(path), "PNG"):
                raise AssertionError("Could not save temporary fixture")
            cls.synthetic_frames.append(image)
        cls.synthetic_frames[0].save(str(cls.original), "PNG")
        manifest = {
            "schema_version": 1, "name": "idle_balloon_float",
            "canvas": {"width": 1024, "height": 1024},
            "fps": 12, "frame_count": len(progress),
            "frames": [f"{i:04d}.png" for i in range(1, len(progress) + 1)],
            "anchor": {"x": .5, "y": .9375},
            "loop": True, "completion": "idle", "transition_ms": 250,
        }
        (folder / "animation.json").write_text(json.dumps(manifest), encoding="utf-8")
        cls.synthetic_clip = load_clip(cls.animations, "idle_balloon_float")

    @classmethod
    def tearDownClass(cls):
        cls.synthetic_frames = ()
        cls.synthetic_clip = None
        cls.fixture.cleanup()

    def setUp(self):
        self.area = QRect(0, 0, 1920, 1040)
        self.work_area = patch("desktop_pet.pet_window.work_area", return_value=self.area)
        self.work_area.start()
        self.windows = []
        self.players = []
        self.patches = []

    def tearDown(self):
        for player in self.players:
            player.shutdown()
        for pet in self.windows:
            pet.close()
            pet.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for mock in reversed(self.patches):
            mock.stop()
        self.work_area.stop()

    def make_player(self, *, scale=100, source=None, directory=None, clip=None):
        pet = PetWindow(PetConfig(
            image_path=source or self.original,
            animation=AnimationConfig(idle_amplitude=0, breathing_amplitude=0,
                                      happy_amplitude=0, surprised_amplitude=0),
        ))
        pet.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        pet.refresh_timer.stop()
        pet.set_scale_percent(scale)
        player = FrameAnimationPlayer(pet, directory or self.animations, pet)
        self.windows.append(pet)
        self.players.append(player)
        decoder = patch("desktop_pet.frame_animation.load_clip", return_value=clip or self.synthetic_clip)
        inline = patch.object(player.runner, "submit", side_effect=lambda work, done: done(work(), ""))
        for mock in (decoder, inline):
            mock.start()
            self.patches.append(mock)
        pet.show()
        self.app.processEvents()
        return pet, player

    def at_edge(self, pet):
        home = clamp_visible_position(QPoint(10000, -10000), pet.character_placement_bounds, self.area)
        pet.move(home)
        return QPoint(home)

    def assert_painted_inside(self, pet):
        bounds, alpha = rendered_geometry(pet)
        if alpha > 1:
            global_bounds = bounds.translated(pet.pos())
            self.assertTrue(QRectF(self.area).adjusted(-1, -1, 1, 1).contains(global_bounds),
                            f"Painted content escaped work area: {global_bounds}")

    def complete(self, pet, player, *, check_bounds=True):
        positions = []
        for _ in range(600):
            pet._advance_frame(1 / 60)
            positions.append(QPoint(pet.pos()))
            if check_bounds:
                self.assert_painted_inside(pet)
            if not player.busy:
                return positions
        self.fail("Playback did not complete within the bounded test clock")

    def test_neutral_first_frame_does_not_jump_to_whole_clip_clearance(self):
        pet, player = self.make_player()
        home = self.at_edge(pet)
        self.assertTrue(player.play("idle_balloon_float", once=True))
        self.assertEqual(pet.pos(), home, "Installing a future prop moved the neutral first frame")
        self.assert_painted_inside(pet)

    def test_natural_completion_has_no_static_tail_glide_or_pending_recovery(self):
        pet, player = self.make_player()
        home = self.at_edge(pet)
        self.assertTrue(player.play("idle_balloon_float", once=True))
        positions = self.complete(pet, player)
        self.assertTrue(any(point != home for point in positions), "Fixture must require temporary clearance")
        self.assertEqual(pet.pos(), home, "Position must be restored before the clip is declared finished")
        self.assertIsNone(pet._edge_recovery, "Static pixels must not start another recovery after playback")
        self.assertFalse(pet._sequence_frames)
        for _ in range(30):
            pet._advance_frame(1 / 60)
            self.assertEqual(pet.pos(), home, "A finished clip is still moving the static pet")

    def test_clearance_motion_happens_during_the_clip_and_keeps_actual_pixels_visible(self):
        pet, player = self.make_player(scale=200)
        home = self.at_edge(pet)
        self.assertTrue(player.play("idle_balloon_float", once=True))
        moved_before_end = False
        for _ in range(600):
            pet._advance_frame(1 / 60)
            self.assert_painted_inside(pet)
            if player.playing and pet.pos() != home:
                moved_before_end = True
            if not player.busy:
                break
        else:
            self.fail("Playback did not finish during the bounded visibility check")
        self.assertTrue(moved_before_end)
        self.assertEqual(pet.pos(), home)
        self.assertIsNone(pet._edge_recovery)

    def test_large_offset_manual_stop_relocates_only_when_the_pet_is_invisible(self):
        pet, player = self.make_player()
        home = self.at_edge(pet)
        self.assertTrue(player.play("idle_balloon_float"))
        for _ in range(180):
            pet._advance_frame(1 / 120)
            if (pet.pos() - home).manhattanLength() >= 35:
                break
        self.assertGreaterEqual((pet.pos() - home).manhattanLength(), 35)
        previous_pos = QPoint(pet.pos())
        _, previous_alpha = rendered_geometry(pet)
        self.assertGreater(previous_alpha, 250)
        player.stop(smooth=True)
        _, alpha = rendered_geometry(pet)
        if pet.pos() != previous_pos:
            self.assertLessEqual(alpha, 1, "Stop moved visible art before the fade-through reached blank")
        saw_partial = False
        saw_blank = alpha <= 1
        for _ in range(120):
            old_pos, old_alpha = QPoint(pet.pos()), alpha
            pet._advance_frame(1 / 120)
            _, alpha = rendered_geometry(pet)
            saw_partial |= 1 < alpha < 250
            saw_blank |= alpha <= 1
            if pet.pos() != old_pos:
                self.assertTrue(old_alpha <= 1 or alpha <= 1,
                                "Manual stop spatially moved a visible silhouette")
            self.assert_painted_inside(pet)
            if not player.busy:
                break
        self.assertTrue(saw_partial, "Manual stop should fade rather than hard-cut")
        self.assertTrue(saw_blank, "A large-distance return needs an actual invisible handoff")
        self.assertFalse(player.busy)
        self.assertEqual(pet.pos(), home)
        self.assertIsNone(pet._edge_recovery)

    def test_drag_freezes_the_plan_and_completion_respects_the_new_drop(self):
        pet, player = self.make_player()
        original_home = self.at_edge(pet)
        self.assertTrue(player.play("idle_balloon_float", once=True))
        pet._advance_frame(.3)
        local = pet.character_transform.map(QPointF(
            pet.sprite_rect.x() + pet._hit_image.width() * .5,
            pet.sprite_rect.y() + pet._hit_image.height() * .6,
        )).toPoint()
        self.assertTrue(pet.character_contains(local))
        start = pet.mapToGlobal(local)
        end = self.area.center()

        def send(kind, position, held):
            button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
            event = QMouseEvent(kind, QPointF(pet.mapFromGlobal(position)), QPointF(position), button,
                                Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier)
            with patch("desktop_pet.pet_window.screen_for_point", return_value=None):
                QApplication.sendEvent(pet, event)

        send(QEvent.Type.MouseButtonPress, start, True)
        send(QEvent.Type.MouseMove, end, True)
        frozen_pos, frozen_index, frozen_pose = QPoint(pet.pos()), pet._sequence_index, pet.animation.pose
        pet._advance_frame(.8)
        self.assertEqual(pet.animation.state, AnimationState.DRAGGING)
        self.assertEqual(pet.pos(), frozen_pos)
        self.assertEqual(pet._sequence_index, frozen_index)
        self.assertEqual(pet.animation.pose, frozen_pose)
        send(QEvent.Type.MouseButtonRelease, end, False)
        pet._advance_frame(pet.config.animation.return_duration + .02)
        dropped = QPoint(pet.pos())
        self.assertNotEqual(dropped, original_home)
        self.complete(pet, player)
        self.assertEqual(pet.pos(), dropped)
        self.assertIsNone(pet._edge_recovery)

    def test_all_installed_idle_endpoints_keep_home_at_two_sizes(self):
        directory = PROJECT_ROOT / "assets/character/animations"
        for name in DEFAULT_IDLE_ACTIONS:
            manifest = read_manifest(directory, name)
            indices = sorted({round(i * (len(manifest.paths) - 1) / 6) for i in range(7)})
            paths = tuple(manifest.paths[i] for i in indices)
            frames = tuple(QImage(str(path)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                           for path in paths)
            self.assertTrue(all(not frame.isNull() for frame in frames))
            sampled = LoadedClip(replace(manifest, paths=paths), frames)
            for scale in (100, 200):
                with self.subTest(name=name, scale=scale):
                    pet, player = self.make_player(
                        scale=scale, source=PROJECT_ROOT / "assets/expressions/neutral.png",
                        directory=directory, clip=sampled,
                    )
                    home = self.at_edge(pet)
                    self.assertTrue(player.play(name, once=True))
                    self.assertEqual(pet.pos(), home)
                    self.complete(pet, player)
                    self.assertEqual(pet.pos(), home)
                    self.assertIsNone(pet._edge_recovery)
                    player.shutdown()
                    pet.close()
                    self.players.remove(player)
                    self.windows.remove(pet)
                    pet.deleteLater()
                    self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    self.app.processEvents()
                    # Restore the prior global decoder patch before installing
                    # the next pack, and promptly release large Qt image caches.
                    for _ in range(2):
                        self.patches.pop().stop()


if __name__ == "__main__":
    unittest.main()
